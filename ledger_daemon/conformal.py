"""Split conformal abstention (Vovk et al. 2005; Angelopoulos & Bates 2021).

The abstention threshold is DERIVED, not tuned: hold out a calibration set of
true-match probabilities, take the ceil((n+1)(1-alpha))/n quantile of the
nonconformity score s = 1 - P(match), and a new pair's prediction set contains
the true label with probability >= 1-alpha, distribution-free, at finite n.

Any pair whose prediction set contains both labels (or neither) is not
classifiable at the chosen error rate -> AMBIGUOUS. This is Fellegi-Sunter's
1969 "possible match / clerical review" region with a modern guarantee.

Caveat stated honestly: the guarantee assumes exchangeability between
calibration and test data. On synthetic data from one generator that holds
trivially; in production you would re-calibrate on a rolling window.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

DEFAULT_ALPHA = 0.01
FALLBACK_Q_HAT = 0.05  # used only when no calibration set exists; reported as such
SCORE_PPM = 1_000_000


def probability_to_ppm(probability: float) -> int:
    """Persist a probability score as integer parts-per-million, never a float."""
    if type(probability) not in (int, float):
        raise TypeError("probability must be numeric")
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be finite and in 0..1")
    return int((Decimal(str(probability)) * SCORE_PPM).to_integral_value(rounding=ROUND_HALF_UP))


def conformal_threshold(cal_true_match_probs: list[float], alpha: float = DEFAULT_ALPHA) -> float:
    """q_hat from nonconformity scores s = 1 - P(match) of calibration true matches."""
    scores = sorted(1.0 - p for p in cal_true_match_probs)
    n = len(scores)
    if n == 0:
        return FALLBACK_Q_HAT
    q_level = min(1.0, math.ceil((n + 1) * (1.0 - alpha)) / n)
    idx = min(n - 1, max(0, math.ceil(q_level * n) - 1))
    return scores[idx]


def decide(p: float, q_hat: float) -> str:
    """Prediction-set decision: MATCH | NON_MATCH | AMBIGUOUS."""
    in_set_match = (1.0 - p) <= q_hat
    in_set_non_match = p <= q_hat
    if in_set_match and not in_set_non_match:
        return "MATCH"
    if in_set_non_match and not in_set_match:
        return "NON_MATCH"
    return "AMBIGUOUS"  # set has 0 or 2 members -> abstain
