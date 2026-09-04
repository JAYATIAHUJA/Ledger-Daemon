"""Sandboxed LLM proposal layer (FR-4).

Invoked ONLY on AMBIGUOUS orders. It has no tool access, no write path to any
store, and its output is a single schema-validated object that is input to the
policy engine — it can never alter state directly (FR-4.5).

When no local model is available the layer degrades to a deterministic
fallback (AMBIGUOUS, confidence 0.0) and the system runs end-to-end with zero
LLM installed (FR-4.4). A proposal from the LLM can only ever LOWER the bar:
policy rule R7 holds anything below 0.85 confidence for a human.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from .models import Verdict

VALID_VERDICTS = {v.value for v in Verdict}
MAX_REASONING_CHARS = 400


@dataclass
class Proposal:
    order_id: str
    proposed_verdict: str
    confidence: float
    evidence_refs: list[str]
    reasoning: str

    def to_dict(self) -> dict:
        return asdict(self)


def _validate(order_id: str, raw: dict) -> Proposal | None:
    """Strict schema validation. Any deviation -> None (caller falls back)."""
    try:
        verdict = raw["proposed_verdict"]
        confidence = raw["confidence"]
        refs = raw["evidence_refs"]
        reasoning = raw["reasoning"]
    except (KeyError, TypeError):
        return None
    if verdict not in VALID_VERDICTS:
        return None
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        return None
    if not isinstance(refs, list) or len(refs) < 1 or not all(isinstance(r, str) for r in refs):
        return None
    if not isinstance(reasoning, str) or len(reasoning) > MAX_REASONING_CHARS:
        return None
    return Proposal(order_id, verdict, float(confidence), refs, reasoning)


def fallback(order_id: str, evidence_refs: list[str]) -> Proposal:
    return Proposal(order_id, Verdict.AMBIGUOUS.value, 0.0,
                    evidence_refs or ["none"],
                    "deterministic fallback: no local model available; abstain")


def propose(order_id: str, evidence_summary: str, evidence_refs: list[str]) -> Proposal:
    """One call per AMBIGUOUS order, strict schema, 400-char reasoning cap.

    Uses a local Ollama instance only when LEDGER_DAEMON_LLM=ollama is set;
    anything unexpected (no server, bad JSON, schema drift) -> fallback.
    """
    if os.environ.get("LEDGER_DAEMON_LLM") != "ollama":
        return fallback(order_id, evidence_refs)
    try:
        import urllib.request
        prompt = (
            "You are reviewing one ambiguous payment reconciliation case. "
            "Return ONLY a JSON object with keys proposed_verdict (one of "
            f"{sorted(VALID_VERDICTS)}), confidence (0..1), evidence_refs "
            "(non-empty list of row ids), reasoning (<=400 chars).\n\n"
            f"Case {order_id}: {evidence_summary}"
        )
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps({
                "model": os.environ.get("LEDGER_DAEMON_LLM_MODEL", "llama3.2"),
                "prompt": prompt, "stream": False, "format": "json",
            }).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        parsed = json.loads(body.get("response", "{}"))
        proposal = _validate(order_id, parsed)
        return proposal if proposal is not None else fallback(order_id, evidence_refs)
    except Exception:
        return fallback(order_id, evidence_refs)
