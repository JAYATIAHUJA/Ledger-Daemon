"""API-to-canonical mapping is pure and testable without a network."""

import importlib
import json

import pytest

from ledger_daemon.datagen import load_batch
from ledger_daemon.ingest import (
    IngestError, _credentials, bank_row, capture_row, order_row, write_batch,
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
    "fee": 10_000, "tax": 1_800, "status": "captured", "method": "upi",
    "created_at": 1_756_684_900, "acquirer_data": {"rrn": "227712345678"},
}
API_SETTLEMENT = {
    "id": "setl_NXk11AbCdE", "amount": 488_200, "status": "processed",
    "utr": "UTIBH25244000123", "created_at": 1_756_771_200,
}


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
    assert capture_row({**API_PAYMENT, "status": "refunded"})["amount_paise"] == -500_000
    assert capture_row({**API_PAYMENT, "status": "authorized"}) is None
    assert capture_row({**API_PAYMENT, "status": "created"}) is None
    assert capture_row({**API_PAYMENT, "order_id": None}) is None


def test_capture_row_takes_utr_from_acquirer_data():
    assert capture_row(API_PAYMENT)["utr"] == "227712345678"
    named = {**API_PAYMENT, "acquirer_data": {"utr": "AXISN000111222"}}
    assert capture_row(named)["utr"] == "AXISN000111222"


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
        "settlements": [API_SETTLEMENT],
    }
    monkeypatch.setattr(module, "_credentials", lambda: ("rzp_test_key", "secret"))
    monkeypatch.setattr(
        module,
        "fetch_all",
        lambda entity, _key, _secret, _limit: by_entity[entity],
    )

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
