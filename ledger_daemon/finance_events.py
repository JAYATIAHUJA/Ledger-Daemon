"""Typed, immutable finance events accepted by accounting identities.

Money is always integer paise.  Event decoding is deliberately closed-world:
unknown event types and schema drift are exceptions for quarantine, never data
that reconciliation silently ignores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import TypeAlias

from .money import paise


class FinanceEventError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinanceEventError("MISSING_FIELD", f"{field} is required")
    return value


def _date(value: object, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise FinanceEventError("INVALID_DATE", f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise FinanceEventError("INVALID_DATE", f"{field} must be YYYY-MM-DD")
    return text


def _enum(value: object, field: str, allowed: frozenset[str]) -> str:
    text = _required_text(value, field)
    if text not in allowed:
        raise FinanceEventError("INVALID_ENUM", f"unsupported {field}: {text!r}")
    return text


@dataclass(frozen=True)
class Refund:
    refund_id: str
    payment_id: str
    order_id: str
    amount_paise: int
    status: str
    created_at: str

    def __post_init__(self) -> None:
        for field in ("refund_id", "payment_id", "order_id"):
            _required_text(getattr(self, field), field)
        paise(self.amount_paise)
        if self.amount_paise <= 0:
            raise FinanceEventError("INVALID_MONEY", "refund amount must be positive")
        _enum(self.status, "status", frozenset({"pending", "processed", "failed"}))
        _date(self.created_at, "created_at")


@dataclass(frozen=True)
class Dispute:
    dispute_id: str
    payment_id: str
    order_id: str
    amount_paise: int
    status: str
    created_at: str

    def __post_init__(self) -> None:
        for field in ("dispute_id", "payment_id", "order_id"):
            _required_text(getattr(self, field), field)
        paise(self.amount_paise)
        if self.amount_paise <= 0:
            raise FinanceEventError("INVALID_MONEY", "dispute amount must be positive")
        _enum(self.status, "status", frozenset({"open", "won", "lost", "closed"}))
        _date(self.created_at, "created_at")


@dataclass(frozen=True)
class Adjustment:
    adjustment_id: str
    settlement_id: str
    amount_paise: int
    kind: str
    created_at: str

    def __post_init__(self) -> None:
        _required_text(self.adjustment_id, "adjustment_id")
        _required_text(self.settlement_id, "settlement_id")
        paise(self.amount_paise)
        if self.amount_paise == 0:
            raise FinanceEventError("INVALID_MONEY", "adjustment amount cannot be zero")
        _enum(
            self.kind,
            "kind",
            frozenset({"credit", "debit", "fee_reversal", "gst_variance"}),
        )
        if ((self.kind in {"credit", "fee_reversal"} and self.amount_paise < 0)
                or (self.kind == "debit" and self.amount_paise > 0)):
            raise FinanceEventError(
                "INVALID_SIGN", f"{self.kind} adjustment sign contradicts its direction"
            )
        _date(self.created_at, "created_at")


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    amount_paise: int
    settled_at: str
    utr: str
    status: str

    def __post_init__(self) -> None:
        _required_text(self.settlement_id, "settlement_id")
        paise(self.amount_paise)
        if self.amount_paise < 0:
            raise FinanceEventError("INVALID_MONEY", "settlement amount cannot be negative")
        _date(self.settled_at, "settled_at")
        if not isinstance(self.utr, str):
            raise FinanceEventError("INVALID_TYPE", "utr must be a string")
        _enum(self.status, "status", frozenset({"pending", "processed", "failed"}))


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    event_type: str
    source_ref: str
    amount_paise: int
    direction: str
    occurred_at: str

    def __post_init__(self) -> None:
        for field in ("entry_id", "event_type", "source_ref"):
            _required_text(getattr(self, field), field)
        paise(self.amount_paise)
        if self.amount_paise <= 0:
            raise FinanceEventError("INVALID_MONEY", "ledger-entry amount must be positive")
        _enum(self.direction, "direction", frozenset({"credit", "debit"}))
        if self.event_type not in {
            "tds_withheld", "fee_reversal", "gst_variance", "settlement_adjustment",
        }:
            raise FinanceEventError(
                "UNKNOWN_EVENT_TYPE", f"unsupported ledger event: {self.event_type!r}"
            )
        _date(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class TdsEvidence:
    """External tax evidence required before a net receipt can close an invoice.

    PANs are accepted only as SHA-256 digests: source adapters must mask the
    identifiers before this boundary.  ``tax_rule_id`` is deliberately data,
    not executable tax logic, so rules remain effective-date versioned.
    """

    evidence_id: str
    order_id: str
    amount_paise: int
    payer_pan_hash: str
    merchant_pan_hash: str
    tax_rule_id: str
    certificate_ref: str
    occurred_at: str

    def __post_init__(self) -> None:
        for field in ("evidence_id", "order_id", "tax_rule_id", "certificate_ref"):
            _required_text(getattr(self, field), field)
        paise(self.amount_paise)
        if self.amount_paise <= 0:
            raise FinanceEventError("INVALID_MONEY", "TDS amount must be positive")
        for field in ("payer_pan_hash", "merchant_pan_hash"):
            value = getattr(self, field)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise FinanceEventError("INVALID_HASH", f"{field} must be a SHA-256 hex digest")
        if re.fullmatch(r"[A-Z0-9]+:[A-Z0-9:_-]+@[0-9]{4}-[0-9]{2}-[0-9]{2}",
                        self.tax_rule_id) is None:
            raise FinanceEventError(
                "INVALID_RULE_ID", "tax_rule_id must include a law, rule and effective date"
            )
        effective = _date(self.tax_rule_id.rsplit("@", 1)[1], "tax_rule_id effective date")
        occurred = _date(self.occurred_at, "occurred_at")
        if effective > occurred:
            raise FinanceEventError(
                "RULE_NOT_EFFECTIVE", "tax rule cannot take effect after the withholding"
            )


FinanceEvent: TypeAlias = Refund | Dispute | Adjustment | Settlement | LedgerEntry | TdsEvidence


_SCHEMAS: dict[str, tuple[type[FinanceEvent], tuple[str, ...]]] = {
    "refund": (Refund, ("refund_id", "payment_id", "order_id", "amount_paise", "status", "created_at")),
    "dispute": (Dispute, ("dispute_id", "payment_id", "order_id", "amount_paise", "status", "created_at")),
    "adjustment": (Adjustment, ("adjustment_id", "settlement_id", "amount_paise", "kind", "created_at")),
    "settlement": (Settlement, ("settlement_id", "amount_paise", "settled_at", "utr", "status")),
    "ledger_entry": (LedgerEntry, ("entry_id", "ledger_event_type", "source_ref", "amount_paise", "direction", "occurred_at")),
    "tds_evidence": (TdsEvidence, (
        "evidence_id", "order_id", "amount_paise", "payer_pan_hash",
        "merchant_pan_hash", "tax_rule_id", "certificate_ref", "occurred_at",
    )),
}


def decode_finance_event(row: dict[str, object]) -> FinanceEvent:
    if not isinstance(row, dict):
        raise FinanceEventError("INVALID_SCHEMA", "finance event must be an object")
    kind = row.get("event_type")
    if kind not in _SCHEMAS:
        raise FinanceEventError("UNKNOWN_EVENT_TYPE", f"unsupported finance event: {kind!r}")
    cls, fields = _SCHEMAS[str(kind)]
    expected = frozenset(fields) | {"event_type"}
    if set(row) != expected:
        missing = sorted(expected - row.keys())
        unknown = sorted(row.keys() - expected)
        raise FinanceEventError(
            "INVALID_SCHEMA", f"finance event fields differ; missing={missing}, unknown={unknown}"
        )
    # The discriminator is not a constructor field except for LedgerEntry,
    # whose business event type is carried separately as ``ledger_event_type``.
    values = {field: row[field] for field in fields}
    if cls is LedgerEntry:
        values["event_type"] = values.pop("ledger_event_type")
    return cls(**values)  # type: ignore[arg-type]


def encode_finance_event(event: FinanceEvent) -> dict[str, object]:
    """Encode an event with an explicit, version-stable discriminator."""
    from dataclasses import asdict

    kinds = {
        Refund: "refund", Dispute: "dispute", Adjustment: "adjustment",
        Settlement: "settlement", LedgerEntry: "ledger_entry",
        TdsEvidence: "tds_evidence",
    }
    row = asdict(event)
    kind = kinds[type(event)]
    if isinstance(event, LedgerEntry):
        row["ledger_event_type"] = row.pop("event_type")
    return {"event_type": kind, **row}
