"""For every verdict outside the chaseable set, the executor is unreachable (AC-5).

The stop may be DENY, HOLD or ESCALATE depending on the verdict's declared
disposition; what is invariant is that it is never ALLOW. See
tests/test_exhaustiveness.py for the disposition table itself.
"""

from ledger_daemon import policy
from ledger_daemon.models import CHASEABLE_VERDICTS, Evidence, Order, OrderVerdict, Verdict


def _order():
    return Order("ORD-1", "INV-1", "CUST-1", "TEST TRADERS", 50_000_00,
                 "2026-08-10", "unpaid", "gateway")


def test_non_chaseable_never_reaches_allow():
    o = _order()
    for verdict in Verdict:
        v = OrderVerdict("ORD-1", verdict, [], Evidence("test"))
        d = policy.evaluate(o, v, "CREATE_PAYMENT_LINK", attempts_so_far=0, contacts_7d=0)
        if verdict in CHASEABLE_VERDICTS:
            assert d.outcome == policy.ALLOW, verdict
        else:
            assert d.outcome in (policy.DENY, policy.HOLD, policy.ESCALATE), verdict
            assert d.rule_fired.startswith("R1_"), verdict


def test_r2_absence_of_evidence_holds():
    o = _order()
    v = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("test"),
                     bank_coverage_ok=False)
    d = policy.evaluate(o, v, "CREATE_PAYMENT_LINK", 0, 0)
    assert d.outcome == policy.HOLD
    assert d.rule_fired == "R2_HOLD_INSUFFICIENT_EVIDENCE"


def test_r3_attempt_cap_and_r4_contact_budget():
    o = _order()
    v = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("test"))
    assert policy.evaluate(o, v, "CREATE_PAYMENT_LINK", 3, 0).rule_fired == "R3_DENY_MAX_ATTEMPTS"
    assert policy.evaluate(o, v, "CREATE_PAYMENT_LINK", 0, 2).rule_fired == "R4_HOLD_CONTACT_BUDGET"


def test_r5_negative_ev_denied():
    o = _order()
    o.amount_paise = 500_00  # EV 0.6*500 = ₹300 <= ₹800 action cost
    v = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("test"))
    assert policy.evaluate(o, v, "CREATE_PAYMENT_LINK", 0, 0).rule_fired == "R5_DENY_NEGATIVE_EV"


def test_r6_auto_charge_needs_mandate():
    o = _order()
    v = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("test"))
    d = policy.evaluate(o, v, "AUTO_CHARGE", 0, 0)
    assert d.outcome == policy.ESCALATE
    assert d.rule_fired == "R6_ESCALATE_AFA_HUMAN"


def test_r7_low_llm_confidence_held():
    o = _order()
    v = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("test"))
    d = policy.evaluate(o, v, "CREATE_PAYMENT_LINK", 0, 0, llm_confidence=0.5)
    assert d.rule_fired == "R7_HOLD_FOR_HUMAN"


def test_default_is_deny():
    o = _order()
    v = OrderVerdict("ORD-1", Verdict.GENUINELY_UNPAID, [], Evidence("test"))
    assert policy.evaluate(o, v, "SEND_MONEY_SOMEWHERE", 0, 0).outcome == policy.DENY
