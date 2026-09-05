from dataclasses import asdict
import json

from ledger_daemon.datagen import (
    FINANCE_PROFILE_NAMES, finance_scenarios, generate, load_finance_events,
)
from ledger_daemon.cli import main
from ledger_daemon.certificates import source_hash_map, source_rows
from ledger_daemon.identities import verify_invoice_identity, verify_settlement_identity


def test_finance_scenarios_cover_declared_edge_case_matrix():
    scenarios = finance_scenarios(42)
    assert set(scenarios) == set(FINANCE_PROFILE_NAMES)
    assert set(scenarios) == {
        "full_refund", "partial_refund", "open_dispute", "closed_dispute",
        "positive_adjustment", "negative_adjustment", "fee_reversal",
        "gst_variance", "cross_period_settlement", "split_capture_payout",
        "tds", "missing_source",
    }


def test_finance_scenarios_are_seed_replayable_and_labelled():
    first = {name: asdict(value) for name, value in finance_scenarios(91).items()}
    second = {name: asdict(value) for name, value in finance_scenarios(91).items()}
    assert first == second
    assert all(value["expected_identity_valid"] in (True, False) for value in first.values())
    assert first["missing_source"]["expected_identity_valid"] is False


def test_every_generated_profile_has_the_declared_oracle_outcome():
    for name, scenario in finance_scenarios(42).items():
        if name == "tds":
            result = verify_invoice_identity(
                scenario.order, scenario.captures, scenario.refunds, scenario.tds_paise,
                tds_source_ref=scenario.ledger_entries[0].entry_id,
            )
        else:
            result = verify_settlement_identity(
                scenario.settlement, scenario.captures, scenario.refunds, scenario.adjustments
            )
        assert result.valid is scenario.expected_identity_valid, name


def test_generated_batch_round_trips_finance_events_with_manifest_provenance(tmp_path):
    paths = generate(42, 100, str(tmp_path))
    first_bytes = (tmp_path / "finance_events.jsonl").read_bytes()
    events = load_finance_events(str(tmp_path))
    manifest = json.loads((tmp_path / "source_manifest.json").read_text(encoding="utf-8"))

    assert paths["finance_events.jsonl"] == str(tmp_path / "finance_events.jsonl")
    assert paths["finance_truth.json"] == str(tmp_path / "finance_truth.json")
    assert events
    assert manifest["sources"]["finance_event"]["accepted"] == len(events)
    assert len(manifest["sources"]["finance_event"]["source_hashes"]) == len(events)
    proof_hashes = set(source_hash_map(source_rows([], [], [], finance_events=events)).values())
    assert proof_hashes == set(manifest["sources"]["finance_event"]["source_hashes"])

    generate(42, 100, str(tmp_path))
    assert (tmp_path / "finance_events.jsonl").read_bytes() == first_bytes


def test_generated_identity_certificate_verifies_through_product_cli(tmp_path, capsys):
    paths = generate(42, 100, str(tmp_path))
    proof = tmp_path / "finance-proof-partial_refund.json"
    assert proof.exists()
    assert paths["finance_identity_sources.jsonl"] == str(
        tmp_path / "finance_identity_sources.jsonl"
    )

    exit_code = main(["verify-proof", str(proof), "--sources", str(tmp_path)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VALID"
