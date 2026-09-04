# Ledger Daemon

**100.0% of already-paid orders that a naive dunning agent would have chased were blocked — with a 4.0% false-hold rate on genuinely unpaid orders, and ₹0 wrongly chased.** (seed 42, n = 500, offline, < 5 s end to end.)

> *"I recorded payment on an outstanding invoice but the system still send out a payment reminder… this will get my clients upset."* — Zoho Invoice user

**Thesis: you cannot autonomously chase money until you can prove it hasn't already arrived.**

Ledger Daemon is a local-first, headless MCP server that performs deterministic three-way reconciliation (payment gateway × bank statement × merchant books) and uses the result as a **hard precondition** on any autonomous revenue-recovery action. Every mainstream dunning tool fires on invoice/gateway status; none makes cross-source reconciliation a precondition to chase. That gap — settlement lag, late authorisation, out-of-band NEFT/UPI — is where paying customers get chased, and "collecting a debt not owed" has been the single largest US debt-collection complaint category every year since 2013.

## Run it

```bash
python -m ledger_daemon demo --seed 42 --n 500
```

One command. Fully offline. Zero signup, zero cloud account, zero required dependencies beyond Python 3.11+ stdlib. Tests: `python -m pytest tests -q` (28 tests, ~10 s).

```
LEDGER DAEMON — EVALUATION REPORT      seed=42  n=500

Match rate ................................ 99.6%  (498/500)
Throughput ................................ 25,000+ orders/sec
Unresolved exceptions ..................... 16  -> eval/exceptions.md
Conformal q_hat ........................... 0.0010  (calibrated)

---- Double-chase prevention ----------------------------------
Already-paid orders a naive agent would chase:  80
Blocked by Ledger Daemon:                       80
DOUBLE-CHASE PREVENTION RATE:                   100.0%  [95% CI 100.0%, 100.0%]

False-hold rate (unpaid, wrongly blocked):      4.0%  (2/50)

Rupees wrongly chased:
  B0 naive ................ ₹2,59,79,763.00  (109 customers)
  B1 gateway-only ......... ₹1,49,22,130.00  (59 customers)
  B2 recon, no gate .......   ₹38,79,161.00  (16 customers)
  Ledger Daemon ...........           ₹0.00  (0 customers)
```

The false-hold rate is always printed next to DCPR — without it, DCPR is gameable by blocking everything. The two false-holds are policy rule R2 working as intended: their `due_date + 3` window extends beyond the bank feed's coverage, and *absence of evidence is not evidence of absence*.

## Architecture

```
 CSVs (gateway × bank × books)
        │
        ▼
 ┌─────────────────────────────┐   deterministic, zero LLM
 │ Reconciliation (4 passes)   │   pass 1: exact UTR join
 │ Fellegi-Sunter weights      │   pass 2: net amount + T+0..T+3 window
 │ optimal assignment          │   pass 3: split-settlement aggregation
 │ conformal abstention        │   pass 4: scored narration match
 └──────────────┬──────────────┘        (only where amount already agrees)
                ▼
        one of 9 Verdicts per order ── AMBIGUOUS ──▶ sandboxed LLM proposal
                │                                    (typed output, no tools,
                ▼                                     R7 holds < 0.85 conf)
 ┌─────────────────────────────┐
 │ Policy engine R1–R7         │   first DENY/HOLD wins; default DENY
 └──────────────┬──────────────┘
                ▼ ALLOW only
 ┌─────────────────────────────┐
 │ Idempotent executor         │   event_id = sha256(order|action|attempt) PK
 │ append-only SQLite audit    │   duplicates rejected by the DB itself
 └─────────────────────────────┘
```

### Trust boundaries

| Layer | May move money? | May write state? | LLM involved? |
|---|---|---|---|
| Reconciliation | no | no | **never** |
| Verdict resolution | no | no | never |
| LLM proposal layer | no | no | optional, sandboxed |
| Policy engine | no | no | never |
| Executor | yes (test mode) | audit log only | never |

### Why this matcher is defensible

- **Fellegi-Sunter (1969) log-likelihood weights**, not an arbitrary fuzzy threshold. The score is a probability, and the per-field weight waterfall *is* the explanation — the actual arithmetic, not a post-hoc approximation. Run `python -m ledger_daemon explain ORD-1316`.
- **u-probabilities are estimated, not tuned** — sampled from random record pairs, no labels needed.
- **Component-wise optimal assignment** — two orders can never claim the same bank credit. A greedy matcher manufactures exactly the false chase this product exists to prevent.
- **Conformal abstention (split conformal, α = 0.01)** — the AMBIGUOUS threshold is *derived* from a calibration batch, giving a distribution-free coverage guarantee. Caveat, stated honestly: the guarantee assumes exchangeability between calibration and test data; on synthetic data from one generator that holds trivially, and in production you would re-calibrate on a rolling window.
- **Cost-sensitive floors (Elkan 2001)** — the threshold moves with the invoice value: blocking a chase on a ₹5,00,000 invoice demands more certainty than on a ₹4,200 one.
- **Integer paise everywhere.** Floats are forbidden in every monetary path and a test enforces it.

