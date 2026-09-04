"""Full pipeline passes with the LLM layer disabled (FR-4.4, AC-8)."""

import os

from ledger_daemon import agent
from ledger_daemon.evaluate import evaluate
from ledger_daemon.models import Verdict
from ledger_daemon.recon import reconcile


def test_fallback_when_no_model(monkeypatch):
    monkeypatch.delenv("LEDGER_DAEMON_LLM", raising=False)
    p = agent.propose("ORD-1", "some ambiguity", ["TXN-1"])
    assert p.proposed_verdict == Verdict.AMBIGUOUS.value
    assert p.confidence == 0.0


def test_schema_validation_rejects_garbage():
    assert agent._validate("ORD-1", {"proposed_verdict": "pay_everyone"}) is None
    assert agent._validate("ORD-1", {"proposed_verdict": "genuinely_unpaid",
                                     "confidence": 2.0, "evidence_refs": ["x"],
                                     "reasoning": "r"}) is None
    assert agent._validate("ORD-1", {"proposed_verdict": "genuinely_unpaid",
                                     "confidence": 0.9, "evidence_refs": [],
                                     "reasoning": "r"}) is None
    ok = agent._validate("ORD-1", {"proposed_verdict": "genuinely_unpaid",
                                   "confidence": 0.9, "evidence_refs": ["TXN-1"],
                                   "reasoning": "clear"})
    assert ok is not None and ok.confidence == 0.9


def test_pipeline_end_to_end_zero_llm(batch, monkeypatch):
    monkeypatch.delenv("LEDGER_DAEMON_LLM", raising=False)
    orders, captures, bank, truth = batch
    result = reconcile(orders, captures, bank)
    report = evaluate(42, orders, captures, result, truth)
    assert report.match_rate >= 0.85           # AC-2
    assert report.dcpr >= 0.90                 # AC-3
    assert report.false_hold_rate <= 0.08      # AC-3
    assert report.wrong_paise["LD"] == 0
    assert report.wrong_counts["B0"] > 0       # AC-4: the contrast is real
