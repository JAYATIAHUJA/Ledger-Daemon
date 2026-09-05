"""Human resolutions can propose data-only rules, never executable policy."""

from dataclasses import replace
import json
import sqlite3

import pytest

from ledger_daemon.cases import CaseState, CaseStore, path_to
from ledger_daemon.certificates import build_certificate, source_hash_map, source_rows
from ledger_daemon.models import BankTxn, Evidence, GatewayCapture, Order, OrderVerdict, Verdict
from ledger_daemon.rules import (
    AnalystRegistry,
    ActivationReceipt,
    EvidenceRegistry,
    HumanAction,
    HumanAuthority,
    RuleFamily,
    RuleProposal,
    RuleStatus,
    RuleStore,
    StaleRuleVersion,
    compile_resolution,
)
from ledger_daemon.ui import resolve_case, rule_proposal_state

ANALYST_CREDENTIAL = "analyst-secret-123"
REVIEWER_CREDENTIAL = "reviewer-secret-456"
FUTURE_EXPIRY = "2099-01-01T00:00:00Z"


def _verified_proof(*, narration="RAZORPAY SETTLEMENT", delay_days=3):
    order = Order(
        "ORD-1", "INV-1", "CUS-1", "Ada", 10_000, "2026-09-01",
        "paid", "gateway",
    )
    capture = GatewayCapture(
        "PAY-1", order.order_id, 10_000, 100, 18, "captured", "upi",
        "2026-09-01", "SET-1", "UTR-CAPTURE",
    )
    bank = BankTxn(
        "TXN-1", f"2026-09-0{1 + delay_days}", 9_882, "credit",
        "UTR-BANK", narration, 9_882,
    )
    rows = source_rows([order], [capture], [bank])
    verdict = OrderVerdict(
        order.order_id, Verdict.SETTLED_LATE, [capture.payment_id, bank.txn_id],
        Evidence("pass2_amount_date", automation_path="exact", risk_authorized=True),
        money_received_paise=10_000,
    )
    proof = build_certificate(
        order, verdict, source_hash_map(rows), "c" * 64, "calibration:test", rows=rows)
    return proof, rows


def _registry(db_path):
    return AnalystRegistry.bootstrap(db_path, {
        "analyst-1": {
            "credential": ANALYST_CREDENTIAL,
            "actions": tuple(HumanAction),
        },
        "reviewer-1": {
            "credential": REVIEWER_CREDENTIAL,
            "actions": (HumanAction.APPROVE, HumanAction.ACTIVATE),
        },
    })


def test_authority_minting_requires_credentials_and_binds_subject_version_expiry(tmp_path):
    db_path = str(tmp_path / "authority.sqlite3")
    registry = _registry(db_path)
    with pytest.raises(ValueError, match="credential"):
        registry.issue(
            "analyst-1", "wrong-secret", HumanAction.RESOLVE,
            subject_id="CASE-1", subject_version=1, expires_at=FUTURE_EXPIRY,
        )
    capability = registry.issue(
        "analyst-1", ANALYST_CREDENTIAL, HumanAction.RESOLVE,
        subject_id="CASE-1", subject_version=1, expires_at=FUTURE_EXPIRY,
    )
    assert capability.subject_id == "CASE-1"
    assert capability.subject_version == 1
    assert capability.expires_at == FUTURE_EXPIRY
    assert capability.nonce
    with pytest.raises(ValueError, match="subject"):
        registry.verify(
            capability, HumanAction.RESOLVE,
            subject_id="CASE-2", subject_version=1,
        )
    with sqlite3.connect(db_path) as conn:
        persisted = " ".join(str(value) for row in conn.execute(
            "SELECT * FROM trusted_analysts") for value in row)
    assert ANALYST_CREDENTIAL not in persisted
    assert REVIEWER_CREDENTIAL not in persisted


def test_verified_evidence_registry_requires_real_valid_semantic_facts(tmp_path):
    db_path = str(tmp_path / "evidence.sqlite3")
    proof, rows = _verified_proof()
    certificate_id = proof.proof_hash
    evidence = EvidenceRegistry.bootstrap(db_path, ((proof, tuple(rows)),))
    assert evidence.supports(
        certificate_id, RuleFamily.DATE_WINDOW, {"days": 3})
    assert not evidence.supports(
        certificate_id, RuleFamily.DATE_WINDOW, {"days": 4})
    with pytest.raises(ValueError, match="certificate"):
        evidence.supports("f" * 64, RuleFamily.DATE_WINDOW, {"days": 3})


