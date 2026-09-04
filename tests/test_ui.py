"""The chase list must be honest: every order in exactly one place, clicks audited."""

import pytest

from ledger_daemon import policy
from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.evaluate import run_ledger_daemon
from ledger_daemon.executor import Executor
from ledger_daemon.recon import reconcile
from ledger_daemon.ui import build_view, load_resolutions, save_resolution


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    d = tmp_path_factory.mktemp("ui-world")
    generate(11, 200, str(d))
    orders, captures, bank, truth = load_batch(str(d))
    result = reconcile(orders, captures, bank, q_hat=0.001)
    _ld, decisions = run_ledger_daemon(orders, result)
    return orders, result, decisions, truth


def test_every_order_lands_in_exactly_one_place(world):
    orders, result, decisions, _ = world
    v = build_view(orders, result.verdicts, decisions, {})
    shown = [r.order_id for r in v.safe + v.blocked + v.needs_you]
    assert len(shown) == len(set(shown)), "an order appeared in two columns"
    assert len(shown) + v.hidden_clean == len(orders)


def test_safe_column_is_exactly_the_allow_set(world):
    orders, result, decisions, _ = world
    v = build_view(orders, result.verdicts, decisions, {})
    assert {r.order_id for r in v.safe} == \
        {oid for oid, d in decisions.items() if d.outcome == policy.ALLOW}


def test_hidden_rows_are_only_clean_agreements(world):
    """Hiding is honesty, not concealment: only books-say-paid, bank-agrees rows."""
    orders, result, decisions, _ = world
    v = build_view(orders, result.verdicts, decisions, {})
    visible = {r.order_id for r in v.safe + v.blocked + v.needs_you}
    by_id = {o.order_id: o for o in orders}
    for oid, d in decisions.items():
        if oid in visible:
            continue
        assert d.rule_fired == "R1_DENY_ALREADY_PAID"
        assert by_id[oid].status == "paid"


def test_a_resolution_moves_the_row_and_only_that_row(world):
    orders, result, decisions, _ = world
    v0 = build_view(orders, result.verdicts, decisions, {})
    assert v0.needs_you, "world must produce at least one HOLD"
    target = v0.needs_you[0].order_id

    v_paid = build_view(orders, result.verdicts, decisions, {target: "paid"})
    assert target in {r.order_id for r in v_paid.blocked}
    assert len(v_paid.needs_you) == len(v0.needs_you) - 1

    v_unpaid = build_view(orders, result.verdicts, decisions, {target: "unpaid"})
    assert target in {r.order_id for r in v_unpaid.safe}
    assert len(v_unpaid.safe) == len(v0.safe) + 1


def test_resolutions_persist_through_the_audit_trail(tmp_path):
    db = str(tmp_path / "ledger.sqlite3")
    execu = Executor(db)
    assert save_resolution(execu, "ORD-77", "paid") is True
    assert save_resolution(execu, "ORD-77", "paid") is False  # idempotent, DB-enforced
    save_resolution(execu, "ORD-77", "unpaid")                # a human changed their mind
    assert load_resolutions(db) == {"ORD-77": "unpaid"}       # last write wins
    with pytest.raises(ValueError):
        save_resolution(execu, "ORD-77", "maybe")


def test_load_resolutions_tolerates_a_missing_db(tmp_path):
    assert load_resolutions(str(tmp_path / "absent.sqlite3")) == {}
