"""Persistent exception state machine (F3).

Reconciliation's honest output includes the cases it refuses to decide:
AMBIGUOUS verdicts, policy holds and escalations, quarantined source rows, and
proofs that failed independent verification. An abstention only earns its keep
if the follow-up is tracked, so each of those opens a case here.

Three properties matter, and each is enforced by the database rather than by
caller discipline:

* **Idempotent opening.** `case_id = sha256(schema | order_id | reason_code)`,
  so re-running the pipeline over the same batch reopens nothing. The
  certificate recorded is the one that first raised the case; a later
  certificate for the same problem does not fork a second case.
* **Declared edges only.** `LEGAL_TRANSITIONS` is the whole graph. A target
  that is not an out-edge of the current state raises `IllegalTransition` and
  leaves version and event count untouched -- no silent repair.
* **Optimistic concurrency.** The version read, the event insert and the state
  write happen inside one `BEGIN IMMEDIATE` transaction. Two analysts holding
  the same `expected_version` produce one winner and one `VersionConflict`;
  the loser must re-read, never overwrite.

`case_events` is append-only in the same sense as the audit log: rows are only
ever inserted. The `cases` row is a materialised projection of that history --
it can be rebuilt from the events, and is kept only so the workbench can list
by state without replaying every case.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .executor import connect

SCHEMA_VERSION = "case-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    certificate_id TEXT NOT NULL,
    state          TEXT NOT NULL,
    version        INTEGER NOT NULL,
    opened_at      TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_events (
    event_id      TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    from_state    TEXT NOT NULL,
    to_state      TEXT NOT NULL,
    actor         TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    at            TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (case_id, seq)
);
CREATE INDEX IF NOT EXISTS cases_by_order ON cases (order_id);
"""


class CaseState(str, Enum):
    OPEN = "OPEN"                              # raised by the pipeline, unowned
    ASSIGNED = "ASSIGNED"                      # a named human owns it
    INVESTIGATING = "INVESTIGATING"            # being worked
    EVIDENCE_REQUESTED = "EVIDENCE_REQUESTED"  # waiting on a customer/bank/gateway
    EVIDENCE_RECEIVED = "EVIDENCE_RECEIVED"    # the answer arrived, not yet checked
    VERIFIED = "VERIFIED"                      # evidence checked against the sources
    RESOLVED = "RESOLVED"                      # terminal: the money question is answered
    WRITTEN_OFF = "WRITTEN_OFF"                # terminal: answered by giving up on it
    ESCALATED = "ESCALATED"                    # out of the normal lane, still open


TERMINAL_STATES = frozenset({CaseState.RESOLVED, CaseState.WRITTEN_OFF})

# The whole graph. INVESTIGATING may reach VERIFIED directly: an analyst who
# already holds the evidence should not have to fake a request for it. Evidence
# may be re-requested when what arrived did not settle the question.
LEGAL_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.OPEN: frozenset({CaseState.ASSIGNED}),
    CaseState.ASSIGNED: frozenset({CaseState.INVESTIGATING}),
    CaseState.INVESTIGATING: frozenset({CaseState.EVIDENCE_REQUESTED, CaseState.VERIFIED}),
    CaseState.EVIDENCE_REQUESTED: frozenset({CaseState.EVIDENCE_RECEIVED}),
    CaseState.EVIDENCE_RECEIVED: frozenset({CaseState.VERIFIED, CaseState.EVIDENCE_REQUESTED}),
    CaseState.VERIFIED: frozenset({CaseState.RESOLVED, CaseState.WRITTEN_OFF}),
    CaseState.ESCALATED: frozenset({CaseState.INVESTIGATING, CaseState.RESOLVED,
                                    CaseState.WRITTEN_OFF}),
    CaseState.RESOLVED: frozenset(),
    CaseState.WRITTEN_OFF: frozenset(),
}

# Any nonterminal state may be escalated; escalating an escalation is a no-op
# and therefore not an edge.
for _state, _targets in list(LEGAL_TRANSITIONS.items()):
    if _state not in TERMINAL_STATES and _state is not CaseState.ESCALATED:
        LEGAL_TRANSITIONS[_state] = _targets | {CaseState.ESCALATED}


def _assert_total() -> None:
    """Fail at import if a state has no declared out-edges (see policy.py)."""
    missing = [s.value for s in CaseState if s not in LEGAL_TRANSITIONS]
    if missing:
        raise ImportError(f"LEGAL_TRANSITIONS is not total; missing: {sorted(missing)}")
    unreachable = set(CaseState) - {CaseState.OPEN} - {
        target for targets in LEGAL_TRANSITIONS.values() for target in targets}
    if unreachable:
        raise ImportError(
            f"case states no transition can reach: {sorted(s.value for s in unreachable)}")