def test_evidence_registry_reverifies_proof_and_detects_persisted_fact_tampering(tmp_path):
    proof, rows = _verified_proof()
    db_path = str(tmp_path / "evidence-tamper.sqlite3")
    evidence = EvidenceRegistry.bootstrap(db_path, ((proof, tuple(rows)),))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE verified_evidence_v2 SET facts_json=?, evidence_hash=?",
            ('[{"type":"date_window_days","value":7}]', "f" * 64),
        )
    with pytest.raises(ValueError, match="integrity"):
        evidence.supports(proof.proof_hash, RuleFamily.DATE_WINDOW, {"days": 7})

    invalid = replace(proof, proof_hash="f" * 64)
    with pytest.raises(ValueError, match="certificate"):
        EvidenceRegistry.bootstrap(
            str(tmp_path / "invalid.sqlite3"), ((invalid, tuple(rows)),))


def test_rule_learning_refuses_unverified_or_unrelated_certificate_facts(tmp_path):
    db_path = str(tmp_path / "unverified.sqlite3")
    registry = _registry(db_path)
    cases = CaseStore(db_path)
    proof, rows = _verified_proof()
    evidence = EvidenceRegistry.bootstrap(db_path, ((proof, tuple(rows)),))
    case = cases.open_case("ORD-1", "AMBIGUOUS_MATCH", "a" * 64)
    invalid = replace(proof, proof_hash="f" * 64)
    authority = registry.issue(
        "analyst-1", ANALYST_CREDENTIAL, HumanAction.RESOLVE,
        subject_id=case.case_id, subject_version=1, expires_at=FUTURE_EXPIRY,
    )
    with pytest.raises(ValueError, match="certificate"):
        resolve_case(
            cases, case, 1, "paid", authority=authority, registry=registry,
            evidence_registry=evidence,
            rule_suggestion={"family": "DATE_WINDOW", "parameters": {"days": 3}},
        )
    with pytest.raises(ValueError, match="certificate"):
        EvidenceRegistry.bootstrap(db_path + ".invalid", ((invalid, tuple(rows)),))
    assert cases.get(case.case_id).state is CaseState.OPEN


def _resolved_events(tmp_path, *, suggestion=None, grounded=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "ledger.sqlite3")
    registry = _registry(db_path)
    store = CaseStore(db_path)
    evidence_registry = None
    if suggestion is not None:
        family = suggestion.get("family")
        parameters = suggestion.get("parameters", {})
        narration = "RAZORPAY SETTLEMENT"
        delay = 3
        if family == "NARRATION_PATTERN" and isinstance(grounded, dict):
            narration = grounded.get("pattern", narration)
        if family == "DATE_WINDOW" and isinstance(grounded, dict):
            candidate = grounded.get("days")
            if type(candidate) is int and 0 <= candidate <= 7:
                delay = candidate
        proof, rows = _verified_proof(narration=narration, delay_days=delay)
        evidence_registry = EvidenceRegistry.bootstrap(db_path, ((proof, tuple(rows)),))
        certificate_id = proof.proof_hash
    else:
        certificate_id = "a" * 64
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", certificate_id)
    resolve_case(
        store,
        case,
        1,
        "paid",
        authority=registry.issue(
            "analyst-1", ANALYST_CREDENTIAL, HumanAction.RESOLVE,
            subject_id=case.case_id, subject_version=1, expires_at=FUTURE_EXPIRY,
        ),
        registry=registry,
        rule_suggestion=suggestion,
        evidence_registry=evidence_registry,
    )
    return store.events(case.case_id)


