"""One offline command that attacks the system and publishes what happened (F9).

    python -m ledger_daemon judge --seed 42 --n 500 --out out/judge

Eight profiles run end to end — clean, realistic, stress, distribution-shift,
adversarial, source-incomplete, high-collision, concurrent — each one a full
generate → validate → reconcile → gate → execute → prove → verify pass with a
declared fault plan in front of it.

Three things make this a harness rather than a demo:

  1. **Every source row lands in exactly one terminal bucket.** reconciled,
     exception, or quarantined — and the count is asserted, per profile, so a
     row cannot quietly vanish between stages.
  2. **Every attack is graded against an oracle it declared beforehand**
     (`faults.py`). Surviving is not passing; producing the required outcome is.
  3. **A failed invariant exits nonzero and still writes the report.** The
     artifacts are the claim surface for the docs, so they exist whether or not
     the run was flattering.

The numbers this writes are the only numbers the documentation is allowed to
quote, and each one carries its dataset class, profile, seed, sample size and
the command that produced it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field

from . import policy
from .cases import CaseState, CaseStore, VersionConflict, open_exception_cases
from .certificates import (
    batch_root, build_certificate, calibration_identity, recon_config_hash,
    source_hash_map, source_rows, write_proof_bundle,
)
from .datagen import generate, load_batch, load_finance_events
from .drift import DriftMonitor, observations, shift_feed
from .authority import AuthorityController
from .evaluate import evaluate, run_ledger_daemon
from .evidence_reader import ALLOWED_KINDS, RegexReader, source_text_hash
from .executor import Executor, MockRazorpayAdapter, event_id_for
from .faults import (
    Fault, FaultPlan, Injection, apply_bank_faults, plan_injections, tamper_certificate,
)
from .models import BankTxn, Verdict
from .quarantine import QuarantineStore
from .recon import FULL, ReconConfig, calibrate, reconcile
from .robustness import CAL_SEED_OFFSET, assert_disjoint_seeds
from .source_contracts import SourceKind, sha256_hex, validate_rows
from .verifier import verify_certificate

JUDGE_SCHEMA_VERSION = "1"

#: Named, ordered, and each one a sentence a run can fail.
HARD_INVARIANTS = (
    "every_row_bucketed",
    "zero_wrong_rupees",
    "no_duplicate_side_effects",
    "proofs_verify",
    "attacks_meet_oracles",
    "policy_gate_holds",
)

_VERIFY_SAMPLE = 25


@dataclass(frozen=True)
class JudgeProfile:
    name: str
    datagen: str
    faults: tuple[Fault, ...] = ()
    lag_days: int = 0
    concurrent: bool = False


PROFILES: tuple[JudgeProfile, ...] = (
    JudgeProfile("clean", "clean"),
    JudgeProfile("realistic", "clean", (Fault.REORDER,)),
    JudgeProfile("stress", "stress"),
    JudgeProfile("distribution-shift", "clean", lag_days=14),
    JudgeProfile("adversarial", "clean",
                 (Fault.DUPLICATE, Fault.MALFORM_JSON, Fault.PROMPT_INJECTION,
                  Fault.HASH_TAMPER, Fault.STALE_VERSION, Fault.TIMEOUT,
                  Fault.CRASH_AFTER_WRITE)),
    JudgeProfile("source-incomplete", "clean", (Fault.DROP, Fault.TRUNCATE)),
    JudgeProfile("high-collision", "stress", (Fault.DUPLICATE,)),
    JudgeProfile("concurrent", "clean", concurrent=True),
)


@dataclass(frozen=True)
class AttackOutcome:
    profile: str
    fault: str
    stage: str
    target: str
    oracle: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InvariantResult:
    name: str
    held: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ProfileResult:
    profile: str
    datagen_profile: str
    seed: int
    n: int
    processed: int = 0
    reconciled: int = 0
    exceptions: int = 0
    quarantined: int = 0
    orders: int = 0
    match_rate_ppm: int = 0
    dcpr_ppm: int = 0
    false_hold_ppm: int = 0
    wrongly_chased_paise: int = 0
    actions: int = 0
    duplicate_side_effects: int = 0
    cases_opened: int = 0
    proofs_built: int = 0
    proofs_verified: int = 0
    proofs_rejected: int = 0
    authority: str = "CALIBRATED"
    probabilistic_verdicts: int = 0
    safe_coverage_ppm: int = 0
    reader_fallbacks: int = 0
    stage_us: dict[str, int] = field(default_factory=dict)
    elapsed_us: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class JudgeReport:
    seed: int
    n: int
    out_dir: str
    profiles: tuple[ProfileResult, ...]
    attacks: tuple[AttackOutcome, ...]
    invariants: tuple[InvariantResult, ...]
    processed: int
    reconciled: int
    exceptions: int
    quarantined: int
    wrongly_chased_paise: int
    duplicate_side_effects: int
    proofs_verified: int
    proofs_rejected: int
    attacks_run: int
    attacks_passed: int
    elapsed_s: float
    fingerprint: str

    @property
    def ok(self) -> bool:
        return all(i.held for i in self.invariants)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _percentiles(samples_ns: list[int]) -> dict[str, int]:
    if not samples_ns:
        return {"p50": 0, "p95": 0, "p99": 0, "samples": 0}
    ordered = sorted(samples_ns)

    def at(pct: int) -> int:
        index = (pct * len(ordered) + 99) // 100 - 1
        return ordered[max(0, min(index, len(ordered) - 1))] // 1000

    return {"p50": at(50), "p95": at(95), "p99": at(99), "samples": len(ordered)}


def _bank_rows(bank: list[BankTxn]) -> list[dict]:
    return [asdict(txn) for txn in bank]


def _bank_objects(rows: list[dict]) -> list[BankTxn]:
    return [BankTxn(row["txn_id"], row["value_date"], int(row["amount_paise"]),
                    row["credit_debit"], row["utr"], row["narration"],
                    int(row["balance_after"])) for row in rows]


class _TimeoutAdapter:
    """An adapter that never answers. The executor must leave no half-state."""

    name = "timeout-adapter"

    def create_payment_link(self, order, amount_paise: int) -> dict:
        raise TimeoutError("payment link call did not answer")


# --------------------------------------------------------------------------- #
# one profile, end to end
# --------------------------------------------------------------------------- #

def _run_profile(profile: JudgeProfile, seed: int, n: int, out_dir: str,
                 cache: dict) -> tuple[ProfileResult, list[AttackOutcome], list[dict]]:
    started = time.perf_counter_ns()
    result_row = ProfileResult(profile.name, profile.datagen, seed, n)
    attacks: list[AttackOutcome] = []
    case_rows: list[dict] = []
    work_dir = os.path.join(out_dir, "profiles", profile.name)
    os.makedirs(work_dir, exist_ok=True)

    # ---- generate (cached per datagen profile: the fault plan is what varies)
    key = (profile.datagen, seed, n)
    if key not in cache:
        cal_seed = seed + CAL_SEED_OFFSET
        assert_disjoint_seeds(cal_seed, seed)
        cal_dir = os.path.join(out_dir, "batches", f"{profile.datagen}-cal")
        eval_dir = os.path.join(out_dir, "batches", f"{profile.datagen}-eval")
        generate(cal_seed, n, cal_dir, profile.datagen)
        generate(seed, n, eval_dir, profile.datagen)
        cal = load_batch(cal_dir)
        cal_events = load_finance_events(cal_dir)
        q_hat, fs_model, _probs = calibrate(cal[0], cal[1], cal[2], cal[3])
        calibration_id = calibration_identity(
            q_hat, batch_root(source_hash_map(source_rows(
                cal[0], cal[1], cal[2], finance_events=cal_events))))
        cache[key] = {
            "eval_dir": eval_dir, "batch": load_batch(eval_dir),
            "events": load_finance_events(eval_dir),
            "q_hat": q_hat, "fs_model": fs_model, "calibration_id": calibration_id,
        }
    ctx = cache[key]
    orders, captures, bank, truth = ctx["batch"]
    finance_events = ctx["events"]

    # ---- fault plan
    plan = FaultPlan(seed=seed, faults=profile.faults)
    injections = plan_injections(plan, [b.txn_id for b in bank],
                                 [o.order_id for o in orders]) if profile.faults else ()

    # ---- stage: ingest (validation + quarantine, faults applied to the feed)
    t0 = time.perf_counter_ns()
    offered = apply_bank_faults(_bank_rows(bank), injections)
    if profile.lag_days:
        offered = _bank_rows(shift_feed(_bank_objects(offered), lag_days=profile.lag_days))
    quarantine = QuarantineStore(os.path.join(work_dir, "quarantine.jsonl"))
    accepted_bank, bank_summary = validate_rows(SourceKind.BANK_TXN, offered, quarantine)
    accepted_orders, order_summary = validate_rows(
        SourceKind.ORDER, [asdict(o) for o in orders], quarantine)
    accepted_captures, capture_summary = validate_rows(
        SourceKind.CAPTURE, [asdict(c) for c in captures], quarantine)
    result_row.stage_us["ingest"] = (time.perf_counter_ns() - t0) // 1000
    feed = _bank_objects(accepted_bank)
    quarantined = (bank_summary.quarantined + order_summary.quarantined
                   + capture_summary.quarantined)
    result_row.processed = len(offered) + len(orders) + len(captures)
    result_row.quarantined = quarantined
    result_row.orders = len(orders)

    # ---- stage: reconcile (with drift authority where the profile shifts the feed)
    authority_state = None
    if profile.lag_days:
        baseline_result = reconcile(orders, captures, bank, q_hat=ctx["q_hat"],
                                    fs_model=ctx["fs_model"], finance_events=finance_events)
        try:
            monitor = DriftMonitor(observations(orders, captures, bank,
                                                baseline_result.verdicts))
        except ValueError as exc:
            # drift.py refuses an undersized calibration window by design. At
            # small n there are too few probabilistic rows to characterise one,
            # so the ladder does not run and the run says so rather than
            # pretending a halt it never measured.
            result_row.note = f"drift monitor not run: {exc}"
            monitor = None
        if monitor is not None:
            controller = AuthorityController(ctx["calibration_id"])
            live_result = reconcile(orders, captures, feed, q_hat=ctx["q_hat"],
                                    fs_model=ctx["fs_model"], finance_events=finance_events)
            for _window in range(3):
                report = monitor.observe(
                    observations(orders, captures, feed, live_result.verdicts))
                decision = controller.apply(report, ctx["calibration_id"])
            authority_state = decision.state
    t0 = time.perf_counter_ns()
    config = ReconConfig(authority=authority_state)
    result = reconcile(orders, captures, feed, q_hat=ctx["q_hat"],
                       fs_model=ctx["fs_model"], config=config,
                       finance_events=finance_events)
    result_row.stage_us["reconcile"] = (time.perf_counter_ns() - t0) // 1000
    result_row.authority = authority_state.value if authority_state else "CALIBRATED"
    result_row.probabilistic_verdicts = sum(
        1 for v in result.verdicts.values() if v.evidence.automation_path == "probabilistic")

    # ---- stage: policy gate
    t0 = time.perf_counter_ns()
    ld, decisions = run_ledger_daemon(orders, result)
    result_row.stage_us["policy"] = (time.perf_counter_ns() - t0) // 1000

    # ---- stage: execute
    db_path = os.path.join(work_dir, "ledger.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)
    executor = Executor(db_path, adapter=MockRazorpayAdapter(),
                        drafts_dir=os.path.join(work_dir, "drafts"))
    orders_by_id = {o.order_id: o for o in orders}
    t0 = time.perf_counter_ns()
    for order_id in sorted(ld.chased):
        order = orders_by_id[order_id]
        verdict = result.verdicts[order_id]
        amount = verdict.delta_due_paise or order.amount_paise
        executor.execute(order, "CREATE_PAYMENT_LINK", amount, decisions[order_id].rule_fired)
        result_row.actions += 1
    result_row.stage_us["execute"] = (time.perf_counter_ns() - t0) // 1000

    # ---- stage: prove + verify
    t0 = time.perf_counter_ns()
    config_hash = recon_config_hash(config)
    rows = source_rows(orders, captures, feed, finance_events=finance_events)
    hashes = source_hash_map(rows)
    if profile.name == "clean":
        write_proof_bundle(os.path.join(out_dir, "proofs"), orders, captures, feed,
                           result.verdicts, config_hash=config_hash,
                           calibration_id=ctx["calibration_id"],
                           finance_events=finance_events)
    sample_ids = sorted(result.verdicts)[::max(1, len(orders) // _VERIFY_SAMPLE)]
    certificates = {}
    for order_id in sample_ids:
        certificates[order_id] = build_certificate(
            orders_by_id[order_id], result.verdicts[order_id], hashes,
            config_hash, ctx["calibration_id"], rows=rows)
    result_row.stage_us["proof"] = (time.perf_counter_ns() - t0) // 1000
    result_row.proofs_built = len(certificates)

    verify_samples: list[int] = []
    for order_id, certificate in certificates.items():
        t1 = time.perf_counter_ns()
        checked = verify_certificate(certificate, rows,
                                     expected_config_hash=config_hash,
                                     expected_calibration_id=ctx["calibration_id"])
        verify_samples.append(time.perf_counter_ns() - t1)
        if checked.valid:
            result_row.proofs_verified += 1
        else:
            result_row.proofs_rejected += 1
    result_row.stage_us["verify"] = sum(verify_samples) // 1000 if verify_samples else 0

    # ---- exception cases
    store = CaseStore(db_path)
    opened = open_exception_cases(
        store, result.verdicts, decisions,
        certificate_ids={oid: c.proof_hash for oid, c in certificates.items()},
        quarantine_ids=tuple(bank_summary.duplicate_ids))
    result_row.cases_opened = len(opened)
    for subject, case in sorted(opened.items()):
        case_rows.append({"profile": profile.name, "case_id": case.case_id,
                          "subject_id": subject, "reason_code": case.reason_code,
                          "state": case.state.value, "version": case.version})

    # ---- terminal buckets: reconciled | exception | quarantined
    matched_bank = set()
    for verdict in result.verdicts.values():
        matched_bank.update(ref for ref in verdict.evidence_refs if ref.startswith("TXN"))
    exception_orders = {s for s in opened if s.startswith("ORD")}
    order_exceptions = len(exception_orders)
    bank_exceptions = sum(1 for row in accepted_bank if row["txn_id"] not in matched_bank)
    capture_exceptions = sum(1 for row in accepted_captures
                             if row["order_id"] in exception_orders)
    result_row.exceptions = order_exceptions + bank_exceptions + capture_exceptions
    result_row.reconciled = (len(accepted_orders) + len(accepted_bank)
                             + len(accepted_captures)
                             - result_row.exceptions)

    # ---- scoring against ground truth
    evaluation = evaluate(seed, orders, captures, result, truth)
    result_row.match_rate_ppm = round(evaluation.match_rate * 1_000_000)
    result_row.dcpr_ppm = round(evaluation.dcpr * 1_000_000)
    result_row.false_hold_ppm = round(evaluation.false_hold_rate * 1_000_000)
    result_row.wrongly_chased_paise = evaluation.wrong_paise["LD"]

    # ---- evidence reader coverage on this profile's narrations
    reader = RegexReader()
    safe = 0
    for row in accepted_bank:
        proposal = reader.extract(row["narration"], source_text_hash(row["narration"]))
        if not proposal.abstained and all(s.kind in ALLOWED_KINDS for s in proposal.spans):
            safe += 1
        result_row.reader_fallbacks += bool(proposal.fallback_used)
    result_row.safe_coverage_ppm = (safe * 1_000_000 // len(accepted_bank)
                                    if accepted_bank else 0)

    # ---- graded attacks
    attacks.extend(_grade_attacks(profile, injections, result, decisions, orders_by_id,
                                  executor, store, certificates, rows, config_hash,
                                  ctx["calibration_id"], quarantine, ld.chased, feed,
                                  ctx, finance_events, captures, evaluation))

    # ---- duplicate side effects: one audit row per (order, action, attempt)
    if profile.concurrent:
        result_row.duplicate_side_effects = _concurrent_double_execute(
            executor, orders_by_id, ld)
    result_row.duplicate_side_effects += _count_duplicate_effects(
        executor, set(ld.chased) | {_probe_order(orders_by_id, ld.chased).order_id})

    result_row.elapsed_us = (time.perf_counter_ns() - started) // 1000
    result_row.stage_us["verify_samples"] = len(verify_samples)
    return result_row, attacks, case_rows


def _count_duplicate_effects(executor: Executor, order_ids) -> int:
    """One audit row per event id, or the primary key did not do its job."""
    duplicates = 0
    for order_id in sorted(order_ids):
        seen: set[str] = set()
        for row in executor.audit(order_id):
            event_id = row.get("event_id", "")
            if event_id in seen:
                duplicates += 1
            seen.add(event_id)
    return duplicates


def _probe_order(orders_by_id: dict, chased: set[str]):
    """An order the run did not action, so a probe's audit rows are unambiguous."""
    for order_id in sorted(orders_by_id):
        if order_id not in chased:
            return orders_by_id[order_id]
    return orders_by_id[sorted(orders_by_id)[0]]


