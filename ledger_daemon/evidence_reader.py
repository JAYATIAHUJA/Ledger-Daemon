"""Typed evidence extraction — the only thing a model is allowed to say (F7).

A reader is handed one piece of text and the hash of the source row that text
came from. It may return character spans of that text, typed with one of a
fixed set of kinds. That is the whole vocabulary. There is no field in which a
reader can put a rupee figure, a verdict, an approval, or a rule id, so a
compromised or hallucinating model cannot express one — the boundary is the
schema, not the prompt.

Three properties are enforced on every span before it leaves this module:

  1. ``text[start:end] == value`` — a span points at the source, it never
     paraphrases it, so an invented reference cannot survive validation.
  2. the source hash matches the text handed in — a reader cannot be replayed
     against a row it did not read.
  3. the kind is on the allowlist and the span budget holds.

Anything else is dropped with an error code, and a reader that ends up with
nothing abstains. Abstention is a normal, expected outcome; the deterministic
matcher does not depend on any of this. Spans are evidence *offered* to the
exception workbench and the proposal layer, never authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .source_contracts import sha256_hex

EVIDENCE_SCHEMA_VERSION = "1"

MAX_SPANS = 32
MAX_INPUT_CHARS = 4096
MAX_NAME_CHARS = 40
CONFIDENCE_PPM = 1_000_000


class EvidenceKind(str, Enum):
    UTR = "utr"
    UPI_REF = "upi_ref"
    VPA = "vpa"
    INVOICE = "invoice"
    MODE = "mode"
    SETTLEMENT_ID = "settlement_id"
    NAME = "name"


ALLOWED_KINDS = frozenset(k.value for k in EvidenceKind)

#: Names a reader may never use for a field. Enforced structurally on the
#: dataclasses (tests) and on raw model output (`reject_forbidden_fields`).
FORBIDDEN_PROPOSAL_FIELDS = frozenset({
    "amount", "amount_paise", "paise", "total", "total_paise", "fee", "fee_paise",
    "tax", "tax_paise", "money_received_paise", "delta_due_paise", "balance",
    "verdict", "proposed_verdict", "decision", "action", "action_type",
    "approve", "approved", "signoff", "rule_id", "rule_ids", "policy",
    "chaseable", "execute", "state", "case_state",
})

ERROR_CODES = frozenset({
    "SOURCE_HASH_MISMATCH", "INPUT_EMPTY", "INPUT_TOO_LONG",
    "SPAN_OUT_OF_BOUNDS", "SPAN_NOT_SUBSTRING", "SPAN_KIND_NOT_ALLOWED",
    "SPAN_LIMIT_EXCEEDED", "SPAN_CONFIDENCE_INVALID", "SPAN_MALFORMED",
    "DISALLOWED_FIELD", "MALFORMED_OUTPUT", "MODEL_UNAVAILABLE",
    "ATTEMPTS_EXHAUSTED",
})


def source_text_hash(text: str) -> str:
    """One hashing convention for reader input, shared by every adapter."""
    return sha256_hex(text)


@dataclass(frozen=True)
class EvidenceSpan:
    kind: str
    value: str
    start: int
    end: int
    source_hash: str
    extractor: str
    confidence_ppm: int

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind, "value": self.value, "start": self.start,
            "end": self.end, "source_hash": self.source_hash,
            "extractor": self.extractor, "confidence_ppm": self.confidence_ppm,
        }


@dataclass(frozen=True)
class EvidenceProposal:
    spans: tuple[EvidenceSpan, ...] = ()
    abstained: bool = True
    errors: tuple[str, ...] = ()
    model_id: str = ""
    prompt_hash: str = ""
    fallback_used: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "spans": [s.to_dict() for s in self.spans],
            "abstained": self.abstained,
            "errors": list(self.errors),
            "model_id": self.model_id,
            "prompt_hash": self.prompt_hash,
            "fallback_used": self.fallback_used,
        }


class EvidenceReader(Protocol):
    reader_id: str

    def extract(self, text: str, source_hash: str) -> EvidenceProposal: ...


def abstain(model_id: str, errors: tuple[str, ...], prompt_hash: str = "",
            fallback_used: bool = False) -> EvidenceProposal:
    return EvidenceProposal((), True, tuple(dict.fromkeys(errors)), model_id,
                            prompt_hash, fallback_used)


def check_input(text: object) -> str | None:
    """Return the error code that forbids reading `text`, or None."""
    if not isinstance(text, str) or not text.strip():
        return "INPUT_EMPTY"
    if len(text) > MAX_INPUT_CHARS:
        return "INPUT_TOO_LONG"
    return None


def reject_forbidden_fields(raw: object) -> bool:
    """True when a raw model object mentions money, verdicts or actions anywhere."""
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and key.strip().lower() in FORBIDDEN_PROPOSAL_FIELDS:
                return True
            if reject_forbidden_fields(value):
                return True
    elif isinstance(raw, (list, tuple)):
        return any(reject_forbidden_fields(item) for item in raw)
    return False


def sanitize_spans(candidates: list[dict], text: str, source_hash: str,
                   extractor: str) -> tuple[tuple[EvidenceSpan, ...], tuple[str, ...]]:
    """Drop every candidate that is not an exact, in-bounds, allowed span.

    Candidates are considered in the order given; a later span overlapping an
    accepted one is discarded, so one stretch of text carries one meaning.
    """
    kept: list[EvidenceSpan] = []
    errors: list[str] = []
    taken: list[tuple[int, int]] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            errors.append("SPAN_MALFORMED")
            continue
        kind = raw.get("kind")
        value = raw.get("value")
        start, end = raw.get("start"), raw.get("end")
        confidence = raw.get("confidence_ppm", CONFIDENCE_PPM)
        if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
            errors.append("SPAN_KIND_NOT_ALLOWED")
            continue
        if not isinstance(value, str) or type(start) is not int or type(end) is not int:
            errors.append("SPAN_MALFORMED")
            continue
        if not 0 <= start < end <= len(text):
            errors.append("SPAN_OUT_OF_BOUNDS")
            continue
        if text[start:end] != value or not value.strip():
            errors.append("SPAN_NOT_SUBSTRING")
            continue
        if type(confidence) is not int or not 0 <= confidence <= CONFIDENCE_PPM:
            errors.append("SPAN_CONFIDENCE_INVALID")
            continue
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            continue
        if len(kept) >= MAX_SPANS:
            errors.append("SPAN_LIMIT_EXCEEDED")
            break
        taken.append((start, end))
        kept.append(EvidenceSpan(kind, value, start, end, source_hash, extractor, confidence))
    kept.sort(key=lambda s: (s.start, s.end, s.kind))
    return tuple(kept), tuple(dict.fromkeys(errors))


# --------------------------------------------------------------------------- #
# The incumbent: the narration regexes the matcher already trusts, wrapped so
# they are scored on the same benchmark as any challenger model.
# --------------------------------------------------------------------------- #

_MODE = re.compile(r"^\s*(NEFT|IMPS|RTGS|UPI|ACH)\b", re.IGNORECASE)
_SETTLEMENT = re.compile(r"RAZORPAYSETTLEMENT-(\S+)", re.IGNORECASE)
_UPI_REF = re.compile(r"UPI/(\d{9,12})", re.IGNORECASE)
_VPA = re.compile(r"([\w.\-]+@[\w]+)")
_INVOICE = re.compile(r"(?:INV|INVOICE)[\s\-/]?(\d{3,8})", re.IGNORECASE)
_UTR = re.compile(r"\b([A-Z]{4}[A-Z0-9]{6,18})\b")
_SEGMENT = re.compile(r"[^-/]+")

#: A name segment is prose, not a reference: no digits, no VPA, not a keyword,
#: and short enough to be a party name rather than a free-text instruction.
_NAME_STOPWORDS = frozenset({
    "PAYMENT FROM", "PAYMENT", "P2A", "P2P", "INV", "INVOICE", "REF", "REFERENCE",
    "NEFT", "IMPS", "RTGS", "UPI", "ACH", "CR", "DR", "BY", "FROM", "TO",
})


class RegexReader:
    """Deterministic, offline, zero-dependency. The default, and the baseline."""

    reader_id = "regex"

    def extract(self, text: str, source_hash: str) -> EvidenceProposal:
        bad_input = check_input(text)
        if bad_input:
            return abstain(self.reader_id, (bad_input,))
        if source_hash and source_hash != source_text_hash(text):
            return abstain(self.reader_id, ("SOURCE_HASH_MISMATCH",))
        spans, errors = sanitize_spans(self._candidates(text), text, source_hash,
                                       self.reader_id)
        return EvidenceProposal(spans, not spans, errors, self.reader_id, "")

    # Ordered by how much the shape constrains the meaning: an exact reference
    # wins the characters it covers, the name heuristic gets what is left over.
    def _candidates(self, text: str) -> list[dict]:
        out: list[dict] = []

        def add(kind: str, match: re.Match, confidence: int = CONFIDENCE_PPM) -> None:
            out.append({"kind": kind, "value": match.group(1),
                        "start": match.start(1), "end": match.end(1),
                        "confidence_ppm": confidence})

        for match in _SETTLEMENT.finditer(text):
            add(EvidenceKind.SETTLEMENT_ID.value, match)
        for match in _UPI_REF.finditer(text):
            add(EvidenceKind.UPI_REF.value, match)
        for match in _VPA.finditer(text):
            add(EvidenceKind.VPA.value, match)
        for match in _INVOICE.finditer(text):
            add(EvidenceKind.INVOICE.value, match)
        mode = _MODE.search(text)
        if mode:
            add(EvidenceKind.MODE.value, mode)
        for match in _UTR.finditer(text):
            # A UTR carries digits; RAZORPAYSETTLEMENT and friends do not.
            if any(ch.isdigit() for ch in match.group(1)):
                add(EvidenceKind.UTR.value, match)
        out.extend(self._name_candidates(text, bool(mode)))
        return out

    def _name_candidates(self, text: str, has_mode: bool) -> list[dict]:
        """The counterparty name in ``MODE-<ref>-<NAME>-INV<n>`` narrations.

        Confidence is 0.6 and stated as a heuristic, not a probability: the rule
        is positional, so a bank that reorders its fields silently costs recall —
        which is what the benchmark exists to measure rather than assume.
        """
        if not has_mode:
            return []
        for index, segment in enumerate(_SEGMENT.finditer(text)):
            if index == 0:
                continue
            raw = segment.group(0)
            stripped = raw.strip()
            if not stripped or len(stripped) > MAX_NAME_CHARS:
                continue
            if "@" in stripped or any(ch.isdigit() for ch in stripped):
                continue
            if stripped.upper() in _NAME_STOPWORDS or not stripped[0].isalpha():
                continue
            start = segment.start(0) + (len(raw) - len(raw.lstrip()))
            return [{"kind": EvidenceKind.NAME.value, "value": stripped,
                     "start": start, "end": start + len(stripped),
                     "confidence_ppm": 600_000}]
        return []
