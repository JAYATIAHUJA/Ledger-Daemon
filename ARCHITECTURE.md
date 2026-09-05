# ARCHITECTURE.md

## Data flow

```
generate --seed N          calibration batch (seed+1000)
   │                            │
   ▼                            ▼
merchant_orders.csv        calibrate(): fit u's, derive conformal q_hat
gateway_captures.csv            │
bank_statement.csv              │
ground_truth.csv                │
   │                            │
   ▼                            ▼
reconcile(orders, captures, bank, q_hat, fs_model)
   ├─ pass 1: exact UTR join (settlement groups ↔ bank credits)
   ├─ pass 3: settlement-id aggregation, Σ(net) must equal the credit exactly
   ├─ pass 2: net amount + value-date window T+0..T+3 (fallback join)
   ├─ pass 4: Fellegi-Sunter scoring, amount-gated candidates only
   │     ├─ component-wise optimal assignment (ties → AMBIGUOUS)
   │     ├─ duplicate-UTR check (→ AMBIGUOUS)
   │     ├─ conformal decision at derived q_hat (→ MATCH/NON_MATCH/AMBIGUOUS)
   │     └─ cost-sensitive floor by invoice value
   └─ flat verdict decision table → exactly one of 11 verdicts + evidence_refs
   │
   ├─ certificates.write_proof_bundle() → one signed proof per order
   │     ├─ verify_certificate(): source hashes, amount equation, rule allowlist
   │     └─ certificate_to_tree(): the same proof, drawn — explain and the UI
   │        read the issued bundle, so all three print one proof_hash
   │
   ▼
DriftMonitor.observe(window)      -> HEALTHY | WARNING | SEVERE | UNDERSIZED
   └─ AuthorityController.apply()  CALIBRATED -> WARNING -> DEGRADED -> HALTED
        ├─ revoke on 2 consecutive severe windows; halt on 3
        ├─ recover only on N healthy windows AND a new calibration id
        └─ every transition appended to the same audit log
   │
   ▼
policy.evaluate(order, verdict, action, history)     R0, R1..R7, default DENY
   ├─ R0 R_DRIFT_HALT: authority revoked -> every probabilistic verdict HOLDs
   │                   (exact proofs are never revoked)
   │ ALLOW only
   ▼
Executor.execute()        sha256 event_id PK, WAL SQLite, append-only audit
   ├─ CREATE_PAYMENT_LINK  (mock adapter | live Razorpay test-mode)
   └─ DRAFT_REMINDER       (stages text; never sends)

every HOLD / ESCALATE / AMBIGUOUS / quarantine / failed proof
   ▼
CaseStore.open_case()     sha256(order|reason) PK -> idempotent; case_events append-only
   └─ transition(case_id, expected_version, target, actor)
        ├─ BEGIN IMMEDIATE: version check + event insert + state write in one txn
        ├─ target not in LEGAL_TRANSITIONS[state] -> IllegalTransition, nothing written
        └─ stale expected_version -> VersionConflict (UI 409, MCP error_type)

verified human resolution + bounded rule suggestion
   │
   ▼
RuleStore: PROPOSED -> authenticated replay -> REPLAY_PASSED
   ├─ any new wrong paise/verdict, invalid proof, or coverage regression -> REJECTED
   ├─ approval before replay -> rejected
   └─ separate reviewer capability -> APPROVED -> ACTIVE
        └─ store-issued activation receipt binds exact history into copied ReconConfig
```

## Module map

| Module | Responsibility |
|---|---|
| `money.py` | integer-paise arithmetic; raises on floats |
| `models.py` | Verdict taxonomy, record dataclasses |
| `datagen.py` | seeded generator, labels at injection time |
| `similarity.py` | Jaro-Winkler + name-vs-narration windows |
| `narration.py` | regex extraction from Indian bank narrations |
| `fs.py` | Fellegi-Sunter weights, u estimation, waterfall |
| `conformal.py` | split conformal threshold + 3-way decision |
| `recon.py` | 4 passes, assignment, verdict table, calibration |
| `policy.py` | R1–R7 gate, default DENY |
| `executor.py` | idempotent actions, append-only audit |
| `agent.py` | sandboxed proposals, deterministic fallback |
| `evaluate.py` | B0/B1/B2 baselines, DCPR, false-hold, report |
| `certificates.py` | proof certificates: canonical JSON, signed integer terms, proof hash |
| `verifier.py` | independent verification; imports no recon/fs/conformal |
| `proof_tree.py` | the certificate drawn for a human; certificate fields only |
| `drift.py` | five integer signals; scores a live window against the calibration batch |
| `authority.py` | the kill switch: hysteresis ladder, revoke fast, recover slow |
| `cases.py` | exception FSM: idempotent cases, declared edges, optimistic concurrency |
| `finance_events.py` | immutable refund/dispute/adjustment/settlement/ledger event schemas |
| `identities.py` | exact signed-paise settlement and invoice accounting identities |
| `risk_control.py` | rupee-weighted authorization; a threshold with a loss budget |
| `rules.py` | learned rules as versioned data; no `eval`, no code path |
| `replay.py` | zero-regression replay gate before any rule activates |
| `rule_demo.py` | executable rule lifecycle evidence: bypass rejection, replay, approval, activation |
| `evidence_reader.py` | typed span extraction; the only vocabulary a model gets |
| `model_adapters.py` | optional challenger readers, default off, abstain on failure |
| `model_benchmark.py` | labelled character-span benchmark and the adapter gate |
| `faults.py` | the declared fault plan and the oracle each fault is graded against |
| `judge.py` | eight profiles, eleven graded attacks, six hard invariants, artifacts |
| `signoff.py` | the controller decision table: sign, sign with caveats, do not sign |
| `panels.py` | what the operations screen knows, gathered once from the run |
| `ui.py` | the operations screen: close, chase list, proofs, cases, risk, audit |
| `mcp_server.py` | 8 MCP tools (FastMCP or stdio fallback) |
| `cli.py` | judge / demo / learn-rule-demo / ui / reconcile / explain / verify-proof / audit / mcp |

## The three authorities, and what can revoke each

| authority | what it rests on | revoked by |
|---|---|---|
| exact | a shared reference: UTR, settlement id, an accounting identity | nothing — it depends on no fitted parameter |
| probabilistic | a Fellegi-Sunter score above a conformal threshold | drift (`R_DRIFT_HALT`) or an unmet rupee budget (`R_RISK_BUDGET`) |
| human | an authenticated analyst resolving a versioned case | a stale version, which is refused rather than merged |

A model holds none of the three. It may return typed character spans of text it
was handed, and the schema it returns them in has no field for a rupee figure, a
verdict, an approval or a rule id — so the boundary is structural rather than
instructional.

## The evidence surface

Everything the system claims is regenerated by one command
(`python -m ledger_daemon judge`) into artifacts the documentation quotes and
nothing else may: `summary.json`, `cases.jsonl`, `attacks.json`, `latency.json`,
`proof-manifest.json`, `claims.md`. A run that breaks a hard invariant exits
nonzero and writes them anyway.

## Why headless, why local

The consumer is an AI agent calling MCP tools over stdio, and a
merchant analyst running one CLI command. All data stays on local disk
(SQLite + CSV); nothing is transmitted; the only optional network call is one
Razorpay test-mode payment link. That is the correct trust posture for a
component whose entire purpose is to *stop* an autonomous system from acting
on unproven beliefs about money.
