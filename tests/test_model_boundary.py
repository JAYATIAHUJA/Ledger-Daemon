"""The model boundary (F7).

A reader may point at text. It may not do arithmetic, issue a verdict, approve
an action, or write state — and the schema is where that is enforced, not the
prompt. Everything here runs with no model installed and no network.
"""

import json

import pytest

from ledger_daemon.evidence_reader import (
    FORBIDDEN_PROPOSAL_FIELDS,
    EvidenceProposal,
    EvidenceSpan,
    RegexReader,
    source_text_hash,
)
from ledger_daemon.model_adapters import (
    ADAPTERS_ENABLED_BY_DEFAULT,
    READER_ENV,
    FallbackReader,
    JsonSpanReader,
    build_reader,
    default_reader,
)
from ledger_daemon.model_benchmark import (
    FIXTURES,
    benchmark_readers,
    gate_adapter,
    render_report,
)

TEXT = "NEFT-HDFCP12345678-ACME TECHNOLOGIES PVT-INV1234"
HASH = source_text_hash(TEXT)


def _reader(*responses):
    """A JsonSpanReader whose transport replays canned model output."""
    replies = list(responses)

    def transport(prompt: str) -> str:
        if not replies:
            raise RuntimeError("model called more times than the test allows")
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return JsonSpanReader("test-model", transport=transport, attempts=2)


# ---- schema boundary ------------------------------------------------------- #

def test_evidence_schema_carries_no_money_verdict_or_action_field():
    fields = set(EvidenceSpan.__dataclass_fields__) | set(EvidenceProposal.__dataclass_fields__)
    assert not fields & FORBIDDEN_PROPOSAL_FIELDS
    body = RegexReader().extract(TEXT, HASH).to_dict()
    flat = json.dumps(body)
    for banned in ("verdict", "amount_paise", "approve", "action", "rule_id"):
        assert banned not in flat


def test_model_output_smuggling_a_verdict_or_amount_is_rejected():
    reader = _reader(json.dumps({
        "spans": [{"kind": "utr", "value": "HDFCP12345678", "start": 5, "end": 18,
                   "confidence_ppm": 900_000}],
        "verdict": "genuinely_unpaid",
        "amount_paise": 120000,
    }), json.dumps({"spans": []}))
    proposal = reader.extract(TEXT, HASH)
    assert "DISALLOWED_FIELD" in proposal.errors
    assert proposal.spans == ()


def test_span_not_present_in_the_source_is_dropped():
    reader = _reader(json.dumps({"spans": [
        {"kind": "utr", "value": "HDFCP12345678", "start": 5, "end": 18,
         "confidence_ppm": 900_000},
        {"kind": "name", "value": "GLOBEX CORP", "start": 19, "end": 30,
         "confidence_ppm": 900_000},
    ]}))
    proposal = reader.extract(TEXT, HASH)
    assert [s.value for s in proposal.spans] == ["HDFCP12345678"]
    assert "SPAN_NOT_SUBSTRING" in proposal.errors


def test_out_of_bounds_and_unknown_kinds_are_dropped():
    reader = _reader(json.dumps({"spans": [
        {"kind": "utr", "value": "x", "start": 10_000, "end": 10_010, "confidence_ppm": 1},
        {"kind": "account_balance", "value": "NEFT", "start": 0, "end": 4, "confidence_ppm": 1},
    ]}))
    proposal = reader.extract(TEXT, HASH)
    assert proposal.spans == ()
    assert "SPAN_OUT_OF_BOUNDS" in proposal.errors
    assert "SPAN_KIND_NOT_ALLOWED" in proposal.errors


# ---- failure is abstention, never a guess ---------------------------------- #

def test_malformed_output_retries_once_then_abstains():
    reader = _reader("not json at all", "{still not json")
    proposal = reader.extract(TEXT, HASH)
    assert proposal.abstained and proposal.spans == ()
    assert "MALFORMED_OUTPUT" in proposal.errors
    assert "ATTEMPTS_EXHAUSTED" in proposal.errors


