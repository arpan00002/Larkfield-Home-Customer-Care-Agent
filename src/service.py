"""Orchestration: ticket -> decision -> draft -> trace -> run + ledger."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from . import parsing
from .data import DataRoom, get_data
from .llm import LLMClient
from .models import DraftReply, LedgerEntry, RunState, Ticket, TicketResponse, Trace
from .policy import PolicyResult, evaluate
from .replies import draft_reply
from .store import Store, get_store


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _action_for(result: PolicyResult) -> tuple[str, Optional[float], str]:
    """Return (action, amount, status) for the ledger."""
    if result.outcome == "PEND":
        return "REFUND", result.amount_usd, "PROPOSED"
    if result.outcome == "ESCALATE":
        return "ESCALATION", None, "PROPOSED"
    if result.outcome in ("PROCEED", "PROCEED_MODIFIED") and result.amount_usd:
        return "REFUND", result.amount_usd, "APPROVED"
    return "REPLY", None, "APPROVED"


def _status_for(result: PolicyResult) -> str:
    if result.outcome == "PEND":
        return "AWAITING_APPROVAL"
    if result.outcome == "ESCALATE":
        return "ESCALATED"
    return "COMPLETED"


def process_ticket(
    ticket: Ticket,
    data: Optional[DataRoom] = None,
    store: Optional[Store] = None,
) -> dict:
    started = time.perf_counter()
    data = data or get_data()
    store = store or get_store()

    # Idempotency: the same ticket_id always returns the same run.
    existing = store.get_by_ticket(ticket.ticket_id)
    if existing:
        return hydrate(existing, store)

    text = f"{ticket.subject or ''}\n{ticket.body or ''}"
    deterministic_order_id = parsing.extract_order_id(text)
    deterministic_order = (
        data.order(deterministic_order_id) if deterministic_order_id else None
    )

    llm = LLMClient()
    hints: Dict = {}
    model_calls = []
    llm_cost = 0.0
    if llm.should_assist(ticket, data, deterministic_order_id, deterministic_order):
        hints, model_calls, llm_cost = llm.assist(
            ticket, data, deterministic_order_id, deterministic_order
        )

    result = evaluate(
        ticket,
        data,
        duplicate_lookup=store.find_duplicate,
        hints=hints,
    )
    facts = dict(result.reply_facts)
    facts["reason_codes"] = list(result.reason_codes)
    draft = draft_reply(ticket, facts, result.outcome, result.ticket_type)

    tools = list(result.tools_called)
    if model_calls:
        tools.append("llm_extract")
    tools.append("draft_reply")
    trace = Trace(
        model_calls=model_calls,
        tools_called=tools,
        documents_used=result.documents_used,
        cost_usd=round(llm_cost, 8),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        assist_mode=llm.mode,
    )

    run_id = f"run_{uuid.uuid4().hex}"
    response = TicketResponse(
        ticket_id=ticket.ticket_id,
        run_id=run_id,
        ticket_type=result.ticket_type,  # type: ignore[arg-type]
        resolved=result.resolved,
        outcome=result.outcome,  # type: ignore[arg-type]
        reason_codes=result.reason_codes,
        checks=result.checks,
        human_approval_required=result.human_approval_required,
        approver_role=result.approver_role,  # type: ignore[arg-type]
        draft_reply=draft,
        trace=trace,
    )

    action, amount, ledger_status = _action_for(result)
    now = utcnow()
    store.add_ledger(
        run_id=run_id,
        ticket_id=ticket.ticket_id,
        action=action,
        amount_usd=amount,
        status=ledger_status,
        recorded_at=now,
    )

    state = response.model_dump()
    state.update(
        {
            "status": _status_for(result),
            "decisions": [],
            "ledger": [dict(x) for x in store.ledger(run_id)],
            "created_at": now,
            "decided_at": None,
            "duplicate_of_run_id": result.duplicate_of_run_id,
        }
    )
    row = store.create_run(
        run_id=run_id,
        ticket_id=ticket.ticket_id,
        response=state,
        status=state["status"],
        customer_id=result.resolved.customer_id,
        order_id=result.resolved.order_id,
        line_nos=[
            int(line["line_no"])
            for line in (result.reply_facts.get("lines") or [])
            if line.get("line_no") is not None and float(line.get("new_amount") or 0) > 0
        ],
        created_at=now,
    )
    # A concurrent duplicate POST may have won the unique ticket_id race.
    if row["run_id"] != run_id:
        return hydrate(row, store)
    return hydrate(row, store)


def hydrate(row: dict, store: Store) -> dict:
    state = json.loads(row["response_json"])
    state["status"] = row["status"]
    state["decided_at"] = row.get("decided_at")
    state["ledger"] = [dict(x) for x in store.ledger(row["run_id"])]
    state["decisions"] = store.decisions(row["run_id"])
    # Validate/normalise through the model; extra fields are documented.
    return RunState(**state).model_dump()


def get_run(run_id: str, store: Optional[Store] = None) -> Optional[dict]:
    store = store or get_store()
    row = store.get_run(run_id)
    return hydrate(row, store) if row else None


def decide(
    run_id: str, decision: str, approver: str, store: Optional[Store] = None
) -> Optional[dict]:
    store = store or get_store()
    row = store.get_run(run_id)
    if not row:
        return None
    state = json.loads(row["response_json"])
    now = utcnow()
    store.add_decision(run_id, decision, approver, now)

    # First decision wins; later decisions are audited but cannot record a
    # second action.
    if row.get("decided_at"):
        return hydrate(store.get_run(run_id), store)  # type: ignore[arg-type]

    if not state.get("human_approval_required"):
        state["decisions"] = store.decisions(run_id)
        store.update_run(run_id, state, row["status"])
        return hydrate(store.get_run(run_id), store)  # type: ignore[arg-type]

    ledger_status = "APPROVED" if decision == "approve" else "REJECTED"
    store.update_ledger(run_id, ledger_status, approver, now)
    state["status"] = ledger_status
    state["decided_at"] = now
    state["human_approval_required"] = False
    state["decisions"] = store.decisions(run_id)
    state["ledger"] = [dict(x) for x in store.ledger(run_id)]
    store.update_run(run_id, state, ledger_status, decided_at=now)
    return hydrate(store.get_run(run_id), store)  # type: ignore[arg-type]


def ledger(store: Optional[Store] = None) -> list:
    store = store or get_store()
    return [LedgerEntry(**x).model_dump() for x in store.ledger()]
