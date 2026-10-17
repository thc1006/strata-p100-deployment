# Evaluating and upgrading Strata to a new upstream release

- When to use: upstream has published a new release, and you need to decide whether it is worth upgrading.
- Prerequisites: `~/dev/strata-git` (a partial clone of upstream, `--filter=blob:none`), and a time window in which you can stop the service (each A/B run needs one restart; the 0.1.44.1 evaluation had 5 runs, 18:37–21:37). Run the commands below from the repo root.
- Last verified:
  - Steps 3 and 4 (the config for the new tree, `ab_versions.sh` and `ab_summary.py`): used on 2026-10-16 to evaluate 0.1.44.1 (the files were in `deploy/` at the time).
  - `bench/build-engine.sh`: run on 2026-10-18 against the 0.1.45 tree, which was already built; no files needed recompiling.
  - The 0.1.45 A/B on 2026-10-17 compared the hint on and off, the patch, `--ple-io mmap`, and the per-layer embedding (PLE) file on ext4. It had no 0.1.43.1 arm.
  - *No actual switch to a new release has been done yet*. Step 6 is a list based on what is known now.

## Steps

1. Read the release notes, and check the state of the PLE read-ahead hint:
   ```bash
   git -C ~/dev/strata-git fetch origin tag <tag>
   git -C ~/dev/strata-git show <tag>:src/kernels/ngram.cpp | grep -n 'if (impl_->locked) return'
   ```
   No output means that the line added by PR #2918 is not in this release (v0.1.45 does not have it), so the new release must also keep `--no-ple-prefetch`. If upstream fixed it in another way, read `PleTable::gather_batch` to check whether it still issues the hint when the table is locked.
2. Build the new release in a separate directory, without touching `~/dev/strata`:
   ```bash
   git -C ~/dev/strata-git worktree add --detach ~/dev/strata-<version> <tag>
   grep '^LLAMA_CPP_COMMIT' ~/dev/strata/setup.py ~/dev/strata-<version>/setup.py   # copy llama.cpp directly only if the two lines are identical
   cp -a ~/dev/strata/third_party/llama.cpp ~/dev/strata-<version>/third_party/
   bench/build-engine.sh ~/dev/strata-<version> <version>
   ```
   Expected: the last line is `built … engine sha256 …`, after about 5 minutes.
3. Create the config for the new tree. It is the same as the live config, except that `exe`, `cwd` and `log` point to the new tree. Save it as `~/dev/strata-<version>/strata-iq3_xxs.json`. To test with read-ahead on (arm C), also save a copy without `--no-ple-prefetch` as `strata-iq3_xxs-prefetch.json`. For an example of how to write it, see `make()` in `bench/make_v045_configs.py`.
4. Run the A/B comparison:
   - First run `sudo systemctl stop strata-q3-health.timer`.
   - Change `V441=` on line 14 of `bench/ab_versions.sh` to the new tree. `PY` on line 15 uses `~/dev/strata/.venv`. If the new release has a different `requirements.txt`, first create a venv for the new tree and change this line.
   - Restart each version at least twice, and alternate the order. The tags must not be the same as tags that already exist in `results/`:
     ```bash
     bench/ab_versions.sh A:<old>-1 B:<new>-1 B:<new>-2 A:<old>-2
     python3 -B bench/ab_summary.py <old>-1 <new>-1 <new>-2 <old>-2 > results/AB-<new-version>.md
     ```
     The title in `ab_summary.py` is hard-coded on line 72. Change it to the new version number first.
   - When the script ends (also when it fails), it starts the production service again. After the test, run `sudo systemctl start strata-q3-health.timer`, and check that `strata-q3` is active.
5. Decide: add a decision record in `docs/decisions/` (whether to upgrade, and the reasons), and add an entry to `CHANGELOG.md`.
6. Switch over (not yet done in practice):
   - `serve/server.py` must be replaced together with the engine, so switch the whole tree as one unit. Do not replace only the engine.
   - The recommended way is to put the new tree at `~/dev/strata` (rename the old tree and keep it). If you do not, you must change every place that hard-codes `~/dev/strata` (on 2026-10-18 this command found 16 files, in `systemd/`, `scripts/`, `tests/` and `bench/`):
     ```bash
     grep -rnE 'dev/strata([/"[:space:]}]|$)|"dev" / "strata"' scripts systemd tests tools bench
     ```
   - `tools/configure_strata_q3.py` was written against `setup.py` and `serve/server.py` of `a0116b2` (the Audited source in the docstring, and `SOURCE_COMMIT`). Check against the new release first, then change these two places, and run `scripts/test.sh`.
   - Regenerate the config with `tools/configure_strata_q3.py`, update the two snapshots in `config/` and `config/deployed-artifacts.sha256`, run `scripts/install-system-files.sh`, and then run `sudo systemctl restart strata-q3`.
   - Run `scripts/check-drift.sh` and `scripts/check-deployment.sh`.

## Rollback

Keep the old tree; do not delete it. Rename the old tree back to `~/dev/strata` (or point the paths back to the old tree), restore `config/`, `systemd/` and `tools/` with git and install them again, and then run `sudo systemctl restart strata-q3`.
