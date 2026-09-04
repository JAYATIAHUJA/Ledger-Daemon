import json
from types import SimpleNamespace

from ledger_daemon import cli


def test_baseline_contract_records_dataset_and_safety_metrics(tmp_path):
    report = SimpleNamespace(
        seed=42,
        n=500,
        dcpr=1.0,
        false_hold_rate=0.04,
        match_rate=0.968,
        wrong_paise={"LD": 0},
    )
    path = tmp_path / "baseline-contract.json"

    cli.write_baseline_contract(str(path), report, elapsed_s=1.25, profile="clean")

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "dataset": {
            "kind": "synthetic",
            "n": 500,
            "profile": "clean",
            "seed": 42,
        },
        "dcpr": 1.0,
        "elapsed_ms": 1250,
        "false_hold_rate": 0.04,
        "match_rate": 0.968,
        "wrongly_chased_paise": 0,
    }


def test_demo_writes_the_measured_baseline_contract(tmp_path, capsys):
    assert cli.main([
        "demo",
        "--seed", "42",
        "--n", "100",
        "--out", str(tmp_path),
    ]) == 0

    path = tmp_path / "eval" / "baseline-contract.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["dataset"] == {
        "kind": "synthetic",
        "n": 100,
        "profile": "clean",
        "seed": 42,
    }
    assert body["elapsed_ms"] >= 0
    assert body["wrongly_chased_paise"] == 0

    output = capsys.readouterr().out
    assert "Downstream Control Demonstration: Why Correct Reconciliation Matters" in output
    assert "Track 03" not in output
