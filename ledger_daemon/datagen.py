"""Synthetic batch generator (FR-1).

Builds: clean orders -> fee/tax -> gateway rows -> bank rows -> then injects each
messiness pattern one at a time, writing the ground-truth label AT INJECTION TIME.
Same seed => byte-identical files (FR-1.2). All money is integer paise.

Messiness distribution (§A7, n=500):
  clean settle T+1 55% | late settle T+2/T+3 8% | out-of-band NEFT/UPI 6%
  split settlement 7%  | partial refund 4%      | refund then repay 2%
  duplicate UTR 3%     | chargeback open 2%     | genuinely unpaid 10%
  failed, never debited 3%
"""

from __future__ import annotations

import csv
import os
import random
from dataclasses import asdict

from .models import BankTxn, GatewayCapture, Order, Verdict
from .money import add, pct_bp, sub

ASOF = "2026-08-28"          # reconciliation run date
STATEMENT_END = "2026-08-28"  # last value_date covered by the bank feed

_FIRST = ["SHARMA", "GUPTA", "MEHTA", "IYER", "REDDY", "KHAN", "PATEL", "SINGH",
          "RAO", "NAIR", "BOSE", "DAS", "JAIN", "AGARWAL", "MISHRA", "PILLAI",
          "KULKARNI", "DESAI", "CHOPRA", "MALHOTRA", "SAXENA", "VERMA", "TIWARI",
          "BHATT", "JOSHI", "SETH", "ANAND", "KAPOOR", "MENON", "BAJAJ"]
_KIND = ["TEXTILES", "TRADERS", "ENTERPRISES", "INDUSTRIES", "EXPORTS", "AGENCIES",
         "DISTRIBUTORS", "ELECTRICALS", "PHARMA", "FOODS", "LOGISTICS", "MOTORS",
         "STEELS", "PLASTICS", "PACKAGING", "INFOTECH"]
_SUFFIX = ["PVT LTD", "LLP", "AND SONS", "AND CO", ""]

_BANKS = ["AXIS", "HDFC", "ICIC", "SBIN", "KKBK", "UTIB", "YESB", "IDFB"]
_VPA_HOSTS = ["okhdfc", "oksbi", "okaxis", "ybl", "paytm", "ibl"]


def _day(d: int) -> str:
    return f"2026-08-{d:02d}"


def _dayplus(date: str, delta: int) -> str:
    d = int(date[8:10]) + delta
    m = 8
    if d > 31:
        d -= 31
        m = 9
    return f"2026-{m:02d}-{d:02d}"


