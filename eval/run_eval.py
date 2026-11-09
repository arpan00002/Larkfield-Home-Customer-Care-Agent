#!/usr/bin/env python3
"""Run the Larkfield evaluation suite.

Pass rule (stated in ARTEFACT.md):

A case passes only when ALL of the following hold:
  1. outcome, human_approval_required and approver_role exactly match expected;
  2. expected reason_codes (when supplied) match as a set;
  3. expected amount_usd matches the REFUND ledger amount (when supplied);
  4. expected ledger action/status is present (when supplied);
  5. no_refund cases have no REFUND ledger entry;
  6. duplicate cases have duplicate_of_run_id set and no REFUND entry;
  7. contract invariants hold: required fields exist, outcomes/check results are
     valid, an UNABLE_TO_VERIFY outcome has at least one UNABLE_TO_VERIFY check,
     PEND/ESCALATE require a human + role, and a refund amount in the ledger is
     also present in the draft reply.

Usage:
    python eval/run_eval.py --variant before --change "damage photo exception disabled"
    python eval/run_eval.py --variant after  --change "enabled RP-3.2 2.6 photo exception"
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CASES = ROOT / "eval" / "cases.json"
RESULTS = ROOT / "eval" / "results"

VALID_OUTCOMES = {
    "PROCEED", "PROCEED_MODIFIED", "PEND", "ESCALATE", "REJECT",
    "INVALID_REQUEST", "UNABLE_TO_VERIFY",
}
VALID_CHECK_RESULTS = {"PASS", "FAIL", "NOT_APPLICABLE", "UNABLE_TO_VERIFY"}
VALID_REASONS = {
    "DAMAGE_EXCEPTION", "PARTIAL_LINES", "ALREADY_REFUNDED", "APPROVAL_REQUIRED",
    "OUT_OF_WINDOW", "ORDER_NOT_FOUND", "CUSTOMER_MISMATCH",
    "CARRIER_STATUS_UNAVAILABLE", "LOST_IN_TRANSIT", "POLICY_OVERRIDE_ATTEMPT",
}


def money(value: float) -> str:
    return f"${value:,.2f}"


def check_case(case: dict, run: dict) -> List[str]:
    failures: List[str] = []
    exp = case["expected"]
    ledger = run.get("ledger") or []

    if run.get("outcome") != exp["outcome"]:
        failures.append(f"outcome={run.get('outcome')} expected={exp['outcome']}")
    if run.get("human_approval_required") != exp["human_approval_required"]:
        failures.append(
            f"human_approval_required={run.get('human_approval_required')} "
            f"expected={exp['human_approval_required']}"
        )
    if run.get("approver_role") != exp["approver_role"]:
        failures.append(
            f"approver_role={run.get('approver_role')} expected={exp['approver_role']}"
        )

    if "reason_codes" in exp:
        got = set(run.get("reason_codes") or [])
        want = set(exp["reason_codes"])
        if got != want:
            failures.append(f"reason_codes={sorted(got)} expected={sorted(want)}")

    refunds = [x for x in ledger if x.get("action") == "REFUND"]
    if "amount_usd" in exp:
        amounts = [x.get("amount_usd") for x in refunds]
        if exp["amount_usd"] not in amounts:
            failures.append(f"refund_amounts={amounts} expected={exp['amount_usd']}")

    if exp.get("no_refund") and refunds:
        failures.append(f"unexpected REFUND ledger entry: {refunds}")

    if exp.get("ledger_action"):
        wanted = [
            x for x in ledger
            if x.get("action") == exp["ledger_action"]
            and (not exp.get("ledger_status") or x.get("status") == exp["ledger_status"])
        ]
        if not wanted:
            failures.append(
                f"ledger has no {exp['ledger_action']}/{exp.get('ledger_status')}: {ledger}"
            )

    if exp.get("duplicate"):
        if not run.get("duplicate_of_run_id"):
            failures.append("expected duplicate_of_run_id, got none")
        if refunds:
            failures.append("duplicate run recorded a second REFUND")

    # ---- contract invariants ------------------------------------------------
    required = [
        "ticket_id", "run_id", "ticket_type", "resolved", "outcome", "reason_codes",
        "checks", "human_approval_required", "approver_role", "draft_reply", "trace",
    ]
    for key in required:
        if key not in run:
            failures.append(f"missing field {key}")
    if run.get("outcome") not in VALID_OUTCOMES:
        failures.append(f"invalid outcome {run.get('outcome')}")
    for chk in run.get("checks") or []:
        if chk.get("result") not in VALID_CHECK_RESULTS:
            failures.append(f"invalid check result {chk.get('result')} on {chk.get('check_id')}")
        if not chk.get("policy_clause"):
            failures.append(f"check {chk.get('check_id')} has no policy_clause")
    unknown = set(run.get("reason_codes") or []) - VALID_REASONS
    if unknown:
        failures.append(f"reason codes not defined in policies/: {sorted(unknown)}")
    if run.get("outcome") == "UNABLE_TO_VERIFY":
        if not any(c.get("result") == "UNABLE_TO_VERIFY" for c in run.get("checks") or []):
            failures.append("UNABLE_TO_VERIFY outcome without an UNABLE_TO_VERIFY check")
    if run.get("outcome") in ("PEND", "ESCALATE"):
        if not run.get("human_approval_required"):
            failures.append("PEND/ESCALATE without human_approval_required")
        if not run.get("approver_role"):
            failures.append("PEND/ESCALATE without approver_role")
    else:
        if run.get("human_approval_required"):
            failures.append("human_approval_required true for a non-human outcome")
        if run.get("approver_role"):
            failures.append("approver_role set for a non-human outcome")
    for entry in refunds:
        if entry.get("amount_usd") is not None:
            if money(float(entry["amount_usd"])) not in (run.get("draft_reply") or {}).get("body", ""):
                failures.append(
                    f"draft reply does not contain refund amount {money(float(entry['amount_usd']))}"
                )
    if (run.get("draft_reply") or {}).get("to") != case["ticket"]["from"].strip():
        failures.append("draft_reply.to does not match sender")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--out", default="")
    parser.add_argument("--variant", default="run")
    parser.add_argument("--change", default="")
    parser.add_argument("--llm", default="off", choices=["off", "auto", "on"])
    args = parser.parse_args()

    os.environ["LLM_ASSIST"] = args.llm
    os.environ.setdefault("NORMALIZED_DATA", str(ROOT / "artifacts" / "normalized.json"))
    os.environ["STORE_PATH"] = str(
        Path(tempfile.gettempdir()) / f"larkfield_eval_{os.getpid()}.sqlite3"
    )
    try:
        Path(os.environ["STORE_PATH"]).unlink()
    except FileNotFoundError:
        pass

    # Import after env is set so the service sees the eval configuration.
    from src.models import Ticket
    from src.service import process_ticket
    from src.store import Store

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    store = Store(os.environ["STORE_PATH"])
    started = datetime.now(timezone.utc)
    results = []
    latencies = []
    costs = []

    for case in cases:
        t0 = time.perf_counter()
        try:
            run = process_ticket(Ticket(**case["ticket"]), store=store)
        except Exception as exc:  # a crash is a failed case, not a failed run
            run = {}
            failures = [f"exception: {exc}"]
        else:
            failures = check_case(case, run)
        wall_ms = round((time.perf_counter() - t0) * 1000, 2)
        latencies.append(wall_ms)
        costs.append(float((run.get("trace") or {}).get("cost_usd") or 0.0))
        results.append(
            {
                "case_id": case["case_id"],
                "kind": case["kind"],
                "negative_kind": case.get("negative_kind"),
                "passed": not failures,
                "failures": failures,
                "expected": case["expected"],
                "actual": {
                    "outcome": run.get("outcome"),
                    "human_approval_required": run.get("human_approval_required"),
                    "approver_role": run.get("approver_role"),
                    "reason_codes": run.get("reason_codes"),
                    "resolved": run.get("resolved"),
                    "ledger": run.get("ledger"),
                    "duplicate_of_run_id": run.get("duplicate_of_run_id"),
                    "trace": run.get("trace"),
                },
                "wall_latency_ms": wall_ms,
            }
        )

    passed = sum(1 for r in results if r["passed"])
    by_kind: Dict[str, dict] = {}
    for r in results:
        key = r["negative_kind"] or r["kind"]
        bucket = by_kind.setdefault(key, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r["passed"])

    def p95(values: List[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
        return round(ordered[idx], 2)

    summary = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "median_cost_usd": round(statistics.median(costs), 8) if costs else 0.0,
        "mean_cost_usd": round(statistics.mean(costs), 8) if costs else 0.0,
        "median_latency_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "p95_latency_ms": p95(latencies),
        "by_kind": by_kind,
        "failure_counts": dict(Counter(r["negative_kind"] or r["kind"] for r in results if not r["passed"])),
    }

    stamp = started.strftime("%Y-%m-%dT%H-%M-%SZ")
    out = Path(args.out) if args.out else RESULTS / f"{stamp}_{args.variant}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": started.isoformat(),
        "variant": args.variant,
        "change": args.change,
        "config": {"llm_assist": args.llm, "cases": str(Path(args.cases).name)},
        "summary": summary,
        "cases": results,
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{args.variant}: {passed}/{len(results)} passed ({summary['pass_rate']*100:.1f}%)")
    print(
        f"median cost ${summary['median_cost_usd']:.8f}/request; "
        f"median latency {summary['median_latency_ms']} ms; p95 {summary['p95_latency_ms']} ms"
    )
    for kind, bucket in sorted(by_kind.items()):
        print(f"  {kind}: {bucket['passed']}/{bucket['total']}")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['case_id']}: {'; '.join(r['failures'])}")
    print(f"Wrote {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
