# Limitations

What this project has not shown, stated plainly, in the order that should worry a
reader most.

## 1. There is no real merchant data anywhere in it

Every number in [CLAIMS.md](CLAIMS.md) comes from a synthetic world produced by
`ledger_daemon/datagen.py`, and its ground-truth labels are
**written at injection time** — the generator records what it did at the moment
it did it, so scoring is exact by construction. That is what makes the metrics computable, and
it is also what makes them limited: the same hand wrote the messiness and the
matcher that handles it.

There is no public three-source financial ground truth (gateway × bank ×
merchant books) to test against. Building one would require a merchant's actual
statement, their gateway export, and their books, with permission to publish the
join. Until that exists, this system is unfalsified rather than validated.

The messiness distribution is modelled on real shapes — NEFT/IMPS/UPI narration
formats, fee-plus-GST arithmetic, split settlements, duplicate UTRs, T+0..T+3
settlement lag, statutory TDS deduction — but "modelled on" is not "sampled
from".

## 2. The conformal guarantee holds only under exchangeability

The abstention threshold `q_hat` is distribution-free but not
distribution-proof: its coverage guarantee assumes the live stream is
exchangeable with the calibration batch it was fitted on. On synthetic data from
one generator that assumption holds trivially, which is precisely why it proves
little.

The drift monitor exists because in production it will not hold. What the
monitor gives is a declared, auditable reaction to that — one severe window
warns, two consecutive revoke probabilistic authority — not a guarantee that the
threshold was right until the moment it fired. In the `distribution-shift`
profile the match rate falls from 96.8% to 36.4% precisely because automation
halts; that is the designed failure direction, and it is a cost, not a feature.

## 3. The adversarial simulator grades itself

The faults in `faults.py` and the oracles they are graded against were authored
together, by this project. Eleven of eleven attacks meeting their oracle means
the system handles the failures its author thought of. It says nothing about the
failure nobody wrote down.

The same applies to the evidence-reader benchmark: sixteen labelled fixtures,
written by the same hand as the extractor they score. Passing is a floor.

## 4. Optional models are not authorities, and have never beaten the baseline

The evidence reader ships as a regex extractor. A model adapter can be enabled,
but it is off by default and stays off until it raises held-out safe coverage
without raising wrong-rupee loss — a gate no challenger has yet been run
through, because none is installed. The measured contribution of the LLM layer
today is zero: with no model present, the deterministic fallback's honest
abstention already agrees with ground truth on most of the exception list.

That is an allocation argument, not a claim of superiority. LLM confidence
scores are poorly calibrated, which would break the conformal and cost-sensitive
layers that the safety numbers depend on.

## 5. Live source coverage varies, and absence of evidence is not evidence of absence

Bank feeds are batch in reality. When the statement window ends before an
order's `due_date + 3`, policy rule R2 **holds** rather than chases — which is
why the false-hold rate is 4.0% and not lower. Two of the fifty unpaid orders in
the clean profile are held for exactly this reason.

Real-data ingestion is partial by construction: `ingest` pulls Razorpay
**test-mode** orders and payments, and processed settlements stand in for the
bank feed until a statement export replaces them. No ground truth is written for
real data, so no accuracy number can be computed from it. See
[SIMULATED_VS_REAL.md](SIMULATED_VS_REAL.md).

## 6. Scope of the concurrency and durability claims

"Zero duplicate side effects" is enforced by a SQLite primary key
(`sha256(order|action|attempt)`) and probed with in-process threads. It is not a
distributed-systems claim: nothing here has been tested across machines, across
processes contending on a network filesystem, or under a crash injected between
the adapter call and the commit at the operating-system level. The
`crash_after_write` fault simulates a restarted process against the same
database, which is weaker than pulling the power.

## 7. The economic figures rest on two assumptions

The recovery-value arithmetic uses a documented 0.6 recovery-rate assumption and
an ₹800 per-incident cost for wrongly contacting a paying customer. The second
number is not published anywhere public; it is an assumption, labelled as one
wherever it appears, and no safety claim depends on it.

## 8. This is not an audit opinion

The controller signoff in the operations screen is an operational gate on
whether a batch is fit to act on. It is not a statutory audit opinion, it is not
prepared under any accounting standard, and it does not substitute for a
chartered accountant reviewing the books.
