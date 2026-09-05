"""Executor adapter selection stays offline unless test mode is explicit."""

import urllib.request

import pytest

from ledger_daemon.executor import (
    LiveRazorpayAdapter,
    MockRazorpayAdapter,
    default_adapter,
)
from ledger_daemon.models import Order


def _order() -> Order:
    return Order(
        "ORD-ADAPTER", "INV-ADAPTER", "CUST-ADAPTER", "DEMO CUSTOMER",
        10_000, "2026-09-05", "unpaid", "gateway",
    )


def test_default_adapter_keeps_synthetic_execution_offline_with_test_credentials(monkeypatch):
    """A demo must not become networked merely because its shell has test keys."""
    monkeypatch.setenv("RZP_TEST_KEY_ID", "rzp_test_demo")
    monkeypatch.setenv("RZP_TEST_KEY_SECRET", "test-secret")

    def network_call(*_args, **_kwargs):
        raise AssertionError("the default synthetic adapter must not call the network")

    monkeypatch.setattr(urllib.request, "urlopen", network_call)
    adapter = default_adapter()

    result = adapter.create_payment_link(_order(), 10_000)

    assert isinstance(adapter, MockRazorpayAdapter)
    assert result["id"].startswith("plink_mock_")


def test_explicit_test_mode_selection_uses_complete_test_credentials(monkeypatch):
    monkeypatch.setenv("RZP_TEST_KEY_ID", "rzp_test_demo")
    monkeypatch.setenv("RZP_TEST_KEY_SECRET", "test-secret")

    adapter = default_adapter(test_mode=True)

    assert isinstance(adapter, LiveRazorpayAdapter)
    assert adapter.key_id == "rzp_test_demo"


@pytest.mark.parametrize(
    ("key_id", "key_secret", "message"),
    [
        ("", "test-secret", "requires both RZP_TEST_KEY_ID and RZP_TEST_KEY_SECRET"),
        ("rzp_test_demo", "", "requires both RZP_TEST_KEY_ID and RZP_TEST_KEY_SECRET"),
        ("rzp_live_demo", "live-secret", "live-mode credentials are refused"),
    ],
)
def test_explicit_test_mode_selection_rejects_invalid_credentials(
        monkeypatch, key_id, key_secret, message):
    monkeypatch.setenv("RZP_TEST_KEY_ID", key_id)
    monkeypatch.setenv("RZP_TEST_KEY_SECRET", key_secret)

    with pytest.raises(ValueError, match=message):
        default_adapter(test_mode=True)


def test_live_adapter_constructor_refuses_live_mode_credentials():
    with pytest.raises(ValueError, match="live-mode credentials are refused"):
        LiveRazorpayAdapter("rzp_live_demo", "live-secret")
