"""Deterministic extraction helpers.

The policy engine is the authority.  These functions turn a free-text email
into the facts the engine needs; they are intentionally conservative and every
uncertain fact is surfaced as UNABLE_TO_VERIFY rather than guessed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# identifiers / email
# ---------------------------------------------------------------------------

_ORDER_PATTERNS = [
    re.compile(r"\bLF[\s\-–_]?(\d{5,7})\b", re.IGNORECASE),
    re.compile(r"#\s*(\d{5,7})\b"),
    re.compile(r"\border\s*(?:number|no\.?|#|id)?\s*[:\-]?\s*(\d{5,7})\b", re.IGNORECASE),
    re.compile(r"\border\s+order\s+(\d{5,7})\b", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
]


def normalize_email(value: str) -> str:
    raw = (value or "").strip()
    match = re.search(r"<([^>]+)>", raw)
    if match:
        raw = match.group(1)
    else:
        match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", raw)
        if match:
            raw = match.group(0)
    return raw.strip().lower()


def normalize_order_id(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", value or "")
    return f"LF-{digits}" if digits else ""


def extract_order_id(text: str) -> Optional[str]:
    """Return the first plausible order id, normalised to LF-######."""
    for pattern in _ORDER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            digits = match.group(1)
            if len(digits) == 5:
                digits = "0" + digits
            return f"LF-{digits}"
    return None


# ---------------------------------------------------------------------------
# text flags
# ---------------------------------------------------------------------------

