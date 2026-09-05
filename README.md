# Ledger Daemon

**Know which orders are paid. See which ones need checking.**

Ledger Daemon compares your orders, Razorpay payments, and bank deposits. It finds unpaid orders, flags unclear matches, and shows the records behind each result. This helps your team avoid chasing a customer who already paid. Open the local app with:

```bash
python -m ledger_daemon ui
```

At `/`, try sample orders, search payment results, check a proof, or watch the app and command-line demos. At `/app`, review the full batch and record human decisions. Fonts and artwork work offline. The GitHub Pages site in `docs/` is exported from this same landing page; it shows saved sample results. See [website setup and export](docs/PAGES.md).

**Synthetic result: 96.8% verdict accuracy (484/500 orders), 100.0% double-chase prevention, 4.0% false holds on genuinely unpaid orders, and ₹0 wrongly chased.**
(synthetic data, seed 42, n = 500, offline, judge fingerprint `45583b83cbff6bd0`. Every number in this README is in [CLAIMS.md](CLAIMS.md) with the command that produced it.)

**Synthetic demo and metrics. It uses a mock executor; no credentialed Razorpay Test Mode API run or payment-link creation is included.**

Verdict accuracy means agreement with ground-truth labels, not automatic resolution coverage. Policy-held orders, exception cases, and source-row exceptions are separate populations. Optional model uplift has not been demonstrated; the demo reports the actual regex benchmark rather than attributing it to an LLM.

Razorpay AI Buildathon 2026 · **Track 04, AI Finance Controller** — the only track this project is submitted under. Revenue recovery appears once, as a *Downstream Control Demonstration: Why Correct Reconciliation Matters*, and never as a second submission.

The local-first controller also exposes an MCP server. Deterministic three-way reconciliation (payment gateway × bank statement × merchant books) provides evidence for the close decision and is a hard precondition on downstream recovery actions.

## One command an evaluator can run

```bash
python -m ledger_daemon judge --seed 42 --n 500 --out out/judge
```

Eight profiles — clean, realistic, stress, distribution-shift, adversarial, source-incomplete, high-collision, concurrent — each a full generate → validate → reconcile → gate → execute → prove → verify pass with a declared fault plan in front of it. It grades eleven injected attacks against oracles they declared beforehand, checks six hard invariants, exits nonzero if any fails, and writes `summary.json`, `cases.jsonl`, `attacks.json`, `latency.json`, `proof-manifest.json` and `claims.md` either way.

Offline, with no keys or network. The recorded 26.9 s run came from one laptop and timing varies by machine. Full results are in [EVALUATION.md](EVALUATION.md).

| document | what it settles |
|---|---|
| [CLAIMS.md](CLAIMS.md) | every published number, with dataset class, profile, seed, n, command and artifact |
| [EVALUATION.md](EVALUATION.md) | the eight profiles, the eleven graded attacks, the cost table |
| [METHODS.md](METHODS.md) | schemas, passes, the score, abstention, the proof and its verifier, metric definitions |
| [LIMITATIONS.md](LIMITATIONS.md) | what this has *not* shown, worst first |
| [SIMULATED_VS_REAL.md](SIMULATED_VS_REAL.md) | synthetic, schema-derived, Test Mode API, and merchant-provided categories — and which may produce a metric |
| [SECURITY.md](SECURITY.md) | PII masking, the test-mode guard, the model boundary, audit integrity |
| [WHAT_BROKE.md](WHAT_BROKE.md) | what broke building the controller, including what is still open |
| [BROKEN.md](BROKEN.md) | what broke building the reconciliation engine |

## Run it

```bash
python -m ledger_daemon demo --seed 42 --n 500
```

Fully offline. Zero signup, zero cloud account, zero required dependencies beyond the Python 3.11+ standard library. Tests: `python -m pytest -q` — the suite prints its own count, so this page does not have one to go stale.

```bash
python -m ledger_daemon ui          # botanical landing + working controller, localhost
python -m ledger_daemon ingest      # fetch Razorpay Test Mode API objects; no real money moves (needs test keys)
python -m ledger_daemon import-statement bank.csv   # parse an HDFC/ICICI statement export
python -m ledger_daemon sweep       # the whole pipeline on 20 unseen seeds
python -m ledger_daemon ablate      # 5-config ablation ladder + risk-coverage curve
python -m ledger_daemon crosscheck  # independent splink cross-check of the matcher
python -m ledger_daemon prove       # watch the policy engine reject an unhandled verdict
python -m ledger_daemon agent-eval  # score the proposal layer against ground truth
python -m ledger_daemon learn-rule-demo  # authenticated rule lifecycle + HTML evidence
python -m ledger_daemon mcp         # run as an MCP server over stdio
```

