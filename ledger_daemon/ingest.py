"""Ingest Razorpay Test Mode API objects into the canonical batch schema.

`python -m ledger_daemon ingest --out data/test-mode-raw` pulls Orders, Payments and
Refunds, Settlements and settlement-reconciliation rows from Razorpay's
sandbox using RZP_TEST_KEY_ID / RZP_TEST_KEY_SECRET. It writes the exact CSVs
`load_batch()` reads, so `reconcile` can exercise the integration boundary.

Honesty about the three sources:

  * merchant_orders.csv  <- /v1/orders        Test Mode order objects
  * gateway_captures.csv <- /v1/payments and /v1/refunds
  * bank_statement.csv   <- /v1/settlements   settlement credits STANDING IN for
                            the bank feed: each processed settlement is the
                            credit line the merchant's statement would show,
                            UTR included. Drop in a real statement export to
                            replace it; the schema is the contract.

Settlement ids and payout UTRs come from /v1/settlements/recon/combined rather
than being guessed from payment UTRs. No ground_truth.csv is written: these API
objects have no labelled oracle, and pretending
otherwise would poison every honest metric downstream. `load_batch` tolerates
its absence; evaluation stays a synthetic-world activity.

stdlib only (urllib), same as the executor. Secrets are read from the
environment and never logged.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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
        # Provider error bodies can contain object ids or customer context.  Keep
        # CLI and CI logs useful without copying that response into the exception.
        raise IngestError(f"GET /{path} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise IngestError(f"GET /{path} failed (network error)") from exc


def fetch_all(entity: str, key_id: str, key_secret: str, limit: int = 1000) -> list[dict]:
    """Paginate /v1/<entity> newest-first until exhausted or `limit` rows."""
    rows: list[dict] = []
    while len(rows) < limit:
        batch = _get(entity, key_id, key_secret, count=PAGE, skip=len(rows)).get("items", [])
        rows.extend(batch)
        if len(batch) < PAGE:
            break
    return rows[:limit]


def fetch_settlement_recon(
    key_id: str,
    key_secret: str,
    *,
    year: int,
    month: int,
    day: int | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Read Razorpay's transaction-level settlement report for one period."""
    try:
        datetime(year, month, day or 1, tzinfo=timezone.utc)
    except ValueError as exc:
        raise IngestError(f"invalid settlement period: {exc}") from exc
    if limit < 1:
        raise IngestError("limit must be at least 1")

    rows: list[dict] = []
    while len(rows) < limit:
        count = min(1000, limit - len(rows))
        batch = _get(
            "settlements/recon/combined",
            key_id,
            key_secret,
            year=year,
            month=month,
            day=day,
            count=count,
            skip=len(rows),
        ).get("items", [])
        rows.extend(batch)
        if len(batch) < count:
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


_STATUS = {"captured": "captured", "failed": "failed", "refunded": "captured"}


def capture_row(p: dict) -> dict | None:
    """One canonical capture per payment; None for states recon cannot use
    (created/authorized are in-flight, not evidence either way)."""
    status = _STATUS.get(p.get("status", ""))
    if status is None or not p.get("order_id"):
        return None
    acquirer = p.get("acquirer_data") or {}
    fee_total = p.get("fee")
    tax = p.get("tax")
    fee_total = 0 if fee_total is None else fee_total
    tax = 0 if tax is None else tax
    if type(fee_total) is not int or type(tax) is not int:
        raise SourceValidationError("FLOAT_MONEY", "payment fee and tax must be integer paise")
    fee_before_tax = fee_total - tax
    if fee_before_tax < 0:
        raise SourceValidationError("INVALID_MONEY", "payment tax cannot exceed total fee")
    row = {
        "payment_id": p["id"],
        "order_id": p["order_id"],
        "amount_paise": p["amount"],
        # Razorpay's payment `fee` already includes `tax`; the canonical
        # schema stores them separately so net = amount - fee - tax.
        "fee_paise": fee_before_tax,
        "tax_paise": tax,
        "status": status,
        "method": p.get("method", ""),
        "captured_at": _day(p.get("created_at")),
        "settlement_id": "",   # joined from /v1/settlements via UTR, not per-payment
        "utr": acquirer.get("utr") or acquirer.get("rrn") or "",
    }
    validate_row(SourceKind.CAPTURE, row)
    return row


