"""Two candidates within 5 points -> AMBIGUOUS, never a guess (FR-2.6, AC-7)."""

from ledger_daemon.models import BankTxn, Order, Verdict
from ledger_daemon.recon import reconcile


def test_duplicate_utr_resolves_ambiguous(batch):
    orders, captures, bank, truth = batch
    dup_ids = [oid for oid, t in truth.items() if t["true_verdict"] == "ambiguous"]
    assert dup_ids, "generator must inject duplicate-UTR cases"
    result = reconcile(orders, captures, bank)
    for oid in dup_ids:
        assert result.verdicts[oid].verdict is Verdict.AMBIGUOUS, oid


def test_two_similar_candidates_tie_to_ambiguous():
    o1 = Order("ORD-A", "INV-100", "CUST-A", "KR ENTERPRISES", 70_000_00,
               "2026-08-10", "unpaid", "bank_transfer")
    o2 = Order("ORD-B", "INV-101", "CUST-B", "K R ENTERPRISE", 70_000_00,
               "2026-08-10", "unpaid", "bank_transfer")
    b1 = BankTxn("TXN-A", "2026-08-11", 70_000_00, "credit", "AXISN111111111",
                 "IMPS/P2A/612938471/K R ENTERPRIS", 0)
    b2 = BankTxn("TXN-B", "2026-08-11", 70_000_00, "credit", "HDFCN222222222",
                 "NEFT-HDFCN52200981-KR ENTERPRISES", 0)
    res = reconcile([o1, o2], [], [b1, b2])
    # both orders score both credits within the tie margin -> nobody may guess
    assert res.verdicts["ORD-A"].verdict is Verdict.AMBIGUOUS
    assert res.verdicts["ORD-B"].verdict is Verdict.AMBIGUOUS


def test_no_greedy_collision_two_orders_one_credit():
    """One credit, two amount-identical orders: at most one may claim it, and only
    with clear separation — a greedy matcher would give it to both or guess."""
    o1 = Order("ORD-A", "INV-100", "CUST-A", "GUPTA FOODS", 30_000_00,
               "2026-08-10", "unpaid", "bank_transfer")
    o2 = Order("ORD-B", "INV-101", "CUST-B", "GUPTA FOODS LLP", 30_000_00,
               "2026-08-10", "unpaid", "bank_transfer")
    b = BankTxn("TXN-C", "2026-08-11", 30_000_00, "credit", "SBINN333333333",
                "NEFT-SBINP11111111-GUPTA FOODS", 0)
    res = reconcile([o1, o2], [], [b])
    claimed = [oid for oid in ("ORD-A", "ORD-B")
               if res.verdicts[oid].verdict is Verdict.PAID_OUT_OF_BAND]
    assert len(claimed) <= 1  # physically impossible for both to have been paid by one credit
