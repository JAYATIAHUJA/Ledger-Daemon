"""The kill switch: who may run the fuzzy matcher, and who took it away (F5).

Probabilistic authority is not a property of the code, it is a claim about the
data: the conformal threshold (D5) and the rupee-risk calibration (F4) hold
only while the live stream is exchangeable with the batch they were fitted on.
`drift.py` measures whether that still looks true. This module decides what to
do about it, and records the decision.

The ladder is deliberately asymmetric:

    CALIBRATED --severe--> WARNING --severe--> DEGRADED --severe--> HALTED
                  ^                                |
                  +-- healthy --+                  +-- healthy x N AND a new
                                                       calibration id --> RECALIBRATED

Degrading is cheap and recovering is expensive, because the two errors are not
symmetric. Revoking authority on a stream that was fine costs some abstentions
a human has to clear; keeping it on a stream that has shifted means auto-
resolving payments with a threshold that no longer means what it says.

Three rules follow from that asymmetry, and each is enforced below:

* **One bad window is not a pattern.** WARNING keeps authority. It takes two
  consecutive severe windows to revoke it, which is what makes a single odd
  batch a note rather than an outage.
* **Only health heals.** A WARNING-severity window does not reset the severe
  counter. A feed that alternates severe and merely-warning is not recovering,
  and a controller that let it reset would never reach HALTED.
* **Recovery needs a new calibration, not just quiet.** Healthy windows alone
  cannot restore revoked authority: the fitted threshold is still the one from
  before the shift. Someone has to refit, and the new `calibration_id` is the
  evidence that they did.

Exact paths -- UTR joins, settlement-id aggregation -- are never revoked. They
do not depend on a fitted threshold, so distribution shift has no bearing on
them; that is the whole reason the system separates exact from probabilistic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from .drift import HEALTHY, SEVERE, DriftReport

# Consecutive healthy windows required before a refit can restore authority.
HEALTHY_WINDOWS_FOR_RECOVERY = 2

# Consecutive severe windows at which authority is revoked / the run is halted.
SEVERE_WINDOWS_TO_DEGRADE = 2
SEVERE_WINDOWS_TO_HALT = 3


class AuthorityState(str, Enum):
    CALIBRATED = "CALIBRATED"                  # fitted, and the stream still fits
    WARNING = "WARNING"                        # one severe window; authority retained
    DEGRADED = "DEGRADED"                      # authority revoked
    AUTOMATION_HALTED = "AUTOMATION_HALTED"    # revoked and latched
    RECALIBRATED = "RECALIBRATED"              # refitted after a halt; authority restored


# One entry per state, no catch-all -- the same discipline policy.py applies to
# the verdict taxonomy. A state that forgets to declare this would silently
# inherit whichever default the last author happened to pick.
AUTHORITY_GRANTS: dict[AuthorityState, bool] = {
    AuthorityState.CALIBRATED: True,
    AuthorityState.WARNING: True,
    AuthorityState.DEGRADED: False,
    AuthorityState.AUTOMATION_HALTED: False,
    AuthorityState.RECALIBRATED: True,
}

REVOKED_STATES = frozenset(
    state for state, granted in AUTHORITY_GRANTS.items() if not granted)


def _assert_total() -> None:
    missing = [state.value for state in AuthorityState if state not in AUTHORITY_GRANTS]
    if missing:
        raise ImportError(
            "AUTHORITY_GRANTS is not exhaustive; every AuthorityState must declare "
            f"whether it may run the fuzzy matcher. Missing: {sorted(missing)}")


_assert_total()


def probabilistic_authorized(state: AuthorityState) -> bool:
    """Whether the fuzzy matcher may auto-resolve under this state."""
    return AUTHORITY_GRANTS[state]


@dataclass(frozen=True)
class AuthorityDecision:
    state: AuthorityState
    previous: AuthorityState
    rule_fired: str
    detail: str
    calibration_id: str
    severity: str
    window_hash: str
    n: int
    consecutive_severe: int
    consecutive_healthy: int

    @property
    def changed(self) -> bool:
        return self.state is not self.previous


class AuthorityController:
    """Applies drift reports to the authority ladder, with hysteresis."""

    def __init__(self, calibration_id: str,
                 state: AuthorityState = AuthorityState.CALIBRATED):
        if not calibration_id:
            raise ValueError("authority must be anchored to a calibration id")
        self.calibration_id = calibration_id
        self.state = state
        self.consecutive_severe = 0
        self.consecutive_healthy = 0
        self.history: list[AuthorityDecision] = []

    @property
    def probabilistic_authorized(self) -> bool:
        return probabilistic_authorized(self.state)

    def apply(self, report: DriftReport, calibration_id: str) -> AuthorityDecision:
        previous = self.state

        if not report.usable:
            # Too little evidence to move on. Counters are left alone: a refused
            # window is neither a bad window nor a good one.
            return self._record(previous, "A0_WINDOW_REFUSED",
                                f"window of {report.n} rows is too small to score",
                                report)

        if report.severity == SEVERE:
            self.consecutive_severe += 1
            self.consecutive_healthy = 0
            return self._escalate(previous, report)

        if report.severity == HEALTHY:
            self.consecutive_healthy += 1
            self.consecutive_severe = 0
            return self._heal(previous, calibration_id, report)

        # WARNING severity: not evidence of a shift, and not evidence of health.
        # Deliberately leaves the severe counter standing (see module docstring).
        return self._record(previous, "A1_WATCH",
                            f"{report.severity.lower()} window; ladder unchanged", report)

    def _escalate(self, previous: AuthorityState, report: DriftReport) -> AuthorityDecision:
        if self.consecutive_severe >= SEVERE_WINDOWS_TO_HALT:
            self.state = AuthorityState.AUTOMATION_HALTED
            rule, detail = "A3_HALT", (
                f"{self.consecutive_severe} consecutive severe windows — "
                "probabilistic automation halted; exact paths continue")
        elif self.consecutive_severe >= SEVERE_WINDOWS_TO_DEGRADE:
            self.state = AuthorityState.DEGRADED
            rule, detail = "A2_DEGRADE", (
                f"{self.consecutive_severe} consecutive severe windows — "
                "probabilistic authority revoked")
        else:
            self.state = AuthorityState.WARNING
            rule, detail = "A1_WARN", (
                "one severe window — authority retained, stream under watch")
        return self._record(previous, rule, detail, report)

    def _heal(self, previous: AuthorityState, calibration_id: str,
              report: DriftReport) -> AuthorityDecision:
        if previous not in REVOKED_STATES:
            # Nothing was revoked, so nothing has to be refitted to come back.
            self.state = (AuthorityState.CALIBRATED
                          if previous is AuthorityState.WARNING else previous)
            return self._record(previous, "A5_CLEAR",
                                "healthy window; stream matches the calibration batch",
                                report)

        if self.consecutive_healthy < HEALTHY_WINDOWS_FOR_RECOVERY:
            return self._record(previous, "A4_RECOVERY_PENDING",
                                f"{self.consecutive_healthy} of "
                                f"{HEALTHY_WINDOWS_FOR_RECOVERY} healthy windows",
                                report)

        if calibration_id == self.calibration_id:
            return self._record(previous, "A4_RECOVERY_NEEDS_NEW_CALIBRATION",
                                "the stream is quiet again but the threshold is still "
                                "the one fitted before the shift — refit to restore",
                                report)

        self.calibration_id = calibration_id
        self.state = AuthorityState.RECALIBRATED
        self.consecutive_severe = 0
        self.consecutive_healthy = 0
        return self._record(previous, "A6_RECALIBRATED",
                            f"refitted as {calibration_id} after "
                            f"{HEALTHY_WINDOWS_FOR_RECOVERY} healthy windows",
                            report)

    def _record(self, previous: AuthorityState, rule: str, detail: str,
                report: DriftReport) -> AuthorityDecision:
        decision = AuthorityDecision(
            state=self.state, previous=previous, rule_fired=rule, detail=detail,
            calibration_id=self.calibration_id, severity=report.severity,
            window_hash=report.window_hash, n=report.n,
            consecutive_severe=self.consecutive_severe,
            consecutive_healthy=self.consecutive_healthy,
        )
        self.history.append(decision)
        return decision


AUTHORITY_SUBJECT = "AUTHORITY"


def audit_authority(executor, decision: AuthorityDecision) -> bool:
    """Append one authority transition to the same append-only log as the rest.

    The event id is content-addressed on the window that caused it, so replaying
    the same window twice cannot inflate the record -- the database rejects the
    duplicate, exactly as it does for executor actions (D9).
    """
    event_id = hashlib.sha256(
        f"authority|{decision.calibration_id}|{decision.window_hash}"
        f"|{decision.previous.value}|{decision.state.value}|{decision.rule_fired}".encode()
    ).hexdigest()
    return executor.audit_write(
        event_id, "authority", "drift-monitor", AUTHORITY_SUBJECT,
        {"window_hash": decision.window_hash, "severity": decision.severity,
         "calibration_id": decision.calibration_id, "n": decision.n},
        {"previous": decision.previous.value, "state": decision.state.value,
         "detail": decision.detail,
         "consecutive_severe": decision.consecutive_severe,
         "consecutive_healthy": decision.consecutive_healthy,
         "probabilistic_authorized": probabilistic_authorized(decision.state)},
        decision.rule_fired, decision.state.value, 0)
