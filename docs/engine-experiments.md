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

## Results

### Screen 1: GROUP_SORT, 04:25-05:23

Three runs in this order: c0-1, c1-1, c0-2. Each started a fresh engine, and all three were usable. The two C0 runs
differed by 0.23% or less on every metric. Source: `results/SCREEN-group-sort.md`.

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

C1 is confirmed. At every prompt size both C1 runs read faster than both C0 runs: 241.7 and 242.0 against 205.8 and
204.2 tok/s at 1.2K, 431.5 and 432.6 against 390.9 and 390.6 at 4K, 756.6 and 756.5 against 694.3 and 695.1 at 16K.
The mean gain is 12.5%, above the 5% of its criterion and the maintainer's 10%. No other metric got worse, and C1
answered as C0 did on the 30 requests that both C0 runs answered alike (the other two are described at the end).

C2 reads prompts up to 4K 9-11% faster and 16K prompts 0.2% faster, and 18 of the 30 requests got other answers. By
the screen's rule it would go on to a confirmation, where its mean gain over the three sizes, 6.7% here, would have to
reach 10%. The maintainer adopted C1 and C3, not C2.

C3 removes the admission stall. While a 16K prompt is read, the decoders' longest gap falls from 22.69 s to 0.49 s,
and the four requests finish 4.5% sooner. The joining prompt's first token comes 11.0% later (24.99 s), and four
users sent together get 5.2% fewer tokens per second in all. Both costs are above the 5% a screen allows; the
maintainer chose to adopt C3 with them.

C3 answered 11 of the 30 requests otherwise than C0: 7 of the 8 four-user requests and all 4 admission decoders
compared, in the two tests where it read prompts while other requests decoded. The requests that ran alone and the
joining prompts got the same answers. Upstream's documentation names a cause for this case: while a prompt is read
beside decoding slots, the experts it borrowed from them run on the CPU in the slots' windows, which rounds
differently, and `--no-prefill-borrow` avoids it (0.1.45's `docs/BATCHING.md`, "Exactness"). That was not tested
here.

The two requests left out of these comparisons are two decoders in round 0 of the admission test. They answered with
1,226 and 1,260 tokens in c0-3, c1-2 and c2-1, and with 1,228 and 1,180 tokens in c1-3 and c0-4; the answers are
identical within each group (c3-1, whose switch changes such answers, gave a third pair). C0 and C1 each gave both
pairs, so GROUP_SORT is not the cause. The screen sent the three decoders in the same instant, and the order in which
they got their first token varied: in c1-3 and c0-4 the third decoder was first, in the other seven runs of screens
1 and 2 the first or the second (`decoders_first_token_vs_join_s` in `results/engine-screen.jsonl`). From screen 3
on, requests the screen used to send at once go out 0.3 s apart, and every answer's text is kept, to see where two
answers part.

The remote evaluation ran beside both screens, pinned to CPU 7. The driver records it from screen 2 on: during each
run's screen it used 0 to 2 CPU-seconds (the `beside` lines of `results/<tag>-arm.txt` and
`results/<tag>-after.txt`).

### Screen 3: pull request #2985 (pipelined MTP), 07:44-08:46

Three runs in this order: m0-1, m1-1, m0-2, on the pull request's tree (commit `2b7ffada` on 0.1.45, built locally
for sm_60), with the deployed configuration less `--batch-groups auto` and with `STRATA_BATCH_SELF_COMMIT=0`. All
were usable. The screen ran the default phases and decode4 and eval4, and sent the requests it sends at once 0.3 s
apart (version 2026-10-19b). The two M0 runs differed by 0.18% or less on every metric and gave the same 44 answers,
character for character. Source: `results/SCREEN-pmtp.md`.

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
sampled thinking mode. The server's per-request decode rates, summed, rose 7.8% and 17.1%; that sum counts every
request alike, whatever the length of its answer. The admission test's four requests finished 8.9% sooner. No
metric was more than 0.3% worse.

M1 answered 3 of the 44 requests otherwise than M0, each late in the answer: a four-user translation from character
486 of 769, and two admission essays from characters 6,829 and 6,617 of about 6,800 (`results/m0-1-texts.jsonl`,
`results/m1-1-texts.jsonl`). Upstream measured the pull request exact with the settings of its "Exactness" section,
which the deployed configuration does not use; with the default settings, it says, outputs drift apart after some
tokens (`docs/BATCHING.md` of the pull request). The pull request is open, not merged (2026-10-19).

### The combination: K0, K1, K1, K0, 11:05-12:39

Four runs in this order: k0-3, k1-3, k1-4, k0-4, with the fixed driver and screen version 2026-10-19c (the first run of
this confirmation is set aside, see the changes to the plan). All were usable. Source: `results/SCREEN-combo.md`.

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
0.55 s against 22.72 and 22.74 s; and the four requests finished 4.2% to 7.0% sooner. The 16 requests that ran alone
got the answers both K0 runs gave.

With the faster read, the joining prompt's first token comes 1.1% later, where C3 alone made it 11.0% later, and four
users sent together get 2.7% more tokens per second. eval4 says nothing here: between the two K0 runs, with the same
answers but one, its rates differed by 26.3% (the server's per-request rates, summed) and 11.1% (per wall second), more
than between the arms.

Of the 51 requests both K0 runs answered alike, K1 answered 23 otherwise, all in the phases where requests decode
beside a read (four 8, eval4 3, admit 6, admit28 6), and the two K1 runs answered 19 requests otherwise than each other
(four 4, eval4 4, admit 5, admit28 6). The two K0 runs differed in one answer, a sampled eval4 request.