def test_learning_requires_store_issued_human_authority(tmp_path):
    db_path = str(tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError, match="human"):
        AnalystRegistry.bootstrap(db_path, {
            "gpt": {"credential": ANALYST_CREDENTIAL, "actions": tuple(HumanAction)}})

    registry = _registry(db_path)
    with pytest.raises(ValueError, match="unknown"):
        registry.issue(
            "somebody", ANALYST_CREDENTIAL, HumanAction.RESOLVE,
            subject_id="CASE-1", subject_version=1, expires_at=FUTURE_EXPIRY)

    store = CaseStore(db_path)
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "a" * 64)
    suggestion = _narration_suggestion()
    with pytest.raises(ValueError, match="authority"):
        resolve_case(
            store, case, 1, "paid", rule_suggestion=suggestion,
            evidence_registry=None,
        )
    for untrusted_actor in ("gpt", "unknown-person"):
        with pytest.raises(ValueError, match="authority"):
            resolve_case(
                store, case, 1, "paid", actor=untrusted_actor,
                rule_suggestion=suggestion,
                evidence_registry=None,
            )
    forged = HumanAuthority(
        actor_id="analyst-1", action=HumanAction.RESOLVE,
        registry_id=registry.registry_id, subject_id=case.case_id,
        subject_version=1, nonce="f" * 64, expires_at=FUTURE_EXPIRY,
        signature="0" * 64,
    )
    with pytest.raises(ValueError, match="authority"):
        resolve_case(
            store, case, 1, "paid", authority=forged, registry=registry,
            rule_suggestion=suggestion,
            evidence_registry=None,
        )


@pytest.mark.parametrize("machine_id", [
    "gpt-4", "model-worker", "service-1", "pipeline-1", "llm-reviewer",
])
def test_registry_rejects_machine_identity_prefixes(tmp_path, machine_id):
    with pytest.raises(ValueError, match="human"):
        AnalystRegistry.bootstrap(
            str(tmp_path / f"{machine_id}.sqlite3"),
            {machine_id: {"credential": ANALYST_CREDENTIAL,
                          "actions": tuple(HumanAction)}},
        )


def test_compiler_requires_an_authenticated_case_store_event_batch(tmp_path):
    events = _resolved_events(
        tmp_path,
        suggestion=_narration_suggestion(),
        grounded={"pattern": "RAZORPAY SETTLEMENT"},
    )
    with pytest.raises(ValueError, match="authenticated"):
        compile_resolution(tuple(events))


def test_rule_store_recomputes_proposal_id_and_hash_from_case_database(tmp_path):
    events = _resolved_events(
        tmp_path,
        suggestion=_narration_suggestion(),
        grounded={"pattern": "RAZORPAY SETTLEMENT"},
    )
    proposal = compile_resolution(events)
    assert proposal is not None
    store = RuleStore(str(tmp_path / "ledger.sqlite3"))
    with pytest.raises(ValueError, match="authenticated proposal"):
        store.add(replace(proposal, rule_id="rule-" + "f" * 64))
    with pytest.raises(ValueError, match="authenticated"):
        store.add(replace(
            proposal,
            source_case_hashes=((proposal.evidence_case_ids[0], "e" * 64),),
        ))


def _narration_suggestion(pattern="RAZORPAY SETTLEMENT"):
    return {
        "family": "NARRATION_PATTERN",
        "parameters": {"pattern": pattern},
    }


def test_verified_human_resolution_compiles_a_versioned_data_rule(tmp_path):
    suggestion = _narration_suggestion()
    events = _resolved_events(
        tmp_path,
        suggestion=suggestion,
        grounded={"pattern": "RAZORPAY SETTLEMENT"},
    )

    proposal = compile_resolution(events)

    assert proposal is not None
    assert proposal.family is RuleFamily.NARRATION_PATTERN
    assert dict(proposal.parameters) == {"pattern": "RAZORPAY SETTLEMENT"}
    assert proposal.evidence_case_ids == (events[0].case_id,)
    assert proposal.status is RuleStatus.PROPOSED
    assert proposal.version == 1
    assert proposal.author == "analyst-1"
    assert proposal.approver == ""
    assert proposal.activation_time == ""
    assert proposal.source_case_hashes[0][0] == events[0].case_id
    assert len(proposal.source_case_hashes[0][1]) == 64


def test_compiler_does_not_infer_a_rule_from_free_text_or_unfinished_history(tmp_path):
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    case = store.open_case("ORD-1", "AMBIGUOUS_MATCH", "a" * 64)
    store.advance(
        case.case_id,
        1,
        path_to(CaseState.OPEN, CaseState.VERIFIED),
        "analyst-1",
        evidence_refs=("try matching settlement narrations",),
    )

    assert compile_resolution(store.events(case.case_id)) is None
    assert compile_resolution(_resolved_events(tmp_path / "other")) is None


