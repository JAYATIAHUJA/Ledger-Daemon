"""Replay-gated deterministic rules learned from verified human case outcomes.

Rules in this module are bounded JSON data.  They are never Python source and
never carry an executable callback.  Every lifecycle change creates a new,
immutable proposal version in the local audit database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping

from .executor import connect

RULE_SCHEMA_VERSION = "rule-proposal-v1"
RULE_STORE_SCHEMA = "rule-store-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SAFE_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _./:-]*")
_MACHINE_ACTORS = frozenset({
    "agent", "chatgpt", "gpt", "ledger-daemon", "llm", "model", "pipeline",
    "service", "system",
})


class HumanAction(str, Enum):
    RESOLVE = "RESOLVE"
    APPROVE = "APPROVE"
    ACTIVATE = "ACTIVATE"
    IMPORT_REPLAY = "IMPORT_REPLAY"


@dataclass(frozen=True)
class HumanAuthority:
    """A scoped capability issued by a persisted trusted-analyst registry."""

    actor_id: str
    action: HumanAction
    registry_id: str
    subject_id: str
    subject_version: int
    nonce: str
    expires_at: str
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, HumanAction):
            raise ValueError("invalid human authority action")
        for name in ("actor_id", "registry_id", "subject_id", "nonce", "expires_at", "signature"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError("invalid human authority")
        if type(self.subject_version) is not int or self.subject_version < 1:
            raise ValueError("invalid human authority subject version")

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action.value,
            "actor_id": self.actor_id,
            "registry_id": self.registry_id,
            "subject_id": self.subject_id,
            "subject_version": self.subject_version,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: object) -> "HumanAuthority":
        expected = {
            "action", "actor_id", "registry_id", "subject_id", "subject_version",
            "nonce", "expires_at", "signature",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid human authority schema")
        try:
            return cls(
                actor_id=value["actor_id"], action=HumanAction(value["action"]),
                registry_id=value["registry_id"], subject_id=value["subject_id"],
                subject_version=value["subject_version"], nonce=value["nonce"],
                expires_at=value["expires_at"], signature=value["signature"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid human authority") from exc


_ANALYST_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyst_registry (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    registry_id TEXT NOT NULL,
    secret      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trusted_analysts (
    actor_id     TEXT PRIMARY KEY,
    actions_json TEXT NOT NULL,
    credential_salt TEXT NOT NULL,
    credential_hash TEXT NOT NULL,
    active       INTEGER NOT NULL CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS used_human_capabilities (
    nonce TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_version INTEGER NOT NULL
);
"""


