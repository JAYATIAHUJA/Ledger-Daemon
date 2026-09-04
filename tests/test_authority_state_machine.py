"""Probabilistic authority is granted by calibration and revoked by evidence.

The conformal and rupee-risk layers are only valid while the live stream looks
like the batch they were fitted on. When it stops looking like it, the honest
move is to stop trusting the fuzzy matcher -- explicitly, in a state a human
can see -- rather than to keep scoring and hope. Escalation is driven by
consecutive severe windows; recovery is deliberately harder than degradation.
"""

import pytest

from ledger_daemon.authority import (
    HEALTHY_WINDOWS_FOR_RECOVERY,
    AuthorityController,
    AuthorityState,
    probabilistic_authorized,
)
from ledger_daemon.drift import HEALTHY, SEVERE, UNDERSIZED, WARNING, DriftReport


def _report(severity: str, window: str = "w") -> DriftReport:
    return DriftReport(severity=severity, n=100, window_hash=window,
                       baseline_hash="base", signals=(),
                       usable=severity != UNDERSIZED)


@pytest.fixture
def controller():
    return AuthorityController("cal-1")


# --------------------------- escalation ------------------------------------- #

def test_three_consecutive_severe_windows_walk_to_halted(controller):
    assert controller.state is AuthorityState.CALIBRATED

    seen = [controller.apply(_report(SEVERE, f"w{i}"), "cal-1").state for i in range(3)]

    assert seen == [AuthorityState.WARNING, AuthorityState.DEGRADED,
                    AuthorityState.AUTOMATION_HALTED]


def test_authority_is_revoked_at_degraded_not_at_warning(controller):
    """One bad window is noise; two in a row is a pattern."""
    controller.apply(_report(SEVERE, "w1"), "cal-1")
    assert controller.probabilistic_authorized is True    # WARNING still trusts the matcher

    controller.apply(_report(SEVERE, "w2"), "cal-1")
    assert controller.state is AuthorityState.DEGRADED
    assert controller.probabilistic_authorized is False


def test_halted_is_a_floor_not_a_ceiling(controller):
    for i in range(6):
        controller.apply(_report(SEVERE, f"w{i}"), "cal-1")
    assert controller.state is AuthorityState.AUTOMATION_HALTED
    assert controller.probabilistic_authorized is False


def test_a_warning_window_never_counts_as_evidence_of_health(controller):
    """Alternating SEVERE/WARNING must still degrade -- only health heals."""
    for i in range(3):
        controller.apply(_report(SEVERE, f"s{i}"), "cal-1")
        controller.apply(_report(WARNING, f"w{i}"), "cal-1")
    assert controller.state is AuthorityState.AUTOMATION_HALTED


def test_an_undersized_window_changes_nothing(controller):
    controller.apply(_report(SEVERE, "w1"), "cal-1")
    before = controller.state

    decision = controller.apply(_report(UNDERSIZED, "tiny"), "cal-1")

    assert decision.rule_fired == "A0_WINDOW_REFUSED"
    assert controller.state is before
    assert decision.state is before


# --------------------------- recovery --------------------------------------- #

def test_a_healthy_window_clears_a_warning_without_recalibration(controller):
    controller.apply(_report(SEVERE, "w1"), "cal-1")
    assert controller.state is AuthorityState.WARNING

    decision = controller.apply(_report(HEALTHY, "w2"), "cal-1")

    assert decision.state is AuthorityState.CALIBRATED
    assert controller.probabilistic_authorized is True


def test_health_alone_cannot_restore_revoked_authority(controller):
    for i in range(3):
        controller.apply(_report(SEVERE, f"s{i}"), "cal-1")

    for i in range(HEALTHY_WINDOWS_FOR_RECOVERY + 2):
        decision = controller.apply(_report(HEALTHY, f"h{i}"), "cal-1")

    assert decision.rule_fired == "A4_RECOVERY_NEEDS_NEW_CALIBRATION"
    assert controller.state is AuthorityState.AUTOMATION_HALTED
    assert controller.probabilistic_authorized is False


def test_a_new_calibration_id_alone_cannot_restore_authority(controller):
    for i in range(3):
        controller.apply(_report(SEVERE, f"s{i}"), "cal-1")

    decision = controller.apply(_report(HEALTHY, "h0"), "cal-2")

    assert controller.state is AuthorityState.AUTOMATION_HALTED
    assert decision.state is AuthorityState.AUTOMATION_HALTED


def test_recovery_needs_healthy_windows_and_a_new_calibration(controller):
    for i in range(3):
        controller.apply(_report(SEVERE, f"s{i}"), "cal-1")
    for i in range(HEALTHY_WINDOWS_FOR_RECOVERY):
        decision = controller.apply(_report(HEALTHY, f"h{i}"), "cal-2")

    assert decision.state is AuthorityState.RECALIBRATED
    assert decision.calibration_id == "cal-2"
    assert controller.probabilistic_authorized is True


def test_recovery_resets_the_ladder(controller):
    for i in range(3):
        controller.apply(_report(SEVERE, f"s{i}"), "cal-1")
    for i in range(HEALTHY_WINDOWS_FOR_RECOVERY):
        controller.apply(_report(HEALTHY, f"h{i}"), "cal-2")

    # after recovery the next bad window starts the ladder over, at WARNING
    decision = controller.apply(_report(SEVERE, "s9"), "cal-2")
    assert decision.state is AuthorityState.WARNING


# --------------------------- the record ------------------------------------- #

def test_every_transition_is_explained_and_attributed(controller):
    decision = controller.apply(_report(SEVERE, "w1"), "cal-1")

    assert decision.previous is AuthorityState.CALIBRATED
    assert decision.state is AuthorityState.WARNING
    assert decision.rule_fired.startswith("A")
    assert decision.detail
    assert decision.window_hash == "w1"
    assert decision.severity == SEVERE
    assert controller.history[-1] is decision


def test_the_authority_table_is_total():
    """Every state must declare whether it may run the fuzzy matcher."""
    for state in AuthorityState:
        assert isinstance(probabilistic_authorized(state), bool)


def test_transitions_are_audited(tmp_path):
    from ledger_daemon.authority import audit_authority
    from ledger_daemon.executor import Executor

    execu = Executor(str(tmp_path / "ledger.sqlite3"))
    controller = AuthorityController("cal-1")
    decision = controller.apply(_report(SEVERE, "w1"), "cal-1")

    assert audit_authority(execu, decision) is True
    assert audit_authority(execu, decision) is False   # idempotent, DB-enforced
    rows = execu.audit("AUTHORITY")
    assert len(rows) == 1
    assert rows[0]["decision"] == AuthorityState.WARNING.value
    assert rows[0]["rule_fired"] == decision.rule_fired
