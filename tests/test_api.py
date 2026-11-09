"""Contract smoke tests for the public HTTP service.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["LLM_ASSIST"] = "off"
os.environ["DAMAGE_EXCEPTION_ENABLED"] = "1"
os.environ["STORE_PATH"] = str(Path(tempfile.gettempdir()) / f"larkfield_test_{os.getpid()}.sqlite3")
try:
    Path(os.environ["STORE_PATH"]).unlink()
except FileNotFoundError:
    pass

from fastapi.testclient import TestClient  # noqa: E402

from src.app import app  # noqa: E402


def public_ticket(ticket_id: str) -> dict:
    tickets = json.loads((ROOT / "data_room/inbox/public_tickets.json").read_text(encoding="utf-8"))
    return next(t for t in tickets if t["ticket_id"] == ticket_id)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_post_ticket_and_get_run(self):
        r = self.client.post("/tickets", json=public_ticket("P01"))
        self.assertEqual(r.status_code, 200, r.text)
        run = r.json()
        self.assertEqual(run["ticket_id"], "P01")
        self.assertEqual(run["outcome"], "PROCEED")
        self.assertTrue(run["run_id"].startswith("run_"))
        self.assertEqual(run["resolved"]["order_id"], "LF-100225")
        self.assertEqual(run["resolved"]["customer_id"], "C10027")
        self.assertEqual(run["ledger"][0]["action"], "REFUND")
        self.assertEqual(run["ledger"][0]["amount_usd"], 64.0)

        r2 = self.client.get(f"/runs/{run['run_id']}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["run_id"], run["run_id"])

    def test_same_ticket_id_is_idempotent(self):
        first = self.client.post("/tickets", json=public_ticket("P07")).json()
        second = self.client.post("/tickets", json=public_ticket("P07")).json()
        self.assertEqual(first["run_id"], second["run_id"])

    def test_pend_approve_records_once(self):
        run = self.client.post("/tickets", json=public_ticket("P02")).json()
        self.assertEqual(run["outcome"], "PEND")
        self.assertTrue(run["human_approval_required"])
        self.assertEqual(run["ledger"][0]["status"], "PROPOSED")
        run_id = run["run_id"]

        r = self.client.post(
            f"/runs/{run_id}/decision", json={"decision": "approve", "approver": "lead@example"}
        )
        self.assertEqual(r.status_code, 200)
        approved = r.json()
        self.assertEqual(approved["status"], "APPROVED")
        self.assertEqual(approved["ledger"][0]["status"], "APPROVED")
        self.assertEqual(len(approved["ledger"]), 1)

        # A second decision (even reject) must not record another action.
        r = self.client.post(
            f"/runs/{run_id}/decision", json={"decision": "reject", "approver": "other@example"}
        )
        self.assertEqual(r.status_code, 200)
        again = r.json()
        self.assertEqual(again["ledger"][0]["status"], "APPROVED")
        self.assertEqual(len(again["ledger"]), 1)
        self.assertEqual(len(again["decisions"]), 2)  # audited, not applied twice

        ledger = self.client.get("/ledger").json()
        matching = [x for x in ledger if x["run_id"] == run_id and x["action"] == "REFUND"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "APPROVED")

    def test_escalation_is_not_a_refund(self):
        run = self.client.post("/tickets", json=public_ticket("P05")).json()
        self.assertEqual(run["outcome"], "ESCALATE")
        self.assertEqual(run["approver_role"], "TRUST_SAFETY")
        self.assertFalse(any(x["action"] == "REFUND" for x in run["ledger"]))

    def test_unable_to_verify_never_has_pass_check_for_that_fact(self):
        run = self.client.post("/tickets", json=public_ticket("P09")).json()
        self.assertEqual(run["outcome"], "UNABLE_TO_VERIFY")
        carrier = [c for c in run["checks"] if c["check_id"] == "CARRIER_STATUS"]
        self.assertEqual(carrier[0]["result"], "UNABLE_TO_VERIFY")

    def test_unknown_run_404(self):
        self.assertEqual(self.client.get("/runs/run_missing").status_code, 404)


if __name__ == "__main__":
    unittest.main()
