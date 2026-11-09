"""Customer-facing draft replies.

Replies are generated from the engine's facts, never from free-form model
output, so the amounts and statuses in a draft always match the decision.
"""
from __future__ import annotations

from typing import List

from .models import DraftReply, Ticket


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _products(lines: List[dict]) -> str:
    parts = []
    for line in lines:
        name = line.get("product_name", "item")
        qty = line.get("affected_qty", 1)
        parts.append(f"{qty} x {name}" if qty and qty > 1 else name)
    if not parts:
        return "the affected item"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def draft_reply(ticket: Ticket, facts: dict, outcome: str, ticket_type: str) -> DraftReply:
    name = (facts.get("customer_name") or "").split(" ")[0]
    greeting = f"Hi {name}," if name else "Hello,"
    order_id = facts.get("order_id") or "your order"
    amount = facts.get("amount")
    lines = facts.get("lines") or []
    prior_total = facts.get("prior_refund_total") or 0.0
    approver = facts.get("approver_role")
    subject = ticket.subject or "Your Larkfield Home request"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    body: List[str] = [greeting, ""]

    if outcome == "PEND":
        body.append(
            f"Thanks for contacting Larkfield Home. I reviewed {order_id} and the affected item"
            f"{'s' if len(lines) != 1 else ''}: {_products(lines)}."
        )
        if prior_total:
            body.append(
                f"A refund of {_money(prior_total)} was already issued for one of the affected lines, "
                "so it is not included again."
            )
        body.append(
            f"I have proposed a refund of {_money(amount or 0)}. Because it is above the $150.00 "
            f"authority limit for automatic processing, it needs approval from a Support Lead before "
            "anything is processed. No refund has been issued yet."
        )
        body.append("We will update you as soon as the review is complete.")
    elif outcome == "PROCEED_MODIFIED":
        body.append(
            f"Thanks for contacting Larkfield Home. I reviewed {order_id} and the affected item"
            f"{'s' if len(lines) != 1 else ''}: {_products(lines)}."
        )
        if prior_total:
            body.append(
                f"A refund of {_money(prior_total)} was already issued for one of the affected lines, "
                "so it is not included again."
            )
        if facts.get("shipping_requested") or facts.get("modified_reason") == "shipping":
            body.append(
                f"Refunds cover the affected item value, not the delivery charge. I have approved a "
                f"refund of {_money(amount or 0)} for the affected item(s)."
            )
        else:
            body.append(
                f"Refunds cover the affected item(s) only, not the whole order. I have approved a "
                f"refund of {_money(amount or 0)} for {_products(lines)}."
            )
        body.append("You should see the refund on your original payment method within 5-10 business days.")
    elif outcome == "PROCEED" and ticket_type in ("REFUND", "RETURN"):
        body.append(
            f"Thanks for contacting Larkfield Home. I reviewed {order_id} and the affected item"
            f"{'s' if len(lines) != 1 else ''}: {_products(lines)}."
        )
        if prior_total:
            body.append(
                f"A refund of {_money(prior_total)} was already issued for one of the affected lines, "
                "so it is not included again."
            )
        if amount:
            body.append(
                f"I have approved a refund of {_money(amount)} for {_products(lines)}. "
                "You should see it on your original payment method within 5-10 business days."
            )
        else:
            body.append(
                "This request is already in progress under an earlier reference, so I have not "
                "created a second refund. Our team will follow up on the existing request."
            )
    elif outcome == "PROCEED" and ticket_type in ("ORDER_STATUS", "DELIVERY_ISSUE"):
        shipment = facts.get("shipment") or {}
        status = (shipment.get("status") or "in transit").replace("_", " ").lower()
        tracking = shipment.get("tracking_no")
        body.append(f"Thanks for contacting Larkfield Home about {order_id}.")
        body.append(
            f"Our carrier's latest status is {status}."
            + (f" The tracking number is {tracking}." if tracking else "")
        )
        if shipment.get("last_event_at"):
            body.append(f"The last carrier scan was {shipment['last_event_at']}.")
        body.append(
            "If the parcel has not moved for more than 10 days, we treat it as lost in transit and "
            "start a carrier claim."
        )
    elif outcome == "ESCALATE" and approver == "TRUST_SAFETY":
        body.append(
            f"Thanks for contacting Larkfield Home about {order_id}. We have passed your message to "
            "our Trust & Safety team for review. No refund has been processed from this email."
        )
    elif outcome == "ESCALATE" and approver == "CARRIER_CLAIMS":
        body.append(
            f"Thanks for contacting Larkfield Home about {order_id}. We are sorry your parcel has "
            "not arrived. We have escalated this to our Carrier Claims team to investigate with the "
            "carrier. No refund has been processed yet."
        )
    elif outcome == "REJECT" and "OUT_OF_WINDOW" in (facts.get("reason_codes") or []):
        limit = facts.get("window_days", 30)
        body.append(
            f"Thanks for contacting Larkfield Home about {order_id}. Our refund policy allows requests "
            f"within {limit} days of the carrier's delivery date"
            + (" (45 days for damaged or faulty items when a photo is supplied)" if limit == 45 else "")
            + "."
        )
        if facts.get("delivery_date") and facts.get("days") is not None:
            body.append(
                f"The carrier recorded delivery on {facts['delivery_date']} and your request was "
                f"received {facts['days']} days later, so it falls outside that window."
            )
        body.append("I am sorry we cannot approve a refund on this occasion.")
    elif outcome == "REJECT" and "ALREADY_REFUNDED" in (facts.get("reason_codes") or []):
        body.append(
            f"Thanks for contacting Larkfield Home about {order_id}. Our records show that a refund "
            "has already been issued for the affected item, so we cannot refund it a second time."
        )
        body.append("If you believe this is wrong, please reply and we will check the payment record.")
    elif outcome == "INVALID_REQUEST":
        if "ORDER_NOT_FOUND" in (facts.get("reason_codes") or []):
            body.append(
                "Thanks for contacting Larkfield Home. We could not find an order matching the number "
                "in your email. Please reply with the order number from your confirmation email so we "
                "can look into this."
            )
        else:
            body.append(
                "Thanks for contacting Larkfield Home. The order you mentioned is not associated with "
                "the email address on this message. For your security we cannot action it from this "
                "address. Please reply from the email used to place the order, or confirm the order "
                "number and account email."
            )
    elif outcome == "UNABLE_TO_VERIFY":
        if "CARRIER_STATUS_UNAVAILABLE" in (facts.get("reason_codes") or []):
            body.append(
                f"Thanks for contacting Larkfield Home about {order_id}. The carrier has not returned "
                "a tracking status for this shipment, so we cannot verify the delivery position yet. "
                "We are checking with the carrier and will update you. No refund has been processed."
            )
        else:
            body.append(
                f"Thanks for contacting Larkfield Home about {order_id}. We need a little more "
                "information to review this request. Please reply with the order number and the item "
                "you are asking about."
            )
    else:
        body.append(
            "Thanks for contacting Larkfield Home. Please reply with your order number and a short "
            "description of the item and issue, and we will review it."
        )

    body.append("")
    body.append("Kind regards,")
    body.append("Larkfield Home Customer Care")
    return DraftReply(to=(ticket.from_ or "").strip(), subject=subject, body="\n".join(body))
