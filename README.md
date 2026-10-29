# Strata on 4× Tesla P100: Qwen3.8-Flash-Next deployment and benchmarks

Last updated: 2026-10-29.

This repository holds the configuration, operations scripts and measurements of a production deployment of [Strata](https://github.com/Niko1221/Strata), an LLM inference engine. The service runs ISTA-DASLab's GSQ-RCO IQ3_S quantization of Qwen3.8-Flash-Next, a mixture-of-experts (MoE) model, on four NVIDIA Tesla P100 16 GB GPUs (Pascal, sm_60), in a VMware VM with a ZFS root file system. It offers OpenAI-compatible and Anthropic-compatible APIs on `127.0.0.1:8080` to the members of one lab.

## What the measurements show

All numbers but the QUANTS.md figures come from this machine. The [deployment record](docs/deployment-record.md) gives the method, the raw files and the limits of each one.

On Strata 0.1.43.1 with IQ3_XXS (the engine until 2026-10-19, the model until 2026-10-20):

- With the per-layer embedding (PLE) table locked in RAM (`--ple-io ram`), the engine still sends one `madvise(MADV_WILLNEED)` hint per PLE row, as 0.1.45 does. OpenZFS turns each hint into an ARC prefetch of the 128 KiB record that holds the row (the dataset's record size: `results/zfs-dataset-20261022.txt`), and reads the record from disk when it is not in the ARC. One 16K-token prompt read 15.5–17.8 GB from disk, and prefill ran at 407–440 tok/s. With `--no-ple-prefetch`, prefill ran at 692–706 tok/s ([section 4](docs/deployment-record.md#4-problems-found-in-this-deployment-by-impact)).
- Four users with short prompts reached 100.0 tok/s in total. One user decoded at 48.7–52.0 tok/s with multi-token prediction ([section 3.1](docs/deployment-record.md#31-one-user-and-short-prompts-deployed)).
- Moving the layer split from 12/24/36 to 13/26/38 raised four-user decode from 85.9 to 100.6 tok/s. IQ3_S runs 13/25/38 ([section 2](docs/deployment-record.md#2-how-the-final-configuration-differs-from-the-original-setup-guide)).
- Long prompts paused the other users. While a 16K-token prompt was read, users who were generating stopped for about 23 s. When four 16K-token prompts arrived at once, the last user waited 94.4 s for the first token ([section 3.2](docs/deployment-record.md#32-long-inputs-and-concurrent-users-the-main-p100-limit-deployed)).
- With 8 GiB of RAM for conversation parking, multi-turn conversations stayed cached while six clients competed for four slots. Cache reuse went from 0/16 to 16/16, and the median time to first token from 38.8 s to 20.2 s ([section 3.4](docs/deployment-record.md#34-conversation-parking)).

On Strata 0.1.45, deployed on 2026-10-19:

- While a 16K-token prompt was read, 0.1.45's concurrent prompt read kept the stop of three decoding users under a second: 0.50–0.61 s with IQ3_XXS ([section 3.8](docs/deployment-record.md#38-strata-0145-and-its-two-switches-engine-experiments)) and 0.64–0.88 s with IQ3_S in its screen (`results/SCREEN-iq3s-k1-vs-k0.md`; the rounds are in `results/engine-screen.jsonl`).
- GSQ-RCO IQ3_S against IQ3_XXS, four runs in the order ABBA and a robustness run ([decision 0004](docs/decisions/0004-iq3-s.md), `results/AB-iq3s-ab3.md`): IQ3_S read prompts 13.16% (4.4K tokens) and 9.61% (32K) slower, and served 1.92%, 6.47% and 6.53% fewer tokens per second with 1, 2 and 4 clients. It is deployed for its quality: it agrees with Q8_0 more often (top-1 87.69% against 84.46%, Strata's QUANTS.md at v0.1.45) and scores higher on ISTA-DASLab's model card.

## Current state of the service

| Item | Details |
|---|---|
| Service | `strata-q3.service`, 4 slots, 32K context each, no API key set |
| Engine | Strata 0.1.45 (commit `477ac13cb`) in `~/dev/strata`, built with CUDA 12.9 for `sm_60` only, with `STRATA_MMQ_GROUP_SORT=1` and `STRATA_PIPED_CONCURRENT=1` ([decision 0005](docs/decisions/0005-strata-0.1.45-group-sort-concurrent-read.md)) |
| Model | ISTA-DASLab's Qwen3.8-Flash-Next GSQ-RCO IQ3_S (revision `ed59f92`), since 2026-10-20 ([decision 0004](docs/decisions/0004-iq3-s.md)); IQ3_XXS before |
| Configuration | Output of `tools/configure_strata_q3.py --profile deployed --model iq3_s`, SHA-256 `e3e5a7b4…` |
| Upstream | ZFS read-ahead hint problem: [follow-up comment on issue #2281](https://github.com/Niko1221/Strata/issues/2281#issuecomment-6216816902) and the proposed engine fix in [PR #2918](https://github.com/Niko1221/Strata/pull/2918) (not merged, no review yet) |

## Repository layout

| Path | Purpose |
|---|---|
| `config/` | Desired state: the tool arguments in `profile.conf`, snapshots of the Strata config, the SHA-256 list of the deployed files, the venv's package list and the host units' states |
| `systemd/` | systemd units, a drop-in and the logrotate rule, installed to `/etc` |
| `scripts/` | Scripts that run on the host: the pre-start check, the wait for the model load, the watchdog, the status and drift checks, the host units' check, the installer for system files, the repository self-test and the deploy script |
| `tools/` | `configure_strata_q3.py`, which turns the config that Strata's `setup.sh` writes into the deployed configuration |
| `tests/` | Unit tests, and the stub harnesses of the `bench/` drivers |
| `bench/` | Load, latency and robustness tests, A/B tests of engine versions, switches and models, tracing, and the engine build script |
| `results/` | Raw test results ([index](results/README.md)) |
| `examples/requests/` | Example API requests |
| `docs/` | Documentation, listed below |
| `.github/workflows/` | CI: `test.yml` runs `scripts/test.sh --repo-only --harnesses --jobs 6` |

## Install and use

[Rebuilding the deployment from scratch](docs/runbooks/rebuild.md) gives, in its steps 2–5, the commands that built 0.1.43.1 with IQ3_XXS on 2026-10-16; no procedure for today's deployment has been run. To reinstall only the files in `/etc` and `/usr/local/sbin`, run `scripts/install-system-files.sh` (with `--dry-run` first to see what it would install).

```bash
scripts/check-deployment.sh        # service status; exits with 1 if it finds a problem
scripts/check-drift.sh             # whether the machine and the running service match the repository
scripts/test.sh                    # repository self-test; run it before each commit (CI also runs the stub harnesses)
scripts/deploy.sh                  # whether origin/main can be deployed here (runs its checks); --apply deploys it
sudo systemctl restart strata-q3   # the service is ready after about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`)
```

## Documentation

| Document | Contents |
|---|---|
| [Daily operations](docs/runbooks/operations.md) | Status, start and stop, logs, test requests, recovery from the failed state, preparation for long tests, rerunning the benchmarks |
| [Changing the configuration](docs/runbooks/change-config.md) | Changing Strata parameters, systemd units or the watchdog |
| [Deploying a commit from GitHub](docs/runbooks/deploy.md) | Bringing this checkout up to a newer `main` whose `test` workflow passed |
| [Rebuilding the deployment from scratch](docs/runbooks/rebuild.md) | CUDA 12.9.1, model download, Strata setup and the systemd install; the build steps are those of 0.1.43.1 with IQ3_XXS |
| [Evaluating and upgrading Strata](docs/runbooks/upgrade-strata.md) | Building a new release and comparing it with the deployed one |
| [System changes and how to undo them](docs/reference/system-changes.md) | Everything the deployment changed outside the repository |
| [Deployment record](docs/deployment-record.md) | The evidence for each setting, measured results, known limits and the test method |
| [Engine experiments](docs/engine-experiments.md) | Plans, criteria and results of the screens of engine switches and changes on 0.1.45, with IQ3_XXS and IQ3_S |
| [Decision records](docs/decisions/) | [0001: keep this repository separate from Strata](docs/decisions/0001-keep-ops-repo-separate-from-strata.md); [0002: repository layout](docs/decisions/0002-repository-layout.md); [0003: CI on GitHub, deployment pulled by this machine](docs/decisions/0003-ci-on-github-deploy-by-pull.md); [0004: IQ3_S, 6 GiB RAM floors and a 2 GiB ARC](docs/decisions/0004-iq3-s.md); [0005: Strata 0.1.45 with its grouped prompt sort and concurrent prompt reads](docs/decisions/0005-strata-0.1.45-group-sort-concurrent-read.md); [0006: the stub harnesses run in CI, and commits that change the checks are deployed by hand](docs/decisions/0006-harnesses-in-ci-checks-by-hand.md); [0007: the maintainer's account may use at most 6 GiB of RAM and no swap](docs/decisions/0007-memory-limit-of-the-maintainers-account.md) |
| Upstream posts | [Comment on issue #2281](docs/upstream/issue-2281-comment.md); [PR #2918](docs/upstream/pr-2918.md), [its comment](docs/upstream/pr-2918-comment.md) and [the reply on its rebase](docs/upstream/pr-2918-rebase-comment.md) onto main `9ae93f7` (0.2.1 with the 0.1.46.1 hotfix); bug reports [#2990](docs/upstream/issue-2990.md) (`sycl/tools/build.sh` exit status) and [#2991](docs/upstream/issue-2991.md) (`ple_reader_selftest` temp files). Upstream closed the PR and both issues in its tracker reset for 0.2.0-beta; they were reopened after a re-test on 0.2.0-beta |
| [Test results](results/README.md) | Index of the raw output files and the commands that regenerate the summaries |
| [Changelog](CHANGELOG.md) | Changes by date |
| [Contributing](CONTRIBUTING.md), [Security policy](SECURITY.md), [Instructions for coding agents](AGENTS.md) | How to report problems and send changes |

## Changing this repository

1. Change the repository first, then apply the change to the machine. For Strata settings, follow [Changing the configuration](docs/runbooks/change-config.md). For system files, use `scripts/install-system-files.sh`.
2. Commit only after `scripts/test.sh` passes, with an entry in `CHANGELOG.md`. After the commit, `scripts/check-drift.sh` in this checkout must report `no drift` (before the commit, it reports the change as drift). In another checkout or worktree, it passes only at the commit of this one; `scripts/check-drift.sh --candidate` compares the machine with that tree instead. After a push, the `test` workflow on GitHub must pass too.
3. Record each important decision in `docs/decisions/` (template: [adr-template.md](docs/decisions/adr-template.md)).
4. Keep the checkout at `~/dev/p100-q3-deployment`: `strata-q3.service` calls the scripts in `scripts/` by absolute path.
5. A change made outside this checkout, such as a pull request or a push from another clone, reaches the machine through `scripts/deploy.sh`. A change to what is installed outside the checkout, or to the checks themselves, is deployed by hand ([Deploying a commit from GitHub](docs/runbooks/deploy.md)).

## License

Apache-2.0, see [`LICENSE`](LICENSE). Some files in `results/` contain excerpts of Strata's documentation and code, which are under the MIT License: see [`results/LICENSE-strata.txt`](results/LICENSE-strata.txt).

Maintainer: [thc1006](https://github.com/thc1006).
