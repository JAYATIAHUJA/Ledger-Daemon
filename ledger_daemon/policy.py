"""Policy engine (FR-5): a deterministic gate in front of every money action.

Rules are evaluated in order; the first DENY/HOLD/ESCALATE wins. The default
outcome on any unhandled state is DENY, not ALLOW (FR-5.2). Every decision
records rule_fired.

R2 encodes "absence of evidence is not evidence of absence": if the bank feed
does not yet cover the window in which payment could have landed, we MUST NOT
chase (FR-5.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CHASEABLE_VERDICTS, Order, OrderVerdict
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


def evaluate(order: Order, verdict: OrderVerdict, action_type: str,
             attempts_so_far: int, contacts_7d: int,
             llm_confidence: float | None = None) -> Decision:
    # R1 — only GENUINELY_UNPAID / PARTIALLY_PAID may reach the executor (FR-3.1)
    if verdict.verdict not in CHASEABLE_VERDICTS:
        return Decision(DENY, "R1", f"verdict {verdict.verdict.value} is not chaseable")

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
