"""The chase list must be honest: every order in exactly one place, clicks audited."""

import pytest

from ledger_daemon import policy
from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.evaluate import run_ledger_daemon
from ledger_daemon.executor import Executor
from ledger_daemon.recon import reconcile
from ledger_daemon.cases import CaseState, CaseStore, VersionConflict, open_exception_cases
from ledger_daemon.ui import (
    _rows_html,
    build_view,
    load_resolutions,
    resolve_case,
    save_resolution,
)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    d = tmp_path_factory.mktemp("ui-world")
    generate(11, 200, str(d))
    orders, captures, bank, truth = load_batch(str(d))
    result = reconcile(orders, captures, bank, q_hat=0.001)
    _ld, decisions = run_ledger_daemon(orders, result)
    return orders, result, decisions, truth, captures, bank


def world_with_feeds(world):
    """The shared world unpacked the way the dashboard panels need it."""
    orders, result, decisions, _truth, captures, bank = world
    return orders, captures, bank, result, decisions


def test_every_order_lands_in_exactly_one_place(world):
    orders, result, decisions, _, _captures, _bank = world
    v = build_view(orders, result.verdicts, decisions, {})
    shown = [r.order_id for r in v.safe + v.blocked + v.needs_you]
    assert len(shown) == len(set(shown)), "an order appeared in two columns"
    assert len(shown) + v.hidden_clean == len(orders)


def test_safe_column_is_exactly_the_allow_set(world):
    orders, result, decisions, _, _captures, _bank = world
    v = build_view(orders, result.verdicts, decisions, {})
    assert {r.order_id for r in v.safe} == \
        {oid for oid, d in decisions.items() if d.outcome == policy.ALLOW}


def test_hidden_rows_are_only_clean_agreements(world):
    """Hiding is honesty, not concealment: only books-say-paid, bank-agrees rows."""
    orders, result, decisions, _, _captures, _bank = world
    v = build_view(orders, result.verdicts, decisions, {})
    visible = {r.order_id for r in v.safe + v.blocked + v.needs_you}
    by_id = {o.order_id: o for o in orders}
    for oid, d in decisions.items():
        if oid in visible:
            continue
        assert d.rule_fired == "R1_DENY_ALREADY_PAID"
        assert by_id[oid].status == "paid"


def test_a_resolution_moves_the_row_and_only_that_row(world):
    orders, result, decisions, _, _captures, _bank = world
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


# --------------------------- exception cases -------------------------------- #

def test_held_rows_carry_the_case_they_belong_to(world, tmp_path):
    orders, result, decisions, _, _captures, _bank = world
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    open_exception_cases(store, result.verdicts, decisions)
    cases = {c.order_id: c for c in store.list_cases()}

    v = build_view(orders, result.verdicts, decisions, {}, cases)
    assert v.needs_you, "world must produce at least one HOLD"
    for row in v.needs_you:
        assert row.case_id and row.case_state == "OPEN" and row.case_version == 1
    for row in v.safe:
        assert row.case_id == "", "an allowed chase is not an exception"


def test_a_row_without_a_case_offers_no_resolution_buttons(world):
    orders, result, decisions, _, _captures, _bank = world
    v = build_view(orders, result.verdicts, decisions, {})
    assert v.needs_you and all(row.case_id == "" for row in v.needs_you)
    assert "acts" not in _rows_html(v.needs_you, True)


