"""Read-only landing experience backed by the current reconciliation batch."""
from __future__ import annotations

import copy
import csv
import io
import json
import zipfile
from dataclasses import asdict
from pathlib import Path

from . import policy
from .certificates import ProofCertificate
from .verifier import verify_certificate

STATIC = Path(__file__).parent / 'static'
ASSETS = {'landing.html', 'landing.css', 'landing.js', 'landscape.png', 'fonts.css', 'cli-demo.json',
          'font-0.ttf', 'font-1.ttf', 'font-2.ttf', 'font-3.ttf', 'font-4.ttf'}


def static_asset(name: str) -> Path | None:
    if name not in ASSETS:
        return None
    path = STATIC / name
    return path if path.is_file() else None


def public_summary(orders, verdicts, decisions, certificates, report, source):
    held = [o for o in orders if decisions[o.order_id].outcome == policy.HOLD]
    rows = [{
        'id': o.order_id, 'amount_paise': o.amount_paise,
        'verdict': verdicts[o.order_id].verdict.value,
        'reason': verdicts[o.order_id].reason,
        'outcome': decisions[o.order_id].outcome,
        'has_proof': o.order_id in certificates,
    } for o in orders]
    return {
        'source': source, 'orders': len(orders), 'held_orders': len(held),
        'held_paise': sum(o.amount_paise for o in held),
        'proofs_attached': len(certificates),
        'verdict_accuracy': report.match_rate if report else None,
        'correct_verdicts': report.matched if report else None,
        'throughput': report.throughput if report else None,
        'wrongly_chased_paise': report.wrong_paise['LD'] if report else None,
        'false_hold_rate': report.false_hold_rate if report else None,
        'model': {'name': 'Deterministic engine', 'measured': False,
                  'detail': 'Optional model uplift has not been demonstrated. No LLM makes financial decisions.'},
        'rows': rows,
    }


def proof_demo_copy(payload):
    changed = copy.deepcopy(payload)
    if changed.get('amount_terms'):
        changed['amount_terms'][0]['amount_paise'] += 1
    else:
        changed['money_received_paise'] = changed.get('money_received_paise', 0) + 1
    return changed


def check_proof(certificate, rows, *, tamper=False, config_hash='', calibration_id=''):
    original = certificate.to_dict()
    payload = proof_demo_copy(original) if tamper else original
    checked = ProofCertificate.from_json(json.dumps(payload))
    result = verify_certificate(checked, rows,
                               expected_config_hash=config_hash or None,
                               expected_calibration_id=calibration_id or None)
    return {'valid': result.valid, 'errors': list(result.error_codes),
            'tampered': tamper, 'certificate': payload,
            'note': 'One paise changed in a temporary copy; the issued proof is unchanged.' if tamper
                    else 'Independently checked against this batch and its configuration.'}


def batch_zip(orders, captures, bank, finance_events=None, *, proof_manifest=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name, records in [('merchant_orders.csv', orders),
                              ('gateway_captures.csv', captures),
                              ('bank_statement.csv', bank)]:
            if not records:
                continue
            text = io.StringIO(newline='')
            rows = [asdict(row) for row in records]
            writer = csv.DictWriter(text, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            bundle.writestr(name, text.getvalue())
        if finance_events:
            from .finance_events import encode_finance_event
            bundle.writestr('finance_events.jsonl', ''.join(
                json.dumps(encode_finance_event(event), ensure_ascii=False) + '\n'
                for event in finance_events))
        if proof_manifest:
            bundle.writestr(
                'proof-manifest.json',
                json.dumps(proof_manifest, indent=2, sort_keys=True) + '\n',
            )
    return output.getvalue()
