import ast
import importlib
import inspect

import pytest


@pytest.fixture
def proof_world():
    certificates = importlib.import_module("ledger_daemon.certificates")
    AmountTerm = certificates.AmountTerm
    ProofCertificate = certificates.ProofCertificate
    from ledger_daemon.source_contracts import sha256_hex

    rows = [
        {
            "order_id": "ORD-1", "invoice_no": "INV-1", "customer_id": "CUS-1",
            "customer_name": "Ada", "amount_paise": 10_000, "due_date": "2026-09-01",
            "status": "partial", "channel_expected": "bank_transfer",
        },
        {
            "payment_id": "PAY-1", "order_id": "ORD-1", "amount_paise": 10_000,
            "fee_paise": 0, "tax_paise": 0, "status": "captured", "method": "upi",
            "captured_at": "2026-09-01", "settlement_id": "SET-1", "utr": "UTR-1",
        },
        {
            "payment_id": "REF-1", "order_id": "ORD-1", "amount_paise": -2_500,
            "fee_paise": 0, "tax_paise": 0, "status": "refund", "method": "upi",
            "captured_at": "2026-09-01", "settlement_id": "SET-1", "utr": "UTR-1",
        },
        {
            "txn_id": "TXN-1", "value_date": "2026-09-02", "amount_paise": 7_500,
            "credit_debit": "credit", "utr": "UTR-1",
            "narration": "RAZORPAYSETTLEMENT-SET-1",
            "balance_after": 50_000,
        },
    ]
    hashes = {
        "ORD-1": sha256_hex(rows[0]), "PAY-1": sha256_hex(rows[1]),
        "REF-1": sha256_hex(rows[2]), "TXN-1": sha256_hex(rows[3]),
    }
    common = dict(
        order_id="ORD-1",
        verdict="partially_paid",
        source_hashes={**hashes, "BATCH_ROOT": certificates.batch_root(hashes)},
        amount_terms=(
            AmountTerm("invoice", 10_000, "ORD-1", "amount_paise"),
            AmountTerm("money_received", -7_500),
            AmountTerm("delta_due", -2_500),
            AmountTerm("gateway_capture", 10_000, "PAY-1", "amount_paise"),
            AmountTerm("gateway_fee", 0, "PAY-1", "fee_paise"),
            AmountTerm("gateway_gst", 0, "PAY-1", "tax_paise"),
            AmountTerm("gateway_refund", -2_500, "REF-1", "amount_paise"),
            AmountTerm("gateway_fee", 0, "REF-1", "fee_paise"),
            AmountTerm("gateway_gst", 0, "REF-1", "tax_paise"),
            AmountTerm("bank_credit", -7_500, "TXN-1", "amount_paise"),
        ),
        money_received_paise=7_500,
        delta_due_paise=2_500,
        rule_ids=("RECON.pass3_settlement_id", "VERDICT.partially_paid"),
        config_hash="c" * 64,
        calibration_id="calibration:test",
        generated_at="2026-09-05T00:00:00Z",
    )
    return ProofCertificate.create(**common), rows, common


def _verify(certificate, rows, **kwargs):
    verifier = importlib.import_module("ledger_daemon.verifier")
    return verifier.verify_certificate(certificate, rows, **kwargs)


def test_verifier_is_independent_of_reconciliation_and_scoring_modules():
    verifier = importlib.import_module("ledger_daemon.verifier")
    tree = ast.parse(inspect.getsource(verifier))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        (node.module or "").split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported.isdisjoint({"recon", "fs", "conformal"})


def test_valid_certificate_recomputes_sources_arithmetic_and_identities(proof_world):
    certificate, rows, _ = proof_world
    result = _verify(
        certificate,
        rows,
        expected_config_hash="c" * 64,
        expected_calibration_id="calibration:test",
    )
    assert result.valid
    assert result.error_codes == ()


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ({"verdict": "settled_clean"}, "PROOF_HASH_MISMATCH"),
        ({"money_received_paise": 7_499}, "PROOF_HASH_MISMATCH"),
        ({"source_hashes": {"ORD-1": "0" * 64, "TXN-1": "0" * 64}}, "PROOF_HASH_MISMATCH"),
        ({"rule_ids": ("RECON.allow_everything",)}, "PROOF_HASH_MISMATCH"),
        ({"calibration_id": "calibration:evil"}, "PROOF_HASH_MISMATCH"),
        ({"proof_hash": "0" * 64}, "PROOF_HASH_MISMATCH"),
    ],
)
def test_direct_tampering_is_detected(proof_world, change, expected_error):
    from dataclasses import replace

    certificate, rows, _ = proof_world
    attacked = replace(certificate, **change)
    assert expected_error in _verify(attacked, rows).error_codes