def test_second_attempt_is_allowed_to_succeed():
    reader = _reader("garbage", json.dumps({"spans": [
        {"kind": "utr", "value": "HDFCP12345678", "start": 5, "end": 18,
         "confidence_ppm": 900_000}]}))
    proposal = reader.extract(TEXT, HASH)
    assert not proposal.abstained
    assert [s.value for s in proposal.spans] == ["HDFCP12345678"]


def test_transport_failure_abstains():
    reader = _reader(TimeoutError("no model listening"), TimeoutError("still nothing"))
    proposal = reader.extract(TEXT, HASH)
    assert proposal.abstained
    assert "MODEL_UNAVAILABLE" in proposal.errors


def test_abstention_falls_back_to_the_regex_reader_immediately():
    wrapped = FallbackReader(_reader("garbage", "garbage"), RegexReader())
    proposal = wrapped.extract(TEXT, HASH)
    assert proposal.fallback_used
    assert not proposal.abstained
    assert ("utr", "HDFCP12345678") in {(s.kind, s.value) for s in proposal.spans}
    assert all(s.extractor == "regex" for s in proposal.spans)


# ---- default-off, and no network without one ------------------------------- #

def test_adapters_are_disabled_by_default(monkeypatch):
    assert ADAPTERS_ENABLED_BY_DEFAULT is False
    monkeypatch.delenv(READER_ENV, raising=False)
    assert default_reader().reader_id == "regex"


def test_no_network_call_when_no_adapter_is_selected(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("the offline core must not open a socket")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.delenv(READER_ENV, raising=False)
    proposal = default_reader().extract(TEXT, HASH)
    assert not proposal.abstained


def test_unknown_adapter_name_falls_back_to_regex():
    reader = build_reader("a-model-nobody-installed")
    assert reader.extract(TEXT, HASH).spans


# ---- the benchmark is the gate --------------------------------------------- #

def test_benchmark_scores_the_regex_reader_and_is_deterministic():
    report = benchmark_readers(FIXTURES, [RegexReader()])
    again = benchmark_readers(FIXTURES, [RegexReader()])
    score = report.scores[0]
    assert score.reader_id == "regex"
    assert score.exact_span_f1_ppm == again.scores[0].exact_span_f1_ppm
    assert 0 <= score.precision_ppm <= 1_000_000
    assert 0 <= score.recall_ppm <= 1_000_000
    assert score.malformed == 0
    assert score.latency_p95_us >= score.latency_p50_us
    assert "regex" in render_report(report)


def test_adapter_stays_off_unless_safe_coverage_improves_without_new_wrong_rupees():
    base = benchmark_readers(FIXTURES, [RegexReader()]).scores[0]
    worse = base.__class__(**{**base.to_dict(), "reader_id": "candidate",
                              "safe_coverage_ppm": base.safe_coverage_ppm + 10_000,
                              "wrong_paise": base.wrong_paise + 1})
    same = base.__class__(**{**base.to_dict(), "reader_id": "candidate"})
    better = base.__class__(**{**base.to_dict(), "reader_id": "candidate",
                               "safe_coverage_ppm": base.safe_coverage_ppm + 10_000})
    assert not gate_adapter(base, worse).enabled
    assert not gate_adapter(base, same).enabled
    assert gate_adapter(base, better).enabled


def test_wrong_rupee_loss_is_integer_paise():
    score = benchmark_readers(FIXTURES, [RegexReader()]).scores[0]
    assert type(score.wrong_paise) is int
    for case in FIXTURES:
        assert type(case.amount_paise) is int


def test_prompt_injection_fixture_yields_no_authority():
    injection = [c for c in FIXTURES if "prompt_injection" in c.tags]
    assert injection, "the benchmark must cover prompt injection"
    for case in injection:
        proposal = RegexReader().extract(case.text, source_text_hash(case.text))
        for span in proposal.spans:
            assert case.text[span.start:span.end] == span.value
        assert not set(proposal.to_dict()) & FORBIDDEN_PROPOSAL_FIELDS


@pytest.mark.parametrize("tag", [
    "truncated_name", "multilingual", "ocr_noise", "invoice", "utr",
    "distractor_numbers", "prompt_injection", "empty", "overlong",
])
def test_benchmark_covers_every_required_condition(tag):
    assert any(tag in case.tags for case in FIXTURES)