@pytest.mark.parametrize(
    ("suggestion", "grounded"),
    [
        (
            {"family": "NARRATION_PATTERN", "parameters": {
                "pattern": "RAZORPAY", "__code__": "import os"
            }},
            {"pattern": "RAZORPAY", "__code__": "import os"},
        ),
        (_narration_suggestion("(a+)+$"), {"pattern": "(a+)+$"}),
        (_narration_suggestion("A" * 65), {"pattern": "A" * 65}),
        ({"family": "DATE_WINDOW", "parameters": {"days": 8}}, {"days": 8}),
        (
            {"family": "FEE_SCHEDULE", "parameters": {"fee_basis_points": 5001}},
            {"fee_basis_points": 5001},
        ),
        (
            {"family": "SOURCE_ALIAS", "parameters": {
                "source": "bank", "alias": "HDFC", "canonical": "hdfc", "shell": "x"
            }},
            {"source": "bank", "alias": "HDFC", "canonical": "hdfc", "shell": "x"},
        ),
    ],
)
def test_compiler_rejects_code_patterns_arbitrary_keys_and_unbounded_parameters(
        tmp_path, suggestion, grounded):
    with pytest.raises((TypeError, ValueError)):
        compile_resolution(_resolved_events(tmp_path, suggestion=suggestion, grounded=grounded))


def test_compiler_rejects_parameters_not_grounded_in_case_evidence(tmp_path):
    with pytest.raises(ValueError, match="evidence"):
        compile_resolution(_resolved_events(
            tmp_path,
            suggestion=_narration_suggestion(),
            grounded={"pattern": "UNRELATED NARRATION"},
        ))


def test_compiler_rejects_detached_model_origin_and_forged_event_content(tmp_path):
    events = _resolved_events(
        tmp_path,
        suggestion=_narration_suggestion(),
        grounded={"pattern": "RAZORPAY SETTLEMENT"},
    )
    model_metadata = dict(events[-1].metadata)
    model_metadata["origin"] = "model"
    with pytest.raises(ValueError, match="authenticated"):
        compile_resolution([*events[:-1], replace(events[-1], metadata=model_metadata)])

    forged_metadata = dict(events[-1].metadata)
    forged_metadata["certificate_id"] = "0" * 64
    with pytest.raises(ValueError, match="authenticated"):
        compile_resolution([*events[:-1], replace(events[-1], metadata=forged_metadata)])


def test_rule_json_is_canonical_versioned_and_rejects_unknown_fields(tmp_path):
    proposal = compile_resolution(_resolved_events(
        tmp_path,
        suggestion={"family": "DATE_WINDOW", "parameters": {"days": 3}},
        grounded={"days": 3},
    ))
    assert proposal is not None

    encoded = proposal.to_json()
    assert encoded == RuleProposal.from_json(encoded).to_json()
    assert json.loads(encoded)["schema_version"] == "rule-proposal-v1"
    poisoned = json.loads(encoded)
    poisoned["python"] = "eval('danger')"
    with pytest.raises(ValueError, match="schema"):
        RuleProposal.from_json(json.dumps(poisoned))


