"""Drift detection compares the live window against the batch we calibrated on.

Every signal is integer arithmetic over observable fields -- parse rate, an
amount histogram, settlement lag, fee basis points -- so a window is scored the
same way on every machine and can be re-derived from the persisted window hash.
The generator's `stress` profile is a real narration-quality shift, so the
tests do not have to fabricate one.
"""

import pytest

from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.drift import (
    HEALTHY,
    WARNING,
    MIN_WINDOW,
    SEVERE,
    UNDERSIZED,
    DriftMonitor,
    DriftObservation,
    observations,
)


def _obs(score=1_000_000, bucket=5, lag=1, fee=200, ref=1_000_000) -> DriftObservation:
    return DriftObservation(narration_score_ppm=score, amount_bucket=bucket,
                            date_lag_days=lag, fee_rate_bp=fee, reference_ppm=ref)


def _window(n: int, **kw) -> list[DriftObservation]:
    return [_obs(**kw) for _ in range(n)]


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    """Observations come from the matcher's own evidence, so the batch is
    reconciled first -- the same way the pipeline produces them."""
    root = tmp_path_factory.mktemp("drift")
    path = str(root / "clean")
    generate(42, 500, path, "clean")
    return load_batch(path)


def _observe(batch, **shift):
    """Reconcile a batch (optionally with a declared feed change) and reduce it."""
    from ledger_daemon.drift import shift_feed
    from ledger_daemon.recon import reconcile

    orders, captures, bank, _truth = batch
    if shift:
        bank = shift_feed(bank, **shift)
    result = reconcile(orders, captures, bank, q_hat=0.001)
    return observations(orders, captures, bank, result.verdicts)


# --------------------------- the contract ----------------------------------- #

def test_an_identical_window_is_healthy():
    baseline = _window(MIN_WINDOW)
    report = DriftMonitor(baseline).observe(_window(MIN_WINDOW))
    assert report.severity == HEALTHY
    assert report.usable is True


def test_an_undersized_window_is_refused_not_scored():
    monitor = DriftMonitor(_window(MIN_WINDOW))
    report = monitor.observe(_window(MIN_WINDOW - 1, score=0))

    assert report.severity == UNDERSIZED
    assert report.usable is False
    assert report.signals == ()


def test_an_undersized_baseline_is_refused_at_construction():
    with pytest.raises(ValueError):
        DriftMonitor(_window(MIN_WINDOW - 1))


def test_window_hash_is_content_addressed_and_order_independent():
    monitor = DriftMonitor(_window(MIN_WINDOW))
    a = _window(MIN_WINDOW - 2) + [_obs(score=10), _obs(score=20)]
    b = [_obs(score=20), _obs(score=10)] + _window(MIN_WINDOW - 2)

    first, second = monitor.observe(a), monitor.observe(b)
    assert first.window_hash == second.window_hash
    assert first.window_hash != monitor.baseline_hash
    assert first.n == MIN_WINDOW


def test_the_report_names_every_signal_it_scored():
    report = DriftMonitor(_window(MIN_WINDOW)).observe(_window(MIN_WINDOW))
    assert {s.name for s in report.signals} == {
        "narration_parse_rate", "nonconformity", "amount_bucket",
        "date_lag", "fee_rate",
    }
    for signal in report.signals:
        assert type(signal.baseline) is int and type(signal.live) is int


# --------------------------- each signal moves alone ------------------------ #

def test_a_collapsed_parse_rate_is_severe():
    baseline = _window(MIN_WINDOW, ref=1_000_000)
    report = DriftMonitor(baseline).observe(_window(MIN_WINDOW, ref=0))

    assert report.severity == SEVERE
    parse = next(s for s in report.signals if s.name == "narration_parse_rate")
    assert parse.baseline == 1_000_000 and parse.live == 0
    assert parse.severity == SEVERE


