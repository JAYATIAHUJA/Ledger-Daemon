"""Deterministic three-way reconciliation (FR-2). Zero LLM anywhere on this path.

Pass 1  exact UTR join (gateway capture -> bank row)
Pass 2  net-of-fee amount + T+0..T+3 date-window join
Pass 3  split-settlement aggregation: sum(captures sharing settlement_id) -> one credit
Pass 4  Fellegi-Sunter scored narration match — ONLY where amount already agrees —
        resolved by component-wise OPTIMAL assignment (no greedy collisions),
        with conformal abstention and cost-sensitive probability floors.

Exactly one Verdict per order (FR-2.5). Ties within 5 weight points -> AMBIGUOUS,
never pick the higher score (FR-2.6). Duplicate-UTR evidence -> AMBIGUOUS (AC-7).
All monetary arithmetic is integer paise (FR-2.7); probabilities are floats and
never touch money. Every verdict carries evidence_refs (FR-2.8).
"""

from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass, field, replace

from . import conformal as cf
from .fs import FSModel, _day_delta, p_match
from .finance_events import Dispute, FinanceEvent
from .models import BankTxn, Evidence, GatewayCapture, Order, OrderVerdict, Verdict
from .money import STATUTORY_TDS_RATES_BP, add, pct_bp, sub, tds_rate_bp
from .narration import parse
from .authority import AuthorityState, probabilistic_authorized
from .risk_control import RiskCalibration, risk_authorized

TIE_MARGIN_POINTS = 5.0
MAX_COMPONENT_EDGES = 64
COST_WRONG_CHASE_PAISE = 800_00       # relationship + support-ticket cost per wrong chase
RECOVERY_RATE_NUM, RECOVERY_RATE_DEN = 6, 10   # 0.6 x amount recoverable, integer ratio


@dataclass(frozen=True)
class ReconConfig:
    """Ablation switches. Defaults = the full system. Each flag removes exactly
    one idea so the ablation table can show every component earning its place."""
    simple_scores: bool = False   # True: plain name-similarity 0..100 @ 82, no F-S
    greedy: bool = False          # True: per-order argmax, collisions allowed
    dup_utr_check: bool = True
    use_conformal: bool = True    # False: hard P(match) >= 0.5 cut
    use_cost_floor: bool = True
    risk_calibration: RiskCalibration | None = None
    # Drift authority in force for this run. None means the monitor never ran,
    # which is not the same as "authorized" -- policy treats an unstated
    # authority as unrevoked, and drift-demo/cli always states it.
    authority: AuthorityState | None = None
    # Data-only learned rule identities.  Copy-on-write activation preserves
    # the hash of every previously issued configuration and certificate.
    active_rule_versions: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        identities = self.active_rule_versions
        if not isinstance(identities, tuple):
            raise TypeError("active rule versions must be an immutable tuple")
        if tuple(sorted(set(identities))) != identities:
            raise ValueError("active rule versions must be unique and canonically sorted")
        identity_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}@v[1-9][0-9]*")
        if any(not isinstance(value, str) or identity_pattern.fullmatch(value) is None
               for value in identities):
            raise ValueError("invalid active rule identity")


def with_active_rule(config: "ReconConfig", receipt: object,
                     store: object) -> "ReconConfig":
    """Bind a store-verified activation receipt into a copied config."""
    from .rules import RuleStore

    if not isinstance(config, ReconConfig):
        raise TypeError("activation requires a ReconConfig")
    if not isinstance(store, RuleStore):
        raise TypeError("activation requires the issuing RuleStore")
    active = store.validate_activation(receipt)
    identities = tuple(sorted(set(config.active_rule_versions + (active.identity,))))
    updated = replace(config)
    object.__setattr__(updated, "active_rule_versions", identities)
    return updated


FULL = ReconConfig()


@dataclass
class ReconResult:
    verdicts: dict[str, OrderVerdict]
    q_hat: float
    q_hat_source: str
    elapsed_s: float
    orders_per_sec: int
    fs_model: FSModel = None
    exception_ids: list[str] = field(default_factory=list)