## Safe learning is a lifecycle, not a prompt

```bash
python -m ledger_daemon learn-rule-demo --out out/rule-demo
```

This executes the stored rule set end to end: a verified human case resolution compiles to bounded JSON, a pre-replay approval attempt is rejected, authenticated confirmed and attack corpora produce a signed zero-regression replay receipt, an independent reviewer approves the exact version, and activation produces a receipt that is bound into a copied reconciliation config. It writes `rule-lifecycle.json`, an inspectable SQLite history, and `rule-lifecycle.html`—a judge-facing operational panel rather than a chat transcript. No model can propose executable code or grant itself approval.

```
LEDGER DAEMON — EVALUATION REPORT      seed=42  n=500

Verdict accuracy (match rate) .............. 96.8%  (484/500 orders)
Unresolved exceptions ..................... 30  -> eval/exceptions.md
Conformal q_hat ........................... 0.0010  (calibrated)

---- Double-chase prevention ----------------------------------
Already-paid orders a naive agent would chase:  100
Blocked by Ledger Daemon:                       100
DOUBLE-CHASE PREVENTION RATE:                   100.0%  [observed 100/100; synthetic batch]

False-hold rate (unpaid, wrongly blocked):      4.0%  (2/50)

Rupees wrongly chased:
  B0 naive ................ ₹3,27,56,717.00  (129 customers)
  B1 gateway-only ......... ₹2,06,98,485.00  (79 customers)
  B2 recon, no gate .......   ₹72,23,062.00  (30 customers)
  Ledger Daemon ...........           ₹0.00  (0 customers)
```

The false-hold rate is always printed next to DCPR — without it, DCPR is gameable by blocking everything. The two false-holds are policy rule R2 working as intended: their `due_date + 3` window extends beyond the bank feed's coverage, and *absence of evidence is not evidence of absence*. The 30 exceptions are honest abstentions — mostly out-of-band and TDS credits whose narration carries no invoice number; every one is blocked from chasing while it waits for a human.

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
        one of 11 Verdicts per order ── AMBIGUOUS ──▶ sandboxed LLM proposal
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

| Capability | External object / side effect | Moves money? | LLM involved? |
|---|---|---|---|
| Reconciliation | no | no | **never** |
| Verdict resolution | audit record only | no | never |
| LLM proposal layer | no | no | optional, sandboxed |
| Policy engine | no | no | never |
| Test Mode payment link | External object / side effect | Does not move money | never |
| Mock executor in the published demo | no external API call | no | never |

### Why this matcher is defensible

- **Fellegi-Sunter (1969) log-likelihood weights**, not an arbitrary fuzzy threshold. The score is a probability, and the per-field weight waterfall *is* the explanation — the actual arithmetic, not a post-hoc approximation. Run `python -m ledger_daemon explain ORD-1316`.
- **u-probabilities are estimated, not tuned** — sampled from random record pairs, no labels needed.
- **Component-wise optimal assignment** — two orders can never claim the same bank credit. A greedy matcher manufactures exactly the false chase this product exists to prevent.
- **Conformal abstention (split conformal, α = 0.01)** — the AMBIGUOUS threshold is *derived* from a calibration batch, giving a distribution-free coverage guarantee. Caveat, stated honestly: the guarantee assumes exchangeability between calibration and test data; on synthetic data from one generator that holds trivially, and in production you would re-calibrate on a rolling window.
- **Cost-sensitive floors (Elkan 2001)** — the threshold moves with the invoice value: blocking a chase on a ₹5,00,000 invoice demands more certainty than on a ₹4,200 one.
- **Integer paise everywhere.** Floats are forbidden in every monetary path and a test enforces it.

## TDS: a percentage is a clue, never proof

An Indian B2B receipt can be lower than its invoice because tax was withheld, but the same arithmetic can also be a genuine short-payment. Ledger Daemon therefore has two different verdicts. A rate-shaped shortfall without external tax evidence is `POSSIBLE_TDS_WITHHOLDING`: collections is held and a human must obtain evidence. `PAID_NET_OF_TDS` is available only when the bank receipt is accompanied by a typed, source-bound TDS record containing the exact withheld amount, hashed payer and merchant PAN identities, an effective-date-versioned tax-rule id, and a certificate reference.

