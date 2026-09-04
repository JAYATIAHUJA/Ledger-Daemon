"""Every verdict must have a declared policy disposition.

The point of these tests is that adding a 10th Verdict is a test failure (and an
ImportError) rather than a silent DENY that nobody notices until a merchant asks
why an order vanished from the chase list.
"""

import pytest

from ledger_daemon import policy
from ledger_daemon.models import (
    CHASEABLE_VERDICTS,
    Evidence,
    Order,
    OrderVerdict,
    Verdict,
)


def _order(amount_paise: int = 50_000_00) -> Order:
    return Order(
        order_id="o1", invoice_no="INV-1", customer_id="c1", customer_name="Acme",
        amount_paise=amount_paise, due_date="2026-01-10", status="unpaid",
        channel_expected="gateway",
    )


def _verdict(v: Verdict) -> OrderVerdict:
    return OrderVerdict(
        order_id="o1", verdict=v, evidence_refs=[], evidence=Evidence(pass_used="test"),
        bank_coverage_ok=True,
    )


def test_every_verdict_has_a_disposition():
    unmapped = [v.value for v in Verdict if v not in policy.VERDICT_DISPOSITION]
    assert unmapped == [], f"verdicts with no declared disposition: {unmapped}"


def test_disposition_table_agrees_with_the_taxonomy():
    chase = {v for v, d in policy.VERDICT_DISPOSITION.items()
             if d is policy.Disposition.CHASE}
    assert chase == set(CHASEABLE_VERDICTS)


def test_an_undeclared_verdict_fails_the_exhaustiveness_check():
    """Simulate someone adding a verdict and forgetting the policy decision."""
    saved = dict(policy.VERDICT_DISPOSITION)
    try:
        del policy.VERDICT_DISPOSITION[Verdict.CHARGEBACK_OPEN]
        with pytest.raises(ImportError, match="not exhaustive"):
            policy._assert_exhaustive()
    finally:
        policy.VERDICT_DISPOSITION.clear()
        policy.VERDICT_DISPOSITION.update(saved)
    policy._assert_exhaustive()  # restored


def test_r1_returns_a_decision_for_every_non_chaseable_verdict():
    for v in Verdict:
        gate = policy._r1(v)
        if v in CHASEABLE_VERDICTS:
            assert gate is None, f"{v.value} must fall through R1 to the later gates"
        else:
            assert gate is not None, f"{v.value} must be stopped by R1"
            assert gate.outcome in (policy.DENY, policy.HOLD, policy.ESCALATE)
            assert gate.rule_fired.startswith("R1_")
            assert v.value in gate.detail


def test_no_verdict_outside_the_chaseable_set_can_ever_reach_allow():
    """The invariant the whole product rests on, asserted over the full taxonomy."""
    for v in Verdict:
        if v in CHASEABLE_VERDICTS:
            continue
        d = policy.evaluate(_order(), _verdict(v), "CREATE_PAYMENT_LINK",
                            attempts_so_far=0, contacts_7d=0)
        assert d.outcome != policy.ALLOW, f"{v.value} reached ALLOW via {d.rule_fired}"


def test_open_disputes_escalate_rather_than_silently_deny():
    d = policy.evaluate(_order(), _verdict(Verdict.CHARGEBACK_OPEN), "CREATE_PAYMENT_LINK",
                        attempts_so_far=0, contacts_7d=0)
    assert d.outcome == policy.ESCALATE
    assert d.rule_fired == "R1_ESCALATE_DISPUTE_OPEN"


def test_ambiguous_holds_for_a_human_rather_than_denying():
    """AMBIGUOUS is an abstention, not a judgement that nothing is owed."""
    d = policy.evaluate(_order(), _verdict(Verdict.AMBIGUOUS), "CREATE_PAYMENT_LINK",
                        attempts_so_far=0, contacts_7d=0)
    assert d.outcome == policy.HOLD
    assert d.rule_fired == "R1_HOLD_AMBIGUOUS"
