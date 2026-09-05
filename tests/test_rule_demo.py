import json

from ledger_daemon.cli import main


def test_learn_rule_demo_exposes_authenticated_lifecycle_and_replay_gate(tmp_path, capsys):
    out = tmp_path / "rule-demo"

    assert main(["learn-rule-demo", "--out", str(out)]) == 0

    report = json.loads((out / "rule-lifecycle.json").read_text(encoding="utf-8"))
    assert [step["status"] for step in report["lifecycle"]] == [
        "PROPOSED", "REPLAY_PASSED", "APPROVED", "ACTIVE",
    ]
    assert [step["version"] for step in report["lifecycle"]] == [1, 2, 3, 4]
    assert report["author"] == "analyst-1"
    assert report["approver"] == "reviewer-1"
    assert report["author"] != report["approver"]
    assert report["replay"] == {
        "attack_cases": 3,
        "confirmed_cases": 3,
        "new_wrong_paise": 0,
        "new_wrong_verdicts": 0,
        "promotable": True,
        "proofs_valid": True,
        "safe_coverage_change": 6,
    }
    assert report["bypass_attempt"] == "REJECTED_BEFORE_REPLAY"
    assert report["old_config_unchanged"] is True
    assert report["active_config_changed"] is True
    assert report["active_rule_identity"].endswith("@v4")
    assert (out / "rule-lifecycle.html").exists()
    assert (out / "rule-lifecycle.sqlite3").exists()
    assert "ACTIVE" in (out / "rule-lifecycle.html").read_text(encoding="utf-8")
    assert "safe learning gate: PASS" in capsys.readouterr().out


def test_learn_rule_demo_is_rerunnable_without_duplicate_state(tmp_path):
    out = tmp_path / "rule-demo"

    assert main(["learn-rule-demo", "--out", str(out)]) == 0
    assert main(["learn-rule-demo", "--out", str(out)]) == 0

    report = json.loads((out / "rule-lifecycle.json").read_text(encoding="utf-8"))
    assert report["lifecycle"][-1]["status"] == "ACTIVE"
    assert report["lifecycle"][-1]["version"] == 4
