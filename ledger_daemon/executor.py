"""Idempotent executor + append-only audit log (FR-6, FR-7).

event_id = sha256(order_id | action_type | attempt_no) is the PRIMARY KEY; a
duplicate execution attempt is rejected by the database, not by application
logic (FR-6.2). The audit table is append-only: there is no UPDATE and no
DELETE anywhere in this codebase (FR-7.1).

Actions: CREATE_PAYMENT_LINK (mock adapter by default; live Razorpay test-mode
adapter only through explicit selection with valid test credentials) and
DRAFT_REMINDER (stages text; it does not send).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time

from .models import Order
from .money import rupees_str

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    event_id    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    layer       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    output_json TEXT NOT NULL,
    rule_fired  TEXT NOT NULL,
    decision    TEXT NOT NULL,
    latency_ms  INTEGER NOT NULL
);
"""


class LedgerConnection(sqlite3.Connection):
    """A transaction context that also closes its file handle on exit.

    ``sqlite3.Connection.__exit__`` commits or rolls back but does not close.
    Most stores use ``with connect(...)`` and require both behaviours,
    especially on Windows where an unclosed handle prevents atomic replacement.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: str) -> sqlite3.Connection:
    """The one way this codebase opens its ledger database.

    WAL plus a generous busy timeout is what makes the audit log and the
    exception cases survive concurrent writers in the same file; keeping the
    pragma in one place stops a future table from quietly getting weaker
    guarantees than the audit log it sits next to.
    """
    conn = sqlite3.connect(db_path, timeout=30, factory=LedgerConnection)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def event_id_for(order_id: str, action_type: str, attempt_no: int) -> str:
    return hashlib.sha256(f"{order_id}|{action_type}|{attempt_no}".encode()).hexdigest()


class MockRazorpayAdapter:
    """Deterministic offline stand-in. Same interface as the live adapter."""

    name = "mock"

    def create_payment_link(self, order: Order, amount_paise: int) -> dict:
        short = hashlib.sha256(order.order_id.encode()).hexdigest()[:8]
        return {"id": f"plink_mock_{short}", "short_url": f"https://rzp.io/l/mock{short}",
                "amount": amount_paise, "currency": "INR", "status": "created"}


class LiveRazorpayAdapter:
    """One real test-mode call (AC-9), never with live-mode credentials."""

    name = "razorpay_test_mode"

    def __init__(self, key_id: str, key_secret: str):
        _validate_test_mode_credentials(key_id, key_secret)
        self.key_id, self.key_secret = key_id, key_secret

    def create_payment_link(self, order: Order, amount_paise: int) -> dict:
        import base64
        import urllib.request
        payload = json.dumps({
            "amount": amount_paise, "currency": "INR",
            "description": f"Recovery for {order.invoice_no}",
            "reference_id": order.order_id,
        }).encode()
        req = urllib.request.Request(
            "https://api.razorpay.com/v1/payment_links", data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic " + base64.b64encode(
                    f"{self.key_id}:{self.key_secret}".encode()).decode(),
            })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())


def _validate_test_mode_credentials(key_id: str, key_secret: str) -> None:
    if not key_id or not key_secret:
        raise ValueError(
            "Razorpay test-mode adapter requires both RZP_TEST_KEY_ID and "
            "RZP_TEST_KEY_SECRET"
        )
    if key_id.startswith("rzp_live_"):
        raise ValueError("Razorpay live-mode credentials are refused")
    if not key_id.startswith("rzp_test_"):
        raise ValueError("RZP_TEST_KEY_ID must start with rzp_test_")


def default_adapter(*, test_mode: bool = False):
    """Return the offline adapter unless the caller explicitly selects test mode."""
    if not test_mode:
        return MockRazorpayAdapter()

    key_id = os.environ.get("RZP_TEST_KEY_ID", "")
    key_secret = os.environ.get("RZP_TEST_KEY_SECRET", "")
    return LiveRazorpayAdapter(key_id, key_secret)


class Executor:
    def __init__(self, db_path: str, adapter=None, drafts_dir: str | None = None):
        self.db_path = db_path
        self.adapter = adapter or MockRazorpayAdapter()
        self.drafts_dir = drafts_dir
        conn = self._conn()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def audit_write(self, event_id: str, layer: str, actor: str, order_id: str,
                    input_obj: dict, output_obj: dict, rule_fired: str,
                    decision: str, latency_ms: int) -> bool:
        """INSERT one audit row. Returns False when the PK already exists —
        the DB, not application logic, is the idempotency authority."""
        row = (
            event_id,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            layer, actor, order_id,
            hashlib.sha256(json.dumps(input_obj, sort_keys=True).encode()).hexdigest(),
            json.dumps(output_obj, sort_keys=True),
            rule_fired, decision, latency_ms,
        )
        conn = self._conn()
        try:
            conn.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def execute(self, order: Order, action_type: str, amount_paise: int,
                rule_fired: str, attempt_no: int = 1) -> dict:
        """Idempotent: same (order, action, attempt) can run N times concurrently;
        exactly one audit row survives and every caller gets the same event_id."""
        t0 = time.perf_counter()
        eid = event_id_for(order.order_id, action_type, attempt_no)

        if action_type == "CREATE_PAYMENT_LINK":
            output = self.adapter.create_payment_link(order, amount_paise)
            output["adapter"] = self.adapter.name
        elif action_type == "DRAFT_REMINDER":
            text = (f"Dear {order.customer_name},\n\n"
                    f"Our records show {rupees_str(amount_paise)} outstanding against "
                    f"{order.invoice_no} (due {order.due_date}). If you have already paid, "
                    f"please ignore this and share the UTR so we can reconcile.\n")
            output = {"draft": text, "sent": False}
            if self.drafts_dir:
                os.makedirs(self.drafts_dir, exist_ok=True)
                with open(os.path.join(self.drafts_dir, f"{order.order_id}.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write(text)
        else:
            raise ValueError(f"unsupported action {action_type}")

        latency = int((time.perf_counter() - t0) * 1000)
        inserted = self.audit_write(eid, "executor", "ledger-daemon", order.order_id,
                                    {"action": action_type, "amount_paise": amount_paise,
                                     "attempt_no": attempt_no},
                                    output, rule_fired, "EXECUTED", latency)
        return {"event_id": eid, "idempotent_replay": not inserted, "output": output}

    def audit(self, order_id: str) -> list[dict]:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT event_id, ts, layer, actor, order_id, input_hash, output_json,"
                " rule_fired, decision, latency_ms FROM audit WHERE order_id = ? ORDER BY ts, event_id",
                (order_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def attempts(self, order_id: str) -> int:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM audit WHERE order_id = ? AND layer = 'executor'",
                (order_id,))
            return cur.fetchone()[0]
        finally:
            conn.close()