Every schedule-driven dunning system reads that shortfall as a debt. Ledger Daemon's exact-amount candidate gate used to make the same mistake in a worse way: the net credit never even became a match candidate, so a fully compliant payer came out `GENUINELY_UNPAID` — the exact wrong chase this project exists to prevent, hiding inside it.

The fix is three deliberate pieces, not a tolerance band:

- **`tds_rate_bp()`** recognises a shortfall that is *exactly* 1%, 2% or 10% of the invoice, integer paise, no tolerance — a shortfall that is *approximately* 2% is a short payment and stays chaseable (test-enforced).
- The candidate gate probes common TDS-shaped net amounts so the receipt can enter matching. That heuristic cannot close the invoice.
- Without typed evidence, policy returns a human `HOLD`; it does not declare the customer compliant or the invoice settled.
- With matching typed evidence, the proof certificate binds the tax-evidence row and its amount. The independent verifier rejects raw PAN values, mismatched amounts, future-effective rules, missing evidence, and tampering.
- Tax applicability is not inferred by this project. The rule id and certificate reference come from an external tax/accounting source; this remains an operational control, not a statutory audit opinion.

Adding the verdict tripped `_assert_exhaustive()` before any of that logic existed — the policy engine refused to import until someone declared what collections does about withheld tax. That is the exhaustiveness guard doing its job on an implemented feature, not in a demo.

## Ablation: every component earns its place

`python -m ledger_daemon ablate` — synthetic evaluation numbers, seed 42 (full table + the conformal risk-coverage curve printed by the command):

| Configuration | Match rate | DCPR | Exceptions | ₹ wrongly chased |
|---|---|---|---|---|
| R0 fuzzy @ 82, greedy | 94.4% | 89.0% | 3 | ₹23,52,176 |
| R1 + optimal assignment | 97.2% | 89.0% | 17 | ₹23,52,176 |
| R2 + Fellegi-Sunter weights | 99.2% | **100.0%** | 18 | **₹0** |
| R3 + conformal abstention | 96.8% | 100.0% | 30 | ₹0 |
| R4 + cost-sensitive floors (shipped) | 96.8% | 100.0% | 30 | ₹0 |

Read R2 vs R3 honestly: conformal abstention *costs* 2.4 points of match rate — twelve extra name-only credits pushed from a confident guess into the exception list — and buys nothing extra in DCPR on this seed. We ship it anyway, and publish the cost, because P(match)=0.999 on a name-similarity alone is exactly the kind of confidence that stops being trustworthy the day narration quality degrades; the stress ablation is where that insurance pays out.

## Answering the hard questions

**"You graded your own exam."** Partly true — so we brought an external examiner. `python -m ledger_daemon crosscheck` hands the same matching universe to **[splink](https://github.com/moj-analytical-services/splink)** (UK Ministry of Justice's open-source Fellegi-Sunter engine — its own u sampling, its own EM-trained m values, zero shared parameters): **100% same-answer agreement, 0 genuinely different answers** on seed 42. That doesn't prove the generator is realistic; it proves the matcher isn't overfit to its own scoring quirks. splink also has no clerical-review region — the 16 orders we abstain on, it is forced to guess about.

**"The data is too clean."** `--profile stress` degrades every narration (typos, heavy truncation, 3/4 of transfers lose their invoice number) and plants amount-collision decoys against unpaid invoices. Result: DCPR stays **100%**, ₹ wrongly chased stays **₹0**, and false-hold rises to 24% — the system pays for noise with *abstention, never with wrong chases*. That is the designed failure direction, and the stress ablation shows greedy fuzzy matching wrongly chasing ₹34 lakh on the same data.

**"Where's the AI?"** Confined and *measured*. `python -m ledger_daemon agent-eval` scores the proposal layer against ground truth on the exception list; with no model installed, the deterministic fallback's honest abstention already agrees with ground truth on 14/16 exceptions — the bar any local model (set `LEDGER_DAEMON_LLM=ollama`) has to beat before R7 lets a single proposal matter. LLMs are kept off the decision path because their confidence scores are poorly calibrated, which would break the conformal and cost-sensitive layers — that's an allocation argument, not a limitation.

**"You got one lucky seed."** `python -m ledger_daemon sweep` re-runs generate → calibrate → reconcile → gate → score on 20 worlds the thresholds were never tuned against, re-deriving q_hat per seed from that seed's own calibration batch so no seed can be rescued by a threshold chosen after seeing it:

| metric | min | median | max |
|---|---:|---:|---:|
| double-chase prevention | 100.0% | 100.0% | 100.0% |
| false-hold rate | 4.0% | 4.0% | 6.0% |
| verdict match rate | 96.8% | 98.7% | 99.4% |
| ₹ wrongly chased | ₹0.00 | ₹0.00 | ₹0.00 |

**20/20 seeds at 100% double-chase prevention and ₹0 wrongly chased.** What this does *not* show: every world comes from the same generator, so the sweep bounds sampling luck, not model risk — the authored-generator limitation below stands in full. The calibration and evaluation seeds are asserted disjoint by `assert_disjoint_seeds()`, so a refactor that collapses them fails loudly instead of quietly reporting an inflated match rate.

**"What happens when a new kind of settlement appears?"** It cannot silently inherit a default. `VERDICT_DISPOSITION` declares one disposition per verdict with no catch-all, `_assert_exhaustive()` raises `ImportError` at module load if a verdict is unmapped or disagrees with `CHASEABLE_VERDICTS`, and the dispatch ends in `typing.assert_never` so a static checker rejects an unhandled branch. Adding a way for money to arrive without deciding what collections does about it is a load-time error on the developer's machine, not a production incident. Watch it fire:

```bash
python -m ledger_daemon prove
```

It injects an unhandled verdict into the live enum and shows the policy engine refusing to load. This is the layer that turned the taxonomy from documentation into a constraint — and it is not hypothetical: both verified and merely possible TDS states were added through exactly this gate.

**"Hand-rolled string matching?"** The stdlib Jaro-Winkler is parity-tested against **rapidfuzz**'s C++ implementation (500 randomized cases, exact agreement — the test caught an observed divergence in Winkler's boost-threshold rule). rapidfuzz is used automatically when installed; the stdlib path remains the dependency-free contract.

