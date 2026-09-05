"""Exact accounting identities expressed as signed integer-paise terms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .certificates import (
    AmountTerm, IdentityCertificate, batch_root, source_hash_map,
)
from .finance_events import Adjustment, Refund, Settlement
from .models import GatewayCapture, Order
from .money import add, paise, sub


@dataclass(frozen=True)
class IdentityResult:
    rule_id: str
    valid: bool
    lhs_paise: int
    rhs_paise: int
    source_refs: tuple[str, ...]
    terms: tuple[AmountTerm, ...] = ()
    error_codes: tuple[str, ...] = ()


def build_identity_certificate(result: IdentityResult,
                               rows: list[dict[str, object]], *,
                               subject_id: str) -> IdentityCertificate:
    """Bind a valid identity and every referenced source into one proof."""
    if not result.valid:
        raise ValueError("cannot issue a certificate for an invalid identity")
    hashes = source_hash_map(rows)
    referenced = {subject_id, *result.source_refs}
    missing = referenced - hashes.keys()
    if missing:
        raise ValueError(f"identity references unknown source rows: {sorted(missing)}")
    selected = {row_id: hashes[row_id] for row_id in referenced}
    selected["BATCH_ROOT"] = batch_root(hashes)
    return IdentityCertificate.create(
        subject_id=subject_id,
        rule_id=result.rule_id,
        lhs_paise=result.lhs_paise,
        rhs_paise=result.rhs_paise,
        source_hashes=selected,
        amount_terms=result.terms,
    )


def _processed_refunds(refunds: Iterable[Refund]) -> list[Refund]:
    return [refund for refund in refunds if refund.status == "processed"]


def _has_duplicates(values: Iterable[str]) -> bool:
    values = list(values)
    return len(values) != len(set(values))


def verify_settlement_identity(
    settlement: Settlement,
    captures: Iterable[GatewayCapture],
    refunds: Iterable[Refund],
    adjustments: Iterable[Adjustment],
) -> IdentityResult:
    captures = list(captures)
    refunds = _processed_refunds(refunds)
    adjustments = list(adjustments)
    terms: list[AmountTerm] = []
    errors: list[str] = []
    capture_ids = {capture.payment_id for capture in captures}

    if not captures:
        errors.append("MISSING_SOURCE")
    if settlement.status != "processed":
        errors.append("UNSETTLED_EVENT")
    all_ids = ([capture.payment_id for capture in captures]
               + [refund.refund_id for refund in refunds]
               + [adjustment.adjustment_id for adjustment in adjustments])
    if _has_duplicates(all_ids):
        errors.append("DUPLICATE_SOURCE")

    for capture in captures:
        if capture.status != "captured" or capture.settlement_id != settlement.settlement_id:
            errors.append("FOREIGN_SOURCE")
        terms.extend((
            AmountTerm("gateway_capture", paise(capture.amount_paise), capture.payment_id, "amount_paise"),
            AmountTerm("gateway_fee", -paise(capture.fee_paise), capture.payment_id, "fee_paise"),
            AmountTerm("gateway_gst", -paise(capture.tax_paise), capture.payment_id, "tax_paise"),
        ))
    for refund in refunds:
        if refund.payment_id not in capture_ids:
            errors.append("FOREIGN_SOURCE")
        terms.append(AmountTerm("refund", -paise(refund.amount_paise), refund.refund_id, "amount_paise"))
    for adjustment in adjustments:
        if adjustment.settlement_id != settlement.settlement_id:
            errors.append("FOREIGN_SOURCE")
        terms.append(AmountTerm("adjustment", paise(adjustment.amount_paise), adjustment.adjustment_id, "amount_paise"))

    rhs = add(*(term.amount_paise for term in terms))
    lhs = paise(settlement.amount_paise)
    if lhs != rhs:
        errors.append("AMOUNT_IDENTITY_MISMATCH")
    errors = list(dict.fromkeys(errors))
    refs = (settlement.settlement_id,) + tuple(term.source_row_id for term in terms)
    return IdentityResult(
        "IDENTITY_SETTLEMENT_NET_V1", not errors, lhs, rhs, refs,
        tuple(terms), tuple(errors),
    )


def verify_invoice_identity(
    order: Order,
    payments: Iterable[GatewayCapture],
    refunds: Iterable[Refund],
    tds_paise: int,
    *,
    tds_source_ref: str = "",
) -> IdentityResult:
    payments = list(payments)
    refunds = _processed_refunds(refunds)
    paise(tds_paise)
    if tds_paise < 0:
        raise ValueError("tds_paise cannot be negative")
    terms: list[AmountTerm] = []
    errors: list[str] = []
    payment_ids = {payment.payment_id for payment in payments}

    if not payments:
        errors.append("MISSING_SOURCE")
    if tds_paise and not tds_source_ref:
        errors.append("TDS_SOURCE_MISSING")
    if _has_duplicates([payment.payment_id for payment in payments]
                       + [refund.refund_id for refund in refunds]):
        errors.append("DUPLICATE_SOURCE")

    for payment in payments:
        if payment.order_id != order.order_id or payment.status != "captured":
            errors.append("FOREIGN_SOURCE")
        terms.append(AmountTerm("payment", paise(payment.amount_paise), payment.payment_id, "amount_paise"))
    for refund in refunds:
        if refund.order_id != order.order_id or refund.payment_id not in payment_ids:
            errors.append("FOREIGN_SOURCE")
        terms.append(AmountTerm("refund", -paise(refund.amount_paise), refund.refund_id, "amount_paise"))
    if tds_paise:
        terms.append(AmountTerm(
            "tds_withheld", tds_paise, tds_source_ref or order.order_id,
            "amount_paise" if tds_source_ref else "tds_paise",
        ))

    rhs = add(*(term.amount_paise for term in terms))
    lhs = paise(order.amount_paise)
    if lhs != rhs:
        errors.append("AMOUNT_IDENTITY_MISMATCH")
    errors = list(dict.fromkeys(errors))
    refs = (order.order_id,) + tuple(term.source_row_id for term in terms)
    return IdentityResult(
        "IDENTITY_INVOICE_COVERAGE_V1", not errors, lhs, rhs, refs,
        tuple(terms), tuple(errors),
    )
