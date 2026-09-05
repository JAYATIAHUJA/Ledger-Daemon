from dataclasses import asdict, replace

from ledger_daemon.finance_events import Adjustment, Refund, Settlement
from ledger_daemon.identities import (
    build_identity_certificate, verify_invoice_identity, verify_settlement_identity,
)
from ledger_daemon.models import GatewayCapture, Order
from ledger_daemon.verifier import verify_identity_certificate


def _capture(payment_id="pay-1", order_id="ORD-1", amount=100_00, fee=200, tax=36):
    return GatewayCapture(payment_id, order_id, amount, fee, tax, "captured", "card",
                          "2026-08-10", "setl-1", "UTR1")


def _order(amount=100_00):
    return Order("ORD-1", "INV-1", "CUST-1", "ACME LTD", amount,
                 "2026-08-10", "paid", "gateway")


def test_settlement_identity_includes_fee_gst_refund_and_signed_adjustment():
    capture = _capture()
    refund = Refund("rfnd-1", "pay-1", "ORD-1", 1000, "processed", "2026-08-11")
    adjustment = Adjustment("adj-1", "setl-1", -50, "debit", "2026-08-12")
    settlement = Settlement("setl-1", 8714, "2026-08-13", "UTR1", "processed")

    result = verify_settlement_identity(settlement, [capture], [refund], [adjustment])

    assert result.valid
    assert result.rule_id == "IDENTITY_SETTLEMENT_NET_V1"
    assert (result.lhs_paise, result.rhs_paise) == (8714, 8714)
    assert {term.name for term in result.terms} >= {
        "gateway_capture", "gateway_fee", "gateway_gst", "refund", "adjustment"
    }


def test_one_paisa_settlement_tamper_is_rejected():
    capture = _capture()
    settlement = Settlement("setl-1", 9764, "2026-08-13", "UTR1", "processed")
    assert verify_settlement_identity(settlement, [capture], [], []).valid

    tampered = replace(settlement, amount_paise=9765)
    result = verify_settlement_identity(tampered, [capture], [], [])
    assert not result.valid
    assert result.error_codes == ("AMOUNT_IDENTITY_MISMATCH",)


def test_invoice_identity_supports_split_capture_partial_refund_and_tds():
    order = _order(100_00)
    payments = [_capture("pay-1", amount=6000, fee=0, tax=0),
                _capture("pay-2", amount=4000, fee=0, tax=0)]
    refund = Refund("rfnd-1", "pay-2", "ORD-1", 1000, "processed", "2026-08-11")

    result = verify_invoice_identity(
        order, payments, [refund], tds_paise=1000, tds_source_ref="tds-1"
    )

    assert result.valid
    assert result.rule_id == "IDENTITY_INVOICE_COVERAGE_V1"
    assert (result.lhs_paise, result.rhs_paise) == (10000, 10000)


def test_invoice_identity_rejects_foreign_order_source():
    result = verify_invoice_identity(_order(), [_capture(order_id="ORD-X")], [], 0)
    assert not result.valid
    assert "FOREIGN_SOURCE" in result.error_codes


def test_missing_settlement_sources_fail_closed_instead_of_proving_zero():
    settlement = Settlement("setl-1", 0, "2026-08-13", "", "processed")
    result = verify_settlement_identity(settlement, [], [], [])
    assert not result.valid
    assert "MISSING_SOURCE" in result.error_codes


def test_split_payout_identity_aggregates_every_capture_exactly_once():
    captures = [
        _capture("pay-1", "ORD-1", 10_000, 200, 36),
        _capture("pay-2", "ORD-2", 20_000, 400, 72),
    ]
    settlement = Settlement("setl-1", 29_292, "2026-09-02", "UTR1", "processed")
    result = verify_settlement_identity(settlement, captures, [], [])
    assert result.valid
    assert result.rhs_paise == 29_292


def test_duplicate_source_cannot_be_counted_twice_to_forge_identity():
    capture = _capture(amount=5000, fee=0, tax=0)
    settlement = Settlement("setl-1", 10_000, "2026-08-13", "UTR1", "processed")
    result = verify_settlement_identity(settlement, [capture, capture], [], [])
    assert not result.valid
    assert "DUPLICATE_SOURCE" in result.error_codes


def test_pending_settlement_is_not_accounting_proof():
    capture = _capture()
    settlement = Settlement("setl-1", 9764, "2026-08-13", "UTR1", "pending")
    result = verify_settlement_identity(settlement, [capture], [], [])
    assert not result.valid
    assert "UNSETTLED_EVENT" in result.error_codes


def test_tds_without_any_payment_source_cannot_prove_invoice():
    result = verify_invoice_identity(_order(), [], [], 10_000)
    assert not result.valid
    assert "MISSING_SOURCE" in result.error_codes


def test_tds_without_external_source_reference_is_rejected():
    payment = _capture(amount=9000, fee=0, tax=0)
    result = verify_invoice_identity(_order(), [payment], [], 1000)
    assert not result.valid
    assert "TDS_SOURCE_MISSING" in result.error_codes


def test_settlement_identity_issues_independently_verifiable_certificate():
    capture = _capture()
    settlement = Settlement("setl-1", 9764, "2026-08-13", "UTR1", "processed")
    identity = verify_settlement_identity(settlement, [capture], [], [])
    rows = [asdict(settlement), asdict(capture)]
    certificate = build_identity_certificate(identity, rows, subject_id="setl-1")
    checked = verify_identity_certificate(certificate, rows)
    assert checked.valid, checked.error_codes


def test_identity_verifier_rejects_one_paisa_source_tamper_even_if_sources_rehashed():
    capture = _capture()
    settlement = Settlement("setl-1", 9764, "2026-08-13", "UTR1", "processed")
    identity = verify_settlement_identity(settlement, [capture], [], [])
    tampered_rows = [asdict(replace(settlement, amount_paise=9765)), asdict(capture)]
    certificate = build_identity_certificate(identity, tampered_rows, subject_id="setl-1")
    checked = verify_identity_certificate(certificate, tampered_rows)
    assert not checked.valid
    assert "IDENTITY_AMOUNT_MISMATCH" in checked.error_codes


def test_identity_verifier_rejects_rehashed_adjustment_with_contradictory_sign():
    capture = _capture()
    adjustment = Adjustment("adj-1", "setl-1", -50, "debit", "2026-08-12")
    settlement = Settlement("setl-1", 9714, "2026-08-13", "UTR1", "processed")
    identity = verify_settlement_identity(settlement, [capture], [], [adjustment])
    forged_adjustment = {**asdict(adjustment), "event_type": "adjustment", "kind": "credit"}
    rows = [asdict(settlement), asdict(capture), forged_adjustment]
    certificate = build_identity_certificate(identity, rows, subject_id="setl-1")
    checked = verify_identity_certificate(certificate, rows)
    assert not checked.valid
    assert "IDENTITY_SOURCE_INVALID" in checked.error_codes
