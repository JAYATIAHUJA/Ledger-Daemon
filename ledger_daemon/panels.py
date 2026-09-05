"""What the operations screen knows, gathered once and rendered as panels (F10).

`ui.py` owns the chase list — the three columns a collections person works all
day. This module owns everything a controller needs *around* it before they are
willing to sign: where the rows came from, which proofs verified, what the
exception queue is holding, whether probabilistic authority is still in force,
and what the run actually cost.

Every panel is built from data the run already produced. Nothing here recomputes
a verdict, re-derives a rupee, or asks a model anything — a dashboard that
disagrees with the engine behind it would be worse than no dashboard.

Where a number was not measured, the panel says so in the words the operator
needs ("no rupee-risk calibration was fitted for this run") rather than showing
a zero that reads like a result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from .money import rupees_str
from .signoff import (
    AuthoritySummary, ExceptionSummary, ProofSummary, RiskSummary, SignoffDecision,
    SourceHealth, decide_signoff,
)
from .source_contracts import SourceKind, validate_rows
from .verifier import verify_certificate

#: How many certificates the screen verifies on load. `verify-proof` and the
#: judge verify exhaustively; a dashboard that took a minute to open would
#: simply not be opened.
UI_VERIFY_SAMPLE = 40


@dataclass
class StageRow:
    name: str
    detail: str
    millis: int = 0


@dataclass
class Panels:
    signoff: SignoffDecision | None = None
    source: SourceHealth | None = None
    proofs: ProofSummary | None = None
    exceptions: ExceptionSummary | None = None
    authority: AuthoritySummary | None = None
    risk: RiskSummary | None = None
    stages: list[StageRow] = field(default_factory=list)
    automation: dict[str, int] = field(default_factory=dict)
    verdict_counts: dict[str, int] = field(default_factory=dict)
    feeds: list[tuple[str, int, int, int]] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)
    cases: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    evaluation: str = ""
    recovery: dict[str, object] = field(default_factory=dict)
    proof_sample: int = 0
    notes: list[str] = field(default_factory=list)


class _NullQuarantine:
    """Counts rejections without writing a second copy of the run's quarantine."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, source: str, row: object, error_code: str, detail: str) -> str:
        self.records.append({"source": source, "error_code": error_code,
                             "detail": detail})
        return f"ui-{len(self.records)}"


def source_health(order_rows: list[dict], capture_rows: list[dict],
                  bank_rows: list[dict]) -> tuple[SourceHealth, list, list]:
    """Re-validate the three feeds so the screen shows what actually got in."""
    sink = _NullQuarantine()
    feeds = []
    accepted_total = duplicates = 0
    for kind, rows in ((SourceKind.ORDER, order_rows),
                       (SourceKind.CAPTURE, capture_rows),
                       (SourceKind.BANK_TXN, bank_rows)):
        accepted, summary = validate_rows(kind, rows, sink)
        feeds.append((kind.value, len(rows), summary.accepted, summary.quarantined))
        accepted_total += summary.accepted
        duplicates += len(summary.duplicate_ids)
    offered = len(order_rows) + len(capture_rows) + len(bank_rows)
    health = SourceHealth(rows_offered=offered, accepted=accepted_total,
                          quarantined=offered - accepted_total, duplicates=duplicates,
                          feeds_seen=sum(1 for _n, offered_n, _a, _q in feeds if offered_n),
                          feeds_expected=3)
    return health, feeds, sink.records


def proof_summary(certificates: dict, rows: list[dict], order_ids: list[str],
                  *, config_hash: str = "", calibration_id: str = "",
                  sample: int = UI_VERIFY_SAMPLE) -> tuple[ProofSummary, int]:
    """Verify a sample on load; report coverage gaps in full.

    `unverified` is not "we did not check it" — it is "this order was decided
    without a certificate at all", which is the thing that must block a signoff.
    """
    if not certificates:
        return ProofSummary(0, 0, 0, len(order_ids), ""), 0
    missing = sum(1 for order_id in order_ids if order_id not in certificates)
    checked = sorted(certificates)[:max(1, sample)]
    verified = rejected = 0
    for order_id in checked:
        result = verify_certificate(
            certificates[order_id], rows,
            expected_config_hash=config_hash or None,
            expected_calibration_id=calibration_id or None)
        if result.valid:
            verified += 1
        else:
            rejected += 1
    bundle_hash = ""
    if certificates:
        bundle_hash = certificates[sorted(certificates)[0]].proof_hash
    return (ProofSummary(built=len(certificates), verified=verified,
                         rejected=rejected, unverified=missing,
                         bundle_hash=bundle_hash),
            len(checked))


def exception_summary(cases: list, held_amounts: dict[str, int],
                      today: str = "") -> ExceptionSummary:
    open_cases = [c for c in cases if c.state.value not in ("resolved", "written_off")]
    material = sum(held_amounts.get(c.order_id, 0) for c in open_cases)
    reference = today or date.today().isoformat()
    ages = [_days_between(c.updated_at[:10], reference) for c in open_cases]
    return ExceptionSummary(
        open_cases=len(open_cases),
        material_open_paise=material,
        oldest_open_days=max(ages, default=0),
        stale_cases=sum(1 for age in ages if age > 30))


