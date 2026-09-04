"""The sweep must be honest by construction, not just by convention."""

import pytest

from ledger_daemon import robustness
from ledger_daemon.robustness import assert_disjoint_seeds, run_seed, summarise


def test_calibration_may_never_be_fitted_on_the_evaluation_batch():
    assert_disjoint_seeds(1042, 42)  # the offset the pipeline actually uses
    with pytest.raises(ValueError, match="must differ"):
        assert_disjoint_seeds(42, 42)


def test_the_pipeline_offset_keeps_the_seeds_disjoint_for_every_seed():
    for seed in (0, 1, 42, 1000, -7):
        assert_disjoint_seeds(seed + robustness.CAL_SEED_OFFSET, seed)


def test_a_sweep_seed_is_reproducible():
    a = run_seed(7, 120)
    b = run_seed(7, 120)
    assert a == b, "the same seed must produce byte-identical metrics"


def test_different_seeds_are_genuinely_different_worlds():
    assert run_seed(7, 120) != run_seed(8, 120)


def test_the_invariant_holds_on_seeds_the_thresholds_never_saw():
    """The claim under test: no already-paid customer is chased, on unseen worlds."""
    for seed in (201, 202, 203):
        r = run_seed(seed, 200)
        assert r["dcpr"] == 1.0, f"seed {seed} chased an already-paid order"
        assert r["ld_wrong_paise"] == 0, f"seed {seed} wrongly chased money"


def test_summary_reports_the_spread_not_the_best_number():
    rows = [
        {"dcpr": 1.0, "false_hold_rate": 0.02, "match_rate": 0.99, "ld_wrong_paise": 0},
        {"dcpr": 0.9, "false_hold_rate": 0.06, "match_rate": 0.97, "ld_wrong_paise": 500},
        {"dcpr": 1.0, "false_hold_rate": 0.04, "match_rate": 0.98, "ld_wrong_paise": 0},
    ]
    s = summarise(rows)
    assert s["dcpr"] == (0.9, 1.0, 1.0)
    assert s["false_hold_rate"] == (0.02, 0.04, 0.06)
    assert s["seeds_at_perfect_dcpr"] == 2
    assert s["seeds_with_zero_wrongly_chased"] == 2
    assert s["n_seeds"] == 3
