# Methods

How each number is produced, in the order the data moves. Every metric
definition here is the one the judge harness implements; nothing is computed a
second way anywhere else.

## Source schemas

Three feeds enter the system, each validated at the boundary before anything
reads it.

| feed | identity | money fields | date field |
|---|---|---|---|
| merchant orders | `order_id` | `amount_paise` | `due_date` |
| gateway captures | `payment_id` | `amount_paise`, `fee_paise`, `tax_paise` | `captured_at` |
| bank statement | `txn_id` | `amount_paise`, `balance_after` | `value_date` |

`validate_row` rejects a row whose identity is missing, whose date or status is
invalid, whose money field is not an integer, or whose identity has already been
seen. A rejected row is never coerced and never repaired: it lands in an
append-only quarantine journal with an error code. Accepted rows are wrapped in
a `SourceEnvelope` carrying a raw hash and a normalized hash, both SHA-256 of
canonical JSON, so hashing is independent of dictionary order.

## Matching passes

Four passes, in order, each one only running where the previous left the order
unresolved.

1. **Exact UTR join.** The reference the bank and the gateway agree on.
2. **Net-of-fee amount within a T+0..T+3 date window.** Captures settle net of
   fee and GST; the window is the settlement lag, not a tolerance.
3. **Split-settlement aggregation.** Σ captures sharing a `settlement_id`
   against one bank credit.
4. **Scored narration match, only where the amount already agrees.** A name
   similarity may break a tie; it may never create one.

Passes 1–3 are **exact**: they consume a reference or an identity and depend on
no fitted parameter. Pass 4 is **probabilistic**, and is the only path drift or
the rupee-risk budget can revoke.

## The score

Pass 4 uses Fellegi-Sunter (1969) log-likelihood weights, not an arbitrary fuzzy
threshold. Per-field agreement contributes `log2(m/u)`; the sum is a
log-likelihood ratio converted to a probability. The weight waterfall *is* the
explanation — it is the arithmetic that produced the number, not a post-hoc
approximation of it.

`u`-probabilities are estimated from random record pairs, unsupervised, needing
no labels. `m`-probabilities come from the calibration batch.

Assignment is **component-wise optimal**, not greedy: two orders can never claim
the same bank credit. A greedy matcher manufactures exactly the false chase this
system exists to prevent.

## Abstention

The AMBIGUOUS threshold is derived, not tuned. Split conformal prediction at
α = 0.01 fits `q_hat` on a labelled calibration batch drawn from a seed asserted
disjoint from the evaluation seed. Cost-sensitive floors (Elkan 2001) then move
the bar with the invoice value: blocking a chase on a ₹5,00,000 invoice demands
more certainty than on a ₹4,200 one.

Ties, duplicate UTRs and scores below the floor all resolve to AMBIGUOUS, which
is an exception, never a guess.

## Money

Every monetary value is an integer number of paise. Floats are forbidden in
every monetary path and a test enforces it — including at the ingestion
boundary, where `"1,23,456.78"` is parsed by string manipulation straight to
`12345678` without touching a float.

## The proof and its verifier

Each verdict is issued with a certificate containing the source rows it consumed
(bound by SHA-256), signed integer-paise `AmountTerm`s that must sum to zero,
the rule ids that fired, and the configuration and calibration identities in
force. `proof_hash` is the SHA-256 of the canonical JSON of everything except
the hash field.

`verifier.py` imports no reconciliation, scoring or conformal code. It
recomputes the claim from the source files in a fixed order — schema and
version, proof hash, source hashes, unique source consumption, the amount
equation, verdict invariants, the rule allowlist, configuration and calibration
identity — and returns every error code it found rather than the first.

## Risk and drift

The rupee-risk controller searches unique score thresholds descending, computing
wrongly auto-resolved paise over all auto-resolved paise, plus a one-sided
Hoeffding-shaped exposure allowance whose effective sample count falls when a few
invoices dominate exposure. Coverage is maximised only within the budget. If no
threshold meets the budget, authority is withheld and `R_RISK_BUDGET` holds
probabilistic evidence.

The drift monitor scores each live window against the calibration batch on five
integer signals — narration parse rate, a nonconformity share, amount mix,
settlement lag, fee basis points — and refuses windows too small to
characterise. One severe window warns; two consecutive revoke probabilistic
authority. Recovery requires consecutive healthy windows **and** a new
calibration id.

## Metric definitions

- **Double-chase prevention rate (DCPR).** Of the orders a naive schedule-driven
  agent would have chased that had in fact already been paid, the share this
  system blocked. Denominator is the already-paid set, not all orders.
- **False-hold rate.** Of the genuinely unpaid orders, the share this system
  held or blocked. Always printed beside DCPR: without it, DCPR is trivially
  gamed by blocking everything.
- **Verdict match rate.** Share of orders whose issued verdict equals the
  generated ground-truth verdict.
- **Rupees wrongly chased.** Sum of the invoice values of orders actioned whose
  ground truth says they were not chaseable. The invariant the judge exits
  nonzero on.
- **Terminal bucket balance.** Every source row offered must end reconciled, an
  exception, or quarantined. Asserted per profile, not in aggregate.
- **Safe coverage (evidence reader).** Share of narrations where the reader
  produced usable evidence and invented nothing.

## The gate on models

The evidence reader is benchmarked on exact character spans: right entity, wrong
offset scores nothing. A model adapter is enabled only if it raises held-out
safe coverage without raising wrong-rupee loss — a tie loses, because the
incumbent regex needs no model, no socket and no second failure mode.

## Reproducing all of it

```
python -m ledger_daemon judge --seed 42 --n 500 --out out/judge
```

Deterministic: the same seed reproduces the same fingerprint. The run writes
`summary.json`, `cases.jsonl`, `attacks.json`, `latency.json`,
`proof-manifest.json` and `claims.md`, and exits nonzero if any hard invariant
fails — while still writing every artifact.
