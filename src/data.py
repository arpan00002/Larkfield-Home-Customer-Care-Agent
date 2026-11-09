"""Load the normalised data room produced by ``python prepare.py``."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "artifacts" / "normalized.json"


class DataRoom:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.customers: Dict[str, dict] = payload["customers"]
        self.customers_by_email: Dict[str, str] = payload["customers_by_email"]
        self.orders: Dict[str, dict] = payload["orders"]
        self.order_items: Dict[str, List[dict]] = payload["order_items"]
        self.refunds: Dict[str, List[dict]] = payload["refunds"]
        self.shipments: Dict[str, List[dict]] = payload["shipments"]
        self.findings: List[str] = payload.get("findings", [])

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "DataRoom":
        artifact = Path(path or os.environ.get("NORMALIZED_DATA", ARTIFACT))
        if not artifact.exists():
            raise FileNotFoundError(
                f"{artifact} not found. Run `python prepare.py` first."
            )
        with artifact.open("r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def customer_by_email(self, email: str) -> Optional[dict]:
        cid = self.customers_by_email.get((email or "").strip().lower())
        return self.customers.get(cid) if cid else None

    def customer(self, customer_id: str) -> Optional[dict]:
        return self.customers.get(customer_id)

    def order(self, order_id: str) -> Optional[dict]:
        return self.orders.get(order_id)

    def lines(self, order_id: str) -> List[dict]:
        return self.order_items.get(order_id, [])

    def shipment(self, order_id: str) -> Optional[dict]:
        rows = self.shipments.get(order_id) or []
        # Prefer a delivered/active row; data room has one row per order in practice.
        for row in rows:
            if row.get("status") in ("DELIVERED", "IN_TRANSIT"):
                return row
        return rows[0] if rows else None

    def prior_refunds(self, order_id: str, line_no: int) -> List[dict]:
        return [
            r
            for r in self.refunds.get(f"{order_id}:{line_no}", [])
            if r.get("status") == "ISSUED"
        ]


_CACHE: Optional[DataRoom] = None


def get_data() -> DataRoom:
    global _CACHE
    if _CACHE is None:
        _CACHE = DataRoom.load()
    return _CACHE
