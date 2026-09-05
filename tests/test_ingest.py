"""API-to-canonical mapping is pure and testable without a network."""

import importlib
import io
import json
import re
import urllib.error

import pytest

from ledger_daemon.datagen import load_batch
from ledger_daemon.ingest import (
    IngestError, _credentials, apply_settlement_recon, bank_row, capture_row,
    ensure_refund_coverage, fetch_settlement_recon, order_row, refund_row, write_batch,
)
from ledger_daemon.recon import reconcile
from ledger_daemon.source_contracts import SourceValidationError

API_ORDER = {
    "id": "order_NXhT2sWnEK", "amount": 500_000, "currency": "INR",
    "receipt": "INV-2291", "status": "paid", "created_at": 1_756_684_800,
    "notes": {"customer_id": "CUST-9", "customer_name": "SHARMA TEXTILES PVT LTD"},
}
API_PAYMENT = {
    "id": "pay_NXhU9dQfEL", "order_id": "order_NXhT2sWnEK", "amount": 500_000,
    "fee": 11_800, "tax": 1_800, "status": "captured", "method": "upi",
    "created_at": 1_756_684_900, "acquirer_data": {"rrn": "227712345678"},
}
API_SETTLEMENT = {
    "id": "setl_NXk11AbCdE", "amount": 488_200, "status": "processed",
    "utr": "UTIBH25244000123", "created_at": 1_756_771_200,
}
API_REFUND = {
    "id": "rfnd_NXmPartial01", "payment_id": API_PAYMENT["id"],
    "amount": 125_000, "status": "processed", "created_at": 1_756_771_100,
}
API_RECON_PAYMENT = {
    "entity_id": API_PAYMENT["id"], "type": "payment", "settled": True,
    "settlement_id": API_SETTLEMENT["id"], "settlement_utr": API_SETTLEMENT["utr"],
}
API_RECON_REFUND = {
    "entity_id": API_REFUND["id"], "payment_id": API_PAYMENT["id"],
    "order_id": API_ORDER["id"], "type": "refund", "settled": True,
    "settlement_id": API_SETTLEMENT["id"], "settlement_utr": API_SETTLEMENT["utr"],
}


def test_http_errors_do_not_expose_provider_response_bodies(monkeypatch):
    module = importlib.import_module("ledger_daemon.ingest")
    private_body = b'{"error":{"description":"customer@example.com / pay_private"}}'

    def fail_request(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.razorpay.com/v1/payments",
            401,
            "Unauthorized",
            {},
            io.BytesIO(private_body),
        )

    monkeypatch.setattr(module.urllib.request, "urlopen", fail_request)
    with pytest.raises(IngestError) as exc:
        module._get("payments", "rzp_test_key", "secret")

    message = str(exc.value)
    assert message == "GET /payments -> HTTP 401"
    assert "customer@example.com" not in message
    assert "pay_private" not in message


def test_order_row_maps_paise_and_receipt():
    r = order_row(API_ORDER)
    assert r["amount_paise"] == 500_000              # API amounts are paise already
    assert r["invoice_no"] == "INV-2291"
    assert r["customer_name"] == "SHARMA TEXTILES PVT LTD"
    assert r["status"] == "paid"
    assert r["due_date"] == "2025-09-01"


def test_order_row_survives_missing_notes():
    bare = {**API_ORDER, "notes": [], "receipt": None, "status": "created"}
    r = order_row(bare)
    assert r["invoice_no"] == bare["id"]             # falls back to the order id
    assert r["customer_name"] == ""
    assert r["status"] == "unpaid"


def test_capture_row_keeps_only_evidence_states():
    assert capture_row(API_PAYMENT)["status"] == "captured"
    refunded_payment = capture_row({**API_PAYMENT, "status": "refunded"})
    assert refunded_payment["status"] == "captured"
    assert refunded_payment["amount_paise"] == 500_000
    assert capture_row({**API_PAYMENT, "status": "authorized"}) is None
    assert capture_row({**API_PAYMENT, "status": "created"}) is None
    assert capture_row({**API_PAYMENT, "order_id": None}) is None


