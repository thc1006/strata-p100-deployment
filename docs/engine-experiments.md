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

- 2026-10-19 05:05, after reading the first two runs (c0-1, c1-1) and before c0-2: to save a separate 80-minute run
  (a confirmation's four runs, about 19 minutes each in `results/ab_engine-group-sort-20261019.log`),
  the confirmation of C1 is embedded in the next screen, in the order C0, C1, C2, C3, C1, C0 (tags c0-3, c1-2, c2-1,
  c3-1, c1-3, c0-4). It stays symmetric (C0 first and last, C1 at the second and fifth places), so a drift over the
  run affects both arms alike, and every step is a fresh engine start. The target metric, not defined precisely
  above, is defined here before those runs: for C1 and C2, the engine's prompt read rate at each of the three prompt
  sizes (1.2K, 4K, 16K), compared size by size. C1 is exact by design, so it is adopted when, at every size, both C1
  runs read faster than both C0 runs, the mean gain over the three sizes is at least 5%, its answers equal C0's, and no
  other metric is worse by more than 5%. (The maintainer's guide was "throughput about 10% or more"; 5% is used for an
  exact switch that changes no answer and costs nothing else, and the result is reported against both.) C2 changes
  the prompts' rounding, so it keeps the 10% rule.
- 2026-10-19 08:00, after screen 2: the maintainer decided to adopt the switches that work well, C1 and C3. The two
  together are confirmed against C0 in the order K0, K1, K1, K0 (`bench/arms-combo.json`), and the confirmed
  combination is then deployed.
- 2026-10-19 08:27, before the combination's runs: K1 (C1 and C3) is confirmed against K0 (C0) when every run
  is usable and, in each K1 run against each K0 run:
  - every prompt size is read faster, with a mean gain over the three sizes of at least 5% (C1's rule);
  - the decoders' longest gap while a 16K prompt joins is shorter, and the four requests' makespan at most 5% longer
    (C3's rule);
  - the requests that run alone (the prefill, decode and turns phases) get K0's answers, as both switches left them
    in screen 2. A difference stops the deployment until it is understood.

  The other metrics are reported against K0, among them the joining prompt's TTFT and the four users' rate, which C3
  alone made 11.0% and 5.2% worse. The runs use the phases prefill, decode, four, decode4, eval4, admit, admit28 and
  turns, and screen version 2026-10-19c. Its eval4 asks harder questions, so that the thinking runs to the
  1536-token cap; the first questions ended after 98 to 874 tokens (m0-1 and m1-1 in `results/engine-screen.jsonl`).
- 2026-10-19 10:17, the maintainer: results of a script with a defect are not used. The combination's first run
  (k0-1, k1-1, k1-2; `results/ab_engine-combo-20261019.log`) is therefore set aside. In it the driver failed to read
  K1's `must_not`, which K1 does not have, and took the missing field for no check; it would have taken a missing
  `must` or `env` the same way. K1's `env` and `must` checks did run and pass. k0-2 was interrupted while its engine
  loaded, which showed a second defect: the restore waited 60 s for a reply that could not exist, printing a traceback
  at each poll. Both are fixed and tested (`tests/ab_engine_stubs.sh`, cases 26 and 27), and the confirmation runs
  again as k0-3, k1-3, k1-4, k0-4, with the same arms, screen and criteria.
- 2026-10-20 10:45: the driver read each step's journal lines, the test unit's and the kernel's, with
  `journalctl --after-cursor` and a filter, from a cursor at the journal's last entry. With a filter, systemd 249's
  journalctl skips the first matching line after such a cursor (`results/journalctl-after-cursor-20261020.txt`), so a
  step lost the first of those lines whenever the entry its cursor pointed at was not one of them. Which lines were
  lost cannot be read again without sudo: the system journal holds them. The driver is fixed and tested
  (`tests/ab_engine_stubs.sh`, cases 28 and 29). By the maintainer's rule the results below are not used for a new
  decision, and the two deployed switches (K1) are confirmed again with the fixed driver, on the model that decision
  0004 keeps; that plan is written here before its runs.
- Recorded on 2026-10-29: three commits changed passages written before their runs. The only criterion that changed is
  the reading for the confirmations, which `136470d` made stricter before they ran, as its paragraph ("Written after
  the screen's results, before the confirmations run") says. `f5f147b` (2026-10-20 22:17, after the IQ3_S screen ended
  at 20:57) first recorded the amendment of 17:25, inserting it before the last two items of the 12:10 amendment; no
  commit holds the 17:25 amendment from before the rerun began. `136470d` (22:45) put those two items back above it
  and reworded two sentences of the 17:25 amendment. `7541739` (2026-10-27) added sources to the 05:05 entry, to R's
  paragraph, to the 12:10 amendment's gap item and to the 17:25 amendment's 8 vCPUs, said that the K1 runs' gaps were
  IQ3_XXS's, and removed from the 17:25 amendment the times of a `scripts/test.sh` run and the length of two unit test
  runs, which no file in `results/` holds. The words as they were before the runs are in `8065650` (the 05:05 entry),
  `ca9e84f` (the IQ3_S plan) and `905ce09` (its 12:10 amendment).

## Results

### Screen 1: GROUP_SORT, 04:25-05:23

Three runs, c0-1, c1-1 and c0-2 in that order, each on a fresh engine; all were usable. The two C0 runs differed by
0.23% or less on every metric. Source: `results/SCREEN-group-sort.md`.

| Metric (0.1.45, deployed configuration) | C0 (c0-1, c0-2) | Spread of C0 | C1 (c1-1) | Change |
|---|---:|---:|---:|---:|
| 1.2K prompt: engine read tok/s | 205.40 | 0.1% | 240.70 | +17.2% |
| 4K prompt: engine read tok/s | 390.55 | 0.2% | 431.30 | +10.4% |
| 16K prompt: engine read tok/s | 695.05 | 0.0% | 755.20 | +8.7% |
| 16K prompt alone: TTFT s | 23.09 | 0.0% | 21.26 | -7.9% |
| 1 user: engine decode tok/s | 39.50 | 0.0% | 39.90 | +1.0% |
| 4 users: total tok/s (prompts' reading included) | 30.66 | 0.2% | 33.80 | +10.2% |
| 16K joining 3 decoders: the joiner's TTFT s | 22.51 | 0.1% | 20.56 | -8.6% |
| ... the decoders' longest gap s | 22.69 | 0.1% | 20.76 | -8.5% |
| ... the four requests' makespan s | 61.99 | 0.0% | 60.04 | -3.1% |

C1 answered all 32 requests exactly as C0 did. At start the engine printed "prompt MMQ sorts the full expert groups
(STRATA_MMQ_GROUP_SORT=1): 48 of 48 layers" (`results/c1-1-arm.txt`). C1 went on to its confirmation.

On 0.1.45 the admission stall remains: a 16K prompt joining three decoders got its first token after 22.5 s, and the
decoders waited 22.7 s. On 0.1.43.1 the deployment record measured the joiner's first token at 23.2 s, with the three
decoders stalled for those 23 s (its section 3.2; `results/SUMMARY.md`).

### Screen 2: C1 again, C2, C3, 05:36-07:30

Six runs in this order: c0-3, c1-2, c2-1, c3-1, c1-3, c0-4. All were usable. Source: `results/SCREEN-sched.md`.

| Metric (0.1.45, deployed configuration) | C0 (c0-3, c0-4) | C1 (c1-2, c1-3) | C2 (c2-1) | C3 (c3-1) |
|---|---:|---:|---:|---:|
| 1.2K prompt: engine read tok/s | 205.00 | +18.0% | +9.2% | +0.4% |
| 4K prompt: engine read tok/s | 390.75 | +10.6% | +10.8% | +0.2% |
| 16K prompt: engine read tok/s | 694.70 | +8.9% | +0.2% | +0.4% |
| 1 user: engine decode tok/s | 39.50 | +0.8% | -1.8% | 0.0% |
| 4 users: total tok/s (prompts' reading included) | 30.63 | +10.2% | +16.0% | -5.2% |
| 16K joining 3 decoders: the joiner's TTFT s | 22.51 | -8.9% | -0.1% | +11.0% |
| ... the decoders' longest gap s | 22.69 | -8.8% | -0.1% | -97.8% (0.49 s) |
| ... the four requests' makespan s | 61.88 | -3.2% | -0.7% | -4.5% |

Each switch showed in the engine's lines (`results/<tag>-engine-lines.txt`). C2 read the 1.2K and 4K prompts in 768-
and 1024-token chunks and kept the usual chunks for 16K. C3 read each joining 16K prompt "on a thread of its own"
while the slots decoded 514 and 516 rows.

C1 is confirmed. At every prompt size both C1 runs read faster than both C0 runs (each run's median is in
`results/SCREEN-sched.md`), with a mean gain of 12.5% over the three sizes, above the 5% of its criterion and the
maintainer's 10%. No other metric got worse, and C1 answered as C0 did on the 30 requests that both C0 runs answered
alike (the other two are described below).

By the screen's rule C2 would go on to a confirmation, where its mean gain over the three sizes, 6.7% here, would
have to reach 10%. It answered 18 of the 30 requests otherwise than C0. The maintainer adopted C1 and C3, not C2.

C3 removes the admission stall. While a 16K prompt is read, the decoders' longest gap falls from 22.69 s to 0.49 s. The joining prompt's first
token comes 11.0% later, and four users sent together get 5.2% fewer tokens per second in all. Both costs are above the 5% a screen
allows; the maintainer chose to adopt C3 with them.

C3 answered 11 of the 30 requests otherwise than C0: 7 of the 8 four-user requests and all 4 admission decoders
compared, in the two tests where it read prompts while other requests decoded. The requests that ran alone and the
joining prompts got C0's answers. Upstream's documentation names a cause for this case: while a prompt is read
beside decoding slots, the experts it borrowed from them run on the CPU in the slots' windows, which rounds
differently, and `--no-prefill-borrow` avoids it (0.1.45's `docs/BATCHING.md`, "Exactness"). That was not tested
here.

The two requests left out of these comparisons are two decoders in round 0 of the admission test. Their answers
had 1,226 and 1,260 tokens in c0-3, c1-2 and c2-1, and 1,228 and 1,180 tokens in c1-3 and c0-4, with the same text
within each group (c3-1 gave a third pair). C0 and C1 each gave both pairs, so GROUP_SORT is not the cause. The
screen sent the three decoders at the same instant, and the order of their first tokens varied: in c1-3 and c0-4
the third decoder was first, in the other seven runs of screens 1 and 2 the first or the second
(`decoders_first_token_vs_join_s` in `results/engine-screen.jsonl`). From screen 3 on, requests the screen used to
send at once go out 0.3 s apart, and every answer's text is kept, to see where two answers part.

The remote evaluation ran beside both screens, pinned to CPU 7. The driver records it from screen 2 on: during each
run's screen it used 0 to 2 CPU-seconds (the `beside` lines of `results/<tag>-arm.txt` and
`results/<tag>-after.txt`).

### Screen 3: pull request #2985 (pipelined MTP), 07:44-08:46

Three runs, m0-1, m1-1 and m0-2 in that order, on the pull request's tree (commit `2b7ffada` on 0.1.45, built
locally for sm_60), with M0's configuration from the table of arms. All were usable. The screen was version
2026-10-19b, with the default phases plus decode4 and eval4. The two M0 runs differed by 0.33% or less on every
metric and gave the same 44 answers, character for character. Source: `results/SCREEN-pmtp.md`; the two rows of
tokens per wall second come from `tokens` and `wall_s` of the decode4 and eval4 records in
`results/engine-screen.jsonl`.

| Metric (the pull request's tree) | M0 (m0-1, m0-2) | M1 (m1-1) |
|---|---:|---:|
| 1.2K / 4K / 16K prompt: engine read tok/s | 205.75 / 390.85 / 694.95 | +0.1% / -0.2% / -0.3% |
| 1 user: engine decode tok/s | 39.60 | -0.3% |
| 4 users: total tok/s (prompts' reading included) | 30.70 | +4.8% |
| 4 users decoding, short prompts: each user's engine decode tok/s, summed | 121.33 | +7.8% |
| ... all four's tokens per wall second | 103.80 | +7.3% |
| 4 users, thinking, sampled: each user's engine decode tok/s, summed | 105.10 | +17.1% |
| ... all four's tokens per wall second | 51.13 | +10.3% |
| 16K joining 3 decoders: the joiner's TTFT s | 22.52 | 0.0% |
| ... the decoders' longest gap s | 22.70 | +0.1% |
| ... the four requests' makespan s | 61.99 | -8.9% |

M1's engine printed its plan at start ("the 4 pipelined slot groups draft up to 1 MTP proposal per slot",
`results/m1-1-arm.txt`), and its batch lines show the groups drafting: they kept 54-55% of the MTP proposals in
decode4 and 78% in eval4 (`results/m1-1-engine-lines.txt`).

By the rule fixed before the results, M1 does not go on: its target, the four users' total rate, rose 4.8%, below the
5% a screen asks. That rate includes the reading of the four 1.5K-token prompts, which M1 leaves as it is. With short
prompts, four users got 7.3% more tokens per wall second with greedy decoding and 10.3% more in the evaluation's
sampled thinking mode. The summed per-request decode rates rose 7.8% and 17.1%; that sum counts every request alike,
whatever the length of its answer. No metric was more than 0.3% worse.

M1 answered 3 of the 44 requests otherwise than M0, each late in the answer: a four-user translation from character
486 of 769, and two admission essays from characters 6,829 and 6,617 of about 6,800 (`results/m0-1-texts.jsonl`,
`results/m1-1-texts.jsonl`). Upstream measured the pull request exact with the settings of its "Exactness" section,
which the deployed configuration does not use; with the default settings, it says, outputs drift apart after some
tokens (`docs/BATCHING.md` of the pull request). The pull request is open, not merged (2026-10-19).

### The combination: K0, K1, K1, K0, 11:05-12:39

Four runs in this order: k0-3, k1-3, k1-4, k0-4, with the fixed driver and screen version 2026-10-19c. All were
usable. The first attempt (k0-1, k1-1, k1-2, with k0-2 interrupted) is set aside; see the change to the plan at
10:17. Source: `results/SCREEN-combo.md`.

| Metric (0.1.45, deployed configuration) | K0 (k0-3, k0-4) | K1 (k1-3, k1-4) |
|---|---:|---:|
| 1.2K / 4K / 16K prompt: engine read tok/s | 205.15 / 389.35 / 694.20 | +17.4% / +10.7% / +9.1% |
| 16K prompt alone: TTFT s | 23.13 | -8.3% |
| 1 user: engine decode tok/s | 39.50 | +0.5% |
| 4 users: total tok/s (prompts' reading included) | 30.65 | +2.7% |
| 16K joining 3 decoders: the joiner's TTFT s | 22.54 | +1.1% |
| ... the decoders' longest gap s | 22.73 | 0.55 s |
| ... the four requests' makespan s | 62.01 | -5.6% |
| 28K joining 3 decoders: the joiner's TTFT s | 35.55 | +0.6% |
| ... the decoders' longest gap s | 35.81 | 0.59 s |
| ... the four requests' makespan s | 75.52 | -8.3% |
| 4 users decoding, short prompts: all four's tokens per wall second | 103.44 | +0.4% |

K1 is confirmed by each criterion fixed at 08:27. In each K1 run against each K0 run, every prompt size was read
faster, by 12.2% to 12.6% on average over the three sizes; the decoders' longest gap while a 16K prompt joined was
0.55 s against 22.72 and 22.74 s; and the four requests finished 4.2% to 7.1% sooner. The 16 requests that ran alone
got the answers both K0 runs gave (`results/engine-screen.jsonl`).

The joining prompt's first token comes 1.1% later (C3 alone made it 11.0% later), and four users sent together get
2.7% more tokens per second. eval4 says nothing here: between the two K0 runs, with the same answers but one, its
rates differed by 26.3% (the server's per-request rates, summed) and 11.1% (per wall second), more than between the
arms.

Of the 51 requests both K0 runs answered alike, K1 answered 23 otherwise, all in the phases where requests decode
beside a read (four 8, eval4 3, admit 6, admit28 6), and the two K1 runs answered 19 requests otherwise than each other
(four 4, eval4 4, admit 5, admit28 6, by phase from `results/engine-screen.jsonl`). The two K0 runs differed in one
answer, a sampled eval4 request.

## IQ3_S: decision 0005's switches again, and the next candidates

Written 2026-10-20 at 11:10, before any of these runs, while decision 0004's `ab3-` runs were under way. The criteria
below are fixed now and not changed after results are seen.

These runs follow the `ab3-` runs and use the model decision 0004 keeps. If it keeps IQ3_XXS, this plan is written
again for IQ3_XXS before any run. Every arm runs Strata 0.1.45, the deployed binary (sha256 `00869ac7…`, in the
experiment tree `~/dev/strata-0.1.45`), with GSQ-RCO IQ3_S and the deployed configuration
(`configure_strata_q3.py --profile deployed --model iq3_s`: cuts 13/25/38 and the 6 GiB floors; the ZFS ARC at 2 GiB).
The arms run in `bench/ab_engine.sh`'s transient unit and are measured with `bench/engine_screen.py`, screen version
2026-10-19c, with the phases prefill, decode, four, decode4, eval4, admit, admit28 and turns. Arms file:
`bench/arms-iq3s.json`; configs: `cfg-s-*.json` in the tree.

| Arm | Change against K1 | Exact by its source | Targets |
|---|---|---|---|
| K1 | none: `STRATA_MMQ_GROUP_SORT=1` and `STRATA_PIPED_CONCURRENT=1`, as deployed | - | the baseline |
| K0 | both of decision 0005's switches off | - | decision 0005 again |
| P4 | `STRATA_SIDE_MIXER=2`, `STRATA_FUSE_WAIT=2`, `STRATA_B6_MMVQ_ROWS=1`, `STRATA_GR_UP75=1` | yes: each one's source says its outputs are bitwise the default's | decode rates |
| P6 | P4 with `STRATA_ADAPT_NOWAIT=1` and `STRATA_ADAPT_LAG=2`: 0.2.0-beta's sm_60 set without `STRATA_FUSE_DECODE`, which 0.1.45 does not have | no: the adaptive tier's swaps take effect at other windows | decode rates |
| R | `--vram-reserve-mib 1280` instead of 1536, on every card (the later cards take the first card's reserve) | no: some experts move from the CPU to CUDA2 | decode rates |
| C2 | `--prefill-pipe 768` with the 2048 cap | no | prompt read rate |

R adds about 128 expert slots to CUDA2, which holds 6218 of its layers' 6656 profiled experts at 2.0 MiB a slot (the
08:30 start's engine lines in `results/ab2-iq3s-robust-switch.txt`); the other cards already hold all of theirs.

The order is K0, K1, P4, P6, R, C2, K1, K0, with the tags s-k0-1, s-k1-1, s-p4-1, s-p6-1, s-r-1, s-c2-1, s-k1-2 and
s-k0-2. Every step is a fresh engine start.

Criteria:
- **K1 against K0:** the criteria of 2026-10-19 08:27, with K1's runs at the second and seventh places and K0's at the
  first and last. If K1 fails one of them on IQ3_S, the maintainer decides; the deployed switches stay until then.
- **A candidate goes on** to the confirmation when its change on a target metric is larger than the spread between
  the two K1 runs and at least 5% in its favour, and no other metric moves against it by more than 5% beyond that
  spread. The target metrics of P4, P6 and R are one user's engine decode rate, four users' total rate and four users
  decoding short prompts (all four's tokens per wall second); C2's is the engine's prompt read rate at each of the
  three prompt sizes.
- **Answers:** P4 must give the requests that run alone (the prefill, decode and turns phases) the answers both K1 runs
  give; a difference is a finding and stops it. For every arm, the requests it answers otherwise than K1 are
  reported, counted only where the two K1 runs agree.
- **The confirmation** puts the candidates that went on, together, against K1 in the order A, B, B, A (A = K1). They
  are adopted when every run is usable and, in each B run against each A run, the metrics on which they went on are
  better, with a mean gain of at least 5% when all of them are exact (P4 alone) and at least 10% otherwise; no other
  metric is worse by more than 5%; and, when all of them are exact, the requests that run alone get the A runs'
  answers.
- **C2** changes how prompts are rounded, so the maintainer decides on it with the screen's numbers; it is confirmed
  only if the maintainer adopts it.
- **An adopted R** is deployed only after a robustness run (`bench/ab_model_run.sh <tag> B robustness`) keeps every
  card at least 700 MiB of VRAM free and MemAvailable at or above 6 GiB.
- A run that is not usable is measured again under a new tag; it is not judged.

Amended at 12:10 the same day, before any of these runs, after a review of the plan, the arms file and the driver:
- **What P6 measures.** In 0.1.45, `STRATA_ADAPT_NOWAIT=1` makes the adaptive tier skip its wait, and the lag is then
  never read (`src/program/generate.cpp`, lines 14251 and 15621). With the layer split and the pipelined slot groups,
  either switch also makes every verifier publish every activation (`set_always_publish`, lines 9606-9612). P6 against
  P4 therefore measures `STRATA_ADAPT_NOWAIT=1` with that, not `STRATA_ADAPT_LAG=2`; upstream's P100 gain for the lag
  came from a run without it.
- **Where P4's switches act here.** `STRATA_SIDE_MIXER=2` acts only in one request's windows, not in a batch window
  (`src/core/verify.cpp`, lines 1008-1016). `STRATA_FUSE_WAIT=2` acts only on a stage whose experts are not all
  resident: CUDA2 when decoding (lines 1975-1984). `STRATA_B6_MMVQ_ROWS=1` acts on the Q6_K, Q4_K and IQ4_XS matrices
  this model has, and `STRATA_GR_UP75=1` on windows of three or more tokens. The engine prints no line for any of the
  six; the arm record shows only that the engine has the variables.
- **The spread** that a candidate's change is held against is that metric's own spread between the two K1 runs.
- **eval4's two metrics** are reported but do not stop a candidate: its two K0 runs differed by 26.3% and 11.1% on
  2026-10-19 (`results/SCREEN-combo.md`).
- **The decoders' longest gap** (16K and 28K joining three decoders) counts against a candidate only when it is worse
  by more than 5% beyond the spread and by more than 0.25 s; the K1 runs' gaps on IQ3_XXS were about half a second
  (`results/SCREEN-combo.md`).
- **The phases** are set for every step: `ENGINE_SCREEN_PHASES=prefill,decode,four,decode4,eval4,admit,admit28,turns`.

- **R's arm check** must also show CUDA2's cache above 6300 slots ("CUDA2 runs layers 25-37, expert cache 63xx
  slots"), the reserve's effect where it matters.
- The driver is changed before these runs: it refuses an `args_set` whose other arms hold the flag with different
  values and an `args_add` that repeats a flag the config holds, and it stops before a step when the arms file or a
  config changed since the check at the start.

Amended at 17:25 the same day: the first run of this screen, 16:24-17:19, is not used. During its first two steps,
other work ran on the machine (8 vCPUs, `results/AB-0.1.45-ple-hint.md`):
- during s-k0-1's measurement (16:39-16:51): a run of `scripts/test.sh`, over both decode4 rounds and
  the start of eval4, and a `scripts/check-drift.sh` run, hashes of the CUDA libraries, and scans and removals in /tmp;
- during s-k1-1's (17:05-17:17): two unit test runs, over decode4's second round and eval4, and a
  few shorter commands.

In s-k0-1's first decode4 round, the four users' engine decode rates summed to 102.0 tokens per second, in s-k1-1's
to 108.5 (`results/s-k0-1-screen.txt`, `results/s-k1-1-screen.txt`). The maintainer stopped the screen during s-p4-1's load and chose to run all eight steps
again, in the same order, under the tags s2-k0-1, s2-k1-1, s2-p4-1, s2-p6-1, s2-r-1, s2-c2-1, s2-k1-2 and s2-k0-2.
While they run, nothing else runs on the machine but the remote evaluation's driver, on CPU 7 as before. The criteria
do not change.

### IQ3_S screen: K0, K1, P4, P6, R, C2, K1, K0, 17:28-20:57

The second run of the screen, tags s2-* (the first is not used: see the amendment of 17:25 above). All eight runs are
usable, and every arm record shows the deployed engine, sha256 `00869ac7…` (`results/SCREEN-iq3s.md`, against K1;
`results/SCREEN-iq3s-k1-vs-k0.md`, K1 against K0). The arm records give the experiment tree's commit as `2b7ffada`
with a five-file diff: `~/dev/strata-pr2985` is a copy of the experiment tree whose `.git` file names the same
worktree entry, so git reports the branch the copy checked out.

Against K1, with the spread between the two K1 runs:

| metric | K1 spread | P4 | P6 | R | C2 |
|---|---:|---:|---:|---:|---:|
| 1 user: engine decode tok/s | 1.6% | +5.3% | +6.2% | +3.1% | -0.6% |
| 4 users: total tok/s | 1.3% | +5.8% | +6.2% | +2.3% | +13.7% |
| 4 users decoding short prompts: tokens per wall second | 3.0% | -1.9% | +1.6% | -0.5% | -0.2% |
| 1.2K / 4K / 16K prompt: engine read tok/s | 0.4% / 0.3% / 0.1% | +0.5% / +0.2% / +0.0% | +0.5% / +0.1% / +0.1% | +0.1% / +0.1% / -0.0% | +10.7% / +8.7% / -0.1% |

- **P4 and P6 go on**: on one user's decode and four users' total rate, each gains at least 5% and more than its
  spread, and no other metric moves against them by more than 5% beyond its spread (eval4 is reported only). P4 gives
  K1's answers to all 12 requests that run alone and that the two K1 runs answer alike (the prefill and decode
  phases). The turns phase cannot test this: both K1 runs, and both K0 runs, answered all four of its requests
  differently from each other, where on IQ3_XXS two runs of the same arm gave all four the same answers (k0-3 and
  k0-4, k1-3 and k1-4, m0-1 and m0-2 of 2026-10-19). These counts come from `results/engine-screen.jsonl`.
- **R does not go on**: +3.1% and +2.3% on its target metrics.
- **C2** reads 1.2K and 4K prompts 10.7% and 8.7% faster, and four users get 13.7% more tokens per second; no other
  metric moves against it by more than 5% beyond its spread.
- **Answers**, counted on the 21 requests that the two K1 runs answer alike: P4 answers 3 of them otherwise than K1
  (all in the four phase), P6 5 (four 4, decode 1), R 7 (four 4, decode 3), C2 9 (prefill 2, decode 3, four 4) and K0
  4 (four 4).
- **K1 against K0**, by the criteria of 2026-10-19 08:27: `STRATA_PIPED_CONCURRENT=1` passes: while a 16K prompt joins
  three decoders, their longest gap is 0.72 and 0.83 s in the K1 runs against 23.27 and 23.20 s in the K0 runs, and the
  four requests' makespan is 1.9% shorter (28K: 0.74 against 36.57 s); the joining prompt's first token comes 8.4%
  later (28K: 8.2%). `STRATA_MMQ_GROUP_SORT=1` fails: on IQ3_S, K1 reads prompts at K0's rate (-0.4%, +0.1% and +0.3%
  at 1.2K, 4K and 16K), where on IQ3_XXS the two switches together read them 9-17% faster (`results/SCREEN-combo.md`).
  Four users get 2.1% more tokens per second with K1, and K1 answers 7 of the 24 requests that both K0 runs answer
  alike otherwise than K0, all in the four phase (`results/SCREEN-iq3s-k1-vs-k0.md`). As the criteria of 08:27 judge
  K1 as a whole, K1 fails them, and the plan leaves the decision to the maintainer.

The maintainer decided after these results: P4 and P6 are confirmed together as the plan says (P6's switches against K1, A B B A,
with the 10% threshold, since P6 is not exact); C2 is adopted and confirmed; `STRATA_MMQ_GROUP_SORT=1` stays deployed
(no cost was measured; removing it would take a restart). The confirmations run after the review fixes are verified.

Written after the screen's results, before the confirmations run (first in commit f5f147b, then made stricter after a
review, the same evening): the plan does not say how a candidate that went on on more than one metric is judged, and
of the readings, the strictest for the candidate is taken. Each metric on which it went on must be better in each B run
than in each A run, and on each of them the mean of the B runs must beat the mean of the A runs by the threshold. For
P6's switches these are one user's decode and four users' total rate, with 10%. For C2 they are the read rates of 1.2K
and 4K prompts, with 10%; and, as this file put it for the same switch on 2026-10-19 (screen 2), its mean gain over
the three prompt sizes must reach 10% too. The other criteria of the plan stand.

### The confirmations of P6's switches and C2: how they run

Written 2026-10-29 at 15:50, before any of these runs, after the review fixes were deployed (2026-10-27,
`results/deploy-review-fixes-20261027.txt`) and the local evaluation ended. Both confirmations run in one call of
`bench/ab_engine.sh`, with `bench/arms-iq3s.json`, screen version 2026-10-19c and
`ENGINE_SCREEN_PHASES=prefill,decode,four,decode4,eval4,admit,admit28,turns`, in the order K1, P6, P6, K1, K1, C2, C2,
K1 with the tags cf-k1-1, cf-p6-1, cf-p6-2, cf-k1-2, cf-k1-3, cf-c2-1, cf-c2-2 and cf-k1-4. P6's confirmation is the
first four runs and C2's the last four; each is A, B, B, A with runs of its own, and every step is a fresh engine
start. Before the call, K1's config `cfg-s-k1.json` equals the live config but for exe, cwd and log, the engine in the
experiment tree has the deployed engine's sha256 (`00869ac7…`), and the config tool's dry run on the live config
changes nothing. The criteria are those above. If both are adopted, the combination is confirmed the same way, with
its own arm and a plan written before its runs. While these runs go on, nothing else runs on the machine.

### The confirmations of P6's switches and C2, 15:51-19:20

All eight runs are usable (`results/CONFIRM-iq3s-p6.md`, `results/CONFIRM-iq3s-c2.md`). Neither candidate is adopted,
and the service keeps K1. On each metric a candidate went on with, both of its runs beat both K1 runs, but only C2's
1.2K read rate gains the 10% the plan asks for. Each value is the mean of two runs' medians.

| metric | K1 | candidate | change |
|---|---:|---:|---:|
| P6: 1 user, engine decode tok/s | 44.20 | 46.70 | +5.7% |
| P6: 4 users, total tok/s | 27.27 | 28.17 | +3.3% |
| C2: 1.2K prompt, engine read tok/s | 200.35 | 222.40 | +11.0% |
| C2: 4K prompt, engine read tok/s | 379.00 | 412.60 | +8.9% |

C2 also had to gain 10% over the three prompt sizes. 16K prompts are read at K1's rate (+0.1%), so it gains 6.6%. It
gives four users 15.8% more tokens per second, which is not one of its target metrics. While a 16K prompt joins three
decoders, their longest gap grows by 0.13 s with P6 and 0.09 s with C2, under the 0.25 s the plan counts.

After the last run, the driver started `strata-q3` again, and `scripts/check-drift.sh` reported no drift
(`results/restart-after-confirm-20261029.txt`).