## Ablation: every component earns its place

`python -m ledger_daemon ablate` — real numbers, seed 42 (full table + the conformal risk-coverage curve printed by the command):

| Configuration | Match rate | DCPR | Exceptions | ₹ wrongly chased |
|---|---|---|---|---|
| R0 fuzzy @ 82, greedy | 95.8% | 92.5% | 1 | ₹14,76,493 |
| R1 + optimal assignment | 98.6% | 92.5% | 15 | ₹14,76,493 |
| R2 + Fellegi-Sunter weights | 99.6% | **100.0%** | 16 | **₹0** |
| R3 + conformal abstention | 99.6% | 100.0% | 16 | ₹0 |
| R4 + cost-sensitive floors (shipped) | 99.6% | 100.0% | 16 | ₹0 |

R3 and R4 are flat on this seed and we publish that anyway: F-S scores are so well separated on this generator that the conformal band never binds — every residual exception comes from assignment ties and duplicate UTRs, not borderline probabilities. Those layers are the insurance that binds when real-world narration quality degrades.

## Answering the hard questions

**"You graded your own exam."** Partly true — so we brought an external examiner. `python -m ledger_daemon crosscheck` hands the same matching universe to **[splink](https://github.com/moj-analytical-services/splink)** (UK Ministry of Justice's open-source Fellegi-Sunter engine — its own u sampling, its own EM-trained m values, zero shared parameters): **100% same-answer agreement, 0 genuinely different answers** on seed 42. That doesn't prove the generator is realistic; it proves the matcher isn't overfit to its own scoring quirks. splink also has no clerical-review region — the 16 orders we abstain on, it is forced to guess about.

**"The data is too clean."** `--profile stress` degrades every narration (typos, heavy truncation, 3/4 of transfers lose their invoice number) and plants amount-collision decoys against unpaid invoices. Result: DCPR stays **100%**, ₹ wrongly chased stays **₹0**, and false-hold rises to 24% — the system pays for noise with *abstention, never with wrong chases*. That is the designed failure direction, and the stress ablation shows greedy fuzzy matching wrongly chasing ₹34 lakh on the same data.

**"Where's the AI?"** Confined and *measured*. `python -m ledger_daemon agent-eval` scores the proposal layer against ground truth on the exception list; with no model installed, the deterministic fallback's honest abstention already agrees with ground truth on 14/16 exceptions — the bar any local model (set `LEDGER_DAEMON_LLM=ollama`) has to beat before R7 lets a single proposal matter. LLMs are kept off the decision path because their confidence scores are poorly calibrated, which would break the conformal and cost-sensitive layers — that's an allocation argument, not a limitation.

**"Hand-rolled string matching?"** The stdlib Jaro-Winkler is parity-tested against **rapidfuzz**'s C++ implementation (500 randomized cases, exact agreement — the test caught a real divergence in Winkler's boost-threshold rule). rapidfuzz is used automatically when installed; the stdlib path remains the dependency-free contract.

## MCP surface (6 tools)

```
reconcile(batch_path) -> {verdict_counts, exception_ids, orders_per_sec}
explain(order_id)     -> evidence chain: pass used, source rows, weight waterfall
propose_recovery()    -> proposals for chaseable verdicts only
approve(proposal_id)  -> executes; idempotent; re-gated by policy; returns event_id
audit(order_id)       -> full append-only trail
report()              -> the evaluation block
```

`propose_recovery()` and `approve()` are separate calls — no single tool call may move money. Run with `python -m ledger_daemon mcp` (uses the official `mcp` SDK if installed, else a built-in stdio fallback). Claude Desktop config:

```json
{"mcpServers": {"ledger-daemon": {"command": "python", "args": ["-m", "ledger_daemon", "mcp", "--root", "out"], "cwd": "<repo path>"}}}
```

## Live Razorpay test-mode call

Set `RZP_TEST_KEY_ID` / `RZP_TEST_KEY_SECRET` and the executor creates one real test-mode payment link. Without keys, a deterministic mock adapter with the same interface is used — a missing key never breaks the demo.

## Limitations

- Synthetic data. The messiness distribution is realistic (NEFT/IMPS/UPI/settlement narration formats, fee+GST arithmetic, split settlements, duplicate UTRs) and labels are written at injection time, but no real merchant data was used.
- The conformal guarantee is only as good as calibration/test exchangeability (see above).
- The recovery-value figures use a documented 0.6 recovery-rate assumption; the per-incident cost of wrongly chasing a paying customer is not publicly published anywhere, so the ₹800 action-cost constant is an assumption, labelled as such.
- Bank feeds are batch in reality; R2 tolerates that by holding, not chasing, until coverage exists.

Every number above is regenerated from source: `python -m ledger_daemon evaluate`, `ablate`, `crosscheck` and `agent-eval` reproduce the full evaluation on seed 42.
