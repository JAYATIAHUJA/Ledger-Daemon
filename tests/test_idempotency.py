"""10 concurrent executes -> exactly 1 audit row (AC-6, FR-6.1/6.2)."""

import threading

from ledger_daemon.executor import Executor, event_id_for
from ledger_daemon.models import Order


def test_concurrent_double_execute_single_audit_row(tmp_path):
    execu = Executor(str(tmp_path / "ledger.sqlite3"))
    order = Order("ORD-77", "INV-77", "CUST-77", "MEHTA EXPORTS", 90_000_00,
                  "2026-08-10", "unpaid", "gateway")
    results = []
    lock = threading.Lock()

    def run():
        r = execu.execute(order, "CREATE_PAYMENT_LINK", order.amount_paise, "R_ALLOW")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = execu.audit("ORD-77")
    assert len(rows) == 1  # the DATABASE rejected the duplicates, not app logic
    eid = event_id_for("ORD-77", "CREATE_PAYMENT_LINK", 1)
    assert rows[0]["event_id"] == eid
    assert all(r["event_id"] == eid for r in results)
    assert sum(1 for r in results if not r["idempotent_replay"]) == 1


def test_codebase_has_no_update_or_delete():
    """FR-7.1: append-only — no UPDATE, no DELETE anywhere in the codebase."""
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "ledger_daemon")
    for fname in os.listdir(root):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(root, fname), encoding="utf-8") as fh:
            src = fh.read().upper()
        assert "UPDATE AUDIT" not in src, fname
        assert "DELETE FROM" not in src, fname