def test_partial_refund_is_a_separate_negative_capture():
    row = refund_row(API_REFUND, {API_PAYMENT["id"]: API_PAYMENT})
    assert row["payment_id"] == API_REFUND["id"]
    assert row["order_id"] == API_ORDER["id"]
    assert row["amount_paise"] == -125_000
    assert row["status"] == "refund"


def test_refunded_payment_fails_closed_when_refund_feed_is_incomplete():
    payment = {**API_PAYMENT, "status": "refunded", "amount_refunded": 500_000}
    with pytest.raises(IngestError) as exc:
        ensure_refund_coverage([payment], [])
    assert "refund coverage incomplete" in str(exc.value)
    assert payment["id"] not in str(exc.value)


def test_refund_coverage_accepts_the_exact_processed_total():
    payment = {
        **API_PAYMENT,
        "status": "captured",
        "refund_status": "partial",
        "amount_refunded": 125_000,
    }
    ensure_refund_coverage([payment], [API_REFUND])


def test_refund_coverage_rejects_rows_that_would_be_quarantined_later():
    payment = {
        **API_PAYMENT,
        "status": "captured",
        "refund_status": "partial",
        "amount_refunded": 125_000,
    }
    invalid_refund = {**API_REFUND, "created_at": None}
    with pytest.raises(IngestError, match="refund coverage incomplete"):
        ensure_refund_coverage([payment], [invalid_refund])


@pytest.mark.parametrize(
    "payment",
    [
        {**API_PAYMENT, "status": "refunded"},
        {**API_PAYMENT, "status": "refunded", "amount_refunded": None},
    ],
)
def test_refunded_payment_requires_an_explicit_refunded_total(payment):
    with pytest.raises(IngestError, match="refund coverage incomplete"):
        ensure_refund_coverage([payment], [])


def test_refund_coverage_rejects_partial_state_at_the_full_amount():
    payment = {
        **API_PAYMENT,
        "status": "captured",
        "refund_status": "partial",
        "amount_refunded": 500_000,
    }
    full_refund = {**API_REFUND, "amount": 500_000}
    with pytest.raises(IngestError, match="refund coverage incomplete"):
        ensure_refund_coverage([payment], [full_refund])


def test_refund_coverage_rejects_refunded_status_below_the_full_amount():
    payment = {
        **API_PAYMENT,
        "status": "refunded",
        "refund_status": "partial",
        "amount_refunded": 125_000,
    }
    with pytest.raises(IngestError, match="refund coverage incomplete"):
        ensure_refund_coverage([payment], [API_REFUND])


@pytest.mark.parametrize("bad_amount", [True, 0.0, -1, 0])
def test_refund_row_rejects_non_integer_or_non_positive_amounts(bad_amount):
    with pytest.raises(SourceValidationError):
        refund_row({**API_REFUND, "amount": bad_amount}, {API_PAYMENT["id"]: API_PAYMENT})


def test_settlement_recon_links_payment_and_refund_to_the_same_credit():
    rows = [
        capture_row({**API_PAYMENT, "status": "refunded"}),
        refund_row(API_REFUND, {API_PAYMENT["id"]: API_PAYMENT}),
    ]
    linked, linked_count = apply_settlement_recon(
        rows, [API_RECON_PAYMENT, API_RECON_REFUND]
    )
    assert linked_count == 2
    assert {row["settlement_id"] for row in linked} == {API_SETTLEMENT["id"]}
    assert {row["utr"] for row in linked} == {API_SETTLEMENT["utr"]}


def test_settlement_recon_fetch_uses_the_official_bounded_period(monkeypatch):
    module = importlib.import_module("ledger_daemon.ingest")
    calls = []

    def fake_get(path, _key, _secret, **params):
        calls.append((path, params))
        return {"items": [API_RECON_PAYMENT]}

    monkeypatch.setattr(module, "_get", fake_get)
    rows = fetch_settlement_recon(
        "rzp_test_key", "secret", year=2026, month=9, day=5, limit=10
    )
    assert rows == [API_RECON_PAYMENT]
    assert calls == [("settlements/recon/combined", {
        "year": 2026, "month": 9, "day": 5, "count": 10, "skip": 0,
    })]


def test_capture_row_takes_utr_from_acquirer_data():
    assert capture_row(API_PAYMENT)["utr"] == "227712345678"
    named = {**API_PAYMENT, "acquirer_data": {"utr": "AXISN000111222"}}
    assert capture_row(named)["utr"] == "AXISN000111222"


