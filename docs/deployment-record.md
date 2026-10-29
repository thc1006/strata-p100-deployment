# Deployment record: Strata with Qwen3.8-Flash-Next on 4× Tesla P100 (ZFS root)

Last updated: 2026-10-29. Runs GSQ-RCO IQ3_S since 2026-10-20 ([decision 0004](decisions/0004-iq3-s.md)) and Strata 0.1.45 (commit `477ac13cb`) since 2026-10-19; the measurements in section 3 were made with IQ3_XXS on 0.1.43.1 (commit `a0116b2`) unless a section says otherwise. CUDA 12.9.1, NVIDIA driver 535.230.02 (`results/cuda-installer-20261016.log`, `results/host-versions-20261024.txt`).

Deployment date: 2026-10-16 (all times are Taiwan time, UTC+8). Machine: VMware VM with 8 vCPU E5-2696 v4, 62.8 GiB RAM, 4× Tesla P100-PCIE-16GB and a ZFS root (CPU and GPUs: `results/setup-20261016.log`; 8 vCPUs: `results/AB-0.1.45-ple-hint.md`; RAM: MemTotal in `results/deploy-0.1.45-20261019.txt`).

This document gives, for each setting, what it is, why, and the measurements behind it, and lists the known limits. Operating procedures are in [`docs/runbooks/`](runbooks/). System changes and how to undo them are in [`docs/reference/system-changes.md`](reference/system-changes.md).

Raw data is in `results/`; `bench/report.py` generates the comparison tables in `results/SUMMARY.md`. A few numbers come from the engine log (`~/dev/strata/strata-iq3_xxs.log`, rotated above 50 MiB) or the systemd journal. The engine's startup lines, the prompt line of every request and the conversation parking lines from before 2026-10-16 17:07 are kept verbatim, with their original line numbers, in `results/engine-log-20261016-extract.txt`; the fault-injection journal is `results/fault-injection-journal.txt`. Each number is labeled with the configuration it came from:

| Tag | Configuration | Used for |
|---|---|---|
| **[deployed]** | The deployed configuration of 2026-10-16 (IQ3_XXS, 0.1.43.1), config SHA-256 `9d774535…` (`results/SUMMARY.md`) | Robustness (tag `deployed`), multi-turn conversations (`final-deployed-mt`), batch consistency, per-layer embedding (PLE) probe |
| **[deployed*]** | [deployed], except that the free-memory floor for parking is the engine default of 2560 MiB (deployed: 8192). Available memory never came near either floor. | Performance suite (tag `final-parking`), fault injection (the engines started at 12:49 and 12:59) |
| [no-parking] | [deployed] without conversation parking (batch-groups auto) | First robustness run (tag `final`), prefill/decode retest (`final-cold`) |
| R0–R4, A/A | Configuration comparison runs (sections 2 and 4; their order is in section 6) | Choosing the configuration only |
| [A/B] | Version or model comparisons on a separate tree or a fresh restart for each run; each section names its runs | Sections 3.6, 3.7 and 3.9 |

---

## 1. Current state of the deployment

| Item | Details |
|---|---|
| API | `http://127.0.0.1:8080/v1` (OpenAI-compatible), `/v1/messages` (Anthropic-compatible). Listens only on 127.0.0.1. No API key is set (section 5). |
| Model | `ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF` IQ3_S, revision `ed59f92`, since 2026-10-20 (IQ3_XXS before; section 3.9). The SHA-256 of both shards matches the Hugging Face LFS hashes (`config/deployed-artifacts.sha256`); shard 2, the PLE table, is IQ3_XXS's file. |
| Engine | Strata 0.1.45, commit `477ac13cbe8e582c20484871ce459d20d82df09d`, built locally with CUDA 12.9 for `sm_60` (`engine-cuda12/BUILD.json`), with `STRATA_MMQ_GROUP_SORT=1` and `STRATA_PIPED_CONCURRENT=1` ([decision 0005](decisions/0005-strata-0.1.45-group-sort-concurrent-read.md)). Until 2026-10-19: 0.1.43.1 (`a0116b2`), kept at `~/dev/strata-0.1.43.1`. |
| Services | `strata-q3.service` (starts at boot), `strata-q3-health.timer` (runs the watchdog every 2 minutes; the watchdog does nothing while the service is starting), `zfs-arc-max.service` (ZFS Adaptive Replacement Cache (ARC) limit 2 GiB; 8 GiB until 2026-10-19, decision 0004) |
| Concurrent serving | 4 slots (`concurrency.serving=4` in `/v1/status`), 32K context per user, INT8 key-value (KV) cache |
| Startup time | With IQ3_S, `systemctl restart` took 550–560 s in the 6 restarts of decision 0004's runs (`results/ab2-iq3s-*-switch.txt`, `results/ab3-iq3s-*-switch.txt`). The first start after the download read the experts at 60 MB/s and took more than 900 s (`results/ab-iq3s-1-switch.txt`). With IQ3_XXS, 12 starts took 428–542 s to become ready (`results/service-load-times.txt`). During a start, `systemctl status` shows `activating`. A full reboot was not measured. |

Quick check: `scripts/check-deployment.sh`

---

## 2. How the final configuration differs from the original setup guide

