# Synthetic, schema-derived, Test Mode, merchant-provided

Four data classes run through this system. They are not interchangeable, and a
number computed on one says nothing about another. Every claim in
[CLAIMS.md](CLAIMS.md) names the class it came from.

| class | what it is | where it comes from | has ground truth? | may produce a metric? |
|---|---|---|---|---|
| **synthetic** | a generated world with labels written at injection time | `ledger_daemon/datagen.py` | yes, by construction | yes — every number in CLAIMS.md |
| **schema-derived** | rows shaped like a real export, with values invented | benchmark fixtures, test fixtures | only where hand-labelled | only for regression floors |
| **Test Mode API** | sandbox objects; no production money | Razorpay Test Mode via `ingest` | no | no |
| **merchant-provided** | a merchant's statement export | `import-statement` on an HDFC/ICICI CSV | no | no |

## synthetic

`generate(seed, n, dir, profile)` writes four CSVs and records what it did as it
did it. Because the label is written at injection time rather than inferred
afterwards, verdict accuracy, double-chase prevention and wrongly-chased rupees
are all exactly computable.

Two profiles ship: `clean` follows the messiness distribution in the
specification; `stress` degrades every narration (typos, heavy truncation, most
transfers losing their invoice number) and plants amount-collision decoys
against unpaid invoices.

The generator is deterministic: the same seed produces byte-identical output.
Calibration and evaluation seeds are asserted disjoint by
`assert_disjoint_seeds()`, so a refactor that collapses them fails loudly
instead of quietly reporting an inflated match rate.

## schema-derived

The evidence-reader benchmark (`model_benchmark.py`) is sixteen narrations
written to look like real bank text — truncated names, Devanagari
counterparties, OCR substitutions, distractor numbers, instruction text pasted
into a narration — with character-span labels. Real shape, invented content. It
is a regression gate on the extraction boundary, not a sample of production
traffic.

## Test Mode API

`python -m ledger_daemon ingest` can read `/v1/orders`, `/v1/payments`,
`/v1/refunds`, `/v1/settlements`, and the combined settlement-reconciliation
feed from Razorpay **Test Mode**. Live-mode keys are refused by design. These are
sandbox API objects and use no production money. **Processed settlements stand
in for the bank feed** until a statement export replaces them, so the third
source is not independent of the second.

The repository currently publishes no credentialed Test Mode run. Its website,
accuracy figures, and rupee figures all come from the synthetic class above.

No `ground_truth.csv` is written, because these Test Mode objects have no labelled oracle. `reconcile`
and the operations screen run unchanged; the evaluation panel says plainly that
nothing was scored.

## merchant-provided

`python -m ledger_daemon import-statement <file>` parses a real HDFC or ICICI
netbanking CSV export into the canonical bank feed: header auto-detected,
preamble lines skipped, amounts parsed as strings straight to integer paise, and
UTRs recovered from the reference column or the narration itself. An
unrecognised header fails loudly rather than guessing; `--bank` forces a shape.

These export layouts are written to the commonly seen shapes, not to a published
specification — banks change them without notice.

Running merchant-provided data through the system produces verdicts and proofs you can
inspect. It does not produce an accuracy figure, and no accuracy figure in this
repository was computed from it.

## The rule

A metric may be published only for the **synthetic** class, and only with its
profile, seed, sample size and command attached. Anything measured on
schema-derived data is labelled a regression floor. Nothing is measured on
Test Mode or merchant-provided data at all.
