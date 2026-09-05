#!/usr/bin/env python3
"""Capture a running synthetic demo as a portable GitHub Pages site.

Run the local controller first, then:
    python scripts/build_pages.py --url http://127.0.0.1:7043 --out docs

Only captured responses and bundled assets are written. Existing unrelated
files are preserved. This command does not publish the site or push Git.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import json
from pathlib import Path
import re
import shutil
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


STATIC = Path(__file__).resolve().parents[1] / 'ledger_daemon' / 'static'
ASSET_SUFFIXES = {'.css', '.js', '.json', '.png', '.ttf', '.txt', '.md'}


def fetch_url(url: str) -> bytes:
    request = Request(url, headers={'User-Agent': 'Ledger-Daemon-Pages-Exporter/1'})
    parsed = urlsplit(url)
    path = parsed.path + ('?' + parsed.query if parsed.query else '')
    transient_codes = {errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
                       10061, 10054, 10060}
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as error:
            # A missing route or failed application response will not recover
            # by queueing the same read again.
            raise OSError(f'GET {path} failed: HTTP {error.code} {error.reason}') from error
        except OSError as error:
            reason = error.reason if isinstance(error, URLError) else error
            transient = (isinstance(reason, (TimeoutError, ConnectionRefusedError, ConnectionResetError))
                         or getattr(reason, 'errno', None) in transient_codes
                         or getattr(reason, 'winerror', None) in transient_codes)
            if transient and attempt < 2:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise OSError(f'GET {path} failed after {attempt + 1} attempts: {reason}') from error


def rewrite_paths(document: str) -> str:
    """Keep URLs within a GitHub project site's repository subdirectory."""
    document = re.sub(r'(?<![\w./:])/assets/', './assets/', document)
    document = re.sub(r'(?<![\w./:])/download/batch\.zip', './data/sample-batch.zip', document)
    document = re.sub(r'(?<![\w./:])/download/report\.json', './data/report.json', document)
    document = re.sub(r'''((?:href|src)\s*=\s*["'])/app(?=[#?"'])''',
                      r'\1./demo.html', document)
    return re.sub(r'''(href\s*=\s*)(["'])/\2''', r'\1\2./index.html\2', document)


STATIC_REVIEW = r"""function resolve(btn, oid, res, version) {
  const dialog = document.getElementById('static-review-dialog');
  dialog.querySelector('[data-review-order]').textContent = oid;
  dialog.showModal();
}
"""


def static_demo(document: str, orders: int) -> str:
    """Retain the actual controller UI, with review writes explained locally."""
    document, replaced = re.subn(
        r"async function resolve\(.*?(?=document\.addEventListener\('input')",
        STATIC_REVIEW, document, count=1, flags=re.DOTALL,
    )
    if replaced != 1 or re.search(r'''fetch\s*\(\s*["']/resolve''', document):
        raise ValueError('Controller review script changed; refusing to publish a broken review action.')
    banner = f"""
<aside aria-label="About this sample" style="padding:14px 24px;background:#e9eddf;color:#263a2d;border-bottom:1px solid #c6cebd;font:14px/1.5 sans-serif">
  <strong>Sample workspace: {orders} synthetic orders.</strong>
  Explore the results, evidence, and review steps. Saving reviews requires the local app.
  <a href="./index.html#run-locally" style="color:inherit;text-decoration:underline">Run the app on your computer</a>.
</aside>
"""
    dialog = """
<dialog id="static-review-dialog" aria-labelledby="static-review-title" style="max-width:520px;padding:28px;border:1px solid #c6cebd;border-radius:14px;background:#f7f5ed;color:#263a2d">
  <h2 id="static-review-title">Save a review in the local app</h2>
  <p>This website shows a saved sample. It cannot save a decision for <strong data-review-order></strong>.</p>
  <p>Run Ledger Daemon on your computer to review cases and keep their history.</p>
  <p><a href="./index.html#run-locally">See the install and run commands</a></p>
  <form method="dialog"><button type="submit">Back to sample</button></form>
</dialog>
"""
    document, bodies = re.subn(r'(<body\b[^>]*>)', lambda match: match[0] + banner,
                              document, count=1, flags=re.IGNORECASE)
    if bodies != 1 or '</body>' not in document:
        raise ValueError('Controller response is missing its HTML body.')
    document = document.replace('</body>', dialog + '</body>', 1)
    return rewrite_paths(document)


def build_pages(url: str, out: Path, *, fetch=fetch_url, assets_dir: Path = STATIC) -> dict:
    url = url.rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('--url must be the HTTP address of a running controller.')

    def get_json(path):
        return json.loads(fetch(url + path))

    # Read every server response before writing the export so capture failures
    # leave an existing Pages site intact.
    summary = get_json('/api/summary')
    if not str(summary.get('source', '')).lower().startswith('synthetic'):
        raise ValueError('Pages export requires a synthetic demo; start the UI without --dir.')
    readers = get_json('/api/readers')
    report = fetch(url + '/download/report.json')
    json.loads(report)
    batch = fetch(url + '/download/batch.zip')
    app = static_demo(fetch(url + '/app').decode('utf-8'), int(summary['orders']))
    proof_ids = [row['id'] for row in summary['rows'] if row.get('has_proof')]

    def capture_proof(order_id):
        route = '/api/proof/' + quote(order_id, safe='')
        return order_id, {'original': get_json(route), 'tampered': get_json(route + '?tamper=1')}

    # The stdlib controller's accept backlog is small. Leave room for a browser
    # while capturing the proof pairs, and retry transient connection failures.
    with ThreadPoolExecutor(max_workers=4) as pool:
        proofs = dict(pool.map(capture_proof, proof_ids))

    index = (assets_dir / 'landing.html').read_text(encoding='utf-8')
    index = rewrite_paths(index)
    index, bodies = re.subn(r'<body\b', '<body data-site-mode="static"', index,
                           count=1, flags=re.IGNORECASE)
    if bodies != 1:
        raise ValueError('Landing template is missing its HTML body.')

    out = Path(out)
    assets_out, data_out = out / 'assets', out / 'data'
    assets_out.mkdir(parents=True, exist_ok=True)
    data_out.mkdir(parents=True, exist_ok=True)
    for asset in sorted(assets_dir.iterdir()):
        if asset.is_file() and asset.suffix.lower() in ASSET_SUFFIXES:
            shutil.copyfile(asset, assets_out / asset.name)
    (out / 'index.html').write_text(index, encoding='utf-8')
    (out / 'demo.html').write_text(app, encoding='utf-8')
    (out / '.nojekyll').write_text('', encoding='utf-8')
    (data_out / 'sample-batch.zip').write_bytes(batch)
    (data_out / 'report.json').write_bytes(report)
    capture = {'orders': summary['orders'], 'proofs': len(proofs),
               'captured_at': datetime.now(timezone.utc).isoformat(),
               'source': summary.get('source', 'synthetic sample'),
               'verification': 'Recorded original and tampered-copy results from the local verifier.'}
    for name, payload in [('summary', summary), ('readers', readers), ('proofs', proofs), ('capture', capture)]:
        (data_out / (name + '.json')).write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                                               encoding='utf-8')
    return capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default='http://127.0.0.1:7043', help='Running local controller address')
    parser.add_argument('--out', type=Path, default=Path('docs'), help='Pages output directory (default: docs)')
    args = parser.parse_args()
    try:
        result = build_pages(args.url, args.out)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'Pages export failed: {error}\n')
    print(f"Exported {result['orders']} orders and {result['proofs']} proof comparisons to {args.out.resolve()}")


if __name__ == '__main__':
    main()