def test_capture_row_splits_tax_from_razorpay_fee_total():
    row = capture_row(API_PAYMENT)
    assert row["fee_paise"] == 10_000
    assert row["tax_paise"] == 1_800
    assert row["amount_paise"] - row["fee_paise"] - row["tax_paise"] == 488_200


@pytest.mark.parametrize(
    "payment",
    [
        {**API_PAYMENT, "fee": 0.0, "tax": 0},
        {**API_PAYMENT, "fee": 100, "tax": False},
    ],
)
def test_capture_row_rejects_non_integer_fee_or_tax_even_when_falsy(payment):
    with pytest.raises(SourceValidationError, match="integer paise"):
        capture_row(payment)


def test_bank_row_is_marked_as_a_settlement_credit():
    r = bank_row(API_SETTLEMENT)
    assert r["credit_debit"] == "credit"
    assert "RAZORPAYSETTLEMENT" in r["narration"]    # keeps it out of the fuzzy pool
    assert bank_row({**API_SETTLEMENT, "status": "created"}) is None


def test_written_batch_round_trips_through_load_batch_and_reconcile(tmp_path):
    write_batch(str(tmp_path), [order_row(API_ORDER)],
                [capture_row(API_PAYMENT)], [bank_row(API_SETTLEMENT)])
    orders, captures, bank, truth = load_batch(str(tmp_path))
    assert truth == {}                               # real data has no oracle
    result = reconcile(orders, captures, bank, q_hat=0.05)
    assert "order_NXhT2sWnEK" in result.verdicts     # the pipeline runs unchanged


def test_live_mode_keys_are_refused(monkeypatch):
    monkeypatch.setenv("RZP_TEST_KEY_ID", "rzp_live_AAAA")
    monkeypatch.setenv("RZP_TEST_KEY_SECRET", "s3cret")
    with pytest.raises(IngestError, match="test mode"):
        _credentials()


