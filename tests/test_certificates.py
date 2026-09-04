import importlib
import json

import pytest

from ledger_daemon.cli import main
from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.models import Evidence, Order, OrderVerdict, Verdict
from ledger_daemon.recon import FULL, reconcile


def test_certificate_canonical_round_trip_and_hash_are_stable():
    certificates = importlib.import_module("ledger_daemon.certificates")
    AmountTerm = certificates.AmountTerm
    ProofCertificate = certificates.ProofCertificate

    certificate = ProofCertificate.create(
        order_id="ORD-1",
        verdict="partially_paid",
        source_hashes={"ORD-1": "a" * 64, "TXN-1": "b" * 64},
        amount_terms=(
            AmountTerm("invoice", 10_000, "ORD-1", "amount_paise"),
            AmountTerm("received", -7_500, "TXN-1", "amount_paise"),
            AmountTerm("delta_due", -2_500),
        ),
        money_received_paise=7_500,
        delta_due_paise=2_500,
        rule_ids=("RECON.pass4_fuzzy", "VERDICT.partially_paid"),
        config_hash="c" * 64,
        calibration_id="calibration:test",
        generated_at="2026-09-05T00:00:00Z",
    )

    encoded = certificate.to_json()
    decoded = ProofCertificate.from_json(encoded)

    assert decoded == certificate
    assert len(certificate.proof_hash) == 64
    assert json.loads(encoded)["proof_hash"] == certificate.proof_hash
    assert encoded == decoded.to_json()


def test_proof_hash_ignores_mapping_insertion_order_but_not_financial_facts():
    certificates = importlib.import_module("ledger_daemon.certificates")
    ProofCertificate = certificates.ProofCertificate

    common = dict(
        order_id="ORD-1",
        verdict="settled_clean",
        amount_terms=(),
        money_received_paise=10_000,
        delta_due_paise=0,
        rule_ids=("RECON.pass1_exact_utr", "VERDICT.settled_clean"),
        config_hash="c" * 64,
        calibration_id="calibration:test",
        generated_at="2026-09-05T00:00:00Z",
    )
    first = ProofCertificate.create(
        source_hashes={"ORD-1": "a" * 64, "TXN-1": "b" * 64}, **common
    )
    second = ProofCertificate.create(
        source_hashes={"TXN-1": "b" * 64, "ORD-1": "a" * 64}, **common
    )
    changed = ProofCertificate.create(
        source_hashes={"ORD-1": "a" * 64, "TXN-1": "b" * 64},
        **{**common, "money_received_paise": 9_999},
    )

    assert first.proof_hash == second.proof_hash
    assert first.to_json() == second.to_json()
    assert changed.proof_hash != first.proof_hash


def test_certificate_parser_rejects_float_money_and_unknown_fields():
    certificates = importlib.import_module("ledger_daemon.certificates")
    valid = certificates.ProofCertificate.create(
        order_id="ORD-1", verdict="ambiguous", source_hashes={"ORD-1": "a" * 64},
        amount_terms=(), money_received_paise=0, delta_due_paise=0,
        rule_ids=("RECON.none", "VERDICT.ambiguous"), config_hash="c" * 64,
        calibration_id="calibration:test", generated_at="2026-09-05T00:00:00Z",
    ).to_dict()

    with pytest.raises(ValueError, match="integer paise"):
        certificates.ProofCertificate.from_json(json.dumps({**valid, "money_received_paise": 0.0}))
    with pytest.raises(ValueError, match="schema"):
        certificates.ProofCertificate.from_json(json.dumps({**valid, "surprise": True}))


