# Claims

Every number this project states appears here with the run it came from. Nothing
in the README, the architecture notes or the interface may quote a figure that is
not in this table.

**Source run:** `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge`
**Fingerprint:** `45583b83cbff6bd0` · 10,801 source rows across eight profiles ·
26.9 s wall, offline, no keys.

The fingerprint covers every counted outcome and excludes wall time, so the same
seed on a different machine reproduces it exactly. If your run prints a different
fingerprint, this table is stale and your run is right.

## Headline

| claim | value | dataset class | profile | seed | n | command | artifact | limitation |
|---|---|---|---|---|---|---|---|---|
| double-chase prevention | 100.0% | synthetic | clean | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | authored generator; labels written at injection time |
| false-hold rate | 4.0% | synthetic | clean | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | the price paid for the line above; never read one without the other |
| rupees wrongly chased | 0 paise | synthetic | clean | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | measured against generated truth, not a real ledger |
| verdict match rate | 96.8% | synthetic | clean | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | one generator; not evidence about real narration quality |

## Every profile

| claim | value | dataset class | profile | seed | n | command | artifact | limitation |
|---|---|---|---|---|---|---|---|---|
| double-chase prevention | 100.0% | synthetic | realistic (rows out of order) | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | arrival order varied, content unchanged |
| double-chase prevention | 100.0% | synthetic | stress | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | false-hold rises to 24.0% — noise is paid for in abstention |
| double-chase prevention | 100.0% | synthetic | distribution-shift | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | match rate falls to 36.4% because automation halts by design |
| double-chase prevention | 100.0% | synthetic | adversarial | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | attacks.json | seven declared faults; oracles authored with them |
| double-chase prevention | 100.0% | synthetic | source-incomplete | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | a dropped credit becomes an exception, never a chase |
| double-chase prevention | 100.0% | synthetic | high-collision | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | duplicate delivery on degraded narrations |
| double-chase prevention | 100.0% | synthetic | concurrent | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | concurrency probed in-process, not across machines |
| rupees wrongly chased | 0 paise | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | the invariant the run exits nonzero on |

## Safety and integrity

| claim | value | dataset class | profile | seed | n | command | artifact | limitation |
|---|---|---|---|---|---|---|---|---|
| attacks meeting their declared oracle | 11 of 11 | synthetic | adversarial, source-incomplete, high-collision, realistic | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | attacks.json | oracles are authored alongside the faults they grade |
| certificates independently verified | 200 | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | proof-manifest.json | sampled per profile; the clean profile writes the full bundle |
| certificates rejected | 0 | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | a tampered certificate is rejected separately, as an attack |
| duplicate side effects | 0 | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | in-process threads; not a distributed-systems claim |
| source rows quarantined | 3 | synthetic | adversarial (2), high-collision (1) | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | injected malformed and duplicate rows, failing closed as required |
| rows placed in exactly one terminal bucket | 10801 of 10801 | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | reconciled, exception, or quarantined — asserted per profile |

## Cost

| claim | value | dataset class | profile | seed | n | command | artifact | limitation |
|---|---|---|---|---|---|---|---|---|
| end-to-end per profile, p50 | 3.44 s | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | latency.json | eight samples; a p99 over eight points is not a distribution |
| reconciliation stage, p50 | 25.9 ms | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | latency.json | 500 orders against three feeds, single process |
| policy gate, p50 | 0.7 ms | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | latency.json | rules only; excludes the executor call |
| whole judge run | 26.9 s | synthetic | all eight | 42 | 500 | `python -m ledger_daemon judge --seed 42 --n 500 --out out/judge` | summary.json | one laptop, one run; not a benchmark |

## What no row here claims

That the generator resembles a real merchant's feeds. Every number above is
evidence that the implementation does what it says on data whose truth is known.
It is not evidence about narration quality at a real bank, about model risk, or
about what happens the first time a real ledger disagrees with this one. See
[LIMITATIONS.md](LIMITATIONS.md) and [SIMULATED_VS_REAL.md](SIMULATED_VS_REAL.md).
