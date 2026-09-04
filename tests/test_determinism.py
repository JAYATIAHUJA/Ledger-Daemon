"""Same seed -> byte-identical files and identical report hash (FR-1.2, NFR-3)."""

import hashlib
import json
import os

from ledger_daemon.datagen import generate, load_batch
from ledger_daemon.evaluate import evaluate, render_report
from ledger_daemon.recon import reconcile


def _hash_dir(d):
    out = {}
    for name in sorted(os.listdir(d)):
        with open(os.path.join(d, name), "rb") as fh:
            out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_same_seed_byte_identical_files(tmp_path):
    generate(42, 200, str(tmp_path / "a"))
    generate(42, 200, str(tmp_path / "b"))
    assert _hash_dir(tmp_path / "a") == _hash_dir(tmp_path / "b")


def test_different_seed_differs(tmp_path):
    generate(42, 200, str(tmp_path / "a"))
    generate(43, 200, str(tmp_path / "c"))
    assert _hash_dir(tmp_path / "a") != _hash_dir(tmp_path / "c")


def test_same_seed_identical_report_hash(tmp_path):
    generate(42, 200, str(tmp_path / "a"))
    orders, captures, bank, truth = load_batch(str(tmp_path / "a"))
    digests = set()
    for _ in range(2):
        result = reconcile(orders, captures, bank)
        report = evaluate(42, orders, captures, result, truth)
        body = render_report(report)
        # strip the (timing-dependent) throughput line; everything else must be identical
        body = "\n".join(l for l in body.splitlines() if not l.startswith("Throughput"))
        digests.add(hashlib.sha256(body.encode()).hexdigest())
    assert len(digests) == 1


def test_generated_batch_emits_complete_source_provenance(tmp_path):
    out = tmp_path / "batch"
    generate(42, 100, str(out))

    manifest = json.loads((out / "source_manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "1"
    assert manifest["sources"]["order"]["accepted"] == 100
    assert len(manifest["sources"]["order"]["source_hashes"]) == 100
    assert manifest["sources"]["capture"]["quarantined"] == 0
    assert manifest["sources"]["bank_txn"]["quarantined"] == 0
