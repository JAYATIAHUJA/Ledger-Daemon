"""Controller JavaScript must preserve escapes and landing deep links."""
from ledger_daemon.ui import View, render_html


def test_controller_conflict_message_does_not_emit_a_literal_js_linebreak():
    html = render_html(View(), 'synthetic')
    assert "reloading\\n\\n'" in html
    assert "reloading\n\n'" not in html


def test_controller_honors_landing_deep_links():
    assert 'location.hash.slice(1)' in render_html(View(), 'synthetic')
