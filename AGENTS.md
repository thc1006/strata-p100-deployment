# Instructions for coding agents

This repository is the configuration and operations code of a production LLM inference service: Strata serving
Qwen3.8-Flash-Next IQ3_XXS on 4× Tesla P100. The checkout at `/home/thc1006/dev/p100-q3-deployment` is the live
deployment. `strata-q3.service` runs `scripts/strata-preflight.sh` and `scripts/strata-wait-ready.sh` from it, and the
preflight runs `tools/configure_strata_q3.py`. A change to `scripts/`, `tools/` or `config/profile.conf` takes effect
at the next start of the service. Other people use the service.

## Commands

- `scripts/test.sh`: unit tests, `bash -n` on every script, Python syntax, `systemd-analyze verify` and a check that
  every program the units run exists. It ends with `all checks passed`.
- `python3 -B -m unittest discover -s tests -k <name>`: run one test, for example `-k test_value_bounds`.
- `scripts/check-drift.sh`: read-only comparison of the machine with the repository. It ends with `no drift`.
- `python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json --profile deployed --dry-run`: shows what
  the tool would change in the live config without writing it.

## Never, unless the maintainer asks for it in this session

- Start, stop, restart or reset `strata-q3.service` or `strata-q3-health.timer`, or kill the engine or `server.py`.
  A start takes 7–9 minutes, and a stop during the load leaves the service failed.
- Run anything in `bench/`. The A/B scripts stop and restart the production service, the tracing scripts use `sudo`,
  and the load tests keep the GPUs busy for up to an hour.
- Run `sudo`, `scripts/install-system-files.sh`, or anything that writes to `/etc`, `/usr/local/sbin` or `~/dev/strata`.
- Run `tools/configure_strata_q3.py` on `~/dev/strata/strata-iq3_xxs.json` without `--dry-run`.
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
- A change to `tools/configure_strata_q3.py` comes with a unit test in `tests/` that fails without the change.
- `scripts/check-drift.sh` still reports `no drift`, or the drift is the intended change and the repository copy
  changes in the same commit.
- `CHANGELOG.md` has an entry for each notable change.

## Conventions

- Shell scripts use `set -u` and fail closed: when a check cannot read its input, it refuses instead of passing.
- Python uses the standard library only and runs on Python 3.10.
- Strata engine arguments never use the `--flag=value` form; the engine rejects it.
- Documentation is in plain English. Every number names the configuration it was measured on and the file in
  `results/` it comes from.
- Decision records follow [docs/decisions/adr-template.md](docs/decisions/adr-template.md).

## Commits

- English commit messages in the imperative mood, one logical change per commit.
- Do not add `Co-authored-by` trailers or any other attribution lines.

## Where to read more

- [README.md](README.md): what runs here and the documentation index.
- [docs/runbooks/](docs/runbooks/): step-by-step procedures, including
  [changing the configuration](docs/runbooks/change-config.md).
- [docs/deployment-record.md](docs/deployment-record.md): why each setting is what it is, with measurements.