def _days_between(start: str, end: str) -> int:
    try:
        return max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days)
    except ValueError:
        return 0


def automation_mix(verdicts: dict) -> dict[str, int]:
    mix = {"exact": 0, "probabilistic": 0, "manual": 0, "authorized": 0}
    for verdict in verdicts.values():
        mix[verdict.evidence.automation_path] = mix.get(
            verdict.evidence.automation_path, 0) + 1
        mix["authorized"] += bool(verdict.evidence.risk_authorized)
    return mix


def authority_summary(verdicts: dict, calibration_id: str = "") -> AuthoritySummary:
    states = {v.evidence.authority_state for v in verdicts.values() if v.evidence.authority_state}
    state = sorted(states)[0] if states else "CALIBRATED"
    return AuthoritySummary(state=state, calibration_id=calibration_id,
                            probabilistic_halted=state in ("DEGRADED", "AUTOMATION_HALTED"))


def risk_summary(verdicts: dict, calibration=None, budget_bp: int = 10,
                 wrongly_chased_paise: int = 0,
                 duplicate_side_effects: int = 0) -> RiskSummary:
    mix = automation_mix(verdicts)
    if calibration is None:
        return RiskSummary(authorized=mix["authorized"] > 0, calibration_id="",
                           loss_upper_bound_bp=0, budget_bp=budget_bp,
                           wrongly_chased_paise=wrongly_chased_paise,
                           duplicate_side_effects=duplicate_side_effects)
    return RiskSummary(authorized=calibration.authority,
                       calibration_id=calibration.calibration_id,
                       loss_upper_bound_bp=calibration.loss_upper_bound_bp,
                       budget_bp=budget_bp,
                       wrongly_chased_paise=wrongly_chased_paise,
                       duplicate_side_effects=duplicate_side_effects)


def recovery_demo(view) -> dict[str, object]:
    """Downstream Control Demonstration: Why Correct Reconciliation Matters.

    Recovery is shown only to make the cost of getting reconciliation wrong
    legible. The blocked column is the whole argument: those are customers a
    schedule-driven chase would have contacted, and the money beside them is
    what a wrong chase would have been about.
    """
    return {
        "chased": len(view.safe),
        "chased_paise": view.total(view.safe),
        "blocked": len(view.blocked),
        "protected_paise": view.total(view.blocked),
        "held": len(view.needs_you),
        "held_paise": view.total(view.needs_you),
    }


def collect(*, view, verdicts: dict, order_rows: list[dict], capture_rows: list[dict],
            bank_rows: list[dict], certificates: dict, source_rows_list: list[dict],
            cases: list, audit: list[dict], order_ids: list[str],
            held_amounts: dict[str, int], config_hash: str = "",
            calibration_id: str = "", risk_calibration=None,
            evaluation: str = "", stages: list[StageRow] | None = None,
            quarantine_records: list[dict] | None = None) -> Panels:
    """Everything the dashboard shows, gathered once, from the run that happened."""
    started = time.perf_counter()
    health, feeds, rejected = source_health(order_rows, capture_rows, bank_rows)
    proofs, sample = proof_summary(certificates, source_rows_list, order_ids,
                                   config_hash=config_hash,
                                   calibration_id=calibration_id)
    exceptions = exception_summary(cases, held_amounts)
    authority = authority_summary(verdicts, calibration_id)
    risk = risk_summary(verdicts, risk_calibration)

    notes: list[str] = []
    if risk_calibration is None:
        notes.append(
            "No rupee-risk calibration was fitted for this run, so probabilistic "
            "evidence is held by R_RISK_BUDGET rather than acted on. That is the "
            "designed default, not a missing number.")
    if proofs.built and sample < proofs.built:
        notes.append(
            f"{sample} of {proofs.built} certificates were verified on load. "
            "`python -m ledger_daemon judge` and `verify-proof` verify exhaustively.")
    if not certificates:
        notes.append(
            "No proof bundle is attached to this batch, so no verdict on this "
            "screen can be checked against one. A live --dir run shows no proofs "
            "by design: a certificate from another batch would render against the "
            "wrong rows.")

    panels = Panels(
        signoff=decide_signoff(health, proofs, exceptions, authority, risk),
        source=health, proofs=proofs, exceptions=exceptions,
        authority=authority, risk=risk,
        stages=list(stages or []),
        automation=automation_mix(verdicts),
        verdict_counts=_verdict_counts(verdicts),
        feeds=feeds,
        quarantine=list(quarantine_records or rejected)[:200],
        cases=[{"case_id": c.case_id, "order_id": c.order_id,
                "reason_code": c.reason_code, "state": c.state.value,
                "version": c.version, "opened_at": c.opened_at,
                "updated_at": c.updated_at,
                "certificate_id": c.certificate_id} for c in cases],
        audit=audit[:300],
        evaluation=evaluation,
        recovery=recovery_demo(view),
        proof_sample=sample,
        notes=notes,
    )
    panels.stages.append(StageRow("dashboard", "panels gathered",
                                  int((time.perf_counter() - started) * 1000)))
    return panels


def _verdict_counts(verdicts: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for verdict in verdicts.values():
        counts[verdict.verdict.value] = counts.get(verdict.verdict.value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def money(paise: int) -> str:
    return rupees_str(paise)