def test_rehashed_one_paise_attack_fails_source_and_equation_checks(proof_world):
    certificates = importlib.import_module("ledger_daemon.certificates")
    certificate, rows, common = proof_world
    terms = list(certificate.amount_terms)
    terms[1] = certificates.AmountTerm("received", -7_499, "TXN-1", "amount_paise")
    attacked = certificates.ProofCertificate.create(**{**common, "amount_terms": tuple(terms)})

    errors = _verify(attacked, rows).error_codes
    assert "AMOUNT_TERM_SOURCE_MISMATCH" in errors
    assert "AMOUNT_EQUATION_FAILED" in errors


def test_rehashed_balanced_one_paise_attack_still_fails_evidence_amount(proof_world):
    certificates = importlib.import_module("ledger_daemon.certificates")
    _, rows, common = proof_world
    attacked = certificates.ProofCertificate.create(
        **{
            **common,
            "amount_terms": (
                certificates.AmountTerm("invoice", 10_000, "ORD-1", "amount_paise"),
                certificates.AmountTerm("money_received", -7_499),
                certificates.AmountTerm("delta_due", -2_501),
            ),
            "money_received_paise": 7_499,
            "delta_due_paise": 2_501,
        }
    )

    assert "EVIDENCE_AMOUNT_MISMATCH" in _verify(attacked, rows).error_codes


def test_rehashed_rule_and_identity_attacks_still_fail(proof_world):
    certificates = importlib.import_module("ledger_daemon.certificates")
    _, rows, common = proof_world
    attacked = certificates.ProofCertificate.create(
        **{
            **common,
            "rule_ids": ("RECON.allow_everything", "VERDICT.partially_paid"),
            "config_hash": "d" * 64,
            "calibration_id": "calibration:evil",
        }
    )

    errors = _verify(
        attacked,
        rows,
        expected_config_hash="c" * 64,
        expected_calibration_id="calibration:test",
    ).error_codes
    assert "RULE_NOT_ALLOWED" in errors
    assert "CONFIG_HASH_MISMATCH" in errors
    assert "CALIBRATION_ID_MISMATCH" in errors


def test_duplicate_source_rows_are_rejected_even_when_hashes_match(proof_world):
    certificate, rows, _ = proof_world
    errors = _verify(certificate, rows + [dict(rows[1])]).error_codes
    assert "DUPLICATE_SOURCE_CONSUMPTION" in errors


def test_rehashed_paid_as_unpaid_forgery_is_rejected_from_complete_batch():
    certificates = importlib.import_module("ledger_daemon.certificates")
    from ledger_daemon.source_contracts import sha256_hex

    rows = [
        {"order_id": "ORD-1", "invoice_no": "INV-1", "customer_id": "CUS-1",
         "customer_name": "Ada", "amount_paise": 10_000, "due_date": "2026-09-01",
         "status": "unpaid", "channel_expected": "gateway"},
        {"payment_id": "PAY-1", "order_id": "ORD-1", "amount_paise": 10_000,
         "fee_paise": 200, "tax_paise": 36, "status": "captured", "method": "upi",
         "captured_at": "2026-09-01", "settlement_id": "SET-1", "utr": "UTR-1"},
        {"txn_id": "TXN-1", "value_date": "2026-09-02", "amount_paise": 9_764,
         "credit_debit": "credit", "utr": "UTR-1", "narration": "SET-1",
         "balance_after": 50_000},
    ]
    hashes = {("PAY-1" if "payment_id" in row else "TXN-1" if "txn_id" in row else "ORD-1"):
              sha256_hex(row) for row in rows}
    forged = certificates.ProofCertificate.create(
        order_id="ORD-1", verdict="genuinely_unpaid",
        source_hashes={"ORD-1": hashes["ORD-1"], "BATCH_ROOT": certificates.batch_root(hashes)},
        amount_terms=(
            certificates.AmountTerm("invoice", 10_000, "ORD-1", "amount_paise"),
            certificates.AmountTerm("unpaid_exposure", -10_000),
        ),
        money_received_paise=0, delta_due_paise=0,
        rule_ids=("RECON.exhausted", "VERDICT.genuinely_unpaid"),
        config_hash="c" * 64, calibration_id="calibration:test",
        generated_at="2026-09-05T00:00:00Z",
    )

    errors = _verify(
        forged, rows, expected_config_hash="c" * 64,
        expected_calibration_id="calibration:test",
    ).error_codes
    assert "NEGATIVE_EVIDENCE_CONTRADICTED" in errors


