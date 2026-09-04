"""No float appears in any monetary path (FR-2.7)."""

import pytest

from ledger_daemon.money import FloatMoneyError, add, paise, pct_bp, sub
from ledger_daemon.recon import net_of, reconcile


def test_money_helpers_reject_floats():
    for bad in (1.0, 99.99, float("nan")):
        with pytest.raises(FloatMoneyError):
            paise(bad)
        with pytest.raises(FloatMoneyError):
            add(100, bad)
        with pytest.raises(FloatMoneyError):
            sub(bad, 100)
        with pytest.raises(FloatMoneyError):
            pct_bp(bad, 200)
    with pytest.raises(FloatMoneyError):
        paise(True) if type(True) is not int else (_ for _ in ()).throw(FloatMoneyError("bool"))


def test_all_generated_amounts_are_int(batch):
    orders, captures, bank, _ = batch
    for o in orders:
        assert type(o.amount_paise) is int
    for c in captures:
        assert type(c.amount_paise) is int
        assert type(c.fee_paise) is int
        assert type(c.tax_paise) is int
        assert type(net_of(c)) is int
    for b in bank:
        assert type(b.amount_paise) is int
        assert type(b.balance_after) is int


def test_verdict_money_fields_are_int(batch):
    orders, captures, bank, _ = batch
    result = reconcile(orders, captures, bank)
    for v in result.verdicts.values():
        assert type(v.money_received_paise) is int
        assert type(v.delta_due_paise) is int
