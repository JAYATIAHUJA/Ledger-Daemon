"""Evaluation harness (FR-8): three baselines vs Ledger Daemon on the same batch.

B0  naive duner        — chase every order whose books status != 'paid'
B1  gateway-only       — chase every order with no successful gateway capture
B2  recon, no gate     — reconcile, then execute on anything not proven paid
LD  Ledger Daemon      — reconcile + policy gate; only ALLOW is chased

DCPR (double-chase prevention rate): W = orders whose ground truth is in
{SETTLED_LATE, PAID_OUT_OF_BAND, REFUNDED_THEN_REPAID} and which B0 would
chase; DCPR = |W blocked by Ledger Daemon| / |W|.

The false-hold rate (genuinely-unpaid orders wrongly blocked / all
genuinely-unpaid) is ALWAYS printed next to DCPR — without it, DCPR is
gameable by blocking everything.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import policy
from .models import CHASEABLE_VERDICTS, Order, Verdict, WRONGLY_CHASEABLE
from .money import rupees_str
from .recon import ReconResult


@dataclass
class SystemOutcome:
    name: str
    chased: set[str] = field(default_factory=set)

    def wrongly_chased(self, truth: dict[str, dict]) -> set[str]:
        return {oid for oid in self.chased
                if truth.get(oid, {}).get("chaseable_bool") == "false"}


@dataclass
class EvalReport:
    seed: int
    n: int
    match_rate: float
    matched: int
    throughput: int
    q_hat: float
    q_hat_source: str
    dcpr: float
    dcpr_ci: tuple[float, float]   # bootstrap 95% CI, 1000 resamples
    w_total: int
    w_blocked: int
    false_hold_rate: float
    false_holds: int
    unpaid_total: int
    wrong_paise: dict[str, int]
    wrong_counts: dict[str, int]
    actions_emitted: int
    net_recovered_paise: int
    ld_wrong_contacts: int
    b0_wrong_contacts: int
    exceptions: list[tuple[str, str, str]]  # (order_id, predicted, reason)
    verdict_counts: dict[str, int]


def run_baselines(orders: list[Order], captures_by_order: dict[str, list],
                  result: ReconResult) -> dict[str, SystemOutcome]:
    b0 = SystemOutcome("B0 naive")
    b1 = SystemOutcome("B1 gateway-only")
    b2 = SystemOutcome("B2 recon, no gate")
    for o in orders:
        if o.status != "paid":
            b0.chased.add(o.order_id)
        caps = captures_by_order.get(o.order_id, [])
        if o.status != "paid" and not any(c.status in ("captured", "chargeback_open") for c in caps):
            b1.chased.add(o.order_id)
        v = result.verdicts[o.order_id].verdict
        if v in (Verdict.GENUINELY_UNPAID, Verdict.PARTIALLY_PAID, Verdict.AMBIGUOUS):
            b2.chased.add(o.order_id)
    return {"B0": b0, "B1": b1, "B2": b2}


def run_ledger_daemon(orders: list[Order], result: ReconResult,
                      attempts=None, contacts=None) -> tuple[SystemOutcome, dict[str, policy.Decision]]:
    ld = SystemOutcome("Ledger Daemon")
    decisions: dict[str, policy.Decision] = {}
    for o in orders:
        v = result.verdicts[o.order_id]
        d = policy.evaluate(
            o, v, "CREATE_PAYMENT_LINK",
            attempts_so_far=attempts(o.order_id) if attempts else 0,
            contacts_7d=contacts(o.order_id) if contacts else 0,
        )
        decisions[o.order_id] = d
        if d.outcome == policy.ALLOW:
            ld.chased.add(o.order_id)
    return ld, decisions


def evaluate(seed: int, orders: list[Order], captures: list, result: ReconResult,
             truth: dict[str, dict]) -> EvalReport:
    caps_by_order: dict[str, list] = {}
    for c in captures:
        caps_by_order.setdefault(c.order_id, []).append(c)

    baselines = run_baselines(orders, caps_by_order, result)
    ld, _decisions = run_ledger_daemon(orders, result)
    systems = {**baselines, "LD": ld}

    matched = sum(1 for o in orders
                  if result.verdicts[o.order_id].verdict.value == truth[o.order_id]["true_verdict"])

    # DCPR
    w = {o.order_id for o in orders
         if truth[o.order_id]["true_verdict"] in {v.value for v in WRONGLY_CHASEABLE}
         and o.order_id in baselines["B0"].chased}
    w_blocked = {oid for oid in w if oid not in ld.chased}
    dcpr = len(w_blocked) / len(w) if w else 1.0
    dcpr_ci = _bootstrap_ci(sorted(w), w_blocked)

    # false-hold — printed next to DCPR, always
    unpaid = {o.order_id for o in orders
              if truth[o.order_id]["true_verdict"] == Verdict.GENUINELY_UNPAID.value}
    false_holds = {oid for oid in unpaid if oid not in ld.chased}
    false_hold_rate = len(false_holds) / len(unpaid) if unpaid else 0.0

    amount = {o.order_id: o.amount_paise for o in orders}
    wrong_paise, wrong_counts = {}, {}
    for key, syso in systems.items():
        bad = syso.wrongly_chased(truth)
        wrong_counts[key] = len(bad)
        wrong_paise[key] = sum(amount[oid] for oid in bad)

    recovered = 0
    for oid in ld.chased:
        if truth[oid]["chaseable_bool"] == "true":
            v = result.verdicts[oid]
            chase = v.delta_due_paise or amount[oid]
            recovered += chase * 6 // 10

    exceptions = [(oid, result.verdicts[oid].verdict.value, result.verdicts[oid].reason)
                  for oid in result.exception_ids]
    verdict_counts: dict[str, int] = {}
    for v in result.verdicts.values():
        verdict_counts[v.verdict.value] = verdict_counts.get(v.verdict.value, 0) + 1

    return EvalReport(
        seed=seed, n=len(orders),
        match_rate=matched / len(orders), matched=matched,
        throughput=result.orders_per_sec,
        q_hat=result.q_hat, q_hat_source=result.q_hat_source,
        dcpr=dcpr, dcpr_ci=dcpr_ci, w_total=len(w), w_blocked=len(w_blocked),
        false_hold_rate=false_hold_rate, false_holds=len(false_holds),
        unpaid_total=len(unpaid),
        wrong_paise=wrong_paise, wrong_counts=wrong_counts,
        actions_emitted=len(ld.chased), net_recovered_paise=recovered,
        ld_wrong_contacts=wrong_counts["LD"], b0_wrong_contacts=wrong_counts["B0"],
        exceptions=exceptions, verdict_counts=verdict_counts,
    )


def _bootstrap_ci(w_ids: list[str], blocked: set[str], resamples: int = 1000,
                  seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap 95% CI on DCPR over the W population (seeded)."""
    if not w_ids:
        return (1.0, 1.0)
    rng = random.Random(seed)
    stats = []
    n = len(w_ids)
    for _ in range(resamples):
        sample = [w_ids[rng.randrange(n)] for _ in range(n)]
        stats.append(sum(1 for oid in sample if oid in blocked) / n)
    stats.sort()
    return (stats[int(0.025 * resamples)], stats[min(resamples - 1, int(0.975 * resamples))])