| Setting | Original setup guide | Deployed | Evidence |
|---|---|---|---|
| PLE table location | baseline `mmap` | **`ram`** (mlock 26.8 GiB: `mlocked_gib` in `deployed-robust-monitor.csv`) | R0 (mmap) read cold text at 126 tok/s (4.4K) and 217 tok/s (32K), and the 2nd and 3rd runs of the same text, on warm pages, at 389 and 569 tok/s (the medians of the 3 runs). Against R1 (ram), R0's 4-user decode was 19–26% lower (four users thinking, per-user median −19%; short prompts, total −22% and per request −26%), and one-user short-prompt decode 21% lower (28.2 against 35.7 tok/s: `per_req_tps` in `results/baseline-serve_load.json` and `results/R1-pleram-serve_load.json`). One-user decode matched R1 only when the same text was rerun. The cause is presumed to be cold-page reads; R0 had no disk monitoring. |
| PLE read-ahead hint | Not mentioned | **`--no-ple-prefetch`** | Section 4, item 1 |
| Layer split | 12/24/36 | **13/25/38** for IQ3_S (13/26/38 for IQ3_XXS) | IQ3_XXS at 13/26/38: 4-user decode 85.9 → 100.6 tok/s (R1b → R2, one run each; A/A restart difference <1%), all 24,576 experts resident (`24576 resident experts` in `batch_test_deployed_engine.log`), each GPU about 60% busy on average (`R2-lastlight-monitor-all.json`). IQ3_S's experts do not all fit in VRAM. By the budget script's estimate from the GGUF headers, 13/25/38 at a 1536 MiB reserve gives the four expert caches 98.97% of the expert profile's routing, against 98.67% at 13/26/38 (`results/iq3s-memory-budget-20261019.txt`). The engine runs IQ3_S at 13/25/38 (`results/ab3-iq3s-1-arm.txt`). |
| VRAM reserve | aggressive 1024 MiB | **1536 MiB** | With IQ3_XXS, every expert is resident at 1536 (`1536 MiB reserved` and `100% of the experts resident` in `batch_test_deployed_engine.log`), so a lower value fits no more experts. With IQ3_S, the expert cache fills CUDA2 down to the reserve (6218 of its 6656 profiled pairs: `results/ab3-iq3s-1-arm.txt`), and by decision 0004's projection, CUDA2 could fall to about 240 MiB free at 1024 MiB ([decision 0004](decisions/0004-iq3-s.md)). No run used 1024 MiB. Upstream #2065 reports a hang at 395–538 MiB free (a Windows case; whether Linux behaves the same is not verified). |
| batch groups | 4 | **auto** (with 4 slots and 4 stages the same as 4: the log shows `4 groups of 1 slot`) | `1` cost 40% of 4-user decode (one user −2%), made the last user's time to first token (TTFT) for long inputs arriving together 2.3–2.8 times as long, and left GPU3 as little as 414 MiB (R3 against R1b, `results/SUMMARY.md`). Unlike a fixed 4, auto still divides the slots when VRAM holds fewer than 4 (not measured). |
| Conversation parking | Not mentioned | **8192 MiB, 8 conversations, ≥1024 tokens, no new parking when available memory is below 6 GiB** (8 GiB until 2026-10-19, decision 0004) | Section 3.4 |
| prefill chunk | 2048 (and suggests comparing 1024/4096) | **2048** | Section 3.3 |
| CPU workers | 6 | 6 | No 4/6/8 comparison was done. In the 47-minute robustness run on [deployed] (IQ3_XXS, every expert in VRAM), CPU0 averaged 90.7% (99.9% during the soak, from 16:02:48 in `deployed-robust-monitor.csv`; the engine main thread) and the other 7 CPUs 4.8–6.7% (`deployed-robust-monitor-all.json`). With IQ3_S, the engine reports `98% of the experts resident` (`results/ab3-iq3s-1-arm.txt`). No CPU-worker count was compared with IQ3_S. |
| Engine version | 0.1.43.1 | **0.1.45** since 2026-10-19, with two switches the engine experiments confirmed (section 3.8, [decision 0005](decisions/0005-strata-0.1.45-group-sort-concurrent-read.md)) | 0.1.44.1 was not adopted: in the local A/B it was within about 1% of 0.1.43.1, except one-user short prompts (section 3.6). Two upstream issues (checked 2026-10-16) report layer split slowing down in the 0.1.43 series. This configuration has neither cause in #2038: `--batch-mtp` on layer split, or the PLE probe falling back to mmap (the startup log shows `4 groups of 1 slot` and `PLE table locked in RAM`). The fix for #2281 (0.1.43.1 decode −20%) is `--no-ple-prefetch` (section 4, item 1). |
| Environment variables | 10 | The guide's 10, and 3 of 0.1.45 since 2026-10-19 | Checked against the source code: in this configuration `STRATA_RESIDENT_HEADROOM_GIB=6` (8 until 2026-10-19), `STRATA_MMQ_GROUP_SORT=1` and `STRATA_PIPED_CONCURRENT=1` change behavior, and `STRATA_MMQ_RESIDENT_SORT_NE=0` keeps an inexact variant of the sort off. The others are off. |
| memlock | Service account memlock set to unlimited; the systemd service sets `LimitMEMLOCK=infinity` | Only the unit's `LimitMEMLOCK=infinity`; the account limits were not changed | It applies to the service only. Run by hand outside systemd, the engine has a memlock limit of about 8 GiB (`RLIMIT_MEMLOCK soft 8428400640` in `ple-hint-count-harness.txt`) and loads the PLE table "not locked" (`batch_test_deployed_engine.log`). |
| ARC limit | modprobe.d + initramfs | systemd oneshot | Does not touch the boot chain. It also sets `zfs_arc_shrinker_limit=0`. |

`tools/configure_strata_q3.py --profile deployed --model iq3_s` generates the configuration. `config/profile.conf` records these arguments, and the preflight checks the live config against them before every start.

---

## 3. Measured results on the deployed configuration

