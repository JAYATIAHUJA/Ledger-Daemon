"""Deterministic, rupee-weighted authorization for probabilistic reconciliation.

For every candidate score threshold, the controller considers the calibration
cases at or above that threshold.  Its observed loss is the wrongly resolved
paise divided by all auto-resolved paise.  To avoid treating that observed rate
as certainty, it adds a one-sided Hoeffding-shaped exposure allowance:

    ceil(10_000 * sqrt(ln(1 / (delta / m)) / (2 * n_eff))) basis points
    n_eff = floor(total_paise ** 2 / sum(amount_paise ** 2))
    delta = (10_000 - confidence_level_bp) / 10_000
    m = number of unique score thresholds searched

The effective-case count deliberately falls when a small number of invoices
dominate exposure, making the allowance larger.  This is a deterministic
operational risk bound used to authorize automation; it is not a claim that a
future production stream meets a particular statistical guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, localcontext
from hashlib import sha256
import json

from .money import paise


SCORE_PPM = 1_000_000
RISK_BP = 10_000


def _integer(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__}: {value!r}")
    return value


def _score_ppm(value: int) -> int:
    value = _integer(value, "score_ppm")
    if not 0 <= value <= SCORE_PPM:
        raise ValueError(f"score_ppm must be in 0..{SCORE_PPM}, got {value}")
    return value


def _basis_points(value: int, name: str) -> int:
    value = _integer(value, name)
    if not 0 <= value <= RISK_BP:
        raise ValueError(f"{name} must be in 0..10000")
    return value


@dataclass(frozen=True)
class RiskBudget:
    max_loss_bp: int
    min_calibration_cases: int
    confidence_level_bp: int

    def __post_init__(self) -> None:
        _validate_budget(self)


def _validate_budget(budget: RiskBudget) -> None:
    max_loss_bp = _integer(budget.max_loss_bp, "max_loss_bp")
    min_cases = _integer(budget.min_calibration_cases, "min_calibration_cases")
    confidence = _integer(budget.confidence_level_bp, "confidence_level_bp")
    if not 0 <= max_loss_bp <= RISK_BP:
        raise ValueError("max_loss_bp must be in 0..10000")
    if min_cases < 1:
        raise ValueError("min_calibration_cases must be positive")
    if not 0 < confidence < RISK_BP:
        raise ValueError("confidence_level_bp must be in 1..9999")


@dataclass(frozen=True)
class RiskCalibration:
    calibration_id: str
    threshold_ppm: int
    loss_upper_bound_bp: int
    safe_coverage_bp: int
    n: int
    authority: bool

    def __post_init__(self) -> None:
        _validate_calibration(self)


def _validate_calibration(calibration: RiskCalibration) -> None:
    if type(calibration.calibration_id) is not str:
        raise TypeError("calibration_id must be a str")
    _score_ppm(calibration.threshold_ppm)
    _basis_points(calibration.loss_upper_bound_bp, "loss_upper_bound_bp")
    _basis_points(calibration.safe_coverage_bp, "safe_coverage_bp")
    n = _integer(calibration.n, "n")
    if n < 0:
        raise ValueError("n must not be negative")
    if type(calibration.authority) is not bool:
        raise TypeError("authority must be bool")
    if calibration.authority and not calibration.calibration_id:
        raise ValueError("authorized calibration requires a calibration_id")
    if calibration.authority and n == 0:
        raise ValueError("authorized calibration requires positive n")


def _upper_bound_bp(wrong_paise: int, total_paise: int, squared_paise: int,
                    confidence_level_bp: int, threshold_count: int) -> int:
    """Return the upward-rounded deterministic rupee-loss bound in basis points."""
    observed_bp = (wrong_paise * RISK_BP + total_paise - 1) // total_paise
    effective_cases = (total_paise * total_paise) // squared_paise
    # Each selected amount is positive, so effective_cases is at least one.
    with localcontext() as context:
        context.prec = 50
        delta = Decimal(RISK_BP - confidence_level_bp) / (
            Decimal(RISK_BP) * Decimal(threshold_count))
        margin = (Decimal(RISK_BP) * ((Decimal(1) / delta).ln()
                  / (Decimal(2) * Decimal(effective_cases))).sqrt())
        margin_bp = int(margin.to_integral_value(rounding=ROUND_CEILING))
    return min(RISK_BP, observed_bp + margin_bp)


def _calibration_id(scores_ppm: list[int], labels: list[bool], amounts_paise: list[int],
                    budget: RiskBudget) -> str:
    payload = {
        "cases": sorted(zip(scores_ppm, labels, amounts_paise)),
        "budget": [budget.max_loss_bp, budget.min_calibration_cases,
                   budget.confidence_level_bp],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    return sha256(encoded).hexdigest()[:24]


def fit_rupee_risk(scores_ppm: list[int], labels: list[bool], amounts_paise: list[int],
                   budget: RiskBudget) -> RiskCalibration:
    """Fit the maximum-coverage score threshold that remains inside *budget*.

    `labels[i]` is true only when automatically resolving calibration case `i`
    would have been correct.  All money is validated as exact integer paise.
    """
    if not isinstance(budget, RiskBudget):
        raise TypeError("budget must be a RiskBudget")
    _validate_budget(budget)
    if len(scores_ppm) != len(labels) or len(scores_ppm) != len(amounts_paise):
        raise ValueError("scores_ppm, labels, and amounts_paise must have equal length")

    scores = [_score_ppm(score) for score in scores_ppm]
    checked_labels: list[bool] = []
    for label in labels:
        if type(label) is not bool:
            raise TypeError(f"labels must be bool, got {type(label).__name__}: {label!r}")
        checked_labels.append(label)
    amounts = [paise(amount) for amount in amounts_paise]
    if any(amount < 0 for amount in amounts):
        raise ValueError("amounts_paise must not be negative")

    calibration_id = _calibration_id(scores, checked_labels, amounts, budget)
    n = len(scores)
    total_value = sum(amounts)
    unavailable = RiskCalibration(calibration_id, SCORE_PPM, RISK_BP, 0, n, False)
    if n < budget.min_calibration_cases or total_value == 0:
        return unavailable

    best: RiskCalibration | None = None
    best_selected_total = -1
    threshold_count = len(set(scores))
    for threshold in sorted(set(scores), reverse=True):
        selected = [index for index, score in enumerate(scores) if score >= threshold and amounts[index] > 0]
        if len(selected) < budget.min_calibration_cases:
            continue
        selected_total = sum(amounts[index] for index in selected)
        selected_wrong = sum(amounts[index] for index in selected if not checked_labels[index])
        selected_squared = sum(amounts[index] * amounts[index] for index in selected)
        upper_bound = _upper_bound_bp(selected_wrong, selected_total, selected_squared,
                                      budget.confidence_level_bp, threshold_count)
        if upper_bound > budget.max_loss_bp:
            continue
        coverage = selected_total * RISK_BP // total_value
        candidate = RiskCalibration(calibration_id, threshold, upper_bound, coverage, n, True)
        if (selected_total > best_selected_total
                or (selected_total == best_selected_total and best is not None
                    and candidate.threshold_ppm > best.threshold_ppm)):
            best = candidate
            best_selected_total = selected_total
    return best or unavailable


def risk_authorized(score_ppm: int, amount_paise: int, calibration: RiskCalibration) -> bool:
    """Whether a new probabilistic decision may be automated under calibration."""
    score = _score_ppm(score_ppm)
    amount = paise(amount_paise)
    if amount <= 0:
        return False
    if not isinstance(calibration, RiskCalibration):
        raise TypeError("calibration must be a RiskCalibration")
    _validate_calibration(calibration)
    return calibration.authority and score >= calibration.threshold_ppm
