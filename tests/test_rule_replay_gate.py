"""A learned rule earns promotion only through a zero-regression replay."""

from dataclasses import replace
import json
import sqlite3

import pytest

from ledger_daemon.certificates import recon_config_hash
from ledger_daemon.certificates import build_certificate, source_hash_map, source_rows
from ledger_daemon.cases import CaseStore
from ledger_daemon.models import BankTxn, Evidence, GatewayCapture, Order, OrderVerdict, Verdict
from ledger_daemon.recon import ReconConfig, with_active_rule
from ledger_daemon.replay import (
    ReplayCaseOutcome, ReplayCorpusStore, ReplayReceipt, ReplayReport, replay_rule,
)
from ledger_daemon.rules import (
    ActivationReceipt,
    AnalystRegistry,
    EvidenceRegistry,
    HumanAction,
    HumanAuthority,
    RuleFamily,
    RuleProposal,
    RuleStatus,
    RuleStore,
    compile_resolution,
)
from ledger_daemon.ui import resolve_case

ANALYST_CREDENTIAL = "analyst-secret-123"
REVIEWER_CREDENTIAL = "reviewer-secret-456"
FUTURE_EXPIRY = "2099-01-01T00:00:00Z"


def _verified_proof():
    order = Order("ORD-1", "INV-1", "CUS-1", "Ada", 10_000,
                  "2026-09-01", "paid", "gateway")
    capture = GatewayCapture(
        "PAY-1", order.order_id, 10_000, 100, 18, "captured", "upi",
        "2026-09-01", "SET-1", "UTR-CAPTURE")
    bank = BankTxn(
        "TXN-1", "2026-09-04", 9_882, "credit", "UTR-BANK",
        "RAZORPAY SETTLEMENT", 9_882)
    rows = source_rows([order], [capture], [bank])
    verdict = OrderVerdict(
        order.order_id, Verdict.SETTLED_LATE, [capture.payment_id, bank.txn_id],
        Evidence("pass2_amount_date", automation_path="exact", risk_authorized=True),
        money_received_paise=10_000)
    return build_certificate(
        order, verdict, source_hash_map(rows), "c" * 64,
        "calibration:test", rows=rows), rows


def _proposal() -> RuleProposal:
    return RuleProposal(
        rule_id="rule-" + "a" * 64,
        family=RuleFamily.DATE_WINDOW,
        parameters={"days": 3},
        evidence_case_ids=("CASE-1",),
        status=RuleStatus.PROPOSED,
        version=1,
        source_case_hashes=(("CASE-1", "b" * 64),),
        author="analyst-1",
    )


def _outcome(case_id="CASE-1", **changes) -> ReplayCaseOutcome:
    values = {
        "case_id": case_id,
        "before_correct": True,
        "after_correct": True,
        "before_wrong_paise": 0,
        "after_wrong_paise": 0,
        "before_proof_valid": True,
        "after_proof_valid": True,
        "before_safe_coverage": 10,
        "after_safe_coverage": 11,
    }
    values.update(changes)
    return ReplayCaseOutcome(**values)


def _authenticated_proposal(tmp_path):
    db_path = str(tmp_path / "ledger.sqlite3")
    registry = AnalystRegistry.bootstrap(db_path, {
        "analyst-1": {"credential": ANALYST_CREDENTIAL,
                      "actions": (HumanAction.RESOLVE,)},
        "reviewer-1": {"credential": REVIEWER_CREDENTIAL,
                       "actions": (HumanAction.APPROVE, HumanAction.ACTIVATE,
                                   HumanAction.IMPORT_REPLAY)},
    })
    case_store = CaseStore(db_path)
    proof, rows = _verified_proof()
    case = case_store.open_case("ORD-1", "AMBIGUOUS_MATCH", proof.proof_hash)
    evidence = EvidenceRegistry.bootstrap(db_path, ((proof, tuple(rows)),))
    resolve_case(
        case_store, case, 1, "paid",
        authority=registry.issue(
            "analyst-1", ANALYST_CREDENTIAL, HumanAction.RESOLVE,
            subject_id=case.case_id, subject_version=1, expires_at=FUTURE_EXPIRY,
        ),
        registry=registry,
        rule_suggestion={"family": "DATE_WINDOW", "parameters": {"days": 3}},
        evidence_registry=evidence,
    )
    proposal = compile_resolution(case_store.events(case.case_id))
    assert proposal is not None
    return proposal, RuleStore(db_path), registry


