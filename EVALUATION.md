# Evaluation

One command produces everything below.

```
python -m ledger_daemon judge --seed 42 --n 500 --out out/judge
```

Offline, no keys, 26.9 s, exit 0. Fingerprint `45583b83cbff6bd0`. Every figure
here is copied from that run's artifacts and appears in [CLAIMS.md](CLAIMS.md)
with its provenance; the data class is **synthetic** throughout, with labels
written at injection time by the generator.

## Eight profiles

| profile | what it changes | DCPR | false-hold | match rate | quarantined | authority |
|---|---|---:|---:|---:|---:|---|
| clean | nothing | 100.0% | 4.0% | 96.8% | 0 | CALIBRATED |
| realistic | rows arrive out of value-date order | 100.0% | 4.0% | 96.8% | 0 | CALIBRATED |
| stress | degraded narrations, collision decoys | 100.0% | 24.0% | 94.8% | 0 | CALIBRATED |
| distribution-shift | settlement lag +14 days | 100.0% | 0.0% | 36.4% | 0 | AUTOMATION_HALTED |
| adversarial | seven declared faults injected | 100.0% | 4.0% | 96.6% | 2 | CALIBRATED |
| source-incomplete | a credit dropped, a narration truncated | 100.0% | 4.0% | 96.8% | 0 | CALIBRATED |
| high-collision | duplicate delivery on degraded narrations | 100.0% | 24.0% | 94.8% | 1 | CALIBRATED |
| concurrent | two writers racing one action | 100.0% | 4.0% | 96.8% | 0 | CALIBRATED |

**Rupees wrongly chased: 0 paise, in all eight.** That is the invariant the run
exits nonzero on.

## Read three of these rows honestly

**stress** costs 20 points of false-hold. Degrade every narration and the system
pays for the noise in abstention — 24% of genuinely unpaid orders get held for a
human rather than chased. That is the designed direction: it never converts noise
into a wrong chase, and it never pretends the noise was not there.

**distribution-shift** costs 60 points of match rate. When the settlement lag
moves 14 days, the drift monitor revokes probabilistic authority after two severe
windows and the fuzzy matcher stops running. Match rate collapses to 36.4%
because most orders now resolve as exceptions rather than verdicts. DCPR stays
100% and false-hold falls to 0% for the same reason — nothing is being decided
that a fitted threshold no longer supports. A system that kept its 96.8% here
would be one that had not noticed.

**adversarial** quarantines 2 rows and still matches 96.6%. The duplicate
delivery and the malformed amount fail closed; the prompt injection becomes an
inert typed span; the tampered certificate is rejected. None of it moved a
rupee.

## Attacks

Eleven injected faults, each graded against an oracle declared before the run.
**11 of 11 met their oracle.**

| fault | stage | oracle | result |
|---|---|---|---|
| reorder | ingestion | verdicts identical under a rotated feed | pass |
| duplicate | ingestion | row fails closed into quarantine | pass |
| drop | ingestion | order may become an exception, never a chase | pass |
| truncate | ingestion | order may become an exception, never a chase | pass |
| malform_json | ingestion | row fails closed into quarantine | pass |
| prompt_injection | ingestion | narration grants no verdict, rule or action | pass |
| stale_version | case transition | stale writer refused, not merged | pass |
| timeout | executor | failed call writes nothing; retry leaves one row | pass |
| crash_after_write | executor | replay after restart leaves exactly one row | pass |
| hash_tamper | proof | tampered certificate fails verification | pass |

Surviving an attack is not passing it. Each fault states in advance what the
system is required to do, and the run is graded against that sentence — an
attack with no oracle proves nothing, because whatever happened becomes the
expected result.

## Integrity

| check | result |
|---|---|
| certificates independently verified | 200, 0 rejected |
| duplicate side effects | 0 |
| source rows in exactly one terminal bucket | 10,801 of 10,801 |
| hard invariants held | 6 of 6 |

The six invariants are named in the run output: `every_row_bucketed`,
`zero_wrong_rupees`, `no_duplicate_side_effects`, `proofs_verify`,
`attacks_meet_oracles`, `policy_gate_holds`.

## Cost

| stage | p50 | p95 | samples |
|---|---:|---:|---:|
| ingest (validate three feeds) | 83 ms | 106 ms | 8 |
| reconcile (four passes, 500 orders) | 25.9 ms | 42.3 ms | 8 |
| policy gate | 0.7 ms | 1.2 ms | 8 |
| proof build | 253 ms | 1,149 ms | 8 |
| verify (per sampled certificate, summed) | 950 ms | 1,228 ms | 8 |
| end to end, per profile | 3.44 s | 3.80 s | 8 |

Eight samples per stage, one per profile. A p99 over eight points is not a
distribution and the artifact reports the sample count beside every percentile
so it is not read as one.

## Downstream Control Demonstration: Why Correct Reconciliation Matters

Recovery appears in this project for one reason: to make the cost of getting
reconciliation wrong legible. On the clean profile, 100 already-paid orders would
have been contacted by a schedule-driven chase. All 100 were blocked, with the
rule that fired recorded against each. The money beside them is not revenue
recovered — it is the conversation with a paying customer that did not have to
happen.

It is not a second product and not a second track.

## Other commands that produce evidence

| command | what it shows |
|---|---|
| `python -m ledger_daemon demo --seed 42 --n 500` | the full pipeline end to end with the evaluation block |
| `python -m ledger_daemon sweep` | 20 unseen seeds, thresholds re-derived per seed |
| `python -m ledger_daemon ablate` | each component earning its place, plus the risk-coverage curve |
| `python -m ledger_daemon drift-demo` | the authority ladder walking to AUTOMATION_HALTED |
| `python -m ledger_daemon bench-readers` | the evidence reader scored on labelled character spans |
| `python -m ledger_daemon crosscheck` | an independent Fellegi-Sunter engine on the same universe |
| `python -m ledger_daemon prove` | the exhaustiveness guard refusing to load on an unhandled verdict |
| `python -m ledger_daemon verify-proof <cert> --sources <dir>` | one certificate checked without the engine |

## What this evaluation is not

Evidence about the implementation, on data whose truth is known. Not evidence
about how real merchant feeds behave, not evidence about model risk, and not a
claim that the generator resembles production. See
[LIMITATIONS.md](LIMITATIONS.md).