def test_certificate_hash_binds_risk_provenance_separately_from_conformal_calibration():
    certificates = importlib.import_module("ledger_daemon.certificates")
    common = dict(
        order_id="ORD-1", verdict="genuinely_unpaid", source_hashes={"ORD-1": "a" * 64},
        amount_terms=(), money_received_paise=0, delta_due_paise=0,
        rule_ids=("RECON.exhausted", "VERDICT.genuinely_unpaid"), config_hash="c" * 64,
        calibration_id="conformal:cal-1", generated_at="2026-09-05T00:00:00Z",
        automation_path="probabilistic", score_ppm=900_000,
        risk_calibration_id="risk:cal-1",
    )
    authorized = certificates.ProofCertificate.create(**common, risk_authorized=True)
    unauthorized = certificates.ProofCertificate.create(**common, risk_authorized=False)

    assert authorized.proof_hash != unauthorized.proof_hash
    assert authorized.calibration_id == "conformal:cal-1"
    assert authorized.risk_calibration_id == "risk:cal-1"


def test_builder_copies_verdict_risk_provenance_into_certificate():
    certificates = importlib.import_module("ledger_daemon.certificates")
    order = Order("ORD-1", "INV-1", "CUS-1", "Ada", 10_000, "2026-09-01", "unpaid", "gateway")
    verdict = OrderVerdict(
        "ORD-1", Verdict.GENUINELY_UNPAID, [],
        Evidence("exhausted", automation_path="exact", risk_authorized=True),
    )
    rows = certificates.source_rows([order], [], [])
    hashes = certificates.source_hash_map(rows)

    certificate = certificates.build_certificate(
        order, verdict, hashes, "c" * 64, "conformal:cal-1", rows=rows,
    )

    assert certificate.automation_path == "exact"
    assert certificate.score_ppm == 0
    assert certificate.risk_calibration_id == ""
    assert certificate.risk_authorized is True


def test_generated_batch_emits_one_independently_verifiable_proof_per_order(tmp_path):
    certificates = importlib.import_module("ledger_daemon.certificates")
    verifier = importlib.import_module("ledger_daemon.verifier")
    batch = tmp_path / "batch"
    proofs = tmp_path / "proofs"
    generate(42, 60, str(batch))
    orders, captures, bank, _ = load_batch(str(batch))
    result = reconcile(orders, captures, bank, q_hat=0.001)
    config_hash = certificates.recon_config_hash(FULL)
    calibration_id = certificates.calibration_identity(0.001, "test-calibration")

    manifest = certificates.write_proof_bundle(
        str(proofs), orders, captures, bank, result.verdicts,
        config_hash=config_hash,
        calibration_id=calibration_id,
    )
    source_rows = certificates.source_rows(orders, captures, bank)

    assert manifest["certificate_count"] == len(orders) == 60
    assert len(list(proofs.glob("ORD-*.json"))) == 60
    assert all(verdict.certificate_id for verdict in result.verdicts.values())
    for order in orders:
        encoded = (proofs / f"{order.order_id}.json").read_text(encoding="utf-8")
        proof = certificates.ProofCertificate.from_json(encoded)
        checked = verifier.verify_certificate(
            proof,
            source_rows,
            expected_config_hash=config_hash,
            expected_calibration_id=calibration_id,
        )
        assert checked.valid, (order.order_id, checked.error_codes)

    settlement_proof = next(
        certificates.ProofCertificate.from_json(path.read_text(encoding="utf-8"))
        for path in proofs.glob("ORD-*.json")
        if "RECON.pass" in path.read_text(encoding="utf-8")
        and "gateway_fee" in path.read_text(encoding="utf-8")
    )
    term_names = {term.name for term in settlement_proof.amount_terms}
    assert {"gateway_capture", "gateway_fee", "gateway_gst", "bank_credit"} <= term_names
    refund_repay = next(
        certificates.ProofCertificate.from_json(path.read_text(encoding="utf-8"))
        for path in proofs.glob("ORD-*.json")
        if '"verdict":"refunded_then_repaid"' in path.read_text(encoding="utf-8")
    )
    assert "gateway_refund" in {term.name for term in refund_repay.amount_terms}


