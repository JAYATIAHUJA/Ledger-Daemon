import importlib

import pytest


def test_source_hash_is_independent_of_dictionary_insertion_order():
    contracts = importlib.import_module("ledger_daemon.source_contracts")

    first = contracts.sha256_hex({"amount_paise": 100, "order_id": "ord_1"})
    second = contracts.sha256_hex({"order_id": "ord_1", "amount_paise": 100})

    assert first == second
    assert first == "b4935553baf30f5179eb642b5d20fd6a0a5b223c85859db9bf1a71295093b755"


def test_float_money_fails_closed_instead_of_being_coerced():
    contracts = importlib.import_module("ledger_daemon.source_contracts")
    row = {
        "order_id": "ord_1",
        "invoice_no": "INV-1",
        "customer_id": "cust_1",
        "customer_name": "Acme Private Limited",
        "amount_paise": 10.5,
        "due_date": "2026-09-05",
        "status": "unpaid",
        "channel_expected": "gateway",
    }

    with pytest.raises(Exception) as exc:
        contracts.validate_row(contracts.SourceKind.ORDER, row)

    assert type(exc.value).__name__ == "SourceValidationError"
    assert exc.value.code == "FLOAT_MONEY"


def test_valid_order_becomes_a_hashed_pii_masked_envelope():
    contracts = importlib.import_module("ledger_daemon.source_contracts")
    row = {
        "order_id": "ord_1",
        "invoice_no": "INV-1",
        "customer_id": "cust_1",
        "customer_name": "Acme Private Limited",
        "amount_paise": 10_000,
        "due_date": "2026-09-05",
        "status": "unpaid",
        "channel_expected": "gateway",
    }

    envelope = contracts.validate_row(contracts.SourceKind.ORDER, row)

    assert envelope.source is contracts.SourceKind.ORDER
    assert envelope.source_row_id == "ord_1"
    assert envelope.schema_version == "1"
    assert len(envelope.raw_hash) == 64
    assert len(envelope.normalized_hash) == 64
    assert envelope.normalized["customer_name"] != "Acme Private Limited"
    assert envelope.normalized["customer_id"] != "cust_1"
    assert envelope.normalized["amount_paise"] == 10_000
    assert row["customer_name"] == "Acme Private Limited"


@pytest.mark.parametrize(
    ("source_name", "row", "expected_code"),
    [
        ("ORDER", {
            "invoice_no": "INV-1", "customer_id": "c1", "customer_name": "Acme",
            "amount_paise": 10_000, "due_date": "2026-09-05", "status": "unpaid",
            "channel_expected": "gateway",
        }, "MISSING_ID"),
        ("ORDER", {
            "order_id": "o1", "invoice_no": "INV-1", "customer_id": "c1",
            "customer_name": "Acme", "amount_paise": 10_000,
            "due_date": "2026-02-30", "status": "unpaid", "channel_expected": "gateway",
        }, "INVALID_DATE"),
        ("ORDER", {
            "order_id": "o1", "invoice_no": "INV-1", "customer_id": "c1",
            "customer_name": "Acme", "amount_paise": 10_000,
            "due_date": "2026-09-05", "status": "probably_paid",
            "channel_expected": "gateway",
        }, "INVALID_ENUM"),
        ("CAPTURE", {
            "payment_id": "p1", "order_id": "o1", "amount_paise": 10_000,
            "fee_paise": 100, "tax_paise": 18, "status": "pending",
            "method": "upi", "captured_at": "2026-09-05", "settlement_id": "", "utr": "",
        }, "INVALID_ENUM"),
        ("BANK_TXN", {
            "txn_id": "t1", "value_date": "2026-09-05", "amount_paise": 10_000,
            "credit_debit": "sideways", "utr": "u1", "narration": "NEFT ACME",
            "balance_after": 20_000,
        }, "INVALID_ENUM"),
        ("BANK_TXN", {
            "txn_id": "t1", "value_date": "2026-09-05", "amount_paise": True,
            "credit_debit": "credit", "utr": "u1", "narration": "NEFT ACME",
            "balance_after": 20_000,
        }, "FLOAT_MONEY"),
        ("ORDER", {
            "order_id": "o1", "invoice_no": "INV-1", "customer_id": "c1",
            "customer_name": "Acme", "amount_paise": -1,
            "due_date": "2026-09-05", "status": "unpaid", "channel_expected": "gateway",
        }, "INVALID_MONEY"),
        ("ORDER", {
            "order_id": "o1", "invoice_no": "INV-1", "customer_id": "c1",
            "customer_name": "Acme", "amount_paise": 10_000,
            "due_date": "2026-09-05", "status": "unpaid", "channel_expected": "gateway",
            "unexpected": "must not pass through",
        }, "UNKNOWN_FIELD"),
        ("BANK_TXN", {
            "txn_id": "t1", "value_date": "2026-09-05", "amount_paise": 10_000,
            "credit_debit": "credit", "utr": "u1", "narration": {"nested": "value"},
            "balance_after": 20_000,
        }, "INVALID_TYPE"),
    ],
)
def test_invalid_source_rows_fail_with_stable_codes(source_name, row, expected_code):
    contracts = importlib.import_module("ledger_daemon.source_contracts")

    with pytest.raises(contracts.SourceValidationError) as exc:
        contracts.validate_row(contracts.SourceKind[source_name], row)

    assert exc.value.code == expected_code
