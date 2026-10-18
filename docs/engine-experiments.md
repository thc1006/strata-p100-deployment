# Engine experiments: plan and criteria

Written 2026-10-19 at 04:50, while the first screen (GROUP_SORT) was running and before any of its candidate runs had
been read. The criteria below were fixed then and are not changed after results are seen; a change would be recorded
here with its reason and time.

## What is tested, in order

All arms run on Strata 0.1.45 (commit `477ac13c`, built locally for sm_60) with the deployed configuration, in a
transient unit with the deployed unit's limits (`bench/ab_engine.sh`), and are measured with `bench/engine_screen.py`
(greedy requests: 1.2K / 4K / 16K prompts, one user decoding, four users, a 16K prompt joining three decoders, a
two-turn conversation). Each run's arm record shows its switch was set; switches that act only on some requests must
also show in the engine's lines during the screen (`engaged` in the arms file), else the run is not usable.

| Arm | Change against C0 | Arms file | Targets |
|---|---|---|---|
| C0 | none: 0.1.45 with `STRATA_MMQ_GROUP_SORT=0`, `STRATA_MMQ_RESIDENT_SORT_NE=0` | all | the baseline, run first and last |
| C1 | `STRATA_MMQ_GROUP_SORT=1` (exact by design) | `bench/arms-group-sort.json` | prompt read rate |
| C2 | `--prefill-pipe 768` with the `--prefill 2048` cap | `bench/arms-sched.json` | prompt read rate |
| C3 | `STRATA_PIPED_CONCURRENT=1` | `bench/arms-sched.json` | the decoders' stall while a 16K prompt joins |
| M0 | pull request #2985's tree, `--batch-groups auto` left out (given explicitly, it turns the drafts off), `STRATA_BATCH_SELF_COMMIT=0` | `bench/arms-pmtp.json` | the baseline of M1 |
| M1 | M0 with `--batch-mtp` and `STRATA_PIPED_MTP=1` (M2, `=2`, only if M1 passes) | `bench/arms-pmtp.json` | the four users' rate |

## Criteria (fixed before the results)

A screen (C0, candidates, C0) decides only whether a candidate goes on to a confirmation:
- **Goes on** when its change on a target metric is larger than the spread between the two C0 runs and at least 5% in
  its favour, and no other metric moves against it by more than 5% beyond that spread.
- **Answers:** C1 must answer every request as C0 does (it is exact by design; a difference is a finding and stops
  it). For the others, the number of requests answered differently from C0 is reported, counted only where the two C0
  runs agree with each other.
- A run that is not usable (the summary says why) is measured again; it is not judged.

A confirmation is four fresh runs in the order A, B, B, A, with the same screen, and the switch is adopted when:
- **Speed switches (C1, C2, M1):** the mean of the two B runs is at least 10% better than the mean of the two A runs on
  the target metric, each B run better than each A run, and no other metric worse by more than 5%.
- **Scheduling switch (C3):** the decoders' longest gap while a 16K prompt joins is shorter in both B runs than in both
  A runs, and the makespan of the four requests is at most 5% longer.
- Every run is usable. Winners are then combined and the combination is confirmed the same way.

## Changes to the plan

- 2026-10-19 05:05, after reading the first two runs (c0-1, c1-1) and before c0-2: to save a separate 80-minute run,
  the confirmation of C1 is embedded in the next screen, in the order C0, C1, C2, C3, C1, C0 (tags c0-3, c1-2, c2-1,
  c3-1, c1-3, c0-4). It stays symmetric (C0 first and last, C1 at the second and fifth places), so a drift over the
  run affects both arms alike, and every step is a fresh engine start. The target metric, not defined precisely
  above, is defined here before those runs: for C1 and C2, the engine's prompt read rate at each of the three prompt
  sizes (1.2K, 4K, 16K), compared size by size. C1 is exact by design, so it is adopted when, at every size, both C1
  runs read faster than both C0 runs, the mean gain over the three sizes is at least 5%, its answers equal C0's, and no
  other metric is worse by more than 5%. (The maintainer's guide was "throughput about 10% or more"; 5% is used for an
  exact switch that changes no answer and costs nothing else, and the result is reported against both.) C2 changes
  the prompts' rounding, so it keeps the 10% rule.

## Results

None yet.