_assert_total()


# Why a case exists. Closed set: an exception the pipeline cannot name is a bug
# in the pipeline, not a free-text field.
REASON_CODES = frozenset({
    "AMBIGUOUS_MATCH",      # recon abstained at the calibrated error rate
    "POLICY_HOLD",          # the gate held the action for a human
    "POLICY_ESCALATION",    # the gate froze it: dispute open, AFA limit
    "QUARANTINED_SOURCE",   # a source row never reached reconciliation
    "PROOF_UNVERIFIED",     # a certificate failed independent verification
})


@dataclass(frozen=True)
class ReconciliationCase:
    case_id: str
    order_id: str
    reason_code: str
    certificate_id: str
    state: CaseState
    version: int
    opened_at: str
    updated_at: str


@dataclass(frozen=True)
class CaseEvent:
    event_id: str
    case_id: str
    seq: int
    from_state: CaseState
    to_state: CaseState
    actor: str
    evidence_refs: tuple[str, ...]
    at: str
    metadata: Mapping[str, object] = MappingProxyType({})


class AuthenticatedCaseEvents(list[CaseEvent]):
    """A case-event snapshot that can be checked against its issuing store."""

    def __init__(self, events: list[CaseEvent], *, db_path: str, case_id: str,
                 certificate_id: str, content_hash: str):
        super().__init__(events)
        self.db_path = db_path
        self.case_id = case_id
        self.certificate_id = certificate_id
        self.content_hash = content_hash

    def verify(self) -> bool:
        current = CaseStore(self.db_path).events(self.case_id)
        return (
            current.content_hash == self.content_hash
            and current.certificate_id == self.certificate_id
            and list(current) == list(self)
        )


class CaseError(Exception):
    """Base class for every refusal this store makes."""


class UnknownCase(CaseError):
    def __init__(self, case_id: str):
        super().__init__(f"unknown case {case_id!r}")
        self.case_id = case_id


class IllegalTransition(CaseError):
    def __init__(self, case_id: str, current: CaseState, target: CaseState):
        super().__init__(
            f"{case_id}: {current.value} -> {target.value} is not a declared transition")
        self.case_id, self.current, self.target = case_id, current, target


class VersionConflict(CaseError):
    def __init__(self, case_id: str, expected: int, actual: int):
        super().__init__(
            f"{case_id}: expected version {expected}, store is at {actual} "
            "-- re-read and retry")
        self.case_id, self.expected, self.actual = case_id, expected, actual


def case_id_for(order_id: str, reason_code: str) -> str:
    """Stable across machines and runs: re-reconciling reopens nothing."""
    return hashlib.sha256(f"{SCHEMA_VERSION}|{order_id}|{reason_code}".encode()).hexdigest()