Most tests used English text (Strata's own documentation and short English passages), and most throughput tests had thinking off. These are synthetic loads; the lab uses Traditional Chinese, so results may differ. Unless noted, each item ran once (R2 had one A/A restart, with a throughput difference of <1%).

### 3.1 One user and short prompts [deployed*]

| Test | Result |
|---|---|
| Total throughput for 1/2/4 users (short prompt, 512 tokens, thinking off, median of 3 rounds) | 34.9/60.3/100.0 tok/s (with 4 users, 28.5 tok/s per user; TTFT p50/p95 2.61/4.35 s) |
| Four users thinking at the same time (medium, budget 1024, max 4096, 3 rounds) | 29.5–32.2 tok/s per user; 38.8–50.9 s per request |
| One-user decode (the two canonical prompts from the community report, with multi-token prediction (MTP)) | 48.7–52.0 tok/s (6 runs) |
| prefill (each of the 3 runs reads a different passage) | 4.4K: 392.5–406.8 tok/s (TTFT about 11.1 s). 32K: 777.9–789.3 tok/s (TTFT about 40.9 s). The lower values include 0.36–1.07 s spent first parking the previous conversation (section 3.3); without it, about 406–408 and 797–804 tok/s. |

> These results are not ranked against other 4× P100 reports, whose engine versions, GPU limits and context lengths differ and are not recorded here. These runs used 0.1.43.1, 1328 MHz/250 W (the cards' top clock and power limit: `results/gpu-limits-20261022.txt`) and 32K context.

### 3.2 Long inputs and concurrent users (the main P100 limit) [deployed*]

| Scenario | Result |
|---|---|
| 4 users each send 8K at the same time | First user TTFT 15.1 s, last user 59.5 s |
| 4 users each send 16K at the same time | First user 23.6 s, last user 94.4 s |
| 4 users each send about 27K at the same time (worst case) | First user 35.9 s, last user 142.9 s |
| While 3 users are decoding, a 4th user sends 16K | New user TTFT 23.2 s. The other 3 users stall completely for these 23 s (0.09 tok/s). |
| While 2 users are decoding, 16K and 8K join one after the other | A user who is decoding pauses for up to 37.5 s |
| A short question sent 1 s into reading a 28K prompt | The short question gets its first token after 35.8 s (it cannot jump the queue) |
| Open arrivals (Poisson, fixed seed, lab-style mixed load), 6 requests per minute | Strict service-level objective (SLO) 7/30 (23%), TTFT p50/p95 19.8/57.1 s, 0 errors |

The strict SLO was fixed before testing: TTFT ≤ 10 s + prompt tokens/400, longest pause ≤ 10 s, decode ≥ 5 tok/s. No request hits the prefix cache (section 6). Poisson at 3 and 12 requests per minute is in section 3.5. On 0.1.45 with `STRATA_PIPED_CONCURRENT=1`, the other users' stalls in rows 4 and 5 and the short question's wait in row 6 are much shorter (section 3.8, section 4 item 4; for row 5, longest pauses of 0.92–1.06 s with IQ3_S in `results/ab3-iq3s-*-perf.log`). The new user's own TTFT in row 4 is not: 23.31–23.36 s with IQ3_XXS and 25.57–25.59 s with IQ3_S (`results/AB-iq3s-ab3.md`).

### 3.3 Prefill chunk comparison (only `--prefill` changed, one run each; 1024/4096 on [deployed], 2048 on [deployed*])

Italics mark the best value in each row.

| Scenario | 1024 | **2048 (chosen)** | 4096 |
|---|---:|---:|---:|
| 4.4K prefill (tok/s) | *438.9* | 393.4 | 301.1 |
| 32K prefill (tok/s) | 669.0 | 783.3 | *804.0* |
| 4×8K last-user TTFT | *59.4 s* | 59.5 s | 71.1 s |
| 4×16K last-user TTFT | 103.8 s | *94.4 s* | 101.2 s |
| 4×27K last-user TTFT | 163.9 s | 142.9 s | *141.4 s* |
| TTFT of a new user's 16K | 25.6 s | *23.2 s* | 24.8 s |
| Longest pause of a user who is decoding (16K + 8K join) | 39.9 s | *37.5 s* | 41.6 s |
| Poisson 6 per minute, strict SLO | 9/30 | 7/30 | 6/30 |

The prefill numbers are medians of three runs. All three settings read the same three passages and parked the same way: before the 2nd and 3rd 4.4K runs and before all three 32K runs, the engine first parked the previous conversation in RAM (0.36–1.15 s across the three settings; 0.36–1.07 s for 2048). The engine starts its prefill timer before it parks, so this time counts as prefill and the table compares like with like. Run by run, 1024 was 11.5–13.6% faster than 2048 at 4.4K and 14.2–15.1% slower at 32K; 4096 was 23–24% slower at 4.4K and 2.6–3.5% faster at 32K. Between restarts, the first 4.4K run with 2048 (no parking before it) read at 406.8 tok/s on [deployed*] and 406.9 tok/s on [no-parking].

2048 was chosen because it was best or near best for long inputs of 16K and above and for the time other users are blocked. In Poisson, 1024 met the SLO on 2 more requests, but request by request 4 results flipped, and [deployed*] and R2 A/A, which differ only in parking, also differ by 3 flips: within the noise of a single test. If most of the lab's documents are under 8K, 1024 is worth considering.

### 3.4 Conversation parking

| Test | Result |
|---|---|
| Multi-turn conversations (4 conversations of 5 turns each, plus 2 clients that send short requests continuously = 6 clients competing for 4 slots), turns 2–5 | [no-parking]: cache reuse 0/16, TTFT median 38.8 s, max 85.1 s → [deployed]: reuse 16/16, TTFT median 20.2 s, max 44.6 s (22 and 26 interfering requests). [deployed] in the robustness test (tag `deployed`): 16/16, 19.8 s, 44.2 s (21 interfering requests). |
| Without competition (a single conversation, or 4 conversations without interference) | The cache is reused normally without parking (`conversation_cache.jsonl`) |
| Whether output after restore is the same as if the conversation never left | IDENTICAL (92/92 tokens). Engine-level test with snapshot read-back verification (`parking_test.out`; [deployed*] plus `--pcie-frac 0 --adapt-every 1000000` for exact comparison). |
| Cost | When a new conversation takes the slot, the previous one (≥1024 tokens) is first saved to RAM: median 89 ms (1K–4K tokens) to 592 ms (20K–40K tokens), longest 1.15 s. Restore median 38 ms (185 parks and 32 restores in the engine log before 15:28). Poisson at 6 per minute, request by request, [deployed*] against the two R2 runs: TTFT median +0.02 to +0.09 s (between the two R2 runs: +0.04 s), and 2 and 3 SLO flips (between the two R2 runs: 1). [no-parking] against [deployed]: at 3 per minute both 22/30, 0 flips, TTFT median +0.09 s; at 12 per minute 6 and 7/30 (5 flips under overload). |
| RAM | 4 conversations take about 4 GB (4.06 GB at `parked=4`, the end of the multi-turn runs on [deployed*] and [deployed]: `engine-log-20261016-extract.txt`). Limit 8 GiB. New conversations are not parked when system available memory is below the floor (8 GiB then, 6 GiB since 2026-10-19). |

### 3.5 Correctness and stability [deployed] (unless noted otherwise)

| Test | Result |
|---|---|
| Token-by-token consistency between batch and solo (4 slots, 128 tokens, exact-comparison settings) | 4/4 IDENTICAL (`batch_test_deployed.out`; a separate run on the baseline configuration was also 4/4) |
| Pascal dp4a emulation (1,073,741,824 cases on each of the four GPUs) | 0 mismatches (`sm60_dp4a_check.txt`) |
| Answer correctness (16 questions with known answers, thinking low) | solo 16/16, 4 in parallel 16/16. The model sometimes writes the Simplified Chinese "最终答案" ("final answer"); this was found in a rerun on [no-parking], and parsing now accepts both Traditional and Simplified. |
| Needle 30K (30,618 tokens, depth 10/50/90%) | 3/3 |
| Context boundary | Exactly filling the context is accepted; 1 token more returns 400 |
| Overload: 8/12 users competing for 4 slots | 0 errors; TTFT p50/p95 8.9/16.3 s, 15.1/28.6 s |
| Disconnect in the middle of a request | The other 3 users completed in full. A 5th request sent right after the disconnect got its first token in 0.72 s, while the others were still decoding. |
| Stream closed early (solo and batch) | No tokens leaked to the next request |
| Thinking budget 128/512 | Finished near the budget (144/528 chunks); answers correct |
| Poisson 3 per minute (the same 30 requests as section 3.2, same seed) | Strict SLO 22/30 (73%), 0 errors; TTFT p50/p95 7.8/28.1 s; longest pause of a decoding user p95/max 24.1/30.3 s. The 8 that missed: short questions 3/13, 2K documents 2/10, 8K documents 3/5 (16K documents: 2/2 met). |
| Poisson 12 per minute (over capacity) | Strict SLO 7/30 (23%), 0 errors; TTFT p50/p95 39.0/174.5 s (queuing). The same 7/30 as at 6 per minute (section 3.2), but p95 rose from 57.1 s to 174.5 s: under overload the SLO count is saturated, and latency is the measure to read. |
| 20-minute soak (4 clients continuously sending short questions, 4K/8K documents, thinking) | 139 requests, 0 errors. Short-question TTFT p50/max 0.67/1.53 s, 4K documents 10.1/11.7 s, 8K documents 14.3/26.2 s. During the soak, engine resident set size (RSS, 36.38 GiB), VRAM on all four GPUs, available RAM (15.8–16.5 GiB) and swap did not change. No GPU throttling; maximum 60 °C. |
| Parking over the whole robustness run (engine log, after the 15:28 start) | 104 parks, 16 restores, 80 evictions (during the soak it stayed at the 8-conversation limit and evicted the least recently used conversation). Park median 110 ms, longest 1.09 s. At most 5.8 GiB in use at once. Engine RSS peaked at 37.06 GiB. Available RAM was at least 15.08 GiB, above the 8 GiB floor, so parking never stopped for lack of memory. rpool physical reads over the whole run: 0.28 GB. |
| [no-parking] 30-minute soak | 203 requests, 0 errors. Engine RSS, available RAM and VRAM did not change during the soak (server.py RSS was not monitored). |
| The original setup guide's three request templates (`examples/requests/`, streaming, once each after deployment) | `request-normal.json` finished in 57 s (2,452 tokens) and `request-deep.json` in 143 s (6,043 tokens). `request-fast.json` was cut off at 46 s by `max_tokens` 2048 (`finish_reason: length`; thinking used only 106 chunks, and the answer did not fit in the rest of the 2048 tokens). Raw output: `results/example-requests.txt` |
| Fault injection: kill -9 engine ([deployed*]; `fault-injection-journal.txt`) | The engine was killed after the service became ready at 12:57:40; the time of the kill was not kept, and the journal has no line for it. The watchdog's next check, at 12:59:48, sent `POST /v1/load`. The engine was running again at 13:07:17, 449 s later, and the first request succeeded at 13:07:36. systemd restart count 0. Detection depends on when the timer fires: up to about 2 minutes. |
| Fault injection: kill -9 server.py ([deployed*]) | systemd immediately cleaned up the leftover engine and restarted the service after 120 s. Ready about 576 s after the kill. Restart count 1. |

### 3.6 0.1.44.1 A/B test and ZFS read-ahead tracing [A/B]

Order: 0.1.43.1 → 0.1.44.1 → 0.1.44.1 with the read-ahead hint on → 0.1.44.1 → 0.1.43.1, each on a fresh start (18:37–21:37; tags `v431-*`, `v441-*`, `v441pf`; `bench/ab_versions.sh`): 0.1.43.1 as the production service, 0.1.44.1 from a separate tree in a temporary systemd unit with the production unit's limits. 0.1.44.1 was built locally from tag `v0.1.44.1` (`296b59d`) with setup.py's flags (CUDA 12.9.1, sm_60, `STRATA_EXPERIMENTAL_SM60=ON`) in `~/dev/strata-0.1.44.1`, with its own `serve/server.py`; the config differs only in exe, cwd and log. Full table: `results/AB-0.1.44.1.md` (generated by `bench/ab_summary.py`).

| Item (both versions with `--no-ple-prefetch`) | 0.1.43.1 (two runs) | 0.1.44.1 (two runs) |
|---|---|---|
| Short-prompt total throughput, 1/4 users, tok/s | 34.0/100.3, 33.4/100.3 | 35.6/100.2, 33.9/100.6 |
| ~4.4K prompt prefill (median of 3 runs) | 392.3, 391.4 | 394.8, 397.0 |
| ~32K prompt prefill (median of 3 runs) | 778.0, 782.0 | 784.1, 783.8 |
| New ~16K prompt prefill | 699.0–707.0, 699.8–706.6 | 703.7–708.3, 703.8–709.2 |
| 4×16K arriving at the same time, last-user TTFT | 94.6, 94.5 s | 94.0, 94.2 s |
| Poisson 0.1/s, strict SLO met | 27%, 33% | 27%, 27% |
| TTFT of a short question inserted during a 28K read | 35.8, 35.8 s | 35.7, 35.7 s |

- The two versions are within about 1% of each other on throughput, prefill, 4×16K and the inserted request's TTFT, except one-user short prompts, where two restarts of the same version already differ by 1.7 tok/s. 0.1.44.1 is faster in both runs only for ~4.4K prompts, by 0.6–1.4%. Four-user thinking per-user decode was 31.5 and 30.7 tok/s on 0.1.43.1 and 30.8 and 30.5 tok/s on 0.1.44.1, the same size as the difference between restarts of one version (0.8). Poisson counts and latency differ a lot between restarts of one version (0.1.43.1 met 8 and 10/30; 0.1.44.1 p95 90.4 and 60.9 s), so no difference between versions can be seen. Upgrading for speed is not worth it.
- 0.1.44.1's change "a short request joins at the next chunk" (#2170) shows no effect in this configuration: a short question still waits until the whole 28K is read (35.7 s).
- 0.1.44.1 with the read-ahead hint on (without `--no-ple-prefetch`): new 16K prompts ran at 458–504 tok/s with TTFT 31.8–35.0 s, and each read 16.4–18.3 GB from disk. With the hint off: 704–709 tok/s, 22.6–22.8 s, 0.00 GB. ARC prefetch misses × 128 KiB match the disk reads (for example, 125,028 × 128 KiB = 16.39 GB).
- Tracing (`bench/traced_probe.sh`) confirmed the mechanism. During three 16K prompts (47,885 tokens), the engine called `madvise` 768,048 times (16 per token), and `vfs_fadvise`, `zpl_fadvise` and `dmu_prefetch` were each called about 767,900 times. Each call spent 92 µs inside `zpl_fadvise` on average, about 24 s per prompt (partly overlapped with GPU compute; TTFT rose by 9–12 s). With read-ahead off, the engine made 64 `madvise` calls per prompt, and `zpl_fadvise` was called 3 and 5 times system-wide in the two runs (`results/AB-0.1.44.1.md`, traced pass).
- With read-ahead on, the engine's own `the OS read X MB` showed only 0.59–1.05 GB per prompt against 16–18 GB of disk reads, because ZFS's own threads do most of the read-ahead. The engine log does not show the scale of the problem (`results/v441pf-engine-lines.txt`, original lines 312–338).
- Short-prompt decode with the hint on was measured in one run only, within restart noise (`results/AB-0.1.44.1.md`); no conclusion is drawn.
- In the 2nd arm, `perf` did not get the SIGINT that sudo forwarded and hung for 26 minutes (the probe ended at 19:47:03, `ple_prefetch_probe.jsonl`; the arm at 20:13:13, `ab-v441-driver.log`), so that arm's ftrace counts include idle time; the data is complete. The tooling now signals `perf` directly.

### 3.7 A/B test of 0.1.45 and the skip-hint-when-locked patch [A/B]

2026-10-17 19:28–22:45, 12 fresh starts: 8 on ZFS and 4 with the PLE table copied to ext4. The patch adds one line, `if (impl_->locked) return;`, in the POSIX branch of `gather_batch` (`~/dev/strata-0.1.45-locked`, branch `ple-skip-hint-when-locked` of the fork `thc1006/Strata`, commit `c1c9e57`). It went upstream on 2026-10-17 23:22 as https://github.com/Niko1221/Strata/pull/2918, was amended on 2026-10-18 to `25f40ed` (same engine code, a new test; `~/dev/strata-0.1.45-locked` is at `25f40ed`) and rebased on 2026-10-20 onto upstream's main (`9ae93f7`, 0.2.1 with the 0.1.46.1 hotfix) as `7114105` with the same patch (`results/pr2918-selftest-021-20261020.txt`). The PR text, its tests and builds are in `docs/upstream/pr-2918.md`. Every request used a fixed nonce, so every run received the same bytes: for all 17 request types the answers were bit-identical across all runs, and all 141 prompt reads were 0 reused (the `strata serve: prompt` lines of `results/z-*-engine-lines.txt` and `results/e-*-engine-lines.txt`). Summary: `results/AB-0.1.45-ple-hint.md`.

| ZFS, 16K prompt (two starts each) | prefill tok/s | Disk reads per prompt | `madvise` calls over 3 prompts |
|---|---:|---:|---:|
| 0.1.45 default (hint on) | 466–479 | 17.2–18.4 GB | 767,936 |
| 0.1.45 `--no-ple-prefetch` | 685–705 | 0.00 GB | 192 |
| Patch default | 685–706 | 0.00–0.01 GB | 192 |

- The patch default and `--no-ple-prefetch` were within 0.3% of each other, prompt by prompt. Decode was 0.3% lower, but the patch build with the flag was just as low, so the patch did not cause it. Whether the gap comes from building the two checkouts separately was not verified.
- 0.1.45 default decode was 2–3% slower (0.977), and prefill was 0.675–0.678×.
- On ext4 the hint was still issued (767,936 times), but no difference in speed, decode or kernel time could be measured: the four runs were within 0.5% of each other, prompt by prompt. (This ext4 was a loop file on ZFS, `~/dev/ple-ext4.img`, deleted after the test.) On this machine the cost comes from ZFS.
- Where the time goes (the kprobes record only the engine's own threads): the thread that issues the hints was in `zpl_fadvise` 768,192 times. It slept only 3 times and was preempted 8,553 times. Waiting for ARC eviction took only 1.0%. Whole-machine kernel time per prompt rose from 3–5 s to 37 s, of which at least 15.8 s was in the ZFS read-completion threads (`z_rd_int`) that complete the prefetch reads. The time was spent on the CPU, including that read completion, not waiting for the disk or for ARC eviction. Memory bandwidth was not measured.
- A harness that counts calls with `-Wl,--wrap=madvise`: with the table locked, 15/255/11,199 calls before the patch (1/16/700 tokens) and 0 after (`results/ple-hint-count-harness.txt`). When the table is not locked, or locking fails (`prlimit --memlock=0`), the hints are issued as before; the mmap runs confirmed it for a table that is not locked, but their page cache was not controlled, so their speed cannot be compared. The PR's first test passed without the patch too; where the table can be locked, the amended test fails without the check and with six other broken versions of the engine code (`results/pr2918-test-mutants.txt`; the hint-count contract: `results/pr2918-contract.txt`). The HIP and SYCL engines were built but not run, since this machine has no AMD or Intel GPU (`results/pr2918-builds.txt`).
- Problems during testing. The first version of the probe could report negative `z_rd_int` CPU, could not show the engine's user and sys times per request, and hid small differences; a revised version fixed these. The first ext4 start failed because `--ple-gguf` in another directory was taken as a duplicate shard 2; it was rerun with a symbolic link. At 21:42 the production service was stopped while it was loading, which left it failed. The watchdog restarted it at 22:13, after its 30-minute cooldown; when that failed, systemd retried at 22:15 under `Restart=on-failure`. The preflight blocked both, because the test engine held `:8080` (`results/journal-20261017-2140.txt`).

### 3.8 Strata 0.1.45 and its two switches [engine experiments]

The engine experiments measured 0.1.45's switches with this configuration, in a transient unit with the service's
limits (`docs/engine-experiments.md`). Against 0.1.45 without them, two runs each in the order K0, K1, K1, K0
(`results/SCREEN-combo.md`): prompts were read 17.4% (1.2K), 10.7% (4K) and 9.1% (16K) faster; while a 16K prompt was
read beside three decoders, their longest stop was 0.55 s instead of 22.73 s (the means of the runs' medians; in the
rounds, 0.50–0.61 s against 22.71–22.75 s: `results/engine-screen.jsonl`), and the joining prompt's first token came
1.1% later; four users with 1.5K prompts got 2.7% more tokens per second; one user's decode did not change (+0.5%).

### 3.9 GSQ-RCO IQ3_S [A/B]

Decision 0004's runs, on 0.1.45 with this configuration: two runs of each model in the order IQ3_XXS, IQ3_S, IQ3_S, IQ3_XXS, each after a restart, then a robustness run on IQ3_S (`results/AB-iq3s-ab3.md`). Every run was clean. Against IQ3_XXS, IQ3_S read 4.4K-token prompts 13.16% slower and 32K-token prompts 9.61% slower, served 1.92%, 6.47% and 6.53% fewer tokens per second with 1, 2 and 4 clients, and a 16K prompt that joined three decoders got its first token 9.62% later. The engine reports 98% of the experts resident in VRAM, against 100%, and 5.46 GiB of experts page-locked in RAM, against 1.12 GiB (`results/AB-iq3s-ab3.md`). In the runs the least free VRAM was 1094 MiB against 1938 MiB and the least MemAvailable 16.8 GiB against 21.6 GiB. In the robustness run every card kept at least 1094 MiB free and MemAvailable stayed at or above 17.08 GiB.

---

## 4. Problems found in this deployment, by impact

1. ZFS × PLE read-ahead hint (not mentioned in the original setup guide): `PleTable::gather_batch` in `src/kernels/ngram.cpp` (about lines 554–577 in 0.1.43.1) calls `madvise(MADV_WILLNEED)` for every PLE row. It checks only `g_ple_prefetch`, not whether the table is already locked in RAM. Both prefill and the MTP verification window of decode call it. On this machine the impact is mainly on prefill (decode, R1 → R1b: 4-user total 84.6 → 85.9, one user 33.0 → 34.8 tok/s). With only `--ple-io ram` set, about 406 GB was read from the disk that holds rpool within 20 minutes during R1. That figure is for the whole `sda`, which also holds the 2 GiB swap partition (SwapTotal in `deploy-0.1.45-20261019.txt`, `sda2` in `ple_prefetch_console.txt`). There were only 1,263 ARC demand misses, so the reads came from read-ahead. R1b, which added only `--no-ple-prefetch` and had similar available memory (about 21 GiB), read 0.049 GB over its whole run (also the whole `sda`), so the reads were not swap. Section 3.6 confirmed the mechanism with perf and ftrace: Linux passes WILLNEED to `vfs_fadvise`, and OpenZFS's `zpl_fadvise` calls `dmu_prefetch` whether or not the page cache already has the data; the prefetch reads the whole 128 KiB record from disk when the record is not in the ARC. `zpl_fadvise` is the same in 2.3.4 and 2.4.0 as in 2.2.2. [2.4.4](https://github.com/openzfs/zfs/blob/zfs-2.4.4/module/os/linux/zfs/zpl_file.c) and master rewrite it, but still start a prefetch for every `MADV_WILLNEED` hint whether or not the page cache has the data; master does it through `dmu_prefetch_user`, which limits the prefetch bytes in flight (checked 2026-10-18).
   - Evidence: `R1-pleram-disk.csv`, `R1b-noplepf-monitor-all.json`; 32K prefill 526–542 → 767–768 tok/s (`stress.jsonl`); 16K probe: R1 407–440 tok/s (engine log), deployed configuration 692–706 tok/s with 0.002–0.144 GB of physical reads on the rpool partition (`sda4`) (`ple_prefetch_probe.jsonl`, reproducible). The 15.5–17.8 GB read per 16K prompt (whole `sda`) exists only in the terminal output of the probe run by hand at the time (`ple_prefetch_console.txt`).
   - Upstream (checked 2026-10-16): in #2281 (2× RTX 3090, `--ple-io ram`, 0.1.43.1 decode −20%), the maintainer suggested `--no-ple-prefetch` on 10-14, the reporter confirmed on 10-15 that it recovers decode and prefill, and the issue was closed that day with "use this flag"; the engine code was not changed. A follow-up comment on 2026-10-16 21:42 (https://github.com/Niko1221/Strata/issues/2281#issuecomment-6216816902, text in `docs/upstream/issue-2281-comment.md`) gave the ZFS read volume on 0.1.44.1, the tracing results and the source locations, and suggested skipping the per-row hint when the table is locked; it did not ask to reopen the issue. On 2026-10-17 the reporter, who also keeps the model on ZFS (#2571), [replied](https://github.com/Niko1221/Strata/issues/2281#issuecomment-6228514958): "I believe it's a ZFS arc issue competing for disk I/O and memory bandwidth." The maintainer has not responded yet. 0.1.45, released the same day, did not change the hint (`ngram.cpp` L567–L590 still checks only `g_ple_prefetch`), so 0.1.45 still needs `--no-ple-prefetch`. The proposed fix is PR #2918 (section 3.7).
2. Cold-cache cost of PLE mmap on ZFS: "run the same text 3 times and take the median" measures warm pages and hides the cost (R0 cold 126/217 against warm 389/569 tok/s).
3. GPU3 is the decode bottleneck: the last-light split improved it.
4. Pipelined admission freeze on 0.1.43.1: with `--batch-groups > 1`, a new prompt is read in one go, everyone else stalls completely, and no request can jump the queue (`include/strata/program/batch_read.hpp`). Both alternatives tried then were worse:
   - `--batch-groups 1`: the longest pause of a decoding user dropped to 8.2 s, but 4-user decode was −40% and long inputs were slower (the last user of 4×16K went from 102.6 s to 263.2 s).
   - Upstream PR #2050 (piece 8192, the only value tested; built separately in `~/dev/strata-pr2050`, not adopted): the pause dropped to about 14.5 s and short questions could jump the queue (14.0 s). But TTFT of new long inputs rose by 40–77% (only 5% for the first 8K), and one-user 32K prefill was −31%. Poisson strict SLO 9 → 6/30. Request by request, 7 results flipped and the TTFT median was 14.4 s higher (R2's A/A: 1 flip, +0.04 s), beyond restart-to-restart noise.

   Since 2026-10-19, 0.1.45's `STRATA_PIPED_CONCURRENT=1` reads a prompt on its own thread while the others decode: while a 16K prompt was read beside three decoders, their longest stop was 0.50–0.61 s with IQ3_XXS (section 3.8) and 0.64–0.88 s with IQ3_S in its screen (section 5). On 0.1.45 with both switches, a short question sent 1 s into a 28K read got its first token after 6.66–6.74 s with IQ3_XXS and 7.84–7.86 s with IQ3_S (`short question TTFT` in `results/ab3-*-perf.log`), against 35.8 s with IQ3_XXS on 0.1.43.1 (section 3.2).
5. Multi-turn conversation cache lost under slot competition: solved by conversation parking (section 3.4).
6. The model sometimes outputs Simplified Chinese: when strict Traditional Chinese is required, ask for it explicitly in the system prompt.

---

## 5. Known limits to share with lab members

- Since 2026-10-20 the service runs GSQ-RCO IQ3_S instead of IQ3_XXS: its answers agree with the 8-bit reference (Q8_0) more often, and it reads prompts 9.61–13.16% slower and gives 1 to 4 clients 1.92–6.53% fewer tokens per second (section 3.9).
- Since 2026-10-19 (`STRATA_PIPED_CONCURRENT=1`), a long document no longer stops other users for the whole read. With IQ3_S, three users generating beside a 16K document stopped for 0.64–0.88 s in the screen's four rounds, and the document's own first token came 8.4% later than with neither of 0.1.45's two switches (`results/SCREEN-iq3s-k1-vs-k0.md`; the rounds are in `results/engine-screen.jsonl`). In decision 0004's `ab3` runs on IQ3_S, the longest pause was 1.88 s, at 12 per minute (`results/ab3-iq3s-robust-robustness.log`). A short question sent 1 s into a 28K read got its first token after 7.84–7.86 s (`results/ab3-iq3s-*-perf.log`), against 35.8 s with IQ3_XXS on 0.1.43.1 (section 3.2). A request answered while a document is read can get a different answer than it would alone, and a different one each time.
- When four users send long documents at the same time, the last one waited about 1.6–2.4 minutes on 0.1.43.1 (94 s at 16K each, 143 s at about 27K each), and waits about 1.7–2.5 minutes with IQ3_S on 0.1.45 (100.6 s at 16K each, `results/AB-iq3s-ab3.md`; 151 s at about 27K each, the `worst` lines of `results/ab3-iq3s-1-perf.log` and `results/ab3-iq3s-2-perf.log`).
- Requests that send no sampling settings are sampled with the model card's settings since 2026-10-19 (0.1.45's `serve/server.py`): temperature 1.0, top_p 0.95 and top_k 20 with thinking; temperature 0.7, top_p 0.8, top_k 20 and presence_penalty 1.5 without. Before, they were decoded greedily. Send `temperature: 0` for greedy answers.
- Capacity: with the same document-heavy mixed load (short questions, 2K/8K/16K documents), on 0.1.43.1 with IQ3_XXS ([deployed]; [deployed*] at 6 per minute; `results/SUMMARY.md`), the strict SLO met rate was 73% (22/30) at 3 requests per minute, 23% (7/30, TTFT p95 57 s) at 6 per minute, and 23% (7/30, TTFT p95 175 s) at 12 per minute. Above capacity, queuing grows, but no test had errors. Actual capacity depends on document length and on how many requests arrive at the same time.
- Slot limit 4: from the 5th user on, requests queue; they do not fail.
- Memory needed to start: the preflight starts the engine only when available memory plus the part of the ARC that can be freed is at least 42 GiB, and waits at most 25 minutes for it. Before the 2026-10-19 start (IQ3_XXS on 0.1.45) there was 58 GiB; after loading, about 27 GiB remained (engine RSS 31.0 GiB, of which the locked PLE table was 26.8 GiB; `results/deploy-0.1.45-20261019.txt`). IQ3_S takes more: of the same 58 GiB, 22.4–23.1 GiB remained after each of its six restarts on 2026-10-20 (`results/ab2-iq3s-*-switch.txt`, `results/ab3-iq3s-*-switch.txt`), so it took up to 35.6 GiB. Until 2026-10-22 the gate was 36 GiB, set for IQ3_XXS: a start at the gate would have left about 5 GiB with IQ3_XXS and only 0.4–1.1 GiB with IQ3_S, below the 6 GiB parking floor. At 42 GiB, a start at the gate leaves at least about 6.4 GiB with IQ3_S.
- `max_tokens` 2048 in `examples/requests/request-fast.json` is not enough for long answers: the answer to the template's question gets cut off. `max_tokens` covers both thinking and the answer. For a complete long answer, use 4096 or more (the template was left unchanged).
- The API has no key, and the service listens only on 127.0.0.1. Any account that can read the config can see the key, so a key adds little while the service is used only on this machine. *Set a key before exposing the service through a reverse proxy or Tailscale*:
  - Add `"api_key": "<long random string>"` to `~/dev/strata/strata-iq3_xxs.json`, then restart. The configuration tool keeps this field, and the watchdog reads it. `scripts/check-deployment.sh` needs it as `STRATA_KEY=<key>`.
  - After that, every client must send `Authorization: Bearer <key>` (or `x-api-key: <key>`). `/health` does not need it.
  - Keep the key only in the live config, never in the repo. `scripts/check-drift.sh` leaves it out when it compares. To update the `config/` snapshot, see [Changing the configuration](runbooks/change-config.md), step 5.
- Start the service only with systemctl. Run by hand, the engine cannot lock the PLE table (memlock limit about 8 GiB: `ple-hint-count-harness.txt`), and `./setup.sh` changes `--prefill 2048` to `auto`.

---

## 6. Test method and confidence in the results

- Method: based on common practice for measuring LLM serving (vLLM bench, GuideLLM, the checklist in arXiv 2507.09019). Closed-loop concurrency sweeps. Open-loop Poisson arrivals (fixed seed; every configuration receives the same request sequence). TTFT, time per output token (TPOT), longest pause (the metric of upstream #2050/#2326). SLO goodput with the SLO fixed before testing. A/A restart controls.
- Noise: in the configuration comparison, only R2 had an A/A run (for the version A/B, see section 3.6). After the A/A restart, throughput differed by <1%, but latency metrics differed a lot: in the same scenario, one decoder's longest pause was 37.2 against 22.9 s, Poisson p95 65.9 against 57.0 s, a single request's TTFT differed by up to 122.5 s, and goodput by 1 request. Every other configuration ran once, so Poisson goodput differences of 1–3 requests are not statistically meaningful. The "loose SLO" in the reports was added after the fact and is for reference only.
- Order of the configuration comparison (start times from the engine log): R0 baseline (06:09) → R1 PLE RAM → R1b read-ahead hint off → R3 batch-groups 1 → R2 layer split → R4 PR #2050 → R2 A/A (10:14) → batch-groups auto, no parking (10:52; `final-cold` prefill/decode same as R2, `final` robustness; the log shows the same as 4 groups, and auto did not run the full performance suite) → parking added (12:12 multi-turn conversations; 13:10 performance suite [deployed*]) → min-free 8192 added (13:43) → prefill 4096 (14:21)/1024 (14:55) → [deployed] robustness (15:28).
- Monitoring: every 2 s, `bench/monitor.py` recorded VRAM, utilization, clock, power, temperature and throttling for all four GPUs, plus RAM, swap, ARC, physical disk reads and (added later) per-CPU utilization. It runs `nvidia-smi` every 2 s, which adds a small load. R0 and R1 had no disk columns; R1 had a separate whole-`sda` disk monitor (`R1-pleram-disk.csv`). From R1b until the A/A run ended at 10:46 (`R2-final-AA-perf.log`), disk reads were recorded for the whole `sda`; after that, for the rpool partition `sda4`, with the swap partition recorded separately. On ZFS, "the OS read X MB" in the engine log includes ARC hits, so it does not measure physical I/O (confirmed by an experiment on this machine).
- Poisson document requests: all documents of the same length are the same text (for example, the 10 requests of 2K are the same passage), but each request starts with a random nonce, so no request hits a prefix cache left by another. In the engine log, the first line of every Poisson document request is `0 reused`. Lines with "reused" during Poisson runs are the same request handed over between the solo and batch paths (the line before is `slot N takes/gave back M tokens`).
- The multi-turn conversation test has 6 clients competing for 4 slots, 2 of them sending short requests continuously. This competition was created on purpose; the level of competition in real use is unknown.
- Adversarial review: six rounds of independent review of the configuration tool, the load-test tool, the system changes and this document (rounds 2 and 3 verified earlier fixes, rounds 4 to 6 checked this document line by line against the raw data, round 6 also the quotes from upstream), and on 2026-10-18 a seventh round that recalculated the numbers from the raw files. Fixed in the tools and the system setup: truncated streams counted as successes, the admission window measured wrongly, wrong percentiles, the watchdog using up the start limit and causing a permanent outage, the watchdog script being modifiable by ordinary users, the failure cooldown going negative through a mawk overflow, command injection into curl `-K` through the API key, and the preflight letting a start proceed when it could not read its data. Also fixed: configurations and ranges labeled wrongly in this document. After round 4, batch consistency, robustness and the soak were retested on the deployed configuration. Rounds 5 to 7 also corrected statements in this document, among them the lower prefill numbers, first taken for restart noise and in fact parking time, a claimed prefix-cache hit in Poisson, R1's 406 GB (whole disk, not the rpool partition), and the PR #2050 and 0.1.44.1 conclusions; round 7 also fixed the p50 of the first 6 Poisson runs in `SUMMARY.md`.
- Not done: full machine reboot (needs a maintenance window); 128K context; PR #2326; other piece values for PR #2050; CPU workers 4/6/8; VRAM reserve of 1024 MiB or below; throughput with long Traditional Chinese documents; long inputs with thinking on.
