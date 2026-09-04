"""The chase list: one screen, three columns, zero dependencies (FR-9).

`python -m ledger_daemon ui` runs the pipeline and serves a single local page
on http://127.0.0.1:7042 — Python stdlib `http.server`, inline CSS/JS, no CDN,
fully offline:

  SAFE TO CHASE   policy said ALLOW; these go to the executor
  BLOCKED         policy said DENY/ESCALATE; each row shows the rupees protected
                  and the exact rule that fired
  NEEDS YOU       policy said HOLD (abstentions, coverage gaps); each row is
                  resolvable in one click, and the click lands in the same
                  append-only sqlite audit trail as every machine decision

A resolution is evidence recorded, not a state silently mutated: "payment
received" blocks the chase, "nothing arrived" releases the order to the chase
list. Refresh re-runs nothing — the page re-renders from decisions already made.

Every NEEDS YOU row is backed by a persisted exception case (cases.py). The
row renders that case's id, state and version, and the resolution posts the
version back; a screen left open while a colleague worked the same case loses
with a 409 instead of overwriting their answer.
"""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import policy
from .cases import (
    CaseState,
    CaseStore,
    ReconciliationCase,
    VersionConflict,
    open_exception_cases,
    path_to,
)
from .executor import Executor
from .models import Order
from .money import rupees_str
from .proof_tree import certificate_to_tree, load_certificates, render_text

PORT = 7042
UI_LAYER = "ui"


# --------------------------- view model (pure) ------------------------------ #

@dataclass
class ViewRow:
    order_id: str
    customer: str
    amount_paise: int
    verdict: str
    rule: str
    detail: str
    p_match: str = ""
    case_id: str = ""        # the exception case this row belongs to, if any
    case_state: str = ""
    case_version: int = 0    # the version a resolution must present to be applied
    proof_hash: str = ""     # the same hash `explain` and `verify-proof` print
    proof_tree: str = ""     # the issued certificate, rendered


@dataclass
class View:
    safe: list[ViewRow] = field(default_factory=list)
    blocked: list[ViewRow] = field(default_factory=list)
    needs_you: list[ViewRow] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)  # order_id -> resolution
    hidden_clean: int = 0            # settled orders whose books already agree
    hidden_clean_paise: int = 0      # nothing to protect; hidden to keep the screen honest

    def total(self, rows: list[ViewRow]) -> int:
        return sum(r.amount_paise for r in rows)


def build_view(orders: list[Order], verdicts: dict, decisions: dict,
               resolutions: dict[str, str],
               cases: dict[str, ReconciliationCase] | None = None,
               certificates: dict | None = None) -> View:
    """Split every order into exactly one column. Resolutions override HOLDs:
    'paid' moves the row to BLOCKED, 'unpaid' moves it to SAFE.

    `cases` carries the exception case behind each held row; its version is
    what a resolution must present, so a screen left open while someone else
    worked the case cannot silently overwrite their answer. `certificates`
    carries the issued proof, rendered by the same code path as `explain`.
    """
    open_cases = cases or {}
    proofs = certificates or {}
    view = View(resolved=dict(resolutions))
    for o in orders:
        v = verdicts[o.order_id]
        d = decisions[o.order_id]
        amount = v.delta_due_paise or o.amount_paise
        case = open_cases.get(o.order_id)
        row = ViewRow(o.order_id, o.customer_name, amount,
                      v.verdict.value, d.rule_fired, d.detail or v.reason,
                      p_match=v.p_match,
                      case_id=case.case_id if case else "",
                      case_state=case.state.value if case else "",
                      case_version=case.version if case else 0)
        certificate = proofs.get(o.order_id)
        if certificate is not None:
            row.proof_hash = certificate.proof_hash
            row.proof_tree = render_text(certificate_to_tree(certificate))
        res = resolutions.get(o.order_id)
        if d.outcome == policy.HOLD and res == "paid":
            row.rule = "HUMAN_RESOLVED_PAID"
            row.detail = "a human confirmed the payment landed"
            view.blocked.append(row)
        elif d.outcome == policy.HOLD and res == "unpaid":
            row.rule = "HUMAN_RESOLVED_UNPAID"
            row.detail = "a human confirmed nothing arrived — released to chase"
            view.safe.append(row)
        elif d.outcome == policy.ALLOW:
            view.safe.append(row)
        elif d.outcome == policy.HOLD:
            view.needs_you.append(row)
        elif d.rule_fired == "R1_DENY_ALREADY_PAID" and o.status == "paid":
            # books and bank agree: settled, nobody would chase it. Hiding these
            # keeps BLOCKED meaning what it claims: saves, not routine agreement.
            view.hidden_clean += 1
            view.hidden_clean_paise += row.amount_paise
        else:  # DENY / ESCALATE where a naive duner WOULD have chased
            view.blocked.append(row)
    for rows in (view.safe, view.blocked, view.needs_you):
        rows.sort(key=lambda r: -r.amount_paise)
    return view