def refund_row(refund: dict, payments_by_id: dict[str, dict]) -> dict | None:
    """Map one processed refund without inventing a full-payment refund.

    Razorpay payment objects retain the original captured amount even after a
    partial refund. Refund objects carry the actual refund amount, so they are
    represented as separate negative capture rows.
    """
    if refund.get("status") != "processed":
        return None
    amount = refund.get("amount")
    if type(amount) is not int:
        raise SourceValidationError("FLOAT_MONEY", "refund amount must be integer paise")
    if amount <= 0:
        raise SourceValidationError("INVALID_MONEY", "refund amount must be positive")
    payment = payments_by_id.get(refund.get("payment_id"))
    order_id = refund.get("order_id") or (payment or {}).get("order_id")
    if not order_id:
        raise SourceValidationError(
            "MISSING_ORDER_ID", "refund cannot be linked to a Razorpay order"
        )
    row = {
        "payment_id": refund["id"],
        "order_id": order_id,
        "amount_paise": -amount,
        "fee_paise": 0,
        "tax_paise": 0,
        "status": "refund",
        "method": "refund",
        "captured_at": _day(refund.get("created_at")),
        "settlement_id": "",
        "utr": "",
    }
    validate_row(SourceKind.CAPTURE, row)
    return row


def apply_settlement_recon(
    captures: list[dict], recon_rows: list[dict]
) -> tuple[list[dict], int]:
    """Attach settlement ids and payout UTRs from the official recon feed."""
    by_entity = {
        row.get("entity_id"): row
        for row in recon_rows
        if row.get("type") in {"payment", "refund"}
        and row.get("settled") is True
        and isinstance(row.get("entity_id"), str)
    }
    linked: list[dict] = []
    linked_count = 0
    for capture in captures:
        row = dict(capture)
        recon = by_entity.get(row.get("payment_id"))
        if recon and recon.get("settlement_id"):
            row["settlement_id"] = recon["settlement_id"]
            row["utr"] = recon.get("settlement_utr") or ""
            validate_row(SourceKind.CAPTURE, row)
            linked_count += 1
        linked.append(row)
    return linked, linked_count


def ensure_refund_coverage(payments: list[dict], refunds: list[dict]) -> None:
    """Refuse a batch when capped refund pagination lost financial events.

    Razorpay exposes `amount_refunded` on each payment. That total is an
    independent completeness check for the separately paginated refund list.
    Continuing after a mismatch could turn a fully refunded payment into a
    clean settlement, so ingestion fails before writing any canonical files.
    """
    payments_by_id = {
        payment.get("id"): payment
        for payment in payments
        if isinstance(payment.get("id"), str)
    }
    processed: dict[str, int] = {}
    seen_refund_ids: set[str] = set()
    malformed = False
    for refund in refunds:
        try:
            mapped = refund_row(refund, payments_by_id)
        except (SourceValidationError, KeyError, TypeError, ValueError):
            malformed = True
            continue
        if mapped is None:
            continue
        refund_id = refund.get("id")
        payment_id = refund.get("payment_id")
        if (not isinstance(refund_id, str) or refund_id in seen_refund_ids
                or not isinstance(payment_id, str)):
            malformed = True
            continue
        seen_refund_ids.add(refund_id)
        processed[payment_id] = processed.get(payment_id, 0) - mapped["amount_paise"]

    incomplete = 0
    for payment in payments:
        expected = payment.get("amount_refunded")
        amount = payment.get("amount")
        status = payment.get("status")
        refund_status = payment.get("refund_status")

        # Razorpay keeps partially refunded payments captured and changes a
        # payment to refunded only after the full amount is returned.  Its
        # refund_status is null, partial, or full.  Missing totals on a payment
        # that claims a refund must therefore fail closed instead of becoming 0.
        invalid_state = False
        if type(amount) is not int or amount <= 0:
            invalid_state = True
        if expected is None:
            if status == "refunded" or refund_status in {"partial", "full"}:
                invalid_state = True
            expected = 0
        if type(expected) is not int or expected < 0:
            invalid_state = True
        elif type(amount) is int and expected > amount:
            invalid_state = True

        if refund_status is None:
            if expected != 0 or status == "refunded":
                invalid_state = True
        elif refund_status == "partial":
            if (status != "captured" or type(amount) is not int
                    or type(expected) is not int
                    or not 0 < expected < amount):
                invalid_state = True
        elif refund_status == "full":
            if status != "refunded" or expected != amount:
                invalid_state = True
        else:
            invalid_state = True

        if invalid_state:
            incomplete += 1
            continue
        if processed.get(payment.get("id"), 0) != expected:
            incomplete += 1
    if malformed or incomplete:
        count = incomplete + int(malformed)
        raise IngestError(
            f"refund coverage incomplete for {count} payment/feed check(s); "
            "increase --limit and retry"
        )


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