def net_of(c: GatewayCapture) -> int:
    return sub(sub(c.amount_paise, c.fee_paise), c.tax_paise)


def cost_sensitive_floor(amount_paise: int) -> float:
    """Elkan (2001) Bayes-optimal threshold under asymmetric cost.

    Declaring PAID blocks the chase; if wrong we lose the recoverable value
    (0.6 x amount). Chasing wrongly costs a fixed relationship/support cost.
    Block (declare paid) only when p >= C_missed / (C_missed + C_wrong_chase).
    """
    c_missed = amount_paise * RECOVERY_RATE_NUM // RECOVERY_RATE_DEN
    return c_missed / (c_missed + COST_WRONG_CHASE_PAISE)


# --------------------------------------------------------------------------- #
# candidate scoring + component-wise optimal assignment (Kuhn 1955 objective, #
# solved exactly by enumeration — components here are tiny by construction)   #
# --------------------------------------------------------------------------- #

def _components(edges: dict[str, list[tuple[str, float, list]]]) -> list[tuple[list[str], list[str]]]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for oid, cands in edges.items():
        for txn_id, _, _ in cands:
            union("O:" + oid, "T:" + txn_id)
    groups: dict[str, tuple[list[str], list[str]]] = {}
    for oid in edges:
        root = find("O:" + oid)
        groups.setdefault(root, ([], []))[0].append(oid)
    seen_t: set[str] = set()
    for oid, cands in edges.items():
        for txn_id, _, _ in cands:
            if txn_id not in seen_t:
                seen_t.add(txn_id)
                groups[find("T:" + txn_id)][1].append(txn_id)
    return [(sorted(o), sorted(t)) for o, t in groups.values()]


def _assign_component(order_ids: list[str], edges: dict[str, list[tuple[str, float, list]]]
                      ) -> dict[str, tuple[str | None, bool]]:
    """Enumerate every feasible assignment (each order -> one credit or None,
    credits unique). Returns {order_id: (chosen_txn or None, ambiguous_tie)}.

    ambiguous_tie is True when assignments within TIE_MARGIN_POINTS of the
    optimum disagree about this order — the FR-2.6 rule, applied to the global
    optimum rather than per-row greed.
    """
    weight = {(o, t): w for o in order_ids for t, w, _ in edges[o]}
    options = [[None] + [t for t, _, _ in edges[o]] for o in order_ids]
    best_total = float("-inf")
    scored: list[tuple[float, tuple]] = []
    for combo in itertools.product(*options):
        chosen = [t for t in combo if t is not None]
        if len(chosen) != len(set(chosen)):
            continue
        total = sum(weight[(o, t)] for o, t in zip(order_ids, combo) if t is not None)
        scored.append((total, combo))
        if total > best_total:
            best_total = total
    near = [combo for total, combo in scored if total >= best_total - TIE_MARGIN_POINTS]
    best_combo = max(scored, key=lambda x: x[0])[1]
    out: dict[str, tuple[str | None, bool]] = {}
    for idx, o in enumerate(order_ids):
        choices = {combo[idx] for combo in near}
        out[o] = (best_combo[idx], len(choices) > 1)
    return out


# --------------------------------------------------------------------------- #
# the engine                                                                  #
# --------------------------------------------------------------------------- #

