"""Canonical source records at the boundary of the reconciliation engine."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Protocol


class SourceKind(str, Enum):
    ORDER = "order"
    CAPTURE = "capture"
    BANK_TXN = "bank_txn"


class SourceValidationError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True)
class SourceEnvelope:
    source: SourceKind
    source_row_id: str
    schema_version: str
    raw_hash: str
    normalized_hash: str
    received_at: str
    normalized: dict[str, object]


@dataclass(frozen=True)
class IngestionSummary:
    accepted: int
    quarantined: int
    duplicate_ids: tuple[str, ...]
    source_hashes: tuple[str, ...]


class QuarantineWriter(Protocol):
    def append(self, source: str, row: dict, error_code: str, detail: str) -> str: ...


_MONEY_FIELDS = {
    SourceKind.ORDER: ("amount_paise",),
    SourceKind.CAPTURE: ("amount_paise", "fee_paise", "tax_paise"),
    SourceKind.BANK_TXN: ("amount_paise", "balance_after"),
}

_ID_FIELDS = {
    SourceKind.ORDER: "order_id",
    SourceKind.CAPTURE: "payment_id",
    SourceKind.BANK_TXN: "txn_id",
}

_REQUIRED_FIELDS = {
    SourceKind.ORDER: frozenset({
        "order_id", "invoice_no", "customer_id", "customer_name", "amount_paise",
        "due_date", "status", "channel_expected",
    }),
    SourceKind.CAPTURE: frozenset({
        "payment_id", "order_id", "amount_paise", "fee_paise", "tax_paise",
        "status", "method", "captured_at", "settlement_id", "utr",
    }),
    SourceKind.BANK_TXN: frozenset({
        "txn_id", "value_date", "amount_paise", "credit_debit", "utr",
        "narration", "balance_after",
    }),
}

_DATE_FIELDS = {
    SourceKind.ORDER: ("due_date",),
    SourceKind.CAPTURE: ("captured_at",),
    SourceKind.BANK_TXN: ("value_date",),
}

_STRING_FIELDS = {
    source: required - set(_MONEY_FIELDS[source])
    for source, required in _REQUIRED_FIELDS.items()
}

_ENUM_FIELDS = {
    SourceKind.ORDER: {
        "status": frozenset({"paid", "unpaid", "partial"}),
        "channel_expected": frozenset({"gateway", "bank_transfer", "mandate"}),
    },
    SourceKind.CAPTURE: {
        "status": frozenset({"captured", "failed", "refund", "chargeback_open"}),
    },
    SourceKind.BANK_TXN: {
        "credit_debit": frozenset({"credit", "debit"}),
    },
}

_PII_FIELDS = frozenset({"customer_id", "customer_name", "email", "phone", "contact", "narration"})


def canonical_json(value: object) -> bytes:
    """Encode a JSON value identically regardless of mapping insertion order."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _mask_value(value: object, field: str = "") -> object:
    if field in _PII_FIELDS and value:
        return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    if isinstance(value, dict):
        return {str(key): _mask_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_value(item) for item in value]
    return value


def mask_pii(row: dict[str, object]) -> dict[str, object]:
    return {field: _mask_value(value, field) for field, value in row.items()}


