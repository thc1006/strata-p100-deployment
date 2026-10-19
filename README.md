# Strata on 4× Tesla P100: Qwen3.8-Flash-Next IQ3_XXS deployment and benchmarks

Last updated: 2026-10-18.

This repository holds the configuration, operations scripts and measurements of a production deployment of [Strata](https://github.com/Niko1221/Strata), an LLM inference engine. The service runs the Qwen3.8-Flash-Next IQ3_XXS GGUF model, a mixture-of-experts (MoE) model, on four NVIDIA Tesla P100 16 GB GPUs (Pascal, sm_60). The machine is a VMware VM with a ZFS root file system. The service offers OpenAI-compatible and Anthropic-compatible APIs on `127.0.0.1:8080` to the members of one lab.

## What the measurements show

All numbers come from this machine. The [deployment record](docs/deployment-record.md) gives the method, the raw files and the limits of each one.

- With the per-layer embedding (PLE) table locked in RAM (`--ple-io ram`), Strata 0.1.43.1, like 0.1.45, still sends one `madvise(MADV_WILLNEED)` hint per PLE row. OpenZFS turns each hint into an ARC prefetch of the 128 KiB record that holds the row, which is read from disk when the record is not in the ARC. One 16K-token prompt read 15.5–17.8 GB from disk, and prefill ran at 407–440 tok/s. With `--no-ple-prefetch`, prefill ran at 692–706 tok/s ([section 4](docs/deployment-record.md#4-problems-found-in-this-deployment-by-impact)). A proposed engine fix is upstream [PR #2918](https://github.com/Niko1221/Strata/pull/2918), not merged yet.
- Four users with short prompts reached 100.0 tok/s in total. One user decoded at 48.7–52.0 tok/s with multi-token prediction ([section 3.1](docs/deployment-record.md#31-one-user-and-short-prompts-deployed)).
- Moving the layer split from 12/24/36 to 13/26/38 raised four-user decode from 85.9 to 100.6 tok/s ([section 2](docs/deployment-record.md#2-how-the-final-configuration-differs-from-the-original-setup-guide)).
- On 0.1.43.1, long prompts paused the other users: while a 16K-token prompt was read, users who were generating stopped for about 23 s, and when four 16K-token prompts arrived at once, the last user waited 94.4 s for the first token ([section 3.2](docs/deployment-record.md#32-long-inputs-and-concurrent-users-the-main-p100-limit-deployed)). 0.1.45's concurrent prompt read, deployed on 2026-10-19, cut that stop to 0.55 s ([section 3.8](docs/deployment-record.md#38-strata-0145-and-its-two-switches-engine-experiments)).
- With 8 GiB of RAM for conversation parking, multi-turn conversations stayed cached while six clients competed for four slots. Cache reuse went from 0/16 to 16/16, and the median time to first token went from 38.8 s to 20.2 s ([section 3.4](docs/deployment-record.md#34-conversation-parking)).

## Current state of the service

| Item | Details |
|---|---|
| Service | `strata-q3.service`, 4 slots, 32K context each, no API key set |
| Engine | Strata 0.1.45 (commit `477ac13cb`) in `~/dev/strata`, built with CUDA 12.9 for `sm_60` only, with `STRATA_MMQ_GROUP_SORT=1` and `STRATA_PIPED_CONCURRENT=1` (decision 0005) |
| Configuration | Output of `tools/configure_strata_q3.py --profile deployed`, SHA-256 `05539a70…` |
| Upstream | ZFS read-ahead hint problem: [follow-up comment on issue #2281](https://github.com/Niko1221/Strata/issues/2281#issuecomment-6216816902) and [PR #2918](https://github.com/Niko1221/Strata/pull/2918) (not merged, no review yet) |

## Repository layout

| Path | Purpose |
|---|---|
| `config/` | Desired state: the tool arguments in `profile.conf`, snapshots of the Strata config, and a SHA-256 list of the deployed files |
| `systemd/` | systemd units and the logrotate rule, installed to `/etc` |
| `scripts/` | Scripts that run on the host: the pre-start check, the wait for the model load, the watchdog, the status and drift checks, the installer for system files, the repository self-test, and the deploy script |
| `tools/` | `configure_strata_q3.py`, which turns the config that Strata's `setup.sh` writes into the deployed configuration |
| `tests/` | Unit tests for `tools/` and `scripts/deploy.sh` |
| `bench/` | Load, latency, robustness, version A/B and tracing tests, and the engine build script |
| `results/` | Raw test results ([index](results/README.md)) |
| `examples/requests/` | Example API requests |
| `docs/` | Documentation, listed below |
| `.github/workflows/` | CI: `test.yml` runs `scripts/test.sh --repo-only` |

## Install and use

To build a new machine, follow [Rebuilding the deployment from scratch](docs/runbooks/rebuild.md). To reinstall only the files in `/etc` and `/usr/local/sbin`, run `scripts/install-system-files.sh` (add `--dry-run` first to see what it would install).

```bash
scripts/check-deployment.sh        # service status; exits with 1 if it finds a problem
scripts/check-drift.sh             # whether the machine matches the repository
scripts/test.sh                    # repository self-test; run it before each commit
scripts/deploy.sh                  # whether origin/main can be deployed here (runs its checks); --apply deploys it
sudo systemctl restart strata-q3   # the service is ready after about 7–9 minutes
```

## Documentation

| Document | Contents |
|---|---|
| [Daily operations](docs/runbooks/operations.md) | Status, start and stop, logs, test requests, recovery from the failed state, preparation for long tests, rerunning the benchmarks |
| [Changing the configuration](docs/runbooks/change-config.md) | Changing Strata parameters, systemd units or the watchdog |
| [Deploying a commit from GitHub](docs/runbooks/deploy.md) | Bringing this checkout up to a newer `main` whose `test` workflow passed |
| [Rebuilding the deployment from scratch](docs/runbooks/rebuild.md) | Driver, CUDA 12.9.1, model download, Strata setup and the systemd install |
| [Evaluating and upgrading Strata](docs/runbooks/upgrade-strata.md) | Building a new release and comparing it with the deployed one |
| [System changes and how to undo them](docs/reference/system-changes.md) | Everything the deployment changed outside the repository |
| [Deployment record](docs/deployment-record.md) | The evidence for each setting, measured results, known limits and the test method |
| [Decision records](docs/decisions/) | [0001: keep this repository separate from Strata](docs/decisions/0001-keep-ops-repo-separate-from-strata.md); [0002: repository layout](docs/decisions/0002-repository-layout.md); [0003: CI on GitHub, deployment pulled by this machine](docs/decisions/0003-ci-on-github-deploy-by-pull.md); [0004: IQ3_S, 6 GiB RAM floors and a 2 GiB ARC (proposed)](docs/decisions/0004-iq3-s.md); [0005: Strata 0.1.45 with its grouped prompt sort and concurrent prompt reads](docs/decisions/0005-strata-0.1.45-group-sort-concurrent-read.md) |
| Upstream posts | [Comment on issue #2281](docs/upstream/issue-2281-comment.md); [PR #2918](docs/upstream/pr-2918.md) and [its comment](docs/upstream/pr-2918-comment.md), closed in upstream's tracker reset for 0.2.0-beta and reopened after a re-test on it; bug reports [#2990](docs/upstream/issue-2990.md) (`sycl/tools/build.sh` exit status) and [#2991](docs/upstream/issue-2991.md) (`ple_reader_selftest` temp files), closed in upstream's tracker reset for 0.2.0-beta and reopened after a re-test on it |
| [Test results](results/README.md) | Index of the raw output files and the commands that regenerate the summaries |
| [Changelog](CHANGELOG.md) | Changes by date |
| [Contributing](CONTRIBUTING.md), [Security policy](SECURITY.md), [Instructions for coding agents](AGENTS.md) | How to report problems and send changes |

## Changing this repository

1. Change the repository first, then apply the change to the machine. For Strata settings, follow [Changing the configuration](docs/runbooks/change-config.md). For system files, use `scripts/install-system-files.sh`.
2. Commit only after `scripts/test.sh` and `scripts/check-drift.sh` both pass, and add an entry to `CHANGELOG.md`. After a push, the `test` workflow on GitHub has to pass as well.
3. Record each important decision in `docs/decisions/` (template: [adr-template.md](docs/decisions/adr-template.md)).
4. Keep the checkout at `~/dev/p100-q3-deployment`. `strata-q3.service` calls the scripts in `scripts/` by absolute path.
5. A change made outside this checkout, such as a pull request or a push from another clone, reaches the machine through `scripts/deploy.sh`, or by hand when it changes what is installed outside the checkout ([Deploying a commit from GitHub](docs/runbooks/deploy.md)).

## License

Apache-2.0, see [`LICENSE`](LICENSE). Some files in `results/` contain excerpts of Strata's documentation and code, which are under the MIT License: see [`results/LICENSE-strata.txt`](results/LICENSE-strata.txt).

Maintainer: [thc1006](https://github.com/thc1006).
