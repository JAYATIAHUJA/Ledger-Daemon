"""PAID_NET_OF_TDS: a compliant B2B payer must never be dunned.

The scenario: the customer pays the invoice minus a statutory TDS rate by bank
transfer. Before this verdict existed, the exact-amount candidate gate never even
considered that credit, so the order came out GENUINELY_UNPAID and the payer got
chased for tax they had already deposited against the merchant's PAN.
"""

from ledger_daemon import policy
from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.models import BankTxn, Evidence, Order, OrderVerdict, Verdict
from ledger_daemon.money import pct_bp, sub, tds_rate_bp
from ledger_daemon.recon import reconcile


def _order(amount=100_000_00):
    return Order("ORD-1", "INV-2291", "CUST-1", "SHARMA TEXTILES PVT LTD",
                 amount, "2026-08-10", "unpaid", "bank_transfer")


def _credit(amount, narration="NEFT-HDFCP12345678-SHARMA TEXTILES PVT-INV2291"):
    return BankTxn("TXN-9", "2026-08-11", amount, "credit", "HDFCN777777777", narration, 0)


# ---- the detector itself: exact statutory rates, no tolerance ----------------

def test_detector_recognises_each_statutory_rate():
    gross = 100_000_00
    for bp in (100, 200, 1000):
        net = sub(gross, pct_bp(gross, bp))
        assert tds_rate_bp(gross, net) == bp


def test_detector_rejects_non_statutory_shortfalls():
    gross = 100_000_00
    assert tds_rate_bp(gross, gross) is None                       # paid in full
    assert tds_rate_bp(gross, sub(gross, pct_bp(gross, 300))) is None   # 3% is not a rate
    assert tds_rate_bp(gross, sub(gross, pct_bp(gross, 200)) - 1) is None  # one paisa off
    assert tds_rate_bp(gross, 0) is None
    assert tds_rate_bp(gross, gross + 100) is None                 # overpayment


# ---- reconciliation: the net credit becomes a candidate and a verdict --------

def test_net_of_tds_credit_is_matched_not_chased():
    o = _order()
    b = _credit(sub(o.amount_paise, pct_bp(o.amount_paise, 200)))  # 2% withheld
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.PAID_NET_OF_TDS
    assert v.money_received_paise == b.amount_paise
    assert "2% statutory TDS" in v.reason


def test_ten_percent_professional_fee_withholding():
    o = _order()
    b = _credit(sub(o.amount_paise, pct_bp(o.amount_paise, 1000)))
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.PAID_NET_OF_TDS
    assert "10% statutory TDS" in v.reason


def test_a_non_statutory_short_payment_stays_chaseable():
    """97% of the invoice is a short payment, not TDS — it must NOT be excused."""
    o = _order()
    b = _credit(sub(o.amount_paise, pct_bp(o.amount_paise, 300)))
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.GENUINELY_UNPAID


# ---- policy: the verdict can never reach the executor ------------------------

def test_policy_denies_with_a_tds_specific_rule():
    v = OrderVerdict("ORD-1", Verdict.PAID_NET_OF_TDS, [], Evidence("pass4_fuzzy"))
    d = policy.evaluate(_order(), v, "CREATE_PAYMENT_LINK", 0, 0)
    assert d.outcome == policy.DENY
    assert d.rule_fired == "R1_DENY_NET_OF_TDS"
    assert "Form 26AS" in d.detail


# ---- end to end on a generated world -----------------------------------------

def test_no_tds_payer_is_ever_chased_on_a_generated_batch(tmp_path):
    generate(7, 300, str(tmp_path))
    orders, captures, bank, truth = load_batch(str(tmp_path))
    result = reconcile(orders, captures, bank, q_hat=0.001)
    tds_ids = {oid for oid, t in truth.items() if t["true_verdict"] == "paid_net_of_tds"}
    assert tds_ids, "the generator must produce TDS cases"
    for o in orders:
        if o.order_id not in tds_ids:
            continue
        d = policy.evaluate(o, result.verdicts[o.order_id], "CREATE_PAYMENT_LINK", 0, 0)
        assert d.outcome != policy.ALLOW, \
            f"{o.order_id}: a compliant TDS payer was chased via {d.rule_fired}"