def _authenticated_replay(store, proposal, registry):
    confirmed = {"confirmed-v1": (_outcome("CONFIRMED-1"),)}
    attacks = {"attacks-v1": (_outcome("ATTACK-1"),)}
    subject = ReplayCorpusStore.import_subject(confirmed, attacks)
    authority = registry.issue(
        "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.IMPORT_REPLAY,
        subject_id=subject, subject_version=1, expires_at=FUTURE_EXPIRY,
    )
    corpora = ReplayCorpusStore.bootstrap(
        store.db_path, confirmed, attacks, authority=authority, registry=registry,
    )
    return corpora.run(proposal, "confirmed-v1", "attacks-v1")


def test_replay_corpus_import_requires_external_exact_single_use_authority(tmp_path):
    proposal, store, registry = _authenticated_proposal(tmp_path)
    confirmed = {"confirmed-v1": (_outcome("CONFIRMED-1"),)}
    attacks = {"attacks-v1": (_outcome("ATTACK-1"),)}
    subject = ReplayCorpusStore.import_subject(confirmed, attacks)
    with pytest.raises(ValueError, match="authority"):
        ReplayCorpusStore.bootstrap(
            store.db_path, confirmed, attacks, authority=None, registry=registry)
    forged = HumanAuthority(
        actor_id="reviewer-1", action=HumanAction.IMPORT_REPLAY,
        registry_id=registry.registry_id, subject_id=subject, subject_version=1,
        nonce="e" * 64, expires_at=FUTURE_EXPIRY, signature="0" * 64,
    )
    with pytest.raises(ValueError, match="authority"):
        ReplayCorpusStore.bootstrap(
            store.db_path, confirmed, attacks, authority=forged, registry=registry)
    wrong = registry.issue(
        "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.IMPORT_REPLAY,
        subject_id="replay-import:" + "f" * 64, subject_version=1,
        expires_at=FUTURE_EXPIRY,
    )
    with pytest.raises(ValueError, match="subject"):
        ReplayCorpusStore.bootstrap(
            store.db_path, confirmed, attacks, authority=wrong, registry=registry)
    authority = registry.issue(
        "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.IMPORT_REPLAY,
        subject_id=subject, subject_version=1, expires_at=FUTURE_EXPIRY,
    )
    ReplayCorpusStore.bootstrap(
        store.db_path, confirmed, attacks, authority=authority, registry=registry)
    with pytest.raises(ValueError, match="used"):
        ReplayCorpusStore.bootstrap(
            store.db_path, confirmed, attacks, authority=authority, registry=registry)


def test_replay_recomputes_a_promotable_report_from_all_supplied_cases():
    report = replay_rule(
        _proposal(),
        (_outcome("CONFIRMED-1"), _outcome("CONFIRMED-2")),
        (_outcome("ATTACK-1"),),
    )

    assert report.promotable is True
    assert report.status is RuleStatus.REPLAY_PASSED
    assert report.confirmed_case_ids == ("CONFIRMED-1", "CONFIRMED-2")
    assert report.attack_case_ids == ("ATTACK-1",)
    assert report.new_wrong_verdicts == 0
    assert report.new_wrong_paise == 0
    assert report.safe_coverage_change == 3
    assert report.proofs_valid is True


def test_replay_report_is_canonical_versioned_json_and_persisted_with_transition(tmp_path):
    proposal, store, _registry = _authenticated_proposal(tmp_path)
    report = replay_rule(proposal, (_outcome("CONFIRMED-1"),), (_outcome("ATTACK-1"),))
    encoded = report.to_json()
    payload = json.loads(encoded)
    assert payload["confirmed_cases"][0]["case_id"] == "CONFIRMED-1"
    assert payload["attack_cases"][0]["case_id"] == "ATTACK-1"
    assert ReplayReport.from_json(encoded).to_json() == encoded
    poisoned = json.loads(encoded)
    poisoned["promotable"] = False
    with pytest.raises(ValueError, match="inconsistent"):
        ReplayReport.from_json(json.dumps(poisoned))

    store.add(proposal)
    replayed = store.record_replay(
        proposal.rule_id, 1, _authenticated_replay(store, proposal, _registry))
    assert store.replay_report(proposal.rule_id, replayed.version).to_json() == encoded