def reconcile(orders: list[Order], captures: list[GatewayCapture], bank: list[BankTxn],
              q_hat: float | None = None, fs_model: FSModel | None = None,
              config: ReconConfig = FULL,
              finance_events: list[FinanceEvent] | tuple[FinanceEvent, ...] = ()) -> ReconResult:
    t0 = time.perf_counter()
    q_source = "calibrated" if q_hat is not None else f"fallback ({cf.FALLBACK_Q_HAT})"
    if q_hat is None:
        q_hat = cf.FALLBACK_Q_HAT

    caps_by_order: dict[str, list[GatewayCapture]] = {}
    for c in captures:
        caps_by_order.setdefault(c.order_id, []).append(c)

    open_disputes: dict[str, list[str]] = {}
    for event in finance_events:
        if isinstance(event, Dispute) and event.status == "open":
            open_disputes.setdefault(event.order_id, []).append(event.dispute_id)

    credits = [b for b in bank if b.credit_debit == "credit"]
    coverage_end = max((b.value_date for b in bank), default="0000-00-00")
    utr_counts: dict[str, int] = {}
    for b in bank:
        if b.utr:
            utr_counts[b.utr] = utr_counts.get(b.utr, 0) + 1

    # ---- passes 1 & 3: settlement groups -> exact UTR / settlement-id join ----
    settle_groups: dict[str, list[GatewayCapture]] = {}
    for c in captures:
        if c.settlement_id:
            settle_groups.setdefault(c.settlement_id, []).append(c)

    credit_by_sid: dict[str, BankTxn] = {}
    credit_by_utr: dict[str, list[BankTxn]] = {}
    for b in credits:
        sid = parse(b.narration).get("settlement_id")
        if sid:
            credit_by_sid[sid] = b
        if b.utr:
            credit_by_utr.setdefault(b.utr, []).append(b)

    consumed: set[str] = set()
    settlement_hit: dict[str, tuple[BankTxn, str]] = {}   # sid -> (credit, pass name)
    for sid, group in settle_groups.items():
        expected = 0
        for c in group:
            expected = add(expected, net_of(c))
        hit = None
        used_pass = ""
        b = credit_by_sid.get(sid)
        if b is not None and b.amount_paise == expected:
            hit, used_pass = b, "pass3_settlement_id"
        else:  # pass 1: exact UTR join
            for c in group:
                for cand in credit_by_utr.get(c.utr, []):
                    if cand.amount_paise == expected:
                        hit, used_pass = cand, "pass1_exact_utr"
                        break
                if hit:
                    break
        if hit is None and expected != 0:
            # pass 2: net amount + value_date within T+0..T+3 of latest capture
            latest = max(c.captured_at for c in group)
            for cand in credits:
                if cand.txn_id in consumed or cand.amount_paise != expected:
                    continue
                if 0 <= _day_delta(latest, cand.value_date) <= 3:
                    hit, used_pass = cand, "pass2_amount_date"
                    break
        if hit is not None:
            settlement_hit[sid] = (hit, used_pass)
            consumed.add(hit.txn_id)

    # ---- pass 4 pool: orders with no capture evidence + refund-net-zero orders ----
    fuzzy_orders: list[Order] = []
    refund_net_zero: set[str] = set()
    for o in orders:
        caps = caps_by_order.get(o.order_id, [])
        live = [c for c in caps if c.status in ("captured", "refund")]
        if not caps:
            fuzzy_orders.append(o)
        elif live and all(c.status in ("captured", "refund") for c in caps):
            total_net = 0
            for c in live:
                total_net = add(total_net, net_of(c))
            if total_net == 0 and any(c.status == "refund" for c in live):
                refund_net_zero.add(o.order_id)
                fuzzy_orders.append(o)

    fuzzy_pool = [b for b in credits if b.txn_id not in consumed
                  and "RAZORPAYSETTLEMENT" not in b.narration]

    model = fs_model or FSModel()
    if fs_model is None:
        model.estimate_u([o for o in orders], fuzzy_pool)

    edges: dict[str, list[tuple[str, float, list]]] = {}
    txn_by_id = {b.txn_id: b for b in fuzzy_pool}
    pool_by_amount: dict[int, list[BankTxn]] = {}
    for b in fuzzy_pool:
        pool_by_amount.setdefault(b.amount_paise, []).append(b)
    from .similarity import name_similarity
    for o in fuzzy_orders:
        cands = []
        # amount gate (FR-2.4): exact paise, plus the three statutory TDS nets —
        # a B2B payer who withheld 1/2/10% must still become a candidate
        candidate_txns = list(pool_by_amount.get(o.amount_paise, []))
        for bp in STATUTORY_TDS_RATES_BP:
            candidate_txns += pool_by_amount.get(sub(o.amount_paise, pct_bp(o.amount_paise, bp)), [])
        for b in candidate_txns:
            if config.simple_scores:
                sim = name_similarity(o.customer_name, b.narration)
                cands.append((b.txn_id, sim * 100.0,
                              [("plain name-similarity x100 (ablation baseline)", f"{sim * 100.0:+.1f}")]))
            else:
                w, waterfall = model.score(o, b)
                cands.append((b.txn_id, w, waterfall))
        edges[o.order_id] = cands

    assignment: dict[str, tuple[str | None, bool]] = {}
    if config.greedy:
        for oid, cands in edges.items():  # per-order argmax; collisions allowed
            best = max(cands, key=lambda x: x[1], default=None)
            assignment[oid] = (best[0] if best else None, False)
    else:
        for order_ids, txn_ids in _components(edges):
            n_edges = sum(len(edges[o]) for o in order_ids)
            if n_edges > MAX_COMPONENT_EDGES:
                for o in order_ids:  # too entangled to prove — honest abstention
                    assignment[o] = (None, True)
                continue
            assignment.update(_assign_component(order_ids, edges))

    # ---- verdict resolution: flat decision table ----
    verdicts: dict[str, OrderVerdict] = {}
    order_by_id = {o.order_id: o for o in orders}

    for o in orders:
        caps = caps_by_order.get(o.order_id, [])
        v = _resolve(o, caps, settlement_hit, assignment, edges, txn_by_id,
                     utr_counts, q_hat, coverage_end, o.order_id in refund_net_zero,
                     config, open_disputes.get(o.order_id, []))
        verdicts[o.order_id] = v

    elapsed = time.perf_counter() - t0
    ops = int(len(orders) / elapsed) if elapsed > 0 else 0
    exceptions = sorted(oid for oid, v in verdicts.items() if v.verdict is Verdict.AMBIGUOUS)
    return ReconResult(verdicts=verdicts, q_hat=q_hat, q_hat_source=q_source,
                       elapsed_s=elapsed, orders_per_sec=ops, fs_model=model,
                       exception_ids=exceptions)