class AnalystRegistry:
    """Store-backed allowlist that issues action-scoped human capabilities."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(_ANALYST_SCHEMA)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(trusted_analysts)")
            }
            if "credential_salt" not in columns:
                conn.execute(
                    "ALTER TABLE trusted_analysts ADD COLUMN credential_salt "
                    "TEXT NOT NULL DEFAULT ''")
            if "credential_hash" not in columns:
                conn.execute(
                    "ALTER TABLE trusted_analysts ADD COLUMN credential_hash "
                    "TEXT NOT NULL DEFAULT ''")
            row = conn.execute(
                "SELECT registry_id FROM analyst_registry WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise ValueError("analyst registry has not been explicitly bootstrapped")
        self.registry_id = row[0]

    @classmethod
    def bootstrap(cls, db_path: str,
                  analysts: Mapping[str, Mapping[str, object]]) -> "AnalystRegistry":
        if not isinstance(analysts, Mapping) or not analysts:
            raise ValueError("trusted human analysts are required")
        normalized: dict[str, tuple[tuple[str, ...], str, str]] = {}
        for actor_id, definition in analysts.items():
            actor = _human_actor(actor_id)
            if not isinstance(definition, Mapping) or set(definition) != {"credential", "actions"}:
                raise ValueError("analyst bootstrap requires credential and actions")
            credential = definition["credential"]
            actions = definition["actions"]
            if not isinstance(credential, str) or len(credential) < 12:
                raise ValueError("analyst credential must be a secret of at least 12 characters")
            if not isinstance(actions, tuple) or not actions:
                raise ValueError("analyst actions must be a non-empty immutable tuple")
            try:
                action_values = tuple(sorted({HumanAction(action).value for action in actions}))
            except (TypeError, ValueError) as exc:
                raise ValueError("unknown analyst action") from exc
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac(
                "sha256", credential.encode("utf-8"), bytes.fromhex(salt), 120_000
            ).hex()
            normalized[actor] = (action_values, salt, digest)

        secret = secrets.token_hex(32)
        registry_id = hashlib.sha256(f"analyst-registry-v1|{secret}".encode()).hexdigest()
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(_ANALYST_SCHEMA)
            if conn.execute("SELECT 1 FROM analyst_registry WHERE singleton=1").fetchone():
                raise ValueError("analyst registry is already bootstrapped")
            conn.execute(
                "INSERT INTO analyst_registry VALUES (1,?,?)", (registry_id, secret))
            for actor, (action_values, salt, digest) in sorted(normalized.items()):
                conn.execute(
                    "INSERT INTO trusted_analysts VALUES (?,?,?,?,1)",
                    (actor, _canonical(list(action_values)), salt, digest),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return cls(db_path)

    @staticmethod
    def _payload(registry_id: str, actor_id: str, action: HumanAction, *,
                 subject_id: str, subject_version: int, nonce: str,
                 expires_at: str) -> bytes:
        return _canonical({
            "action": action.value,
            "actor_id": actor_id,
            "registry_id": registry_id,
            "subject_id": subject_id,
            "subject_version": subject_version,
            "nonce": nonce,
            "expires_at": expires_at,
            "schema_version": "human-authority-v2",
        }).encode("utf-8")

    def issue(self, actor_id: str, credential: str, action: HumanAction, *,
              subject_id: str, subject_version: int,
              expires_at: str) -> HumanAuthority:
        try:
            scoped_action = HumanAction(action)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown authority action") from exc
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT actions_json, credential_salt, credential_hash, active "
                "FROM trusted_analysts WHERE actor_id=?",
                (actor_id,),
            ).fetchone()
            secret_row = conn.execute(
                "SELECT secret FROM analyst_registry WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise ValueError("unknown human identity")
        if not isinstance(credential, str):
            raise ValueError("human credential is required")
        supplied = hashlib.pbkdf2_hmac(
            "sha256", credential.encode("utf-8"), bytes.fromhex(row[1]), 120_000
        ).hex()
        if not hmac.compare_digest(supplied, row[2]):
            raise ValueError("invalid human credential")
        if row[3] != 1 or scoped_action.value not in json.loads(row[0]):
            raise ValueError("human identity is not authorized for this action")
        if not isinstance(subject_id, str) or not subject_id or not _ID_RE.fullmatch(subject_id):
            raise ValueError("authority subject is required")
        if type(subject_version) is not int or subject_version < 1:
            raise ValueError("authority subject version must be positive")
        expiry = _parse_utc(expires_at, "authority expiry")
        if expiry <= datetime.now(timezone.utc):
            raise ValueError("human authority has expired")
        nonce = secrets.token_hex(32)
        signature = hmac.new(
            secret_row[0].encode(),
            self._payload(
                self.registry_id, actor_id, scoped_action, subject_id=subject_id,
                subject_version=subject_version, nonce=nonce, expires_at=expires_at,
            ),
            hashlib.sha256,
        ).hexdigest()
        return HumanAuthority(
            actor_id, scoped_action, self.registry_id, subject_id, subject_version,
            nonce, expires_at, signature,
        )

    def verify(self, authority: object, action: HumanAction, *,
               subject_id: str, subject_version: int) -> str:
        with connect(self.db_path) as conn:
            return self._verify(conn, authority, action, subject_id, subject_version)

    def _verify(self, conn: sqlite3.Connection, authority: object, action: HumanAction,
                subject_id: str, subject_version: int) -> str:
        if not isinstance(authority, HumanAuthority):
            raise ValueError("store-issued human authority is required")
        try:
            required = HumanAction(action)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown authority action") from exc
        if authority.action is not required or authority.registry_id != self.registry_id:
            raise ValueError("human authority has the wrong scope or registry")
        if authority.subject_id != subject_id or authority.subject_version != subject_version:
            raise ValueError("human authority has the wrong subject or version")
        if _parse_utc(authority.expires_at, "authority expiry") <= datetime.now(timezone.utc):
            raise ValueError("human authority has expired")
        row = conn.execute(
            "SELECT actions_json, active FROM trusted_analysts WHERE actor_id=?",
            (authority.actor_id,),
        ).fetchone()
        secret = conn.execute(
            "SELECT secret FROM analyst_registry WHERE singleton=1"
        ).fetchone()[0]
        expected = hmac.new(
            secret.encode(),
            self._payload(
                self.registry_id, authority.actor_id, required,
                subject_id=authority.subject_id,
                subject_version=authority.subject_version,
                nonce=authority.nonce, expires_at=authority.expires_at,
            ),
            hashlib.sha256,
        ).hexdigest()
        if (row is None or row[1] != 1 or required.value not in json.loads(row[0])
                or not hmac.compare_digest(authority.signature, expected)):
            raise ValueError("invalid or revoked human authority")
        return authority.actor_id

    def _verify_and_consume(self, conn: sqlite3.Connection, authority: object,
                            action: HumanAction, subject_id: str,
                            subject_version: int) -> str:
        actor = self._verify(conn, authority, action, subject_id, subject_version)
        try:
            conn.execute(
                "INSERT INTO used_human_capabilities VALUES (?,?,?,?,?)",
                (authority.nonce, actor, HumanAction(action).value,
                 subject_id, subject_version),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("human authority capability has already been used") from exc
        return actor

    def verify_consumed(self, authority: object, action: HumanAction, *,
                        subject_id: str, subject_version: int) -> str:
        with connect(self.db_path) as conn:
            actor = self._verify(conn, authority, action, subject_id, subject_version)
            row = conn.execute(
                "SELECT actor_id,action,subject_id,subject_version "
                "FROM used_human_capabilities WHERE nonce=?", (authority.nonce,),
            ).fetchone()
        expected = (actor, HumanAction(action).value, subject_id, subject_version)
        if row != expected:
            raise ValueError("human authority capability was not transactionally consumed")
        return actor


class RuleFamily(str, Enum):
    EXACT_REFERENCE = "EXACT_REFERENCE"
    NARRATION_PATTERN = "NARRATION_PATTERN"
    DATE_WINDOW = "DATE_WINDOW"
    FEE_SCHEDULE = "FEE_SCHEDULE"
    SOURCE_ALIAS = "SOURCE_ALIAS"


class RuleStatus(str, Enum):
    PROPOSED = "PROPOSED"
    REPLAY_PASSED = "REPLAY_PASSED"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ActivationReceipt:
    rule_id: str
    rule_version: int
    history_hash: str
    registry_id: str
    activated_by: str
    activation_time: str


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be UTC to whole-second precision")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc


def _human_actor(actor: object) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a human actor is required")
    normalized = actor.strip().lower()
    machine_prefixes = tuple(f"{name}-" for name in _MACHINE_ACTORS)
    if normalized in _MACHINE_ACTORS or normalized.startswith(machine_prefixes):
        raise ValueError("rule authority must be a named human")
    return actor.strip()


def _safe_text(value: object, name: str, *, maximum: int = 128) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or _SAFE_TEXT_RE.fullmatch(value) is None):
        raise ValueError(f"{name} must be bounded literal text")
    return value


def _validate_parameters(family: RuleFamily, raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise TypeError("rule parameters must be a JSON object")
    parameters = dict(raw)

    if family is RuleFamily.EXACT_REFERENCE:
        if set(parameters) == {"field", "value"}:
            if parameters["field"] not in {"utr", "settlement_id", "payment_id", "order_id"}:
                raise ValueError("EXACT_REFERENCE field is not allowlisted")
            _safe_text(parameters["value"], "reference value")
        elif set(parameters) == {"source_field", "target_field"}:
            allowed = {"utr", "settlement_id", "payment_id", "order_id"}
            if parameters["source_field"] not in allowed or parameters["target_field"] not in allowed:
                raise ValueError("EXACT_REFERENCE fields are not allowlisted")
        else:
            raise ValueError("invalid EXACT_REFERENCE parameter schema")
    elif family is RuleFamily.NARRATION_PATTERN:
        if set(parameters) != {"pattern"}:
            raise ValueError("invalid NARRATION_PATTERN parameter schema")
        pattern = _safe_text(parameters["pattern"], "narration pattern", maximum=64)
        if any(character in pattern for character in "*+?{}[]()|\\^$"):
            raise ValueError("narration patterns must be literal, not regular expressions")
    elif family is RuleFamily.DATE_WINDOW:
        if set(parameters) != {"days"}:
            raise ValueError("invalid DATE_WINDOW parameter schema")
        if type(parameters["days"]) is not int or not 0 <= parameters["days"] <= 7:
            raise ValueError("DATE_WINDOW days must be an integer from 0 through 7")
    elif family is RuleFamily.FEE_SCHEDULE:
        if not set(parameters) in ({"fee_basis_points"},
                                   {"fee_basis_points", "tax_basis_points"}):
            raise ValueError("invalid FEE_SCHEDULE parameter schema")
        for name, value in parameters.items():
            if type(value) is not int or not 0 <= value <= 5000:
                raise ValueError(f"{name} must be bounded integer basis points")
    elif family is RuleFamily.SOURCE_ALIAS:
        if set(parameters) != {"source", "alias", "canonical"}:
            raise ValueError("invalid SOURCE_ALIAS parameter schema")
        if parameters["source"] not in {"bank", "gateway", "merchant"}:
            raise ValueError("SOURCE_ALIAS source is not allowlisted")
        _safe_text(parameters["alias"], "source alias", maximum=64)
        _safe_text(parameters["canonical"], "canonical source", maximum=64)
    else:  # Enum makes this unreachable, but the closed-world refusal is deliberate.
        raise ValueError("unknown rule family")
    return parameters


_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_registry (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    registry_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verified_evidence_v2 (
    certificate_id TEXT PRIMARY KEY,
    certificate_json TEXT NOT NULL,
    source_rows_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL
);
"""