def test_store_persists_immutable_versions_and_rejects_illegal_or_stale_changes(tmp_path):
    proposal = compile_resolution(_resolved_events(
        tmp_path,
        suggestion={"family": "DATE_WINDOW", "parameters": {"days": 3}},
        grounded={"days": 3},
    ))
    assert proposal is not None
    registry = AnalystRegistry(str(tmp_path / "ledger.sqlite3"))
    store = RuleStore(str(tmp_path / "ledger.sqlite3"))
    stored = store.add(proposal)

    from ledger_daemon.replay import ReplayCaseOutcome, ReplayCorpusStore

    confirmed = {"confirmed-v1": (
        ReplayCaseOutcome("CASE-1", True, True, 0, 0, True, True, 10, 11),
    )}
    attacks = {"attacks-v1": (
        ReplayCaseOutcome("ATTACK-1", True, True, 0, 0, True, True, 10, 10),
    )}
    import_authority = registry.issue(
        "analyst-1", ANALYST_CREDENTIAL, HumanAction.IMPORT_REPLAY,
        subject_id=ReplayCorpusStore.import_subject(confirmed, attacks),
        subject_version=1, expires_at=FUTURE_EXPIRY)
    corpora = ReplayCorpusStore.bootstrap(
        store.db_path, confirmed, attacks,
        authority=import_authority, registry=registry)
    replayed = store.record_replay(
        stored.rule_id, 1, corpora.run(stored, "confirmed-v1", "attacks-v1"))
    assert replayed.status is RuleStatus.REPLAY_PASSED and replayed.version == 2

    with pytest.raises(StaleRuleVersion):
        store.approve(
            stored.rule_id, 1, registry.issue(
                "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
                subject_id=stored.rule_id, subject_version=1, expires_at=FUTURE_EXPIRY))
    approved = store.approve(
        stored.rule_id, 2, registry.issue(
            "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
            subject_id=stored.rule_id, subject_version=2, expires_at=FUTURE_EXPIRY))
    assert approved.status is RuleStatus.APPROVED and approved.version == 3
    with pytest.raises(StaleRuleVersion):
        store.approve(
            stored.rule_id, 2, registry.issue(
                "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
                subject_id=stored.rule_id, subject_version=2, expires_at=FUTURE_EXPIRY))
    with pytest.raises(ValueError, match="REPLAY_PASSED"):
        store.approve(
            stored.rule_id, 3, registry.issue(
                "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
                subject_id=stored.rule_id, subject_version=3, expires_at=FUTURE_EXPIRY))
    receipt = store.activate(
        stored.rule_id, 3, registry.issue(
            "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.ACTIVATE,
            subject_id=stored.rule_id, subject_version=3, expires_at=FUTURE_EXPIRY),
        "2026-09-05T10:00:00Z")
    assert isinstance(receipt, ActivationReceipt)
    active = store.get(stored.rule_id)
    assert active.status is RuleStatus.ACTIVE and active.version == 4
    assert active.approver == "reviewer-1"
    assert active.activated_by == "reviewer-1"
    assert active.activation_time == "2026-09-05T10:00:00Z"
    assert [item.version for item in store.history(stored.rule_id)] == [1, 2, 3, 4]
    assert store.get(stored.rule_id, 1).to_json() == stored.to_json()
    assert rule_proposal_state(active) == {
        "rule_id": active.rule_id,
        "family": "DATE_WINDOW",
        "status": "ACTIVE",
        "version": 4,
        "author": "analyst-1",
        "approver": "reviewer-1",
        "activated_by": "reviewer-1",
        "activation_time": "2026-09-05T10:00:00Z",
    }


def test_rule_cannot_activate_before_replay_and_human_approval(tmp_path):
    proposal = compile_resolution(_resolved_events(
        tmp_path,
        suggestion={"family": "DATE_WINDOW", "parameters": {"days": 3}},
        grounded={"days": 3},
    ))
    assert proposal is not None
    registry = AnalystRegistry(str(tmp_path / "ledger.sqlite3"))
    store = RuleStore(str(tmp_path / "ledger.sqlite3"))
    store.add(proposal)
    activation = registry.issue(
        "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.ACTIVATE,
        subject_id=proposal.rule_id, subject_version=1, expires_at=FUTURE_EXPIRY)
    approval = registry.issue(
        "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
        subject_id=proposal.rule_id, subject_version=1, expires_at=FUTURE_EXPIRY)
    with pytest.raises(ValueError, match="APPROVED"):
        store.activate(proposal.rule_id, 1, activation, "2026-09-05T10:00:00Z")
    with pytest.raises(ValueError, match="REPLAY_PASSED"):
        store.approve(proposal.rule_id, 1, approval)
    with pytest.raises(ValueError, match="authority"):
        store.approve(proposal.rule_id, 1, "reviewer-1")
    with pytest.raises(ValueError, match="scope"):
        store.approve(
            proposal.rule_id, 1,
            registry.issue(
                "analyst-1", ANALYST_CREDENTIAL, HumanAction.RESOLVE,
                subject_id=proposal.rule_id, subject_version=1,
                expires_at=FUTURE_EXPIRY),
        )
    with pytest.raises(ValueError, match="authority"):
        store.activate(proposal.rule_id, 1, None, "2026-09-05T10:00:00Z")
    with pytest.raises(ValueError, match="scope"):
        store.activate(
            proposal.rule_id, 1,
            registry.issue(
                "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
                subject_id=proposal.rule_id, subject_version=1,
                expires_at=FUTURE_EXPIRY),
            "2026-09-05T10:00:00Z",
        )
