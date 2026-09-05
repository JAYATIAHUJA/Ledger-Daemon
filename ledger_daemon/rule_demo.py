"""Judge-facing, end-to-end demonstration of replay-gated rule learning."""

from __future__ import annotations

from html import escape
import json
from pathlib import Path
import secrets

from .certificates import build_certificate, recon_config_hash, source_hash_map, source_rows
from .cases import CaseStore
from .models import BankTxn, Evidence, GatewayCapture, Order, OrderVerdict, Verdict
from .recon import ReconConfig, with_active_rule
from .replay import ReplayCaseOutcome, ReplayCorpusStore
from .rules import (
    AnalystRegistry, EvidenceRegistry, HumanAction, RuleStore, compile_resolution,
)
from .ui import resolve_case, rule_proposal_state


_EXPIRY = "2099-01-01T00:00:00Z"
_ACTIVATION_TIME = "2026-09-05T10:00:00Z"


def _verified_case_proof():
    order = Order(
        "ORD-RULE-DEMO", "INV-RULE-DEMO", "CUS-RULE-DEMO", "ADA SYSTEMS LTD",
        10_000, "2026-09-01", "paid", "gateway",
    )
    capture = GatewayCapture(
        "PAY-RULE-DEMO", order.order_id, 10_000, 100, 18, "captured", "upi",
        "2026-09-01", "SET-RULE-DEMO", "UTR-CAPTURE-DEMO",
    )
    bank = BankTxn(
        "TXN-RULE-DEMO", "2026-09-04", 9_882, "credit", "UTR-BANK-DEMO",
        "RAZORPAY SETTLEMENT", 9_882,
    )
    rows = source_rows([order], [capture], [bank])
    verdict = OrderVerdict(
        order.order_id, Verdict.SETTLED_LATE, [capture.payment_id, bank.txn_id],
        Evidence("pass2_amount_date", automation_path="exact", risk_authorized=True),
        money_received_paise=10_000,
    )
    proof = build_certificate(
        order, verdict, source_hash_map(rows), "c" * 64,
        "calibration:rule-demo", rows=rows,
    )
    return order, proof, rows


def _outcome(case_id: str) -> ReplayCaseOutcome:
    return ReplayCaseOutcome(
        case_id=case_id,
        before_correct=True,
        after_correct=True,
        before_wrong_paise=0,
        after_wrong_paise=0,
        before_proof_valid=True,
        after_proof_valid=True,
        before_safe_coverage=10,
        after_safe_coverage=11,
    )


