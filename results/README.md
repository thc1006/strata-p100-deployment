# Test results

Raw output of all tests, grouped by tag (the start of the file name). The `deploy/...` paths in the files are the old locations from before the 2026-10-18 directory reorganization; the contents are kept as they were. Only the host name was replaced with `<host>` before publication. In the `pr2918-*` files, `<work>`, `<rocm>` and `<oneapi>` stand for temporary folders, and in `deploy-sh-mutants-20261018.txt`, `switch-model-mutants-20261020.txt`, `run-robustness-mutants-20261020.txt` and `journal-window-mutants-20261020.txt`, `<tmp>` does (`<worktree>` is the checkout the mutants were copied from).

The long-document requests are taken from Strata's documentation (`docs/*.md` and `README.md`). Some files (for example `*-trace.txt` and `ple_hint_probe.jsonl`) contain excerpts of these documents or of model answers. `ple_hint_count.cpp.txt` and `pr2918-contract.cpp.txt` contain code copied from Strata's `src/ngram/ple_reader_test.cpp`, and `pr2918-mutants.diff.txt` contains excerpts of its `src/kernels/ngram.cpp`. Strata is licensed under the MIT License, Copyright (c) 2026 Niko1221 and the Strata contributors; the full license text is in [`LICENSE-strata.txt`](LICENSE-strata.txt).