@pytest.mark.parametrize(("customer_name", "narration"), [
    ("Aarav Consulting", "NEFT-HDFCP123-ARAV-CONSLTNG"),
    ("Tech Solutions", "NEFT-HDFCP123-UNPARSEABLE"),
])
def test_rehashed_bank_paid_as_unpaid_forgery_is_rejected(customer_name, narration):
    certificates = importlib.import_module("ledger_daemon.certificates")
    from ledger_daemon.source_contracts import sha256_hex

    order = {"order_id": "ORD-1", "invoice_no": "INV-1001", "customer_id": "CUS-1",
             "customer_name": customer_name, "amount_paise": 10_000,
             "due_date": "2026-09-01", "status": "unpaid",
             "channel_expected": "bank_transfer"}
    bank = {"txn_id": "TXN-1", "value_date": "2026-09-02", "amount_paise": 10_000,
            "credit_debit": "credit", "utr": "UTR-1",
            "narration": narration, "balance_after": 50_000}
    hashes = {"ORD-1": sha256_hex(order), "TXN-1": sha256_hex(bank)}
    forged = certificates.ProofCertificate.create(
        order_id="ORD-1", verdict="genuinely_unpaid",
        source_hashes={"ORD-1": hashes["ORD-1"], "BATCH_ROOT": certificates.batch_root(hashes)},
        amount_terms=(
            certificates.AmountTerm("invoice", 10_000, "ORD-1", "amount_paise"),
            certificates.AmountTerm("unpaid_exposure", -10_000),
        ), money_received_paise=0, delta_due_paise=0,
        rule_ids=("RECON.exhausted", "VERDICT.genuinely_unpaid"),
        config_hash="c" * 64, calibration_id="calibration:test",
        generated_at="2026-09-05T00:00:00Z",
    )

    errors = _verify(
        forged, [order, bank], expected_config_hash="c" * 64,
        expected_calibration_id="calibration:test",
    ).error_codes
    assert "NEGATIVE_EVIDENCE_CONTRADICTED" in errors


def test_rehashed_certificate_cannot_omit_batch_root_or_money_terms(proof_world):
    certificates = importlib.import_module("ledger_daemon.certificates")
    certificate, rows, common = proof_world
    without_root = certificates.ProofCertificate.create(
        **{**common, "source_hashes": dict(certificate.source_hashes) | {"BATCH_ROOT": None}}
    )
    no_terms = certificates.ProofCertificate.create(**{**common, "amount_terms": ()})

    assert "BATCH_ROOT_MISMATCH" in _verify(without_root, rows).error_codes
    assert "AMOUNT_TERM_SCHEMA_INVALID" in _verify(no_terms, rows).error_codes


def test_rule_verdict_pair_and_identity_expectations_are_mandatory(proof_world):
    certificates = importlib.import_module("ledger_daemon.certificates")
    _, rows, common = proof_world
    mismatched = certificates.ProofCertificate.create(
        **{**common, "rule_ids": ("RECON.exhausted", "VERDICT.partially_paid")}
    )
    errors = _verify(mismatched, rows).error_codes
    assert "RULE_VERDICT_MISMATCH" in errors
    assert "IDENTITY_EXPECTATION_MISSING" in errors


def test_verifier_api_returns_schema_error_instead_of_crashing(proof_world):
    from dataclasses import replace

    certificate, rows, _ = proof_world
    malformed = replace(certificate, money_received_paise="7500")
    result = _verify(
        malformed, rows, expected_config_hash="c" * 64,
        expected_calibration_id="calibration:test",
    )
    assert not result.valid
    assert "CERTIFICATE_SCHEMA_INVALID" in result.error_codes
