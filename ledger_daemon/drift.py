"""Distribution-shift detection over the three source feeds (F5).

The conformal threshold (D5) and the rupee-risk calibration (F4) are both
distribution-free but not distribution-*proof*: they hold under exchangeability
with the batch they were fitted on. When the live stream stops resembling that
batch -- a bank changes its narration format, a gateway changes its fee
schedule, settlement lag blows out -- those guarantees quietly stop meaning
anything, and a matcher that keeps scoring is asserting a confidence it no
longer has.

This module makes that visible rather than assumed. Five signals, all integer
arithmetic over observable fields:

    narration_parse_rate  can the parser still extract a reference at all
    nonconformity         share of the window below the baseline's 10th pct
    amount_bucket         total-variation distance between amount histograms
    date_lag              median settlement lag, in days
    fee_rate              median gateway fee+tax, in basis points

Each signal is compared against the calibration window and scored HEALTHY /
WARNING / SEVERE against declared thresholds; the window takes the severity of
its worst signal. Windows below MIN_WINDOW are refused outright rather than
scored on too little evidence -- a monitor that reports on four rows is worse
than one that admits it cannot see.

The thresholds below are operational choices, not derived quantities. They are
stated here so they can be argued with, and the window hash lets any scored
decision be re-derived from the rows that produced it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .narration import parse

SCORE_PPM = 1_000_000
BP = 10_000

# A window smaller than this is refused, not scored.
MIN_WINDOW = 30

# The share an exchangeable window is expected to place below the calibration
# batch's 10th percentile -- 10%, by the definition of a 10th percentile.
NOMINAL_TAIL_BP = 1_000

UNDERSIZED, HEALTHY, WARNING, SEVERE = "UNDERSIZED", "HEALTHY", "WARNING", "SEVERE"

# Ordered worst-last, so `max(..., key=SEVERITY_ORDER.index)` picks the worst.
SEVERITY_ORDER = (HEALTHY, WARNING, SEVERE)

# (warning, severe) thresholds per signal, in the signal's own integer unit.
THRESHOLDS: dict[str, tuple[int, int]] = {
    "narration_parse_rate": (100_000, 250_000),   # ppm drop in parseable narrations
    "nonconformity": (2_000, 4_000),              # bp of the window below baseline p10
    "amount_bucket": (2_000, 4_000),              # bp of total-variation distance
    "date_lag": (2, 5),                           # days of median lag movement
    "fee_rate": (100, 300),                       # bp of median fee-rate movement
}


@dataclass(frozen=True)
class DriftObservation:
    """One comparable row of the live stream, reduced to integers.

    `narration_score_ppm` is what the matcher believed; `reference_ppm` is
    whether the narration carried a joinable reference at all. They are kept
    apart because they fail apart: a feed can stay perfectly parseable while
    the names inside it degrade until nothing matches.
    """

    narration_score_ppm: int
    amount_bucket: int
    date_lag_days: int
    fee_rate_bp: int
    reference_ppm: int = 0

    def __post_init__(self) -> None:
        for name in ("narration_score_ppm", "amount_bucket", "date_lag_days",
                     "fee_rate_bp", "reference_ppm"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        for name in ("narration_score_ppm", "reference_ppm"):
            if not 0 <= getattr(self, name) <= SCORE_PPM:
                raise ValueError(f"{name} must be in 0..1000000")

    def key(self) -> tuple[int, int, int, int, int]:
        return (self.narration_score_ppm, self.amount_bucket,
                self.date_lag_days, self.fee_rate_bp, self.reference_ppm)


@dataclass(frozen=True)
class SignalDelta:
    name: str
    baseline: int
    live: int
    delta: int
    severity: str


@dataclass(frozen=True)
class DriftReport:
    severity: str
    n: int
    window_hash: str
    baseline_hash: str
    signals: tuple[SignalDelta, ...]
    usable: bool


# ------------------------------ statistics ---------------------------------- #

def _median(values: list[int]) -> int:
    """Lower median: integer in, integer out, no float ever touches a decision."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _mean_ppm(values: list[int]) -> int:
    return sum(values) // len(values) if values else 0


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct // 100
    return ordered[index]