## The chase list (one screen)

`python -m ledger_daemon ui` serves a single local page — stdlib `http.server`, inline CSS/JS, no CDN, fully offline:

- **SAFE TO CHASE** — proven unpaid, every gate passed; the executor acts on these.
- **BLOCKED** — customers a naive duner would have chased, wrongly, with the rupees protected and the exact rule that fired on every row. Orders where books and bank simply agree are counted in the footer, not shown — BLOCKED means *saves*, not routine agreement.
- **NEEDS YOU** — the honest abstentions, each resolvable in one click: *payment received* blocks the chase, *nothing arrived* releases it. Every click lands in the same append-only sqlite audit trail as the machine's decisions, idempotently — a resolution is evidence recorded, not a state silently mutated.

`--dir data/test-mode-raw` points the same screen at an imported/Test Mode batch. Imported/Test Mode records are not evidence for the published synthetic metrics and have no generated ground-truth score.

## Optional Razorpay Test Mode capability

`python -m ledger_daemon ingest` can optionally fetch Razorpay Test Mode API objects from `/v1/orders`, `/v1/payments`, `/v1/refunds`, `/v1/settlements`, and `/v1/settlements/recon/combined` when the operator supplies test keys (production-mode keys are refused by design). The recon feed links payments and refunds to settlements. This capability is not evidence from the published demo or metrics, and Test Mode objects do not move real money. It writes canonical batch CSVs so `reconcile` and `ui` can run on gateway records; those imported records have no ground truth score because they have no generated oracle.

`python -m ledger_daemon import-statement <file>` completes the triangle: it parses an HDFC or ICICI netbanking CSV export (auto-detected from the header, preamble lines skipped, amounts string-parsed straight to integer paise — the no-float contract holds at the ingestion boundary too) and replaces the batch's bank feed, UTRs recovered from the reference column or the narration itself. Unrecognised headers fail loudly instead of guessing; `--bank` forces a shape when detection is wrong.

## When the data shifts, the matcher loses its licence

The conformal threshold and the rupee-risk calibration are distribution-free, not distribution-proof: both hold only while the live stream is exchangeable with the batch they were fitted on. A drift monitor scores each live window against that batch on five integer signals — narration parse rate, a nonconformity share, amount mix, settlement lag, fee basis points — and an authority ladder decides what to do about it.

```
python -m ledger_daemon drift-demo --seed 42 --n 500 --out out/drift-demo

      window                   severity   rule         state
      unchanged                HEALTHY    A5_CLEAR     CALIBRATED
      settlement +7d           SEVERE     A1_WARN      WARNING
      settlement +10d          SEVERE     A2_DEGRADE   DEGRADED
      settlement +14d          SEVERE     A3_HALT      AUTOMATION_HALTED

      CALIBRATED -> WARNING -> DEGRADED -> AUTOMATION_HALTED
```

