"""Statement parsing: real export shapes in, integer paise out, no float ever."""

import pytest

from ledger_daemon.parsers import (
    StatementError, detect_bank, paise_from_str, parse_statement, write_bank_csv,
)
from ledger_daemon.datagen import load_batch

HDFC = """Date,Narration,Value Dat,Debit Amount,Credit Amount,Chq/Ref Number,Closing Balance
01/09/26,NEFT CR-AXIS BANK-SHARMA TEXTILES PVT-INV2291,01/09/26,,"1,23,456.78",AXISN123456789,"12,03,456.78"
02/09/26,IMPS/P2A/629912345678/GUPTA INFOTECH,02/09/26,,"98,000.00",,"13,01,456.78"
03/09/26,ACH-D-ELECTRICITYBOARD,03/09/26,"5,400.00",,,"12,96,056.78"
"""

ICICI = """S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )
1,01/09/2026,01/09/2026,,NEFT-UTIBH25244000123-DESAI FOODS LLP-INV2302,,"4,94,109.00","44,94,109.00"
2,02/09/2026,02/09/2026,,UPI/629900112233/PAYMENT FROM KHAN PLASTICS,,"62,801.00","45,56,910.00"
"""

WITH_PREAMBLE = """HDFC BANK LTD
Statement for A/c XXXX1234 from 01/09/26 to 30/09/26

Date,Narration,Value Dat,Debit Amount,Credit Amount,Chq/Ref Number,Closing Balance
01/09/26,NEFT CR-SBI-RAO DISTRIBUTORS-INV2410,01/09/26,,"17,391.00",SBIN0555666777,"1,00,000.00"
"""


def test_paise_from_str_is_exact_string_arithmetic():
    assert paise_from_str("1,23,456.78") == 1_23_456_78
    assert paise_from_str("98,000.00") == 98_000_00
    assert paise_from_str("0.5") == 50          # one decimal -> padded, not scaled
    assert paise_from_str("-250.25") == -250_25
    assert paise_from_str("") == 0
    with pytest.raises(StatementError):
        paise_from_str("12.345")                # three decimals is not money
    with pytest.raises(StatementError):
        paise_from_str("1O0.00")                # letter O, a real OCR/export artefact


def test_detect_bank_from_headers():
    assert detect_bank(HDFC.splitlines()[0].split(",")) == "hdfc"
    assert detect_bank(ICICI.splitlines()[0].split(",")) == "icici"
    with pytest.raises(StatementError, match="unrecognised"):
        detect_bank(["Sr", "Details", "Amount"])


def test_hdfc_rows_parse_with_utr_and_direction(tmp_path):
    f = tmp_path / "hdfc.csv"
    f.write_text(HDFC, encoding="utf-8")
    txns = parse_statement(str(f))
    assert [t.credit_debit for t in txns] == ["credit", "credit", "debit"]
    assert txns[0].amount_paise == 1_23_456_78
    assert txns[0].utr == "AXISN123456789"          # from the ref column
    assert txns[1].utr == "629912345678"            # recovered from the narration
    assert txns[0].value_date == "2026-09-01"
    assert txns[0].balance_after == 12_03_456_78


def test_icici_rows_parse(tmp_path):
    f = tmp_path / "icici.csv"
    f.write_text(ICICI, encoding="utf-8")
    txns = parse_statement(str(f))
    assert len(txns) == 2
    assert txns[0].utr == "UTIBH25244000123"
    assert txns[0].amount_paise == 4_94_109_00
    assert txns[0].value_date == "2026-09-01"


def test_preamble_lines_before_the_header_are_skipped(tmp_path):
    f = tmp_path / "export.csv"
    f.write_text(WITH_PREAMBLE, encoding="utf-8")
    txns = parse_statement(str(f))
    assert len(txns) == 1
    assert txns[0].narration.startswith("NEFT CR-SBI-RAO")


def test_written_csv_round_trips_through_load_batch(tmp_path):
    f = tmp_path / "hdfc.csv"
    f.write_text(HDFC, encoding="utf-8")
    batch = tmp_path / "batch"
    write_bank_csv(parse_statement(str(f)), str(batch))
    # load_batch needs the other two files; empty-but-valid is enough here
    (batch / "merchant_orders.csv").write_text(
        "order_id,invoice_no,customer_id,customer_name,amount_paise,due_date,status,channel_expected\n")
    (batch / "gateway_captures.csv").write_text(
        "payment_id,order_id,amount_paise,fee_paise,tax_paise,status,method,captured_at,settlement_id,utr\n")
    _orders, _caps, bank, _truth = load_batch(str(batch))
    assert len(bank) == 3
    assert bank[0].amount_paise == 1_23_456_78     # survived as integer paise


def test_unrecognised_file_fails_loudly_instead_of_guessing(tmp_path):
    f = tmp_path / "mystery.csv"
    f.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(StatementError, match="no recognisable header"):
        parse_statement(str(f))
