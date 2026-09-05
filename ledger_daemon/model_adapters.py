"""Optional challenger readers, and the rules for when one is allowed to run (F7).

The default reader is the regex incumbent: offline, deterministic, no model, no
socket. An adapter is opt-in through `LEDGER_DAEMON_READER` and is *still* not
authoritative when it runs — its output goes through the same span validation as
everything else, and anything it cannot answer cleanly falls straight back to the
regex reader on the same text.

Four failure modes are treated identically, because from the ledger's point of
view they are identical — no evidence arrived:

  no model listening | timeout | output that is not JSON | output that breaks
  the span schema

Each gets two attempts, then abstention. A model is never asked a second time
after it tries to send a forbidden field (money, verdict, action): that is a
boundary violation, not a flaky call, and retrying it would only be sampling
until the boundary happens to hold.

`ADAPTERS_ENABLED_BY_DEFAULT` is False and stays False until
`model_benchmark.gate_adapter` says a candidate raises held-out safe coverage
without costing a single wrongly-read rupee.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Callable

from .evidence_reader import (
    EvidenceProposal,
    EvidenceReader,
    RegexReader,
    abstain,
    check_input,
    reject_forbidden_fields,
    sanitize_spans,
    source_text_hash,
)
from .source_contracts import sha256_hex

READER_ENV = "LEDGER_DAEMON_READER"
MODEL_ENV = "LEDGER_DAEMON_READER_MODEL"

#: No adapter ships enabled. The benchmark gate is the only way this changes.
ADAPTERS_ENABLED_BY_DEFAULT = False

DEFAULT_MODEL_ID = "llama3.2"
DEFAULT_TIMEOUT_S = 15
DEFAULT_ATTEMPTS = 2
PROMPT_VERSION = "1"

_PROMPT = (
    "You label spans in one bank narration. Return ONLY JSON of the form "
    '{"spans": [{"kind": ..., "value": ..., "start": ..., "end": ..., '
    '"confidence_ppm": ...}]}. kind must be one of utr, upi_ref, vpa, invoice, '
    "mode, settlement_id, name. start/end are character offsets into the text "
    "and value must equal text[start:end] exactly. Do not compute totals, do "
    "not judge whether the invoice is paid, do not recommend any action. If you "
    'are unsure, return {"spans": []}.\n\nText (prompt v'
    + PROMPT_VERSION + "):\n"
)


def build_prompt(text: str) -> str:
    return _PROMPT + text


def _http_transport(model_id: str, timeout_s: int) -> Callable[[str], str]:
    """Local Ollama over stdlib HTTP. Imported lazily; never touched by default."""

    def transport(prompt: str) -> str:
        import urllib.request

        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps({"model": model_id, "prompt": prompt,
                             "stream": False, "format": "json"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode()).get("response", "")

    return transport


class JsonSpanReader:
    """A model that returns typed spans as JSON, held to the span contract.

    `transport` is injected so the boundary can be tested without a model, a
    socket, or a fixture recording pretending to be one.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID,
                 transport: Callable[[str], str] | None = None,
                 attempts: int = DEFAULT_ATTEMPTS,
                 timeout_s: int = DEFAULT_TIMEOUT_S):
        self.model_id = model_id
        self.attempts = max(1, attempts)
        self._transport = transport or _http_transport(model_id, timeout_s)

    @property
    def reader_id(self) -> str:
        return f"model:{self.model_id}"

    def extract(self, text: str, source_hash: str) -> EvidenceProposal:
        bad_input = check_input(text)
        if bad_input:
            return abstain(self.reader_id, (bad_input,))
        if source_hash and source_hash != source_text_hash(text):
            return abstain(self.reader_id, ("SOURCE_HASH_MISMATCH",))

        prompt = build_prompt(text)
        prompt_hash = sha256_hex(prompt)
        errors: list[str] = []
        for _attempt in range(self.attempts):
            try:
                raw_output = self._transport(prompt)
            except Exception:
                errors.append("MODEL_UNAVAILABLE")
                continue
            try:
                parsed = json.loads(raw_output)
            except (TypeError, ValueError):
                errors.append("MALFORMED_OUTPUT")
                continue
            if not isinstance(parsed, dict) or not isinstance(parsed.get("spans"), list):
                errors.append("MALFORMED_OUTPUT")
                continue
            if reject_forbidden_fields(parsed):
                # It tried to price, judge, or act. Do not ask again.
                return abstain(self.reader_id, tuple(errors) + ("DISALLOWED_FIELD",),
                               prompt_hash)
            spans, span_errors = sanitize_spans(parsed["spans"], text, source_hash,
                                                self.reader_id)
            return EvidenceProposal(spans, not spans,
                                    tuple(dict.fromkeys(errors + list(span_errors))),
                                    self.reader_id, prompt_hash)
        return abstain(self.reader_id, tuple(errors) + ("ATTEMPTS_EXHAUSTED",), prompt_hash)


class FallbackReader:
    """Run the challenger; the moment it abstains, read the text with regex."""

    def __init__(self, primary: EvidenceReader, fallback: EvidenceReader | None = None):
        self.primary = primary
        self.fallback = fallback or RegexReader()

    @property
    def reader_id(self) -> str:
        return f"{self.primary.reader_id}+{self.fallback.reader_id}"

    def extract(self, text: str, source_hash: str) -> EvidenceProposal:
        proposal = self.primary.extract(text, source_hash)
        if not proposal.abstained:
            return proposal
        backup = self.fallback.extract(text, source_hash)
        return replace(
            backup,
            errors=tuple(dict.fromkeys(proposal.errors + backup.errors)),
            model_id=self.reader_id,
            prompt_hash=proposal.prompt_hash,
            fallback_used=True,
        )


def build_reader(name: str, model_id: str = "") -> EvidenceReader:
    """Name -> reader. An unknown or unavailable name is the regex reader.

    Unknown names do not raise: an operator typo must degrade to the safe,
    deterministic path rather than take the exception queue down with it.
    """
    choice = (name or "").strip().lower()
    if choice in ("ollama", "model", "json-span"):
        return FallbackReader(JsonSpanReader(model_id or os.environ.get(MODEL_ENV)
                                             or DEFAULT_MODEL_ID))
    return RegexReader()


def default_reader() -> EvidenceReader:
    """What the rest of the system gets: regex, unless an operator opted in."""
    return build_reader(os.environ.get(READER_ENV, ""))