def test_resolving_walks_the_case_along_declared_states(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")

    moved = resolve_case(store, case, 1, "paid", actor="analyst-1")

    assert moved.state is CaseState.RESOLVED
    events = store.events(case.case_id)
    assert [e.to_state.value for e in events] == [
        "OPEN", "ASSIGNED", "INVESTIGATING", "VERIFIED", "RESOLVED"]
    assert events[-1].evidence_refs == ("human-resolution:paid",)
    assert events[-1].actor == "analyst-1"


def test_a_stale_screen_cannot_overwrite_a_colleague(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    store.transition(case.case_id, 1, CaseState.ASSIGNED, "analyst-1")

    with pytest.raises(VersionConflict):
        resolve_case(store, case, 1, "unpaid")          # rendered at v1, store at v2

    assert store.get(case.case_id).state is CaseState.ASSIGNED
    assert len(store.events(case.case_id)) == 2


def test_resolution_refuses_a_verdict_it_does_not_understand(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    with pytest.raises(ValueError):
        resolve_case(store, case, 1, "maybe")
    assert store.get(case.case_id).version == 1


# --------------------------- the operations dashboard (F10) ----------------- #

def test_panels_gather_source_health_proofs_and_cases(world, tmp_path):
    import os

    from ledger_daemon.cases import CaseStore, open_exception_cases
    from ledger_daemon.certificates import recon_config_hash, write_proof_bundle
    from ledger_daemon.executor import Executor, MockRazorpayAdapter
    from ledger_daemon.proof_tree import load_certificates
    from ledger_daemon.recon import FULL
    from ledger_daemon.ui import build_panels

    orders, captures, bank, result, decisions = world_with_feeds(world)
    proofs = str(tmp_path / "proofs")
    write_proof_bundle(proofs, orders, captures, bank, result.verdicts,
                       config_hash=recon_config_hash(FULL), calibration_id="cal-x")
    execu = Executor(str(tmp_path / "l.sqlite3"), adapter=MockRazorpayAdapter())
    store = CaseStore(execu.db_path)
    open_exception_cases(store, result.verdicts, decisions)
    state = {"resolutions": {}, "cases": {c.order_id: c for c in store.list_cases()},
             "certificates": load_certificates(proofs)}

    panels = build_panels(orders, result.verdicts, decisions, execu, store, state,
                          captures=captures, bank=bank,
                          config_hash=recon_config_hash(FULL), calibration_id="cal-x")

    assert panels.source.feeds_seen == 3
    assert panels.source.accepted == panels.source.rows_offered
    assert panels.proofs.built == len(orders)
    assert panels.proofs.rejected == 0
    assert panels.proofs.unverified == 0
    assert panels.signoff is not None
    assert sum(panels.verdict_counts.values()) == len(orders)
    assert sum(panels.automation[k] for k in ("exact", "probabilistic", "manual")) == len(orders)
    assert os.path.isdir(proofs)


def test_a_batch_with_no_bundle_blocks_signoff_rather_than_signing_blind(world, tmp_path):
    from ledger_daemon.cases import CaseStore
    from ledger_daemon.executor import Executor, MockRazorpayAdapter
    from ledger_daemon.signoff import SignoffStatus
    from ledger_daemon.ui import build_panels

    orders, captures, bank, result, decisions = world_with_feeds(world)
    execu = Executor(str(tmp_path / "l.sqlite3"), adapter=MockRazorpayAdapter())
    store = CaseStore(execu.db_path)
    state = {"resolutions": {}, "cases": {}, "certificates": {}}

    panels = build_panels(orders, result.verdicts, decisions, execu, store, state,
                          captures=captures, bank=bank)

    assert panels.signoff.status is SignoffStatus.DO_NOT_SIGN
    assert "PROOF_COVERAGE_INCOMPLETE" in panels.signoff.blockers
    assert any("no proof bundle" in note.lower() for note in panels.notes)


def test_every_panel_renders_and_the_close_view_needs_no_javascript(world):
    from ledger_daemon.panels import Panels
    from ledger_daemon.ui import build_view, render_html

    orders, _captures, _bank, result, decisions = world_with_feeds(world)
    view = build_view(orders, result.verdicts, decisions, {})
    html = render_html(view, "test source", Panels())

    for panel in ("close", "chase", "sources", "proofs", "cases", "risk", "run",
                  "evaluation", "audit", "recovery"):
        assert f'data-panel="{panel}"' in html
    # the landing panel is served visible, so a page without script still reads
    assert '<section class="panel" data-panel="close">' in html
    assert '<section class="panel" data-panel="chase" hidden>' in html
    assert "Downstream Control Demonstration: Why Correct Reconciliation Matters" in html
    assert "Track 03" not in html


def test_recovery_is_framed_as_a_demonstration_not_a_product(world):
    from ledger_daemon.panels import recovery_demo
    from ledger_daemon.ui import build_view

    orders, _captures, _bank, result, decisions = world_with_feeds(world)
    view = build_view(orders, result.verdicts, decisions, {})
    numbers = recovery_demo(view)
    assert numbers["blocked"] >= 0
    assert numbers["protected_paise"] == view.total(view.blocked)
    assert type(numbers["protected_paise"]) is int
