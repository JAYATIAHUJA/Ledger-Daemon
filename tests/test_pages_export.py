import io
import json
import zipfile

import pytest
from urllib.error import HTTPError, URLError

from scripts.build_pages import build_pages, fetch_url, rewrite_paths, static_demo
import scripts.build_pages as exporter


def test_snapshot_read_retries_transient_connection_refusals(monkeypatch):
    attempts = []
    delays = []

    def open_response(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise URLError(ConnectionRefusedError(10061, 'connection refused'))
        return io.BytesIO(b'{"valid":true}')

    monkeypatch.setattr(exporter, 'urlopen', open_response)
    monkeypatch.setattr(exporter.time, 'sleep', delays.append)
    assert fetch_url('http://127.0.0.1:7043/api/proof/order-a') == b'{"valid":true}'
    assert len(attempts) == 3
    assert delays == [0.25, 0.5]


def test_snapshot_read_reports_path_without_retrying_http_errors(monkeypatch):
    attempts = []

    def open_response(request, timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url, 404, 'Not Found', {}, None)

    monkeypatch.setattr(exporter, 'urlopen', open_response)
    monkeypatch.setattr(exporter.time, 'sleep', lambda delay: pytest.fail('HTTP 404 must not be retried'))
    with pytest.raises(OSError, match=r'/api/proof/missing.*404'):
        fetch_url('http://127.0.0.1:7043/api/proof/missing')
    assert len(attempts) == 1


def test_snapshot_read_retries_are_bounded_and_name_failed_path(monkeypatch):
    attempts = []

    def open_response(request, timeout):
        attempts.append(request.full_url)
        raise TimeoutError('read timed out')

    monkeypatch.setattr(exporter, 'urlopen', open_response)
    monkeypatch.setattr(exporter.time, 'sleep', lambda delay: None)
    with pytest.raises(OSError, match=r'/api/proof/order-a\?tamper=1.*3 attempts'):
        fetch_url('http://127.0.0.1:7043/api/proof/order-a?tamper=1')
    assert len(attempts) == 3


def test_project_pages_urls_preserve_hashes_and_external_links():
    document = '''<a href="/">Home</a><a href='/app#cases'>Cases</a>
<iframe src="/app#sources"></iframe><link href="/assets/landing.css">
<a href="/download/batch.zip">CSV</a><a href="/download/report.json">Report</a>
<a href="https://example.com/app">External</a>'''
    rewritten = rewrite_paths(document)
    assert 'href="./index.html"' in rewritten
    assert "href='./demo.html#cases'" in rewritten
    assert 'src="./demo.html#sources"' in rewritten
    assert 'href="./assets/landing.css"' in rewritten
    assert './data/sample-batch.zip' in rewritten
    assert './data/report.json' in rewritten
    assert 'https://example.com/app' in rewritten
    assert rewrite_paths(rewritten) == rewritten
    assert rewrite_paths('<a href="https://example.com/assets/file.css">external</a>') == '<a href="https://example.com/assets/file.css">external</a>'


def test_static_demo_keeps_tabs_and_replaces_review_write_with_explanation():
    document = '''<html><body><nav class="tabs">Cases</nav><script>
async function resolve(btn, oid, res, version) {
 const r = await fetch('/resolve', {method:'POST'});
 if (r.ok) location.reload();
}
document.addEventListener('input', e => { console.log(e); });
function showPanel(name) { return name; }
</script></body></html>'''
    result = static_demo(document, 500)
    assert "fetch('/resolve'" not in result
    assert 'function showPanel(name)' in result
    assert 'document.addEventListener(\'input\'' in result
    assert '500 synthetic orders' in result
    assert 'id="static-review-dialog"' in result
    assert 'showModal()' in result
    assert './index.html#run-locally' in result
    assert 'alert(' not in result


def test_export_captures_proofs_downloads_and_required_assets(tmp_path):
    assets = tmp_path / 'source'
    assets.mkdir()
    (assets / 'landing.html').write_text('<html><body><a href="/app#cases">Demo</a></body></html>', encoding='utf-8')
    (assets / 'landing.css').write_text('body{}', encoding='utf-8')
    (assets / 'landing.js').write_text('const test = true;', encoding='utf-8')
    (assets / 'dmsans-OFL.txt').write_text('license', encoding='utf-8')
    output = tmp_path / 'docs'
    output.mkdir()
    (output / 'keep.txt').write_text('existing file', encoding='utf-8')
    payloads = {
        '/api/summary': {'source': 'synthetic world, seed 42, n=2', 'orders': 2, 'rows': [{'id': 'order a', 'has_proof': True}, {'id': 'b', 'has_proof': False}]},
        '/api/readers': {'status': 'measured'},
        '/api/proof/order%20a': {'valid': True, 'certificate': {'order_id': 'order a'}},
        '/api/proof/order%20a?tamper=1': {'valid': False, 'certificate': {'order_id': 'order a'}},
        '/download/report.json': {'matched': 2},
    }
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, 'w') as sample:
        sample.writestr('merchant_orders.csv', 'order_id\na\n')
    called = []

    def fetch(url):
        path = url.removeprefix('http://127.0.0.1:7043')
        called.append(path)
        if path == '/app':
            return b'''<html><body><script>async function resolve(btn, oid, res, version) {fetch('/resolve');}
document.addEventListener('input', e => {});</script></body></html>'''
        if path == '/download/batch.zip':
            return zipped.getvalue()
        return json.dumps(payloads[path]).encode()

    result = build_pages('http://127.0.0.1:7043/', output, fetch=fetch, assets_dir=assets)
    assert result['proofs'] == 1
    assert (output / 'keep.txt').read_text() == 'existing file'
    assert (output / '.nojekyll').exists()
    assert 'data-site-mode="static"' in (output / 'index.html').read_text()
    assert './demo.html#cases' in (output / 'index.html').read_text()
    assert (output / 'assets' / 'dmsans-OFL.txt').read_text() == 'license'
    assert (output / 'data' / 'sample-batch.zip').read_bytes() == zipped.getvalue()
    proofs = json.loads((output / 'data' / 'proofs.json').read_text())
    assert proofs['order a']['original']['valid'] is True
    assert proofs['order a']['tampered']['valid'] is False
    assert len(called) == 7


def test_export_does_not_label_live_financial_data_as_public_synthetic_demo(tmp_path):
    with pytest.raises(ValueError, match='synthetic'):
        build_pages('http://127.0.0.1:7043', tmp_path / 'docs',
                    fetch=lambda url: json.dumps({'source': 'live batch: customer-records'}).encode())
    assert not (tmp_path / 'docs').exists()
