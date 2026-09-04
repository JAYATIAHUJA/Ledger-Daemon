"""Two analysts, one case, one truth.

The optimistic-concurrency claim is not "we check a version"; it is that a
stale writer cannot overwrite a fresh one. The version read, the event insert
and the state write happen inside one BEGIN IMMEDIATE transaction, so the loser
of a race gets a VersionConflict and the case history stays linear.
"""

import threading

import pytest

from ledger_daemon.cases import CaseState, CaseStore, VersionConflict


def _race(fn, n: int) -> tuple[list, list]:
    ok, conflicts = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def run(i: int) -> None:
        barrier.wait()
        try:
            result = fn(i)
        except VersionConflict as exc:
            with lock:
                conflicts.append(exc)
        else:
            with lock:
                ok.append(result)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ok, conflicts


def test_two_writers_on_the_same_version_produce_one_winner(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "proof-1")

    ok, conflicts = _race(
        lambda i: store.transition(case.case_id, 1, CaseState.ASSIGNED, f"analyst-{i}"), 2)

    assert len(ok) == 1 and len(conflicts) == 1
    assert store.get(case.case_id).version == 2
    assert len(store.events(case.case_id)) == 2


def test_ten_writers_leave_exactly_one_event(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "POLICY_HOLD", "proof-1")

    ok, conflicts = _race(
        lambda i: store.transition(case.case_id, 1, CaseState.ASSIGNED, f"analyst-{i}"), 10)

    assert len(ok) == 1 and len(conflicts) == 9
    events = store.events(case.case_id)
    assert [e.seq for e in events] == [1, 2]
    assert events[-1].actor in {f"analyst-{i}" for i in range(10)}


def test_concurrent_opens_create_exactly_one_case(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))

    ok, conflicts = _race(
        lambda i: store.open_case("ORD-1", "QUARANTINED_SOURCE", "proof-1"), 8)

    assert not conflicts
    assert {c.case_id for c in ok} == {ok[0].case_id}
    assert len(store.list_cases()) == 1
    assert len(store.events(ok[0].case_id)) == 1


def test_a_conflict_reports_the_version_the_writer_must_reread(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "PROOF_UNVERIFIED", "proof-1")
    store.transition(case.case_id, 1, CaseState.ASSIGNED, "analyst-1")

    with pytest.raises(VersionConflict) as exc:
        store.transition(case.case_id, 1, CaseState.ASSIGNED, "analyst-2")
    assert (exc.value.case_id, exc.value.expected, exc.value.actual) == (case.case_id, 1, 2)