def render_report(r: EvalReport, date: str = "") -> str:
    lines = [
        f"LEDGER DAEMON — EVALUATION REPORT      seed={r.seed}  n={r.n}  {date}".rstrip(),
        "",
        f"Verdict accuracy (match rate) .............. {r.match_rate:.1%}  ({r.matched}/{r.n} orders)",
        f"Throughput ................................ {r.throughput:,} orders/sec",
        f"Unresolved exceptions ..................... {len(r.exceptions)}  -> eval/exceptions.md",
        f"Conformal q_hat ........................... {r.q_hat:.4f}  ({r.q_hat_source})",
        "",
        "---- Double-chase prevention " + "-" * 34,
        f"Already-paid orders a naive agent would chase:  {r.w_total}",
        f"Blocked by Ledger Daemon:                       {r.w_blocked}",
        f"DOUBLE-CHASE PREVENTION RATE:                   {r.dcpr:.1%}  "
        f"[observed {r.w_blocked}/{r.w_total}; synthetic batch]",
        "",
        f"False-hold rate (unpaid, wrongly blocked):      {r.false_hold_rate:.1%}  "
        f"({r.false_holds}/{r.unpaid_total})",
        "",
        "Rupees wrongly chased:",
        f"  B0 naive ................ {rupees_str(r.wrong_paise['B0']):>15}  ({r.wrong_counts['B0']} customers)",
        f"  B1 gateway-only ......... {rupees_str(r.wrong_paise['B1']):>15}  ({r.wrong_counts['B1']} customers)",
        f"  B2 recon, no gate ....... {rupees_str(r.wrong_paise['B2']):>15}  ({r.wrong_counts['B2']} customers)",
        f"  Ledger Daemon ........... {rupees_str(r.wrong_paise['LD']):>15}  ({r.wrong_counts['LD']} customers)",
        "",
        "---- Downstream Control Demonstration: Why Correct Reconciliation Matters " + "-" * 8,
        f"Actions emitted ........................... {r.actions_emitted}",
        f"Modeled recovery (assumed 0.6 x chased) ... {rupees_str(r.net_recovered_paise)}",
        f"Customers wrongly contacted ............... {r.ld_wrong_contacts}  (B0: {r.b0_wrong_contacts})",
        "",
        "---- Verdict distribution " + "-" * 37,
    ]
    for name in sorted(r.verdict_counts):
        lines.append(f"  {name:<24} {r.verdict_counts[name]:>4}")
    return "\n".join(lines) + "\n"


def render_exceptions(r: EvalReport) -> str:
    lines = ["# Unresolved exceptions (honest list)", "",
             "Every order the deterministic layer refused to classify, and why.",
             "These route to a human — never to the executor.", ""]
    if not r.exceptions:
        lines.append("(none)")
    for oid, predicted, reason in r.exceptions:
        lines.append(f"- **{oid}** — {predicted}: {reason}")
    return "\n".join(lines) + "\n"