def test_store_recomputes_serialized_replay_gate_instead_of_trusting_a_flag(tmp_path):
    proposal, store, _registry = _authenticated_proposal(tmp_path)
    report = replay_rule(proposal, (_outcome("CONFIRMED-1"),), (_outcome("ATTACK-1"),))
    forged = replace(report, new_wrong_paise=100, promotable=True,
                     status=RuleStatus.REPLAY_PASSED)
    store.add(proposal)
    with pytest.raises(TypeError, match="receipt"):
        store.record_replay(proposal.rule_id, 1, forged)
    assert store.get(proposal.rule_id).status is RuleStatus.PROPOSED


def test_rule_store_refuses_a_caller_built_report_even_when_internally_consistent(tmp_path):
    proposal, store, _registry = _authenticated_proposal(tmp_path)
    store.add(proposal)
    caller_report = replay_rule(
        proposal, (_outcome("CONFIRMED-1"),), (_outcome("ATTACK-1"),))
    with pytest.raises(TypeError, match="receipt"):
        store.record_replay(proposal.rule_id, 1, caller_report)
    assert store.get(proposal.rule_id).status is RuleStatus.PROPOSED


def test_replay_transition_requires_runner_authenticated_persisted_corpora(tmp_path):
    proposal, store, registry = _authenticated_proposal(tmp_path)
    store.add(proposal)
    confirmed = {"confirmed-v1": (_outcome("CONFIRMED-1"),)}
    attacks = {"attacks-v1": (_outcome("ATTACK-1"),)}
    authority = registry.issue(
        "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.IMPORT_REPLAY,
        subject_id=ReplayCorpusStore.import_subject(confirmed, attacks),
        subject_version=1, expires_at=FUTURE_EXPIRY)
    corpora = ReplayCorpusStore.bootstrap(
        store.db_path, confirmed, attacks, authority=authority, registry=registry)
    receipt = corpora.run(proposal, "confirmed-v1", "attacks-v1")
    forged = replace(receipt, signature="0" * 64)
    with pytest.raises(ValueError, match="receipt"):
        store.record_replay(proposal.rule_id, 1, forged)
    replayed = store.record_replay(proposal.rule_id, 1, receipt)
    assert replayed.status is RuleStatus.REPLAY_PASSED


def test_replay_receipt_detects_persisted_record_tampering(tmp_path):
    proposal, store, registry = _authenticated_proposal(tmp_path)
    store.add(proposal)
    receipt = _authenticated_replay(store, proposal, registry)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE replay_records SET outcome_json=? "
            "WHERE corpus_id='confirmed-v1' AND seq=1",
            (json.dumps(_outcome("CONFIRMED-1", after_wrong_paise=100).to_dict()),),
        )
    with pytest.raises(ValueError, match="receipt"):
        store.record_replay(proposal.rule_id, 1, receipt)
    assert store.get(proposal.rule_id).status is RuleStatus.PROPOSED


def test_replay_corpus_store_refuses_empty_corpus_and_handcrafted_receipt(tmp_path):
    proposal, store, registry = _authenticated_proposal(tmp_path)
    with pytest.raises(ValueError, match="confirmed"):
        ReplayCorpusStore.bootstrap(
            store.db_path, {"confirmed-v1": ()},
            {"attacks-v1": (_outcome("ATTACK-1"),)},
            authority=None, registry=registry,
        )
    fake = ReplayReceipt(
        rule_id=proposal.rule_id, rule_version=1, runner_id="f" * 64,
        confirmed_corpus_id="confirmed-v1", confirmed_corpus_hash="e" * 64,
        attack_corpus_id="attacks-v1", attack_corpus_hash="d" * 64,
        outcome_hashes=("c" * 64, "b" * 64), report_json=replay_rule(
            proposal, (_outcome("CONFIRMED-1"),), (_outcome("ATTACK-1"),)
        ).to_json(), signature="a" * 64,
    )
    store.add(proposal)
    with pytest.raises(ValueError, match="receipt"):
        store.record_replay(proposal.rule_id, 1, fake)


def test_replay_requires_nonempty_confirmed_and_attack_corpora():
    with pytest.raises(ValueError, match="confirmed"):
        replay_rule(_proposal(), (), (_outcome("ATTACK-1"),))
    with pytest.raises(ValueError, match="attack"):
        replay_rule(_proposal(), (_outcome("CONFIRMED-1"),), ())


def test_one_newly_wrong_rupee_rejects_the_proposal():
    report = replay_rule(_proposal(), (_outcome(
        after_correct=False,
        after_wrong_paise=100,
    ),), (_outcome("ATTACK-1"),))

    assert report.promotable is False
    assert report.status is RuleStatus.REJECTED
    assert report.new_wrong_verdicts == 1
    assert report.new_wrong_paise == 100


