"""Ingest real Razorpay test-mode data into the canonical batch schema (FR-1).

`python -m ledger_daemon ingest --out data/live` pulls Orders, Payments and
Settlements from the Razorpay API using the same RZP_TEST_KEY_ID /
RZP_TEST_KEY_SECRET pair the executor uses, and writes the exact CSVs
`load_batch()` reads — so `reconcile` runs unchanged on real gateway data.

Honesty about the three sources:

  * merchant_orders.csv  <- /v1/orders        REAL gateway-side order records
  * gateway_captures.csv <- /v1/payments      REAL captures/failures/refunds
  * bank_statement.csv   <- /v1/settlements   settlement credits STANDING IN for
                            the bank feed: each processed settlement is the
                            credit line the merchant's statement would show,
                            UTR included. Drop in a real statement export to
                            replace it; the schema is the contract.

No ground_truth.csv is written: real data has no oracle, and pretending
otherwise would poison every honest metric downstream. `load_batch` tolerates
its absence; evaluation stays a synthetic-world activity.

stdlib only (urllib), same as the executor. Secrets are read from the
environment and never logged.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .quarantine import QuarantineStore
from .finance_events import FinanceEvent, decode_finance_event
from .source_contracts import (
    SourceKind, SourceValidationError, validate_row, validate_rows,
    write_source_manifest,
)

API = "https://api.razorpay.com/v1"
PAGE = 100


class IngestError(RuntimeError):
    pass


def decode_finance_events(rows: list[object],
                          quarantine: QuarantineStore) -> list[FinanceEvent]:
    """Decode a mixed finance-event feed, quarantining every invalid row.

    A row is never silently skipped: unsupported event types, malformed
    schemas, and invalid money all become durable quarantine records.
    """
    valid_rows, _summary = validate_rows(SourceKind.FINANCE_EVENT, rows, quarantine)
    return [decode_finance_event(row) for row in valid_rows]


def _credentials() -> tuple[str, str]:
    key_id = os.environ.get("RZP_TEST_KEY_ID", "")
    key_secret = os.environ.get("RZP_TEST_KEY_SECRET", "")
    if not key_id.startswith("rzp_test_") or not key_secret:
        raise IngestError(
            "set RZP_TEST_KEY_ID (rzp_test_...) and RZP_TEST_KEY_SECRET to ingest; "
            "live-mode keys are refused by design — this tool only reads test mode"
        )
    return key_id, key_secret


def _get(path: str, key_id: str, key_secret: str, **params) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{API}/{path}?{qs}" if qs else f"{API}/{path}")
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise IngestError(f"GET /{path} -> HTTP {exc.code}: {exc.read().decode()[:200]}") from exc
    except urllib.error.URLError as exc:
        raise IngestError(f"GET /{path} failed: {exc.reason}") from exc


def fetch_all(entity: str, key_id: str, key_secret: str, limit: int = 1000) -> list[dict]:
    """Paginate /v1/<entity> newest-first until exhausted or `limit` rows."""
    rows: list[dict] = []
    while len(rows) < limit:
        batch = _get(entity, key_id, key_secret, count=PAGE, skip=len(rows)).get("items", [])
        rows.extend(batch)
        if len(batch) < PAGE:
            break
    return rows[:limit]


# ---- pure mapping functions: API JSON -> canonical rows (unit-tested) -------- #

def _day(epoch: int | None) -> str:
    if type(epoch) is not int or epoch <= 0:
        raise SourceValidationError("MISSING_DATE", "source created_at must be a positive epoch")
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def order_row(o: dict) -> dict:
    notes = o.get("notes") or {}
    if isinstance(notes, list):
        notes = {}
    row = {
        "order_id": o["id"],
        "invoice_no": o.get("receipt") or o["id"],
        "customer_id": notes.get("customer_id", ""),
        "customer_name": notes.get("customer_name", ""),
        "amount_paise": o["amount"],               # API amounts are already paise
        "due_date": _day(o.get("created_at")),
        "status": "paid" if o.get("status") == "paid" else "unpaid",
        "channel_expected": "gateway",
    }
    validate_row(SourceKind.ORDER, row)
    return row


_STATUS = {"captured": "captured", "failed": "failed", "refunded": "refund"}


def capture_row(p: dict) -> dict | None:
    """One canonical capture per payment; None for states recon cannot use
    (created/authorized are in-flight, not evidence either way)."""
    status = _STATUS.get(p.get("status", ""))
    if status is None or not p.get("order_id"):
        return None
    acquirer = p.get("acquirer_data") or {}
    amount = p["amount"] if status != "refund" else -p["amount"]
    row = {
        "payment_id": p["id"],
        "order_id": p["order_id"],
        "amount_paise": amount,
        "fee_paise": p.get("fee") or 0,
        "tax_paise": p.get("tax") or 0,
        "status": status,
        "method": p.get("method", ""),
        "captured_at": _day(p.get("created_at")),
        "settlement_id": "",   # joined from /v1/settlements via UTR, not per-payment
        "utr": acquirer.get("utr") or acquirer.get("rrn") or "",
    }
    validate_row(SourceKind.CAPTURE, row)
    return row


def bank_row(s: dict) -> dict | None:
    """A processed settlement, rendered as the bank credit it becomes."""
    if s.get("status") != "processed":
        return None
    row = {
        "txn_id": s["id"],
        "value_date": _day(s.get("created_at")),
        "amount_paise": s["amount"],
        "credit_debit": "credit",
        "utr": s.get("utr") or "",
        # the marker recon uses to keep settlements out of the fuzzy pool
        "narration": f"RAZORPAYSETTLEMENT {s['id']}",
        "balance_after": 0,    # a statement export carries this; settlements do not
    }
    validate_row(SourceKind.BANK_TXN, row)
    return row


def write_batch(out_dir: str, orders: list[dict], captures: list[dict],
                bank: list[dict], *,
                finance_events: list[dict[str, object]] | None = None) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    quarantine = QuarantineStore(os.path.join(out_dir, "quarantine.jsonl"))
    spec = [
        ("merchant_orders.csv", SourceKind.ORDER, orders,
         ["order_id", "invoice_no", "customer_id", "customer_name", "amount_paise",
          "due_date", "status", "channel_expected"]),
        ("gateway_captures.csv", SourceKind.CAPTURE, captures,
         ["payment_id", "order_id", "amount_paise", "fee_paise", "tax_paise",
          "status", "method", "captured_at", "settlement_id", "utr"]),
        ("bank_statement.csv", SourceKind.BANK_TXN, bank,
         ["txn_id", "value_date", "amount_paise", "credit_debit", "utr",
          "narration", "balance_after"]),
    ]
    paths = {}
    summaries = {}
    for name, source, rows, fields in spec:
        accepted, summary = validate_rows(source, rows, quarantine)
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(accepted)
        paths[name] = path
        summaries[source] = summary
    event_rows, event_summary = validate_rows(
        SourceKind.FINANCE_EVENT, list(finance_events or []), quarantine
    )
    event_path = os.path.join(out_dir, "finance_events.jsonl")
    with open(event_path, "w", encoding="utf-8") as fh:
        for row in event_rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    paths["finance_events.jsonl"] = event_path
    summaries[SourceKind.FINANCE_EVENT] = event_summary
    write_source_manifest(out_dir, summaries)
    return paths


def _map_api_rows(source: SourceKind, api_rows: list[dict], mapper,
                  quarantine: QuarantineStore) -> tuple[list[dict], int, int]:
    mapped: list[dict] = []
    skipped = 0
    quarantined = 0
    for raw in api_rows:
        try:
            row = mapper(raw)
        except SourceValidationError as exc:
            quarantine.append(source.value, raw, exc.code, str(exc))
            quarantined += 1
            continue
        except (KeyError, TypeError, ValueError) as exc:
            quarantine.append(source.value, raw, "MALFORMED_SOURCE", type(exc).__name__)
            quarantined += 1
            continue
        if row is None:
            skipped += 1
        else:
            mapped.append(row)
    return mapped, skipped, quarantined


def ingest(out_dir: str, limit: int = 1000) -> dict[str, int]:
    key_id, key_secret = _credentials()
    api_orders = fetch_all("orders", key_id, key_secret, limit)
    api_payments = fetch_all("payments", key_id, key_secret, limit)
    api_settlements = fetch_all("settlements", key_id, key_secret, limit)

    quarantine = QuarantineStore(os.path.join(out_dir, "quarantine.jsonl"))
    orders, skipped_orders, q_orders = _map_api_rows(
        SourceKind.ORDER, api_orders, order_row, quarantine)
    captures, skipped_payments, q_payments = _map_api_rows(
        SourceKind.CAPTURE, api_payments, capture_row, quarantine)
    bank, unprocessed_settlements, q_settlements = _map_api_rows(
        SourceKind.BANK_TXN, api_settlements, bank_row, quarantine)
    write_batch(out_dir, orders, captures, bank)

    with open(os.path.join(out_dir, "source_manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    q_duplicates = sum(s["quarantined"] for s in manifest["sources"].values())
    sources = manifest["sources"]
    return {"orders": sources[SourceKind.ORDER.value]["accepted"],
            "captures": sources[SourceKind.CAPTURE.value]["accepted"],
            "bank": sources[SourceKind.BANK_TXN.value]["accepted"],
            "skipped_orders": skipped_orders,
            "skipped_payments": skipped_payments,
            "unprocessed_settlements": unprocessed_settlements,
            "quarantined": q_orders + q_payments + q_settlements + q_duplicates}
