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

API = "https://api.razorpay.com/v1"
PAGE = 100


class IngestError(RuntimeError):
    pass


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
    return time.strftime("%Y-%m-%d", time.gmtime(epoch or 0))


def order_row(o: dict) -> dict:
    notes = o.get("notes") or {}
    if isinstance(notes, list):
        notes = {}
    return {
        "order_id": o["id"],
        "invoice_no": o.get("receipt") or o["id"],
        "customer_id": notes.get("customer_id", ""),
        "customer_name": notes.get("customer_name", ""),
        "amount_paise": int(o["amount"]),          # API amounts are already paise
        "due_date": _day(o.get("created_at")),
        "status": "paid" if o.get("status") == "paid" else "unpaid",
        "channel_expected": "gateway",
    }


_STATUS = {"captured": "captured", "failed": "failed", "refunded": "refund"}


def capture_row(p: dict) -> dict | None:
    """One canonical capture per payment; None for states recon cannot use
    (created/authorized are in-flight, not evidence either way)."""
    status = _STATUS.get(p.get("status", ""))
    if status is None or not p.get("order_id"):
        return None
    acquirer = p.get("acquirer_data") or {}
    return {
        "payment_id": p["id"],
        "order_id": p["order_id"],
        "amount_paise": int(p["amount"]) if status != "refund" else -int(p["amount"]),
        "fee_paise": int(p.get("fee") or 0),
        "tax_paise": int(p.get("tax") or 0),
        "status": status,
        "method": p.get("method", ""),
        "captured_at": _day(p.get("created_at")),
        "settlement_id": "",   # joined from /v1/settlements via UTR, not per-payment
        "utr": acquirer.get("utr") or acquirer.get("rrn") or "",
    }


def bank_row(s: dict) -> dict | None:
    """A processed settlement, rendered as the bank credit it becomes."""
    if s.get("status") != "processed":
        return None
    return {
        "txn_id": s["id"],
        "value_date": _day(s.get("created_at")),
        "amount_paise": int(s["amount"]),
        "credit_debit": "credit",
        "utr": s.get("utr") or "",
        # the marker recon uses to keep settlements out of the fuzzy pool
        "narration": f"RAZORPAYSETTLEMENT {s['id']}",
        "balance_after": 0,    # a statement export carries this; settlements do not
    }


def write_batch(out_dir: str, orders: list[dict], captures: list[dict],
                bank: list[dict]) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    spec = [
        ("merchant_orders.csv", orders,
         ["order_id", "invoice_no", "customer_id", "customer_name", "amount_paise",
          "due_date", "status", "channel_expected"]),
        ("gateway_captures.csv", captures,
         ["payment_id", "order_id", "amount_paise", "fee_paise", "tax_paise",
          "status", "method", "captured_at", "settlement_id", "utr"]),
        ("bank_statement.csv", bank,
         ["txn_id", "value_date", "amount_paise", "credit_debit", "utr",
          "narration", "balance_after"]),
    ]
    paths = {}
    for name, rows, fields in spec:
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        paths[name] = path
    return paths


def ingest(out_dir: str, limit: int = 1000) -> dict[str, int]:
    key_id, key_secret = _credentials()
    api_orders = fetch_all("orders", key_id, key_secret, limit)
    api_payments = fetch_all("payments", key_id, key_secret, limit)
    api_settlements = fetch_all("settlements", key_id, key_secret, limit)

    orders = [order_row(o) for o in api_orders]
    captures = [c for c in (capture_row(p) for p in api_payments) if c]
    bank = [b for b in (bank_row(s) for s in api_settlements) if b]
    write_batch(out_dir, orders, captures, bank)
    return {"orders": len(orders), "captures": len(captures), "bank": len(bank),
            "skipped_payments": len(api_payments) - len(captures),
            "unprocessed_settlements": len(api_settlements) - len(bank)}