class EvidenceRegistry:
    """Immutable offline registry of proof-verified, typed evidence facts."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(_EVIDENCE_SCHEMA)
            row = conn.execute(
                "SELECT registry_id FROM evidence_registry WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise ValueError("evidence registry has not been explicitly bootstrapped")
        self.registry_id = row[0]

    @classmethod
    def bootstrap(cls, db_path: str, certificates: object) -> "EvidenceRegistry":
        if not isinstance(certificates, tuple) or not certificates:
            raise ValueError("verified evidence certificates are required")
        normalized: dict[str, tuple[str, str, str, str]] = {}
        for entry in certificates:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("evidence import requires certificate and source rows")
            certificate, source_records = entry
            certificate_json, rows_json, facts_json, evidence_hash = cls._verified_entry(
                certificate, source_records)
            certificate_id = certificate.proof_hash
            if certificate_id in normalized:
                raise ValueError("duplicate evidence certificate")
            normalized[certificate_id] = (
                certificate_json, rows_json, facts_json, evidence_hash)
        registry_id = hashlib.sha256(_canonical({
            "certificates": {key: value[3] for key, value in sorted(normalized.items())},
            "schema_version": "evidence-registry-v2",
        }).encode()).hexdigest()
        conn = connect(db_path)
        try:
            conn.executescript(_EVIDENCE_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM evidence_registry WHERE singleton=1").fetchone():
                raise ValueError("evidence registry is already bootstrapped")
            conn.execute("INSERT INTO evidence_registry VALUES (1,?)", (registry_id,))
            for certificate_id, values in sorted(normalized.items()):
                conn.execute(
                    "INSERT INTO verified_evidence_v2 VALUES (?,?,?,?,?)",
                    (certificate_id, *values),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return cls(db_path)

    @staticmethod
    def _verified_entry(certificate: object, source_records: object,
                        ) -> tuple[str, str, str, str]:
        from .certificates import ProofCertificate, _row_id, source_hash_map
        from .verifier import verify_certificate

        if not isinstance(certificate, ProofCertificate) or not isinstance(source_records, tuple):
            raise ValueError("evidence import requires an actual proof certificate and source rows")
        rows = tuple(source_records)
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("evidence certificate source rows are required")
        checked = verify_certificate(
            certificate, list(rows), expected_config_hash=certificate.config_hash,
            expected_calibration_id=certificate.calibration_id)
        if not checked.valid:
            raise ValueError(
                "evidence certificate verification failed: " + ",".join(checked.error_codes))
        actual_hashes = source_hash_map(rows)
        claimed = set(dict(certificate.source_hashes)) - {"BATCH_ROOT"}
        indexed = {_row_id(row): row for row in rows}
        if claimed - actual_hashes.keys():
            raise ValueError("evidence certificate source rows are incomplete")
        facts: list[dict[str, object]] = []
        claimed_rows = [indexed[row_id] for row_id in sorted(claimed)]
        captures = [row for row in claimed_rows if "payment_id" in row]
        credits = [row for row in claimed_rows
                   if "txn_id" in row and row.get("credit_debit") == "credit"]
        for capture in captures:
            for credit in credits:
                try:
                    from datetime import date
                    delay = (
                        date.fromisoformat(str(credit.get("value_date")))
                        - date.fromisoformat(str(capture.get("captured_at")))
                    ).days
                except ValueError:
                    continue
                if 0 <= delay <= 7:
                    facts.append({"type": "date_window_days", "value": delay})
        for row in claimed_rows:
            narration = row.get("narration")
            if (isinstance(narration, str) and 0 < len(narration) <= 64
                    and _SAFE_TEXT_RE.fullmatch(narration)
                    and not any(char in narration for char in "*+?{}[]()|\\^$")):
                facts.append({"type": "narration_literal", "value": narration})
            for field in ("utr", "settlement_id", "payment_id", "order_id"):
                value = row.get(field)
                if isinstance(value, str) and value and len(value) <= 128:
                    facts.append({"type": "exact_reference", "field": field, "value": value})
        unique = tuple(json.loads(item) for item in sorted({_canonical(fact) for fact in facts}))
        certificate_json = certificate.to_json()
        rows_json = _canonical(sorted(rows, key=_canonical))
        facts_json = _canonical(list(unique))
        evidence_hash = hashlib.sha256(_canonical({
            "certificate_hash": certificate.proof_hash,
            "facts": list(unique),
            "source_rows_hash": hashlib.sha256(rows_json.encode()).hexdigest(),
            "schema_version": "verified-evidence-v2",
        }).encode()).hexdigest()
        return certificate_json, rows_json, facts_json, evidence_hash

    @staticmethod
    def _required_facts(family: RuleFamily,
                        parameters: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        params = _validate_parameters(family, parameters)
        if family is RuleFamily.DATE_WINDOW:
            return ({"type": "date_window_days", "value": params["days"]},)
        if family is RuleFamily.NARRATION_PATTERN:
            return ({"type": "narration_literal", "value": params["pattern"]},)
        if family in {RuleFamily.FEE_SCHEDULE, RuleFamily.SOURCE_ALIAS}:
            raise ValueError("rule family is not derivable from certificate evidence")
        if set(params) == {"field", "value"}:
            return ({"type": "exact_reference", **params},)
        return ({"type": "exact_reference_fields", **params},)

    def assertion(self, certificate_id: str, family: RuleFamily,
                  parameters: Mapping[str, object]) -> dict[str, str]:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT certificate_json,source_rows_json,facts_json,evidence_hash "
                "FROM verified_evidence_v2 "
                "WHERE certificate_id=?", (certificate_id,),
            ).fetchone()
        if row is None:
            raise ValueError("evidence certificate is not registered")
        from .certificates import ProofCertificate
        try:
            certificate = ProofCertificate.from_json(row[0])
            source_records = tuple(json.loads(row[1]))
            expected = self._verified_entry(certificate, source_records)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("evidence certificate integrity verification failed") from exc
        if (certificate.proof_hash != certificate_id
                or row[0] != expected[0] or row[1] != expected[1]
                or row[2] != expected[2] or row[3] != expected[3]):
            raise ValueError("evidence registry integrity mismatch")
        required = self._required_facts(RuleFamily(family), parameters)
        available = json.loads(row[2])
        if any(fact not in available for fact in required):
            raise ValueError("evidence does not semantically support every rule parameter")
        return {
            "evidence_hash": row[3],
            "fact_hash": hashlib.sha256(_canonical(list(required)).encode()).hexdigest(),
            "registry_id": self.registry_id,
        }

    def supports(self, certificate_id: str, family: RuleFamily,
                 parameters: Mapping[str, object]) -> bool:
        try:
            self.assertion(certificate_id, family, parameters)
            return True
        except ValueError as exc:
            if any(word in str(exc) for word in ("certificate", "proof", "integrity")):
                raise
            return False


@dataclass(frozen=True)
class RuleProposal:
    rule_id: str
    family: RuleFamily
    parameters: Mapping[str, object]
    evidence_case_ids: tuple[str, ...]
    status: RuleStatus
    version: int
    source_case_hashes: tuple[tuple[str, str], ...] = ()
    author: str = ""
    approver: str = ""
    activated_by: str = ""
    activation_time: str = ""

    def __post_init__(self) -> None:
        try:
            family = self.family if isinstance(self.family, RuleFamily) else RuleFamily(self.family)
            status = self.status if isinstance(self.status, RuleStatus) else RuleStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("rule family and status are closed enums") from exc
        if not isinstance(self.rule_id, str) or _ID_RE.fullmatch(self.rule_id) is None:
            raise ValueError("invalid rule id")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("rule version must be a positive integer")
        parameters = _validate_parameters(family, self.parameters)
        if (not isinstance(self.evidence_case_ids, tuple) or not self.evidence_case_ids
                or any(not isinstance(value, str) or not value for value in self.evidence_case_ids)):
            raise ValueError("evidence case ids must be a non-empty immutable tuple")
        if tuple(sorted(set(self.evidence_case_ids))) != self.evidence_case_ids:
            raise ValueError("evidence case ids must be unique and canonically sorted")
        hashes = tuple(self.source_case_hashes)
        if tuple(sorted(hashes)) != hashes or tuple(case_id for case_id, _ in hashes) != self.evidence_case_ids:
            raise ValueError("source case hashes must correspond to evidence cases")
        if any(_HASH_RE.fullmatch(value) is None for _, value in hashes):
            raise ValueError("invalid source case hash")
        author = _human_actor(self.author)
        approver = self.approver
        activated_by = self.activated_by
        activation_time = self.activation_time
        if status in {RuleStatus.APPROVED, RuleStatus.ACTIVE}:
            approver = _human_actor(approver)
        elif approver:
            raise ValueError("an unapproved rule cannot name an approver")
        if status is RuleStatus.ACTIVE:
            activated_by = _human_actor(activated_by)
            if not isinstance(activation_time, str) or _UTC_RE.fullmatch(activation_time) is None:
                raise ValueError("an active rule requires a UTC activation time")
        elif activation_time or activated_by:
            raise ValueError("only an active rule can have activation metadata")

        object.__setattr__(self, "family", family)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "parameters", MappingProxyType(parameters))
        object.__setattr__(self, "source_case_hashes", hashes)
        object.__setattr__(self, "author", author)
        object.__setattr__(self, "approver", approver)
        object.__setattr__(self, "activated_by", activated_by)

    @property
    def identity(self) -> str:
        return f"{self.rule_id}@v{self.version}"

    def to_dict(self) -> dict[str, object]:
        return {
            "activation_time": self.activation_time,
            "activated_by": self.activated_by,
            "approver": self.approver,
            "author": self.author,
            "evidence_case_ids": list(self.evidence_case_ids),
            "family": self.family.value,
            "parameters": dict(self.parameters),
            "rule_id": self.rule_id,
            "schema_version": RULE_SCHEMA_VERSION,
            "source_case_hashes": dict(self.source_case_hashes),
            "status": self.status.value,
            "version": self.version,
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_json(cls, encoded: str) -> "RuleProposal":
        try:
            value = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid rule JSON") from exc
        expected = {
            "activation_time", "activated_by", "approver", "author", "evidence_case_ids", "family",
            "parameters", "rule_id", "schema_version", "source_case_hashes", "status",
            "version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid rule proposal schema")
        if value["schema_version"] != RULE_SCHEMA_VERSION:
            raise ValueError("unsupported rule proposal schema version")
        if not isinstance(value["evidence_case_ids"], list) or not isinstance(value["source_case_hashes"], dict):
            raise ValueError("invalid rule proposal collections")
        return cls(
            rule_id=value["rule_id"], family=value["family"], parameters=value["parameters"],
            evidence_case_ids=tuple(value["evidence_case_ids"]), status=value["status"],
            version=value["version"],
            source_case_hashes=tuple(sorted(value["source_case_hashes"].items())),
            author=value["author"], approver=value["approver"],
            activated_by=value["activated_by"], activation_time=value["activation_time"],
        )


def compile_resolution(case_events: Iterable[object]) -> RuleProposal | None:
    """Compile one complete structured suggestion from a verified human case.

    The compiler deliberately does not inspect free-text evidence references.
    Missing suggestions return ``None``; malformed or forged suggestions fail
    closed with an exception.
    """
    from .cases import AuthenticatedCaseEvents

    if not isinstance(case_events, AuthenticatedCaseEvents) or not case_events.verify():
        raise ValueError("compile_resolution requires authenticated CaseStore events")
    events = tuple(case_events)
    if not events:
        return None
    case_ids = {getattr(event, "case_id", None) for event in events}
    if len(case_ids) != 1 or None in case_ids:
        raise ValueError("a proposal must come from one case history")
    if tuple(getattr(event, "seq", None) for event in events) != tuple(range(1, len(events) + 1)):
        raise ValueError("case history is incomplete or out of order")
    terminal = events[-1]
    to_state = getattr(getattr(terminal, "to_state", None), "value", None)
    verified = any(
        getattr(getattr(event, "to_state", None), "value", None) == "VERIFIED"
        for event in events[:-1]
    )
    if to_state != "RESOLVED" or not verified:
        return None
    metadata = dict(getattr(terminal, "metadata", {}) or {})
    suggestion = metadata.get("rule_suggestion")
    if suggestion is None:
        return None
    expected_metadata = {
        "authority", "certificate_id", "evidence_assertion", "origin", "resolution",
        "rule_suggestion", "verified",
    }
    if set(metadata) != expected_metadata:
        raise ValueError("invalid structured resolution metadata schema")
    authority = HumanAuthority.from_dict(metadata["authority"])
    actor = AnalystRegistry(case_events.db_path).verify_consumed(
        authority, HumanAction.RESOLVE, subject_id=case_events.case_id,
        subject_version=authority.subject_version,
    )
    if getattr(terminal, "actor", "") != actor:
        raise ValueError("resolution actor does not match human authority")
    if metadata["origin"] != "human" or metadata["verified"] is not True:
        raise ValueError("only verified human resolutions can propose rules")
    if metadata["resolution"] not in {"paid", "unpaid"}:
        raise ValueError("unknown human resolution")
    if not isinstance(suggestion, Mapping) or set(suggestion) != {"family", "parameters"}:
        raise ValueError("invalid rule suggestion schema")
    try:
        family = RuleFamily(suggestion["family"])
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown rule family") from exc
    parameters = _validate_parameters(family, suggestion["parameters"])
    if metadata["certificate_id"] != case_events.certificate_id:
        raise ValueError("resolution certificate identity is not authenticated")
    expected_assertion = EvidenceRegistry(case_events.db_path).assertion(
        case_events.certificate_id, family, parameters)
    if metadata["evidence_assertion"] != expected_assertion:
        raise ValueError("rule parameters are not bound to verified typed evidence")
    case_id = next(iter(case_ids))
    expected_hash = case_events.content_hash
    rule_digest = hashlib.sha256(_canonical({
        "family": family.value,
        "parameters": parameters,
        "source_case_hashes": {case_id: expected_hash},
        "schema_version": RULE_SCHEMA_VERSION,
    }).encode("utf-8")).hexdigest()
    return RuleProposal(
        rule_id=f"rule-{rule_digest}", family=family, parameters=parameters,
        evidence_case_ids=(case_id,), status=RuleStatus.PROPOSED, version=1,
        source_case_hashes=((case_id, expected_hash),), author=actor,
    )


class StaleRuleVersion(ValueError):
    def __init__(self, rule_id: str, expected: int, actual: int):
        super().__init__(f"{rule_id}: expected version {expected}, store is at {actual}")
        self.rule_id, self.expected, self.actual = rule_id, expected, actual


_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    replay_json  TEXT NOT NULL,
    store_schema TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);
CREATE TABLE IF NOT EXISTS rule_replay_receipts (
    rule_id TEXT NOT NULL,
    transition_version INTEGER NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (rule_id, transition_version)
);
"""


