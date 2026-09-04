"""MCP surface (FR-9): six tools over stdio.

    reconcile(batch_path)  -> {match_rate?, verdict_counts, exception_ids}
    explain(order_id)      -> evidence chain: pass used, source rows, waterfall, why
    propose_recovery()     -> proposals for chaseable verdicts only
    approve(proposal_id)   -> executes; idempotent; returns event_id
    audit(order_id)        -> full append-only trail
    report()               -> the evaluation block

propose_recovery() and approve() are separate calls — no single tool call may
move money (FR-9.1).

Uses the official `mcp` python SDK (FastMCP) when installed; otherwise falls
back to a minimal JSON-RPC stdio loop implementing the same six tools, so the
demo has zero required dependencies.
"""

from __future__ import annotations

import json
import os
import sys

from . import policy
from .cli import render_explain
from .datagen import load_batch
from .evaluate import evaluate, render_report, run_ledger_daemon
from .executor import Executor, default_adapter
from .recon import reconcile as recon_run


class Service:
    """Stateful core shared by both transports."""

    def __init__(self, root: str):
        self.root = root
        self.batch_dir = os.path.join(root, "data", "batch")
        self.executor = Executor(os.path.join(root, "ledger.sqlite3"),
                                 adapter=default_adapter(),
                                 drafts_dir=os.path.join(root, "drafts"))
        self._loaded = None
        self._result = None
        self._proposals: dict[str, dict] = {}

    def _ensure(self, batch_path: str | None = None):
        path = batch_path or self.batch_dir
        if self._loaded != path:
            self.orders, self.captures, self.bank, self.truth = load_batch(path)
            self.orders_by_id = {o.order_id: o for o in self.orders}
            self._result = recon_run(self.orders, self.captures, self.bank)
            self._loaded = path
        return self._result

    def reconcile(self, batch_path: str = "") -> dict:
        res = self._ensure(batch_path or None)
        counts: dict[str, int] = {}
        for v in res.verdicts.values():
            counts[v.verdict.value] = counts.get(v.verdict.value, 0) + 1
        return {"orders": len(res.verdicts), "verdict_counts": dict(sorted(counts.items())),
                "exception_ids": res.exception_ids, "orders_per_sec": res.orders_per_sec}

    def explain(self, order_id: str) -> str:
        res = self._ensure()
        v = res.verdicts.get(order_id)
        return render_explain(v) if v else f"unknown order {order_id}"

    def propose_recovery(self) -> list[dict]:
        res = self._ensure()
        ld, decisions = run_ledger_daemon(self.orders, res,
                                          attempts=self.executor.attempts)
        out = []
        for oid in sorted(ld.chased):
            v = res.verdicts[oid]
            o = self.orders_by_id[oid]
            pid = f"prop_{oid}_CREATE_PAYMENT_LINK"
            amount = v.delta_due_paise or o.amount_paise
            prop = {"proposal_id": pid, "order_id": oid, "action": "CREATE_PAYMENT_LINK",
                    "amount_paise": amount, "verdict": v.verdict.value,
                    "rule_fired": decisions[oid].rule_fired}
            self._proposals[pid] = prop
            out.append(prop)
        return out

    def approve(self, proposal_id: str) -> dict:
        prop = self._proposals.get(proposal_id)
        if prop is None:
            return {"error": f"unknown proposal {proposal_id} — call propose_recovery first"}
        res = self._ensure()
        o = self.orders_by_id[prop["order_id"]]
        v = res.verdicts[o.order_id]
        # re-gate at execution time: approval does not bypass policy (FR-9.1)
        d = policy.evaluate(o, v, prop["action"],
                            attempts_so_far=self.executor.attempts(o.order_id),
                            contacts_7d=0)
        if d.outcome != policy.ALLOW:
            return {"decision": d.outcome, "rule_fired": d.rule_fired, "detail": d.detail}
        r = self.executor.execute(o, prop["action"], prop["amount_paise"], d.rule_fired)
        return {"decision": "EXECUTED", "event_id": r["event_id"],
                "idempotent_replay": r["idempotent_replay"], "output": r["output"]}

    def audit(self, order_id: str) -> list[dict]:
        return self.executor.audit(order_id)

    def report(self) -> str:
        res = self._ensure()
        if not self.truth:
            return "no ground_truth.csv in batch — report needs a labelled batch"
        rep = evaluate(42, self.orders, self.captures, res, self.truth)
        return render_report(rep)


def _run_fastmcp(svc: Service) -> int:
    from mcp.server.fastmcp import FastMCP
    app = FastMCP("ledger-daemon")

    app.tool()(svc.reconcile)
    app.tool()(svc.explain)
    app.tool()(svc.propose_recovery)
    app.tool()(svc.approve)
    app.tool()(svc.audit)
    app.tool()(svc.report)
    app.run()
    return 0


def _run_stdio_fallback(svc: Service) -> int:
    """Minimal JSON-RPC 2.0 loop speaking enough MCP to list/call the six tools."""
    tools = {
        "reconcile": (svc.reconcile, {"batch_path": {"type": "string"}}),
        "explain": (svc.explain, {"order_id": {"type": "string"}}),
        "propose_recovery": (svc.propose_recovery, {}),
        "approve": (svc.approve, {"proposal_id": {"type": "string"}}),
        "audit": (svc.audit, {"order_id": {"type": "string"}}),
        "report": (svc.report, {}),
    }
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "serverInfo": {"name": "ledger-daemon", "version": "1.0.0"},
                      "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": name,
                 "description": fn.__doc__ or name,
                 "inputSchema": {"type": "object", "properties": props}}
                for name, (fn, props) in tools.items()]}
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments", {})
            fn, _ = tools[name]
            try:
                out = fn(**args)
            except Exception as exc:  # surface, never crash the loop
                out = {"error": str(exc)}
            text = out if isinstance(out, str) else json.dumps(out, indent=2)
            result = {"content": [{"type": "text", "text": text}]}
        elif mid is None:
            continue  # notification
        else:
            result = {}
        if mid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
            sys.stdout.flush()
    return 0


def main(root: str = "out") -> int:
    svc = Service(root)
    try:
        return _run_fastmcp(svc)
    except ImportError:
        return _run_stdio_fallback(svc)