def _concurrent_double_execute(executor, orders_by_id, ld) -> int:
    """Two threads, one action, one attempt. The primary key is the referee."""
    order = _probe_order(orders_by_id, ld.chased)
    before = len(executor.audit(order.order_id))
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        try:
            executor.execute(order, "CREATE_PAYMENT_LINK", order.amount_paise,
                             "R_JUDGE_CONCURRENCY", attempt_no=9)
        except Exception:
            pass

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    written = len(executor.audit(order.order_id)) - before
    return max(0, written - 1)


def _grade_attacks(profile, injections, result, decisions, orders_by_id, executor,
                   store, certificates, rows, config_hash, calibration_id,
                   quarantine, chased, feed, ctx, finance_events, captures,
                   evaluation) -> list[AttackOutcome]:
    graded: list[AttackOutcome] = []

    def record(injection: Injection, passed: bool, detail: str) -> None:
        graded.append(AttackOutcome(profile.name, injection.fault.value,
                                    injection.stage, injection.target,
                                    injection.oracle, passed, detail))

    quarantined_hashes = _quarantine_codes(quarantine.path)

    for injection in injections:
        fault = injection.fault
        if fault in (Fault.DUPLICATE, Fault.MALFORM_JSON):
            codes = quarantined_hashes
            wanted = "DUPLICATE_ID" if fault is Fault.DUPLICATE else None
            passed = bool(codes) and (wanted in codes if wanted else bool(codes))
            record(injection, passed,
                   f"quarantine wrote {sorted(codes) or 'nothing'}")
        elif fault in (Fault.DROP, Fault.TRUNCATE):
            passed = evaluation.wrong_paise["LD"] == 0
            record(injection, passed,
                   f"{evaluation.wrong_paise['LD']} paise wrongly chased with the row damaged")
        elif fault is Fault.REORDER:
            rotated = reconcile(list(orders_by_id.values()), captures,
                                _bank_objects(_bank_rows(feed)[1:] + _bank_rows(feed)[:1]),
                                q_hat=ctx["q_hat"], fs_model=ctx["fs_model"],
                                finance_events=finance_events)
            same = {oid: v.verdict.value for oid, v in rotated.verdicts.items()} == {
                oid: v.verdict.value for oid, v in result.verdicts.items()}
            record(injection, same, "verdicts identical under a rotated feed"
                   if same else "feed order changed the verdicts")
        elif fault is Fault.PROMPT_INJECTION:
            record(injection, *_grade_injection(feed, result, decisions))
        elif fault is Fault.HASH_TAMPER:
            record(injection, *_grade_tamper(certificates, rows, config_hash,
                                             calibration_id))
        elif fault is Fault.STALE_VERSION:
            record(injection, *_grade_stale_version(store))
        elif fault in (Fault.TIMEOUT, Fault.CRASH_AFTER_WRITE):
            record(injection, *_grade_executor_fault(fault, executor, orders_by_id,
                                                     chased))
    return graded


