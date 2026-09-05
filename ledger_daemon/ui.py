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
import os
import sqlite3
import webbrowser
import mimetypes
from urllib.parse import urlsplit, parse_qs, unquote
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

@dataclass(frozen=True)
class BatchPresentation:
    """The only source and evaluation fields permitted in public UI payloads."""

    source_label: str
    evaluation: str
    evaluation_report: object | None


def batch_presentation(provenance: str, _unsafe_source: str, evaluation: str = "",
                       evaluation_report: object | None = None) -> BatchPresentation:
    """Map explicit safe provenance to public labels without reflecting paths.

    Imported datasets have no ground-truth oracle, including Test Mode captures.
    The source text is intentionally ignored because callers may construct it
    from a local path.
    """
    if provenance == "synthetic":
        return BatchPresentation("Synthetic batch", evaluation, evaluation_report)
    if provenance == "test_mode":
        return BatchPresentation("Test Mode batch", "", None)
    return BatchPresentation("Imported batch", "", None)


def public_batch_summary(orders, verdicts, decisions, certificates,
                         presentation: BatchPresentation) -> dict:
    """Build the API payload from the same sanitized presentation as the page."""
    from .landing import public_summary

    return public_summary(orders, verdicts, decisions, certificates,
                          presentation.evaluation_report, presentation.source_label)

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
                 resolution: str, actor: str = "human", *,
                 authority: object | None = None,
                 registry: object | None = None,
                 rule_suggestion: dict[str, object] | None = None,
                 evidence_registry: object | None = None) -> ReconciliationCase:
    """Walk the exception case to RESOLVED, one declared hop at a time.

    The click is one gesture but the case still records the whole route --
    who took it, that it was investigated, that the evidence was checked --
    because a resolution nobody can reconstruct is not an audit trail.
    A stale `expected_version` raises VersionConflict and changes nothing.
    """
    if resolution not in ("paid", "unpaid"):
        raise ValueError(f"resolution must be paid|unpaid, got {resolution!r}")
    from .rules import (
        AnalystRegistry, EvidenceRegistry, HumanAction, HumanAuthority,
        RuleFamily, _validate_parameters,
    )

    trusted = authority is not None or registry is not None
    if trusted:
        if not isinstance(registry, AnalystRegistry):
            raise ValueError("store-backed analyst registry is required")
        if registry.db_path != store.db_path:
            raise ValueError("analyst registry must share the case store")
        actor = registry.verify(
            authority, HumanAction.RESOLVE,
            subject_id=case.case_id, subject_version=expected_version,
        )
    elif rule_suggestion is not None:
        raise ValueError("store-issued human authority is required for rule learning")

    suggestion = dict(rule_suggestion) if rule_suggestion is not None else None
    evidence_assertion = None
    if suggestion is not None:
        if not isinstance(evidence_registry, EvidenceRegistry):
            raise ValueError("verified evidence registry is required for rule learning")
        if evidence_registry.db_path != store.db_path:
            raise ValueError("evidence registry must share the case store")
        if set(suggestion) != {"family", "parameters"}:
            raise ValueError("invalid rule suggestion schema")
        family = RuleFamily(suggestion["family"])
        parameters = _validate_parameters(family, suggestion["parameters"])
        evidence_assertion = evidence_registry.assertion(
            case.certificate_id, family, parameters)
    metadata = {
        "authority": authority.to_dict() if isinstance(authority, HumanAuthority) else None,
        "certificate_id": case.certificate_id,
        "evidence_assertion": evidence_assertion,
        "origin": "human" if trusted else "untrusted",
        "resolution": resolution,
        "rule_suggestion": suggestion,
        "verified": trusted,
    }
    return store.advance(
        case.case_id, expected_version,
        path_to(case.state, CaseState.RESOLVED), actor,
        evidence_refs=(f"human-resolution:{resolution}",),
        event_metadata=metadata,
        authority_use=(registry, authority, HumanAction.RESOLVE,
                       case.case_id, expected_version) if trusted else None)


