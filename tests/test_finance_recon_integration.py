from ledger_daemon import certificates
from ledger_daemon.cli import main
from ledger_daemon.datagen import load_finance_events
from dataclasses import asdict, replace
import json

from ledger_daemon.finance_events import Dispute
from ledger_daemon.models import Order, Verdict
from ledger_daemon.recon import FULL, reconcile
from ledger_daemon.verifier import verify_certificate
from ledger_daemon.ingest import write_batch


def _order():
    return Order("ORD-1", "INV-1", "CUST-1", "ACME LTD", 10_000,
                 "2026-08-10", "paid", "gateway")


def test_open_typed_dispute_freezes_order_before_payment_matching():
    dispute = Dispute("disp-1", "pay-1", "ORD-1", 10_000, "open", "2026-08-11")
    verdict = reconcile([_order()], [], [], finance_events=[dispute]).verdicts["ORD-1"]
    assert verdict.verdict is Verdict.CHARGEBACK_OPEN
    assert verdict.evidence.pass_used == "finance_event_dispute"
    assert verdict.evidence.source_rows == ["disp-1"]


def test_closed_dispute_does_not_fabricate_an_open_chargeback():
    dispute = Dispute("disp-1", "pay-1", "ORD-1", 10_000, "closed", "2026-08-11")
    verdict = reconcile([_order()], [], [], finance_events=[dispute]).verdicts["ORD-1"]
    assert verdict.verdict is not Verdict.CHARGEBACK_OPEN


def test_open_dispute_verdict_produces_independently_verifiable_proof():
    order = _order()
    dispute = Dispute("disp-1", "pay-1", "ORD-1", 10_000, "open", "2026-08-11")
    verdict = reconcile([order], [], [], finance_events=[dispute]).verdicts["ORD-1"]
    rows = certificates.source_rows([order], [], [], finance_events=[dispute])
    hashes = certificates.source_hash_map(rows)
    certificate = certificates.build_certificate(
        order, verdict, hashes, certificates.recon_config_hash(FULL), "cal-1", rows=rows
    )

    result = verify_certificate(
        certificate, rows,
        expected_config_hash=certificates.recon_config_hash(FULL),
        expected_calibration_id="cal-1",
    )

    assert result.valid, result.error_codes
    assert "RECON.finance_event_dispute" in certificate.rule_ids


def test_verifier_rejects_closed_dispute_even_when_attacker_rehashes_sources():
    order = _order()
    open_dispute = Dispute("disp-1", "pay-1", "ORD-1", 10_000, "open", "2026-08-11")
    verdict = reconcile([order], [], [], finance_events=[open_dispute]).verdicts["ORD-1"]
    closed_dispute = replace(open_dispute, status="closed")
    forged_rows = [asdict(order), asdict(closed_dispute)]
    hashes = certificates.source_hash_map(forged_rows)
    forged = certificates.build_certificate(
        order, verdict, hashes, certificates.recon_config_hash(FULL), "cal-1",
        rows=forged_rows,
    )
    result = verify_certificate(
        forged, forged_rows,
        expected_config_hash=certificates.recon_config_hash(FULL),
        expected_calibration_id="cal-1",
    )
    assert not result.valid
    assert "EVIDENCE_STATUS_MISMATCH" in result.error_codes


def test_verify_proof_cli_reloads_finance_event_sources(tmp_path, capsys):
    order = _order()
    dispute = Dispute("disp-1", "pay-1", "ORD-1", 10_000, "open", "2026-08-11")
    write_batch(str(tmp_path / "batch"), [asdict(order)], [], [],
                finance_events=[{"event_type": "dispute", **asdict(dispute)}])
    events = load_finance_events(str(tmp_path / "batch"))
    verdict = reconcile([order], [], [], finance_events=events).verdicts["ORD-1"]
    rows = certificates.source_rows([order], [], [], finance_events=events)
    hashes = certificates.source_hash_map(rows)
    config_hash = certificates.recon_config_hash(FULL)
    certificate = certificates.build_certificate(
        order, verdict, hashes, config_hash, "cal-1", rows=rows
    )
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(certificate.to_json(), encoding="utf-8")

    exit_code = main([
        "verify-proof", str(proof_path), "--sources", str(tmp_path / "batch"),
        "--config-hash", config_hash, "--calibration-id", "cal-1",
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VALID"
