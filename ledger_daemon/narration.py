"""Narration parsing. Indian bank narrations are semi-structured; regex extraction
beats ML here and is far more explainable."""

from __future__ import annotations

import re

PATTERNS = {
    "utr": re.compile(r"\b([A-Z]{4}[A-Z0-9]{6,18})\b"),
    "upi_ref": re.compile(r"UPI/(\d{9,12})"),
    "vpa": re.compile(r"([\w.\-]+@[\w]+)"),
    "invoice": re.compile(r"\b(?:INV|INVOICE)[\s\-]?(\d{3,8})\b"),
    "mode": re.compile(r"^(NEFT|IMPS|RTGS|UPI|ACH)"),
    "settlement": re.compile(r"RAZORPAYSETTLEMENT-(\S+)"),
}


def parse(narration: str) -> dict[str, str]:
    out: dict[str, str] = {}
    up = narration.upper()
    m = PATTERNS["mode"].match(up)
    if m:
        out["mode"] = m.group(1)
    m = PATTERNS["settlement"].search(narration)
    if m:
        out["settlement_id"] = m.group(1)
    m = PATTERNS["invoice"].search(up)
    if m:
        out["invoice"] = m.group(1)
    m = PATTERNS["upi_ref"].search(up)
    if m:
        out["upi_ref"] = m.group(1)
    m = PATTERNS["vpa"].search(narration)
    if m:
        out["vpa"] = m.group(1)
    return out


def invoice_in_narration(invoice_no: str, narration: str) -> bool:
    digits = "".join(ch for ch in invoice_no if ch.isdigit())
    if not digits:
        return False
    found = parse(narration).get("invoice")
    if found and found == digits:
        return True
    # fallback: bare number at a token boundary
    return re.search(rf"\b{re.escape(digits)}\b", narration) is not None