def test_a_settlement_lag_blowout_is_severe():
    baseline = _window(MIN_WINDOW, lag=1)
    report = DriftMonitor(baseline).observe(_window(MIN_WINDOW, lag=30))
    lag = next(s for s in report.signals if s.name == "date_lag")
    assert lag.severity == SEVERE and report.severity == SEVERE


def test_a_fee_schedule_change_is_visible():
    baseline = _window(MIN_WINDOW, fee=200)
    report = DriftMonitor(baseline).observe(_window(MIN_WINDOW, fee=900))
    fee = next(s for s in report.signals if s.name == "fee_rate")
    assert fee.severity == SEVERE


def test_an_amount_mix_shift_is_visible():
    baseline = _window(MIN_WINDOW, bucket=3)
    report = DriftMonitor(baseline).observe(_window(MIN_WINDOW, bucket=9))
    amount = next(s for s in report.signals if s.name == "amount_bucket")
    assert amount.severity == SEVERE


def test_the_window_severity_is_the_worst_signal():
    baseline = _window(MIN_WINDOW)
    healthy_but_one = _window(MIN_WINDOW - 1) + [_obs(lag=90)]
    report = DriftMonitor(baseline).observe(healthy_but_one)
    assert report.severity == max(
        (s.severity for s in report.signals),
        key=lambda sev: (HEALTHY, "WARNING", SEVERE).index(sev))


# --------------------------- against the real generator --------------------- #

def test_observations_are_integers_derived_from_the_batch(batch):
    obs = _observe(batch)

    assert len(obs) >= MIN_WINDOW
    for item in obs:
        assert type(item.narration_score_ppm) is int
        assert type(item.amount_bucket) is int
        assert type(item.date_lag_days) is int
        assert type(item.fee_rate_bp) is int
        assert 0 <= item.narration_score_ppm <= 1_000_000


def test_a_batch_does_not_drift_from_itself(batch):
    baseline = _observe(batch)
    assert DriftMonitor(baseline).observe(baseline).severity == HEALTHY


def test_a_declared_settlement_delay_escalates_with_its_size(batch):
    """The bank starts settling later and later; severity must follow.

    The shift is declared rather than mined out of a lucky generator seed, so
    what this asserts is a property of the monitor and not of the fixture.
    """
    monitor = DriftMonitor(_observe(batch))

    assert monitor.observe(_observe(batch, lag_days=3)).severity == WARNING
    assert monitor.observe(_observe(batch, lag_days=7)).severity == SEVERE
    assert monitor.observe(_observe(batch, lag_days=14)).severity == SEVERE


def test_scoring_a_window_is_deterministic(batch):
    monitor = DriftMonitor(_observe(batch))
    window = _observe(batch, lag_days=7)

    first, second = monitor.observe(window), monitor.observe(window)
    assert first == second


# --------------------------- what a halt actually does ---------------------- #

def _run(profile: str, seed: int, authority, tmp_path):
    """Reconcile a batch under a stated authority and gate every order."""
    from ledger_daemon import policy
    from ledger_daemon.evaluate import run_ledger_daemon
    from ledger_daemon.recon import ReconConfig, reconcile

    path = str(tmp_path / f"{profile}-{seed}-{authority}")
    generate(seed, 500, path, profile)
    orders, captures, bank, _truth = load_batch(path)
    result = reconcile(orders, captures, bank, q_hat=0.001,
                       config=ReconConfig(authority=authority))
    _ld, decisions = run_ledger_daemon(orders, result)
    return orders, result, decisions, policy


def test_a_halt_stops_the_fuzzy_path_and_only_the_fuzzy_path(tmp_path):
    from ledger_daemon.authority import AuthorityState

    orders, result, decisions, policy = _run(
        "clean", 42, AuthorityState.AUTOMATION_HALTED, tmp_path)

    halted = {oid for oid, d in decisions.items() if d.rule_fired == "R_DRIFT_HALT"}
    assert halted, "a halt that stops nothing is not a kill switch"

    for oid, decision in decisions.items():
        evidence = result.verdicts[oid].evidence
        if decision.rule_fired == "R_DRIFT_HALT":
            assert evidence.automation_path == "probabilistic"
            assert decision.outcome == policy.HOLD
        if decision.outcome == policy.ALLOW:
            # Nothing probabilistic may still be acting while authority is revoked.
            assert evidence.automation_path == "exact", oid