def _event_id(case_id: str, seq: int) -> str:
    return hashlib.sha256(f"{SCHEMA_VERSION}|{case_id}|{seq}".encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_to(start: CaseState, target: CaseState) -> tuple[CaseState, ...]:
    """The shortest declared route from `start` to `target`.

    A one-click resolution in the workbench still has to walk the graph; this
    computes the hops rather than hard-coding them, so an edge added above
    cannot leave the workbench asserting a route that no longer exists.
    Ties are broken by state name, which keeps the route deterministic.

    ESCALATED is never a waypoint. It is a shorter route to almost everything,
    but escalating is a human judgement about a case, not a step a machine may
    take on the way somewhere else; it is only ever an explicit destination.
    """
    if start is target:
        return ()
    frontier: list[tuple[CaseState, tuple[CaseState, ...]]] = [(start, ())]
    seen = {start}
    while frontier:
        state, route = frontier.pop(0)
        for nxt in sorted(LEGAL_TRANSITIONS[state], key=lambda s: s.value):
            if nxt in seen:
                continue
            if nxt is target:
                return route + (nxt,)
            if nxt is CaseState.ESCALATED:
                continue
            seen.add(nxt)
            frontier.append((nxt, route + (nxt,)))
    raise IllegalTransition("<graph>", start, target)


class CaseStore:
    """Cases and their history, in the same WAL database as the audit log."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(case_events)").fetchall()
            }
            if "metadata_json" not in columns:
                conn.execute(
                    "ALTER TABLE case_events ADD COLUMN metadata_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------ writes -------------------------------- #

    def open_case(self, order_id: str, reason_code: str,
                  certificate_id: str) -> ReconciliationCase:
        """Idempotent per (order, reason).

        `order_id` names the subject of the case: an order id, or a
        quarantine_id for a source row that never became one.
        """
        if reason_code not in REASON_CODES:
            raise ValueError(
                f"unknown case reason {reason_code!r}; declared: {sorted(REASON_CODES)}")
        if not order_id:
            raise ValueError("a case must name its subject")
        case_id = case_id_for(order_id, reason_code)
        now = _now()
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._read(conn, case_id)
            if existing is not None:
                conn.commit()
                return existing
            conn.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
                (case_id, order_id, reason_code, certificate_id,
                 CaseState.OPEN.value, 1, now, now))
            self._append_event(conn, case_id, 1, CaseState.OPEN, CaseState.OPEN,
                               "ledger-daemon", (), now)
            conn.commit()
        finally:
            conn.close()
        return ReconciliationCase(case_id, order_id, reason_code, certificate_id,
                                  CaseState.OPEN, 1, now, now)

    def transition(self, case_id: str, expected_version: int, target: CaseState,
                   actor: str, evidence_refs: tuple[str, ...] = ()) -> ReconciliationCase:
        """One declared hop. A stale `expected_version` raises VersionConflict."""
        return self.advance(case_id, expected_version, (target,), actor, evidence_refs)

    def advance(self, case_id: str, expected_version: int,
                path: tuple[CaseState, ...], actor: str,
                evidence_refs: tuple[str, ...] = (),
                event_metadata: Mapping[str, object] | None = None,
                authority_use: tuple[object, object, object, str, int] | None = None,
                ) -> ReconciliationCase:
        """Apply a whole path of declared hops, or none of it.

        The workbench resolves a held order in one click, but the case must
        still walk its declared states; doing that in one transaction keeps the
        history linear and keeps a half-walked path from surviving a crash.
        """
        if not actor:
            raise ValueError("every transition must name an actor")
        refs = tuple(evidence_refs)
        metadata = dict(event_metadata or {})
        try:
            metadata_json = json.dumps(
                metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("case event metadata must be JSON data") from exc
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            case = self._read(conn, case_id)
            if case is None:
                raise UnknownCase(case_id)
            if case.version != expected_version:
                raise VersionConflict(case_id, expected_version, case.version)
            if not path:
                conn.commit()
                return case

            if authority_use is not None:
                registry, authority, action, subject_id, subject_version = authority_use
                human = registry._verify_and_consume(
                    conn, authority, action, subject_id, subject_version)
                if human != actor:
                    raise ValueError("case actor does not match human authority")

            state, version = case.state, case.version
            now = _now()
            for index, target in enumerate(path):
                if target not in LEGAL_TRANSITIONS[state]:
                    raise IllegalTransition(case_id, state, target)
                version += 1
                self._append_event(
                    conn, case_id, version, state, target, actor, refs, now,
                    metadata_json if index == len(path) - 1 else "{}",
                )
                state = target
            conn.execute(
                "UPDATE cases SET state = ?, version = ?, updated_at = ? WHERE case_id = ?",
                (state.value, version, now, case_id))
            conn.commit()
            return ReconciliationCase(case.case_id, case.order_id, case.reason_code,
                                      case.certificate_id, state, version,
                                      case.opened_at, now)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _append_event(conn: sqlite3.Connection, case_id: str, seq: int,
                      from_state: CaseState, to_state: CaseState, actor: str,
                      evidence_refs: tuple[str, ...], at: str,
                      metadata_json: str = "{}") -> None:
        conn.execute(
            "INSERT INTO case_events "
            "(event_id,case_id,seq,from_state,to_state,actor,evidence_refs,at,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_event_id(case_id, seq), case_id, seq, from_state.value, to_state.value,
             actor, "\n".join(evidence_refs), at, metadata_json))

    # ------------------------------ reads --------------------------------- #

    @staticmethod
    def _row_to_case(row: tuple) -> ReconciliationCase:
        return ReconciliationCase(row[0], row[1], row[2], row[3],
                                  CaseState(row[4]), row[5], row[6], row[7])

    def _read(self, conn: sqlite3.Connection, case_id: str) -> ReconciliationCase | None:
        row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return None if row is None else self._row_to_case(row)

    def get(self, case_id: str) -> ReconciliationCase:
        conn = connect(self.db_path)
        try:
            case = self._read(conn, case_id)
        finally:
            conn.close()
        if case is None:
            raise UnknownCase(case_id)
        return case

    def find(self, order_id: str, reason_code: str) -> ReconciliationCase | None:
        conn = connect(self.db_path)
        try:
            return self._read(conn, case_id_for(order_id, reason_code))
        finally:
            conn.close()

    def by_order(self, order_id: str) -> list[ReconciliationCase]:
        return self._query("WHERE order_id = ? ORDER BY reason_code", (order_id,))

    def list_cases(self, open_only: bool = False) -> list[ReconciliationCase]:
        if not open_only:
            return self._query("ORDER BY opened_at, case_id", ())
        terminal = tuple(sorted(s.value for s in TERMINAL_STATES))
        placeholders = ",".join("?" * len(terminal))
        return self._query(
            f"WHERE state NOT IN ({placeholders}) ORDER BY opened_at, case_id", terminal)

    def _query(self, clause: str, params: tuple) -> list[ReconciliationCase]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(f"SELECT * FROM cases {clause}", params).fetchall()
        finally:
            conn.close()
        return [self._row_to_case(row) for row in rows]

    @staticmethod
    def _events_hash(case: ReconciliationCase, events: list[CaseEvent]) -> str:
        payload = {
            "case": {
                "case_id": case.case_id,
                "certificate_id": case.certificate_id,
                "order_id": case.order_id,
                "reason_code": case.reason_code,
                "state": case.state.value,
                "version": case.version,
            },
            "events": [{
                "actor": event.actor,
                "case_id": event.case_id,
                "event_id": event.event_id,
                "evidence_refs": list(event.evidence_refs),
                "from_state": event.from_state.value,
                "metadata": dict(event.metadata),
                "seq": event.seq,
                "to_state": event.to_state.value,
            } for event in events],
            "schema_version": "authenticated-case-events-v1",
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def events(self, case_id: str) -> AuthenticatedCaseEvents:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM case_events WHERE case_id = ? ORDER BY seq", (case_id,)
            ).fetchall()
            case = self._read(conn, case_id)
        finally:
            conn.close()
        if case is None:
            raise UnknownCase(case_id)
        events = [
            CaseEvent(row[0], row[1], row[2], CaseState(row[3]), CaseState(row[4]),
                      row[5], tuple(row[6].split("\n")) if row[6] else (), row[7],
                      MappingProxyType(json.loads(row[8]) if len(row) > 8 else {}))
            for row in rows
        ]
        return AuthenticatedCaseEvents(
            events, db_path=self.db_path, case_id=case_id,
            certificate_id=case.certificate_id,
            content_hash=self._events_hash(case, events),
        )


# --------------------------- pipeline integration --------------------------- #

# One case per subject, not one per complaint: an AMBIGUOUS verdict also draws
# an R1 hold, and opening two cases for it would double the workbench without
# adding a fact. Precedence runs from the most structural failure downwards.
_REASON_PRECEDENCE = (
    "PROOF_UNVERIFIED", "POLICY_ESCALATION", "AMBIGUOUS_MATCH", "POLICY_HOLD",
)


def open_exception_cases(store: "CaseStore", verdicts: dict, decisions: dict, *,
                         certificate_ids: dict[str, str] | None = None,
                         quarantine_ids: tuple[str, ...] = (),
                         unverified_order_ids: tuple[str, ...] = (),
                         ) -> dict[str, ReconciliationCase]:
    """Open (idempotently) one case for every exception this run produced.

    Returns subject id -> case. Re-running the same batch returns the same
    cases at whatever state their humans have since moved them to.
    """
    certificates = certificate_ids or {}
    reasons: dict[str, str] = {}

    for order_id in unverified_order_ids:
        reasons[order_id] = "PROOF_UNVERIFIED"
    for order_id, verdict in verdicts.items():
        if verdict.verdict.value == "ambiguous":
            reasons.setdefault(order_id, "AMBIGUOUS_MATCH")
    for order_id, decision in decisions.items():
        if decision.outcome == "ESCALATE":
            reasons.setdefault(order_id, "POLICY_ESCALATION")
        elif decision.outcome == "HOLD":
            reasons.setdefault(order_id, "POLICY_HOLD")

    ranked = sorted(reasons.items(), key=lambda kv: (_REASON_PRECEDENCE.index(kv[1]), kv[0]))
    opened = {
        subject: store.open_case(subject, reason, certificates.get(subject, ""))
        for subject, reason in ranked
    }
    for quarantine_id in sorted(quarantine_ids):
        opened[quarantine_id] = store.open_case(
            quarantine_id, "QUARANTINED_SOURCE", "")
    return opened
