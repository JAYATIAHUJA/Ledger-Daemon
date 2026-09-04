"""Rupee-weighted calibration must abstain rather than hide expensive mistakes."""

import pytest

from ledger_daemon.conformal import probability_to_ppm
from ledger_daemon.money import FloatMoneyError
from ledger_daemon.risk_control import (
    RiskBudget,
    fit_rupee_risk,
    risk_authorized,
)


def test_high_value_false_match_forces_a_stricter_safe_threshold():
    """Removing the high-value mistake is necessary before the budget is met."""
    scores = list(range(999_000, 998_900, -1)) + [990_000]
    labels = [True] * 100 + [False]
    amounts = [10_000] * 100 + [5_00_000_00]

    calibration = fit_rupee_risk(scores, labels, amounts, RiskBudget(2_000, 100, 9_500))

    assert calibration.authority
    assert calibration.threshold_ppm > 990_000
    assert calibration.loss_upper_bound_bp <= 2_000
    assert calibration.safe_coverage_bp > 0


def test_brief_high_value_false_match_cannot_silently_authorize():
    calibration = fit_rupee_risk(
        [999_000, 990_000, 900_000], [True, False, True],
        [10_000, 5_00_000_00, 50_000], RiskBudget(10, 3, 9_500),
    )

    assert calibration.loss_upper_bound_bp <= 10 or not calibration.authority


@pytest.mark.parametrize(
    ("scores", "labels", "amounts", "budget"),
    [
        ([], [], [], RiskBudget(10, 1, 9_500)),
        ([999_000], [True], [10_000], RiskBudget(10, 2, 9_500)),
        ([999_000, 998_000], [True, True], [0, 0], RiskBudget(10, 2, 9_500)),
    ],
)
def test_empty_undersized_or_zero_value_calibration_has_no_authority(scores, labels, amounts, budget):
    calibration = fit_rupee_risk(scores, labels, amounts, budget)

    assert not calibration.authority
    assert calibration.safe_coverage_bp == 0
    assert not risk_authorized(999_000, 10_000, calibration)


def test_money_inputs_reject_float_and_bool_values():
    budget = RiskBudget(10, 1, 9_500)
    with pytest.raises(FloatMoneyError):
        fit_rupee_risk([999_000], [True], [10.0], budget)
    with pytest.raises(FloatMoneyError):
        fit_rupee_risk([999_000], [True], [True], budget)


def test_fit_and_authorization_are_deterministic_and_thresholded():
    budget = RiskBudget(10_000, 2, 5_000)
    first = fit_rupee_risk([900_000, 800_000], [True, True], [10_000, 20_000], budget)
    second = fit_rupee_risk([900_000, 800_000], [True, True], [10_000, 20_000], budget)

    assert first == second
    assert risk_authorized(first.threshold_ppm, 10_000, first) is first.authority
    assert not risk_authorized(first.threshold_ppm - 1, 10_000, first)
    with pytest.raises(FloatMoneyError):
        risk_authorized(first.threshold_ppm, 1.0, first)


def test_probability_scores_are_persisted_as_integer_ppm():
    assert probability_to_ppm(0.5) == 500_000
    assert probability_to_ppm(1.0) == 1_000_000
    with pytest.raises(ValueError):
        probability_to_ppm(1.01)


def test_many_searched_thresholds_spend_the_confidence_budget_simultaneously():
    """A threshold search cannot reuse a 95% tail allowance 100 times."""
    calibration = fit_rupee_risk(
        list(range(999_000, 998_900, -1)), [True] * 100, [10_000] * 100,
        RiskBudget(1_500, 100, 9_500),
    )

    assert not calibration.authority


def test_each_authorized_threshold_needs_enough_positive_value_cases():
    calibration = fit_rupee_risk(
        [999_000] + [0] * 99, [True] * 100, [10_000] + [0] * 99,
        RiskBudget(10_000, 100, 1),
    )

    assert not calibration.authority


def test_calibration_id_is_invariant_to_input_triple_order():
    budget = RiskBudget(10_000, 3, 5_000)
    first = fit_rupee_risk([900_000, 800_000, 700_000], [True, False, True],
                           [10_000, 20_000, 30_000], budget)
    reordered = fit_rupee_risk([700_000, 900_000, 800_000], [True, True, False],
                               [30_000, 10_000, 20_000], budget)

    assert first.calibration_id == reordered.calibration_id


@pytest.mark.parametrize(
    "args",
    [
        ("", 0, 0, 1, 1, True),
        ("cal-1", -1, 0, 1, 1, True),
        ("cal-1", 1_000_001, 0, 1, 1, True),
        ("cal-1", 0, -1, 1, 1, True),
        ("cal-1", 0, 0, 10_001, 1, True),
        ("cal-1", 0, 0, 1, -1, True),
        ("cal-1", 0, 0, 1, 1, 1),
    ],
)
def test_risk_calibration_rejects_malformed_authority_fields(args):
    from ledger_daemon.risk_control import RiskCalibration

    with pytest.raises((TypeError, ValueError)):
        RiskCalibration(*args)


def test_risk_authorized_revalidates_a_tampered_calibration():
    from ledger_daemon.risk_control import RiskCalibration

    calibration = RiskCalibration("cal-1", 0, 0, 1, 1, True)
    object.__setattr__(calibration, "authority", 1)

    with pytest.raises(TypeError):
        risk_authorized(999_000, 10_000, calibration)


def test_threshold_selection_uses_exact_paise_when_rounded_coverage_ties():
    calibration = fit_rupee_risk(
        [900_000, 800_000, 700_000], [True, True, False],
        [1_000_000, 1, 99_000_000], RiskBudget(8_000, 1, 1),
    )

    assert calibration.authority
    assert calibration.threshold_ppm == 800_000
    assert calibration.safe_coverage_bp == 100


def test_evidence_rejects_non_string_risk_calibration_id():
    from ledger_daemon.models import Evidence

    with pytest.raises(TypeError):
        Evidence("pass4_fuzzy", automation_path="probabilistic", risk_calibration_id=1)


def test_fit_revalidates_a_tampered_frozen_budget():
    budget = RiskBudget(10_000, 1, 5_000)
    object.__setattr__(budget, "min_calibration_cases", 0)

    with pytest.raises(ValueError):
        fit_rupee_risk([900_000], [True], [10_000], budget)