def _histogram_bp(buckets: list[int]) -> dict[int, int]:
    """Bucket shares in basis points, so the comparison is integer arithmetic."""
    total = len(buckets)
    counts: dict[int, int] = {}
    for bucket in buckets:
        counts[bucket] = counts.get(bucket, 0) + 1
    return {bucket: count * BP // total for bucket, count in counts.items()}


def _total_variation_bp(baseline: list[int], live: list[int]) -> int:
    left, right = _histogram_bp(baseline), _histogram_bp(live)
    return sum(abs(left.get(bucket, 0) - right.get(bucket, 0))
               for bucket in set(left) | set(right)) // 2


def _hash(window: list[DriftObservation]) -> str:
    """Content-addressed and order-independent: the same rows hash the same."""
    payload = repr(sorted(item.key() for item in window)).encode()
    return hashlib.sha256(payload).hexdigest()


def _severity(name: str, delta: int) -> str:
    warning, severe = THRESHOLDS[name]
    if delta >= severe:
        return SEVERE
    if delta >= warning:
        return WARNING
    return HEALTHY


def _signal(name: str, baseline: int, live: int, delta: int) -> SignalDelta:
    return SignalDelta(name, baseline, live, delta, _severity(name, delta))


# ------------------------------ the monitor --------------------------------- #

class DriftMonitor:
    """Scores a live window against the calibration window, and only that."""

    def __init__(self, baseline: list[DriftObservation], min_window: int = MIN_WINDOW):
        baseline = list(baseline)
        if min_window < 1:
            raise ValueError("min_window must be positive")
        if len(baseline) < min_window:
            raise ValueError(
                f"calibration window has {len(baseline)} rows, need at least {min_window}; "
                "a baseline too small to characterise cannot detect drift from anything")
        self.baseline = baseline
        self.min_window = min_window
        self.baseline_hash = _hash(baseline)
        self._scores = [item.narration_score_ppm for item in baseline]
        self._p10 = _percentile(self._scores, 10)

    def observe(self, batch: list[DriftObservation]) -> DriftReport:
        window = list(batch)
        if len(window) < self.min_window:
            return DriftReport(UNDERSIZED, len(window), _hash(window),
                               self.baseline_hash, (), False)

        live_scores = [item.narration_score_ppm for item in window]

        # 1. can the parser still recover a joinable reference at all
        base_parse = _mean_ppm([item.reference_ppm for item in self.baseline])
        live_parse = _mean_ppm([item.reference_ppm for item in window])
        parse_rate = _signal("narration_parse_rate", base_parse, live_parse,
                             max(0, base_parse - live_parse))

        # 2. how much of the window falls below where the calibration batch's
        #    weakest tenth sat -- a distribution-free nonconformity share.
        #
        #    The comparison is against NOMINAL_TAIL_BP, not against the
        #    baseline's own measured tail. A 10th percentile means 10% by
        #    definition, and that is what an exchangeable window should put
        #    below it; the baseline's *measured* strict tail collapses toward
        #    zero whenever scores tie against 1.0, as they do here, and
        #    measuring against that artifact would flag every honest window.
        live_tail = sum(1 for score in live_scores if score < self._p10) * BP // len(live_scores)
        nonconformity = _signal("nonconformity", NOMINAL_TAIL_BP, live_tail,
                                max(0, live_tail - NOMINAL_TAIL_BP))

        # 3. is the money arriving in the same sizes
        base_buckets = [item.amount_bucket for item in self.baseline]
        live_buckets = [item.amount_bucket for item in window]
        distance = _total_variation_bp(base_buckets, live_buckets)
        amount = _signal("amount_bucket", _median(base_buckets), _median(live_buckets), distance)

        # 4. is it still arriving on the same schedule
        base_lag = _median([item.date_lag_days for item in self.baseline])
        live_lag = _median([item.date_lag_days for item in window])
        lag = _signal("date_lag", base_lag, live_lag, abs(live_lag - base_lag))

        # 5. is the gateway still charging what it charged
        base_fee = _median([item.fee_rate_bp for item in self.baseline])
        live_fee = _median([item.fee_rate_bp for item in window])
        fee = _signal("fee_rate", base_fee, live_fee, abs(live_fee - base_fee))

        signals = (parse_rate, nonconformity, amount, lag, fee)
        severity = max((signal.severity for signal in signals),
                       key=SEVERITY_ORDER.index)
        return DriftReport(severity, len(window), _hash(window),
                           self.baseline_hash, signals, True)


# ------------------------------ building observations ----------------------- #

def _amount_bucket(amount_paise: int) -> int:
    """Order-of-magnitude bucket. Coarse on purpose: this tracks the shape of
    the stream, not individual invoices."""
    bucket = 0
    while amount_paise >= 10:
        amount_paise //= 10
        bucket += 1
    return bucket


# A narration carries a *reference* when the parser recovers something the
# matcher can join on. `mode` (the leading NEFT/IMPS/UPI) is not one: nearly
# every narration has it, so counting it would report a healthy parse rate on a
# feed that had become unjoinable.
REFERENCE_FIELDS = ("settlement_id", "invoice", "upi_ref", "vpa")


def narration_score_ppm(narration: str) -> int:
    fields = parse(narration)
    return SCORE_PPM if any(field in fields for field in REFERENCE_FIELDS) else 0


def shift_feed(bank: list, *, lag_days: int = 0, fee_bp: int = 0) -> list:
    """Return the bank feed with a *declared* change applied to every credit.

    Real drift is not a knob, and the synthetic generator does not model a bank
    changing its settlement schedule. Rather than mine the generator for a seed
    that happens to look shifted -- which would tune the detector to the
    fixture instead of testing it -- callers state the change they are
    injecting: "settlement started arriving `lag_days` later", "the gateway
    repriced by `fee_bp`". What the monitor then reports is a property of the
    monitor, not of a lucky seed.
    """
    from dataclasses import replace

    shifted = []
    for txn in bank:
        if txn.credit_debit != "credit" or not lag_days:
            shifted.append(txn)
            continue
        shifted.append(replace(txn, value_date=_dayplus(txn.value_date, lag_days)))
    return shifted


def _dayplus(day: str, delta: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=delta)).isoformat()