class RuleStore:
    """Append-only rule versions in the same SQLite format on every machine."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(_STORE_SCHEMA)

    def add(self, proposal: RuleProposal) -> RuleProposal:
        if proposal.status is not RuleStatus.PROPOSED or proposal.version != 1:
            raise ValueError("only a version-one PROPOSED rule can be added")
        from .cases import CaseStore, UnknownCase

        if len(proposal.evidence_case_ids) != 1:
            raise ValueError("authenticated proposals require exactly one source case")
        try:
            expected = compile_resolution(
                CaseStore(self.db_path).events(proposal.evidence_case_ids[0]))
        except (UnknownCase, ValueError) as exc:
            raise ValueError("proposal has no authenticated case evidence") from exc
        if expected is None or expected.to_json() != proposal.to_json():
            raise ValueError("proposal does not match the authenticated proposal payload")
        try:
            with connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO rule_versions VALUES (?,?,?,?,?,?)",
                    (proposal.rule_id, proposal.version, proposal.status.value,
                     proposal.to_json(), "", RULE_STORE_SCHEMA),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("rule proposal version already exists") from exc
        return proposal

    def get(self, rule_id: str, version: int | None = None) -> RuleProposal:
        with connect(self.db_path) as conn:
            if version is None:
                row = conn.execute(
                    "SELECT proposal_json FROM rule_versions WHERE rule_id=? "
                    "ORDER BY version DESC LIMIT 1", (rule_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT proposal_json FROM rule_versions WHERE rule_id=? AND version=?",
                    (rule_id, version),
                ).fetchone()
        if row is None:
            raise KeyError(f"unknown rule version {rule_id}@v{version}")
        return RuleProposal.from_json(row[0])

    def history(self, rule_id: str) -> list[RuleProposal]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT proposal_json FROM rule_versions WHERE rule_id=? ORDER BY version",
                (rule_id,),
            ).fetchall()
        return [RuleProposal.from_json(row[0]) for row in rows]

    def replay_report(self, rule_id: str, transition_version: int) -> object:
        """Read the immutable replay evidence stored with a lifecycle version."""
        from .replay import ReplayReport

        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT replay_json FROM rule_versions WHERE rule_id=? AND version=?",
                (rule_id, transition_version),
            ).fetchone()
        if row is None or not row[0]:
            raise KeyError(f"no replay report for {rule_id}@v{transition_version}")
        return ReplayReport.from_json(row[0])

    def _transition(self, rule_id: str, expected_version: int, required: RuleStatus,
                    target: RuleStatus, *, approver: str = "", activation_time: str = "",
                    activated_by: str = "", replay_json: str = "",
                    replay_receipt_json: str = "",
                    authority: HumanAuthority | None = None,
                    authority_action: HumanAction | None = None) -> RuleProposal:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version, proposal_json FROM rule_versions WHERE rule_id=? "
                "ORDER BY version DESC LIMIT 1", (rule_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown rule {rule_id}")
            if row[0] != expected_version:
                raise StaleRuleVersion(rule_id, expected_version, row[0])
            current = RuleProposal.from_json(row[1])
            if current.status is not required:
                raise ValueError(f"rule must be {required.value} before {target.value}")
            human = ""
            if authority_action is not None:
                human = AnalystRegistry(self.db_path)._verify_and_consume(
                    conn, authority, authority_action, rule_id, expected_version)
                if authority_action is HumanAction.APPROVE:
                    approver = human
                elif authority_action is HumanAction.ACTIVATE:
                    activated_by = human
            updated = replace(
                current, status=target, version=current.version + 1,
                approver=approver or current.approver,
                activated_by=activated_by,
                activation_time=activation_time,
            )
            conn.execute(
                "INSERT INTO rule_versions VALUES (?,?,?,?,?,?)",
                (updated.rule_id, updated.version, updated.status.value,
                 updated.to_json(), replay_json, RULE_STORE_SCHEMA),
            )
            if replay_receipt_json:
                conn.execute(
                    "INSERT INTO rule_replay_receipts VALUES (?,?,?)",
                    (updated.rule_id, updated.version, replay_receipt_json),
                )
            conn.commit()
            return updated
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_replay(self, rule_id: str, expected_version: int, receipt: object) -> RuleProposal:
        from .replay import ReplayCorpusStore, ReplayReceipt

        if not isinstance(receipt, ReplayReceipt):
            raise TypeError("runner-authenticated replay receipt is required")
        current = self.get(rule_id)
        try:
            validated = ReplayCorpusStore(self.db_path).verify(current, receipt)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid replay receipt: {exc}") from exc
        encoded = validated.to_json()
        status = validated.status
        if status not in {RuleStatus.REPLAY_PASSED, RuleStatus.REJECTED}:
            raise ValueError("replay report has no valid gate result")
        return self._transition(
            rule_id, expected_version, RuleStatus.PROPOSED, status,
            replay_json=encoded, replay_receipt_json=receipt.to_json(),
        )

    def approve(self, rule_id: str, expected_version: int,
                authority: HumanAuthority) -> RuleProposal:
        AnalystRegistry(self.db_path).verify(
            authority, HumanAction.APPROVE,
            subject_id=rule_id, subject_version=expected_version)
        return self._transition(
            rule_id, expected_version, RuleStatus.REPLAY_PASSED, RuleStatus.APPROVED,
            authority=authority, authority_action=HumanAction.APPROVE,
        )

    def _history_hash(self, rule_id: str, through_version: int) -> str:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT v.version,v.proposal_json,v.replay_json,v.store_schema,"
                "COALESCE(r.receipt_json,'') FROM rule_versions v "
                "LEFT JOIN rule_replay_receipts r ON r.rule_id=v.rule_id "
                "AND r.transition_version=v.version "
                "WHERE v.rule_id=? AND v.version<=? ORDER BY v.version",
                (rule_id, through_version),
            ).fetchall()
        return hashlib.sha256(_canonical({
            "rule_id": rule_id,
            "versions": [list(row) for row in rows],
        }).encode("utf-8")).hexdigest()

    def activate(self, rule_id: str, expected_version: int,
                 authority: HumanAuthority, activation_time: str) -> ActivationReceipt:
        registry = AnalystRegistry(self.db_path)
        registry.verify(
            authority, HumanAction.ACTIVATE,
            subject_id=rule_id, subject_version=expected_version)
        active = self._transition(
            rule_id, expected_version, RuleStatus.APPROVED, RuleStatus.ACTIVE,
            authority=authority, authority_action=HumanAction.ACTIVATE,
            activation_time=activation_time,
        )
        return ActivationReceipt(
            rule_id=active.rule_id, rule_version=active.version,
            history_hash=self._history_hash(active.rule_id, active.version),
            registry_id=registry.registry_id, activated_by=active.activated_by,
            activation_time=active.activation_time,
        )

    def validate_activation(self, receipt: object) -> RuleProposal:
        if not isinstance(receipt, ActivationReceipt):
            raise ValueError("store-issued activation receipt is required")
        registry = AnalystRegistry(self.db_path)
        if receipt.registry_id != registry.registry_id:
            raise ValueError("activation receipt belongs to another registry")
        try:
            active = self.get(receipt.rule_id, receipt.rule_version)
        except KeyError as exc:
            raise ValueError("activation receipt names an unknown rule version") from exc
        if (
            active.status is not RuleStatus.ACTIVE
            or active.activated_by != receipt.activated_by
            or active.activation_time != receipt.activation_time
            or self._history_hash(active.rule_id, active.version) != receipt.history_hash
        ):
            raise ValueError("activation receipt does not match persisted lifecycle history")
        return active
