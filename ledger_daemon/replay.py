"""Zero-regression replay gate for proposed deterministic rules."""

from __future__ import annotations

import json
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

from .executor import connect
from .rules import AnalystRegistry, HumanAction, HumanAuthority, RuleProposal, RuleStatus

REPLAY_SCHEMA_VERSION = "rule-replay-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ReplayCaseOutcome:
    case_id: str
    before_correct: bool
    after_correct: bool
    before_wrong_paise: int
    after_wrong_paise: int
    before_proof_valid: bool
    after_proof_valid: bool
    before_safe_coverage: int
    after_safe_coverage: int

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id:
            raise ValueError("replay case id is required")
        for name in ("before_correct", "after_correct", "before_proof_valid", "after_proof_valid"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        for name in ("before_wrong_paise", "after_wrong_paise",
                     "before_safe_coverage", "after_safe_coverage"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "after_correct": self.after_correct,
            "after_proof_valid": self.after_proof_valid,
            "after_safe_coverage": self.after_safe_coverage,
            "after_wrong_paise": self.after_wrong_paise,
            "before_correct": self.before_correct,
            "before_proof_valid": self.before_proof_valid,
            "before_safe_coverage": self.before_safe_coverage,
            "before_wrong_paise": self.before_wrong_paise,
            "case_id": self.case_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReplayCaseOutcome":
        expected = {
            "after_correct", "after_proof_valid", "after_safe_coverage",
            "after_wrong_paise", "before_correct", "before_proof_valid",
            "before_safe_coverage", "before_wrong_paise", "case_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid replay case outcome schema")
        return cls(**value)


@dataclass(frozen=True)
class ReplayReport:
    rule_id: str
    rule_version: int
    status: RuleStatus
    confirmed_cases: tuple[ReplayCaseOutcome, ...]
    attack_cases: tuple[ReplayCaseOutcome, ...]
    new_wrong_verdicts: int
    new_wrong_paise: int
    proofs_valid: bool
    confirmed_safe_coverage_change: int
    attack_safe_coverage_change: int
    safe_coverage_change: int
    promotable: bool

    @property
    def confirmed_case_ids(self) -> tuple[str, ...]:
        return tuple(outcome.case_id for outcome in self.confirmed_cases)

    @property
    def attack_case_ids(self) -> tuple[str, ...]:
        return tuple(outcome.case_id for outcome in self.attack_cases)

    def to_json(self) -> str:
        return json.dumps({
            "attack_cases": [outcome.to_dict() for outcome in self.attack_cases],
            "confirmed_cases": [outcome.to_dict() for outcome in self.confirmed_cases],
            "confirmed_safe_coverage_change": self.confirmed_safe_coverage_change,
            "new_wrong_paise": self.new_wrong_paise,
            "new_wrong_verdicts": self.new_wrong_verdicts,
            "promotable": self.promotable,
            "proofs_valid": self.proofs_valid,
            "attack_safe_coverage_change": self.attack_safe_coverage_change,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "safe_coverage_change": self.safe_coverage_change,
            "schema_version": REPLAY_SCHEMA_VERSION,
            "status": self.status.value,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


    @classmethod
    def from_json(cls, encoded: str) -> "ReplayReport":
        try:
            value = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid replay report JSON") from exc
        expected = {
            "attack_cases", "attack_safe_coverage_change", "confirmed_cases",
            "confirmed_safe_coverage_change", "new_wrong_paise",
            "new_wrong_verdicts", "promotable", "proofs_valid", "rule_id",
            "rule_version", "safe_coverage_change", "schema_version", "status",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid replay report schema")
        if value["schema_version"] != REPLAY_SCHEMA_VERSION:
            raise ValueError("unsupported replay report schema version")
        if (not isinstance(value["confirmed_cases"], list)
                or not isinstance(value["attack_cases"], list)):
            raise ValueError("invalid replay report cases")
        confirmed = tuple(ReplayCaseOutcome.from_dict(item) for item in value["confirmed_cases"])
        attacks = tuple(ReplayCaseOutcome.from_dict(item) for item in value["attack_cases"])
        if not confirmed or not attacks:
            raise ValueError("replay report requires confirmed and attack corpora")
        for name in ("new_wrong_paise", "new_wrong_verdicts", "rule_version",
                     "confirmed_safe_coverage_change", "attack_safe_coverage_change",
                     "safe_coverage_change"):
            if type(value[name]) is not int:
                raise TypeError(f"{name} must be an integer")
        if value["new_wrong_paise"] < 0 or value["new_wrong_verdicts"] < 0:
            raise ValueError("replay regressions cannot be negative")
        if type(value["promotable"]) is not bool or type(value["proofs_valid"]) is not bool:
            raise TypeError("replay booleans must be bool")
        try:
            status = RuleStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid replay status") from exc
        outcomes = confirmed + attacks
        computed_wrong_verdicts = sum(
            outcome.before_correct and not outcome.after_correct for outcome in outcomes)
        computed_wrong_paise = sum(
            max(0, outcome.after_wrong_paise - outcome.before_wrong_paise)
            for outcome in outcomes)
        computed_proofs_valid = all(outcome.after_proof_valid for outcome in outcomes)
        computed_confirmed_coverage = sum(
            outcome.after_safe_coverage - outcome.before_safe_coverage
            for outcome in confirmed)
        computed_attack_coverage = sum(
            outcome.after_safe_coverage - outcome.before_safe_coverage
            for outcome in attacks)
        computed_coverage_change = sum(
            outcome.after_safe_coverage - outcome.before_safe_coverage
            for outcome in outcomes)
        if (
            value["new_wrong_verdicts"] != computed_wrong_verdicts
            or value["new_wrong_paise"] != computed_wrong_paise
            or value["proofs_valid"] is not computed_proofs_valid
            or value["confirmed_safe_coverage_change"] != computed_confirmed_coverage
            or value["attack_safe_coverage_change"] != computed_attack_coverage
            or value["safe_coverage_change"] != computed_coverage_change
        ):
            raise ValueError("inconsistent replay report aggregates")
        recomputed = (
            computed_wrong_verdicts == 0
            and computed_wrong_paise == 0
            and computed_proofs_valid
            and computed_confirmed_coverage >= 0
            and computed_attack_coverage >= 0
        )
        expected_status = RuleStatus.REPLAY_PASSED if recomputed else RuleStatus.REJECTED
        if value["promotable"] is not recomputed or status is not expected_status:
            raise ValueError("inconsistent replay report gate result")
        all_ids = [outcome.case_id for outcome in confirmed + attacks]
        if (any(not isinstance(case_id, str) or not case_id for case_id in all_ids)
                or len(all_ids) != len(set(all_ids))):
            raise ValueError("invalid or duplicate replay case id")
        if (not isinstance(value["rule_id"], str) or not value["rule_id"]
                or value["rule_version"] < 1):
            raise ValueError("invalid replay rule identity")
        return cls(
            rule_id=value["rule_id"], rule_version=value["rule_version"], status=status,
            confirmed_cases=confirmed, attack_cases=attacks,
            new_wrong_verdicts=value["new_wrong_verdicts"],
            new_wrong_paise=value["new_wrong_paise"], proofs_valid=value["proofs_valid"],
            confirmed_safe_coverage_change=value["confirmed_safe_coverage_change"],
            attack_safe_coverage_change=value["attack_safe_coverage_change"],
            safe_coverage_change=value["safe_coverage_change"],
            promotable=value["promotable"],
        )


@dataclass(frozen=True)
class ReplayReceipt:
    """Runner-signed binding of a rule version to two persisted corpora."""

    rule_id: str
    rule_version: int
    runner_id: str
    confirmed_corpus_id: str
    confirmed_corpus_hash: str
    attack_corpus_id: str
    attack_corpus_hash: str
    outcome_hashes: tuple[str, ...]
    report_json: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("rule_id", "confirmed_corpus_id", "attack_corpus_id"):
            if not isinstance(getattr(self, name), str) or _ID_RE.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"invalid replay receipt {name}")
        for name in ("runner_id", "confirmed_corpus_hash", "attack_corpus_hash", "signature"):
            if not isinstance(getattr(self, name), str) or _HASH_RE.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"invalid replay receipt {name}")
        if type(self.rule_version) is not int or self.rule_version < 1:
            raise ValueError("invalid replay receipt rule version")
        if (not isinstance(self.outcome_hashes, tuple) or not self.outcome_hashes
                or any(_HASH_RE.fullmatch(item) is None for item in self.outcome_hashes)):
            raise ValueError("invalid replay receipt outcome hashes")
        ReplayReport.from_json(self.report_json)

    def payload(self) -> dict[str, object]:
        return {
            "attack_corpus_hash": self.attack_corpus_hash,
            "attack_corpus_id": self.attack_corpus_id,
            "confirmed_corpus_hash": self.confirmed_corpus_hash,
            "confirmed_corpus_id": self.confirmed_corpus_id,
            "outcome_hashes": list(self.outcome_hashes),
            "report_json": self.report_json,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "runner_id": self.runner_id,
            "schema_version": "replay-receipt-v1",
        }

    def to_json(self) -> str:
        return _canonical({**self.payload(), "signature": self.signature})


_CORPUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_runner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    runner_id TEXT NOT NULL,
    secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_corpora (
    corpus_id TEXT PRIMARY KEY,
    corpus_class TEXT NOT NULL CHECK (corpus_class IN ('CONFIRMED','ATTACK')),
    corpus_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_records (
    corpus_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    outcome_json TEXT NOT NULL,
    outcome_hash TEXT NOT NULL,
    PRIMARY KEY (corpus_id, seq)
);
"""


class ReplayCorpusStore:
    """Immutable corpora imported through an external one-time authority.

    The analyst registry is provisioned independently. This store creates its
    local runner signing key only after consuming an exact corpus-commitment
    capability; raw outcomes alone never establish replay trust.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(_CORPUS_SCHEMA)
            row = conn.execute(
                "SELECT runner_id FROM replay_runner WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise ValueError("replay runner has not been explicitly bootstrapped")
        self.runner_id = row[0]

    @staticmethod
    def _outcome_json(outcome: ReplayCaseOutcome) -> str:
        return _canonical(outcome.to_dict())

    @classmethod
    def _normalize(cls, confirmed: Mapping[str, tuple[ReplayCaseOutcome, ...]],
                   attacks: Mapping[str, tuple[ReplayCaseOutcome, ...]],
                   ) -> list[tuple[str, str, str, tuple[tuple[str, str], ...]]]:
        if not isinstance(confirmed, Mapping) or not confirmed:
            raise ValueError("a non-empty confirmed corpus registry is required")
        if not isinstance(attacks, Mapping) or not attacks:
            raise ValueError("a non-empty attack corpus registry is required")
        normalized: list[tuple[str, str, str, tuple[tuple[str, str], ...]]] = []
        all_case_ids: set[str] = set()
        for corpus_class, corpora in (("CONFIRMED", confirmed), ("ATTACK", attacks)):
            for corpus_id, outcomes in corpora.items():
                if not isinstance(corpus_id, str) or _ID_RE.fullmatch(corpus_id) is None:
                    raise ValueError("invalid replay corpus id")
                if not isinstance(outcomes, tuple) or not outcomes:
                    raise ValueError(f"{corpus_class.lower()} corpus must be non-empty")
                records: list[tuple[str, str]] = []
                for outcome in outcomes:
                    if not isinstance(outcome, ReplayCaseOutcome):
                        raise TypeError("replay corpus records must be ReplayCaseOutcome")
                    if outcome.case_id in all_case_ids:
                        raise ValueError("duplicate replay case id")
                    all_case_ids.add(outcome.case_id)
                    encoded = cls._outcome_json(outcome)
                    records.append((encoded, hashlib.sha256(encoded.encode()).hexdigest()))
                corpus_hash = hashlib.sha256(_canonical({
                    "corpus_class": corpus_class, "corpus_id": corpus_id,
                    "outcome_hashes": [item[1] for item in records],
                    "schema_version": "replay-corpus-v1",
                }).encode()).hexdigest()
                normalized.append((corpus_id, corpus_class, corpus_hash, tuple(records)))
        return normalized

    @classmethod
    def import_subject(cls, confirmed: Mapping[str, tuple[ReplayCaseOutcome, ...]],
                       attacks: Mapping[str, tuple[ReplayCaseOutcome, ...]]) -> str:
        normalized = cls._normalize(confirmed, attacks)
        commitment = hashlib.sha256(_canonical({
            "corpora": [{
                "corpus_class": corpus_class,
                "corpus_hash": corpus_hash,
                "corpus_id": corpus_id,
            } for corpus_id, corpus_class, corpus_hash, _records in sorted(normalized)],
            "required_roles": ["ATTACK", "CONFIRMED"],
            "schema_version": "replay-corpus-import-v1",
        }).encode()).hexdigest()
        return f"replay-import:{commitment}"

    @classmethod
    def bootstrap(cls, db_path: str,
                  confirmed: Mapping[str, tuple[ReplayCaseOutcome, ...]],
                  attacks: Mapping[str, tuple[ReplayCaseOutcome, ...]], *,
                  authority: HumanAuthority | None,
                  registry: AnalystRegistry) -> "ReplayCorpusStore":
        normalized = cls._normalize(confirmed, attacks)
        subject = cls.import_subject(confirmed, attacks)
        if not isinstance(registry, AnalystRegistry) or registry.db_path != db_path:
            raise ValueError("external corpus authority registry is required")
        registry.verify(
            authority, HumanAction.IMPORT_REPLAY,
            subject_id=subject, subject_version=1,
        )
        secret = secrets.token_hex(32)
        runner_id = hashlib.sha256(f"replay-runner-v1|{secret}".encode()).hexdigest()
        conn = connect(db_path)
        try:
            conn.executescript(_CORPUS_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            registry._verify_and_consume(
                conn, authority, HumanAction.IMPORT_REPLAY, subject, 1)
            if conn.execute("SELECT 1 FROM replay_runner WHERE singleton=1").fetchone():
                raise ValueError("replay runner is already bootstrapped")
            conn.execute("INSERT INTO replay_runner VALUES (1,?,?)", (runner_id, secret))
            for corpus_id, corpus_class, corpus_hash, records in sorted(normalized):
                conn.execute(
                    "INSERT INTO replay_corpora VALUES (?,?,?)",
                    (corpus_id, corpus_class, corpus_hash),
                )
                for seq, (encoded, outcome_hash) in enumerate(records, 1):
                    conn.execute(
                        "INSERT INTO replay_records VALUES (?,?,?,?)",
                        (corpus_id, seq, encoded, outcome_hash),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return cls(db_path)

    def _load(self, corpus_id: str, expected_class: str,
              conn: sqlite3.Connection | None = None,
              ) -> tuple[str, tuple[ReplayCaseOutcome, ...], tuple[str, ...]]:
        own = conn is None
        conn = connect(self.db_path) if conn is None else conn
        try:
            corpus = conn.execute(
                "SELECT corpus_class,corpus_hash FROM replay_corpora WHERE corpus_id=?",
                (corpus_id,),
            ).fetchone()
            rows = conn.execute(
                "SELECT outcome_json,outcome_hash FROM replay_records "
                "WHERE corpus_id=? ORDER BY seq", (corpus_id,),
            ).fetchall()
        finally:
            if own:
                conn.close()
        if corpus is None or corpus[0] != expected_class or not rows:
            raise ValueError(f"replay receipt names no {expected_class.lower()} corpus")
        outcomes, hashes = [], []
        for encoded, stored_hash in rows:
            actual_hash = hashlib.sha256(encoded.encode()).hexdigest()
            if actual_hash != stored_hash:
                raise ValueError("persisted replay outcome hash mismatch")
            outcomes.append(ReplayCaseOutcome.from_dict(json.loads(encoded)))
            hashes.append(actual_hash)
        actual_corpus_hash = hashlib.sha256(_canonical({
            "corpus_class": expected_class, "corpus_id": corpus_id,
            "outcome_hashes": hashes, "schema_version": "replay-corpus-v1",
        }).encode()).hexdigest()
        if actual_corpus_hash != corpus[1]:
            raise ValueError("persisted replay corpus hash mismatch")
        return actual_corpus_hash, tuple(outcomes), tuple(hashes)

    def run(self, rule: RuleProposal, confirmed_corpus_id: str,
            attack_corpus_id: str) -> ReplayReceipt:
        confirmed_hash, confirmed, confirmed_outcome_hashes = self._load(
            confirmed_corpus_id, "CONFIRMED")
        attack_hash, attacks, attack_outcome_hashes = self._load(
            attack_corpus_id, "ATTACK")
        report = replay_rule(rule, confirmed, attacks)
        with connect(self.db_path) as conn:
            secret = conn.execute(
                "SELECT secret FROM replay_runner WHERE singleton=1"
            ).fetchone()[0]
        unsigned = ReplayReceipt(
            rule_id=rule.rule_id, rule_version=rule.version, runner_id=self.runner_id,
            confirmed_corpus_id=confirmed_corpus_id,
            confirmed_corpus_hash=confirmed_hash,
            attack_corpus_id=attack_corpus_id, attack_corpus_hash=attack_hash,
            outcome_hashes=confirmed_outcome_hashes + attack_outcome_hashes,
            report_json=report.to_json(), signature="0" * 64,
        )
        signature = hmac.new(
            secret.encode(), _canonical(unsigned.payload()).encode(), hashlib.sha256
        ).hexdigest()
        return ReplayReceipt(**{**unsigned.__dict__, "signature": signature})

    def verify(self, rule: RuleProposal, receipt: object) -> ReplayReport:
        if not isinstance(receipt, ReplayReceipt):
            raise TypeError("runner-authenticated replay receipt is required")
        if receipt.rule_id != rule.rule_id or receipt.rule_version != rule.version:
            raise ValueError("replay receipt does not bind this rule version")
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT runner_id,secret FROM replay_runner WHERE singleton=1"
            ).fetchone()
            if row is None or receipt.runner_id != row[0]:
                raise ValueError("invalid replay receipt runner")
            expected = hmac.new(
                row[1].encode(), _canonical(receipt.payload()).encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(receipt.signature, expected):
                raise ValueError("invalid replay receipt signature")
            confirmed_hash, confirmed, confirmed_hashes = self._load(
                receipt.confirmed_corpus_id, "CONFIRMED", conn)
            attack_hash, attacks, attack_hashes = self._load(
                receipt.attack_corpus_id, "ATTACK", conn)
        if (receipt.confirmed_corpus_hash != confirmed_hash
                or receipt.attack_corpus_hash != attack_hash
                or receipt.outcome_hashes != confirmed_hashes + attack_hashes):
            raise ValueError("replay receipt corpus binding is invalid")
        report = replay_rule(rule, confirmed, attacks)
        if receipt.report_json != report.to_json():
            raise ValueError("replay receipt report does not match persisted outcomes")
        return report


def replay_rule(rule: RuleProposal,
                confirmed_cases: Iterable[ReplayCaseOutcome],
                attack_cases: Iterable[ReplayCaseOutcome]) -> ReplayReport:
    """Recompute the complete replay gate; no caller-supplied verdict is trusted."""
    if not isinstance(rule, RuleProposal) or rule.status is not RuleStatus.PROPOSED:
        raise ValueError("only a PROPOSED rule can enter replay")
    confirmed = tuple(confirmed_cases)
    attacks = tuple(attack_cases)
    if not confirmed:
        raise ValueError("replay requires a non-empty confirmed corpus")
    if not attacks:
        raise ValueError("replay requires a non-empty attack corpus")
    outcomes = confirmed + attacks
    if any(not isinstance(outcome, ReplayCaseOutcome) for outcome in outcomes):
        raise TypeError("replay inputs must be immutable ReplayCaseOutcome records")
    case_ids = [outcome.case_id for outcome in outcomes]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate replay case id")

    new_wrong_verdicts = sum(
        outcome.before_correct and not outcome.after_correct for outcome in outcomes)
    new_wrong_paise = sum(
        max(0, outcome.after_wrong_paise - outcome.before_wrong_paise)
        for outcome in outcomes)
    proofs_valid = all(outcome.after_proof_valid for outcome in outcomes)
    confirmed_coverage_change = sum(
        outcome.after_safe_coverage - outcome.before_safe_coverage
        for outcome in confirmed)
    attack_coverage_change = sum(
        outcome.after_safe_coverage - outcome.before_safe_coverage
        for outcome in attacks)
    safe_coverage_change = sum(
        outcome.after_safe_coverage - outcome.before_safe_coverage
        for outcome in outcomes)
    promotable = (
        new_wrong_verdicts == 0
        and new_wrong_paise == 0
        and proofs_valid
        and confirmed_coverage_change >= 0
        and attack_coverage_change >= 0
    )
    status = RuleStatus.REPLAY_PASSED if promotable else RuleStatus.REJECTED
    return ReplayReport(
        rule_id=rule.rule_id,
        rule_version=rule.version,
        status=status,
        confirmed_cases=confirmed,
        attack_cases=attacks,
        new_wrong_verdicts=new_wrong_verdicts,
        new_wrong_paise=new_wrong_paise,
        proofs_valid=proofs_valid,
        confirmed_safe_coverage_change=confirmed_coverage_change,
        attack_safe_coverage_change=attack_coverage_change,
        safe_coverage_change=safe_coverage_change,
        promotable=promotable,
    )