def _resolve(o: Order, caps: list[GatewayCapture],
             settlement_hit: dict, assignment: dict, edges: dict, txn_by_id: dict,
             utr_counts: dict, q_hat: float, coverage_end: str,
             is_refund_net_zero: bool, config: ReconConfig = FULL,
             open_dispute_refs: list[str] | None = None) -> OrderVerdict:
    def mk(verdict, ev, **kw):
        return OrderVerdict(order_id=o.order_id, verdict=verdict,
                            evidence_refs=ev.source_rows, evidence=ev, **kw)

    # A typed open dispute trumps payment evidence: freeze and escalate.
    if open_dispute_refs:
        ev = Evidence("finance_event_dispute", sorted(open_dispute_refs),
                      "open dispute in typed finance-event feed",
                      automation_path="exact", risk_authorized=True)
        return mk(Verdict.CHARGEBACK_OPEN, ev, money_received_paise=o.amount_paise,
                  reason="disputed funds — freeze and escalate, never dun")

    # chargeback trumps everything: freeze, escalate
    cb = [c for c in caps if c.status == "chargeback_open"]
    if cb:
        ev = Evidence("gateway_status", [c.payment_id for c in cb], "chargeback open on gateway",
                      automation_path="exact", risk_authorized=True)
        return mk(Verdict.CHARGEBACK_OPEN, ev, money_received_paise=o.amount_paise,
                  reason="disputed funds — freeze and escalate, never dun")

    captured = [c for c in caps if c.status == "captured"]
    refunds = [c for c in caps if c.status == "refund"]
    failed = [c for c in caps if c.status == "failed"]

    if captured and not is_refund_net_zero:
        sid = captured[0].settlement_id
        hit = settlement_hit.get(sid)
        if hit is not None:
            credit, used_pass = hit
            latest_capture = max(c.captured_at for c in captured)
            delta = _day_delta(latest_capture, credit.value_date)
            refs = [c.payment_id for c in captured + refunds] + [credit.txn_id]
            detail = (f"settlement {sid}: sum(net) = {credit.amount_paise} paise "
                      f"= bank credit {credit.txn_id} (T+{delta})")
            ev = Evidence(used_pass, refs, detail, automation_path="exact", risk_authorized=True)
            if refunds:
                refunded = 0
                for r in refunds:
                    refunded = add(refunded, -r.amount_paise)
                return mk(Verdict.PARTIALLY_PAID, ev,
                          money_received_paise=sub(o.amount_paise, refunded),
                          delta_due_paise=refunded,
                          reason="net settled minus partial refund — chase only the delta")
            verdict = Verdict.SETTLED_CLEAN if delta <= 1 else Verdict.SETTLED_LATE
            return mk(verdict, ev, money_received_paise=o.amount_paise,
                      reason=f"settled T+{delta}")
        # captured but no settlement credit found
        covered = _day_delta(coverage_end, max(c.captured_at for c in captured)) <= -3
        ev = Evidence("none", [c.payment_id for c in captured],
                      "captured on gateway; no matching bank credit found")
        return mk(Verdict.AMBIGUOUS, ev, bank_coverage_ok=covered,
                  reason="gateway says captured; bank cannot confirm"
                         + ("" if covered else " — statement does not yet cover the window"))

    if failed and not captured and not refunds:
        ev = Evidence("gateway_status", [c.payment_id for c in failed],
                      "failed at gateway, no bank debit",
                      automation_path="exact", risk_authorized=True)
        return mk(Verdict.FAILED_NOT_DEBITED, ev,
                  reason="payment failed and was never debited — retry, do not dun")

    # ---- fuzzy path: no gateway evidence (or refund-net-zero looking for repayment) ----
    chosen, tie = assignment.get(o.order_id, (None, False))
    probabilistic_evidence: Evidence | None = None
    cands = edges.get(o.order_id, [])
    if tie:
        ev = Evidence("pass4_fuzzy", [t for t, _, _ in cands],
                      f"{len(cands)} candidate credits within {TIE_MARGIN_POINTS} weight points")
        return mk(Verdict.AMBIGUOUS, ev,
                  reason="competing bank credits cannot be separated — escalate, never guess")
    if chosen is None and cands:
        # A scored, amount-compatible candidate was considered and rejected by
        # the global assignment. That is not an exact absence-of-payment proof.
        _, strongest_weight, strongest_waterfall = max(cands, key=lambda item: item[1])
        probability = (0.99 if strongest_weight >= 82.0 else 0.0) if config.simple_scores else p_match(strongest_weight)
        score_ppm = cf.probability_to_ppm(probability)
        calibration = config.risk_calibration
        probabilistic_evidence = Evidence(
            "pass4_rejected", [txn_id for txn_id, _, _ in cands],
            "amount-compatible candidates rejected by global assignment", strongest_waterfall,
            automation_path="probabilistic",
            risk_calibration_id=calibration.calibration_id if calibration else "",
            risk_authorized=False,
            score_ppm=score_ppm,
            authority_state=config.authority.value if config.authority else "",
        )
    if chosen is not None:
        b = txn_by_id[chosen]
        w, waterfall = next((wt, wf) for t, wt, wf in cands if t == chosen)
        if config.dup_utr_check and utr_counts.get(b.utr, 0) > 1:
            ev = Evidence("pass4_fuzzy", [b.txn_id], f"UTR {b.utr} appears "
                          f"{utr_counts[b.utr]}x in the statement", waterfall)
            return mk(Verdict.AMBIGUOUS, ev,
                      reason="duplicate UTR — evidence untrustworthy, escalate")
        if config.simple_scores:  # ablation baseline: fuzzy @ 82, no probability
            p = 0.99 if w >= 82.0 else 0.0
            decision = "MATCH" if w >= 82.0 else "NON_MATCH"
        else:
            p = p_match(w)
            if config.use_conformal:
                decision = cf.decide(p, q_hat)
            else:
                decision = "MATCH" if p >= 0.5 else "NON_MATCH"
            if config.use_cost_floor:
                floor = cost_sensitive_floor(o.amount_paise)
                if decision == "MATCH" and p < floor:
                    decision = "AMBIGUOUS"
        score_ppm = cf.probability_to_ppm(p)
        calibration = config.risk_calibration
        ev = Evidence(
            "pass4_fuzzy", [b.txn_id],
            f"credit {b.txn_id} '{b.narration[:60]}' weight {w:+.2f}", waterfall,
            automation_path="probabilistic",
            risk_calibration_id=calibration.calibration_id if calibration else "",
            risk_authorized=(risk_authorized(score_ppm, o.amount_paise, calibration)
                             if calibration else False),
            score_ppm=score_ppm,
            authority_state=config.authority.value if config.authority else "",
        )
        probabilistic_evidence = ev
        if decision == "MATCH":
            rate = None if is_refund_net_zero else tds_rate_bp(o.amount_paise, b.amount_paise)
            if rate is not None:
                withheld = sub(o.amount_paise, b.amount_paise)
                return mk(Verdict.PAID_NET_OF_TDS, ev,
                          money_received_paise=b.amount_paise,
                          p_match=f"{p:.4f}",
                          reason=f"paid net of {rate // 100}% statutory TDS "
                                 f"({withheld} paise withheld against Form 26AS) — "
                                 f"P(match)={p:.4f}")
            verdict = Verdict.REFUNDED_THEN_REPAID if is_refund_net_zero else Verdict.PAID_OUT_OF_BAND
            floors = f"conformal q_hat={q_hat:.4f}" if config.use_conformal else "P>=0.5"
            if config.use_cost_floor and not config.simple_scores:
                floors += f", cost-sensitive {cost_sensitive_floor(o.amount_paise):.3f}"
            return mk(verdict, ev, money_received_paise=b.amount_paise,
                      p_match=f"{p:.4f}",
                      reason=f"P(match)={p:.4f} >= floors ({floors})")
        if decision == "AMBIGUOUS":
            return mk(Verdict.AMBIGUOUS, ev, p_match=f"{p:.4f}",
                      reason=f"P(match)={p:.4f} not classifiable at the chosen error rate")
        # NON_MATCH falls through to unpaid

    if is_refund_net_zero:
        ev = Evidence("net_arithmetic", [c.payment_id for c in caps],
                      "capture fully refunded; no repayment credit found")
        return mk(Verdict.AMBIGUOUS, ev,
                  reason="refunded in full; repayment not provable from bank feed")

    covered = _day_delta(coverage_end, o.due_date) <= -3
    ev = probabilistic_evidence or Evidence(
        "exhausted", [], "no gateway capture, no bank credit matches",
        automation_path="exact", risk_authorized=True,
    )
    return mk(Verdict.GENUINELY_UNPAID, ev, bank_coverage_ok=covered,
              reason="no evidence of money received on any of the three sources"
                     + ("" if covered else " — but bank feed does not yet cover due_date+3"))


# --------------------------------------------------------------------------- #
# calibration: harvest true-match probabilities from a labelled batch          #
# --------------------------------------------------------------------------- #

def calibrate(orders: list[Order], captures: list[GatewayCapture], bank: list[BankTxn],
              truth: dict[str, dict], alpha: float = cf.DEFAULT_ALPHA
              ) -> tuple[float, FSModel, list[float]]:
    """Run the deterministic pipeline on a labelled calibration batch and derive
    q_hat from the P(match) of pairs whose ground truth says the money arrived
    out-of-band. Returns (q_hat, fitted FSModel, calibration probs)."""
    result = reconcile(orders, captures, bank, q_hat=0.5)  # permissive; we only need scores
    probs: list[float] = []
    for oid, v in result.verdicts.items():
        t = truth.get(oid)
        if not t:
            continue
        if t["true_verdict"] in ("paid_out_of_band", "refunded_then_repaid",
                                 "paid_net_of_tds") and v.p_match:
            probs.append(float(v.p_match))
    return cf.conformal_threshold(probs, alpha), result.fs_model, probs
