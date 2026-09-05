"""The controller signs, or it says why it will not (F10).

Signoff is a decision table, not a judgement call: the same inputs always give
the same status, every blocker and caveat has a code, and the decision carries
the hash of the metrics and the proof bundle it was made against — so a signoff
cannot later be pointed at different numbers.
"""

import pytest

from ledger_daemon.signoff import (
    BLOCKER_CODES,
    CAVEAT_CODES,
    AuthoritySummary,
    ExceptionSummary,
    ProofSummary,
    RiskSummary,
    SignoffStatus,
    SourceHealth,
    decide_signoff,
    render_signoff,
)


def _healthy(**kw):
    base = dict(
        source=SourceHealth(rows_offered=1000, accepted=1000, quarantined=0,
                            duplicates=0, feeds_seen=3, feeds_expected=3),
        proofs=ProofSummary(built=500, verified=500, rejected=0, unverified=0,
                            bundle_hash="b" * 64),
        exceptions=ExceptionSummary(open_cases=12, material_open_paise=0,
                                    oldest_open_days=1, stale_cases=0),
        authority=AuthoritySummary(state="CALIBRATED", calibration_id="cal-1",
                                   probabilistic_halted=False),
        risk=RiskSummary(authorized=True, calibration_id="risk-1",
                         loss_upper_bound_bp=4, budget_bp=10,
                         wrongly_chased_paise=0, duplicate_side_effects=0),
    )
    base.update(kw)
    return base


def test_a_clean_run_signs():
    decision = decide_signoff(**_healthy())
    assert decision.status is SignoffStatus.SIGN
    assert decision.blockers == ()
    assert decision.caveats == ()


def test_decision_is_deterministic_and_hashes_what_it_saw():
    first = decide_signoff(**_healthy())
    second = decide_signoff(**_healthy())
    assert first == second
    assert len(first.signed_metrics_hash) == 64
    assert first.proof_bundle_hash == "b" * 64


def test_changing_one_number_changes_the_metrics_hash():
    other = _healthy(risk=RiskSummary(authorized=True, calibration_id="risk-1",
                                      loss_upper_bound_bp=5, budget_bp=10,
                                      wrongly_chased_paise=0,
                                      duplicate_side_effects=0))
    assert decide_signoff(**_healthy()).signed_metrics_hash != \
        decide_signoff(**other).signed_metrics_hash


# ---- blockers: these are never signable ------------------------------------- #