@pytest.mark.parametrize("bad_case", [
    _outcome(after_proof_valid=False),
    _outcome(before_safe_coverage=10, after_safe_coverage=9),
    _outcome(before_wrong_paise=0, after_wrong_paise=1),
])
def test_invalid_proofs_coverage_loss_or_any_new_wrong_paise_rejects(bad_case):
    report = replay_rule(_proposal(), (bad_case,), (_outcome("ATTACK-1"),))
    assert report.promotable is False
    assert report.status is RuleStatus.REJECTED


def test_attack_cases_are_not_excluded_from_the_gate():
    report = replay_rule(
        _proposal(),
        (_outcome("CONFIRMED-1"),),
        (_outcome("ATTACK-1", after_correct=False, after_wrong_paise=100),),
    )
    assert report.promotable is False
    assert report.new_wrong_paise == 100


@pytest.mark.parametrize("changes", [
    {"after_wrong_paise": 1.0},
    {"after_wrong_paise": True},
    {"after_wrong_paise": -1},
    {"after_correct": 1},
    {"case_id": ""},
])
def test_replay_outcomes_are_bounded_typed_and_integer_paise(changes):
    with pytest.raises((TypeError, ValueError)):
        _outcome(**changes)


def test_replay_rejects_duplicate_case_ids_and_non_proposed_rules():
    same = _outcome("CASE-X")
    with pytest.raises(ValueError, match="duplicate"):
        replay_rule(_proposal(), (same,), (same,))
    with pytest.raises(ValueError, match="PROPOSED"):
        replay_rule(replace(_proposal(), status=RuleStatus.REPLAY_PASSED), (same,), ())


def test_activation_receipt_binds_rule_version_without_mutating_old_config(tmp_path):
    proposal, store, registry = _authenticated_proposal(tmp_path)
    store.add(proposal)
    replayed = store.record_replay(
        proposal.rule_id, 1, _authenticated_replay(store, proposal, registry))
    approved = store.approve(
        proposal.rule_id, replayed.version,
        registry.issue(
            "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.APPROVE,
            subject_id=proposal.rule_id, subject_version=replayed.version,
            expires_at=FUTURE_EXPIRY),
    )
    receipt = store.activate(
        proposal.rule_id, approved.version,
        registry.issue(
            "reviewer-1", REVIEWER_CREDENTIAL, HumanAction.ACTIVATE,
            subject_id=proposal.rule_id, subject_version=approved.version,
            expires_at=FUTURE_EXPIRY),
        "2026-09-05T10:00:00Z",
    )
    old = ReconConfig()
    old_hash = recon_config_hash(old)

    new = with_active_rule(old, receipt, store)

    assert old.active_rule_versions == ()
    assert recon_config_hash(old) == old_hash
    assert new.active_rule_versions == (store.get(proposal.rule_id).identity,)
    assert recon_config_hash(new) != old_hash
    assert with_active_rule(new, receipt, store) == new


def test_recon_config_rejects_direct_rule_identity_injection():
    with pytest.raises(TypeError):
        ReconConfig(active_rule_versions=["rule-a@v1"])
    with pytest.raises(TypeError):
        ReconConfig(active_rule_versions=("rule-z@v1", "rule-a@v1"))
    with pytest.raises(TypeError):
        ReconConfig(active_rule_versions=("rule-a",))


def test_never_stored_active_rule_or_fabricated_receipt_cannot_change_config(tmp_path):
    proposal, store, _registry = _authenticated_proposal(tmp_path)
    fabricated = ActivationReceipt(
        rule_id=proposal.rule_id,
        rule_version=4,
        history_hash="f" * 64,
        registry_id="e" * 64,
        activated_by="reviewer-1",
        activation_time="2026-09-05T10:00:00Z",
    )
    old = ReconConfig()
    old_hash = recon_config_hash(old)
    with pytest.raises(ValueError, match="receipt"):
        with_active_rule(old, fabricated, store)
    with pytest.raises((TypeError, ValueError)):
        with_active_rule(old, replace(
            proposal, status=RuleStatus.ACTIVE, version=4, approver="reviewer-1",
            activated_by="reviewer-1", activation_time="2026-09-05T10:00:00Z"), store)
    assert recon_config_hash(old) == old_hash
