"""Public UI batch labels must not disclose local source details."""

from types import SimpleNamespace as S

from ledger_daemon.ui import View, batch_presentation, public_batch_summary, render_html


def test_imported_batch_presentation_hides_local_origin_and_evaluation_metrics():
    """Changing an imported label back to a path or enabling its score is a leak."""
    report = object()

    presentation = batch_presentation(
        "imported", "C:\\Users\\Lenovo\\private-batch", "made-up evaluation", report)

    assert presentation.source_label == "Imported batch"
    assert presentation.evaluation == ""
    assert presentation.evaluation_report is None
    assert "C:\\Users" not in presentation.source_label
    assert "live" not in presentation.source_label.lower()


def test_imported_api_and_controller_share_the_safe_batch_presentation():
    """Passing the raw source to either public surface would disclose it."""
    report = S(match_rate=1.0, matched=1, throughput=1.0,
               wrong_paise={"LD": 0}, false_hold_rate=0.0)
    presentation = batch_presentation(
        "imported", "C:\\Users\\Lenovo\\private-batch", "measured", report)
    order = S(order_id="order-1", amount_paise=100)
    verdicts = {"order-1": S(verdict=S(value="AMBIGUOUS"), reason="missing evidence")}
    decisions = {"order-1": S(outcome="HOLD")}

    payload = public_batch_summary([order], verdicts, decisions, {}, presentation)
    page = render_html(View(), presentation.source_label)

    assert payload["source"] == "Imported batch"
    assert payload["verdict_accuracy"] is None
    assert "C:\\Users" not in page


def test_test_mode_label_requires_explicit_safe_provenance():
    presentation = batch_presentation(
        "test_mode", "C:\\private\\razorpay-batch", "made-up evaluation", object())

    assert presentation.source_label == "Test Mode batch"
    assert presentation.evaluation == ""
    assert presentation.evaluation_report is None


def test_synthetic_batch_keeps_its_label_and_evaluation():
    report = object()

    presentation = batch_presentation(
        "synthetic", "generated batch", "measured on generated labels", report)

    assert presentation.source_label == "Synthetic batch"
    assert presentation.evaluation == "measured on generated labels"
    assert presentation.evaluation_report is report