def rule_proposal_state(rule: object) -> dict[str, object]:
    """Bounded rule lifecycle data for the analyst workbench."""
    from .rules import RuleProposal

    if not isinstance(rule, RuleProposal):
        raise TypeError("rule state requires a RuleProposal")
    return {
        "rule_id": rule.rule_id,
        "family": rule.family.value,
        "status": rule.status.value,
        "version": rule.version,
        "author": rule.author,
        "approver": rule.approver,
        "activated_by": rule.activated_by,
        "activation_time": rule.activation_time,
    }


def approve_rule_proposal(store: object, rule_id: str, expected_version: int,
                          authority: object) -> object:
    """Human-only approval helper; the store enforces replay and version gates."""
    from .rules import RuleStore

    if not isinstance(store, RuleStore):
        raise TypeError("approval requires a RuleStore")
    return store.approve(rule_id, expected_version, authority)


def activate_rule_proposal(store: object, rule_id: str, expected_version: int,
                           authority: object, activation_time: str) -> object:
    """Activate only an approved exact version; no UI bypass exists."""
    from .rules import RuleStore

    if not isinstance(store, RuleStore):
        raise TypeError("activation requires a RuleStore")
    return store.activate(rule_id, expected_version, authority, activation_time)


# --------------------------- rendering -------------------------------------- #

