# Instructions for coding agents

This repository is the configuration and operations code of a production LLM inference service: Strata serving
Qwen3.8-Flash-Next GSQ-RCO IQ3_S on 4× Tesla P100. The checkout at `/home/thc1006/dev/p100-q3-deployment` is the live
deployment. `strata-q3.service` runs `scripts/strata-preflight.sh` and `scripts/strata-wait-ready.sh` from it, and the
preflight runs `tools/configure_strata_q3.py`. A change to `scripts/`, `tools/` or `config/profile.conf` takes effect
at the next start of the service. Other people use the service.

## Commands

- `scripts/test.sh`: unit tests, `bash -n` and shellcheck (errors) on every script (shellcheck is skipped where it is
  not installed, but not with `--repo-only`), Python syntax (also the Python inside the shell scripts),
  `systemd-analyze verify` and a check that every program the units run exists. It ends with `all checks passed`.
  `--repo-only` skips what needs this machine. `--harnesses` also runs the stub harnesses that start fake engines
  (`tests/ab_engine_stubs.sh`, `ab_model_run_stubs.sh`, `switch_model_stubs.sh`): the fake engines have the command
  line the preflight looks for, so on this machine they run only while the service is stopped. CI
  (`.github/workflows/test.yml`) runs `--repo-only --harnesses`.
- `python3 -B -m unittest discover -s tests -k <name>`: run one test, for example `-k test_value_bounds`.
- `scripts/check-drift.sh`: read-only comparison of the machine, and of the running service, with the repository. It
  ends with `no drift`. Uncommitted changes in the checkout the service runs are drift: run it after a commit.
  `--full` also hashes the files over 1 GiB. From another checkout or worktree it passes only at the commit of the
  live checkout; `--candidate` compares the machine with that tree instead, as with a commit to be deployed
  (`scripts/deploy.sh` does).
- `python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json $(sed -e 's/#.*//' config/profile.conf) --dry-run`: shows what
  the tool would change in the live config without writing it.
- `scripts/deploy.sh`: whether `origin/main` can be deployed to this checkout. To find out, it runs that commit's
  `scripts/test.sh` and `scripts/check-drift.sh` here; the checkout stays as it was. `--apply` then deploys it: a
  fast-forward, with no sudo and no restart.

## Never, unless the maintainer asks for it in this session

- Start, stop, restart or reset `strata-q3.service` or `strata-q3-health.timer`, or kill the engine or `server.py`.
  A restart takes about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`), and a stop during the load
  leaves the service failed.
- Run anything in `bench/`. The A/B scripts stop and restart the production service, the tracing scripts use `sudo`,
  and the load tests keep the GPUs busy for up to an hour (a robustness run with a 30-minute soak took 59.8–60.4
  minutes: `results/*-robustness.log`).
- Run `sudo`, `scripts/install-system-files.sh`, or anything that writes to `/etc`, `/usr/local/sbin` or `~/dev/strata`.
- Run `tools/configure_strata_q3.py` on `~/dev/strata/strata-iq3_xxs.json` without `--dry-run`.
- Run `scripts/deploy.sh --apply`. It changes what the service runs at its next start.
- Edit a file in `results/`. These are raw measurements; new results come only from new runs.
- Change a number in the documentation without the raw file in `results/` that supports it.
- Commit an API key, a password, an email address or the machine's host name. Results write the host name as `<host>`.

## Ask first

- Changes to `systemd/`, `scripts/strata-preflight.sh`, `scripts/strata-wait-ready.sh` or `scripts/strata-health.sh`.
  They run in production, and the watchdog runs as root.
- Changes to what the preflight allows or refuses: `NOTE_ONLY_KEYS` and `critical()` in
  `tools/configure_strata_q3.py`.

## Done means

- `scripts/test.sh` passes. Report its last lines.
- After a push, the `test` workflow passes on GitHub (`gh run list -R thc1006/strata-p100-deployment -w test.yml -L 3`;
  the checkout has two GitHub remotes). It runs the stub harnesses, which `scripts/test.sh` here skips.
- A change to `tools/configure_strata_q3.py` comes with a unit test in `tests/` that fails without the change.
- After the commit, `scripts/check-drift.sh` still reports `no drift`, or the drift is the intended change and the
  repository copy changes in the same commit.
- `CHANGELOG.md` has an entry for each notable change.

## Conventions

- Shell scripts use `set -u` and fail closed: when a check cannot read its input, it refuses instead of passing.
- Python uses the standard library only and runs on Python 3.10.
- Strata engine arguments never use the `--flag=value` form; the engine rejects it.
- Documentation is in plain English. Every number names the configuration it was measured on and the file in
  `results/` it comes from.
- Decision records follow [docs/decisions/adr-template.md](docs/decisions/adr-template.md).
- On this machine, start every job you run (tests, experiments, generators of test input) in a capped scope:
  `systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 -- choom -n 500 -- <command>`, one heavy job at a
  time. The account may use 6 GiB in all ([decision 0007](docs/decisions/0007-memory-limit-of-the-maintainers-account.md)), and on
  2026-10-21 a test input that grew without bound took the service down for 11 minutes.

## Commits

- English commit messages in the imperative mood, one logical change per commit.
- Do not add `Co-authored-by` trailers or any other attribution lines.

## Where to read more

- [README.md](README.md): what runs here and the documentation index.
- [docs/runbooks/](docs/runbooks/): step-by-step procedures, including
  [changing the configuration](docs/runbooks/change-config.md).
- [docs/deployment-record.md](docs/deployment-record.md): why each setting is what it is, with measurements.
