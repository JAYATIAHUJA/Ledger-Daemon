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
   └─ flat verdict decision table → exactly one of 9 verdicts + evidence_refs
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
| `mcp_server.py` | 8 MCP tools (FastMCP or stdio fallback) |
| `cli.py` | demo / generate / reconcile / explain / audit / mcp |

## Why headless, why local

The consumer is an AI agent calling MCP tools over stdio, and a
merchant analyst running one CLI command. All data stays on local disk
(SQLite + CSV); nothing is transmitted; the only optional network call is one
Razorpay test-mode payment link. That is the correct trust posture for a
component whose entire purpose is to *stop* an autonomous system from acting
on unproven beliefs about money.