_POLICY_OVERRIDE = [
    re.compile(r"ignore\s+(?:all\s+|any\s+|your\s+)*(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"note\s+to\s+the\s+(?:ai|assistant|agent|bot)", re.I),
    re.compile(r"pre[\s\-]?approved", re.I),
    re.compile(r"authori[sz]ed\s+to\s+(?:process|refund|approve|issue)", re.I),
    re.compile(r"bypass\s+(?:the\s+)?(?:policy|rules|limit|review)", re.I),
    re.compile(r"override\s+(?:the\s+)?(?:policy|rules|limit|review)", re.I),
    re.compile(r"without\s+(?:any\s+)?review", re.I),
    re.compile(r"charge[\s\-]?back", re.I),
    re.compile(r"legal\s+action|lawyer|attorney|\bsue\b", re.I),
    re.compile(r"\[system\]", re.I),
    re.compile(r"mark\s+human_approval_required\s+false", re.I),
    re.compile(r"issue\s+a\s+full\s+refund", re.I),
    re.compile(r"you\s+are\s+(?:authorised|authorized)\s+to", re.I),
    re.compile(r"this\s+customer\s+is\s+pre[\s\-]?approved", re.I),
    re.compile(r"(?:manager|supervisor|support\s+lead)\s+(?:has\s+)?approved", re.I),
    re.compile(r"\bas\s+the\s+(?:support\s+lead|manager|supervisor)\b", re.I),
    re.compile(r"(?:i|we)\s+approve\s+(?:this|the)\s+refund", re.I),
    re.compile(r"make\s+an?\s+exception|waive\s+(?:the\s+)?(?:policy|rule|limit)", re.I),
    re.compile(r"(?:system\s+prompt|developer\s+mode|jailbreak|do\s+not\s+follow\s+(?:the\s+)?policy)", re.I),
    re.compile(r"(?:file\s+a\s+complaint|better\s+business\s+bureau|\bBBB\b|dispute\s+the\s+charge)", re.I),
]

_DAMAGE = re.compile(
    r"\b(damag\w*|broken|broke|cracked|crack\w*|faulty|defect\w*|chipped|smashed|"
    r"shattered|scratch\w*|dented|dent\w*|stained|torn|ripped|wobbly|loose|"
    r"not\s+working|doesn'?t\s+work|missing\s+(?:parts?|pieces?)|"
    r"arrived\s+(?:with\s+)?(?:a\s+)?(?:chip|dent|scratch))\b",
    re.I,
)
_WRONG_ITEM = re.compile(
    r"\b(wrong\s+(?:item|product|thing|one)|received\s+something\s+else|"
    r"not\s+what\s+i\s+ordered|not\s+the\s+(?:item|product)\s+i\s+ordered|"
    r"different\s+(?:item|product)|incorrect\s+(?:item|product)|not\s+as\s+described|"
    r"sent\s+the\s+wrong)\b",
    re.I,
)
_RETURN = re.compile(
    r"\b(return\w*|exchange\w*|send(?:ing)?\s+(?:it\s+)?back|send\s+back)\b", re.I
)
_REFUND = re.compile(
    r"\b(refund\w*|money\s+back|reimburs\w*|compensat\w*|credit\s+back)\b", re.I
)
_NON_DELIVERY = re.compile(
    r"\b(never\s+(?:arrived|received|came|showed\s+up|turned\s+up)|not\s+(?:arrived|received)|"
    r"hasn'?t\s+(?:arrived|come|shown\s+up|turned\s+up)|didn'?t\s+(?:arrive|come)|"
    r"did\s+not\s+(?:arrive|come)|missing|lost|still\s+waiting|"
    r"haven'?t\s+received|not\s+delivered|no\s+sign\s+of\s+(?:it|the\s+(?:order|parcel|package))|"
    r"still\s+not\s+(?:here|arrived))\b",
    re.I,
)
_ORDER_STATUS = re.compile(
    r"\b(where\s*(?:is|'s)\s+my\s+(?:order|parcel|package|stuff)|status|tracking|"
    r"where\s+is\s+(?:it|my\s+parcel)|hasn'?t\s+(?:moved|changed)|has\s+not\s+(?:moved|changed)|"
    r"stuck|update|when\s+will\s+(?:it|my\s+\w+)\s+(?:arrive|be\s+delivered)|delivery\s+date)\b",
    re.I,
)
_SHIPPING = re.compile(
    r"\b(delivery\s+(?:charge|fee|cost)|shipping\s+(?:fee|cost|charge)|postage)\b", re.I
)
_WHOLE_ORDER = re.compile(
    r"\b(whole\s+order|entire\s+order|full\s+order|all\s+of\s+(?:it|them)|"
    r"everything|all\s+(?:the\s+)?items?|money\s+back\s+for\s+the\s+(?:whole|entire)\s+order)\b",
    re.I,
)
_ALL_DAMAGED = re.compile(
    r"\b(everything|all\s+(?:the\s+)?items?|both\s+items?|whole\s+order)\b.{0,40}"
    r"\b(damag\w*|broken|cracked|faulty|defect\w*)\b",
    re.I | re.S,
)
_PHOTO_FILENAME = re.compile(r"\.(?:jpe?g|png|gif|webp|heic|bmp|tiff?)$", re.I)


def has_policy_override(text: str) -> bool:
    return any(p.search(text or "") for p in _POLICY_OVERRIDE)


def has_damage(text: str) -> bool:
    return bool(_DAMAGE.search(text or ""))


def has_wrong_item(text: str) -> bool:
    return bool(_WRONG_ITEM.search(text or ""))


def has_return(text: str) -> bool:
    return bool(_RETURN.search(text or ""))


def has_refund(text: str) -> bool:
    return bool(_REFUND.search(text or ""))


def has_non_delivery(text: str) -> bool:
    return bool(_NON_DELIVERY.search(text or ""))


def has_order_status(text: str) -> bool:
    return bool(_ORDER_STATUS.search(text or ""))


def asks_shipping(text: str) -> bool:
    return bool(_SHIPPING.search(text or ""))


def asks_whole_order(text: str) -> bool:
    return bool(_WHOLE_ORDER.search(text or ""))


def all_items_damaged(text: str) -> bool:
    return bool(_ALL_DAMAGED.search(text or ""))


def has_photo(attachments: Iterable[dict]) -> bool:
    for att in attachments or []:
        ctype = str(att.get("content_type") or "").lower()
        name = str(att.get("file_name") or "")
        if ctype.startswith("image/") or _PHOTO_FILENAME.search(name):
            return True
    return False


# ---------------------------------------------------------------------------
# product / quantity
# ---------------------------------------------------------------------------

# Short customer-friendly aliases keyed by a normalised product token.
_ALIASES = {
    "mirror": ["mirror", "wall mirror"],
    "lamp": ["lamp", "floor lamp", "arc lamp"],
    "chair": ["chair", "dining chair"],
    "vase": ["vase"],
    "mug": ["mug", "mugs", "mug set", "stoneware"],
    "shelf": ["shelf", "shelves", "floating shelf"],
    "candle": ["candle", "candles", "candle trio"],
    "throw": ["throw", "blanket", "linen throw"],
    "rug": ["rug", "carpet", "wool rug"],
    "duvet": ["duvet", "duvet set", "bedding", "percale"],
    "table": ["side table"],
    "cushion": ["cushion", "cushion cover", "cushion covers", "pillow cover"],
}


def _product_alias(product_name: str) -> Optional[str]:
    name = (product_name or "").lower()
    for key in _ALIASES:
        if key in name:
            return key
    # Fall back to the longest non-generic word in the product name.
    words = [w for w in re.findall(r"[a-z]+", name) if len(w) > 3]
    return max(words, key=len) if words else None


def _mentions(body: str, alias: str) -> bool:
    return bool(re.search(r"\b" + re.escape(alias) + r"\b", body or "", re.I))


def match_product_lines(body: str, lines: List[dict]) -> List[dict]:
    """Lines whose product the customer explicitly names.

    If several lines are the same product and the email uses a singular
    reference, only the first line is treated as the affected line (the
    customer did not say "both" / "two").
    """
    body_l = body or ""
    by_product: dict[str, List[dict]] = {}
    for line in lines:
        alias = _product_alias(line.get("product_name", ""))
        if alias and _mentions(body_l, alias):
            by_product.setdefault(line["product_name"], []).append(line)
    matched: List[dict] = []
    plural = bool(re.search(r"\b(both|all|two|2|three|3)\b", body_l, re.I))
    for group in by_product.values():
        if len(group) > 1 and not plural:
            matched.append(group[0])
        else:
            matched.extend(group)
    matched.sort(key=lambda x: x["line_no"])
    return matched


def requested_quantity(body: str, line: dict) -> int:
    """How many units of this line the customer is asking about."""
    ordered = int(line.get("qty") or 1)
    text = body or ""
    lower = text.lower()
    alias = _product_alias(line.get("product_name", "")) or ""
    if re.search(r"\bone\s+of\s+(?:my\s+|the\s+)?", lower):
        return 1
    if re.search(r"\b(?:the\s+)?(?:broken|damaged|faulty|defective)\s+one\b", lower):
        return 1
    if re.search(r"\b(?:just|only)\s+one\b|\bsingle\b", lower):
        return 1
    if re.search(r"\b(?:just|only)\s+(?:the\s+)?(?:one\s+)?(?:broken|damaged|faulty|defective)?\s*" + re.escape(alias), lower):
        return 1
    if re.search(r"\bone\s+" + re.escape(alias), lower) and ordered > 1:
        return 1
    if re.search(r"\ba\s+" + re.escape(alias), lower) and ordered > 1:
        return 1
    if re.search(r"\b(?:both|all|two|2)\b", lower) and alias and _mentions(lower, alias):
        return ordered
    if re.search(r"\btwo\s+of\b", lower):
        return min(2, ordered)
    if re.search(r"\b(?:two|2)\s+" + re.escape(alias), lower):
        return min(2, ordered)
    return ordered


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_between(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[int]:
    if not later or not earlier:
        return None
    return (later.date() - earlier.date()).days


def hours_between(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[float]:
    if not later or not earlier:
        return None
    return (later - earlier).total_seconds() / 3600.0


def classify_ticket_type(
    body: str, *, non_delivery: bool, status: bool, damage: bool, wrong: bool,
    return_: bool, refund: bool, shipping: bool,
) -> str:
    if non_delivery or (status and not refund and not return_):
        # A lost / not-delivered claim is a delivery issue; a plain "where is it"
        # on a moving shipment is an order-status question (refined later).
        return "DELIVERY_ISSUE" if non_delivery else "ORDER_STATUS"
    if damage or wrong:
        return "REFUND"
    if return_:
        return "RETURN"
    if refund:
        return "REFUND"
    if status:
        return "ORDER_STATUS"
    return "OTHER"