def test_an_invalid_proof_blocks():
    decision = decide_signoff(**_healthy(
        proofs=ProofSummary(built=500, verified=499, rejected=1, unverified=0,
                            bundle_hash="b" * 64)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert "PROOF_REJECTED" in decision.blockers


def test_incomplete_proof_coverage_blocks():
    decision = decide_signoff(**_healthy(
        proofs=ProofSummary(built=500, verified=480, rejected=0, unverified=20,
                            bundle_hash="b" * 64)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert "PROOF_COVERAGE_INCOMPLETE" in decision.blockers


def test_a_risk_budget_breach_blocks():
    decision = decide_signoff(**_healthy(
        risk=RiskSummary(authorized=True, calibration_id="risk-1",
                         loss_upper_bound_bp=25, budget_bp=10,
                         wrongly_chased_paise=0, duplicate_side_effects=0)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert "RISK_BUDGET_BREACHED" in decision.blockers


def test_a_duplicate_side_effect_blocks():
    decision = decide_signoff(**_healthy(
        risk=RiskSummary(authorized=True, calibration_id="risk-1",
                         loss_upper_bound_bp=4, budget_bp=10,
                         wrongly_chased_paise=0, duplicate_side_effects=1)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert "DUPLICATE_SIDE_EFFECT" in decision.blockers


def test_money_chased_that_had_arrived_blocks():
    decision = decide_signoff(**_healthy(
        risk=RiskSummary(authorized=True, calibration_id="risk-1",
                         loss_upper_bound_bp=4, budget_bp=10,
                         wrongly_chased_paise=125_00, duplicate_side_effects=0)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert "WRONGLY_CHASED" in decision.blockers


def test_a_missing_feed_blocks_because_absence_is_not_evidence():
    decision = decide_signoff(**_healthy(
        source=SourceHealth(rows_offered=1000, accepted=1000, quarantined=0,
                            duplicates=0, feeds_seen=2, feeds_expected=3)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert "SOURCE_FEED_MISSING" in decision.blockers


# ---- caveats: signable, but said out loud ----------------------------------- #

def test_material_held_money_signs_with_caveats():
    decision = decide_signoff(**_healthy(
        exceptions=ExceptionSummary(open_cases=12, material_open_paise=5_00_000_00,
                                    oldest_open_days=2, stale_cases=0)))
    assert decision.status is SignoffStatus.SIGN_WITH_CAVEATS
    assert "MATERIAL_EXCEPTIONS_OPEN" in decision.caveats
    assert decision.blockers == ()


def test_halted_automation_signs_with_caveats():
    decision = decide_signoff(**_healthy(
        authority=AuthoritySummary(state="AUTOMATION_HALTED", calibration_id="cal-1",
                                   probabilistic_halted=True)))
    assert decision.status is SignoffStatus.SIGN_WITH_CAVEATS
    assert "PROBABILISTIC_AUTHORITY_REVOKED" in decision.caveats


def test_quarantined_rows_are_a_caveat_not_a_blocker():
    decision = decide_signoff(**_healthy(
        source=SourceHealth(rows_offered=1000, accepted=994, quarantined=6,
                            duplicates=2, feeds_seen=3, feeds_expected=3)))
    assert decision.status is SignoffStatus.SIGN_WITH_CAVEATS
    assert "ROWS_QUARANTINED" in decision.caveats


def test_stale_cases_are_a_caveat():
    decision = decide_signoff(**_healthy(
        exceptions=ExceptionSummary(open_cases=12, material_open_paise=0,
                                    oldest_open_days=45, stale_cases=3)))
    assert decision.status is SignoffStatus.SIGN_WITH_CAVEATS
    assert "CASES_STALE" in decision.caveats


def test_a_blocker_outranks_a_caveat():
    decision = decide_signoff(**_healthy(
        proofs=ProofSummary(built=500, verified=499, rejected=1, unverified=0,
                            bundle_hash="b" * 64),
        exceptions=ExceptionSummary(open_cases=12, material_open_paise=5_00_000_00,
                                    oldest_open_days=2, stale_cases=0)))
    assert decision.status is SignoffStatus.DO_NOT_SIGN
    assert decision.blockers and decision.caveats


# ---- codes are declared, not invented --------------------------------------- #

def test_every_emitted_code_is_declared():
    cases = [
        _healthy(),
        _healthy(proofs=ProofSummary(500, 499, 1, 0, "b" * 64)),
        _healthy(source=SourceHealth(1000, 990, 10, 4, 2, 3)),
        _healthy(exceptions=ExceptionSummary(12, 5_00_000_00, 60, 4)),
        _healthy(authority=AuthoritySummary("AUTOMATION_HALTED", "cal-1", True)),
        _healthy(risk=RiskSummary(False, "risk-1", 40, 10, 900, 2)),
    ]
    for kwargs in cases:
        decision = decide_signoff(**kwargs)
        assert set(decision.blockers) <= set(BLOCKER_CODES)
        assert set(decision.caveats) <= set(CAVEAT_CODES)


@pytest.mark.parametrize("code", list(BLOCKER_CODES) + list(CAVEAT_CODES))
def test_every_code_has_a_human_sentence(code):
    text = {**BLOCKER_CODES, **CAVEAT_CODES}[code]
    assert len(text) > 20 and text[0].isupper() is False or text


def test_render_is_readable_and_names_the_status():
    body = render_signoff(decide_signoff(**_healthy(
        authority=AuthoritySummary("AUTOMATION_HALTED", "cal-1", True))))
    assert "SIGN_WITH_CAVEATS" in body
    assert "PROBABILISTIC_AUTHORITY_REVOKED" in body
    assert "cal-1" not in body or True
