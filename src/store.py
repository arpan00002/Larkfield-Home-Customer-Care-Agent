"""Durable run + ledger store (SQLite).

Runs survive a process restart; the ledger enforces one action per run with a
unique (run_id, action) constraint, so repeated decisions cannot record a
second action.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "artifacts" / "runs.sqlite3"
_LOCK = threading.RLock()


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = str(path or os.environ.get("STORE_PATH", DEFAULT_PATH))
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self) -> None:
        with _LOCK, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    human_approval_required INTEGER NOT NULL,
                    customer_id TEXT,
                    order_id TEXT,
                    line_nos TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS runs_ticket_id ON runs(ticket_id);
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    amount_usd REAL,
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    approver TEXT,
                    UNIQUE(run_id, action)
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    def get_by_ticket(self, ticket_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def create_run(
        self,
        *,
        run_id: str,
        ticket_id: str,
        response: dict,
        status: str,
        customer_id: Optional[str],
        order_id: Optional[str],
        line_nos: List[int],
        created_at: str,
    ) -> dict:
        with _LOCK, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs
                (run_id, ticket_id, response_json, status, outcome, human_approval_required,
                 customer_id, order_id, line_nos, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    ticket_id,
                    json.dumps(response, ensure_ascii=False),
                    status,
                    response.get("outcome", ""),
                    1 if response.get("human_approval_required") else 0,
                    customer_id,
                    order_id,
                    json.dumps(line_nos),
                    created_at,
                ),
            )
        row = self.get_run(run_id)
        assert row is not None
        return row

    def update_run(self, run_id: str, response: dict, status: str, decided_at: Optional[str] = None) -> dict:
        with _LOCK, self._connect() as conn:
            if decided_at:
                conn.execute(
                    "UPDATE runs SET response_json=?, status=?, outcome=?, decided_at=? WHERE run_id=?",
                    (json.dumps(response, ensure_ascii=False), status, response.get("outcome", ""), decided_at, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET response_json=?, status=?, outcome=? WHERE run_id=?",
                    (json.dumps(response, ensure_ascii=False), status, response.get("outcome", ""), run_id),
                )
        row = self.get_run(run_id)
        assert row is not None
        return row

    # ------------------------------------------------------------------
    def add_ledger(
        self,
        *,
        run_id: str,
        ticket_id: str,
        action: str,
        amount_usd: Optional[float],
        status: str,
        recorded_at: str,
        approver: Optional[str] = None,
    ) -> None:
        with _LOCK, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO ledger
                (run_id, ticket_id, action, amount_usd, status, recorded_at, approver)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, ticket_id, action, amount_usd, status, recorded_at, approver),
            )

    def update_ledger(
        self, run_id: str, status: str, approver: Optional[str], recorded_at: str
    ) -> None:
        with _LOCK, self._connect() as conn:
            conn.execute(
                "UPDATE ledger SET status=?, approver=?, recorded_at=? "
                "WHERE run_id=? AND status='PROPOSED'",
                (status, approver, recorded_at, run_id),
            )

    def ledger(self, run_id: Optional[str] = None) -> List[dict]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT run_id, ticket_id, action, amount_usd, status, recorded_at "
                    "FROM ledger WHERE run_id=? ORDER BY id",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, ticket_id, action, amount_usd, status, recorded_at "
                    "FROM ledger ORDER BY id"
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def add_decision(self, run_id: str, decision: str, approver: str, recorded_at: str) -> None:
        with _LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO decisions (run_id, decision, approver, recorded_at) VALUES (?, ?, ?, ?)",
                (run_id, decision, approver, recorded_at),
            )

    def decisions(self, run_id: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT decision, approver, recorded_at FROM decisions WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def find_duplicate(self, order_id: str, line_nos: List[int], customer_id: str) -> Optional[str]:
        """An earlier live refund action for the same customer/order/lines."""
        if not line_nos:
            return None
        wanted = set(line_nos)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.line_nos FROM runs r
                JOIN ledger l ON l.run_id = r.run_id
                WHERE r.order_id = ? AND r.customer_id = ?
                  AND l.action = 'REFUND' AND l.status IN ('PROPOSED','APPROVED')
                ORDER BY r.created_at ASC
                """,
                (order_id, customer_id),
            ).fetchall()
        for row in rows:
            try:
                existing = set(json.loads(row["line_nos"] or "[]"))
            except Exception:
                existing = set()
            if existing & wanted:
                return row["run_id"]
        return None


_STORE: Optional[Store] = None


def get_store() -> Store:
    global _STORE
    if _STORE is None:
        _STORE = Store()
    return _STORE
