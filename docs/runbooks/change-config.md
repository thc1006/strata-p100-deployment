# Changing the configuration of the Strata service

- When to use: you want to change the launch parameters of Strata. `tools/configure_strata_q3.py` can change `--profile`, `--ple-io`, `--split`, `--prefill`, `--pool-workers`, `--reserve-mib`, `--batch-groups` and `--conversation-cache-mib` (see `--help` for details). The reasoning defaults (medium, 1024) are hard-coded in the tool and are not part of this procedure.
- Prerequisites: know what you will change and why. Write the reason in the [deployment record](../deployment-record.md) or in a new [decision record](../decisions/). Run the commands below from the repo root.
- Last verified: the prefill 4096/1024 experiment on 2026-10-16 and its revert used steps 1 and 3, the restart, and the first rollback method (the files were in `deploy/` at the time). On 2026-10-18, with no change to the settings, steps 2 and 5 were run as written (the files were byte-for-byte identical before and after). The alternative method for api_key was run once in a test directory with a fake key, and `check-drift.sh` did not print the key.

## Steps

1. Change the parameters in `config/profile.conf`, for example from `--profile deployed` to `--profile deployed --prefill 1024`. At every start, preflight compares this line with the live config:
   - When the only differences are in `--prefill`, `--pool-workers` or `--reserve-mib` (1024 or higher), or in fields that the web settings change, such as the reasoning defaults and `fit_max_tokens`, preflight writes one note line to the log and starts as usual (the list is the tool's `NOTE_ONLY_KEYS`).
   - When only the format differs (indentation, BOM, the order of args, or duplicate args), it also only writes a note.
   - Any other difference makes it refuse to start, for example in `--split`, `--ple-io`, `--batch-groups` or `--conversation-cache-mib`, or a change to another `--profile`.
   - In addition, preflight always refuses to start when the engine is not `~/dev/strata/engine-cuda12/strata`, when `lazy_load` is true, when the args use the `--flag=value` form (the engine does not accept it), or when the VRAM reserve of the first card is not within 1024–16383 MiB (without `--vram-reserve-mib` the engine uses 700; for values below 1024, see issue #2065). The tool's `--reserve-mib` also accepts only 1024–16383.
2. Preview the change:
   ```bash
   python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json $(sed -e 's/#.*//' config/profile.conf) --dry-run
   ```
   Expected: in the JSON output, `changes` holds only the items you want to change, in the format `parameter: old value -> new value`.
3. Write the change: run the same line again without `--dry-run`. When there are changes, the tool first backs up the old file as `~/dev/strata/strata-iq3_xxs.json.before-p100-q3` (if that file exists, it adds `.1`, `.2`, … in order).
4. Apply and check:
   ```bash
   sudo systemctl restart strata-q3      # about 7–9 minutes
   scripts/check-deployment.sh
   ```
5. When you decide to keep the change, update the expected state in the repo:
   ```bash
   python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1], encoding="utf-8-sig")).get("api_key") in (None, "") else "api_key set: do not copy")' ~/dev/strata/strata-iq3_xxs.json &&
   cp ~/dev/strata/strata-iq3_xxs.json config/strata-iq3_xxs.deployed.json &&
   new=$(cd ~/dev && sha256sum strata/strata-iq3_xxs.json) &&
   sed -i "s|^[0-9a-f]\{64\}  strata/strata-iq3_xxs.json\$|$new|" config/deployed-artifacts.sha256 &&
   scripts/check-drift.sh && scripts/test.sh
   ```
   Expected: `check-drift.sh` reports `no drift`, and `test.sh` reports `all checks passed`. If DRIFT appears, find the cause first and do not commit. When the checks pass, add an entry to `CHANGELOG.md`, and then run `git add config/ CHANGELOG.md && git commit -m "config: <what changed and why>"`.

   When the config sets `api_key`, the first line stops and none of the later lines run: the key must not go into the repo. In that case, generate the snapshot from the original setup config instead (`check-drift.sh` compares in the same way). You do not need to change the line in the manifest:
   ```bash
   t=$(mktemp -d) && cp config/strata-iq3_xxs.setup.json "$t/c.json" &&
   python3 tools/configure_strata_q3.py "$t/c.json" $(sed -e 's/#.*//' config/profile.conf) > /dev/null &&
   cp "$t/c.json" config/strata-iq3_xxs.deployed.json && rm -r "$t"
   ```
   After that, run `scripts/check-drift.sh && scripts/test.sh` in the same way. When they pass, add a CHANGELOG entry and commit.

## Rollback

First change `config/profile.conf` back to its original line. Then do one of the following:

- Regenerate the config with the commands in steps 2 and 3.
- Copy the newest backup (the config from before the most recent write) back: `cp "$(ls -t ~/dev/strata/strata-iq3_xxs.json.before-p100-q3* | head -1)" ~/dev/strata/strata-iq3_xxs.json`

After that, run `sudo systemctl restart strata-q3`.

## Change the systemd units or the watchdog

First edit `systemd/` or `scripts/strata-health.sh`, and then install with `scripts/install-system-files.sh` (it installs only the files that differ, and runs `daemon-reload` when a unit has changed; add `--dry-run` first to see which files it would install). Changes to `strata-q3.service` take effect at the next start. Finally, run `scripts/test.sh` and `scripts/check-drift.sh`, and commit.