def _write_public_receipt(
    out_dir: str,
    *,
    fetched: dict[str, int],
    accepted: dict[str, int],
    settlement_period: dict[str, int],
    linked_captures: int,
) -> str:
    """Write aggregate proof of a read-only Test Mode capture.

    API objects, identifiers, customer fields, credentials, amounts and
    narrations stay in the private batch directory and never enter this file.
    """
    manifest_path = os.path.join(out_dir, "source_manifest.json")
    with open(manifest_path, "rb") as fh:
        manifest_sha256 = hashlib.sha256(fh.read()).hexdigest()
    receipt = {
        "accepted": accepted,
        "bank_feed": {
            "independent": False,
            "kind": "razorpay_settlement_proxy",
        },
        "batch_manifest_sha256": manifest_sha256,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fetched": fetched,
        "ground_truth": "absent",
        "linked_captures": linked_captures,
        "mode": "test",
        "read_only": True,
        "schema_version": "1",
        "settlement_recon_period": settlement_period,
        "source": "razorpay_test_mode_api",
    }
    path = os.path.join(out_dir, "razorpay_test_mode_receipt.json")
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(temp_path, path)
    return path


def ingest(
    out_dir: str,
    limit: int = 1000,
    *,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> dict[str, int | str]:
    key_id, key_secret = _credentials()
    now = datetime.now(timezone.utc)
    capture_year = now.year if year is None else year
    capture_month = now.month if month is None else month
    api_orders = fetch_all("orders", key_id, key_secret, limit)
    api_payments = fetch_all("payments", key_id, key_secret, limit)
    api_refunds = fetch_all("refunds", key_id, key_secret, limit)
    ensure_refund_coverage(api_payments, api_refunds)
    api_settlements = fetch_all("settlements", key_id, key_secret, limit)
    api_recon = fetch_settlement_recon(
        key_id,
        key_secret,
        year=capture_year,
        month=capture_month,
        day=day,
        limit=limit,
    )

    quarantine = QuarantineStore(os.path.join(out_dir, "quarantine.jsonl"))
    orders, skipped_orders, q_orders = _map_api_rows(
        SourceKind.ORDER, api_orders, order_row, quarantine)
    captures, skipped_payments, q_payments = _map_api_rows(
        SourceKind.CAPTURE, api_payments, capture_row, quarantine)
    payments_by_id = {
        payment.get("id"): payment
        for payment in api_payments
        if isinstance(payment.get("id"), str)
    }
    refunds, skipped_refunds, q_refunds = _map_api_rows(
        SourceKind.CAPTURE,
        api_refunds,
        lambda refund: refund_row(refund, payments_by_id),
        quarantine,
    )
    captures, linked_captures = apply_settlement_recon(captures + refunds, api_recon)
    bank, unprocessed_settlements, q_settlements = _map_api_rows(
        SourceKind.BANK_TXN, api_settlements, bank_row, quarantine)
    write_batch(out_dir, orders, captures, bank)

    with open(os.path.join(out_dir, "source_manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    q_duplicates = sum(s["quarantined"] for s in manifest["sources"].values())
    sources = manifest["sources"]
    accepted = {
        "orders": sources[SourceKind.ORDER.value]["accepted"],
        "payments": sources[SourceKind.CAPTURE.value]["accepted"],
        "settlement_proxies": sources[SourceKind.BANK_TXN.value]["accepted"],
    }
    receipt_path = _write_public_receipt(
        out_dir,
        fetched={
            "orders": len(api_orders),
            "payments": len(api_payments),
            "refunds": len(api_refunds),
            "settlements": len(api_settlements),
            "settlement_recon": len(api_recon),
        },
        accepted=accepted,
        settlement_period={
            "year": capture_year,
            "month": capture_month,
            **({"day": day} if day is not None else {}),
        },
        linked_captures=linked_captures,
    )
    return {"orders": accepted["orders"],
            "captures": accepted["payments"],
            "bank": accepted["settlement_proxies"],
            "skipped_orders": skipped_orders,
            "skipped_payments": skipped_payments,
            "skipped_refunds": skipped_refunds,
            "unprocessed_settlements": unprocessed_settlements,
            "linked_captures": linked_captures,
            "quarantined": (q_orders + q_payments + q_refunds
                            + q_settlements + q_duplicates),
            "receipt": receipt_path}
