"""Whether the controller signs the close, and what it refuses to sign (F10).

A finance controller does not sign a set of books because the run finished. They
sign because specific things are true, and where they are not, they either
refuse or sign with the exception written down. This module is that decision as
a table:

  DO_NOT_SIGN         something is wrong that no note can make acceptable —
                      a proof that does not verify, an order with no proof at
                      all, a feed that never arrived, a rupee chased that had
                      already been paid, an action that fired twice, or a risk
                      bound outside its budget.
  SIGN_WITH_CAVEATS   the numbers hold, and something material is still open:
                      money held for a human, revoked automation authority,
                      quarantined rows, cases going stale.
  SIGN               nothing in either list.

Two design choices matter more than the thresholds:

**A blocker always outranks a caveat.** There is no arithmetic that trades one
against the other, because "we chased a paying customer, but only a little" is
not a sentence a controller gets to write.

**The decision carries hashes.** `signed_metrics_hash` binds the exact numbers
it saw and `proof_bundle_hash` binds the certificates they came from, so a
signoff cannot later be re-pointed at a different run. Change one basis point
and the hash changes with it.

Nothing here is a legal or statutory audit opinion. It is an operational gate
that says whether this batch is fit to act on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from .source_contracts import sha256_hex

#: Money held for a human above this is worth naming in the signoff.
MATERIAL_OPEN_PAISE = 1_00_000_00

#: A case nobody has moved in this many days is drifting, not being worked.
STALE_CASE_DAYS = 30

BLOCKER_CODES: dict[str, str] = {
    "PROOF_REJECTED": "a certificate failed independent verification",
    "PROOF_COVERAGE_INCOMPLETE": "an order was decided without a verifiable proof",
    "SOURCE_FEED_MISSING": "a source feed never arrived; absence of evidence is not evidence of absence",
    "WRONGLY_CHASED": "money was chased that the sources show had already arrived",
    "DUPLICATE_SIDE_EFFECT": "an action wrote more than once",
    "RISK_BUDGET_BREACHED": "the rupee-loss upper bound sits outside its budget",
    "NO_ROWS_PROCESSED": "the run reconciled nothing, so there is nothing to sign",
}

CAVEAT_CODES: dict[str, str] = {
    "MATERIAL_EXCEPTIONS_OPEN": "material money is held for a human and is not in these numbers",
    "PROBABILISTIC_AUTHORITY_REVOKED": "drift revoked the fuzzy matcher; only exact proofs ran",
    "RISK_AUTHORITY_WITHHELD": "no calibration met the rupee budget, so probabilistic decisions were held",
    "ROWS_QUARANTINED": "source rows failed closed and were excluded from the close",
    "CASES_STALE": "open cases have not moved in a long time",
    "DUPLICATE_SOURCE_ROWS": "the same source row was delivered more than once",
}


class SignoffStatus(str, Enum):
    SIGN = "SIGN"
    SIGN_WITH_CAVEATS = "SIGN_WITH_CAVEATS"
    DO_NOT_SIGN = "DO_NOT_SIGN"


@dataclass(frozen=True)
class SourceHealth:
    rows_offered: int
    accepted: int
    quarantined: int
    duplicates: int
    feeds_seen: int
    feeds_expected: int


@dataclass(frozen=True)
class ProofSummary:
    built: int
    verified: int
    rejected: int
    unverified: int
    bundle_hash: str


@dataclass(frozen=True)
class ExceptionSummary:
    open_cases: int
    material_open_paise: int
    oldest_open_days: int
    stale_cases: int


@dataclass(frozen=True)
class AuthoritySummary:
    state: str
    calibration_id: str
    probabilistic_halted: bool


@dataclass(frozen=True)
class RiskSummary:
    authorized: bool
    calibration_id: str
    loss_upper_bound_bp: int
    budget_bp: int
    wrongly_chased_paise: int
    duplicate_side_effects: int


@dataclass(frozen=True)
class SignoffDecision:
    status: SignoffStatus
    blockers: tuple[str, ...]
    caveats: tuple[str, ...]
    signed_metrics_hash: str
    proof_bundle_hash: str

    def to_dict(self) -> dict[str, object]:
        body = asdict(self)
        body["status"] = self.status.value
        body["blockers"] = list(self.blockers)
        body["caveats"] = list(self.caveats)
        return body

    def explain(self) -> list[tuple[str, str]]:
        """(code, sentence) for everything this decision is standing on."""
        return ([(code, BLOCKER_CODES[code]) for code in self.blockers]
                + [(code, CAVEAT_CODES[code]) for code in self.caveats])


def decide_signoff(source: SourceHealth, proofs: ProofSummary,
                   exceptions: ExceptionSummary, authority: AuthoritySummary,
                   risk: RiskSummary) -> SignoffDecision:
    """Same inputs, same status, every time. No thresholds are read at runtime."""
    blockers: list[str] = []
    caveats: list[str] = []

    if proofs.rejected > 0:
        blockers.append("PROOF_REJECTED")
    if proofs.unverified > 0:
        blockers.append("PROOF_COVERAGE_INCOMPLETE")
    if source.feeds_seen < source.feeds_expected:
        blockers.append("SOURCE_FEED_MISSING")
    if source.accepted <= 0:
        blockers.append("NO_ROWS_PROCESSED")
    if risk.wrongly_chased_paise != 0:
        blockers.append("WRONGLY_CHASED")
    if risk.duplicate_side_effects != 0:
        blockers.append("DUPLICATE_SIDE_EFFECT")
    if risk.loss_upper_bound_bp > risk.budget_bp:
        blockers.append("RISK_BUDGET_BREACHED")

    if exceptions.material_open_paise >= MATERIAL_OPEN_PAISE:
        caveats.append("MATERIAL_EXCEPTIONS_OPEN")
    if authority.probabilistic_halted or authority.state in (
            "DEGRADED", "AUTOMATION_HALTED"):
        caveats.append("PROBABILISTIC_AUTHORITY_REVOKED")
    if not risk.authorized:
        caveats.append("RISK_AUTHORITY_WITHHELD")
    if source.quarantined > 0:
        caveats.append("ROWS_QUARANTINED")
    if source.duplicates > 0:
        caveats.append("DUPLICATE_SOURCE_ROWS")
    if exceptions.stale_cases > 0 or exceptions.oldest_open_days > STALE_CASE_DAYS:
        caveats.append("CASES_STALE")

    if blockers:
        status = SignoffStatus.DO_NOT_SIGN
    elif caveats:
        status = SignoffStatus.SIGN_WITH_CAVEATS
    else:
        status = SignoffStatus.SIGN

    metrics_hash = sha256_hex({
        "source": asdict(source), "proofs": asdict(proofs),
        "exceptions": asdict(exceptions), "authority": asdict(authority),
        "risk": asdict(risk),
    })
    return SignoffDecision(status, tuple(blockers), tuple(caveats),
                           metrics_hash, proofs.bundle_hash)


def render_signoff(decision: SignoffDecision) -> str:
    lines = [f"CONTROLLER SIGNOFF: {decision.status.value}", ""]
    if decision.blockers:
        lines.append("Blocked by:")
        lines += [f"  [x] {code} — {BLOCKER_CODES[code]}" for code in decision.blockers]
        lines.append("")
    if decision.caveats:
        lines.append("Signed subject to:")
        lines += [f"  [!] {code} — {CAVEAT_CODES[code]}" for code in decision.caveats]
        lines.append("")
    if not decision.blockers and not decision.caveats:
        lines += ["Nothing outstanding: every proof verified, every source feed present,",
                  "no rupee chased that had already arrived, no action written twice.", ""]
    lines += [f"metrics  {decision.signed_metrics_hash[:16]}",
              f"proofs   {decision.proof_bundle_hash[:16]}",
              "",
              "An operational gate on whether this batch is fit to act on —",
              "not a statutory audit opinion."]
    return "\n".join(lines)