def test_exact_settlement_and_utr_proofs_survive_the_halt(tmp_path):
    from ledger_daemon.authority import AuthorityState

    _orders, result, calibrated, _policy = _run(
        "clean", 42, AuthorityState.CALIBRATED, tmp_path)
    _orders2, result2, halted, policy = _run(
        "clean", 42, AuthorityState.AUTOMATION_HALTED, tmp_path)

    exact = {oid for oid, v in result.verdicts.items()
             if v.evidence.automation_path == "exact"}
    assert exact
    for oid in exact:
        assert halted[oid].outcome == calibrated[oid].outcome
        assert halted[oid].rule_fired == calibrated[oid].rule_fired


def test_a_halt_never_causes_a_wrong_chase(tmp_path):
    """Revoking authority may only ever remove actions, never add them."""
    from ledger_daemon.authority import AuthorityState

    _o, _r, calibrated, policy = _run("clean", 42, AuthorityState.CALIBRATED, tmp_path)
    _o2, _r2, halted, _p = _run("clean", 42, AuthorityState.AUTOMATION_HALTED, tmp_path)

    before = {oid for oid, d in calibrated.items() if d.outcome == policy.ALLOW}
    after = {oid for oid, d in halted.items() if d.outcome == policy.ALLOW}
    assert after <= before


def test_a_warning_state_still_permits_the_fuzzy_path(tmp_path):
    from ledger_daemon.authority import AuthorityState

    _o, _r, warned, policy = _run("clean", 42, AuthorityState.WARNING, tmp_path)
    assert not any(d.rule_fired == "R_DRIFT_HALT" for d in warned.values())


def test_a_halt_turns_a_confident_paid_verdict_into_a_human_question(tmp_path):
    """A fuzzy 'already paid' is an assertion the halted matcher can no longer make.

    Before the halt it is a confident DENY; after, it is a HOLD -- the order
    moves from BLOCKED to NEEDS YOU rather than silently keeping a verdict
    whose threshold has been revoked.
    """
    from ledger_daemon.authority import AuthorityState

    _o, result, calibrated, policy = _run("clean", 42, AuthorityState.CALIBRATED, tmp_path)
    _o2, _r2, halted, _p = _run("clean", 42, AuthorityState.AUTOMATION_HALTED, tmp_path)

    fuzzy_paid = [oid for oid, v in result.verdicts.items()
                  if v.evidence.automation_path == "probabilistic"
                  and v.verdict.value in ("paid_out_of_band", "paid_net_of_tds",
                                          "refunded_then_repaid")]
    assert fuzzy_paid

    for oid in fuzzy_paid:
        assert calibrated[oid].outcome == policy.DENY
        assert halted[oid].outcome == policy.HOLD
        assert halted[oid].rule_fired == "R_DRIFT_HALT"


def test_a_halted_verdict_opens_an_exception_case(tmp_path):
    """F3 already routes holds to the workbench; a drift halt is no exception."""
    from ledger_daemon.authority import AuthorityState
    from ledger_daemon.cases import CaseStore, open_exception_cases

    _o, result, halted, _policy = _run(
        "clean", 42, AuthorityState.AUTOMATION_HALTED, tmp_path)
    store = CaseStore(str(tmp_path / "ledger.sqlite3"))
    opened = open_exception_cases(store, result.verdicts, halted)

    drifted = [oid for oid, d in halted.items() if d.rule_fired == "R_DRIFT_HALT"]
    assert drifted
    assert all(oid in opened for oid in drifted)
