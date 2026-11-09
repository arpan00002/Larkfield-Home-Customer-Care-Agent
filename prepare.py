#!/usr/bin/env python3
"""Reproducibly prepare the Larkfield data room.

One command:

    python prepare.py

Reads the verbatim export in ``data_room/`` (or ``--data-room PATH``) and writes
``artifacts/normalized.json``.  It never edits the source files: every source
file is hashed before and after and the command fails if a byte changed.

Use ``--check`` to verify that the committed artifact matches what the current
source would produce (useful in CI / at deploy time).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_ROOM = ROOT / "data_room"
DEFAULT_OUT = ROOT / "artifacts" / "normalized.json"

SOURCE_FILES = [
    "data/customers.csv",
    "data/orders.csv",
    "data/order_items.csv",
    "data/refunds.csv",
    "data/shipments.csv",
    "policies/refund_policy_RP-3.2.md",
    "policies/help_centre_faq_v2.md",
    "policies/warehouse_picking_WH-PICK-1.4.md",
    "tickets/support_inbox_export.json",
    "tickets/eval_seed_labelled.json",
    "inbox/public_tickets.json",
    "schema/response_schema.py",
    "DATA_DICTIONARY.md",
]

MONEY_RE = re.compile(r"[^0-9.\-]")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path: Path) -> List[dict]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def money(value: Any) -> float:
    if value is None:
        return 0.0
    cleaned = MONEY_RE.sub("", str(value)).strip()
    if cleaned in ("", "-", "."):
        return 0.0
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return 0.0


def norm_order_id(value: Any) -> str:
    raw = re.sub(r"[^0-9]", "", str(value or ""))
    return f"LF-{raw}" if raw else ""


def norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-room", default=str(DEFAULT_ROOM))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    room = Path(args.data_room)
    out = Path(args.out)
    if not room.exists():
        print(f"ERROR: data room not found: {room}", file=sys.stderr)
        return 2

    before = {rel: sha256(room / rel) for rel in SOURCE_FILES if (room / rel).exists()}
    payload, findings = prepare(room)
    after = {rel: sha256(room / rel) for rel in before}

    changed = [rel for rel in before if before[rel] != after[rel]]
    if changed:
        print(f"ERROR: source files were modified while preparing: {changed}", file=sys.stderr)
        return 3

    payload["source_hashes"] = before
    payload["findings"] = findings
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if args.check:
        if not out.exists():
            print(f"ERROR: missing artifact {out}", file=sys.stderr)
            return 4
        if out.read_text(encoding="utf-8") != rendered:
            print(f"ERROR: {out} is stale; run `python prepare.py`", file=sys.stderr)
            return 5
        print(f"OK: {out} matches the data room")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    print(f"Wrote {out} ({len(rendered)} bytes)")
    for finding in findings:
        print(f"  - {finding}")
    return 0


def prepare(room: Path) -> Tuple[Dict[str, Any], List[str]]:
    findings: List[str] = []

    # ---- customers ---------------------------------------------------------
    customers: Dict[str, dict] = {}
    by_email: Dict[str, str] = {}
    for row in read_csv(room / "data/customers.csv"):
        cid = (row.get("customer_id") or "").strip()
        email = norm_email(row.get("email"))
        customers[cid] = {
            "customer_id": cid,
            "name": (row.get("name") or "").strip(),
            "email": email,
            "email_raw": row.get("email") or "",
            "country": (row.get("country") or "").strip(),
            "created_at": (row.get("created_at") or "").strip(),
            "loyalty_tier": (row.get("loyalty_tier") or "").strip(),
        }
        by_email[email] = cid

    # ---- orders ------------------------------------------------------------
    orders: Dict[str, dict] = {}
    duplicate_orders: List[str] = []
    for row in read_csv(room / "data/orders.csv"):
        oid = norm_order_id(row.get("order_id"))
        if not oid:
            continue
        if oid in orders:
            duplicate_orders.append(oid)
            continue
        orders[oid] = {
            "order_id": oid,
            "customer_id": (row.get("customer_id") or "").strip(),
            "order_date_raw": (row.get("order_date") or "").strip(),
            "status": (row.get("status") or "").strip(),
            "order_total": money(row.get("Order Total")),
            "shipping_fee": money(row.get("shipping_fee")),
            "currency": (row.get("currency") or "USD").strip(),
            "payment_method": (row.get("payment_method") or "").strip(),
        }
    if duplicate_orders:
        findings.append(
            "orders.csv contains duplicate order_id rows: "
            + ", ".join(sorted(set(duplicate_orders)))
            + " (kept first occurrence)"
        )

    # ---- order items -------------------------------------------------------
    items: Dict[str, List[dict]] = {}
    for row in read_csv(room / "data/order_items.csv"):
        oid = norm_order_id(row.get("order_id"))
        try:
            line_no = int(str(row.get("line_no") or "0").strip())
            qty = int(str(row.get("qty") or "0").strip())
        except ValueError:
            findings.append(f"order_items.csv has a non-numeric line/qty on {oid}")
            continue
        items.setdefault(oid, []).append(
            {
                "order_id": oid,
                "line_no": line_no,
                "sku": (row.get("sku") or "").strip(),
                "product_name": (row.get("product_name") or "").strip(),
                "qty": qty,
                "unit_price": money(row.get("unit_price")),
            }
        )
    for lines in items.values():
        lines.sort(key=lambda x: x["line_no"])

    orphan_items = sorted(set(items) - set(orders))
    if orphan_items:
        findings.append("order_items without an order row: " + ", ".join(orphan_items))
    missing_items = sorted(set(orders) - set(items))
    if missing_items:
        findings.append("orders without any order line: " + ", ".join(missing_items))

    # ---- refunds -----------------------------------------------------------
    refunds: Dict[str, List[dict]] = {}
    for row in read_csv(room / "data/refunds.csv"):
        oid = norm_order_id(row.get("order_id"))
        try:
            line_no = int(str(row.get("line_no") or "0").strip())
        except ValueError:
            continue
        refunds.setdefault(f"{oid}:{line_no}", []).append(
            {
                "refund_id": (row.get("refund_id") or "").strip(),
                "order_id": oid,
                "line_no": line_no,
                "amount": money(row.get("amount")),
                "status": (row.get("status") or "").strip(),
                "requested_at": (row.get("requested_at") or "").strip(),
                "approved_by": (row.get("approved_by") or "").strip(),
            }
        )

    # ---- shipments ---------------------------------------------------------
    shipments: Dict[str, List[dict]] = {}
    for row in read_csv(room / "data/shipments.csv"):
        oid = norm_order_id(row.get("order_id"))
        shipments.setdefault(oid, []).append(
            {
                "shipment_id": (row.get("shipment_id") or "").strip(),
                "order_id": oid,
                "carrier": (row.get("carrier") or "").strip(),
                "tracking_no": (row.get("tracking_no") or "").strip(),
                "status": (row.get("status") or "").strip(),
                "shipped_at": (row.get("shipped_at") or "").strip(),
                "delivered_at": (row.get("delivered_at") or "").strip(),
                "last_event_at": (row.get("last_event_at") or "").strip(),
            }
        )

    orphan_shipments = sorted(set(shipments) - set(orders))
    if orphan_shipments:
        findings.append("shipments without an order row: " + ", ".join(orphan_shipments))
    missing_shipments = sorted(set(orders) - set(shipments))
    if missing_shipments:
        findings.append("orders without a shipment row: " + ", ".join(missing_shipments))
    unavailable = sorted(
        oid
        for oid, rows in shipments.items()
        if any(r["status"] == "UNAVAILABLE" for r in rows)
    )
    if unavailable:
        findings.append(
            "carrier status UNAVAILABLE (cannot verify non-delivery): "
            + ", ".join(unavailable)
        )
    anomalies = []
    for oid, rows in shipments.items():
        for r in rows:
            if r["shipped_at"] and r["last_event_at"] and r["last_event_at"] < r["shipped_at"]:
                anomalies.append(f"{oid} last_event_at precedes shipped_at ({r['last_event_at']} < {r['shipped_at']})")
    if anomalies:
        findings.append("carrier timestamp anomalies: " + "; ".join(anomalies))

    # ---- policy consistency checks ----------------------------------------
    rp = (room / "policies/refund_policy_RP-3.2.md").read_text(encoding="utf-8")
    faq = (room / "policies/help_centre_faq_v2.md").read_text(encoding="utf-8")
    if "14 days" in faq and "30 days" in rp:
        findings.append(
            "policy conflict: help_centre_faq_v2 says 14-day returns and $250 auto-refunds; "
            "refund_policy_RP-3.2 (effective 2026-07-01, explicitly replaces earlier guidance) "
            "says 30/45 days and $150 authority. RP-3.2 is authoritative; FAQ is not used."
        )
    if "warehouse" in (room / "policies/warehouse_picking_WH-PICK-1.4.md").read_text(encoding="utf-8").lower():
        findings.append(
            "warehouse_picking_WH-PICK-1.4 is internal fulfilment guidance; it does not set "
            "customer refund/return rules and is excluded from the decision engine."
        )

    # ---- tickets sanity ----------------------------------------------------
    tickets_path = room / "tickets/support_inbox_export.json"
    if tickets_path.exists():
        tickets = json.loads(tickets_path.read_text(encoding="utf-8"))
        ids = [t.get("ticket_id") for t in tickets]
        forwarded = [t for t in tickets if str(t.get("subject", "")).lower().startswith("fwd:")]
        findings.append(
            f"tickets/support_inbox_export.json: {len(tickets)} emails, {len(set(ids))} unique ids, "
            f"{len(forwarded)} forwarded duplicates ({', '.join(t.get('ticket_id','?') for t in forwarded) or 'none'})"
        )
    public_path = room / "inbox/public_tickets.json"
    if public_path.exists():
        public = json.loads(public_path.read_text(encoding="utf-8"))
        findings.append(f"inbox/public_tickets.json: {len(public)} unlabelled tickets")

    return (
        {
            "schema_version": 1,
            "customers": customers,
            "customers_by_email": by_email,
            "orders": orders,
            "order_items": items,
            "refunds": refunds,
            "shipments": shipments,
        },
        findings,
    )


if __name__ == "__main__":
    raise SystemExit(main())