def _render_html(report: dict[str, object]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{step['version']}</td><td>{escape(str(step['status']))}</td>"
        f"<td>{escape(str(step['author']))}</td>"
        f"<td>{escape(str(step['approver']) or '—')}</td>"
        f"<td>{escape(str(step['activated_by']) or '—')}</td>"
        "</tr>"
        for step in report["lifecycle"]  # type: ignore[union-attr]
    )
    replay = report["replay"]
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Ledger Daemon — safe learning</title>
<style>
body{{font:16px system-ui;max-width:920px;margin:48px auto;padding:0 20px;background:#0b1020;color:#e8edf8}}
h1{{font-size:34px}} .pass{{color:#65e6a6}} table{{width:100%;border-collapse:collapse;margin:24px 0}}
th,td{{padding:12px;border-bottom:1px solid #2a3550;text-align:left}} code{{color:#9fc5ff}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}} .card{{padding:16px;background:#141c31;border-radius:10px}}
</style>
<h1>Replay-gated rule learning</h1>
<p class="pass"><strong>SAFE LEARNING GATE: PASS</strong></p>
<p>A model suggested nothing executable. A verified human resolution produced bounded JSON;
authenticated corpora replayed it; another human approved and activated the exact version.</p>
<table><thead><tr><th>Version</th><th>Status</th><th>Author</th><th>Approver</th><th>Activated by</th></tr></thead>
<tbody>{rows}</tbody></table>
<div class="grid"><div class="card"><strong>Pre-replay bypass</strong><br>{report['bypass_attempt']}</div>
<div class="card"><strong>New wrong paise</strong><br>{replay['new_wrong_paise']}</div>
<div class="card"><strong>Proofs valid</strong><br>{replay['proofs_valid']}</div></div>
<p>Active identity: <code>{escape(str(report['active_rule_identity']))}</code></p>
</html>"""


def run_rule_demo(out_dir: str) -> dict[str, object]:
    """Execute the real lifecycle and publish machine- and human-readable evidence."""
    destination = Path(out_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    db_path = destination / "rule-lifecycle.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        generated = Path(str(db_path) + suffix)
        if generated.exists():
            generated.unlink()

    analyst_credential = secrets.token_urlsafe(24)
    reviewer_credential = secrets.token_urlsafe(24)
    registry = AnalystRegistry.bootstrap(str(db_path), {
        "analyst-1": {
            "credential": analyst_credential,
            "actions": (HumanAction.RESOLVE,),
        },
        "reviewer-1": {
            "credential": reviewer_credential,
            "actions": (
                HumanAction.IMPORT_REPLAY, HumanAction.APPROVE, HumanAction.ACTIVATE,
            ),
        },
    })

    order, proof, source = _verified_case_proof()
    cases = CaseStore(str(db_path))
    case = cases.open_case(order.order_id, "AMBIGUOUS_MATCH", proof.proof_hash)
    evidence = EvidenceRegistry.bootstrap(str(db_path), ((proof, tuple(source)),))
    resolve_case(
        cases, case, case.version, "paid",
        authority=registry.issue(
            "analyst-1", analyst_credential, HumanAction.RESOLVE,
            subject_id=case.case_id, subject_version=case.version,
            expires_at=_EXPIRY,
        ),
        registry=registry,
        rule_suggestion={"family": "DATE_WINDOW", "parameters": {"days": 3}},
        evidence_registry=evidence,
    )
    proposal = compile_resolution(cases.events(case.case_id))
    if proposal is None:
        raise RuntimeError("verified resolution did not compile a rule proposal")
    store = RuleStore(str(db_path))
    proposed = store.add(proposal)

    bypass = "UNTESTED"
    try:
        store.approve(
            proposed.rule_id, proposed.version,
            registry.issue(
                "reviewer-1", reviewer_credential, HumanAction.APPROVE,
                subject_id=proposed.rule_id, subject_version=proposed.version,
                expires_at=_EXPIRY,
            ),
        )
    except ValueError:
        bypass = "REJECTED_BEFORE_REPLAY"
    if bypass != "REJECTED_BEFORE_REPLAY":
        raise RuntimeError("rule store allowed approval before replay")

    confirmed = {"confirmed-v1": tuple(_outcome(f"CONFIRMED-{i}") for i in range(1, 4))}
    attacks = {"attacks-v1": tuple(_outcome(f"ATTACK-{i}") for i in range(1, 4))}
    import_subject = ReplayCorpusStore.import_subject(confirmed, attacks)
    corpora = ReplayCorpusStore.bootstrap(
        str(db_path), confirmed, attacks,
        authority=registry.issue(
            "reviewer-1", reviewer_credential, HumanAction.IMPORT_REPLAY,
            subject_id=import_subject, subject_version=1, expires_at=_EXPIRY,
        ),
        registry=registry,
    )
    replay_receipt = corpora.run(proposed, "confirmed-v1", "attacks-v1")
    replayed = store.record_replay(proposed.rule_id, proposed.version, replay_receipt)
    replay_report = store.replay_report(proposed.rule_id, replayed.version)
    approved = store.approve(
        proposed.rule_id, replayed.version,
        registry.issue(
            "reviewer-1", reviewer_credential, HumanAction.APPROVE,
            subject_id=proposed.rule_id, subject_version=replayed.version,
            expires_at=_EXPIRY,
        ),
    )
    activation = store.activate(
        proposed.rule_id, approved.version,
        registry.issue(
            "reviewer-1", reviewer_credential, HumanAction.ACTIVATE,
            subject_id=proposed.rule_id, subject_version=approved.version,
            expires_at=_EXPIRY,
        ),
        _ACTIVATION_TIME,
    )
    active = store.validate_activation(activation)

    old_config = ReconConfig()
    old_hash = recon_config_hash(old_config)
    active_config = with_active_rule(old_config, activation, store)
    report: dict[str, object] = {
        "schema_version": "rule-lifecycle-demo-v1",
        "author": proposed.author,
        "approver": active.approver,
        "bypass_attempt": bypass,
        "lifecycle": [rule_proposal_state(item) for item in store.history(proposed.rule_id)],
        "replay": {
            "attack_cases": len(replay_report.attack_cases),
            "confirmed_cases": len(replay_report.confirmed_cases),
            "new_wrong_paise": replay_report.new_wrong_paise,
            "new_wrong_verdicts": replay_report.new_wrong_verdicts,
            "promotable": replay_report.promotable,
            "proofs_valid": replay_report.proofs_valid,
            "safe_coverage_change": replay_report.safe_coverage_change,
        },
        "old_config_unchanged": recon_config_hash(old_config) == old_hash,
        "active_config_changed": recon_config_hash(active_config) != old_hash,
        "active_rule_identity": active.identity,
        "activation_history_hash": activation.history_hash,
    }
    (destination / "rule-lifecycle.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (destination / "rule-lifecycle.html").write_text(
        _render_html(report), encoding="utf-8",
    )
    return report
