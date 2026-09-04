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
   ▼
policy.evaluate(order, verdict, action, history)     R1..R7, default DENY
   │ ALLOW only
   ▼
Executor.execute()        sha256 event_id PK, WAL SQLite, append-only audit
   ├─ CREATE_PAYMENT_LINK  (mock adapter | live Razorpay test-mode)
   └─ DRAFT_REMINDER       (stages text; never sends)
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
| `mcp_server.py` | 6 MCP tools (FastMCP or stdio fallback) |
| `cli.py` | demo / generate / reconcile / explain / audit / mcp |

## Why headless, why local

The consumer is an AI agent calling MCP tools over stdio, and a
merchant analyst running one CLI command. All data stays on local disk
(SQLite + CSV); nothing is transmitted; the only optional network call is one
Razorpay test-mode payment link. That is the correct trust posture for a
component whose entire purpose is to *stop* an autonomous system from acting
on unproven beliefs about money.