# --------------------------- resolutions store ------------------------------ #

def load_resolutions(db_path: str) -> dict[str, str]:
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT order_id, decision FROM audit WHERE layer = ? ORDER BY ts",
                (UI_LAYER,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {oid: decision for oid, decision in rows}  # last write wins


def save_resolution(execu: Executor, order_id: str, resolution: str) -> bool:
    if resolution not in ("paid", "unpaid"):
        raise ValueError(f"resolution must be paid|unpaid, got {resolution!r}")
    return execu.audit_write(
        f"uires_{order_id}_{resolution}", UI_LAYER, "human", order_id,
        {"order_id": order_id}, {"resolution": resolution},
        "HUMAN_RESOLVED_" + resolution.upper(), resolution, 0)


def resolve_case(store: CaseStore, case: ReconciliationCase, expected_version: int,
                 resolution: str, actor: str = "human") -> ReconciliationCase:
    """Walk the exception case to RESOLVED, one declared hop at a time.

    The click is one gesture but the case still records the whole route --
    who took it, that it was investigated, that the evidence was checked --
    because a resolution nobody can reconstruct is not an audit trail.
    A stale `expected_version` raises VersionConflict and changes nothing.
    """
    if resolution not in ("paid", "unpaid"):
        raise ValueError(f"resolution must be paid|unpaid, got {resolution!r}")
    return store.advance(
        case.case_id, expected_version,
        path_to(case.state, CaseState.RESOLVED), actor,
        evidence_refs=(f"human-resolution:{resolution}",))


# --------------------------- rendering -------------------------------------- #

_VERDICT_LABEL = {
    "settled_clean": "settled",
    "settled_late": "settled late",
    "paid_out_of_band": "paid by bank transfer",
    "refunded_then_repaid": "refunded, then repaid",
    "paid_net_of_tds": "paid net of TDS",
    "partially_paid": "partially paid",
    "genuinely_unpaid": "genuinely unpaid",
    "failed_not_debited": "failed, never debited",
    "chargeback_open": "chargeback open",
    "ambiguous": "needs a human eye",
}


def _rows_html(rows: list[ViewRow], resolvable: bool) -> str:
    out = []
    for r in rows:
        buttons = ""
        if resolvable and r.case_id:
            oid = escape(r.order_id)
            ver = int(r.case_version)
            buttons = (
                '<div class="acts">'
                f'<button class="ok" onclick="resolve(this, \'{oid}\', \'paid\', {ver})">'
                '&#10003;&nbsp;Payment received &mdash; do not chase</button>'
                f'<button class="warn" onclick="resolve(this, \'{oid}\', \'unpaid\', {ver})">'
                '&#10007;&nbsp;Nothing arrived &mdash; safe to chase</button>'
                '</div>')
        pm = (f'<span class="pm">P(match) {escape(r.p_match)}</span>'
              if r.p_match else "")
        case = (f'<span class="case">case {escape(r.case_id[:12])} &middot; '
                f'{escape(r.case_state)} &middot; v{int(r.case_version)}</span>'
                if r.case_id else "")
        proof = (f'<details class="proof"><summary>proof '
                 f'{escape(r.proof_hash[:16])}&hellip;</summary>'
                 f'<pre>{escape(r.proof_tree)}</pre></details>'
                 if r.proof_tree else "")
        label = _VERDICT_LABEL.get(r.verdict, r.verdict)
        out.append(
            f'<details class="row" data-search="{escape((r.order_id + " " + r.customer).lower())}">'
            f'<summary>'
            f'<span class="cust">{escape(r.customer) or "&mdash;"}'
            f'<span class="oid">{escape(r.order_id)}</span></span>'
            f'<span class="badge">{escape(label)}</span>'
            f'<span class="amt">{escape(rupees_str(r.amount_paise))}</span>'
            f'</summary>'
            f'<div class="detail"><span class="rule">{escape(r.rule)}</span> '
            f'{escape(r.detail)} {pm}{case}{proof}{buttons}</div>'
            f'</details>')
    return "\n".join(out) or '<p class="empty">nothing here &mdash; all clear</p>'


def render_html(view: View, source: str) -> str:
    v = view
    css = """
:root { --bg:#f5f6f8; --card:#ffffff; --line:#e6e8ee; --tx:#1c2230; --dim:#68718a;
        --safe:#0e9f6e; --safebg:#e6f6f0; --block:#d64550; --blockbg:#fbeaec;
        --hold:#b47d10; --holdbg:#fdf3df; }
* { box-sizing:border-box; margin:0 }
body { background:var(--bg); color:var(--tx); padding:32px clamp(16px,4vw,48px);
       font:15px/1.55 -apple-system,"Segoe UI",Roboto,"Noto Sans",sans-serif }
header { display:flex; flex-wrap:wrap; align-items:baseline; gap:12px; margin-bottom:8px }
h1 { font-size:22px; font-weight:650; letter-spacing:-.01em }
.src { color:var(--dim); font-size:14px }
.tag { margin-left:auto; color:var(--dim); font-size:13px }
.tiles { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:18px 0 10px }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:16px 18px; box-shadow:0 1px 2px rgba(16,24,40,.04) }
.tile .k { font-size:12px; font-weight:600; letter-spacing:.09em; color:var(--dim) }
.tile .v { font-size:24px; font-weight:650; margin-top:4px;
           font-variant-numeric:tabular-nums }
.tile .n { color:var(--dim); font-size:13px }
.tile.safe .v { color:var(--safe) } .tile.block .v { color:var(--block) }
.tile.hold .v { color:var(--hold) }
.search { width:100%; margin:8px 0 18px; padding:11px 14px; font:inherit;
          border:1px solid var(--line); border-radius:10px; background:var(--card) }
.search:focus { outline:2px solid #c6d4f7 }
.cols { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; align-items:start }
.col { background:var(--card); border:1px solid var(--line); border-radius:12px;
       overflow:hidden; box-shadow:0 1px 2px rgba(16,24,40,.04) }
.col > h2 { font-size:12.5px; font-weight:650; letter-spacing:.09em; padding:13px 16px;
       display:flex; justify-content:space-between; border-bottom:1px solid var(--line) }
.col.safe > h2 { color:var(--safe); background:var(--safebg) }
.col.block > h2 { color:var(--block); background:var(--blockbg) }
.col.hold > h2 { color:var(--hold); background:var(--holdbg) }
.col > h2 .sum { font-weight:500; font-variant-numeric:tabular-nums }
.row { border-bottom:1px solid var(--line) }
.row:last-child { border-bottom:none }
.row summary { display:flex; align-items:center; gap:10px; padding:11px 16px;
               cursor:pointer; list-style:none }
.row summary::-webkit-details-marker { display:none }
.row summary:hover { background:#fafbfd }
.cust { flex:1; min-width:0; font-weight:550; overflow:hidden;
        text-overflow:ellipsis; white-space:nowrap }
.oid { display:block; font-size:12px; font-weight:400; color:var(--dim) }
.badge { font-size:11.5px; color:var(--dim); background:var(--bg);
         border:1px solid var(--line); border-radius:99px; padding:2px 9px;
         white-space:nowrap }
.amt { font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap }
.detail { padding:2px 16px 14px; color:var(--dim); font-size:13.5px }
.rule { font-family:ui-monospace,Consolas,monospace; font-size:12px;
        color:var(--tx); background:var(--bg); border-radius:6px; padding:1px 6px }
.pm { color:var(--hold) }
.case { display:block; margin-top:6px; font-family:ui-monospace,Consolas,monospace;
        font-size:11.5px; color:var(--dim) }
.proof { margin-top:8px }
.proof > summary { cursor:pointer; font-family:ui-monospace,Consolas,monospace;
        font-size:11.5px; color:var(--dim) }
.proof pre { margin-top:6px; padding:10px 12px; background:var(--bg);
        border:1px solid var(--line); border-radius:8px; overflow-x:auto;
        font-size:11px; line-height:1.5; color:var(--tx) }
.acts { margin-top:12px; display:flex; flex-wrap:wrap; gap:10px }
.acts button { font:inherit; font-size:13.5px; font-weight:550; cursor:pointer;
        border-radius:9px; padding:9px 14px; border:1px solid transparent }
.acts .ok { background:var(--safe); color:#fff }
.acts .ok:hover { filter:brightness(1.06) }
.acts .warn { background:#fff; color:var(--block); border-color:var(--block) }
.acts .warn:hover { background:var(--blockbg) }
.acts button:disabled { opacity:.5; cursor:wait }
.empty { padding:20px 16px; color:var(--dim) }
footer { margin-top:22px; color:var(--dim); font-size:13px }
@media (max-width:1020px) { .cols,.tiles { grid-template-columns:1fr } }
"""
    js = """
async function resolve(btn, oid, res, version) {
  btn.disabled = true;
  const r = await fetch('/resolve', {method:'POST',
    headers:{'content-type':'application/json'},
    body: JSON.stringify({order_id: oid, resolution: res, expected_version: version})});
  if (r.ok) location.reload();
  else if (r.status === 409) {
    // someone else moved this case while this screen sat open
    alert('this case moved since the page loaded — reloading\n\n' + await r.text());
    location.reload();
  }
  else { btn.disabled = false; alert('resolution failed: ' + await r.text()); }
}
document.addEventListener('input', e => {
  if (e.target.id !== 'q') return;
  const q = e.target.value.toLowerCase().trim();
  document.querySelectorAll('.row').forEach(el =>
    el.style.display = el.dataset.search.includes(q) ? '' : 'none');
});
"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chase List — Ledger Daemon</title>
<style>{css}</style></head><body>
<header><h1>Today&rsquo;s chase list</h1><span class="src">{escape(source)}</span>
<span class="tag">every decision below is auditable to the rupee</span></header>
<div class="tiles">
<div class="tile safe"><div class="k">SAFE TO CHASE</div>
<div class="v">{escape(rupees_str(v.total(v.safe)))}</div>
<div class="n">{len(v.safe)} orders &mdash; proven unpaid, gates passed</div></div>
<div class="tile block"><div class="k">BLOCKED</div>
<div class="v">{escape(rupees_str(v.total(v.blocked)))}</div>
<div class="n">{len(v.blocked)} customers a naive duner would have chased &mdash; wrongly</div></div>
<div class="tile hold"><div class="k">NEEDS YOU</div>
<div class="v">{len(v.needs_you)}</div>
<div class="n">honest abstentions &mdash; one click each resolves them</div></div>
</div>
<input id="q" class="search" type="search"
 placeholder="search customer or order id&hellip;" autocomplete="off">
<div class="cols">
<section class="col safe"><h2>SAFE TO CHASE &middot; {len(v.safe)}
<span class="sum">{escape(rupees_str(v.total(v.safe)))}</span></h2>{_rows_html(v.safe, False)}</section>
<section class="col block"><h2>BLOCKED &middot; {len(v.blocked)}
<span class="sum">{escape(rupees_str(v.total(v.blocked)))} protected</span></h2>{_rows_html(v.blocked, False)}</section>
<section class="col hold"><h2>NEEDS YOU &middot; {len(v.needs_you)}
<span class="sum">{escape(rupees_str(v.total(v.needs_you)))}</span></h2>{_rows_html(v.needs_you, True)}</section>
</div>
<footer>{v.hidden_clean} settled orders ({escape(rupees_str(v.hidden_clean_paise))})
reconciled clean &mdash; books and bank agree, so they are not shown.
Ledger Daemon: deterministic three-way reconciliation as a hard precondition on
chasing money; clicks land in the same append-only audit trail as machine decisions.</footer>
<script>{js}</script></body></html>"""


# --------------------------- server ----------------------------------------- #

def serve(orders: list[Order], verdicts: dict, decisions: dict,
          execu: Executor, source: str, port: int = PORT,
          open_browser: bool = True, proofs_dir: str = "") -> None:
    # Cases live in the same WAL database as the audit log, and are opened
    # idempotently: restarting the server picks up whatever states the
    # previous session's analysts left behind.
    store = CaseStore(execu.db_path)
    open_exception_cases(store, verdicts, decisions,
                         certificate_ids={oid: v.certificate_id
                                          for oid, v in verdicts.items()})
    # Read the issued bundle rather than rebuilding it, so the screen shows the
    # same proof hash as `explain` and `verify-proof`.
    state = {
        "resolutions": load_resolutions(execu.db_path),
        "cases": {c.order_id: c for c in store.list_cases()},
        "certificates": load_certificates(proofs_dir) if proofs_dir else {},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal clean
            pass

        def _send(self, code: int, body: bytes,
                  ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                return self._send(404, b"not found", "text/plain")
            view = build_view(orders, verdicts, decisions, state["resolutions"],
                              state["cases"], state["certificates"])
            self._send(200, render_html(view, source).encode())

        def do_POST(self):
            if self.path != "/resolve":
                return self._send(404, b"not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                oid, res = body["order_id"], body["resolution"]
                if oid not in decisions:
                    return self._send(404, b"unknown order", "text/plain")
                case = state["cases"].get(oid)
                if case is None:
                    return self._send(409, b"no open case for this order", "text/plain")
                # The screen must present the version it was rendered from: a
                # stale tab loses to whoever worked the case in the meantime.
                expected = int(body["expected_version"])
                moved = resolve_case(store, case, expected, res)
                save_resolution(execu, oid, res)
                state["resolutions"][oid] = res
                state["cases"][oid] = moved
                self._send(200, b"ok", "text/plain")
            except VersionConflict as exc:
                fresh = store.get(exc.case_id)
                state["cases"][fresh.order_id] = fresh  # so a reload shows the truth
                self._send(409, str(exc).encode(), "text/plain")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._send(400, str(exc).encode(), "text/plain")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"chase list at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
