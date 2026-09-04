"""Multi-seed robustness sweep: is the headline number a lucky draw?

The committed evaluation runs one seed. A single seed cannot distinguish "this
policy works" from "this world was kind". This module re-runs the whole pipeline
-- generate, calibrate, reconcile, gate, score -- on N worlds the thresholds were
never tuned against, and reports the distribution rather than the best number.

Two properties make the sweep honest:

  * Nothing is refitted per seed by hand. q_hat is re-derived from that seed's
    own calibration batch, exactly as the demo does it, so a seed cannot be
    rescued by a threshold chosen after seeing it.
  * Calibration and evaluation seeds are asserted disjoint (see
    assert_disjoint_seeds), so the abstention threshold is never fitted on the
    batch it is scored on.

What the sweep does NOT show: every world comes from the same generator, so it
bounds sampling luck, not model risk. The authored-generator limitation stated in
the README survives this in full.
"""

from __future__ import annotations

import os
import statistics
import tempfile

from .datagen import generate, load_batch
from .evaluate import evaluate
from .money import rupees_str
from .recon import calibrate, reconcile

CAL_SEED_OFFSET = 1000


def assert_disjoint_seeds(cal_seed: int, eval_seed: int) -> None:
    """Refuse to run if the abstention threshold would be fitted on the test set.

    Structural, not a convention: a future refactor that collapses the two seeds
    fails here rather than silently reporting an inflated match rate.
    """
    if cal_seed == eval_seed:
        raise ValueError(
            f"calibration and evaluation seeds must differ (both {eval_seed}); "
            "fitting q_hat on the batch it is scored on voids the conformal guarantee"
        )


def run_seed(seed: int, n: int, profile: str = "clean") -> dict:
    """One world, end to end. Returns the metrics the sweep summarises."""
    cal_seed = seed + CAL_SEED_OFFSET
    assert_disjoint_seeds(cal_seed, seed)

    with tempfile.TemporaryDirectory(prefix="ld-sweep-") as tmp:
        cal_dir = os.path.join(tmp, "cal")
        eval_dir = os.path.join(tmp, "eval")

        generate(cal_seed, n, cal_dir, profile)
        cal = load_batch(cal_dir)
        q_hat, fs_model, _probs = calibrate(cal[0], cal[1], cal[2], cal[3])

        generate(seed, n, eval_dir, profile)
        orders, captures, bank, truth = load_batch(eval_dir)

        result = reconcile(orders, captures, bank, q_hat=q_hat, fs_model=fs_model)
        r = evaluate(seed, orders, captures, result, truth)

    return {
        "seed": seed,
        "dcpr": r.dcpr,
        "false_hold_rate": r.false_hold_rate,
        "match_rate": r.match_rate,
        "exceptions": len(r.exceptions),
        "ld_wrong_paise": r.wrong_paise["LD"],
        "b0_wrong_paise": r.wrong_paise["B0"],
        "b2_wrong_paise": r.wrong_paise["B2"],
        "q_hat": r.q_hat,
    }


def run_sweep(seeds: list[int], n: int, profile: str = "clean") -> list[dict]:
    return [run_seed(s, n, profile) for s in seeds]


def _spread(values: list[float]) -> tuple[float, float, float]:
    return (min(values), statistics.median(values), max(values))


def summarise(rows: list[dict]) -> dict:
    return {
        "dcpr": _spread([r["dcpr"] for r in rows]),
        "false_hold_rate": _spread([r["false_hold_rate"] for r in rows]),
        "match_rate": _spread([r["match_rate"] for r in rows]),
        "ld_wrong_paise": _spread([float(r["ld_wrong_paise"]) for r in rows]),
        "seeds_at_perfect_dcpr": sum(1 for r in rows if r["dcpr"] >= 1.0),
        "seeds_with_zero_wrongly_chased": sum(1 for r in rows if r["ld_wrong_paise"] == 0),
        "n_seeds": len(rows),
    }


def render(rows: list[dict], n: int, profile: str) -> str:
    s = summarise(rows)
    worst_fh = max(rows, key=lambda r: r["false_hold_rate"])
    out = [
        f"# Robustness sweep — {s['n_seeds']} unseen seeds, n={n} each, profile={profile}",
        "",
        "The committed evaluation uses one seed. These are worlds the thresholds were",
        "never tuned against; q_hat is re-derived per seed from that seed's own",
        "calibration batch.",
        "",
        "| metric | min | median | max |",
        "|---|---:|---:|---:|",
        f"| double-chase prevention | {s['dcpr'][0]:.1%} | {s['dcpr'][1]:.1%} | {s['dcpr'][2]:.1%} |",
        f"| false-hold rate | {s['false_hold_rate'][0]:.1%} | {s['false_hold_rate'][1]:.1%} | {s['false_hold_rate'][2]:.1%} |",
        f"| verdict match rate | {s['match_rate'][0]:.1%} | {s['match_rate'][1]:.1%} | {s['match_rate'][2]:.1%} |",
        f"| ₹ wrongly chased | {rupees_str(int(s['ld_wrong_paise'][0]))} | "
        f"{rupees_str(int(s['ld_wrong_paise'][1]))} | {rupees_str(int(s['ld_wrong_paise'][2]))} |",
        "",
        f"**{s['seeds_at_perfect_dcpr']}/{s['n_seeds']} seeds at 100% double-chase prevention; "
        f"{s['seeds_with_zero_wrongly_chased']}/{s['n_seeds']} seeds at ₹0 wrongly chased.**",
        "",
        "Reported rather than buried: the worst seed for false holds is "
        f"{worst_fh['seed']}, at {worst_fh['false_hold_rate']:.1%} "
        f"({worst_fh['exceptions']} unresolved exceptions).",
        "",
        "False holds are the price of R2 refusing to chase outside the bank feed's",
        "coverage window. One seed can hide that price; this table cannot.",
        "",
        "## Per-seed",
        "",
        "| seed | DCPR | false-hold | match | exceptions | q_hat | ₹ wrongly chased (LD) | (B0) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['seed']} | {r['dcpr']:.1%} | {r['false_hold_rate']:.1%} | "
            f"{r['match_rate']:.1%} | {r['exceptions']} | {r['q_hat']:.4f} | "
            f"{rupees_str(r['ld_wrong_paise'])} | {rupees_str(r['b0_wrong_paise'])} |"
        )
    out.append("")
    return "\n".join(out)