One severe window warns; two consecutive revoke probabilistic authority. Recovery needs consecutive healthy windows **and** a new calibration id — quiet alone leaves the threshold that was fitted before the shift. `R_DRIFT_HALT` then holds every probabilistic verdict, in both directions: a fuzzy "already paid" stops being a confident DENY and becomes a human's question. Exact UTR and settlement-id proofs are untouched, because they never depended on a fitted threshold.

The shift above is declared, not mined from a lucky seed — see D18 in DECISIONS.md. Every transition lands in the same append-only audit log as the rest: `python -m ledger_daemon audit AUTHORITY --db out/drift-demo/ledger.sqlite3`.

## Proofs you can check without trusting the engine

Every verdict is issued with a certificate: the source rows it consumed (bound by SHA-256), the signed integer-paise terms that must sum to zero, the rule ids, and the calibration and config identities it was decided under. `proof_hash` is the SHA-256 of the canonical JSON of all of it.

Each result proof/supporting records package detects changes to exported evidence but cannot prove source systems are truthful. It supports checking what was exported and how the controller calculated its result; it does not establish that an upstream gateway, bank, or books system reported the truth.

```
python -m ledger_daemon verify-proof out/proofs/ORD-1381.json --sources out/data/batch
{"errors": [], "order_id": "ORD-1381", "proof_hash": "198629be...", "status": "VALID"}
```

`verifier.py` imports no reconciliation, scoring or conformal code — it recomputes the claim from the source files and reports error codes. Tamper with one paise, one hash, one rule id, or the verdict, and it says so.

For a proof downloaded from the public sample, extract the sample-data ZIP and save the proof JSON beside its included `proof-manifest.json`; use that extracted directory as `--sources`.

`python -m ledger_daemon explain ORD-1381` draws the same certificate as a tree, and the workbench renders it inline behind each row. All three surfaces read the issued bundle rather than rebuilding it, so they print one proof hash. Nodes marked `[ok]` are recomputed on the spot (proof hash, amount equation); nodes marked `[claim]` are what `verify-proof` settles against the sources — a tree that painted those green would be theatre.

## MCP surface (8 tools)

```
reconcile(batch_path) -> {verdict_counts, exception_ids, orders_per_sec, open_cases}
explain(order_id)     -> evidence chain: pass used, source rows, weight waterfall
propose_recovery()    -> proposals for chaseable verdicts only
approve(proposal_id)  -> executes; idempotent; re-gated by policy; returns event_id
audit(order_id)       -> full append-only trail
report()              -> the evaluation block
cases(open_only)      -> exception cases: reason, state, version, full history
case_transition(case_id, expected_version, target, actor) -> one declared FSM hop
```

`propose_recovery()` and `approve()` are separate calls — no single tool call may move money. `case_transition()` demands the version the caller last read: an agent acting on a stale view is refused, not merged. Run with `python -m ledger_daemon mcp` (uses the official `mcp` SDK if installed, else a built-in stdio fallback). MCP client config:

```json
{"mcpServers": {"ledger-daemon": {"command": "python", "args": ["-m", "ledger_daemon", "mcp", "--root", "out"], "cwd": "<repo path>"}}}
```

## Optional Test Mode payment-link capability

The code exposes an explicit programmatic Test Mode adapter (`default_adapter(test_mode=True)`). An operator who supplies `RZP_TEST_KEY_ID` / `RZP_TEST_KEY_SECRET` can use it to create a Razorpay Test Mode payment link. That link is an external object/side effect; it does not move money. The published demo does not supply credentials, run this capability, or create a payment link; it uses the deterministic mock executor instead.

## Limitations

- Synthetic data. The messiness distribution is realistic (NEFT/IMPS/UPI/settlement narration formats, fee+GST arithmetic, split settlements, duplicate UTRs) and labels are written at injection time, but no real merchant data was used.
- The conformal guarantee is only as good as calibration/test exchangeability (see above).
- The recovery-value figures use a documented 0.6 recovery-rate assumption; the per-incident cost of wrongly chasing a paying customer is not publicly published anywhere, so the ₹800 action-cost constant is an assumption, labelled as such.
- Bank feeds are batch in reality; R2 tolerates that by holding, not chasing, until coverage exists.

Every number above is regenerated from source: `demo`, `ablate`, `sweep`, `crosscheck` and `agent-eval` reproduce the full evaluation — seed 42 for the headline, seeds 100-119 for the robustness table.
