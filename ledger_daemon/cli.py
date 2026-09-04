"""CLI: `python -m ledger_daemon demo --seed 42 --n 500` runs the whole thing
offline in one command (AC-1). Also: generate | reconcile | explain | audit | mcp.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import agent, policy
from .datagen import generate, load_batch
from .evaluate import EvalReport, evaluate, render_exceptions, render_report, run_ledger_daemon
from .executor import Executor, default_adapter
from .models import Verdict
from .money import rupees_str
from .recon import calibrate, reconcile
from .robustness import CAL_SEED_OFFSET, assert_disjoint_seeds


def _paths(root: str) -> dict:
    return {
        "eval_batch": os.path.join(root, "data", "batch"),
        "cal_batch": os.path.join(root, "data", "calibration"),
        "eval_dir": os.path.join(root, "eval"),
        "db": os.path.join(root, "ledger.sqlite3"),
        "drafts": os.path.join(root, "drafts"),
    }


def write_baseline_contract(path: str, report: EvalReport,
                            elapsed_s: float, profile: str) -> None:
    """Persist the measured demo contract as deterministic, machine-readable JSON."""
    payload = {
        "dataset": {
            "kind": "synthetic",
            "profile": profile,
            "seed": report.seed,
            "n": report.n,
        },
        "dcpr": report.dcpr,
        "elapsed_ms": round(elapsed_s * 1000),
        "false_hold_rate": report.false_hold_rate,
        "match_rate": report.match_rate,
        "wrongly_chased_paise": report.wrong_paise["LD"],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True, indent=2)
        fh.write("\n")


def cmd_demo(args) -> int:
    t0 = time.perf_counter()
    root = args.out
    p = _paths(root)

    profile = getattr(args, "profile", "clean")
    cal_seed = args.seed + CAL_SEED_OFFSET
    assert_disjoint_seeds(cal_seed, args.seed)
    print(f"[1/6] generating calibration batch (seed={cal_seed}, n={args.n}, profile={profile}) ...")
    generate(cal_seed, args.n, p["cal_batch"], profile)
    cal = load_batch(p["cal_batch"])
    q_hat, fs_model, cal_probs = calibrate(cal[0], cal[1], cal[2], cal[3])
    print(f"      conformal q_hat = {q_hat:.4f} from {len(cal_probs)} labelled true matches (alpha=0.01)")

    print(f"[2/6] generating evaluation batch (seed={args.seed}, n={args.n}) ...")
    generate(args.seed, args.n, p["eval_batch"], profile)
    orders, captures, bank, truth = load_batch(p["eval_batch"])

    print("[3/6] reconciling (deterministic, zero LLM) ...")
    result = reconcile(orders, captures, bank, q_hat=q_hat, fs_model=fs_model)

    print("[4/6] policy gate + executor (idempotent, append-only audit) ...")
    if os.path.exists(p["db"]):
        os.remove(p["db"])
    execu = Executor(p["db"], adapter=default_adapter(), drafts_dir=p["drafts"])
    ld, decisions = run_ledger_daemon(orders, result)
    orders_by_id = {o.order_id: o for o in orders}
    executed = 0
    for oid in sorted(ld.chased):
        o = orders_by_id[oid]
        v = result.verdicts[oid]
        amount = v.delta_due_paise or o.amount_paise
        execu.execute(o, "CREATE_PAYMENT_LINK", amount, decisions[oid].rule_fired)
        execu.execute(o, "DRAFT_REMINDER", amount, decisions[oid].rule_fired)
        executed += 1
    print(f"      {executed} orders actioned via {execu.adapter.name} adapter; "
          f"{len(orders) - executed} blocked or held")

    print("[5/6] sandboxed proposal layer on AMBIGUOUS orders ...")
    proposals = []
    for oid in result.exception_ids:
        v = result.verdicts[oid]
        prop = agent.propose(oid, v.reason, v.evidence_refs)
        proposals.append(prop)
        execu.audit_write(
            f"prop_{oid}", "agent", "llm-proposal", oid,
            {"reason": v.reason}, prop.to_dict(),
            "R7" if prop.confidence < policy.LLM_MIN_CONFIDENCE else "R_ALLOW",
            "PROPOSED", 0)
    held = sum(1 for pr in proposals if pr.confidence < policy.LLM_MIN_CONFIDENCE)
    print(f"      {len(proposals)} proposals, {held} held for human (confidence < 0.85)")

    print("[6/6] evaluation harness ...")
    report = evaluate(args.seed, orders, captures, result, truth)
    os.makedirs(p["eval_dir"], exist_ok=True)
    write_baseline_contract(
        os.path.join(p["eval_dir"], "baseline-contract.json"),
        report,
        time.perf_counter() - t0,
        profile,
    )
    suffix = "" if profile == "clean" else f"-{profile}"
    body = render_report(report, date=f"profile={profile}" if profile != "clean" else "")
    with open(os.path.join(p["eval_dir"], f"results{suffix}.md"), "w", encoding="utf-8") as fh:
        fh.write("```\n" + body + "```\n")
    with open(os.path.join(p["eval_dir"], f"exceptions{suffix}.md"), "w", encoding="utf-8") as fh:
        fh.write(render_exceptions(report))

    print()
    print(body)
    print(f"total wall time: {time.perf_counter() - t0:.1f}s  "
          f"(artifacts in {os.path.abspath(root)})")
    return 0


def cmd_import_statement(args) -> int:
    from .parsers import StatementError, parse_statement, write_bank_csv
    try:
        txns = parse_statement(args.file, bank=args.bank)
    except StatementError as exc:
        print(f"import failed: {exc}")
        return 1
    path = write_bank_csv(txns, args.out)
    credits = sum(1 for t in txns if t.credit_debit == "credit")
    print(f"parsed {len(txns)} rows ({credits} credits) -> {os.path.abspath(path)}")
    print(f"  python -m ledger_daemon reconcile --dir {args.out}")
    print(f"  python -m ledger_daemon ui --dir {args.out}")
    return 0


def cmd_ui(args) -> int:
    from .ui import serve
    p = _paths(args.out)
    if args.dir:
        orders, captures, bank, _truth = load_batch(args.dir)
        result = reconcile(orders, captures, bank, q_hat=0.001)
        source = f"live batch: {os.path.abspath(args.dir)}"
    else:
        assert_disjoint_seeds(args.seed + CAL_SEED_OFFSET, args.seed)
        generate(args.seed + CAL_SEED_OFFSET, args.n, p["cal_batch"])
        cal = load_batch(p["cal_batch"])
        q_hat, fs_model, _probs = calibrate(cal[0], cal[1], cal[2], cal[3])
        generate(args.seed, args.n, p["eval_batch"])
        orders, captures, bank, _truth = load_batch(p["eval_batch"])
        result = reconcile(orders, captures, bank, q_hat=q_hat, fs_model=fs_model)
        source = f"synthetic world, seed {args.seed}, n={args.n}"
    _ld, decisions = run_ledger_daemon(orders, result)
    execu = Executor(p["db"], adapter=default_adapter(), drafts_dir=p["drafts"])
    serve(orders, result.verdicts, decisions, execu, source,
          port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_ingest(args) -> int:
    from .ingest import IngestError, ingest
    try:
        counts = ingest(args.out, limit=args.limit)
    except IngestError as exc:
        print(f"ingest failed: {exc}")
        return 1
    print(f"wrote {os.path.abspath(args.out)}:")
    print(f"  merchant_orders.csv    {counts['orders']} orders  (/v1/orders)")
    print(f"  gateway_captures.csv   {counts['captures']} captures  (/v1/payments; "
          f"{counts['skipped_payments']} in-flight payments skipped)")
    print(f"  bank_statement.csv     {counts['bank']} settlement credits standing in "
          f"for the bank feed  ({counts['unprocessed_settlements']} unprocessed skipped)")
    print(f"  quarantine.jsonl       {counts['quarantined']} malformed or duplicate source rows")
    print("no ground_truth.csv: real data has no oracle. next:")
    print(f"  python -m ledger_daemon reconcile --dir {args.out}")
    return 0


def cmd_prove(args) -> int:
    from .prove import run
    return run()


def cmd_sweep(args) -> int:
    from .robustness import render, run_sweep

    profile = getattr(args, "profile", "clean")
    seeds = [args.start + i for i in range(args.seeds)]
    print(f"sweeping {len(seeds)} unseen seeds (n={args.n}, profile={profile}); "
          "q_hat is re-derived per seed, nothing is refitted by hand ...")
    t0 = time.perf_counter()
    rows = run_sweep(seeds, args.n, profile)
    for r in rows:
        print(f"  seed {r['seed']:>5}  DCPR {r['dcpr']:6.1%}  "
              f"false-hold {r['false_hold_rate']:6.1%}  match {r['match_rate']:6.1%}  "
              f"wrongly chased {rupees_str(r['ld_wrong_paise'])}")
    body = render(rows, args.n, profile)
    p = _paths(args.out)
    os.makedirs(p["eval_dir"], exist_ok=True)
    suffix = "" if profile == "clean" else f"-{profile}"
    out_path = os.path.join(p["eval_dir"], f"robustness{suffix}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print()
    print(body)
    print(f"wrote {out_path}  ({time.perf_counter() - t0:.1f}s)")
    return 0


def cmd_ablate(args) -> int:
    from .ablation import render, run_ablation, sweep_alpha
    p = _paths(args.out)
    profile = getattr(args, "profile", "clean")
    assert_disjoint_seeds(args.seed + CAL_SEED_OFFSET, args.seed)
    generate(args.seed + CAL_SEED_OFFSET, args.n, p["cal_batch"], profile)
    cal = load_batch(p["cal_batch"])
    q_hat, fs_model, cal_probs = calibrate(cal[0], cal[1], cal[2], cal[3])
    generate(args.seed, args.n, p["eval_batch"], profile)
    orders, captures, bank, truth = load_batch(p["eval_batch"])
    print(f"running 5-config ablation ladder (profile={profile}) ...")
    rows = run_ablation(args.seed, orders, captures, bank, truth, q_hat, fs_model)
    print("sweeping conformal alpha for the risk-coverage curve ...")
    sweep = sweep_alpha(args.seed, orders, captures, bank, truth, cal_probs, fs_model)
    body = render(rows, sweep, args.seed, args.n, profile)
    os.makedirs(p["eval_dir"], exist_ok=True)
    suffix = "" if profile == "clean" else f"-{profile}"
    out_path = os.path.join(p["eval_dir"], f"ablation{suffix}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print()
    print(body)
    print(f"wrote {out_path}")
    return 0


def cmd_agent_eval(args) -> int:
    """Score the sandboxed LLM proposal layer against ground truth on the
    exception list. With no local model the fallback is what gets measured —
    the layer is an evaluated component either way, never a mascot."""
    p = _paths(args.out)
    if not os.path.exists(os.path.join(p["eval_batch"], "merchant_orders.csv")):
        generate(args.seed, args.n, p["eval_batch"], getattr(args, "profile", "clean"))
    orders, captures, bank, truth = load_batch(p["eval_batch"])
    result = reconcile(orders, captures, bank)
    backend = "ollama" if os.environ.get("LEDGER_DAEMON_LLM") == "ollama" else "deterministic fallback"
    rows, correct, actionable, held = [], 0, 0, 0
    for oid in result.exception_ids:
        v = result.verdicts[oid]
        prop = agent.propose(oid, f"{v.reason}. Evidence: {v.evidence.detail}", v.evidence_refs)
        truth_v = truth.get(oid, {}).get("true_verdict", "?")
        ok = prop.proposed_verdict == truth_v
        correct += ok
        if prop.confidence >= policy.LLM_MIN_CONFIDENCE:
            actionable += 1
        else:
            held += 1
        rows.append(f"| {oid} | {truth_v} | {prop.proposed_verdict} | "
                    f"{prop.confidence:.2f} | {'yes' if ok else 'no'} |")
    n = len(result.exception_ids)
    body = "\n".join([
        f"# Proposal-layer evaluation  (backend: {backend}, seed={args.seed}, n={args.n})", "",
        f"- Exceptions evaluated: {n}",
        f"- Proposals agreeing with ground truth: {correct}/{n}"
        + (f" ({correct / n:.0%})" if n else ""),
        f"- Proposals above the R7 confidence bar (0.85): {actionable}",
        f"- Held for human by R7: {held}",
        "",
        "Even a perfect proposal changes nothing directly: proposals are input to the",
        "policy gate (FR-4.5), and anything under 0.85 confidence is held for a human.", "",
        "| order | ground truth | proposed | confidence | correct |",
        "|---|---|---|---|---|",
        *rows,
    ]) + "\n"
    os.makedirs(p["eval_dir"], exist_ok=True)
    out_path = os.path.join(p["eval_dir"], "agent-eval.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print(body)
    print(f"wrote {out_path}")
    if backend != "ollama":
        print("\n(no local model: set LEDGER_DAEMON_LLM=ollama with Ollama running to "
              "score a real model; the fallback's honest abstention is what was measured)")
    return 0


def cmd_crosscheck(args) -> int:
    from .crosscheck import run_crosscheck
    p = _paths(args.out)
    if not os.path.exists(os.path.join(p["eval_batch"], "merchant_orders.csv")):
        generate(args.seed, args.n, p["eval_batch"], getattr(args, "profile", "clean"))
    print("running splink cross-check (independent Fellegi-Sunter + EM) ...")
    body = run_crosscheck(p["eval_batch"])
    os.makedirs(p["eval_dir"], exist_ok=True)
    out_path = os.path.join(p["eval_dir"], "crosscheck.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print()
    print(body)
    print(f"wrote {out_path}")
    return 0


def cmd_generate(args) -> int:
    paths = generate(args.seed, args.n, args.out)
    for name, path in paths.items():
        print(f"wrote {path}")
    return 0


def cmd_reconcile(args) -> int:
    orders, captures, bank, truth = load_batch(args.batch)
    result = reconcile(orders, captures, bank)
    counts: dict[str, int] = {}
    for v in result.verdicts.values():
        counts[v.verdict.value] = counts.get(v.verdict.value, 0) + 1
    print(json.dumps({
        "orders": len(orders),
        "verdict_counts": dict(sorted(counts.items())),
        "exceptions": result.exception_ids,
        "orders_per_sec": result.orders_per_sec,
    }, indent=2))
    return 0


def cmd_explain(args) -> int:
    orders, captures, bank, truth = load_batch(args.batch)
    result = reconcile(orders, captures, bank)
    v = result.verdicts.get(args.order_id)
    if v is None:
        print(f"unknown order {args.order_id}", file=sys.stderr)
        return 1
    print(render_explain(v))
    return 0


def render_explain(v) -> str:
    lines = [
        f"order      : {v.order_id}",
        f"verdict    : {v.verdict.value}",
        f"pass used  : {v.evidence.pass_used}",
        f"evidence   : {', '.join(v.evidence_refs) or '(none)'}",
        f"detail     : {v.evidence.detail}",
        f"reason     : {v.reason}",
    ]
    if v.money_received_paise:
        lines.append(f"received   : {rupees_str(v.money_received_paise)}")
    if v.delta_due_paise:
        lines.append(f"delta due  : {rupees_str(v.delta_due_paise)}")
    if v.evidence.weight_waterfall:
        lines.append("weight waterfall (Fellegi-Sunter, log2 units):")
        for label, w in v.evidence.weight_waterfall:
            lines.append(f"  {label:<55} {w:>7}")
        if v.p_match:
            lines.append(f"  {'P(match) = 1/(1+2^-total)':<55} {v.p_match:>7}")
    return "\n".join(lines)


def cmd_audit(args) -> int:
    execu = Executor(args.db)
    rows = execu.audit(args.order_id)
    print(json.dumps(rows, indent=2))
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import main as mcp_main
    return mcp_main(args.root)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; ₹ needs UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="ledger-daemon",
                                 description="Never chase money you already have.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="end-to-end offline demo: generate, reconcile, gate, execute, evaluate")
    d.add_argument("--seed", type=int, default=42)
    d.add_argument("--n", type=int, default=500)
    d.add_argument("--out", default="out")
    d.add_argument("--profile", choices=["clean", "stress"], default="clean",
                   help="stress = typo'd/truncated narrations + amount-collision decoys")
    d.set_defaults(fn=cmd_demo)

    ist = sub.add_parser("import-statement", help="parse a real bank statement export (HDFC/ICICI/canonical) into a batch dir")
    ist.add_argument("file", help="path to the statement CSV export")
    ist.add_argument("--out", default=os.path.join("data", "live"))
    ist.add_argument("--bank", choices=["auto", "hdfc", "icici", "canonical"], default="auto")
    ist.set_defaults(fn=cmd_import_statement)

    u = sub.add_parser("ui", help="serve the one-screen chase list on localhost")
    u.add_argument("--seed", type=int, default=42)
    u.add_argument("--n", type=int, default=500)
    u.add_argument("--dir", default="", help="reconcile this batch dir (e.g. an ingested live one) instead of a synthetic world")
    u.add_argument("--out", default="out")
    u.add_argument("--port", type=int, default=7042)
    u.add_argument("--no-browser", action="store_true")
    u.set_defaults(fn=cmd_ui)

    ig = sub.add_parser("ingest", help="pull real Razorpay test-mode orders/payments/settlements into a batch dir")
    ig.add_argument("--out", default=os.path.join("data", "live"))
    ig.add_argument("--limit", type=int, default=1000, help="max rows per entity")
    ig.set_defaults(fn=cmd_ingest)

    pv = sub.add_parser("prove", help="demonstrate the verdict-exhaustiveness guard firing")
    pv.set_defaults(fn=cmd_prove)

    sw = sub.add_parser("sweep", help="re-run the whole pipeline on N unseen seeds")
    sw.add_argument("--seeds", type=int, default=20, help="how many seeds to sweep")
    sw.add_argument("--start", type=int, default=100, help="first seed (default 100, well clear of the committed 42)")
    sw.add_argument("--n", type=int, default=500)
    sw.add_argument("--out", default="out")
    sw.add_argument("--profile", choices=["clean", "stress"], default="clean")
    sw.set_defaults(fn=cmd_sweep)

    ab = sub.add_parser("ablate", help="5-config ablation ladder + conformal risk-coverage curve")
    ab.add_argument("--seed", type=int, default=42)
    ab.add_argument("--n", type=int, default=500)
    ab.add_argument("--out", default="out")
    ab.add_argument("--profile", choices=["clean", "stress"], default="clean")
    ab.set_defaults(fn=cmd_ablate)

    ae = sub.add_parser("agent-eval", help="score the LLM proposal layer against ground truth on exceptions")
    ae.add_argument("--seed", type=int, default=42)
    ae.add_argument("--n", type=int, default=500)
    ae.add_argument("--out", default="out")
    ae.add_argument("--profile", choices=["clean", "stress"], default="clean")
    ae.set_defaults(fn=cmd_agent_eval)

    cc = sub.add_parser("crosscheck", help="independent splink (MoJ) cross-check of the matcher")
    cc.add_argument("--seed", type=int, default=42)
    cc.add_argument("--n", type=int, default=500)
    cc.add_argument("--out", default="out")
    cc.add_argument("--profile", choices=["clean", "stress"], default="clean")
    cc.set_defaults(fn=cmd_crosscheck)

    g = sub.add_parser("generate", help="write the four synthetic CSVs")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--n", type=int, default=500)
    g.add_argument("--out", default="out/data/batch")
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("reconcile", help="reconcile a batch directory")
    r.add_argument("--batch", default="out/data/batch")
    r.set_defaults(fn=cmd_reconcile)

    e = sub.add_parser("explain", help="evidence chain for one order")
    e.add_argument("order_id")
    e.add_argument("--batch", default="out/data/batch")
    e.set_defaults(fn=cmd_explain)

    a = sub.add_parser("audit", help="append-only audit trail for one order")
    a.add_argument("order_id")
    a.add_argument("--db", default="out/ledger.sqlite3")
    a.set_defaults(fn=cmd_audit)

    m = sub.add_parser("mcp", help="run the MCP server (stdio)")
    m.add_argument("--root", default="out")
    m.set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
