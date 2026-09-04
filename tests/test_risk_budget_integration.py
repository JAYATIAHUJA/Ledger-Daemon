"""Policy must hold uncalibrated fuzzy evidence while retaining exact eligibility."""

from ledger_daemon import policy
from ledger_daemon.fs import FSModel
from ledger_daemon.models import BankTxn, Evidence, GatewayCapture, Order, OrderVerdict, Verdict
from ledger_daemon.recon import FULL, ReconConfig, reconcile
from ledger_daemon.risk_control import RiskCalibration


def _order() -> Order:
    return Order("ORD-1", "INV-1", "CUST-1", "TEST TRADERS", 50_000_00,
                 "2026-08-10", "unpaid", "gateway")


def test_probabilistic_chase_without_authorized_calibration_holds_before_allow():
    verdict = OrderVerdict(
        "ORD-1", Verdict.GENUINELY_UNPAID, [],
        Evidence("pass4_fuzzy", automation_path="probabilistic", risk_authorized=False),
    )

    decision = policy.evaluate(_order(), verdict, "CREATE_PAYMENT_LINK", 0, 0)

    assert decision.outcome == policy.HOLD
    assert decision.rule_fired == "R_RISK_BUDGET"


def test_exact_proof_remains_eligible_without_calibration():
    verdict = OrderVerdict(
        "ORD-1", Verdict.GENUINELY_UNPAID, [],
        Evidence("exhausted", automation_path="exact"),
    )

    decision = policy.evaluate(_order(), verdict, "CREATE_PAYMENT_LINK", 0, 0)

    assert decision.outcome == policy.ALLOW
    assert decision.rule_fired == "R_ALLOW"


def test_fuzzy_reconciliation_records_unauthorized_probabilistic_evidence():
    order = _order()
    bank = BankTxn("TXN-1", "2026-08-11", order.amount_paise, "credit", "UTR-1",
                   "NEFT TEST TRADERS INV-1", 0)

    verdict = reconcile([order], [], [bank]).verdicts[order.order_id]

    assert verdict.evidence.automation_path == "probabilistic"
    assert verdict.evidence.risk_calibration_id == ""
    assert not verdict.evidence.risk_authorized
    assert type(verdict.evidence.score_ppm) is int


def test_fuzzy_reconciliation_records_authorized_calibration():
    order = _order()
    bank = BankTxn("TXN-1", "2026-08-11", order.amount_paise, "credit", "UTR-1",
                   "NEFT TEST TRADERS INV-1", 0)
    calibration = RiskCalibration("cal-1", 0, 0, 10_000, 3, True)

    verdict = reconcile([order], [], [bank], config=ReconConfig(risk_calibration=calibration)).verdicts[order.order_id]

    assert verdict.evidence.automation_path == "probabilistic"
    assert verdict.evidence.risk_calibration_id == "cal-1"
    assert verdict.evidence.risk_authorized


def test_exact_reconciliation_records_exact_eligible_evidence():
    order = _order()
    capture = GatewayCapture("PAY-1", order.order_id, order.amount_paise, 0, 0, "captured",
                             "upi", "2026-08-10", "SETTLE-1", "UTR-1")
    bank = BankTxn("TXN-1", "2026-08-11", order.amount_paise, "credit", "UTR-1",
                   "SETTLEMENT CREDIT", 0)

    verdict = reconcile([order], [capture], [bank]).verdicts[order.order_id]

    assert verdict.evidence.automation_path == "exact"
    assert verdict.evidence.risk_authorized


def test_manual_evidence_holds_a_recognized_chase_action():
    verdict = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("operator_note"))

    decision = policy.evaluate(_order(), verdict, "CREATE_PAYMENT_LINK", 0, 0)

    assert decision.outcome == policy.HOLD
    assert decision.rule_fired == "R_RISK_BUDGET"


def test_empty_calibration_id_cannot_authorize_probabilistic_evidence():
    verdict = OrderVerdict(
        "ORD-1", Verdict.GENUINELY_UNPAID, [],
        Evidence("pass4_fuzzy", automation_path="probabilistic", risk_authorized=True),
    )

    decision = policy.evaluate(_order(), verdict, "CREATE_PAYMENT_LINK", 0, 0)

    assert decision.outcome == policy.HOLD
    assert decision.rule_fired == "R_RISK_BUDGET"


def test_unknown_action_keeps_default_deny_before_risk_budget():
    verdict = OrderVerdict(
        "ORD-1", Verdict.GENUINELY_UNPAID, [],
        Evidence("pass4_fuzzy", automation_path="probabilistic", risk_authorized=False),
    )

    decision = policy.evaluate(_order(), verdict, "UNRECOGNIZED_ACTION", 0, 0)

    assert decision.outcome == policy.DENY
    assert decision.rule_fired == "R_DEFAULT_DENY"


def test_attempt_cap_keeps_deny_before_risk_budget():
    verdict = OrderVerdict(
        "ORD-1", Verdict.GENUINELY_UNPAID, [],
        Evidence("pass4_fuzzy", automation_path="probabilistic", risk_authorized=False),
    )

    decision = policy.evaluate(_order(), verdict, "CREATE_PAYMENT_LINK", policy.MAX_ATTEMPTS, 0)

    assert decision.outcome == policy.DENY
    assert decision.rule_fired == "R3_DENY_MAX_ATTEMPTS"


def test_rejected_amount_compatible_candidate_cannot_fall_through_as_exact_unpaid():
    order = _order()
    bank = BankTxn("TXN-REJECTED", "2026-08-15", order.amount_paise, "credit", "UTR-1",
                   "NEFT TEST TRADERS INV-1", 0)

    verdict = reconcile([order], [], [bank], fs_model=FSModel(prior_log_odds=-100)).verdicts[order.order_id]
    decision = policy.evaluate(order, verdict, "CREATE_PAYMENT_LINK", 0, 0)

    assert verdict.verdict is Verdict.GENUINELY_UNPAID
    assert verdict.evidence.pass_used == "pass4_rejected"
    assert verdict.evidence.automation_path == "probabilistic"
    assert not verdict.evidence.risk_authorized
    assert decision.outcome == policy.HOLD
    assert decision.rule_fired == "R_RISK_BUDGET"

    from ledger_daemon import certificates, verifier
    rows = certificates.source_rows([order], [], [bank])
    config_hash = certificates.recon_config_hash(FULL)
    conformal_calibration_id = "conformal:rejected-candidate"
    certificate = certificates.build_certificate(
        order, verdict, certificates.source_hash_map(rows), config_hash,
        conformal_calibration_id, rows=rows,
    )
    checked = verifier.verify_certificate(
        certificate, rows, expected_config_hash=config_hash,
        expected_calibration_id=conformal_calibration_id,
    )

    assert certificate.rule_ids[0] == "RECON.pass4_rejected"
    assert checked.valid, checked.error_codes

    from dataclasses import replace
    from ledger_daemon.source_contracts import sha256_hex
    forged = replace(certificate, automation_path="exact", risk_authorized=True)
    forged = replace(forged, proof_hash=sha256_hex(forged._payload()))
    rejected = verifier.verify_certificate(
        forged, rows, expected_config_hash=config_hash,
        expected_calibration_id=conformal_calibration_id,
    )
    assert "RISK_PROVENANCE_INVALID" in rejected.error_codes