def validate_row(source: SourceKind, row: dict[str, object]) -> SourceEnvelope:
    missing = _REQUIRED_FIELDS[source] - row.keys()
    id_field = _ID_FIELDS[source]
    if id_field in missing or not isinstance(row.get(id_field), str) or not row.get(id_field, "").strip():
        raise SourceValidationError("MISSING_ID", f"{source.value}.{id_field} is required")
    if missing:
        raise SourceValidationError(
            "MISSING_FIELD",
            f"{source.value} missing required fields: {sorted(missing)}",
        )
    unknown = row.keys() - _REQUIRED_FIELDS[source]
    if unknown:
        raise SourceValidationError(
            "UNKNOWN_FIELD",
            f"{source.value} has unknown fields: {sorted(unknown)}",
        )

    for field in _MONEY_FIELDS[source]:
        if type(row[field]) is not int:
            raise SourceValidationError(
                "FLOAT_MONEY",
                f"{source.value}.{field} must be integer paise",
            )

    for field in _STRING_FIELDS[source]:
        if not isinstance(row[field], str):
            raise SourceValidationError(
                "INVALID_TYPE",
                f"{source.value}.{field} must be a string",
            )

    if source is SourceKind.ORDER and row["amount_paise"] <= 0:
        raise SourceValidationError("INVALID_MONEY", "order amount must be positive")
    if source is SourceKind.BANK_TXN and row["amount_paise"] <= 0:
        raise SourceValidationError("INVALID_MONEY", "bank transaction amount must be positive")
    if source is SourceKind.CAPTURE:
        amount = row["amount_paise"]
        if amount == 0 or row["fee_paise"] < 0 or row["tax_paise"] < 0:
            raise SourceValidationError("INVALID_MONEY", "capture amount/sign or fees are invalid")

    for field in _DATE_FIELDS[source]:
        raw = row[field]
        try:
            parsed = date.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != raw:
            raise SourceValidationError("INVALID_DATE", f"{source.value}.{field} is not YYYY-MM-DD")

    for field, allowed in _ENUM_FIELDS[source].items():
        if row[field] not in allowed:
            raise SourceValidationError(
                "INVALID_ENUM",
                f"{source.value}.{field} has unsupported value {row[field]!r}",
            )

    if source is SourceKind.CAPTURE:
        is_refund = row["status"] == "refund"
        if (is_refund and row["amount_paise"] > 0) or (not is_refund and row["amount_paise"] < 0):
            raise SourceValidationError("INVALID_MONEY", "capture amount sign disagrees with status")

    normalized = mask_pii(row)
    return SourceEnvelope(
        source=source,
        source_row_id=str(row[_ID_FIELDS[source]]),
        schema_version="1",
        raw_hash=sha256_hex(row),
        normalized_hash=sha256_hex(normalized),
        received_at=datetime.now(timezone.utc).isoformat(),
        normalized=normalized,
    )


def validate_rows(source: SourceKind, rows: list[dict],
                  quarantine_store: QuarantineWriter) -> tuple[list[dict], IngestionSummary]:
    accepted: list[dict] = []
    source_hashes: list[str] = []
    duplicate_ids: list[str] = []
    seen_ids: set[str] = set()
    quarantined = 0

    for row in rows:
        try:
            envelope = validate_row(source, row)
        except SourceValidationError as exc:
            quarantine_store.append(source.value, row, exc.code, str(exc))
            quarantined += 1
            continue

        if envelope.source_row_id in seen_ids:
            quarantine_store.append(
                source.value,
                row,
                "DUPLICATE_ID",
                f"duplicate {source.value} identifier {envelope.source_row_id!r}",
            )
            duplicate_ids.append(envelope.source_row_id)
            quarantined += 1
            continue

        seen_ids.add(envelope.source_row_id)
        accepted.append(dict(row))
        source_hashes.append(envelope.raw_hash)

    return accepted, IngestionSummary(
        accepted=len(accepted),
        quarantined=quarantined,
        duplicate_ids=tuple(duplicate_ids),
        source_hashes=tuple(source_hashes),
    )


def write_source_manifest(out_dir: str,
                          summaries: dict[SourceKind, IngestionSummary]) -> str:
    path = os.path.join(out_dir, "source_manifest.json")
    manifest = {"schema_version": "1", "sources": {}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("schema_version") != "1" or not isinstance(manifest.get("sources"), dict):
            raise SourceValidationError("INVALID_MANIFEST", "source manifest schema is invalid")

    for source, summary in summaries.items():
        manifest["sources"][source.value] = {
            "accepted": summary.accepted,
            "quarantined": summary.quarantined,
            "duplicate_ids": list(summary.duplicate_ids),
            "source_hashes": list(summary.source_hashes),
        }

    os.makedirs(out_dir, exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(temp_path, path)
    return path
