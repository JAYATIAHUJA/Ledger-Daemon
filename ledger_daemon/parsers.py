"""Real bank statement exports -> the canonical bank_statement.csv (FR-1.2).

`python -m ledger_daemon import-statement <file> --out data/live` detects the
bank from the header row and rewrites the batch's bank feed, so `reconcile` and
`ui` run against the merchant's actual statement instead of settlement records.

Supported shapes (the common netbanking CSV exports):

  HDFC   Date, Narration, Value Dat(e), Debit Amount, Credit Amount,
         Chq/Ref Number, Closing Balance                       (dates DD/MM/YY)
  ICICI  S No., Value Date, Transaction Date, Cheque Number,
         Transaction Remarks, Withdrawal Amount (INR ),
         Deposit Amount (INR ), Balance (INR )                 (dates DD/MM/YYYY)
  canonical  the schema datagen writes; passed through after validation

Headers are matched case-insensitively on normalised names, so cosmetic export
differences (spacing, "Dat" vs "Date") do not break parsing. Amounts are parsed
as strings straight to integer paise — "1,23,456.78" never touches a float
(NFR: money.py's no-float contract holds at the ingestion boundary too).

Honesty note: these shapes are written to the commonly seen export layouts, not
to a published spec — banks change them without notice. The parser fails loudly
on an unrecognised header rather than guessing, and `--bank` forces a specific
one when detection is wrong.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import asdict

from .models import BankTxn
from .quarantine import QuarantineStore
from .source_contracts import SourceKind, validate_rows, write_source_manifest

_UTR_TOKEN = re.compile(r"\b([A-Z]{2,6}[0-9]{6,16}|[0-9]{12,16})\b")


class StatementError(ValueError):
    pass


def _norm(header: str) -> str:
    return re.sub(r"[^a-z]", "", header.lower())


def paise_from_str(raw: str) -> int:
    """'1,23,456.78' -> 12345678. String arithmetic only; no float ever."""
    s = raw.strip().replace(",", "").replace("₹", "")
    if not s or s in ("-", "0", "0.0", "0.00"):
        return 0
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if not re.fullmatch(r"[0-9]+(\.[0-9]{1,2})?", s):
        raise StatementError(f"unparseable amount: {raw!r}")
    rupees, _, frac = s.partition(".")
    return (-1 if neg else 1) * (int(rupees) * 100 + int(frac.ljust(2, "0") or "0"))


def _date_iso(raw: str) -> str:
    """DD/MM/YY, DD/MM/YYYY or DD-MM-YYYY -> YYYY-MM-DD. Already-ISO passes through."""
    s = raw.strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        return s
    m = re.fullmatch(r"([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{2}|[0-9]{4})", s)
    if not m:
        raise StatementError(f"unparseable date: {raw!r}")
    d, mo, y = m.groups()
    if len(y) == 2:
        y = "20" + y
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _utr_from(ref: str, narration: str) -> str:
    ref = ref.strip()
    if ref and ref not in ("0", "-", "000000000000"):
        return ref
    m = _UTR_TOKEN.search(narration.upper())
    return m.group(1) if m else ""


# ---- per-bank row shapes (normalised header name -> field) ------------------ #

_SHAPES = {
    "hdfc": {
        "required": {"narration", "debitamount", "creditamount"},
        "date": ("valuedat", "valuedate", "date"),
        "narration": ("narration",),
        "debit": ("debitamount",),
        "credit": ("creditamount",),
        "ref": ("chqrefnumber", "chqrefno", "refnumber"),
        "balance": ("closingbalance",),
    },
    "icici": {
        "required": {"transactionremarks", "withdrawalamountinr", "depositamountinr"},
        "date": ("valuedate", "transactiondate"),
        "narration": ("transactionremarks",),
        "debit": ("withdrawalamountinr",),
        "credit": ("depositamountinr",),
        "ref": ("chequenumber",),
        "balance": ("balanceinr",),
    },
    "canonical": {
        "required": {"txnid", "valuedate", "amountpaise", "creditdebit", "narration"},
    },
}


def detect_bank(headers: list[str]) -> str:
    normed = {_norm(h) for h in headers}
    for bank, shape in _SHAPES.items():
        if shape["required"] <= normed:
            return bank
    raise StatementError(
        "unrecognised statement header: " + ", ".join(headers[:8]) +
        " — pass --bank hdfc|icici|canonical to force a shape")


def _pick(row: dict[str, str], names: tuple[str, ...]) -> str:
    for n in names:
        if n in row:
            return row[n]
    return ""


def parse_statement(path: str, bank: str = "auto") -> list[BankTxn]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        # netbanking exports often carry preamble lines before the real header;
        # skip until a line that detects as a known shape
        rows = list(csv.reader(fh))
    header_idx = None
    for i, row in enumerate(rows[:20]):
        try:
            found = detect_bank(row)
        except StatementError:
            continue
        header_idx = i
        if bank == "auto":
            bank = found
        break
    if header_idx is None:
        raise StatementError(f"{path}: no recognisable header in the first 20 lines")
    if bank not in _SHAPES:
        raise StatementError(f"unknown bank {bank!r}; use hdfc|icici|canonical")

    headers = [_norm(h) for h in rows[header_idx]]
    txns: list[BankTxn] = []
    for n, raw in enumerate(rows[header_idx + 1:], start=1):
        if not any(cell.strip() for cell in raw):
            continue
        row = dict(zip(headers, raw))
        if bank == "canonical":
            txns.append(BankTxn(row["txnid"], row["valuedate"], int(row["amountpaise"]),
                                row["creditdebit"], row.get("utr", ""),
                                row["narration"], int(row.get("balanceafter", 0) or 0)))
            continue
        shape = _SHAPES[bank]
        narration = _pick(row, shape["narration"]).strip()
        debit = paise_from_str(_pick(row, shape["debit"]))
        credit = paise_from_str(_pick(row, shape["credit"]))
        if debit and credit:
            raise StatementError(f"{path} row {n}: both debit and credit set")
        balance_raw = _pick(row, shape["balance"]).strip()
        txns.append(BankTxn(
            txn_id=f"{bank.upper()}-{n:05d}",
            value_date=_date_iso(_pick(row, shape["date"])),
            amount_paise=credit or debit,
            credit_debit="credit" if credit else "debit",
            utr=_utr_from(_pick(row, shape["ref"]), narration),
            narration=narration,
            balance_after=paise_from_str(balance_raw) if balance_raw else 0,
        ))
    if not txns:
        raise StatementError(f"{path}: header recognised ({bank}) but no rows parsed")
    return txns


def write_bank_csv(txns: list[BankTxn], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    quarantine = QuarantineStore(os.path.join(out_dir, "quarantine.jsonl"))
    accepted, summary = validate_rows(
        SourceKind.BANK_TXN,
        [asdict(txn) for txn in txns],
        quarantine,
    )
    path = os.path.join(out_dir, "bank_statement.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["txn_id", "value_date", "amount_paise", "credit_debit", "utr",
                    "narration", "balance_after"])
        for row in accepted:
            w.writerow([row["txn_id"], row["value_date"], row["amount_paise"],
                        row["credit_debit"], row["utr"], row["narration"],
                        row["balance_after"]])
    write_source_manifest(out_dir, {SourceKind.BANK_TXN: summary})
    return path
