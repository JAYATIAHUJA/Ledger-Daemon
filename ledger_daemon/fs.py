"""Fellegi-Sunter probabilistic record linkage (Fellegi & Sunter 1969).

Each comparison field i has:
    m_i = P(field agrees | pair is a true match)
    u_i = P(field agrees | pair is a non-match)
and contributes a log-likelihood ratio weight:
    w_i = log2(m_i/u_i)         if it agrees
    w_i = log2((1-m_i)/(1-u_i)) if it disagrees
Total weight (plus a prior log-odds term) converts exactly to a probability:
    P(match) = 1 / (1 + 2^-total)

The per-field waterfall IS the explanation — the actual arithmetic the decision
was made with, not a post-hoc approximation.

m-priors come from the documented generator noise process (DECISIONS.md);
u-probabilities are *estimated* by sampling random pairs (the cheap unsupervised
shortcut: u_i ~= P(two random records agree on field i)) — no labels needed.

Probabilities here are floats. They never touch monetary arithmetic; every
rupee figure remains integer paise end to end.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .models import BankTxn, Order
from .money import tds_rate_bp
from .narration import invoice_in_narration
from .similarity import name_similarity

NAME_SIM_THRESHOLD = 0.85


@dataclass
class FieldWeights:
    m: float
    u: float

    @property
    def agree(self) -> float:
        return math.log2(self.m / self.u)

    @property
    def disagree(self) -> float:
        return math.log2((1.0 - self.m) / (1.0 - self.u))


@dataclass
class FSModel:
    # design-time m priors, sampled u estimates (see estimate_u)
    amount: FieldWeights = field(default_factory=lambda: FieldWeights(m=0.98, u=0.002))
    date: FieldWeights = field(default_factory=lambda: FieldWeights(m=0.95, u=0.25))
    invoice: FieldWeights = field(default_factory=lambda: FieldWeights(m=0.60, u=0.001))
    name: FieldWeights = field(default_factory=lambda: FieldWeights(m=0.85, u=0.01))
    prior_log_odds: float = -8.97  # log2(1/500) default; recomputed per batch

    def estimate_u(self, orders: list[Order], credits: list[BankTxn], samples: int = 4000) -> None:
        """u_i ~= P(two random records agree on field i), by deterministic sampling."""
        if not orders or not credits:
            return
        self.prior_log_odds = math.log2(1.0 / max(len(credits), 2))
        if len(orders) < 25 or len(credits) < 25:
            return  # sampling is meaningless on tiny pools; keep documented priors
        rng = random.Random(0)  # fixed seed: determinism (NFR-3)
        agree = {"amount": 0, "date": 0, "invoice": 0, "name": 0}
        for _ in range(samples):
            o = orders[rng.randrange(len(orders))]
            b = credits[rng.randrange(len(credits))]
            if o.amount_paise == b.amount_paise:
                agree["amount"] += 1
            if abs(_day_delta(o.due_date, b.value_date)) <= 3:
                agree["date"] += 1
            if invoice_in_narration(o.invoice_no, b.narration):
                agree["invoice"] += 1
            if name_similarity(o.customer_name, b.narration) >= NAME_SIM_THRESHOLD:
                agree["name"] += 1
        floor = 1.0 / samples  # never let u hit zero
        for fw, key in ((self.amount, "amount"), (self.date, "date"),
                        (self.invoice, "invoice"), (self.name, "name")):
            u = min(max(agree[key] / samples, floor), 0.9)
            if u < fw.m * 0.7:  # a u approaching m carries no signal; keep the prior
                fw.u = u

    def score(self, o: Order, b: BankTxn) -> tuple[float, list[tuple[str, str]]]:
        """Total log2 weight + the human-readable waterfall."""
        total = self.prior_log_odds
        waterfall = [("prior (1 in %d credits)" % round(2 ** -self.prior_log_odds), f"{self.prior_log_odds:+.2f}")]

        def apply(name: str, fw: FieldWeights, agrees: bool, note: str = "") -> None:
            nonlocal total
            w = fw.agree if agrees else fw.disagree
            total += w
            tag = "agrees" if agrees else "disagrees"
            extra = f" {note}" if note else ""
            waterfall.append((f"{name} {tag}{extra} (m={fw.m:.2f} u={fw.u:.4f})", f"{w:+.2f}"))

        rate = tds_rate_bp(o.amount_paise, b.amount_paise)
        if rate is not None:
            apply(f"amount (net of {rate // 100}% statutory TDS)", self.amount, True)
        else:
            apply("amount (exact paise)", self.amount, o.amount_paise == b.amount_paise)
        apply("date within T+0..T+3", self.date, abs(_day_delta(o.due_date, b.value_date)) <= 3)
        apply("invoice no. in narration", self.invoice, invoice_in_narration(o.invoice_no, b.narration))
        sim = name_similarity(o.customer_name, b.narration)
        apply("narration ~ customer name", self.name, sim >= NAME_SIM_THRESHOLD, f"(JW {sim:.2f})")
        return total, waterfall


def p_match(total_weight: float) -> float:
    return 1.0 / (1.0 + 2.0 ** (-total_weight))


def _day_delta(d1: str, d2: str) -> int:
    """Days d2 - d1 for YYYY-MM-DD within one year (generator stays in Aug/Sep)."""
    def ordinal(d: str) -> int:
        y, m, day = int(d[:4]), int(d[5:7]), int(d[8:10])
        days_in = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        return y * 366 + sum(days_in[: m - 1]) + day
    return ordinal(d2) - ordinal(d1)
