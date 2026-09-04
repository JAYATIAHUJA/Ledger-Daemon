"""Every exception is a case, and a case moves only along declared edges.

The exception list is where reconciliation admits it does not know. That
admission is worthless if the follow-up is untracked, so each abstention,
hold, escalation, quarantine and failed proof becomes a persisted case whose
history is append-only and whose transitions are checked against a declared
graph. An illegal or stale transition must be a no-op, not a repair.
"""

import pytest

from ledger_daemon.cases import (
    REASON_CODES,
    TERMINAL_STATES,
    CaseState,
    CaseStore,
    IllegalTransition,
    UnknownCase,
    VersionConflict,
)


@pytest.fixture
def store(tmp_path):
    return CaseStore(str(tmp_path / "ledger.sqlite3"))


def test_open_case_starts_at_open_version_one(store):
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    assert case.state is CaseState.OPEN
    assert case.version == 1
    assert case.order_id == "ORD-1"
    assert case.certificate_id == "proof-1"
    assert [e.to_state for e in store.events(case.case_id)] == [CaseState.OPEN]


def test_open_case_is_idempotent_per_order_and_reason(store):
    first = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    again = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-2")
    assert again.case_id == first.case_id
    assert again.version == 1
    assert len(store.events(first.case_id)) == 1
    other = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    assert other.case_id != first.case_id


def test_case_id_is_deterministic_across_stores(tmp_path):
    a = CaseStore(str(tmp_path / "a.sqlite3")).open_case("ORD-1", "POLICY_HOLD", "p")
    b = CaseStore(str(tmp_path / "b.sqlite3")).open_case("ORD-1", "POLICY_HOLD", "q")
    assert a.case_id == b.case_id


def test_unknown_reason_code_fails_closed(store):
    with pytest.raises(ValueError):
        store.open_case("ORD-1", "BECAUSE_I_SAID_SO", "proof-1")
    assert REASON_CODES == frozenset({
        "AMBIGUOUS_MATCH", "POLICY_HOLD", "POLICY_ESCALATION",
        "QUARANTINED_SOURCE", "PROOF_UNVERIFIED",
    })


def test_the_declared_happy_path_walks_end_to_end(store):
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    case = store.transition(case.case_id, 1, CaseState.ASSIGNED, "analyst-1")
    assert case.version == 2 and case.state is CaseState.ASSIGNED
    for target in (CaseState.INVESTIGATING, CaseState.EVIDENCE_REQUESTED,
                   CaseState.EVIDENCE_RECEIVED, CaseState.VERIFIED, CaseState.RESOLVED):
        case = store.transition(case.case_id, case.version, target, "analyst-1")
    assert case.state is CaseState.RESOLVED
    assert case.version == 7
    assert [e.seq for e in store.events(case.case_id)] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.parametrize("state", [
    CaseState.OPEN, CaseState.ASSIGNED, CaseState.INVESTIGATING,
    CaseState.EVIDENCE_REQUESTED, CaseState.EVIDENCE_RECEIVED, CaseState.VERIFIED,
])
def test_any_nonterminal_state_may_escalate(store, state):
    case = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    case = store.advance(case.case_id, 1, _path_to(state), "analyst-1")
    case = store.transition(case.case_id, case.version, CaseState.ESCALATED, "analyst-1")
    assert case.state is CaseState.ESCALATED


@pytest.mark.parametrize("target", [
    CaseState.RESOLVED, CaseState.VERIFIED, CaseState.EVIDENCE_RECEIVED, CaseState.OPEN,
])
def test_illegal_transitions_change_nothing(store, target):
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    with pytest.raises(IllegalTransition):
        store.transition(case.case_id, 1, target, "analyst-1")
    after = store.get(case.case_id)
    assert after.version == 1 and after.state is CaseState.OPEN
    assert len(store.events(case.case_id)) == 1


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_a_terminal_case_accepts_nothing_further(store, terminal):
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    case = store.advance(case.case_id, 1, _path_to(CaseState.VERIFIED), "analyst-1")
    case = store.transition(case.case_id, case.version, terminal, "analyst-1")
    for target in CaseState:
        with pytest.raises(IllegalTransition):
            store.transition(case.case_id, case.version, target, "analyst-1")
    assert store.get(case.case_id).version == case.version


def test_a_stale_version_is_refused_not_applied(store):
    case = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    store.transition(case.case_id, 1, CaseState.ASSIGNED, "analyst-1")
    with pytest.raises(VersionConflict) as exc:
        store.transition(case.case_id, 1, CaseState.INVESTIGATING, "analyst-2")
    assert exc.value.expected == 1 and exc.value.actual == 2
    assert store.get(case.case_id).state is CaseState.ASSIGNED
    assert len(store.events(case.case_id)) == 2


def test_unknown_case_is_refused(store):
    with pytest.raises(UnknownCase):
        store.transition("no-such-case", 1, CaseState.ASSIGNED, "analyst-1")


def test_advance_applies_a_whole_path_or_none_of_it(store):
    case = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")
    with pytest.raises(IllegalTransition):
        store.advance(case.case_id, 1,
                      (CaseState.ASSIGNED, CaseState.RESOLVED), "analyst-1")
    after = store.get(case.case_id)
    assert after.version == 1 and after.state is CaseState.OPEN
    assert len(store.events(case.case_id)) == 1


def test_events_record_actor_and_evidence(store):
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    store.transition(case.case_id, 1, CaseState.ASSIGNED, "analyst-1",
                     evidence_refs=("TXN-9", "PAY-3"))
    event = store.events(case.case_id)[-1]
    assert event.actor == "analyst-1"
    assert event.from_state is CaseState.OPEN
    assert event.to_state is CaseState.ASSIGNED
    assert event.evidence_refs == ("TXN-9", "PAY-3")


def test_open_cases_are_listable_by_order_and_state(store):
    a = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")
    b = store.open_case("ORD-2", "POLICY_HOLD", "proof-2")
    store.advance(b.case_id, 1, _path_to(CaseState.VERIFIED) + (CaseState.RESOLVED,), "analyst-1")
    assert [c.case_id for c in store.by_order("ORD-1")] == [a.case_id]
    assert {c.case_id for c in store.list_cases(open_only=True)} == {a.case_id}
    assert len(store.list_cases()) == 2


def _path_to(state: CaseState) -> tuple[CaseState, ...]:
    chain = (CaseState.ASSIGNED, CaseState.INVESTIGATING, CaseState.EVIDENCE_REQUESTED,
             CaseState.EVIDENCE_RECEIVED, CaseState.VERIFIED)
    if state is CaseState.OPEN:
        return ()
    return chain[:chain.index(state) + 1]