def test_batch_writer_quarantines_invalid_rows_and_emits_provenance(tmp_path):
    valid = order_row(API_ORDER)
    invalid = {**valid, "order_id": "order_bad", "amount_paise": 10.5}

    write_batch(
        str(tmp_path),
        [valid, invalid],
        [capture_row(API_PAYMENT)],
        [bank_row(API_SETTLEMENT)],
    )

    orders, captures, bank, _truth = load_batch(str(tmp_path))
    assert [row.order_id for row in orders] == [API_ORDER["id"]]
    assert len(captures) == 1
    assert len(bank) == 1

    manifest = json.loads((tmp_path / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1"
    assert manifest["sources"]["order"]["accepted"] == 1
    assert manifest["sources"]["order"]["quarantined"] == 1
    assert len(manifest["sources"]["order"]["source_hashes"]) == 1

    quarantined = json.loads((tmp_path / "quarantine.jsonl").read_text(encoding="utf-8"))
    assert quarantined["error_code"] == "FLOAT_MONEY"


@pytest.mark.parametrize(
    ("mapper", "payload"),
    [
        (order_row, {**API_ORDER, "amount": 500_000.5}),
        (capture_row, {**API_PAYMENT, "fee": 10_000.5}),
        (bank_row, {**API_SETTLEMENT, "amount": 488_200.5}),
    ],
)
def test_api_mappers_never_truncate_float_money(mapper, payload):
    with pytest.raises(SourceValidationError) as exc:
        mapper(payload)

    assert exc.value.code == "FLOAT_MONEY"


@pytest.mark.parametrize(
    ("mapper", "payload"),
    [
        (order_row, {**API_ORDER, "created_at": None}),
        (capture_row, {**API_PAYMENT, "created_at": None}),
        (bank_row, {**API_SETTLEMENT, "created_at": None}),
    ],
)
def test_api_mappers_never_invent_epoch_for_missing_dates(mapper, payload):
    with pytest.raises(SourceValidationError) as exc:
        mapper(payload)

    assert exc.value.code == "MISSING_DATE"


def test_ingest_quarantines_bad_api_items_without_dropping_valid_items(tmp_path, monkeypatch):
    module = importlib.import_module("ledger_daemon.ingest")
    by_entity = {
        "orders": [
            API_ORDER,
            {**API_ORDER, "id": "order_bad", "amount": 10.5},
            {**API_ORDER, "receipt": "DUPLICATE-ID", "amount": 600_000},
        ],
        "payments": [API_PAYMENT],
        "refunds": [],
        "settlements": [API_SETTLEMENT],
    }
    monkeypatch.setattr(module, "_credentials", lambda: ("rzp_test_key", "secret"))
    monkeypatch.setattr(
        module,
        "fetch_all",
        lambda entity, _key, _secret, _limit: by_entity[entity],
    )
    monkeypatch.setattr(module, "fetch_settlement_recon", lambda *_args, **_kwargs: [])

    counts = module.ingest(str(tmp_path), limit=10)

    assert counts["orders"] == 1
    assert counts["captures"] == 1
    assert counts["bank"] == 1
    assert counts["quarantined"] == 2
    orders, _captures, _bank, _truth = load_batch(str(tmp_path))
    assert [order.order_id for order in orders] == [API_ORDER["id"]]
    records = [
        json.loads(line)
        for line in (tmp_path / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {record["error_code"] for record in records} == {"FLOAT_MONEY", "DUPLICATE_ID"}
    quarantine_text = (tmp_path / "quarantine.jsonl").read_text(encoding="utf-8")
    assert "SHARMA TEXTILES PVT LTD" not in quarantine_text
    assert "CUST-9" not in quarantine_text


def test_ingest_writes_a_public_safe_test_mode_receipt(tmp_path, monkeypatch):
    """A credentialed read leaves evidence without publishing source objects."""
    module = importlib.import_module("ledger_daemon.ingest")
    by_entity = {
        "orders": [API_ORDER],
        "payments": [API_PAYMENT],
        "refunds": [],
        "settlements": [API_SETTLEMENT],
    }
    monkeypatch.setattr(module, "_credentials", lambda: ("rzp_test_private", "secret-value"))
    monkeypatch.setattr(
        module, "fetch_all",
        lambda entity, _key, _secret, _limit: by_entity[entity],
    )
    monkeypatch.setattr(module, "fetch_settlement_recon", lambda *_args, **_kwargs: [])

    counts = module.ingest(str(tmp_path), limit=10)
    receipt_path = tmp_path / "razorpay_test_mode_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert counts["receipt"] == str(receipt_path)
    assert receipt["source"] == "razorpay_test_mode_api"
    assert receipt["mode"] == "test"
    assert receipt["read_only"] is True
    assert receipt["fetched"] == {
        "orders": 1,
        "payments": 1,
        "refunds": 0,
        "settlements": 1,
        "settlement_recon": 0,
    }
    assert receipt["accepted"] == {"orders": 1, "payments": 1, "settlement_proxies": 1}
    assert receipt["bank_feed"] == {
        "independent": False,
        "kind": "razorpay_settlement_proxy",
    }
    assert receipt["ground_truth"] == "absent"
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["batch_manifest_sha256"])
    assert receipt["captured_at"].endswith("Z")

    public_text = receipt_path.read_text(encoding="utf-8")
    for private_value in (
        "rzp_test_private", "secret-value", API_ORDER["id"],
        API_PAYMENT["id"], API_SETTLEMENT["id"], "SHARMA TEXTILES",
    ):
        assert private_value not in public_text


def test_ingest_cli_points_to_the_existing_reconcile_batch_flag(tmp_path, monkeypatch, capsys):
    module = importlib.import_module("ledger_daemon.ingest")
    receipt = tmp_path / "razorpay_test_mode_receipt.json"
    monkeypatch.setattr(module, "ingest", lambda *_args, **_kwargs: {
        "orders": 1, "captures": 1, "bank": 1,
        "skipped_payments": 0, "skipped_refunds": 0,
        "unprocessed_settlements": 0, "linked_captures": 1,
        "quarantined": 0, "receipt": str(receipt),
    })
    from ledger_daemon.cli import cmd_ingest

    assert cmd_ingest(type("Args", (), {"out": str(tmp_path), "limit": 10})()) == 0
    output = capsys.readouterr().out
    assert f"python -m ledger_daemon reconcile --batch {tmp_path}" in output
    assert "reconcile --dir" not in output
    assert str(receipt) in output