def test_proof_bundle_is_byte_deterministic_and_cli_verifies_it(tmp_path, capsys):
    certificates = importlib.import_module("ledger_daemon.certificates")
    batch = tmp_path / "batch"
    generate(7, 20, str(batch))
    orders, captures, bank, _ = load_batch(str(batch))
    config_hash = certificates.recon_config_hash(FULL)
    calibration_id = certificates.calibration_identity(0.01, "fixed")

    dirs = [tmp_path / "proofs-a", tmp_path / "proofs-b"]
    for directory in dirs:
        result = reconcile(orders, captures, bank, q_hat=0.01)
        certificates.write_proof_bundle(
            str(directory), orders, captures, bank, result.verdicts,
            config_hash=config_hash,
            calibration_id=calibration_id,
        )

    first = (dirs[0] / "ORD-1000.json").read_bytes()
    assert first == (dirs[1] / "ORD-1000.json").read_bytes()
    assert main([
        "verify-proof", str(dirs[0] / "ORD-1000.json"),
        "--sources", str(batch),
    ]) == 0
    output = capsys.readouterr().out
    assert "VALID" in output

    isolated = tmp_path / "isolated-proof.json"
    isolated.write_bytes(first)
    assert main(["verify-proof", str(isolated), "--sources", str(batch)]) == 1
    assert "IDENTITY_EXPECTATION_MISSING" in capsys.readouterr().out
    assert main([
        "verify-proof", str(isolated), "--sources", str(batch),
        "--config-hash", config_hash, "--calibration-id", calibration_id,
    ]) == 0


def test_rewriting_bundle_removes_only_stale_manifest_owned_proofs(tmp_path):
    certificates = importlib.import_module("ledger_daemon.certificates")
    batch = tmp_path / "batch"
    proofs = tmp_path / "proofs"
    generate(8, 12, str(batch))
    orders, captures, bank, _ = load_batch(str(batch))
    result = reconcile(orders, captures, bank, q_hat=0.01)
    kwargs = {
        "config_hash": certificates.recon_config_hash(FULL),
        "calibration_id": certificates.calibration_identity(0.01, "fixed"),
    }
    certificates.write_proof_bundle(
        str(proofs), orders, captures, bank, result.verdicts, **kwargs
    )
    keep = orders[:5]
    (proofs / "reviewer-notes.json").write_text("{}", encoding="utf-8")
    certificates.write_proof_bundle(
        str(proofs), keep, captures, bank,
        {order.order_id: result.verdicts[order.order_id] for order in keep}, **kwargs,
    )

    order_proofs = [path for path in proofs.glob("ORD-*.json")]
    assert len(order_proofs) == 5
    assert (proofs / "reviewer-notes.json").exists()


def test_stress_batch_proofs_survive_independent_verification(tmp_path):
    certificates = importlib.import_module("ledger_daemon.certificates")
    verifier = importlib.import_module("ledger_daemon.verifier")
    batch = tmp_path / "stress"
    proofs = tmp_path / "proofs"
    generate(99, 200, str(batch), profile="stress")
    orders, captures, bank, _ = load_batch(str(batch))
    result = reconcile(orders, captures, bank, q_hat=0.001)
    config_hash = certificates.recon_config_hash(FULL)
    calibration_id = certificates.calibration_identity(0.001, "stress-test")
    certificates.write_proof_bundle(
        str(proofs), orders, captures, bank, result.verdicts,
        config_hash=config_hash, calibration_id=calibration_id,
    )
    rows = certificates.source_rows(orders, captures, bank)

    failures = {}
    for path in proofs.glob("ORD-*.json"):
        proof = certificates.ProofCertificate.from_json(path.read_text(encoding="utf-8"))
        checked = verifier.verify_certificate(
            proof, rows, expected_config_hash=config_hash,
            expected_calibration_id=calibration_id,
        )
        if not checked.valid:
            failures[proof.order_id] = checked.error_codes
    assert failures == {}
