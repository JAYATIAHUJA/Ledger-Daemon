"""Policy engine (FR-5): a deterministic gate in front of every money action.

Rules are evaluated in order; the first DENY/HOLD/ESCALATE wins. The default
outcome on any unhandled state is DENY, not ALLOW (FR-5.2). Every decision
records rule_fired.

R1 is a total function over the verdict taxonomy: VERDICT_DISPOSITION declares
one disposition per Verdict, with no catch-all, and _assert_exhaustive() turns a
forgotten verdict into an ImportError at module load. Adding a failure state
without deciding what collections does about it cannot reach production.

R2 encodes "absence of evidence is not evidence of absence": if the bank feed
does not yet cover the window in which payment could have landed, we MUST NOT
chase (FR-5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import assert_never

from .models import CHASEABLE_VERDICTS, Order, OrderVerdict, Verdict
from .recon import COST_WRONG_CHASE_PAISE, RECOVERY_RATE_DEN, RECOVERY_RATE_NUM

MAX_ATTEMPTS = 3
CONTACT_BUDGET_7D = 2
AFA_LIMIT_PAISE = 15_000_00       # RBI e-mandate AFA threshold
LLM_MIN_CONFIDENCE = 0.85

ALLOW, DENY, HOLD, ESCALATE = "ALLOW", "DENY", "HOLD", "ESCALATE"


@dataclass
class Decision:
    outcome: str      # ALLOW | DENY | HOLD | ESCALATE
    rule_fired: str
    detail: str = ""


class Disposition(str, Enum):
    """What R1 does with a verdict, before any of the later gates run.

    One entry per Verdict, no catch-all. Adding a 10th verdict without deciding
    its disposition is an ImportError at module load, not a silent DENY in
    production -- see _assert_exhaustive() below.
    """
    CHASE = "CHASE"                          # proceed to R2..R7
    BLOCK_ALREADY_PAID = "BLOCK_ALREADY_PAID"    # the money is provably in
    BLOCK_NOT_A_DEBT = "BLOCK_NOT_A_DEBT"        # never debited; retry, do not dun
    FREEZE_ESCALATE = "FREEZE_ESCALATE"          # dispute open; a human owns this
    BLOCK_STATUTORY_DEDUCTION = "BLOCK_STATUTORY_DEDUCTION"  # shortfall is withheld tax
    ABSTAIN_FOR_HUMAN = "ABSTAIN_FOR_HUMAN"      # not classifiable at alpha; exception list


VERDICT_DISPOSITION: dict[Verdict, Disposition] = {
    Verdict.SETTLED_CLEAN:        Disposition.BLOCK_ALREADY_PAID,
    Verdict.SETTLED_LATE:         Disposition.BLOCK_ALREADY_PAID,
    Verdict.PAID_OUT_OF_BAND:     Disposition.BLOCK_ALREADY_PAID,
    Verdict.REFUNDED_THEN_REPAID: Disposition.BLOCK_ALREADY_PAID,
    Verdict.PARTIALLY_PAID:       Disposition.CHASE,
    Verdict.GENUINELY_UNPAID:     Disposition.CHASE,
    Verdict.PAID_NET_OF_TDS:      Disposition.BLOCK_STATUTORY_DEDUCTION,
    Verdict.FAILED_NOT_DEBITED:   Disposition.BLOCK_NOT_A_DEBT,
    Verdict.CHARGEBACK_OPEN:      Disposition.FREEZE_ESCALATE,
    Verdict.AMBIGUOUS:            Disposition.ABSTAIN_FOR_HUMAN,
}


def _assert_exhaustive() -> None:
    """Fail at import if any Verdict has no declared disposition.

    This is the closest Python gets to a compile-time exhaustiveness check: the
    cost of forgetting is paid by whoever adds the verdict, at import, on their
    machine -- not by a merchant whose customer got chased for money they paid.
    """
    missing = [v.value for v in Verdict if v not in VERDICT_DISPOSITION]
    if missing:
        raise ImportError(
            "VERDICT_DISPOSITION is not exhaustive; every Verdict must declare "
            f"what the policy engine does with it. Missing: {sorted(missing)}"
        )
    stray = [v for v in VERDICT_DISPOSITION if not isinstance(v, Verdict)]
    if stray:
        raise ImportError(f"VERDICT_DISPOSITION has non-Verdict keys: {stray}")
    # CHASE must agree with the taxonomy's own chaseable set (models.py).
    chase = {v for v, d in VERDICT_DISPOSITION.items() if d is Disposition.CHASE}
    if chase != set(CHASEABLE_VERDICTS):
        raise ImportError(
            "VERDICT_DISPOSITION disagrees with CHASEABLE_VERDICTS: "
            f"{sorted(v.value for v in chase ^ set(CHASEABLE_VERDICTS))}"
        )


_assert_exhaustive()


def _r1(verdict: Verdict) -> Decision | None:
    """R1 -- taxonomy gate. Returns None only when the verdict may be chased.

    The match below has no `else`: assert_never() makes a static checker reject
    an unhandled Disposition, and raises at runtime if one slips through.
    """
    d = VERDICT_DISPOSITION[verdict]
    if d is Disposition.CHASE:
        return None
    if d is Disposition.BLOCK_ALREADY_PAID:
        return Decision(DENY, "R1_DENY_ALREADY_PAID",
                        f"{verdict.value}: money already received, chasing it is the harm")
    if d is Disposition.BLOCK_NOT_A_DEBT:
        return Decision(DENY, "R1_DENY_NOT_A_DEBT",
                        f"{verdict.value}: never debited, this is a retry not a dunning case")
    if d is Disposition.BLOCK_STATUTORY_DEDUCTION:
        return Decision(DENY, "R1_DENY_NET_OF_TDS",
                        f"{verdict.value}: the shortfall is statutory TDS the customer has "
                        "withheld and will deposit against your PAN — reconcile Form 26AS, "
                        "do not dun a legally compliant payer")
    if d is Disposition.FREEZE_ESCALATE:
        return Decision(ESCALATE, "R1_ESCALATE_DISPUTE_OPEN",
                        f"{verdict.value}: dispute in flight, collections must freeze")
    if d is Disposition.ABSTAIN_FOR_HUMAN:
        return Decision(HOLD, "R1_HOLD_AMBIGUOUS",
                        f"{verdict.value}: not classifiable at the calibrated error rate")
    assert_never(d)


def evaluate(order: Order, verdict: OrderVerdict, action_type: str,
             attempts_so_far: int, contacts_7d: int,
             llm_confidence: float | None = None) -> Decision:
    # R1 — taxonomy gate: exactly one declared disposition per verdict (FR-3.1)
    gate = _r1(verdict.verdict)
    if gate is not None:
        return gate

    # R2 — bank statement must cover due_date + 3 days
    if not verdict.bank_coverage_ok:
        return Decision(HOLD, "R2_HOLD_INSUFFICIENT_EVIDENCE",
                        "bank feed does not yet cover the window payment could land in")

    # R3 — attempt cap
    if attempts_so_far >= MAX_ATTEMPTS:
        return Decision(DENY, "R3_DENY_MAX_ATTEMPTS", f"{attempts_so_far} attempts already made")

    # R4 — contact budget, trailing 7 days
    if contacts_7d >= CONTACT_BUDGET_7D:
        return Decision(HOLD, "R4_HOLD_CONTACT_BUDGET", f"{contacts_7d} contacts in trailing 7d")

    # R5 — expected value must exceed cost of action (integer paise arithmetic)
    chase_amount = verdict.delta_due_paise or order.amount_paise
    expected_value = chase_amount * RECOVERY_RATE_NUM // RECOVERY_RATE_DEN
    if expected_value <= COST_WRONG_CHASE_PAISE:
        return Decision(DENY, "R5_DENY_NEGATIVE_EV",
                        f"EV {expected_value} paise <= action cost {COST_WRONG_CHASE_PAISE} paise")

    # R6 — auto-debit above the AFA limit requires a recurring mandate; else a human
    if action_type == "AUTO_CHARGE" and (
            order.channel_expected != "mandate" or chase_amount > AFA_LIMIT_PAISE):
        return Decision(ESCALATE, "R6_ESCALATE_AFA_HUMAN",
                        "auto-charge without qualifying mandate / above AFA limit")

    # R7 — LLM-derived verdicts need confidence >= 0.85
    if llm_confidence is not None and llm_confidence < LLM_MIN_CONFIDENCE:
        return Decision(HOLD, "R7_HOLD_FOR_HUMAN",
                        f"LLM confidence {llm_confidence:.2f} < {LLM_MIN_CONFIDENCE}")

    if action_type in ("CREATE_PAYMENT_LINK", "DRAFT_REMINDER", "AUTO_CHARGE"):
        return Decision(ALLOW, "R_ALLOW", f"all gates passed for {action_type}")

    return Decision(DENY, "R_DEFAULT_DENY", f"unhandled action type {action_type}")  # FR-5.2
