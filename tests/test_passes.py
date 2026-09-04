"""Each reconciliation pass matches its intended fixture (FR-2.1..2.4)."""

from ledger_daemon.models import BankTxn, GatewayCapture, Order, Verdict
from ledger_daemon.recon import reconcile


def _order(oid="ORD-1", amount=100_000_00, due="2026-08-10", name="SHARMA TEXTILES PVT LTD"):
    return Order(oid, "INV-2291", "CUST-1", name, amount, due, "unpaid", "gateway")


def _capture(oid="ORD-1", amount=100_000_00, sid="rzp_settlement_1", utr="AXISN123456789",
             status="captured", day="2026-08-10"):
    fee = amount * 200 // 10_000
    tax = fee * 1800 // 10_000
    return GatewayCapture("pay_1", oid, amount, fee, tax, status, "card", day, sid, utr)


def test_pass1_exact_utr_join():
    o = _order()
    c = _capture()
    net = c.amount_paise - c.fee_paise - c.tax_paise
    # narration deliberately lacks the settlement id -> only the UTR can join it
    b = BankTxn("TXN-1", "2026-08-11", net, "credit", c.utr, "SETTLEMENT CREDIT", 0)
    v = reconcile([o], [c], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.SETTLED_CLEAN
    assert v.evidence.pass_used == "pass1_exact_utr"
    assert "TXN-1" in v.evidence_refs


def test_pass2_amount_date_window():
    o = _order()
    c = _capture(utr="")  # no UTR, no settlement narration -> amount+date is all we have
    net = c.amount_paise - c.fee_paise - c.tax_paise
    b = BankTxn("TXN-2", "2026-08-12", net, "credit", "OTHERUTR9999999", "BATCH CREDIT", 0)
    v = reconcile([o], [c], [b]).verdicts["ORD-1"]
    assert v.verdict in (Verdict.SETTLED_CLEAN, Verdict.SETTLED_LATE)
    assert v.evidence.pass_used == "pass2_amount_date"


def test_pass3_split_settlement_aggregation():
    orders = [_order("ORD-1"), _order("ORD-2", amount=50_000_00)]
    c1 = _capture("ORD-1")
    c2 = _capture("ORD-2", amount=50_000_00)
    c2.payment_id = "pay_2"
    total = sum(c.amount_paise - c.fee_paise - c.tax_paise for c in (c1, c2))
    b = BankTxn("TXN-3", "2026-08-11", total, "credit", c1.utr,
                "RAZORPAYSETTLEMENT-rzp_settlement_1", 0)
    res = reconcile(orders, [c1, c2], [b])
    assert res.verdicts["ORD-1"].verdict is Verdict.SETTLED_CLEAN
    assert res.verdicts["ORD-2"].verdict is Verdict.SETTLED_CLEAN
    assert res.verdicts["ORD-1"].evidence.pass_used == "pass3_settlement_id"


def test_pass4_out_of_band_fuzzy():
    o = _order()
    b = BankTxn("TXN-4", "2026-08-11", o.amount_paise, "credit", "HDFCN987654321",
                "NEFT-AXISP00234119-SHARMA TEXTILES PVT-INV2291", 0)
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.PAID_OUT_OF_BAND
    assert v.evidence.pass_used == "pass4_fuzzy"
    assert v.evidence.weight_waterfall  # the explanation is the arithmetic


def test_pass4_requires_amount_agreement():
    o = _order()
    b = BankTxn("TXN-5", "2026-08-11", o.amount_paise - 100, "credit", "HDFCN987654321",
                "NEFT-AXISP00234119-SHARMA TEXTILES PVT-INV2291", 0)
    v = reconcile([o], [], [b]).verdicts["ORD-1"]
    assert v.verdict is Verdict.GENUINELY_UNPAID  # amount gate: no candidate at all


def test_failed_never_debited():
    o = _order()
    c = _capture(status="failed", sid="", utr="")
    v = reconcile([o], [c], []).verdicts["ORD-1"]
    assert v.verdict is Verdict.FAILED_NOT_DEBITED


def test_chargeback_freezes():
    o = _order()
    c = _capture(status="chargeback_open")
    v = reconcile([o], [c], []).verdicts["ORD-1"]
    assert v.verdict is Verdict.CHARGEBACK_OPEN