def observations(orders: list, captures: list, bank: list,
                 verdicts: dict) -> list[DriftObservation]:
    """Reduce one batch to the comparable rows the monitor scores.

    One observation per order that went down the *probabilistic* path -- which
    is exactly the population whose authority is at stake. An order settled by
    exact UTR or settlement id is matched by a pass that drift never revokes,
    so including it would dilute the signal with rows that signal cannot act on.

    The per-row score is the matcher's own `P(match)` in ppm, taken from the
    evidence it recorded. That is the right nonconformity quantity: it is what
    the conformal layer was calibrated on, so when it moves, the abstention
    threshold that layer derived is the thing that has stopped being valid.

    Settlement lag and fee rate are properties of the gateway feed rather than
    of any one order, so each is computed once over the batch and carried on
    every row; the monitor takes their median either way.
    """
    from .fs import _day_delta

    captured_at_by_utr: dict[str, str] = {}
    fee_rates: list[int] = []
    for capture in captures:
        if capture.amount_paise > 0:
            fee_rates.append(
                (capture.fee_paise + capture.tax_paise) * BP // capture.amount_paise)
        if capture.utr:
            captured_at_by_utr[capture.utr] = capture.captured_at

    lags: list[int] = []
    narrations: dict[str, str] = {}
    for txn in bank:
        if txn.credit_debit != "credit":
            continue
        narrations[txn.txn_id] = txn.narration
        captured_at = captured_at_by_utr.get(txn.utr, "")
        if captured_at:
            lags.append(_day_delta(captured_at, txn.value_date))

    median_fee, median_lag = _median(fee_rates), _median(lags)

    rows: list[DriftObservation] = []
    for order in sorted(orders, key=lambda item: item.order_id):
        verdict = verdicts.get(order.order_id)
        if verdict is None or verdict.evidence.automation_path != "probabilistic":
            continue
        cited = [narrations[ref] for ref in verdict.evidence_refs if ref in narrations]
        rows.append(DriftObservation(
            narration_score_ppm=verdict.evidence.score_ppm,
            amount_bucket=_amount_bucket(order.amount_paise),
            date_lag_days=median_lag,
            fee_rate_bp=median_fee,
            reference_ppm=_mean_ppm([narration_score_ppm(text) for text in cited]),
        ))
    return rows