class _Gen:
    def __init__(self, seed: int, n: int, profile: str = "clean"):
        self.rng = random.Random(seed)
        self.n = n
        self.profile = profile  # "clean" = SRS §A7 | "stress" = degraded narrations,
                                # amount-collision decoys; same verdict distribution
        self.orders: list[Order] = []
        self.captures: list[GatewayCapture] = []
        self.bank: list[BankTxn] = []
        self.truth: list[dict] = []
        self._txn_no = 80000
        self._pay_no = 40000
        self._set_no = 7000

    # ---------- id helpers ----------
    def _utr(self) -> str:
        bank = self.rng.choice(_BANKS)
        return f"{bank}N{self.rng.randrange(10**8, 10**9)}"

    def _payment_id(self) -> str:
        self._pay_no += 1
        return f"pay_LD{self._pay_no}"

    def _txn_id(self) -> str:
        self._txn_no += 1
        return f"TXN-{self._txn_no}"

    def _settlement_id(self) -> str:
        self._set_no += 1
        return f"rzp_settlement_{self._set_no}"

    def _name(self) -> str:
        return " ".join(
            p for p in (self.rng.choice(_FIRST), self.rng.choice(_KIND), self.rng.choice(_SUFFIX)) if p
        )

    def _amount(self) -> int:
        # ₹500 .. ₹5,00,000 in whole rupees -> paise
        return self.rng.randrange(500, 500_000) * 100

    # ---------- row emitters ----------
    def _order(self, i: int, name: str, amount: int, status: str, channel: str) -> Order:
        due = _day(self.rng.randrange(3, 24))
        o = Order(
            order_id=f"ORD-{1000 + i}",
            invoice_no=f"INV-{2000 + i}",
            customer_id=f"CUST-{500 + i}",
            customer_name=name,
            amount_paise=amount,
            due_date=due,
            status=status,
            channel_expected=channel,
        )
        self.orders.append(o)
        return o

    def _capture(self, o: Order, status: str = "captured", amount: int | None = None,
                 settlement_id: str = "", utr: str = "", day_offset: int = 0,
                 fee: bool = True) -> GatewayCapture:
        amt = o.amount_paise if amount is None else amount
        f = pct_bp(amt, 200) if fee and amt > 0 else 0
        t = pct_bp(f, 1800) if fee and amt > 0 else 0
        c = GatewayCapture(
            payment_id=self._payment_id(),
            order_id=o.order_id,
            amount_paise=amt,
            fee_paise=f,
            tax_paise=t,
            status=status,
            method=self.rng.choice(["card", "upi", "netbanking"]),
            captured_at=_dayplus(o.due_date, day_offset),
            settlement_id=settlement_id,
            utr=utr,
        )
        self.captures.append(c)
        return c

    def _bank_credit(self, value_date: str, amount: int, utr: str, narration: str) -> BankTxn:
        b = BankTxn(
            txn_id=self._txn_id(),
            value_date=value_date,
            amount_paise=amount,
            credit_debit="credit",
            utr=utr,
            narration=narration,
            balance_after=0,  # filled after sorting
        )
        self.bank.append(b)
        return b

    def _typo(self, s: str) -> str:
        """1-2 keyboard-ish edits: swap, drop, or double a character."""
        chars = list(s)
        for _ in range(self.rng.randrange(1, 3)):
            if len(chars) < 4:
                break
            i = self.rng.randrange(1, len(chars) - 1)
            op = self.rng.randrange(3)
            if op == 0:
                chars[i], chars[i - 1] = chars[i - 1], chars[i]
            elif op == 1:
                del chars[i]
            else:
                chars.insert(i, chars[i])
        return "".join(chars)

    def _oob_narration(self, o: Order, with_invoice: bool, truncate: bool) -> str:
        name = o.customer_name.replace(" PVT LTD", " PVT").replace(" AND CO", "")
        if truncate:
            cut = self.rng.randrange(3, 9) if self.profile == "stress" else self.rng.randrange(2, 5)
            name = name[: max(8, len(name) - cut)]
        if self.profile == "stress":
            name = self._typo(name)
        inv = "".join(ch for ch in o.invoice_no if ch.isdigit())
        style = self.rng.randrange(3)
        if style == 0:
            s = f"NEFT-{self.rng.choice(_BANKS)}P{self.rng.randrange(10**7, 10**8)}-{name}"
            if with_invoice:
                s += f"-INV{inv}"
            return s
        if style == 1:
            s = f"IMPS/P2A/{self.rng.randrange(10**8, 10**9)}/{name}"
            if with_invoice:
                s += f"/INV {inv}"
            return s
        vpa = name.split()[0].lower() + "@" + self.rng.choice(_VPA_HOSTS)
        s = f"UPI/{self.rng.randrange(10**11, 10**12)}/Payment from/{vpa}"
        if with_invoice:
            s += f"/INV{inv}"
        return s

    def _label(self, o: Order, verdict: Verdict, money_received: bool, chaseable: bool) -> None:
        self.truth.append({
            "order_id": o.order_id,
            "true_verdict": verdict.value,
            "money_received_bool": str(money_received).lower(),
            "chaseable_bool": str(chaseable).lower(),
        })

    def _settle_to_bank(self, captures: list[GatewayCapture], delta_days: int) -> None:
        """One settlement credit for a group of captures sharing a settlement_id."""
        sid = self._settlement_id()
        utr = self._utr()
        total = 0
        latest = captures[0].captured_at
        for c in captures:
            c.settlement_id = sid
            c.utr = utr
            total = add(total, sub(sub(c.amount_paise, c.fee_paise), c.tax_paise))
            if c.captured_at > latest:
                latest = c.captured_at
        self._bank_credit(
            value_date=_dayplus(latest, delta_days),
            amount=total,
            utr=utr,
            narration=f"RAZORPAYSETTLEMENT-{sid}",
        )

    # ---------- patterns ----------
    def build(self) -> None:
        n = self.n
        counts = {
            "clean": int(n * 0.55),
            "late": int(n * 0.08),
            "oob": int(n * 0.06),
            "tds": int(n * 0.04),
            "split": int(n * 0.07),
            "partial_refund": int(n * 0.04),
            "refund_repay": int(n * 0.02),
            "dup_utr": int(n * 0.03) // 2 * 2,  # pairs
            "chargeback": int(n * 0.02),
            "unpaid": int(n * 0.10),
            "failed": int(n * 0.03),
        }
        counts["clean"] += n - sum(counts.values())  # remainder -> clean
        i = 0

        # clean settle T+1
        for _ in range(counts["clean"]):
            channel = "mandate" if self.rng.random() < 0.1 else "gateway"
            o = self._order(i, self._name(), self._amount(), "paid", channel); i += 1
            c = self._capture(o, day_offset=0)
            self._settle_to_bank([c], delta_days=1)
            self._label(o, Verdict.SETTLED_CLEAN, True, False)

        # late settle T+2/T+3 — books still say unpaid at recon time
        for _ in range(counts["late"]):
            o = self._order(i, self._name(), self._amount(), "unpaid", "gateway"); i += 1
            c = self._capture(o, day_offset=0)
            self._settle_to_bank([c], delta_days=self.rng.choice([2, 3]))
            self._label(o, Verdict.SETTLED_LATE, True, False)

        # out-of-band NEFT/IMPS/UPI — no gateway row at all
        for k in range(counts["oob"]):
            o = self._order(i, self._name(), self._amount(), "unpaid", "bank_transfer"); i += 1
            if self.profile == "stress":
                with_inv = k % 4 == 0      # only 1/4 carry an invoice number
                truncate = True
            else:
                with_inv = k % 3 != 2      # 1/3 have name only (fuzzy-only path)
                truncate = k % 2 == 0
            self._bank_credit(
                value_date=_dayplus(o.due_date, self.rng.randrange(0, 3)),
                amount=o.amount_paise,
                utr=self._utr(),
                narration=self._oob_narration(o, with_inv, truncate),
            )
            self._label(o, Verdict.PAID_OUT_OF_BAND, True, False)

        # B2B payer deducts statutory TDS and pays the net by bank transfer —
        # no gateway row; books say unpaid; the shortfall is withheld tax,
        # recoverable from Form 26AS, and chasing it insults a compliant payer
        for k in range(counts["tds"]):
            o = self._order(i, self._name(), self._amount(), "unpaid", "bank_transfer"); i += 1
            rate_bp = self.rng.choice([200, 1000])   # 2% services / 10% professional fees
            net = sub(o.amount_paise, pct_bp(o.amount_paise, rate_bp))
            with_inv = k % 3 != 2
            self._bank_credit(
                value_date=_dayplus(o.due_date, self.rng.randrange(0, 3)),
                amount=net,
                utr=self._utr(),
                narration=self._oob_narration(o, with_inv, k % 2 == 0),
            )
            self._label(o, Verdict.PAID_NET_OF_TDS, True, False)

        # split settlement: 2-4 captures share one settlement_id -> one credit
        remaining = counts["split"]
        while remaining > 0:
            g = min(remaining, self.rng.choice([2, 3, 4]))
            members = []
            for _ in range(g):
                members.append(self._order(i, self._name(), self._amount(), "paid", "gateway")); i += 1
            # a settlement batch covers one capture day: captures share captured_at
            batch_day = max(m.due_date for m in members)
            group = []
            for o in members:
                c = self._capture(o, day_offset=0)
                c.captured_at = batch_day
                group.append((o, c))
            self._settle_to_bank([c for _, c in group], delta_days=1)
            for o, _ in group:
                self._label(o, Verdict.SETTLED_CLEAN, True, False)
            remaining -= g

        # partial refund: capture + partial gateway refund, settlement nets the two
        for _ in range(counts["partial_refund"]):
            o = self._order(i, self._name(), self._amount(), "partial", "gateway"); i += 1
            c = self._capture(o, day_offset=0)
            refund = (o.amount_paise // 400) * 100  # ~25%, whole rupees
            r = self._capture(o, status="refund", amount=-refund, day_offset=1, fee=False)
            self._settle_to_bank([c, r], delta_days=1)
            self._label(o, Verdict.PARTIALLY_PAID, True, True)

        # refund then repay: capture + full refund (settlement nets to 0 credit is
        # suppressed) + customer repays gross via NEFT
        for _ in range(counts["refund_repay"]):
            o = self._order(i, self._name(), self._amount(), "unpaid", "gateway"); i += 1
            c = self._capture(o, day_offset=0)
            net = sub(sub(c.amount_paise, c.fee_paise), c.tax_paise)
            self._capture(o, status="refund", amount=-net, day_offset=1, fee=False)
            # no settlement credit (nets to zero); repayment arrives out-of-band
            self._bank_credit(
                value_date=_dayplus(o.due_date, 3),
                amount=o.amount_paise,
                utr=self._utr(),
                narration=self._oob_narration(o, True, False),
            )
            self._label(o, Verdict.REFUNDED_THEN_REPAID, True, False)

        # duplicate UTR pairs: two similar customers, same amount, two credits
        # sharing one UTR, name-only narrations -> must resolve AMBIGUOUS
        for _ in range(counts["dup_utr"] // 2):
            base = self.rng.choice(_FIRST)
            kind = self.rng.choice(_KIND)
            amt = self._amount()
            o1 = self._order(i, f"{base} {kind}", amt, "unpaid", "bank_transfer"); i += 1
            o2 = self._order(i, f"{base[0]} {base[1:]} {kind}", amt, "unpaid", "bank_transfer"); i += 1
            utr = self._utr()
            for o in (o1, o2):
                self._bank_credit(
                    value_date=_dayplus(o.due_date, 1),
                    amount=amt,
                    utr=utr,
                    narration=self._oob_narration(o, False, True),
                )
                self._label(o, Verdict.AMBIGUOUS, True, False)

        # chargeback open: captured but disputed -> freeze
        for _ in range(counts["chargeback"]):
            o = self._order(i, self._name(), self._amount(), "paid", "gateway"); i += 1
            self._capture(o, status="chargeback_open", day_offset=0)
            self._label(o, Verdict.CHARGEBACK_OPEN, True, False)

        # genuinely unpaid: nothing anywhere. Two of them fall due so late that the
        # statement can't cover due_date+3 yet -> the R2 coverage HOLD is exercised.
        unpaid_orders = []
        for k in range(counts["unpaid"]):
            o = self._order(i, self._name(), self._amount(), "unpaid",
                            self.rng.choice(["gateway", "bank_transfer"])); i += 1
            if k < 2:
                o.due_date = "2026-08-27"  # due_date+3 beyond statement end
            unpaid_orders.append(o)
            self._label(o, Verdict.GENUINELY_UNPAID, False, True)

        # stress only: amount-collision decoys — unrelated credits whose amount
        # exactly equals a genuinely-unpaid order's invoice. A matcher that leans
        # on amount alone will wrongly mark these paid.
        if self.profile == "stress":
            for o in unpaid_orders[2:12]:
                self._bank_credit(
                    value_date=_dayplus(o.due_date, self.rng.randrange(0, 3)),
                    amount=o.amount_paise,
                    utr=self._utr(),
                    narration=f"NEFT-{self.rng.choice(_BANKS)}P{self.rng.randrange(10**7, 10**8)}"
                              f"-{self._name()}-ADVANCE",
                )

        # failed, never debited: gateway failure, no bank row -> retry, not dun
        for _ in range(counts["failed"]):
            o = self._order(i, self._name(), self._amount(), "unpaid", "gateway"); i += 1
            self._capture(o, status="failed", day_offset=0)
            self._label(o, Verdict.FAILED_NOT_DEBITED, False, False)

        # decoy bank noise: rent, salary, vendor debits + unrelated credits
        for _ in range(30):
            amt = self.rng.randrange(1_000, 300_000) * 100
            day = _day(self.rng.randrange(2, 27))
            if self.rng.random() < 0.5:
                self.bank.append(BankTxn(self._txn_id(), day, amt, "debit", self._utr(),
                                         self.rng.choice([
                                             "NEFT-RENT-AUG-PROPCARE LLP",
                                             "SAL-2026-08 PAYROLL BATCH",
                                             "IMPS/P2A/VENDOR PAYMENT/GSTIN",
                                             "ACH-D-ELECTRICITYBOARD",
                                         ]), 0))
            else:
                self._bank_credit(day, amt, self._utr(),
                                  f"NEFT-{self.rng.choice(_BANKS)}P{self.rng.randrange(10**7, 10**8)}-MISC RECEIPT")

        # sort + running balance (deterministic)
        self.bank.sort(key=lambda b: (b.value_date, b.txn_id))
        bal = 10_00_00_000_00  # ₹10 crore opening, in paise
        for b in self.bank:
            bal = bal + b.amount_paise if b.credit_debit == "credit" else bal - b.amount_paise
            b.balance_after = bal


def generate(seed: int, n: int, out_dir: str, profile: str = "clean") -> dict[str, str]:
    """Write the four CSVs (FR-1.1). Returns {name: path}."""
    g = _Gen(seed, n, profile)
    g.build()
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    def write(name: str, rows: list[dict], fields: list[str]) -> None:
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        paths[name] = path

    write("merchant_orders.csv", [asdict(o) for o in g.orders],
          ["order_id", "invoice_no", "customer_id", "customer_name", "amount_paise",
           "due_date", "status", "channel_expected"])
    write("gateway_captures.csv", [asdict(c) for c in g.captures],
          ["payment_id", "order_id", "amount_paise", "fee_paise", "tax_paise",
           "status", "method", "captured_at", "settlement_id", "utr"])
    write("bank_statement.csv", [asdict(b) for b in g.bank],
          ["txn_id", "value_date", "amount_paise", "credit_debit", "utr",
           "narration", "balance_after"])
    write("ground_truth.csv", g.truth,
          ["order_id", "true_verdict", "money_received_bool", "chaseable_bool"])
    return paths


def load_batch(dir_path: str) -> tuple[list[Order], list[GatewayCapture], list[BankTxn], dict[str, dict]]:
    def rows(name):
        with open(os.path.join(dir_path, name), newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    orders = [Order(r["order_id"], r["invoice_no"], r["customer_id"], r["customer_name"],
                    int(r["amount_paise"]), r["due_date"], r["status"], r["channel_expected"])
              for r in rows("merchant_orders.csv")]
    captures = [GatewayCapture(r["payment_id"], r["order_id"], int(r["amount_paise"]),
                               int(r["fee_paise"]), int(r["tax_paise"]), r["status"],
                               r["method"], r["captured_at"], r["settlement_id"], r["utr"])
                for r in rows("gateway_captures.csv")]
    bank = [BankTxn(r["txn_id"], r["value_date"], int(r["amount_paise"]), r["credit_debit"],
                    r["utr"], r["narration"], int(r["balance_after"]))
            for r in rows("bank_statement.csv")]
    truth = {}
    try:
        truth = {r["order_id"]: r for r in rows("ground_truth.csv")}
    except FileNotFoundError:
        pass
    return orders, captures, bank, truth