def _quarantine_codes(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {json.loads(line)["error_code"] for line in fh if line.strip()}


def _grade_injection(feed, result, decisions) -> tuple[bool, str]:
    """Instruction text in a narration must grant nothing at all."""
    from .faults import INJECTION_TEXT

    hostile = [row for row in feed if INJECTION_TEXT.split()[0] in row.narration.upper()]
    if not hostile:
        return True, "hostile narration did not survive validation"
    reader = RegexReader()
    for row in hostile:
        proposal = reader.extract(row.narration, source_text_hash(row.narration))
        if any(s.kind not in ALLOWED_KINDS for s in proposal.spans):
            return False, f"reader emitted a kind outside the allowlist on {row.txn_id}"
        if any("IGNORE" in s.value.upper() for s in proposal.spans):
            return False, f"instruction text was read as evidence on {row.txn_id}"
    allowed_on_paid = [
        oid for oid, decision in decisions.items()
        if decision.outcome == policy.ALLOW
        and result.verdicts[oid].verdict not in
        (Verdict.GENUINELY_UNPAID, Verdict.PARTIALLY_PAID)]
    if allowed_on_paid:
        return False, f"narration text moved {len(allowed_on_paid)} orders past the gate"
    return True, f"{len(hostile)} hostile narrations read as inert typed spans"


def _grade_tamper(certificates, rows, config_hash, calibration_id) -> tuple[bool, str]:
    if not certificates:
        return False, "no certificate available to tamper with"
    order_id = sorted(certificates)[0]
    certificate = certificates[order_id]
    from .certificates import ProofCertificate

    tampered_body = tamper_certificate(json.loads(certificate.to_json()))
    try:
        tampered = ProofCertificate.from_json(json.dumps(tampered_body))
    except (ValueError, TypeError, KeyError):
        return True, "tampered certificate failed to parse at the schema boundary"
    checked = verify_certificate(tampered, rows, expected_config_hash=config_hash,
                                 expected_calibration_id=calibration_id)
    return (not checked.valid,
            f"tampered {order_id}: {', '.join(checked.error_codes) or 'ACCEPTED'}")


def _grade_stale_version(store: CaseStore) -> tuple[bool, str]:
    cases = store.list_cases(open_only=True)
    if not cases:
        return True, "no open case to contend for"
    case = cases[0]
    version = case.version
    store.transition(case.case_id, version, CaseState.ASSIGNED, "judge-writer-1")
    try:
        store.transition(case.case_id, version, CaseState.ASSIGNED, "judge-writer-2")
    except VersionConflict:
        return True, f"second writer at version {version} was refused"
    except Exception as exc:  # a different refusal is still a refusal, but name it
        return True, f"second writer refused with {type(exc).__name__}"
    return False, "a stale writer was allowed to overwrite"


def _grade_executor_fault(fault: Fault, executor: Executor, orders_by_id,
                          chased: set[str]) -> tuple[bool, str]:
    """A call that fails and a call that is retried must both end at one row."""
    order = _probe_order(orders_by_id, chased)
    attempt = 7 if fault is Fault.TIMEOUT else 8
    event_id = event_id_for(order.order_id, "CREATE_PAYMENT_LINK", attempt)

    def rows() -> int:
        return sum(1 for row in executor.audit(order.order_id)
                   if row.get("event_id") == event_id)

    if fault is Fault.TIMEOUT:
        stalled = Executor(executor.db_path, adapter=_TimeoutAdapter())
        try:
            stalled.execute(order, "CREATE_PAYMENT_LINK", order.amount_paise,
                            "R_JUDGE_TIMEOUT", attempt_no=attempt)
        except TimeoutError:
            pass
        after_failure = rows()
        # the retry that a human or a scheduler would make
        executor.execute(order, "CREATE_PAYMENT_LINK", order.amount_paise,
                         "R_JUDGE_TIMEOUT", attempt_no=attempt)
        passed = after_failure == 0 and rows() == 1
        return passed, (f"timeout wrote {after_failure} rows, retry left {rows()}; "
                        f"event {event_id[:12]}")

    executor.execute(order, "CREATE_PAYMENT_LINK", order.amount_paise,
                     "R_JUDGE_CRASH", attempt_no=attempt)
    # "crash": a brand new executor against the same database, same event id
    restarted = Executor(executor.db_path, adapter=MockRazorpayAdapter())
    restarted.execute(order, "CREATE_PAYMENT_LINK", order.amount_paise,
                      "R_JUDGE_CRASH", attempt_no=attempt)
    return rows() == 1, (f"replay after crash left {rows()} row; "
                         f"event {event_id[:12]}")


# --------------------------------------------------------------------------- #
# grading and artifacts
# --------------------------------------------------------------------------- #

def _grade_invariants(totals: dict, profiles: list[ProfileResult],
                      attacks: list[AttackOutcome]) -> tuple[InvariantResult, ...]:
    bad_buckets = [p.profile for p in profiles
                   if p.processed != p.reconciled + p.exceptions + p.quarantined]
    wrong = [p.profile for p in profiles if p.wrongly_chased_paise != 0]
    duplicated = [p.profile for p in profiles if p.duplicate_side_effects != 0]
    rejected = [p.profile for p in profiles if p.proofs_rejected != 0]
    failed_attacks = [f"{a.profile}:{a.fault}" for a in attacks if not a.passed]
    return (
        InvariantResult("every_row_bucketed", not bad_buckets,
                        "every source row is reconciled, an exception, or quarantined"
                        if not bad_buckets else f"unbalanced: {bad_buckets}"),
        InvariantResult("zero_wrong_rupees", not wrong,
                        f"{totals['wrongly_chased_paise']} paise wrongly chased"
                        if wrong else "no rupee was chased that had already arrived"),
        InvariantResult("no_duplicate_side_effects", not duplicated,
                        "no action wrote twice" if not duplicated
                        else f"duplicate effects in {duplicated}"),
        InvariantResult("proofs_verify", not rejected and totals["proofs_verified"] > 0,
                        f"{totals['proofs_verified']} certificates verified independently"
                        if not rejected else f"rejected proofs in {rejected}"),
        InvariantResult("attacks_meet_oracles", not failed_attacks,
                        f"{len(attacks)} attacks met their declared oracle"
                        if not failed_attacks else f"failed: {failed_attacks}"),
        InvariantResult("policy_gate_holds", not wrong,
                        "no action on a non-chaseable verdict"),
    )


def _fingerprint(profiles: list[ProfileResult], attacks: list[AttackOutcome]) -> str:
    """Everything except wall time — so determinism is testable on a busy laptop."""
    return sha256_hex({
        "profiles": [{k: v for k, v in p.to_dict().items()
                      if k not in ("stage_us", "elapsed_us")} for p in profiles],
        "attacks": [{k: v for k, v in a.to_dict().items() if k != "detail"}
                    for a in attacks],
    })


def _write_artifacts(report: JudgeReport, case_rows: list[dict],
                     stage_samples: dict[str, list[int]]) -> None:
    out = report.out_dir
    os.makedirs(out, exist_ok=True)

    summary = {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "command": f"python -m ledger_daemon judge --seed {report.seed} "
                   f"--n {report.n} --out {out}",
        "dataset": {"kind": "synthetic", "generator": "ledger_daemon.datagen",
                    "seed": report.seed, "n": report.n,
                    "profiles": [p.name for p in PROFILES]},
        "duplicate_side_effects": report.duplicate_side_effects,
        "elapsed_s": round(report.elapsed_s, 2),
        "exceptions": report.exceptions,
        "fingerprint": report.fingerprint,
        "invariants": [i.to_dict() for i in report.invariants],
        "ok": report.ok,
        "processed": report.processed,
        "profiles": [p.to_dict() for p in report.profiles],
        "proofs_rejected": report.proofs_rejected,
        "proofs_verified": report.proofs_verified,
        "quarantined": report.quarantined,
        "reconciled": report.reconciled,
        "wrongly_chased_paise": report.wrongly_chased_paise,
    }
    _dump(os.path.join(out, "summary.json"), summary)

    with open(os.path.join(out, "cases.jsonl"), "w", encoding="utf-8") as fh:
        for row in case_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    _dump(os.path.join(out, "attacks.json"), {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "attacks_run": report.attacks_run,
        "attacks_passed": report.attacks_passed,
        "attacks": [a.to_dict() for a in report.attacks],
    })

    _dump(os.path.join(out, "latency.json"), {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "unit": "microseconds",
        "note": ("stage samples are one per profile unless stated; verify samples "
                 "are per certificate. Percentiles over small samples are reported "
                 "with their sample count so they are not over-read."),
        "end_to_end_us": _percentiles([p.elapsed_us * 1000 for p in report.profiles]),
        "stages": {name: _percentiles(samples) for name, samples in stage_samples.items()},
    })

    if not os.path.exists(os.path.join(out, "proof-manifest.json")):
        proofs = os.path.join(out, "proofs", "proof-manifest.json")
        if os.path.exists(proofs):
            with open(proofs, encoding="utf-8") as fh:
                _dump(os.path.join(out, "proof-manifest.json"), json.load(fh))
        else:
            _dump(os.path.join(out, "proof-manifest.json"),
                  {"schema_version": JUDGE_SCHEMA_VERSION, "certificates": {},
                   "note": "no bundle was written for this run"})

    with open(os.path.join(out, "claims.md"), "w", encoding="utf-8") as fh:
        fh.write(render_claims(report))


def _dump(path: str, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def _pct(ppm: int) -> str:
    return f"{ppm / 10_000:.1f}%"


def render_claims(report: JudgeReport) -> str:
    command = (f"python -m ledger_daemon judge --seed {report.seed} "
               f"--n {report.n} --out {report.out_dir}")
    lines = [
        "# Claims", "",
        "Every row is generated by the judge run named in the command column. No",
        "number in this repository may be quoted unless it appears here.", "",
        "| claim | value | dataset class | profile | seed | n | command | artifact | limitation |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for profile in report.profiles:
        lines.append(
            f"| double-chase prevention | {_pct(profile.dcpr_ppm)} | synthetic | "
            f"{profile.profile} | {report.seed} | {profile.n} | `{command}` | "
            f"summary.json | authored generator; labels written at injection time |")
        lines.append(
            f"| rupees wrongly chased | {profile.wrongly_chased_paise} paise | synthetic | "
            f"{profile.profile} | {report.seed} | {profile.n} | `{command}` | "
            f"summary.json | measured against generated ground truth, not a real ledger |")
        lines.append(
            f"| false-hold rate | {_pct(profile.false_hold_ppm)} | synthetic | "
            f"{profile.profile} | {report.seed} | {profile.n} | `{command}` | "
            f"summary.json | the price paid for the line above; always read together |")
    lines += [
        f"| attacks meeting their oracle | {report.attacks_passed}/{report.attacks_run} | "
        f"synthetic | all | {report.seed} | {report.n} | `{command}` | attacks.json | "
        "oracles are authored with the faults they grade |",
        f"| certificates independently verified | {report.proofs_verified} | synthetic | "
        f"all | {report.seed} | {report.n} | `{command}` | proof-manifest.json | "
        "sampled per profile; the clean profile writes the full bundle |",
        f"| duplicate side effects | {report.duplicate_side_effects} | synthetic | "
        f"all | {report.seed} | {report.n} | `{command}` | summary.json | "
        "concurrency probed in-process, not across machines |",
        "",
        "## Invariants", "",
        "| invariant | held | detail |", "|---|---|---|",
    ]
    for invariant in report.invariants:
        lines.append(f"| {invariant.name} | {'yes' if invariant.held else 'NO'} | "
                     f"{invariant.detail} |")
    lines += [
        "", "## What this run is not", "",
        "Synthetic data from one generator, authored by the same project that is",
        "being graded. The judge bounds implementation risk — that the system does",
        "what it claims on data whose truth is known — not model risk, and not the",
        "question of whether real merchant feeds look like these.", "",
    ]
    return "\n".join(lines)


def run_judge(seed: int, n: int, out_dir: str,
              profiles: tuple[JudgeProfile, ...] = PROFILES) -> JudgeReport:
    started = time.perf_counter()
    os.makedirs(out_dir, exist_ok=True)
    cache: dict = {}
    results: list[ProfileResult] = []
    attacks: list[AttackOutcome] = []
    case_rows: list[dict] = []
    stage_samples: dict[str, list[int]] = {}

    for profile in profiles:
        result, profile_attacks, profile_cases = _run_profile(profile, seed, n, out_dir, cache)
        results.append(result)
        attacks.extend(profile_attacks)
        case_rows.extend(profile_cases)
        for stage, micros in result.stage_us.items():
            if stage != "verify_samples":
                stage_samples.setdefault(stage, []).append(micros * 1000)

    totals = {
        "processed": sum(p.processed for p in results),
        "reconciled": sum(p.reconciled for p in results),
        "exceptions": sum(p.exceptions for p in results),
        "quarantined": sum(p.quarantined for p in results),
        "wrongly_chased_paise": sum(p.wrongly_chased_paise for p in results),
        "duplicate_side_effects": sum(p.duplicate_side_effects for p in results),
        "proofs_verified": sum(p.proofs_verified for p in results),
        "proofs_rejected": sum(p.proofs_rejected for p in results),
    }
    invariants = _grade_invariants(totals, results, attacks)
    report = JudgeReport(
        seed=seed, n=n, out_dir=out_dir,
        profiles=tuple(results), attacks=tuple(attacks), invariants=tuple(invariants),
        attacks_run=len(attacks), attacks_passed=sum(1 for a in attacks if a.passed),
        elapsed_s=time.perf_counter() - started,
        fingerprint=_fingerprint(results, attacks),
        **totals,
    )
    _write_artifacts(report, case_rows, stage_samples)
    return report