| Tag | Date | Contents | Summarized in |
|---|---|---|---|
| `baseline-*`, `four_user.jsonl` | 10-16 | R0: the baseline configuration of the original setup guide (the R0 four-user concurrent test had no tag yet at the time, so its file is `four_user.jsonl`) | `SUMMARY.md`; deployment record, section 2 and section 4 point 2 |
| `R1-pleram-*`, `R1b-noplepf-*`, `R2-*`, `R3-groups1-*`, `R4-pr2050-*` | 10-16 | Configuration comparison: per-layer embedding (PLE) locked in RAM, read-ahead hint off, layer split (including A/A restarts), `--batch-groups 1`, upstream PR #2050 | `SUMMARY.md`; for the basis, see deployment record sections 2 and 4 |
| `final-*`, `exp-prefill*`, `deployed-*` | 10-16 | Performance, robustness and prefill chunk comparison after deployment | `SUMMARY.md`; deployment record section 3 |
| `conversation_cache.jsonl`, `parking_test*` | 10-16 | Conversation parking tests | Deployment record section 3.4 |
| `batch_test*`, `sm60_dp4a_check.txt` | 10-16 | Batch consistency; numerical check of the sm_60 kernels | Deployment record section 3.5 |
| `latency.jsonl`, `stress.jsonl` | 10-16 | Per-request details of the latency and stress tests (shared by all groups, told apart by the `tag` field) | Deployment record section 3 |
| `ple_prefetch_*` | 10-16 | Probes of the PLE read-ahead hint on ZFS: one record for `deployed-noplepf`; the other 10 belong to the 0.1.44.1 A/B | Deployment record section 4 and section 3.6; `AB-0.1.44.1.md` |
| `v431-*`, `v441-*`, `v441pf-*`, `ab-v441-driver.log` | 10-16 | A/B of 0.1.43.1 against 0.1.44.1, and 0.1.44.1 with the read-ahead hint on | `AB-0.1.44.1.md`; deployment record section 3.6 |
| `z-*`, `e-*`, `ab-v045*-driver.log`, `ple_hint_probe.jsonl`, `ple-hint-count-harness.txt`, `ple_hint_count.cpp.txt`, `ple-selftest-*.log` | 10-17 | A/B of 0.1.45 and the skip-hint-when-locked patch (upstream PR #2918); `z-` on ZFS, `e-` on an ext4 copy | `AB-0.1.45-ple-hint.md`; deployment record section 3.7 |
| `pr2918-*` | 10-18 | Checking the PR #2918 test: a hint-count harness (`pr2918-contract.cpp.txt`) and its output, the test against broken versions of the engine code (`pr2918-mutants.diff.txt`), and the CUDA, HIP and SYCL builds of the amended PR | Deployment record section 3.7 |
| `deploy-sh-mutants-20261018.txt` | 10-18 | `tests/test_deploy.py` against 94 broken copies of `scripts/deploy.sh`, and the script that makes them | Decision record 0003 |
| `switch-model-mutants-20261020.txt` | 10-20 | `tests/switch_model_stubs.sh` against 51 broken copies of `bench/switch_model.sh`, the tests of the concurrent-read report against 10 broken copies of `bench/ab_model_run.sh` and `bench/ab_model_summary.py`, and the scripts that make them | Decision record 0004 |
| `run-robustness-mutants-20261020.txt` | 10-20 | `tests/run_robustness_stubs.sh` against 8 broken copies of `bench/run_robustness.sh`, among them the script before it wrote the host name as `<host>`, and the script that makes them | `CHANGELOG.md`, 2026-10-20 |
| `journal-window-mutants-20261020.txt` | 10-20 | `tests/ab_model_run_stubs.sh` and `tests/ab_engine_stubs.sh` against 10 broken copies of the journal windows of `bench/ab_model_run.sh` and `bench/ab_engine.sh`, among them the scripts before they read the windows from an entry of their own, and the script that makes them | `CHANGELOG.md`, 2026-10-20 |
| `journalctl-after-cursor-20261020.txt` | 10-20 | systemd 249's `journalctl --after-cursor` with a filter against `--cursor` at an entry of one's own: the start of `ab2-iq3s-robust` read both ways, three entries written with `logger` read back, and the script that ran it | Decision record 0004; `docs/engine-experiments.md` |
| `deploy-sh-first-run-20261018.txt` | 10-18 | The first deploy with `scripts/deploy.sh` against GitHub, in a scratch clone, and the script that ran it | `docs/runbooks/deploy.md` |
| `deploy-0.1.45-20261019.txt` | 10-19 | The switch to Strata 0.1.45 with its two switches: the window's steps, `scripts/check-drift.sh`, the engine's start-up lines, a prompt read beside a decoding request, `scripts/check-deployment.sh` | `docs/decisions/0005-strata-0.1.45-group-sort-concurrent-read.md`, `docs/runbooks/upgrade-strata.md` |
| `sycl-build-sh-exit-20261018.txt`, `ple-selftest-shared-tmp-20261018.txt` | 10-18 | Two upstream problems found while building PR #2918: the exit status of `sycl/tools/build.sh`, and two runs of `ple_reader_test --selftest` that share one temp directory | Upstream bug reports in `docs/upstream/` |
| `sycl-build-sh-exit-20261020.txt`, `ple-selftest-shared-tmp-20261020.txt` | 10-20 | The same two problems re-tested on 0.2.0-beta, for reopening the two issues | Upstream bug reports in `docs/upstream/` |
| `pr2918-selftest-020-20261020.txt` | 10-20 | PR #2918 applied to 0.2.0-beta: its selftest at three memlock limits, and without its fix, for reopening the PR | `docs/upstream/pr-2918.md` |
| `engine-log-20261016-extract.txt`, `fault-injection-journal.txt` | 10-16 | Verbatim excerpts of the engine log (with the original line numbers); excerpts of the systemd journal | Deployment record, notes on the sources of the numbers |
| `setup-20261016.log` | 10-16 | Full output of `setup.sh` | `docs/runbooks/rebuild.md` step 5 |
| `check-drift-full-20261018.txt` | 10-18 | Full output of one `scripts/check-drift.sh --full` run (including model file hashes, 590 s) | `docs/runbooks/rebuild.md` step 7 |
| `service-load-times.txt` | 10-16–10-18 | Load time of each start of the production service (copied from the journal on 2026-10-18) | Deployment record section 1 |
| `example-request-*.sse.gz`, `example-requests.txt` | 10-16 | Raw streamed output (gzip) of the three sample requests of the original setup guide, and statistics recalculated from it | Deployment record section 3.5 |
| `host-units-before-20261018.txt` | 10-18 | The state of every systemd unit `config/host-units.conf` turns off, before the change | `docs/runbooks/operations.md` |
| `iq3xxs-1-*` | 10-18 | The standard perf set (`bench/run_profile.sh`) on the deployed IQ3_XXS engine, up for 20 hours, with the 8 GiB ARC and RAM floors, measured before the A/B tools were reviewed: not part of the IQ3_S A/B | Decision record 0004 |
| `c0-*`, `c1-*`, `c2-*`, `c3-*`, `m0-*`, `m1-*`, `k0-*`, `k1-*`, `engine-screen.jsonl`, `ab_engine-*.log`, `SCREEN-*.md` | 10-19 | Engine experiments on 0.1.45 (`bench/ab_engine.sh`): per run its arm and after records, the engine's per-request lines and the screen's output, and from the `m` runs on every answer's text (`<tag>-texts.jsonl`) and each GPU's use once a second (`<tag>-gpu.txt`); the screen's per-request records (`engine-screen.jsonl`, told apart by `tag`); the driver's log per screen; the summaries `bench/ab_engine_summary.py` generated | `docs/engine-experiments.md` |
| `ab2-*`, `AB-iq3s.md` | 10-20 | Decision 0004's A/B: four runs of the perf set in the order ABBA (`bench/ab_model_run.sh`), each with the record of the restart before it (`bench/switch_model.sh`), the summary (`bench/ab_model_summary.py`, exit 1: only the speed criterion fails), and the robustness run on IQ3_S (`ab2-iq3s-robust-*`; its log is not here, it holds the host name). Not used: the wrapper's journal windows could lose a run's first line | Decision record 0004 |
| `ab-iq3xxs-1-*`, `ab-iq3s-1-switch.txt*` | 10-20 | The first start of decision 0004's runs, before the 1800 s engine-ready limit: one IQ3_XXS run, then the switch to IQ3_S that server.py ended at 900 s, and the switch back; not part of the A/B | Decision record 0004 |
| `iq3s-memory-budget-20261019.txt`, `iq3s-model-files-20261019.txt` | 10-19 | Where each candidate quantization's experts would live (`bench/model_memory_budget.py`, rerun of the 10-18 computation), and the candidates' file sizes (local shards; Hugging Face's listing at the pinned revisions) | Decision record 0004 |

Not in git: `*.perf.data` (raw kprobe recordings, several hundred MB each; see `.gitignore`). The parsed results are in `z-hint-2-m-mem.txt` and `e-hint-1-m-mem.txt`.

The commands below regenerate the summaries. They were rerun on 2026-10-18. Apart from the line that names the generating script, which now shows the new path, 6 p50 values in the Poisson table of `SUMMARY.md` changed: for R1 to R2 A/A, the upper median was stored at the time, and `report.py` now recalculates them from the per-request data with nearest-rank, the same as for all other groups. All other numbers are unchanged.

```bash
python3 bench/report.py results > results/SUMMARY.md
python3 -B bench/ab_summary.py v431-1 v441-1 v441pf v441-2 v431-2 > results/AB-0.1.44.1.md
python3 -B bench/ab_v045_summary.py \
  "z-off-1=ZFS release --no-ple-prefetch=z-off-1" "z-lk-1=ZFS branch=z-off-1" "z-hint-1=ZFS release default=z-off-1" \
  "z-lk-2=ZFS branch (2nd)=z-off-1" "z-off-2=ZFS release --no-ple-prefetch (2nd)=z-off-1" \
  "z-lkmm=ZFS branch --ple-io mmap=z-off-1" "z-lkoff=ZFS branch --no-ple-prefetch=z-off-1" \
  "z-hint-2=ZFS release default (2nd)=z-off-1" "e-off-0=ext4 release --no-ple-prefetch=e-off-0" \
  "e-hint-1=ext4 release default=e-off-0" "e-off-1=ext4 release --no-ple-prefetch (2nd)=e-off-0" \
  "e-hint-2=ext4 release default (2nd)=e-off-0" > /dev/null      # writes results/AB-0.1.45-ple-hint.md directly
```
