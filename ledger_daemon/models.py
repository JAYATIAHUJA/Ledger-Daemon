"""Domain models: the 9-verdict taxonomy (FR-3) and record types.

Exactly one verdict per order. The executor is reachable only for
GENUINELY_UNPAID and PARTIALLY_PAID (FR-3.1) — enforced by the policy engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Public domain-model compatibility: finance-event implementations live in
# their focused module but remain importable from ``models`` for consumers that
# treat this module as the aggregate domain boundary.
from .finance_events import Adjustment, Dispute, LedgerEntry, Refund, Settlement


class Verdict(str, Enum):
    SETTLED_CLEAN = "settled_clean"            # gw ok, bank ok, on time
    SETTLED_LATE = "settled_late"              # gw ok, bank ok, T+2/T+3   <- wrongly chaseable
    PAID_OUT_OF_BAND = "paid_out_of_band"      # gw absent, bank NEFT/UPI  <- wrongly chaseable
    REFUNDED_THEN_REPAID = "refunded_then_repaid"  # net settled           <- wrongly chaseable
    PARTIALLY_PAID = "partially_paid"          # chase only the delta
    PAID_NET_OF_TDS = "paid_net_of_tds"        # bank credit = invoice minus a
                                               # statutory TDS rate; the shortfall
                                               # lives in Form 26AS, not in dunning
    GENUINELY_UNPAID = "genuinely_unpaid"      # the ONLY clean chaseable state
    FAILED_NOT_DEBITED = "failed_not_debited"  # retry, do not dun
    CHARGEBACK_OPEN = "chargeback_open"        # freeze, escalate
    AMBIGUOUS = "ambiguous"                    # honest exception list


CHASEABLE_VERDICTS = frozenset({Verdict.GENUINELY_UNPAID, Verdict.PARTIALLY_PAID})

# Verdicts a naive duner would wrongly chase (used in the DCPR definition, §A6).
WRONGLY_CHASEABLE = frozenset(
    {Verdict.SETTLED_LATE, Verdict.PAID_OUT_OF_BAND, Verdict.REFUNDED_THEN_REPAID,
     Verdict.PAID_NET_OF_TDS}
)


@dataclass
class Order:
    order_id: str
    invoice_no: str
    customer_id: str
    customer_name: str
    amount_paise: int
    due_date: str          # YYYY-MM-DD
    status: str            # what the merchant's books believe: paid|unpaid|partial
    channel_expected: str  # gateway|bank_transfer|mandate


@dataclass
class GatewayCapture:
    payment_id: str
    order_id: str
    amount_paise: int
    fee_paise: int
    tax_paise: int
    status: str            # captured|failed|refund|chargeback_open
    method: str            # card|upi|netbanking
    captured_at: str       # YYYY-MM-DD
    settlement_id: str     # empty if never settled
    utr: str               # empty until bank-settled


@dataclass
class BankTxn:
    txn_id: str
    value_date: str        # YYYY-MM-DD
    amount_paise: int
    credit_debit: str      # credit|debit
    utr: str
    narration: str
    balance_after: int


@dataclass
class Evidence:
    """One scored justification for a verdict: the source rows and the arithmetic."""
    pass_used: str
    source_rows: list[str] = field(default_factory=list)
    detail: str = ""
    weight_waterfall: list[tuple[str, str]] = field(default_factory=list)  # (component, contribution)
    # Automation provenance is durable verdict evidence. Deterministic proofs
    # need no calibration; fuzzy scores do.
    automation_path: str = "manual"       # exact | probabilistic | manual
    risk_calibration_id: str = ""
    risk_authorized: bool = False
    score_ppm: int = 0                     # persisted score; never a float
    authority_state: str = ""              # drift authority this decision ran under

    def __post_init__(self) -> None:
        if self.automation_path not in {"exact", "probabilistic", "manual"}:
            raise ValueError("automation_path must be exact, probabilistic, or manual")
        if type(self.risk_calibration_id) is not str:
            raise TypeError("risk_calibration_id must be str")
        if type(self.risk_authorized) is not bool:
            raise TypeError("risk_authorized must be bool")
        if type(self.score_ppm) is not int or not 0 <= self.score_ppm <= 1_000_000:
            raise ValueError("score_ppm must be an integer in 0..1000000")


@dataclass
class OrderVerdict:
    order_id: str
    verdict: Verdict
    evidence_refs: list[str]
    evidence: Evidence
    money_received_paise: int = 0
    delta_due_paise: int = 0          # for PARTIALLY_PAID: what may actually be chased
    p_match: str = ""                  # rendered probability (string — never used in money math)
    reason: str = ""
    bank_coverage_ok: bool = True      # False -> policy R2 must HOLD (absence of evidence
                                       # is not evidence of absence)
    certificate_id: str = ""           # populated when the immutable proof bundle is emitted
