import pytest
import json

from ledger_daemon.finance_events import (
    Adjustment,
    Dispute,
    FinanceEventError,
    Refund,
    Settlement,
    decode_finance_event,
)
from ledger_daemon.money import FloatMoneyError
from ledger_daemon.ingest import decode_finance_events
from ledger_daemon.quarantine import QuarantineStore
from ledger_daemon.source_contracts import SourceKind, validate_row


def test_finance_event_money_rejects_float_and_bool():
    for bad in (10.5, True):
        with pytest.raises(FloatMoneyError):
            Refund("rfnd-1", "pay-1", "ORD-1", bad, "processed", "2026-08-10")


def test_unknown_finance_event_type_fails_closed():
    with pytest.raises(FinanceEventError) as exc:
        decode_finance_event({"event_type": "crypto_payout", "event_id": "evt-1"})
    assert exc.value.code == "UNKNOWN_EVENT_TYPE"


@pytest.mark.parametrize(
    ("row", "expected_type"),
    [
        ({"event_type": "refund", "refund_id": "rfnd-1", "payment_id": "pay-1",
          "order_id": "ORD-1", "amount_paise": 2500, "status": "processed",
          "created_at": "2026-08-10"}, Refund),
        ({"event_type": "dispute", "dispute_id": "disp-1", "payment_id": "pay-1",
          "order_id": "ORD-1", "amount_paise": 10000, "status": "open",
          "created_at": "2026-08-11"}, Dispute),
        ({"event_type": "adjustment", "adjustment_id": "adj-1",
          "settlement_id": "setl-1", "amount_paise": -118,
          "kind": "gst_variance", "created_at": "2026-08-12"}, Adjustment),
        ({"event_type": "settlement", "settlement_id": "setl-1",
          "amount_paise": 9702, "settled_at": "2026-08-13", "utr": "UTR1",
          "status": "processed"}, Settlement),
    ],
)
def test_known_finance_events_decode_to_typed_records(row, expected_type):
    assert isinstance(decode_finance_event(row), expected_type)


def test_ingest_quarantines_unknown_event_without_dropping_valid_event(tmp_path):
    valid = {"event_type": "refund", "refund_id": "rfnd-1", "payment_id": "pay-1",
             "order_id": "ORD-1", "amount_paise": 2500, "status": "processed",
             "created_at": "2026-08-10"}
    unknown = {"event_type": "crypto_payout", "event_id": "evt-1", "email": "x@example.com"}
    quarantine = QuarantineStore(str(tmp_path / "quarantine.jsonl"))

    accepted = decode_finance_events([valid, unknown], quarantine)

    assert [event.refund_id for event in accepted] == ["rfnd-1"]
    record = json.loads((tmp_path / "quarantine.jsonl").read_text(encoding="utf-8"))
    assert record["error_code"] == "UNKNOWN_EVENT_TYPE"
    assert "x@example.com" not in json.dumps(record)


def test_ingest_quarantines_float_money_with_stable_code(tmp_path):
    bad = {"event_type": "refund", "refund_id": "rfnd-1", "payment_id": "pay-1",
           "order_id": "ORD-1", "amount_paise": 25.5, "status": "processed",
           "created_at": "2026-08-10"}
    quarantine = QuarantineStore(str(tmp_path / "quarantine.jsonl"))
    assert decode_finance_events([bad], quarantine) == []
    record = json.loads((tmp_path / "quarantine.jsonl").read_text(encoding="utf-8"))
    assert record["error_code"] == "FLOAT_MONEY"


def test_non_object_event_is_quarantined_without_echoing_sensitive_payload(tmp_path):
    quarantine = QuarantineStore(str(tmp_path / "quarantine.jsonl"))
    assert decode_finance_events(["customer-secret@example.com"], quarantine) == []
    body = (tmp_path / "quarantine.jsonl").read_text(encoding="utf-8")
    assert "customer-secret@example.com" not in body
    assert json.loads(body)["error_code"] == "INVALID_SCHEMA"


@pytest.mark.parametrize(
    ("amount", "kind"),
    [(100, "debit"), (-100, "credit")],
)
def test_adjustment_direction_cannot_contradict_amount_sign(amount, kind):
    with pytest.raises(FinanceEventError) as exc:
        Adjustment("adj-1", "setl-1", amount, kind, "2026-08-12")
    assert exc.value.code == "INVALID_SIGN"


def test_finance_event_source_envelope_has_order_independent_hash_and_event_id():
    row = {"event_type": "refund", "refund_id": "rfnd-1", "payment_id": "pay-1",
           "order_id": "ORD-1", "amount_paise": 2500, "status": "processed",
           "created_at": "2026-08-10"}
    first = validate_row(SourceKind.FINANCE_EVENT, row)
    second = validate_row(SourceKind.FINANCE_EVENT, dict(reversed(list(row.items()))))
    assert first.source_row_id == "rfnd-1"
    assert first.raw_hash == second.raw_hash


def test_duplicate_finance_event_id_is_quarantined(tmp_path):
    row = {"event_type": "refund", "refund_id": "rfnd-1", "payment_id": "pay-1",
           "order_id": "ORD-1", "amount_paise": 2500, "status": "processed",
           "created_at": "2026-08-10"}
    quarantine = QuarantineStore(str(tmp_path / "quarantine.jsonl"))
    accepted = decode_finance_events([row, dict(row)], quarantine)
    assert len(accepted) == 1
    record = json.loads((tmp_path / "quarantine.jsonl").read_text(encoding="utf-8"))
    assert record["error_code"] == "DUPLICATE_ID"


def test_unknown_ledger_entry_subtype_fails_closed():
    row = {"event_type": "ledger_entry", "entry_id": "entry-1",
           "ledger_event_type": "crypto_payout", "source_ref": "ORD-1",
           "amount_paise": 1000, "direction": "credit", "occurred_at": "2026-08-10"}
    with pytest.raises(FinanceEventError) as exc:
        decode_finance_event(row)
    assert exc.value.code == "UNKNOWN_EVENT_TYPE"
