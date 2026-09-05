"""One command an evaluator can run, and the invariants it is allowed to fail on (F9).

The judge is the only claim-generating surface in the repo: every number that
appears in the docs is supposed to come out of here, with its dataset class,
profile, seed, sample size and command attached. A run that cannot honour an
invariant exits nonzero and still writes its report — a harness that hides its
own failure is worth less than no harness.
"""

import json
import os

import pytest

from ledger_daemon.judge import (
    HARD_INVARIANTS,
    PROFILES,
    JudgeReport,
    run_judge,
)


@pytest.fixture(scope="module")
def judged(tmp_path_factory):
    out = tmp_path_factory.mktemp("judge")
    return run_judge(42, 100, str(out)), str(out)


def test_judge_contract(judged):
    report, _out = judged
    assert isinstance(report, JudgeReport)
    assert report.wrongly_chased_paise == 0
    assert report.processed == report.reconciled + report.exceptions + report.quarantined
    assert report.duplicate_side_effects == 0


def test_every_profile_places_every_row_in_exactly_one_terminal_bucket(judged):
    report, _out = judged
    assert len(report.profiles) == len(PROFILES)
    for profile in report.profiles:
        assert profile.processed == (
            profile.reconciled + profile.exceptions + profile.quarantined), profile.profile
        assert profile.processed >= 50


def test_no_profile_chases_a_rupee_it_should_not(judged):
    report, _out = judged
    for profile in report.profiles:
        assert profile.wrongly_chased_paise == 0, profile.profile
        assert type(profile.wrongly_chased_paise) is int


def test_every_attack_is_graded_against_its_declared_oracle(judged):
    report, _out = judged
    assert report.attacks_run > 0
    for attack in report.attacks:
        assert attack.oracle
        assert attack.detail
    assert report.attacks_passed == report.attacks_run, [
        a.to_dict() for a in report.attacks if not a.passed]


def test_tampered_proofs_are_rejected_and_clean_proofs_verify(judged):
    report, _out = judged
    assert report.proofs_verified > 0
    assert report.proofs_rejected == 0
    tamper = [a for a in report.attacks if a.oracle == "proof_rejected"]
    assert tamper and all(a.passed for a in tamper)


def test_hard_invariants_are_named_and_all_hold(judged):
    report, _out = judged
    assert {i.name for i in report.invariants} == set(HARD_INVARIANTS)
    assert report.ok, [i.name for i in report.invariants if not i.held]


def test_artifacts_are_written(judged):
    _report, out = judged
    for name in ("summary.json", "cases.jsonl", "attacks.json", "latency.json",
                 "proof-manifest.json", "claims.md"):
        path = os.path.join(out, name)
        assert os.path.exists(path), name
        assert os.path.getsize(path) > 0, name


def test_summary_is_canonical_json_with_provenance(judged):
    _report, out = judged
    with open(os.path.join(out, "summary.json"), encoding="utf-8") as fh:
        body = json.load(fh)
    assert body["dataset"]["kind"] == "synthetic"
    assert body["dataset"]["seed"] == 42
    assert body["dataset"]["n"] == 100
    assert body["command"].startswith("python -m ledger_daemon judge")
    assert body["wrongly_chased_paise"] == 0
    assert body["duplicate_side_effects"] == 0
    for profile in body["profiles"]:
        assert profile["profile"] in {p.name for p in PROFILES}


def test_claims_carry_dataset_profile_seed_n_command_and_artifact(judged):
    _report, out = judged
    with open(os.path.join(out, "claims.md"), encoding="utf-8") as fh:
        claims = fh.read()
    for column in ("claim", "dataset class", "profile", "seed", "n", "command",
                   "artifact", "limitation"):
        assert column in claims
    assert "synthetic" in claims
    assert "python -m ledger_daemon judge" in claims


def test_latency_reports_percentiles_by_stage_and_end_to_end(judged):
    _report, out = judged
    with open(os.path.join(out, "latency.json"), encoding="utf-8") as fh:
        latency = json.load(fh)
    assert latency["end_to_end_us"]["p50"] > 0
    for key in ("p50", "p95", "p99"):
        assert key in latency["end_to_end_us"]
    assert set(latency["stages"]) >= {"ingest", "reconcile", "policy", "proof", "verify"}
    for stage in latency["stages"].values():
        assert stage["p99"] >= stage["p50"] >= 0
        assert stage["samples"] > 0


def test_cases_jsonl_is_one_json_object_per_line(judged):
    _report, out = judged
    with open(os.path.join(out, "cases.jsonl"), encoding="utf-8") as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]
    assert lines
    for line in lines:
        row = json.loads(line)
        assert {"profile", "case_id", "subject_id", "reason_code", "state", "version"} <= set(row)


def test_a_failing_invariant_exits_nonzero_but_still_writes_the_report(tmp_path):
    from ledger_daemon import judge as judge_module

    out = str(tmp_path / "broken")
    original = judge_module._grade_invariants

    def sabotage(report_fields, profiles, attacks):
        held = original(report_fields, profiles, attacks)
        return tuple(
            i if i.name != "zero_wrong_rupees"
            else judge_module.InvariantResult(i.name, False, "forced failure for the test")
            for i in held)

    judge_module._grade_invariants = sabotage
    try:
        report = run_judge(42, 60, out)
    finally:
        judge_module._grade_invariants = original

    assert not report.ok
    assert os.path.exists(os.path.join(out, "summary.json"))
    with open(os.path.join(out, "summary.json"), encoding="utf-8") as fh:
        assert fh.read().count("zero_wrong_rupees") >= 1


def test_run_is_deterministic_for_the_same_seed(tmp_path):
    first = run_judge(7, 60, str(tmp_path / "a"))
    second = run_judge(7, 60, str(tmp_path / "b"))
    assert first.fingerprint == second.fingerprint
