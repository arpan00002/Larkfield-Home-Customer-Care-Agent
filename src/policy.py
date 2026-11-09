"""Deterministic policy engine for Larkfield Home Customer Care.

The engine implements refund_policy_RP-3.2.md.  It never executes an action:
it produces a decision, checks, reason codes and a draft reply.  A language
model may assist extraction elsewhere, but no model output can override a
policy result here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import parsing
from .data import DataRoom
from .models import Check, DocumentUsed, Evidence, Resolved, Ticket

POLICY_DOC = "refund_policy_RP-3.2.md"
POLICY_VERSION = "3.2"

REASON_ORDER = [
    "DAMAGE_EXCEPTION",
    "PARTIAL_LINES",
    "ALREADY_REFUNDED",
    "APPROVAL_REQUIRED",
    "OUT_OF_WINDOW",
    "ORDER_NOT_FOUND",
    "CUSTOMER_MISMATCH",
    "CARRIER_STATUS_UNAVAILABLE",
    "LOST_IN_TRANSIT",
    "POLICY_OVERRIDE_ATTEMPT",
]


@dataclass
class PolicyResult:
    ticket_type: str
    resolved: Resolved
    outcome: str
    reason_codes: List[str] = field(default_factory=list)
    checks: List[Check] = field(default_factory=list)
    human_approval_required: bool = False
    approver_role: Optional[str] = None
    amount_usd: Optional[float] = None
    reply_facts: Dict = field(default_factory=dict)
    documents_used: List[DocumentUsed] = field(
        default_factory=lambda: [DocumentUsed(document=POLICY_DOC, version=POLICY_VERSION)]
    )
    tools_called: List[str] = field(default_factory=list)
    duplicate_of_run_id: Optional[str] = None

    def add_check(
        self,
        check_id: str,
        result: str,
        clause: Optional[str],
        evidence: Optional[List[Evidence]] = None,
    ) -> None:
        self.checks.append(
            Check(
                check_id=check_id,
                result=result,  # type: ignore[arg-type]
                policy_clause=clause,
                evidence=evidence or [],
            )
        )

    def reason(self, code: str) -> None:
        if code not in self.reason_codes:
            self.reason_codes.append(code)

    def tool(self, name: str) -> None:
        if name not in self.tools_called:
            self.tools_called.append(name)


def _ev(source: str, ref: str, value: object) -> Evidence:
    return Evidence(source=source, ref=str(ref), value=str(value))


def _money(value: float) -> str:
    return f"${value:,.2f}"


def evaluate(
    ticket: Ticket,
    data: DataRoom,
    duplicate_lookup: Optional[Callable[[str, List[int], str], Optional[str]]] = None,
    hints: Optional[Dict] = None,
) -> PolicyResult:
    hints = hints or {}
    body = ticket.body or ""
    text = f"{ticket.subject or ''}\n{body}"
    sender = parsing.normalize_email(ticket.from_)

    damage = parsing.has_damage(text) or bool(hints.get("damage"))
    wrong = parsing.has_wrong_item(text) or bool(hints.get("wrong_item"))
    return_ = parsing.has_return(text) or bool(hints.get("return"))
    refund = parsing.has_refund(text) or bool(hints.get("refund"))
    non_delivery = parsing.has_non_delivery(text) or bool(hints.get("non_delivery"))
    status = parsing.has_order_status(text) or bool(hints.get("order_status"))
    shipping = parsing.asks_shipping(text)
    whole_order = parsing.asks_whole_order(text)
    photo = parsing.has_photo([a.model_dump() for a in ticket.attachments])
    override = parsing.has_policy_override(text) or bool(hints.get("policy_override"))

    ticket_type = parsing.classify_ticket_type(
        body,
        non_delivery=non_delivery,
        status=status,
        damage=damage,
        wrong=wrong,
        return_=return_,
        refund=refund,
        shipping=shipping,
    )
    if ticket_type == "OTHER" and hints.get("ticket_type") in (
        "REFUND",
        "RETURN",
        "ORDER_STATUS",
        "DELIVERY_ISSUE",
    ):
        ticket_type = hints["ticket_type"]

    result = PolicyResult(
        ticket_type=ticket_type,
        resolved=Resolved(order_id=None, customer_id=None),
        outcome="PROCEED",
    )
    for name in ("normalize_sender", "extract_order_id", "lookup_customer", "lookup_order"):
        result.tool(name)
    result.reply_facts.update(
        {
            "sender": sender,
            "customer_name": "",
            "order_id": None,
            "ticket_type": ticket_type,
            "damage": damage,
            "photo": photo,
        }
    )

    # ---- identity ---------------------------------------------------------
    order_id = parsing.extract_order_id(text) or hints.get("order_id")
    customer = data.customer_by_email(sender)
    order = data.order(order_id) if order_id else None

    if order:
        result.resolved = Resolved(order_id=order["order_id"], customer_id=order["customer_id"])
        result.reply_facts["order_id"] = order["order_id"]
    if customer:
        result.reply_facts["customer_name"] = customer.get("name", "")

    result.add_check(
        "ORDER_IDENTIFIED",
        "PASS" if order else ("FAIL" if order_id else "UNABLE_TO_VERIFY"),
        "RP-3.2 §4.1",
        [
            _ev(
                "ticket",
                "body",
                f"extracted order_id={order_id or 'none'}; exists={bool(order)}",
            )
        ],
    )
    if not order:
        result.add_check(
            "CUSTOMER_MATCH",
            "NOT_APPLICABLE",
            "RP-3.2 §4.1",
            [_ev("data/customers.csv", sender or "empty", "no order to match")],
        )
    else:
        match = bool(customer and customer["customer_id"] == order["customer_id"])
        result.add_check(
            "CUSTOMER_MATCH",
            "PASS" if match else "FAIL",
            "RP-3.2 §4.1",
            [
                _ev(
                    "data/customers.csv",
                    sender or "empty",
                    f"sender_customer={customer['customer_id'] if customer else 'not found'}; "
                    f"order_customer={order['customer_id']}",
                )
            ],
        )

    result.add_check(
        "POLICY_OVERRIDE",
        "FAIL" if override else "PASS",
        "RP-3.2 §5.2",
        [_ev("ticket", "body", "instruction/override language detected" if override else "none")],
    )

    # ---- policy override takes precedence over any refund -----------------
    if override:
        result.outcome = "ESCALATE"
        result.reason("POLICY_OVERRIDE_ATTEMPT")
        result.human_approval_required = True
        result.approver_role = "TRUST_SAFETY"
        result.reply_facts.update({"outcome": result.outcome, "approver_role": "TRUST_SAFETY"})
        return result

    # ---- identity failures -------------------------------------------------
    if order_id and not order:
        result.outcome = "INVALID_REQUEST"
        result.reason("ORDER_NOT_FOUND")
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    if order and (not customer or customer["customer_id"] != order["customer_id"]):
        result.outcome = "INVALID_REQUEST"
        result.reason("CUSTOMER_MISMATCH")
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    # ---- no order id -------------------------------------------------------
    if not order_id:
        actionable = refund or return_ or damage or wrong or non_delivery
        if not actionable:
            result.outcome = "PROCEED"
            result.ticket_type = "OTHER"
            result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
            return result
        result.outcome = "UNABLE_TO_VERIFY"
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    # From here the order and sender are established.
    assert order is not None
    lines = data.lines(order["order_id"])
    shipment = data.shipment(order["order_id"])
    result.tool("lookup_order_items")
    result.tool("lookup_shipments")

    # ---- delivery / order-status path -------------------------------------
    if ticket_type in ("ORDER_STATUS", "DELIVERY_ISSUE"):
        return _delivery_decision(
            result, order, shipment, ticket, non_delivery=non_delivery, refund=refund
        )

    # ---- refund / return / other path -------------------------------------
    if ticket_type == "OTHER":
        result.outcome = "PROCEED"
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    return _refund_decision(
        result,
        data,
        ticket,
        order,
        lines,
        shipment,
        body=body,
        damage=damage,
        wrong=wrong,
        photo=photo,
        shipping=shipping,
        whole_order=whole_order,
        duplicate_lookup=duplicate_lookup,
        hints=hints,
    )


# ---------------------------------------------------------------------------
# delivery / order status
# ---------------------------------------------------------------------------


def _delivery_decision(
    result: PolicyResult,
    order: dict,
    shipment: Optional[dict],
    ticket: Ticket,
    *,
    non_delivery: bool,
    refund: bool,
) -> PolicyResult:
    result.reply_facts["outcome"] = None
    if not shipment:
        result.outcome = "UNABLE_TO_VERIFY"
        result.reason("CARRIER_STATUS_UNAVAILABLE")
        result.add_check(
            "CARRIER_STATUS",
            "UNABLE_TO_VERIFY",
            "RP-3.2 §4.2",
            [_ev("data/shipments.csv", order["order_id"], "no shipment row")],
        )
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    status = shipment.get("status")
    last_event = parsing.parse_dt(shipment.get("last_event_at"))
    shipped = parsing.parse_dt(shipment.get("shipped_at"))
    delivered = parsing.parse_dt(shipment.get("delivered_at"))
    received = parsing.parse_dt(ticket.received_at)
    last_movement = last_event or shipped

    no_movement_data = not last_event and not delivered and not shipped
    if status == "UNAVAILABLE" or no_movement_data:
        result.outcome = "UNABLE_TO_VERIFY"
        result.reason("CARRIER_STATUS_UNAVAILABLE")
        result.add_check(
            "CARRIER_STATUS",
            "UNABLE_TO_VERIFY",
            "RP-3.2 §4.2",
            [
                _ev(
                    "data/shipments.csv",
                    shipment.get("tracking_no") or order["order_id"],
                    f"status={status}; last_event_at={shipment.get('last_event_at') or 'empty'}",
                )
            ],
        )
        result.reply_facts.update(
            {
                "outcome": result.outcome,
                "approver_role": None,
                "shipment": shipment,
                "order_id": order["order_id"],
            }
        )
        return result

    idle_hours = parsing.hours_between(received, last_movement)
    idle_days = round(idle_hours / 24.0, 2) if idle_hours is not None else None

    if status == "IN_TRANSIT" and idle_hours is not None and idle_hours > 240:
        result.ticket_type = "DELIVERY_ISSUE"
        result.outcome = "ESCALATE"
        result.reason("LOST_IN_TRANSIT")
        result.human_approval_required = True
        result.approver_role = "CARRIER_CLAIMS"
        result.add_check(
            "CARRIER_MOVEMENT",
            "FAIL",
            "RP-3.2 §5.1",
            [
                _ev(
                    "data/shipments.csv",
                    shipment.get("tracking_no") or order["order_id"],
                    f"no movement for {idle_days} days (last_event_at={shipment.get('last_event_at')})",
                )
            ],
        )
        result.reply_facts.update(
            {
                "outcome": result.outcome,
                "approver_role": "CARRIER_CLAIMS",
                "shipment": shipment,
                "order_id": order["order_id"],
                "idle_days": idle_days,
            }
        )
        return result

    result.add_check(
        "CARRIER_MOVEMENT",
        "PASS",
        "RP-3.2 §5.1/§5.3",
        [
            _ev(
                "data/shipments.csv",
                shipment.get("tracking_no") or order["order_id"],
                f"status={status}; idle_days={idle_days}",
            )
        ],
    )

    if status == "DELIVERED" and non_delivery:
        # Open case in RP-3.2: the carrier records a delivery scan but the
        # customer says nothing arrived.  Rule applied: treat as a carrier
        # dispute and hand to Carrier Claims; no refund proposed.
        result.ticket_type = "DELIVERY_ISSUE"
        result.outcome = "ESCALATE"
        result.human_approval_required = True
        result.approver_role = "CARRIER_CLAIMS"
        result.add_check(
            "DELIVERY_CONFIRMED",
            "FAIL",
            "RP-3.2 §4.2 (open case)",
            [
                _ev(
                    "data/shipments.csv",
                    shipment.get("tracking_no") or order["order_id"],
                    f"carrier status DELIVERED at {shipment.get('delivered_at')}; customer disputes",
                )
            ],
        )
        result.reply_facts.update(
            {
                "outcome": result.outcome,
                "approver_role": "CARRIER_CLAIMS",
                "shipment": shipment,
                "order_id": order["order_id"],
            }
        )
        return result

    # Moving normally (or delivered with a status question): information only.
    result.outcome = "PROCEED"
    result.reply_facts.update(
        {
            "outcome": result.outcome,
            "approver_role": None,
            "shipment": shipment,
            "order_id": order["order_id"],
            "idle_days": idle_days,
            "refund_requested": refund,
        }
    )
    return result


# ---------------------------------------------------------------------------
# refund / return
# ---------------------------------------------------------------------------


def _refund_decision(
    result: PolicyResult,
    data: DataRoom,
    ticket: Ticket,
    order: dict,
    lines: List[dict],
    shipment: Optional[dict],
    *,
    body: str,
    damage: bool,
    wrong: bool,
    photo: bool,
    shipping: bool,
    whole_order: bool,
    duplicate_lookup: Optional[Callable[[str, List[int], str], Optional[str]]],
    hints: Optional[Dict] = None,
) -> PolicyResult:
    hints = hints or {}
    received = parsing.parse_dt(ticket.received_at)

    # Delivery date is the carrier scan recorded by the carrier.
    delivered = parsing.parse_dt(shipment.get("delivered_at")) if shipment else None
    if not delivered and shipment and shipment.get("status") == "DELIVERED":
        delivered = parsing.parse_dt(shipment.get("last_event_at"))
    if not delivered:
        result.add_check(
            "DELIVERY_DATE",
            "UNABLE_TO_VERIFY",
            "RP-3.2 §2.1",
            [
                _ev(
                    "data/shipments.csv",
                    order["order_id"],
                    f"no delivery scan (shipment_status={shipment.get('status') if shipment else 'missing'})",
                )
            ],
        )
        result.outcome = "UNABLE_TO_VERIFY"
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    days = parsing.days_between(received, delivered)
    result.reply_facts.update(
        {"order_id": order["order_id"], "delivery_date": delivered.date().isoformat(), "days": days}
    )
    result.add_check(
        "DELIVERY_DATE",
        "PASS",
        "RP-3.2 §2.1",
        [
            _ev(
                "data/shipments.csv",
                shipment.get("tracking_no") or order["order_id"],
                f"delivered_at={shipment.get('delivered_at') or shipment.get('last_event_at')}",
            )
        ],
    )

    # Window: 30 days ordinary; damaged/faulty with photo up to 45 days.
    # The flag exists so the evaluation can show a before/after for this rule.
    damage_exception_enabled = os.environ.get("DAMAGE_EXCEPTION_ENABLED", "1") != "0"
    window_reason = None
    in_window = False
    damage_exception = False
    if days is not None and days <= 30:
        in_window = True
    elif days is not None and days <= 45 and damage and photo and damage_exception_enabled:
        in_window = True
        damage_exception = True
        window_reason = "DAMAGE_EXCEPTION"
    result.add_check(
        "RETURN_WINDOW",
        "PASS" if in_window else "FAIL",
        "RP-3.2 §2.1/§2.3/§2.6",
        [
            _ev(
                "ticket",
                "received_at",
                f"days_since_delivery={days}; damage={damage}; photo={photo}; "
                f"limit={'45' if (damage and photo and damage_exception_enabled) else '30'}",
            )
        ],
    )
    if damage and photo:
        result.add_check(
            "DAMAGE_EXCEPTION",
            "PASS" if damage_exception else ("FAIL" if days is not None and days > 45 else "NOT_APPLICABLE"),
            "RP-3.2 §2.6",
            [
                _ev(
                    "ticket",
                    "attachments",
                    f"photo supplied; days_since_delivery={days}; "
                    f"exception_applied={damage_exception}",
                )
            ],
        )
    else:
        result.add_check(
            "DAMAGE_EXCEPTION",
            "NOT_APPLICABLE",
            "RP-3.2 §2.6",
            [_ev("ticket", "attachments", f"damage={damage}; photo={photo}")],
        )

    if not in_window:
        result.outcome = "REJECT"
        result.reason("OUT_OF_WINDOW")
        result.reply_facts.update(
            {
                "outcome": result.outcome,
                "approver_role": None,
                "window_days": 45 if (damage and photo and damage_exception_enabled) else 30,
            }
        )
        return result

    if window_reason:
        result.reason(window_reason)

    # Affected lines: explicitly named products, single-line orders, or a
    # genuinely all-items-damaged claim.
    matched = parsing.match_product_lines(body, lines)
    result.tool("match_product_lines")
    if not matched and hints.get("product_name"):
        wanted = str(hints["product_name"]).lower()
        matched = [l for l in lines if l["product_name"].lower() == wanted]
    if not matched:
        if len(lines) == 1:
            matched = lines
        elif whole_order and parsing.all_items_damaged(body):
            matched = lines
    if not matched:
        result.add_check(
            "AFFECTED_LINES",
            "UNABLE_TO_VERIFY",
            "RP-3.2 §2.2/§2.4",
            [
                _ev(
                    "data/order_items.csv",
                    order["order_id"],
                    f"could not identify affected line(s) among {len(lines)} line(s)",
                )
            ],
        )
        result.outcome = "UNABLE_TO_VERIFY"
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result
    result.add_check(
        "AFFECTED_LINES",
        "PASS",
        "RP-3.2 §2.2/§2.4",
        [
            _ev(
                "data/order_items.csv",
                f"{order['order_id']}:{line['line_no']}",
                f"{line['product_name']} qty={line['qty']} unit={_money(line['unit_price'])}",
            )
            for line in matched
        ],
    )

    # Money: affected units only, less refunds already ISSUED for the line.
    total = 0.0
    fully_refunded: List[dict] = []
    line_details: List[dict] = []
    prior_any = False
    for line in matched:
        qty = parsing.requested_quantity(body, line)
        if len(matched) == 1 and isinstance(hints.get("quantity"), int):
            qty = int(hints["quantity"])
        qty = max(1, min(qty, int(line["qty"])))
        value = round(qty * float(line["unit_price"]), 2)
        prior_rows = data.prior_refunds(order["order_id"], line["line_no"])
        prior = round(sum(float(r["amount"]) for r in prior_rows), 2)
        if prior:
            prior_any = True
        new_amount = round(max(0.0, value - prior), 2)
        if new_amount == 0.0 and prior > 0:
            fully_refunded.append(line)
        total = round(total + new_amount, 2)
        line_details.append(
            {
                "line_no": line["line_no"],
                "product_name": line["product_name"],
                "affected_qty": qty,
                "unit_price": float(line["unit_price"]),
                "line_value": value,
                "prior_refund": prior,
                "new_amount": new_amount,
            }
        )
    result.tool("lookup_refunds")
    result.add_check(
        "PRIOR_REFUNDS",
        "FAIL" if prior_any else "PASS",
        "RP-3.2 §2.5",
        [
            _ev(
                "data/refunds.csv",
                f"{order['order_id']}:{line['line_no']}",
                f"issued={line['prior_refund']:.2f}; new_amount={line['new_amount']:.2f}",
            )
            for line in line_details
            if line["prior_refund"] > 0
        ]
        or [_ev("data/refunds.csv", order["order_id"], "no prior refunds on affected lines")],
    )
    if fully_refunded:
        result.reason("ALREADY_REFUNDED")

    result.reply_facts.update(
        {
            "lines": line_details,
            "prior_refund_total": round(sum(x["prior_refund"] for x in line_details), 2),
            "amount": total,
        }
    )

    if total <= 0:
        result.outcome = "REJECT"
        if fully_refunded:
            result.reason("ALREADY_REFUNDED")
        result.reply_facts.update({"outcome": result.outcome, "approver_role": None})
        return result

    # Scope: did the customer ask for more than the affected lines?
    all_line_nos = {line["line_no"] for line in lines}
    matched_nos = {line["line_no"] for line in matched}
    asked_more = bool(shipping) or (whole_order and matched_nos != all_line_nos)
    result.add_check(
        "REQUESTED_SCOPE",
        "FAIL" if asked_more else "PASS",
        "RP-3.2 §2.4",
        [
            _ev(
                "ticket",
                "body",
                f"whole_order={whole_order}; shipping_requested={shipping}; "
                f"affected_lines={sorted(matched_nos)} of {sorted(all_line_nos)}",
            )
        ],
    )
    if asked_more:
        result.reason("PARTIAL_LINES")
    result.reply_facts["shipping_requested"] = shipping
    result.reply_facts["whole_order"] = whole_order

    # Authority.
    if total > 150.0:
        result.reason("APPROVAL_REQUIRED")
        result.add_check(
            "REFUND_AUTHORITY",
            "FAIL",
            "RP-3.2 §3.1/§3.2",
            [_ev("policy", "RP-3.2 §3.2", f"amount={_money(total)} > $150.00")],
        )
        result.outcome = "PEND"
        result.human_approval_required = True
        result.approver_role = "SUPPORT_LEAD"
    else:
        result.add_check(
            "REFUND_AUTHORITY",
            "PASS",
            "RP-3.2 §3.1",
            [_ev("policy", "RP-3.2 §3.1", f"amount={_money(total)} <= $150.00")],
        )
        result.outcome = "PROCEED_MODIFIED" if asked_more else "PROCEED"
        result.approver_role = None

    result.amount_usd = total

    # Open case: a repeat request for the same order + line that already has
    # a live refund action is answered, but no second action is proposed.
    if duplicate_lookup:
        duplicate = duplicate_lookup(order["order_id"], sorted(matched_nos), order["customer_id"])
        if duplicate:
            result.duplicate_of_run_id = duplicate
            result.add_check(
                "DUPLICATE_REQUEST",
                "FAIL",
                "RP-3.2 §3.3 (open case)",
                [_ev("runs", duplicate, "an earlier run already proposed/approved this refund")],
            )
            result.outcome = "PROCEED"
            result.reason_codes = []
            result.human_approval_required = False
            result.approver_role = None
            result.amount_usd = None
            result.reply_facts.update(
                {"outcome": result.outcome, "approver_role": None, "duplicate_of": duplicate, "amount": None}
            )
            return result
        result.add_check(
            "DUPLICATE_REQUEST",
            "PASS",
            "RP-3.2 §3.3 (open case)",
            [_ev("runs", order["order_id"], "no earlier live refund action for these lines")],
        )

    result.reply_facts.update({"outcome": result.outcome, "approver_role": result.approver_role})
    return result
