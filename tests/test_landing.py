import copy
from pathlib import Path

import pytest

from ledger_daemon.landing import static_asset, public_summary, check_proof


def test_static_assets_reject_traversal():
    assert static_asset('landing.css').is_file()
    assert static_asset('../executor.py') is None
    assert static_asset('/etc/passwd') is None
    assert static_asset('missing.js') is None


def test_summary_keeps_order_populations_and_actual_model_status():
    from types import SimpleNamespace as S
    orders = [S(order_id='a', amount_paise=100), S(order_id='b', amount_paise=200)]
    verdicts = {x.order_id:S(verdict=S(value='AMBIGUOUS'), reason='evidence missing') for x in orders}
    decisions = {'a':S(outcome='HOLD'), 'b':S(outcome='ALLOW')}
    summary = public_summary(orders, verdicts, decisions, {}, None, 'synthetic test')
    assert summary['orders'] == 2
    assert summary['held_orders'] == 1
    assert summary['held_paise'] == 100
    assert summary['verdict_accuracy'] is None
    assert summary['model']['measured'] is False
    assert 'synthetic' in summary['source']


def test_proof_demo_uses_independent_verifier_without_mutating_bundle():
    from ledger_daemon.certificates import ProofCertificate
    from ledger_daemon.landing import proof_demo_copy
    # A demo mutation must never touch the stored original, even if invalid.
    original = {'order_id':'a', 'proof_hash':'abc', 'amount_terms':[{'amount_paise':100}]}
    snapshot = copy.deepcopy(original)
    changed = proof_demo_copy(original)
    assert original == snapshot
    assert changed != original
    assert changed['proof_hash'] == original['proof_hash']


def test_sample_download_preserves_finance_events_and_proof_manifest():
    import io
    import json
    import zipfile
    from ledger_daemon.finance_events import Refund, decode_finance_event
    from ledger_daemon.landing import batch_zip
    refund = Refund('refund-1', 'payment-1', 'order-1', 200, 'processed', '2026-09-05')
    manifest = {
        'config_hash': 'sha256:config',
        'calibration_id': 'sha256:calibration',
        'certificates': {'order-1': {'proof_hash': 'abc'}},
    }
    with zipfile.ZipFile(
        io.BytesIO(batch_zip([], [], [], [refund], proof_manifest=manifest))
    ) as bundle:
        restored = decode_finance_event(json.loads(bundle.read('finance_events.jsonl')))
        restored_manifest = json.loads(bundle.read('proof-manifest.json'))
    assert restored == refund
    assert restored_manifest == manifest


def test_public_copy_sets_honest_demo_and_evidence_boundaries():
    """Deleting these limits would make the public synthetic demo misleading."""
    root = Path(__file__).resolve().parents[1]
    readme = (root / 'README.md').read_text(encoding='utf-8')
    landing = (root / 'ledger_daemon' / 'static' / 'landing.html').read_text(encoding='utf-8')
    script = (root / 'ledger_daemon' / 'static' / 'landing.js').read_text(encoding='utf-8')

    disclosure = ('Synthetic demo and metrics. It uses a mock executor; no credentialed '
                  'Razorpay Test Mode API run or payment-link creation is included.')
    assert disclosure in readme
    assert disclosure in landing
    assert 'Optional Razorpay Test Mode capability' in readme
    assert '| Test Mode payment link | External object / side effect | Does not move money |' in readme
    assert 'result proof/supporting records' in readme
    assert 'detects changes to exported evidence but cannot prove source systems are truthful' in readme
    assert 'Current sample' in landing
    assert 'GUIDED SAMPLE' in landing
    assert 'Ledger Daemon controller' in landing
    assert 'Open full sample app' in landing
    assert 'recorded synthetic command output' in landing
    assert 'Matched synthetic answer' in landing
    assert 'Orders paused by rules' in landing
    assert 'known generated labels' in script
    assert 'No ground-truth score is available for imported or Test Mode data.' in script
    assert 'Machine-dependent timing' in landing
    assert 'rel="icon"' in landing
    assert 'data:image/svg+xml' in landing
    assert "document.querySelector('.live-label').textContent='Current sample';" in script
    assert "document.querySelector('.tour-frame-label').textContent='GUIDED SAMPLE · SAVED APP RUN';" in script
    assert ".replace('Open full sample app'" not in script
    assert 'fetch Razorpay Test Mode API objects; no real money moves' in readme
    assert '--dir data/test-mode-raw' in readme
    assert 'imported/Test Mode batch' in readme
    assert '/v1/refunds' in readme
    assert '/v1/settlements/recon/combined' in readme
    assert 'recon feed links payments and refunds to settlements' in readme
    assert 'pull real Razorpay test-mode data' not in readme
    assert 'data/live' not in readme
    assert 'live demo' not in (readme + landing + script).lower()
