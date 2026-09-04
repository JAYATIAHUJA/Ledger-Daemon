import importlib
import json


def test_quarantine_is_idempotent_and_masks_pii(tmp_path):
    quarantine = importlib.import_module("ledger_daemon.quarantine")
    path = tmp_path / "quarantine.jsonl"
    store = quarantine.QuarantineStore(str(path))
    row = {
        "order_id": "ord_bad",
        "customer_name": "Private Customer",
        "email": "private@example.com",
        "amount_paise": 10.5,
    }

    first = store.append("order", row, "FLOAT_MONEY", "amount must be integer paise")
    second = store.append("order", row, "FLOAT_MONEY", "amount must be integer paise")

    assert first == second
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["quarantine_id"] == first
    assert records[0]["error_code"] == "FLOAT_MONEY"
    assert len(records[0]["raw_hash"]) == 64
    assert records[0]["row"]["customer_name"] != "Private Customer"
    assert records[0]["row"]["email"] != "private@example.com"
    assert "Private Customer" not in path.read_text(encoding="utf-8")
    assert "private@example.com" not in path.read_text(encoding="utf-8")


def test_duplicate_source_id_is_quarantined_before_reconciliation(tmp_path):
    contracts = importlib.import_module("ledger_daemon.source_contracts")
    quarantine = importlib.import_module("ledger_daemon.quarantine")
    store = quarantine.QuarantineStore(str(tmp_path / "quarantine.jsonl"))
    base = {
        "order_id": "ord_1",
        "invoice_no": "INV-1",
        "customer_id": "cust_1",
        "customer_name": "Acme",
        "amount_paise": 10_000,
        "due_date": "2026-09-05",
        "status": "unpaid",
        "channel_expected": "gateway",
    }

    accepted, summary = contracts.validate_rows(
        contracts.SourceKind.ORDER,
        [base, {**base, "amount_paise": 20_000}],
        store,
    )

    assert accepted == [base]
    assert summary.accepted == 1
    assert summary.quarantined == 1
    assert summary.duplicate_ids == ("ord_1",)
    assert len(summary.source_hashes) == 1
    record = json.loads((tmp_path / "quarantine.jsonl").read_text(encoding="utf-8"))
    assert record["error_code"] == "DUPLICATE_ID"
