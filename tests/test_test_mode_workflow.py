"""Static contract for the manual Test Mode evidence capture workflow."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test-mode-evidence.yml"


def test_manual_test_mode_capture_is_credentialed_and_public_safe():
    assert WORKFLOW.is_file(), "manual Test Mode evidence workflow is missing"
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "pull_request:" not in workflow
    assert "schedule:" not in workflow
    assert "RZP_TEST_KEY_ID: ${{ secrets.RZP_TEST_KEY_ID }}" in workflow
    assert "RZP_TEST_KEY_SECRET: ${{ secrets.RZP_TEST_KEY_SECRET }}" in workflow
    job_setup = workflow.split("    steps:", 1)[0]
    assert "secrets.RZP_TEST_KEY_ID" not in job_setup
    assert "secrets.RZP_TEST_KEY_SECRET" not in job_setup
    assert "test -n \"${RZP_TEST_KEY_ID}\"" in workflow
    assert "test -n \"${RZP_TEST_KEY_SECRET}\"" in workflow
    assert "CAPTURE_LIMIT: ${{ inputs.limit }}" in workflow
    assert '[[ "$CAPTURE_LIMIT" =~ ^[0-9]+$ ]]' in workflow
    assert 'python -m ledger_daemon ingest --out data/test-mode-raw --limit "$CAPTURE_LIMIT"' in workflow
    assert '> /tmp/ledger-daemon-ingest.log 2>&1' in workflow
    assert '> reconciliation-raw.json 2> /tmp/ledger-daemon-reconcile.log' in workflow
    assert "cat /tmp/ledger-daemon-ingest.log" not in workflow
    assert "cat /tmp/ledger-daemon-reconcile.log" not in workflow
    assert '--limit "${{ inputs.limit }}"' not in workflow
    assert "python -m ledger_daemon reconcile --batch data/test-mode-raw" in workflow
    assert 'Path("data/test-mode-raw/razorpay_test_mode_receipt.json")' in workflow
    assert '"batch_manifest_sha256"' in workflow
    assert '"settlement_recon_period"' in workflow
    assert '"redaction": "counts-only"' in workflow

    upload = workflow.split("uses: actions/upload-artifact@v4", 1)[1]
    for safe_path in (
        "evidence/razorpay-test-mode-receipt.json",
        "evidence/test-mode-reconciliation-manifest.json",
    ):
        assert safe_path in upload
    for raw_path in (
        "data/test-mode-raw",
        "merchant_orders.csv",
        "gateway_captures.csv",
        "bank_statement.csv",
        "reconciliation-raw.json",
        ".sqlite",
    ):
        assert raw_path not in upload


def test_raw_test_mode_capture_paths_are_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for raw_path in (
        "data/live/", "data/test-mode/", "data/test-mode-raw/",
        "reconciliation-raw.json",
    ):
        assert raw_path in ignored
