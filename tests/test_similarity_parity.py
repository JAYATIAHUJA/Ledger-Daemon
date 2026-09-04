"""Parity between the stdlib Jaro-Winkler and rapidfuzz's C++ implementation.

Guards the optional-acceleration path: whichever backend is active, scores
must agree, or the F-S weights (and everything above them) silently shift.
Skipped when rapidfuzz is not installed — the stdlib path is the contract.
"""

import random

import pytest

rapidfuzz = pytest.importorskip("rapidfuzz")

from rapidfuzz.distance.JaroWinkler import similarity as rf_jw

from ledger_daemon import similarity


def _pure_jw(s1: str, s2: str) -> float:
    """The stdlib implementation, bypassing the rapidfuzz fast path."""
    j = similarity.jaro(s1, s2)
    if j <= 0.7:  # Winkler's boost threshold
        return j
    prefix = 0
    for c1, c2 in zip(s1[:4], s2[:4]):
        if c1 == c2:
            prefix += 1
        else:
            break
    return j + prefix * 0.1 * (1.0 - j)


NAMES = ["KR ENTERPRISES", "K R ENTERPRIS", "SHARMA TEXTILES PVT LTD", "SHARMATEXTILESPVT",
         "GUPTA FOODS", "GUPTA FOODS LLP", "MEHTA EXPORTS", "MEHTAEXPORT", "", "A",
         "BOSE PACKAGING", "BOSEPACKAGIN", "IYER LOGISTICS AND SONS"]


def test_stdlib_matches_rapidfuzz_on_name_pairs():
    for a in NAMES:
        for b in NAMES:
            assert _pure_jw(a, b) == pytest.approx(rf_jw(a, b), abs=1e-9), (a, b)


def test_parity_on_random_strings():
    rng = random.Random(7)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ "
    for _ in range(500):
        a = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 20)))
        b = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 20)))
        assert _pure_jw(a, b) == pytest.approx(rf_jw(a, b), abs=1e-9), (a, b)
