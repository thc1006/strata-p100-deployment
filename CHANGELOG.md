# Changelog

Notable changes to this machine and this repository, newest first. There are no version numbers; sections are by date. The categories follow [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).

## 2026-10-18

### Added

- `configure_strata_q3.py --model iq3_xxs|iq3_s`: the GSQ-RCO model the config must load (IQ3_XXS stays the default). For the other model it rewrites `--native`, `--ple-gguf`, `--pack`, `tokenizer` and `model_name`, and refuses any path that is not in setup's layout; IQ3_S takes the cuts 13/25/38. A model change is a critical difference, so preflight refuses a live config whose model differs from `config/profile.conf`. How to switch: `docs/runbooks/change-config.md`; why: [decision 0004](docs/decisions/0004-iq3-s.md).
- The box no longer runs a desktop or idle daemons: it boots into `multi-user.target`, and 30 system units and 4 of the service account's are disabled or masked (cups, avahi, ModemManager, fwupd, packagekit, docker, containerd, snapd and others; `config/host-units.conf` gives each reason). Xorg had held 4 MiB on every P100, and the snap refreshes ran four times a day. `scripts/host-units.sh` checks the states (`check-drift.sh` runs it) and sets them again (`apply`); the states before are in `results/host-units-before-20261018.txt`. Remote access, the network, ZFS, NVIDIA, cron, logging and unattended security updates stay on.
- CI: `.github/workflows/test.yml` runs `scripts/test.sh --repo-only` on every push to `main` and every pull request, in Canonical's Ubuntu Base 22.04.5 ([decision 0003](docs/decisions/0003-ci-on-github-deploy-by-pull.md)).
- `scripts/deploy.sh` deploys `origin/main` to this checkout when the newest `test` run for its push passed and nothing in the commits applies outside the checkout. Even without `--apply`, it runs the new commit's `test.sh` and `check-drift.sh` here. It never runs sudo or restarts the service ([Deploying a commit from GitHub](docs/runbooks/deploy.md)). `tests/test_deploy.py` has 21 tests for it, which fail on 92 of 94 broken copies of the script (`results/deploy-sh-mutants-20261018.txt`). `results/deploy-sh-first-run-20261018.txt` records its first deploy against GitHub, in a scratch clone.
- `scripts/install-system-files.sh`, `scripts/test.sh`, `bench/build-engine.sh`.
- Decision records: 0001, operations repository separate from Strata; 0002, directory structure; 0003, CI on GitHub and deployment pulled by this machine.
- `results/README.md`: an index of the test results.
- `results/`: 12 service load times, the raw output of three example requests, the engine lines of the 0.1.44.1 run with the read-ahead hint on, and the output of one `check-drift.sh --full` run. `.gitattributes` keeps the original line endings in `results/`.
- Unit tests went from 29 to 38. They cover preflight decisions, format differences, backups, concurrent modification and CLI errors.
- `LICENSE` (Apache-2.0), `CONTRIBUTING.md`, `SECURITY.md`, `AGENTS.md` (instructions for coding agents), and `results/LICENSE-strata.txt` (Strata's MIT license, for the excerpts in `results/`).
- `results/pr2918-*`: the check of the PR #2918 test (a hint-count harness and its output, and the test against seven broken versions of the engine code), and the CUDA, HIP and SYCL builds of the amended PR.
- Upstream bug reports #2990 (`sycl/tools/build.sh` exits 1 after a successful build and 0 after a failed compile) and #2991 (two `ple_reader_selftest` runs that share the temp directory can crash or hang), in `docs/upstream/`; their raw results are `results/sycl-build-sh-exit-20261018.txt` and `results/ple-selftest-shared-tmp-20261018.txt`.

### Changed

- RAM floors from 8 to 6 GiB and the ZFS ARC cap from 8 to 2 GiB: the box serves only this model. `configure_strata_q3.py` writes `STRATA_RESIDENT_HEADROOM_GIB=6` (the RAM the page-locked copy of the experts no card holds leaves free) and `--conversation-cache-min-free-mib 6144` (the parking floor); `zfs-arc-max.service` writes 2 GiB, and `check-deployment.sh` expects it. The live config changes, so this takes a restart, and the unit takes `scripts/install-system-files.sh`.
- `scripts/test.sh --repo-only` skips the programs outside the repository, which exist only on this machine. `test.sh` now exits with 2 on an unknown argument, fails when `systemd-analyze` is missing or fails (it passed before), and no longer needs `bc`. `CONTRIBUTING.md` now asks contributors to run `--repo-only`.
- Upstream PR #2918: the commit was amended to `25f40ed`, with the same engine code. Its test now counts the row hints, so it fails when the check is removed. The description is shorter and links to the raw results (`docs/upstream/pr-2918.md`); a comment on the PR says what changed (`docs/upstream/pr-2918-comment.md`).
- The directories were reorganized by purpose ([decision 0002](docs/decisions/0002-repository-layout.md)). ExecStartPre and ExecStartPost in `strata-q3.service` now use paths under `scripts/`. After reinstalling, one restart verified the change: preflight and wait-ready both worked normally.
- The deployment record keeps only the basis for each setting, the measurements and the limitations. The operating steps moved to `docs/runbooks/`, and the list of system changes moved to `docs/reference/`.
- Cleanup before publication: the original setup guide received before deployment was removed (it had no author information); the host name was replaced with `<host>` in the documents and in the raw result files; the deployment record no longer contains working notes or a description of local account permissions. All documents were translated from Traditional Chinese into English, and the deployment record moved to `docs/deployment-record.md`.
- preflight: any difference between the live config and `config/profile.conf` now blocks startup, except for `NOTE_ONLY_KEYS` (`--prefill`, `--pool-workers`, `--vram-reserve-mib` and the web Settings fields). It also blocks startup when `--vram-reserve-mib` is missing (the engine then uses 700 MiB), when the reserve is outside 1024–16383 MiB, or when the args use `--flag=value`. `configure_strata_q3.py --reserve-mib` now accepts only 1024–16383.
- The watchdog now reads the API key as the service account, reads only regular files and accepts at most 1 MiB of response. `strata-q3-health.service` now has a systemd sandbox (`ProtectSystem=strict` and other settings). It was reinstalled. Inside the sandbox, it was confirmed that the watchdog does nothing while the service is healthy and writes to its counter when HTTP fails.
- `check-drift.sh` also checks the owner and permissions of installed files, the drop-ins of each unit, the ZFS Adaptive Replacement Cache (ARC) parameters, an empty `profile.conf` and the manifest format. `check-deployment.sh` exits with 1 when it finds a problem.
- After the preflight and wait-ready changes above, one restart at 02:26 verified them: preflight `ok: 4 GPUs, 57 GiB usable, config matches`, `loaded after 443 s`, restart count 0. The watchdog checks after that finished normally inside the sandbox, and `check-deployment.sh` and `check-drift.sh` found no problems.

### Fixed

- README and deployment record: a ZFS prefetch reads a record from disk only when the record is not in the ARC; they said that every hint read a whole record. The deployment record also said that `zpl_fadvise` is the same in OpenZFS 2.4.4 and master; those versions rewrite it, though they still start a prefetch for every hint. Section 3.7 now counts ZFS's read completion in the CPU time.
- Deployment record: the Poisson conclusion for PR #2050 (previously stated as within the noise), the scope of the 0.1.44.1 conclusion, two upstream dates (previously in UTC), the source of the retry at 10-17 22:15, and several times and units.
- `results/SUMMARY.md`: the Poisson p50 of the first 6 groups was the upper median. `bench/report.py` now recalculates it from the per-request data with nearest-rank, the same as for the other groups.
- `configure_strata_q3.py`: also rewrites the config in the standard format when it differs only in indentation or a BOM. Backup numbers now continue after the highest existing number.
- `strata-wait-ready.sh` and preflight now measure the time spent waiting for GPUs with `/proc/uptime`, so clock adjustments at boot do not affect it; preflight can read a config that has a BOM (as server.py can); `check-drift.sh` also checks the last line of a manifest that has no newline at the end.

## 2026-10-17

### Added

- A/B of 0.1.45 and the skip-hint-when-locked patch ([`results/AB-0.1.45-ple-hint.md`](results/AB-0.1.45-ple-hint.md)). It compared the 0.1.45 hint on and off, the patch, `--ple-io mmap`, and the per-layer embedding (PLE) file on ext4. It had no 0.1.43.1 arm. The patch went upstream as [PR #2918](https://github.com/Niko1221/Strata/pull/2918). The production service stays on 0.1.43.1; the 0.1.45 release notes say "Moved to 0.1.46: the Volta/Pascal series".
- This repository (private at the time), `check-drift.sh` and the config snapshot (in `deploy/` at the time).

## 2026-10-16

### Added

- Deployment of Strata 0.1.43.1 (`a0116b2`): `strata-q3.service`, `strata-q3-health.timer`, `zfs-arc-max.service` and logrotate. The deployed configuration is `--profile deployed`: `--ple-io ram` with `--no-ple-prefetch`, layer split 13/26/38, 8 GiB of conversation parking.
- A/B of 0.1.44.1 ([`results/AB-0.1.44.1.md`](results/AB-0.1.44.1.md)): throughput and prefill within about 1% of 0.1.43.1 (except one-user short prompts), so no upgrade. The measurements of the ZFS read-ahead hint problem were posted upstream in [#2281](https://github.com/Niko1221/Strata/issues/2281#issuecomment-6216816902).