_VERDICT_LABEL = {
    "settled_clean": "settled",
    "settled_late": "settled late",
    "paid_out_of_band": "paid by bank transfer",
    "refunded_then_repaid": "refunded, then repaid",
    "paid_net_of_tds": "paid net of TDS",
    "possible_tds_withholding": "possible TDS — evidence required",
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


def _kv_table(rows: list[tuple[str, str]], cls: str = "kv") -> str:
    body = "".join(f'<tr><th>{escape(k)}</th><td>{escape(str(val))}</td></tr>'
                   for k, val in rows)
    return f'<table class="{cls}">{body}</table>'


def _signoff_html(panels) -> str:
    from .signoff import BLOCKER_CODES, CAVEAT_CODES

    decision = panels.signoff
    if decision is None:
        return '<p class="empty">no signoff was computed for this run</p>'
    tone = {"SIGN": "safe", "SIGN_WITH_CAVEATS": "hold",
            "DO_NOT_SIGN": "block"}[decision.status.value]
    items = "".join(
        f'<li class="blk"><code>{escape(code)}</code> {escape(BLOCKER_CODES[code])}</li>'
        for code in decision.blockers)
    items += "".join(
        f'<li class="cav"><code>{escape(code)}</code> {escape(CAVEAT_CODES[code])}</li>'
        for code in decision.caveats)
    if not items:
        items = ('<li class="cav">Nothing outstanding: every proof that was checked '
                 'verified, every feed arrived, no rupee was chased that had already '
                 'landed, and no action wrote twice.</li>')
    notes = "".join(f"<li>{escape(note)}</li>" for note in panels.notes)
    return (
        f'<div class="signoff {tone}"><div class="k">CONTROLLER SIGNOFF</div>'
        f'<div class="v">{escape(decision.status.value.replace("_", " "))}</div></div>'
        f'<ul class="codes">{items}</ul>'
        f'<div class="hashes"><span>metrics '
        f'{escape(decision.signed_metrics_hash[:16])}</span>'
        f'<span>proofs {escape(decision.proof_bundle_hash[:16] or "none")}</span></div>'
        + (f'<div class="notes"><h3>Read this with the numbers</h3>'
           f'<ul>{notes}</ul></div>' if notes else "")
        + '<p class="fine">An operational gate on whether this batch is fit to act '
          'on &mdash; not a statutory audit opinion.</p>')


def _close_overview_html(panels, view) -> str:
    """Distinct populations stay labelled; a policy hold is not a match error."""
    total = len(view.safe) + len(view.blocked) + len(view.needs_you) + view.hidden_clean
    exceptions = panels.exceptions
    return ('<h3>Reconciliation overview</h3>' + _kv_table([
        ('Orders in this batch', str(total)),
        ('Orders needing review', str(len(view.needs_you))),
        ('Order value awaiting review', rupees_str(view.total(view.needs_you))),
        ('Open exception cases', str(exceptions.open_cases) if exceptions else 'not available'),
        ('Proof certificates attached', str(panels.proofs.built) if panels.proofs else '0'),
        ('Proofs independently checked on this screen', str(panels.proof_sample)),
    ]) + '<p class="fine">Order counts, case counts, and source-row counts describe '
         'different populations. Ground-truth verdict accuracy is shown in Evaluation; '
         'it is not automatic resolution coverage.</p>'
         '<p><button data-goto="cases">Review exceptions</button> '
         '<button data-goto="proofs">Inspect evidence</button> '
         '<button data-goto="evaluation">See measured results</button></p>')


def _sources_html(panels) -> str:
    health = panels.source
    if health is None:
        return '<p class="empty">source health was not gathered for this run</p>'
    feed_rows = "".join(
        f'<tr><th>{escape(name)}</th><td>{offered}</td><td>{accepted}</td>'
        f'<td class="{"bad" if quarantined else ""}">{quarantined}</td></tr>'
        for name, offered, accepted, quarantined in panels.feeds)
    quarantine = "".join(
        f'<tr><td>{escape(str(rec.get("source", "")))}</td>'
        f'<td><code>{escape(str(rec.get("error_code", "")))}</code></td>'
        f'<td>{escape(str(rec.get("detail", ""))[:160])}</td></tr>'
        for rec in panels.quarantine) or (
        '<tr><td colspan="3" class="empty">nothing was rejected</td></tr>')
    return (
        f'{_kv_table([("rows offered", health.rows_offered), ("accepted", health.accepted), ("quarantined", health.quarantined), ("duplicate ids", health.duplicates), ("feeds present", f"{health.feeds_seen} of {health.feeds_expected}")])}'
        f'<h3>Per feed</h3><table class="grid"><thead><tr><th>feed</th><th>offered</th>'
        f'<th>accepted</th><th>quarantined</th></tr></thead><tbody>{feed_rows}</tbody></table>'
        f'<h3>Quarantined rows</h3><p class="fine">A row that fails validation is '
        f'never repaired and never guessed at &mdash; it fails closed, here, with the '
        f'reason it failed.</p>'
        f'<table class="grid"><thead><tr><th>feed</th><th>code</th><th>detail</th></tr>'
        f'</thead><tbody>{quarantine}</tbody></table>')


def _proofs_html(panels, view: View) -> str:
    proofs = panels.proofs
    if proofs is None or not proofs.built:
        return ('<p class="empty">no proof bundle is attached to this batch</p>'
                '<p class="fine">A live <code>--dir</code> run shows no proofs by '
                'design: a certificate issued against another batch would render '
                'against the wrong rows &mdash; the precise confusion this system '
                'exists to prevent.</p>')
    rows = [r for r in (view.safe + view.blocked + view.needs_you) if r.proof_tree]
    trees = "".join(
        f'<details class="proof card"><summary>{escape(r.order_id)} &middot; '
        f'{escape(r.customer)} &middot; {escape(r.proof_hash[:16])}&hellip;</summary>'
        f'<pre>{escape(r.proof_tree)}</pre></details>' for r in rows[:120])
    return (
        f'{_kv_table([("certificates issued", proofs.built), ("verified on load", f"{proofs.verified} of {panels.proof_sample} sampled"), ("rejected", proofs.rejected), ("orders decided without a proof", proofs.unverified)])}'
        f'<p class="fine">Verification here is a sample so the screen opens quickly. '
        f'<code>python -m ledger_daemon judge</code> and <code>verify-proof</code> '
        f'check every certificate, and neither imports the reconciliation code.</p>'
        f'<h3>Certificates</h3>{trees or "<p class=\'empty\'>no rendered proofs</p>"}')


def _cases_html(panels) -> str:
    exceptions = panels.exceptions
    head = _kv_table([
        ("open cases", exceptions.open_cases if exceptions else 0),
        ("money held for a human",
         rupees_str(exceptions.material_open_paise) if exceptions else "—"),
        ("oldest open case", f"{exceptions.oldest_open_days} days" if exceptions else "—"),
        ("stale (over 30 days)", exceptions.stale_cases if exceptions else 0),
    ])
    rows = "".join(
        f'<tr><td><code>{escape(c["case_id"][:12])}</code></td>'
        f'<td>{escape(c["order_id"])}</td><td>{escape(c["reason_code"])}</td>'
        f'<td>{escape(c["state"])}</td><td>v{int(c["version"])}</td>'
        f'<td>{escape(c["opened_at"][:19])}</td></tr>' for c in panels.cases[:200])
    return (f'{head}<h3>Exception cases</h3>'
            f'<p class="fine">Every held row is a persisted case with a state and a '
            f'version. A resolution must present the version it read, so a screen left '
            f'open cannot overwrite a colleague who worked the same case.</p>'
            f'<table class="grid"><thead><tr><th>case</th><th>order</th><th>reason</th>'
            f'<th>state</th><th>version</th><th>opened</th></tr></thead>'
            f'<tbody>{rows or "<tr><td colspan=6 class=empty>no open cases</td></tr>"}'
            f'</tbody></table>')


def _risk_html(panels) -> str:
    risk, authority = panels.risk, panels.authority
    mix = panels.automation
    total = max(1, sum(mix.get(k, 0) for k in ("exact", "probabilistic", "manual")))
    bars = "".join(
        f'<div class="bar"><span class="lbl">{escape(name)}</span>'
        f'<span class="track"><span class="fill {name}" style="width:'
        f'{mix.get(name, 0) * 100 // total}%"></span></span>'
        f'<span class="num">{mix.get(name, 0)}</span></div>'
        for name in ("exact", "probabilistic", "manual"))
    return (
        f'<h3>How each verdict was reached</h3>{bars}'
        f'<p class="fine">Exact proofs (UTR and settlement joins) never depended on a '
        f'fitted threshold, so drift cannot revoke them. Probabilistic verdicts can be '
        f'&mdash; and are, the moment the data stops looking like the calibration.</p>'
        f'<h3>Rupee-risk budget</h3>'
        f'{_kv_table([("authorized", "yes" if risk and risk.authorized else "no"), ("calibration id", (risk.calibration_id if risk and risk.calibration_id else "not fitted for this run")), ("loss upper bound", f"{risk.loss_upper_bound_bp} bp" if risk else "—"), ("budget", f"{risk.budget_bp} bp" if risk else "—")])}'
        f'<h3>Drift authority</h3>'
        f'{_kv_table([("state", authority.state if authority else "—"), ("calibration id", (authority.calibration_id if authority and authority.calibration_id else "—")), ("probabilistic matching", "halted" if authority and authority.probabilistic_halted else "in force")])}'
        f'<p class="fine">One severe window warns; two consecutive revoke '
        f'probabilistic authority. Recovery needs consecutive healthy windows '
        f'<em>and</em> a new calibration id &mdash; quiet alone leaves a threshold '
        f'that was fitted before the shift. Watch it move: '
        f'<code>python -m ledger_daemon drift-demo</code>.</p>')


def _audit_html(panels) -> str:
    rows = "".join(
        f'<tr><td>{escape(str(row.get("ts", "")))}</td>'
        f'<td>{escape(str(row.get("layer", "")))}</td>'
        f'<td>{escape(str(row.get("order_id", "")))}</td>'
        f'<td><code>{escape(str(row.get("rule_fired", "")))}</code></td>'
        f'<td>{escape(str(row.get("decision", "")))}</td></tr>'
        for row in panels.audit)
    return (f'<p class="fine">Append-only. No UPDATE and no DELETE exists anywhere in '
            f'this system; a human click lands here in the same table as a machine '
            f'decision, keyed by <code>sha256(order|action|attempt)</code>.</p>'
            f'<table class="grid"><thead><tr><th>when</th><th>layer</th><th>order</th>'
            f'<th>rule</th><th>decision</th></tr></thead><tbody>'
            f'{rows or "<tr><td colspan=5 class=empty>nothing recorded yet</td></tr>"}'
            f'</tbody></table>')


def _run_html(panels) -> str:
    stage_rows = "".join(
        f'<tr><th>{escape(s.name)}</th><td>{escape(s.detail)}</td>'
        f'<td>{s.millis} ms</td></tr>' for s in panels.stages)
    verdicts = "".join(
        f'<tr><th>{escape(_VERDICT_LABEL.get(name, name))}</th><td>{count}</td></tr>'
        for name, count in panels.verdict_counts.items())
    return (f'<h3>Stages</h3><table class="kv">{stage_rows or ""}</table>'
            f'<h3>Verdicts issued</h3><table class="kv">{verdicts}</table>'
            f'<p class="fine">Exactly one verdict per order, from a closed set. Adding '
            f'a way for money to arrive without deciding what collections does about it '
            f'is an <code>ImportError</code> on a developer machine, not a production '
            f'incident.</p>')


def _recovery_html(panels) -> str:
    r = panels.recovery
    return (
        '<h3>Downstream Control Demonstration: Why Correct Reconciliation Matters</h3>'
        '<p class="fine">Recovery is shown here only to make the cost of getting '
        'reconciliation wrong legible. It is not a second product and not a second '
        'track: the number that matters is the middle row.</p>'
        + _kv_table([
            ("released to chase", f'{r.get("chased", 0)} orders · '
                                  f'{rupees_str(int(r.get("chased_paise", 0)))}'),
            ("blocked — a schedule-driven chase would have contacted these",
             f'{r.get("blocked", 0)} customers · '
             f'{rupees_str(int(r.get("protected_paise", 0)))} protected'),
            ("held for a human", f'{r.get("held", 0)} orders · '
                                 f'{rupees_str(int(r.get("held_paise", 0)))}'),
        ]))


def _evaluation_html(panels) -> str:
    if not panels.evaluation:
        return ('<p class="empty">no evaluation was run against this batch</p>'
                '<p class="fine">Real data has no oracle, so a live <code>--dir</code> '
                'run is not scored. Accuracy numbers come only from generated worlds '
                'where the truth was written at injection time.</p>')
    return (f'<pre class="report">{escape(panels.evaluation)}</pre>'
            f'<p class="fine">Measured against labels written at injection time by '
            f'<code>datagen.py</code>. Synthetic, authored by this project, and '
            f'therefore evidence about the implementation rather than about how real '
            f'merchant feeds behave.</p>')


def render_html(view: View, source: str, panels=None) -> str:
    v = view
    css = """
@import url('/assets/fonts.css');
:root { --bg:#f4f1e8; --card:#fffef9; --line:#deded2; --tx:#33382d; --dim:#797f6e;
        --safe:#0e9f6e; --safebg:#e6f6f0; --block:#d64550; --blockbg:#fbeaec;
        --hold:#b47d10; --holdbg:#fdf3df; }
* { box-sizing:border-box; margin:0 }
body { background:var(--bg); color:var(--tx); padding:32px clamp(16px,4vw,48px);
       font:15px/1.55 'DM Sans',"Segoe UI",sans-serif }
header { display:flex; flex-wrap:wrap; align-items:baseline; gap:12px; margin-bottom:8px }
h1 { font:36px/1.1 'Instrument Serif',Georgia,serif; letter-spacing:-.01em }
button { font:inherit; cursor:pointer; border:1px solid var(--line); padding:8px 14px;
         background:var(--card); color:var(--tx); border-radius:5px }
button:hover { background:#e9eedf }
button:focus-visible { outline:2px solid #5b7049; outline-offset:3px }
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

nav.tabs { display:flex; flex-wrap:wrap; gap:4px; margin:18px 0 16px;
        border-bottom:1px solid var(--line) }
nav.tabs button { font:inherit; font-size:13.5px; font-weight:550; cursor:pointer;
        background:none; border:none; border-bottom:2px solid transparent;
        color:var(--dim); padding:9px 13px; border-radius:8px 8px 0 0 }
nav.tabs button:hover { background:var(--card); color:var(--tx) }
nav.tabs button[aria-selected="true"] { color:var(--tx); border-bottom-color:var(--tx);
        background:var(--card) }
nav.tabs button:focus-visible { outline:2px solid #c6d4f7; outline-offset:-2px }
.panel[hidden] { display:none }
.panel > h3 { font-size:12.5px; font-weight:650; letter-spacing:.08em; color:var(--dim);
        text-transform:uppercase; margin:22px 0 10px }
.panel > h3:first-child { margin-top:4px }
.card, table.kv, table.grid { background:var(--card); border:1px solid var(--line);
        border-radius:12px; overflow:hidden; width:100% }
table.kv, table.grid { border-collapse:separate; border-spacing:0; font-size:13.5px }
table.kv th, table.grid th { text-align:left; font-weight:550; color:var(--dim);
        padding:9px 14px; white-space:nowrap }
table.kv td, table.grid td { padding:9px 14px; font-variant-numeric:tabular-nums }
table.kv tr + tr th, table.kv tr + tr td,
table.grid tbody tr th, table.grid tbody tr td { border-top:1px solid var(--line) }
table.grid thead th { background:var(--bg); font-size:11.5px; letter-spacing:.06em;
        text-transform:uppercase }
table.grid td.bad { color:var(--block); font-weight:600 }
.signoff { border-radius:12px; border:1px solid var(--line); padding:18px 20px;
        background:var(--card) }
.signoff .k { font-size:12px; font-weight:600; letter-spacing:.09em; color:var(--dim) }
.signoff .v { font-size:28px; font-weight:680; margin-top:6px; letter-spacing:-.01em }
.signoff.safe { border-color:var(--safe); background:var(--safebg) }
.signoff.safe .v { color:var(--safe) }
.signoff.hold { border-color:var(--hold); background:var(--holdbg) }
.signoff.hold .v { color:var(--hold) }
.signoff.block { border-color:var(--block); background:var(--blockbg) }
.signoff.block .v { color:var(--block) }
ul.codes { list-style:none; margin:14px 0 0; display:grid; gap:8px }
ul.codes li { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:11px 14px; font-size:13.5px; color:var(--dim) }
ul.codes li.blk { border-left:3px solid var(--block) }
ul.codes li.cav { border-left:3px solid var(--hold) }
ul.codes code, .fine code, table code { font-family:ui-monospace,Consolas,monospace;
        font-size:12px; color:var(--tx); background:var(--bg); border-radius:6px;
        padding:1px 6px }
.hashes { display:flex; gap:18px; margin-top:12px; color:var(--dim);
        font-family:ui-monospace,Consolas,monospace; font-size:11.5px }
.notes { margin-top:18px }
.notes h3 { font-size:12.5px; letter-spacing:.08em; text-transform:uppercase;
        color:var(--dim); margin-bottom:8px }
.notes ul { margin:0; padding-left:18px; color:var(--dim); font-size:13.5px;
        display:grid; gap:6px }
.fine { color:var(--dim); font-size:13px; margin-top:10px; max-width:76ch }
.bar { display:flex; align-items:center; gap:12px; margin:7px 0; font-size:13.5px }
.bar .lbl { width:110px; color:var(--dim) }
.bar .track { flex:1; height:9px; background:var(--card); border:1px solid var(--line);
        border-radius:99px; overflow:hidden }
.bar .fill { display:block; height:100%; background:var(--dim) }
.bar .fill.exact { background:var(--safe) }
.bar .fill.probabilistic { background:var(--hold) }
.bar .num { width:52px; text-align:right; font-variant-numeric:tabular-nums }
pre.report { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:16px 18px; overflow-x:auto; font-size:12px; line-height:1.5 }
details.card { margin-bottom:8px }
details.card > summary { cursor:pointer; padding:11px 14px; font-size:13px;
        font-family:ui-monospace,Consolas,monospace; color:var(--dim) }
details.card pre { margin:0; padding:12px 14px; border-top:1px solid var(--line);
        background:var(--bg); overflow-x:auto; font-size:11px; line-height:1.5 }
.tablewrap { overflow-x:auto }
"""
    js = r"""
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
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => { p.hidden = p.dataset.panel !== name; });
  document.querySelectorAll('nav.tabs button').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  try { localStorage.setItem('ld.panel', name); } catch (e) { /* private window */ }
}
document.addEventListener('click', e => {
  const tab = e.target.closest('nav.tabs button');
  if (tab) { showPanel(tab.dataset.tab); return; }
  const jump = e.target.closest('[data-goto]');
  if (jump) { showPanel(jump.dataset.goto); window.scrollTo({top: 0}); }
});
(function () {
  let start = 'close';
  try { start = location.hash.slice(1) || localStorage.getItem('ld.panel') || 'close'; } catch (e) { /* ignore */ }
  if (!document.querySelector(`.panel[data-panel="${start}"]`)) start = 'close';
  showPanel(start);
})();
"""
    from .panels import Panels

    p = panels if panels is not None else Panels()
    chase = f"""
<div class="tiles">
<div class="tile safe" data-goto="chase"><div class="k">SAFE TO CHASE</div>
<div class="v">{escape(rupees_str(v.total(v.safe)))}</div>
<div class="n">{len(v.safe)} orders &mdash; proven unpaid, gates passed</div></div>
<div class="tile block" data-goto="chase"><div class="k">BLOCKED</div>
<div class="v">{escape(rupees_str(v.total(v.blocked)))}</div>
<div class="n">{len(v.blocked)} customers a schedule-driven chase would have contacted &mdash; wrongly</div></div>
<div class="tile hold" data-goto="cases"><div class="k">NEEDS YOU</div>
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
<p class="fine">{v.hidden_clean} settled orders ({escape(rupees_str(v.hidden_clean_paise))})
reconciled clean and are not listed: books and bank already agree, so BLOCKED keeps
meaning saves rather than routine agreement.</p>"""

    tabs = [("close", "Close"), ("chase", "Chase list"), ("sources", "Sources"),
            ("proofs", "Proofs"), ("cases", "Exceptions"), ("risk", "Risk &amp; drift"),
            ("run", "Run"), ("evaluation", "Evaluation"), ("audit", "Audit"),
            ("recovery", "Recovery")]
    # The first panel is rendered visible by the server, not by script: with
    # JavaScript unavailable the close view still reads, and there is no blank
    # first paint while the tab handler installs itself.
    nav = "".join(
        f'<button role="tab" data-tab="{key}" '
        f'aria-selected="{"true" if key == "close" else "false"}">{label}</button>'
        for key, label in tabs)
    bodies = {
        "close": _signoff_html(p) + _close_overview_html(p, v),
        "chase": chase,
        "sources": _sources_html(p),
        "proofs": _proofs_html(p, v),
        "cases": _cases_html(p),
        "risk": _risk_html(p),
        "run": _run_html(p),
        "evaluation": _evaluation_html(p),
        "audit": _audit_html(p),
        "recovery": _recovery_html(p),
    }
    sections = "".join(
        f'<section class="panel" data-panel="{key}"'
        f'{"" if key == "close" else " hidden"}>{bodies[key]}</section>'
        for key, _label in tabs)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ledger Daemon — Close</title>
<style>{css}</style></head><body>
<header><h1><a href="/" style="color:inherit;text-decoration:none">Ledger Daemon</a></h1><span class="src">{escape(source)}</span>
<span class="tag">every decision below is auditable to the rupee</span></header>
<nav class="tabs" role="tablist">{nav}</nav>
{sections}
<footer>Deterministic three-way reconciliation as a hard precondition on chasing
money. Clicks land in the same append-only audit trail as machine decisions;
nothing on this screen recomputes a verdict or a rupee.</footer>
<script>{js}</script></body></html>"""


# --------------------------- server ----------------------------------------- #

def build_panels(orders, verdicts, decisions, execu, store, state, *,
                 captures=(), bank=(), evaluation: str = "", config_hash: str = "",
                 calibration_id: str = "", risk_calibration=None, stages=(), finance_events=()):
    """Gather the dashboard once. Cheap enough to redo per request except the
    proof sample, which is why the caller holds on to the result."""
    from dataclasses import asdict as _asdict

    from . import panels as panels_mod
    from .certificates import source_rows

    certificates = state["certificates"]
    cases = list(state["cases"].values())
    held = {o.order_id: (verdicts[o.order_id].delta_due_paise or o.amount_paise)
            for o in orders if decisions[o.order_id].outcome == policy.HOLD}
    audit: list[dict] = []
    for order_id in sorted({c.order_id for c in cases} | set(held))[:60]:
        audit.extend(execu.audit(order_id))
    audit.sort(key=lambda row: str(row.get("ts", "")), reverse=True)

    view = build_view(orders, verdicts, decisions, state["resolutions"],
                      state["cases"], certificates)
    return panels_mod.collect(
        view=view, verdicts=verdicts,
        order_rows=[_asdict(o) for o in orders],
        capture_rows=[_asdict(c) for c in captures],
        bank_rows=[_asdict(b) for b in bank],
        certificates=certificates,
        source_rows_list=source_rows(list(orders), list(captures), list(bank), finance_events=finance_events),
        cases=cases, audit=audit,
        order_ids=[o.order_id for o in orders], held_amounts=held,
        config_hash=config_hash, calibration_id=calibration_id,
        risk_calibration=risk_calibration, evaluation=evaluation,
        stages=list(stages))


def serve(orders: list[Order], verdicts: dict, decisions: dict,
          execu: Executor, source: str, port: int = PORT,
          open_browser: bool = True, proofs_dir: str = "",
          captures=(), bank=(), evaluation: str = "", config_hash: str = "",
          calibration_id: str = "", risk_calibration=None, stages=(), evaluation_report=None,
          finance_events=(), provenance: str = "imported") -> None:
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
    proof_manifest = None
    if proofs_dir:
        try:
            with open(os.path.join(proofs_dir, "proof-manifest.json"), encoding="utf-8") as fh:
                loaded_manifest = json.load(fh)
            if isinstance(loaded_manifest, dict):
                proof_manifest = loaded_manifest
        except (OSError, json.JSONDecodeError):
            pass
    presentation = batch_presentation(provenance, source, evaluation, evaluation_report)
    from .landing import static_asset, check_proof, batch_zip
    from .certificates import source_rows
    from .model_benchmark import FIXTURES, benchmark_readers
    from .evidence_reader import RegexReader
    proof_sources = source_rows(list(orders), list(captures), list(bank), finance_events=finance_events)
    reader_score = benchmark_readers(FIXTURES, [RegexReader()]).scores[0].to_dict()

    def summary():
        return public_batch_summary(orders, verdicts, decisions,
                                    state['certificates'], presentation)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal clean
            pass

        def _send(self, code: int, body: bytes,
                  ctype: str = "text/html; charset=utf-8", filename: str = "") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            path = unquote(url.path)
            if path in ('/', '/index.html'):
                return self._send(200, static_asset('landing.html').read_bytes())
            if path.startswith('/assets/'):
                asset = static_asset(path[len('/assets/'):])
                if asset is None:
                    return self._send(404, b'asset not found', 'text/plain')
                return self._send(200, asset.read_bytes(), mimetypes.guess_type(asset.name)[0] or 'application/octet-stream')
            if path == '/api/summary':
                return self._send(200, json.dumps(summary()).encode(), 'application/json')
            if path == '/api/readers':
                return self._send(200, json.dumps(reader_score).encode(), 'application/json')
            if path == '/download/report.json':
                return self._send(200, json.dumps(summary(), indent=2).encode(),
                                  'application/json', 'ledger-daemon-batch-report.json')
            if path == '/download/batch.zip':
                return self._send(200, batch_zip(
                    orders, captures, bank, finance_events,
                    proof_manifest=proof_manifest,
                ),
                                  'application/zip', 'ledger-daemon-sample.zip')
            if path.startswith('/api/proof/') or path.startswith('/download/proof/'):
                download = path.startswith('/download/')
                oid = path.rsplit('/', 1)[-1]
                if download and oid.endswith('.json'):
                    oid = oid[:-5]
                certificate = state['certificates'].get(oid)
                if certificate is None:
                    return self._send(404, b'proof not found', 'text/plain')
                if download:
                    return self._send(200, certificate.to_json().encode(), 'application/json', 'ledger-proof.json')
                checked = check_proof(certificate, proof_sources,
                    tamper=parse_qs(url.query).get('tamper') == ['1'],
                    config_hash=config_hash, calibration_id=calibration_id)
                return self._send(200, json.dumps(checked).encode(), 'application/json')
            if path != '/app':
                return self._send(404, b"not found", "text/plain")
            view = build_view(orders, verdicts, decisions, state["resolutions"],
                              state["cases"], state["certificates"])
            panels = build_panels(
                orders, verdicts, decisions, execu, store, state,
                captures=captures, bank=bank, evaluation=presentation.evaluation,
                config_hash=config_hash, calibration_id=calibration_id,
                risk_calibration=risk_calibration, stages=stages, finance_events=finance_events)
            self._send(200, render_html(view, presentation.source_label, panels).encode())

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
    print(f"landing page at {url} | controller at {url}/app  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
