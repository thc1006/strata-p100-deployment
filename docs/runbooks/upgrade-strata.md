# Evaluating and upgrading Strata to a new upstream release

- When to use: upstream has published a new release, and you need to decide whether it is worth upgrading.
- Prerequisites: `~/dev/strata-git` (a partial clone of upstream, `--filter=blob:none`), and a time window in which you can stop the service (each A/B run needs one restart; the 0.1.44.1 evaluation had 5 runs, 18:37–21:37: `results/ab-v441-driver.log`). Run the commands below from the repo root.
- Last verified:
  - Steps 3 and 4 (the config for the new tree, `ab_versions.sh` and `ab_summary.py`): used on 2026-10-16 to evaluate 0.1.44.1 (the files were in `deploy/` at the time).
  - `bench/build-engine.sh`: run on 2026-10-18 against the 0.1.45 tree, which was already built; no files needed recompiling.
  - The 0.1.45 A/B on 2026-10-17 compared the hint on and off, the patch, `--ple-io mmap`, and the per-layer embedding (PLE) file on ext4. It had no 0.1.43.1 arm.
  - Step 6: done on 2026-10-19 to switch from 0.1.43.1 to 0.1.45, with the commands below (`results/deploy-0.1.45-20261019.txt`).

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
   Expected: the last line is `built … engine sha256 …`. If it prints `build failed` and the end of `build.log` that it shows has a compiler that was killed (`Killed signal terminated program …`, or `Killed`), and `oom_kill` in `/sys/fs/cgroup/user.slice/user-1003.slice/memory.events` went up, the build passed the account's 6 GiB limit: lift the limit for the build as [Daily operations](operations.md#the-memory-limit-of-the-maintainers-account) says, or lower `-j` in `bench/build-engine.sh`.
3. Create the config for the new tree. It is the same as the live config, except that `exe`, `cwd` and `log` point to the new tree. Save it as `~/dev/strata-<version>/strata-iq3_xxs.json`. To test with read-ahead on (arm C), also save a copy without `--no-ple-prefetch` as `strata-iq3_xxs-prefetch.json`. For an example of how to write it, see `make()` in `bench/make_v045_configs.py`.
4. Run the A/B comparison:
   - First run `sudo systemctl stop strata-q3-health.timer`.
   - Change the `V441=` line of `bench/ab_versions.sh` to the new tree. Its `PY=` line uses `~/dev/strata/.venv`. If the new release has a different `requirements.txt`, first create a venv for the new tree and change this line.
   - Restart each version at least twice, and alternate the order. The tags must not be the same as tags that already exist in `results/`:
     ```bash
     bench/ab_versions.sh A:<old>-1 B:<new>-1 B:<new>-2 A:<old>-2
     python3 -B bench/ab_summary.py <old>-1 <new>-1 <new>-2 <old>-2 > results/AB-<new-version>.md
     ```
     The title in `ab_summary.py` is hard-coded on line 72. Change it to the new version number first.
   - When the script ends (also when it fails), it starts the production service again. After the test, run `sudo systemctl start strata-q3-health.timer`, and check that `strata-q3` is active.
5. Decide: add a decision record in `docs/decisions/` (whether to upgrade, and the reasons), and add an entry to `CHANGELOG.md`.
6. Switch over (done on 2026-10-19 for 0.1.45):
   - `serve/server.py` must be replaced together with the engine, so switch the whole tree as one unit. Do not replace only the engine. Put the new tree at `~/dev/strata` and keep the old one: every place in `scripts/`, `systemd/`, `tests/`, `tools/` and `bench/` that names `~/dev/strata` then stays right (`grep -rnE 'dev/strata([/"[:space:]}]|$)|"dev" / "strata"' scripts systemd tests tools bench`).
   - `tools/configure_strata_q3.py` names the release its assumptions about `setup.py`, `serve/server.py` and the engine's options were checked against (the Audited source in the docstring, and `SOURCE_COMMIT`: `477ac13cb`, 0.1.45, since 2026-10-19). Check them against the new release first, then change these two places, and run `scripts/test.sh`.
   - In a commit: the tool's changes, `config/strata-iq3_xxs.deployed.json` (the tool's output from `config/strata-iq3_xxs.setup.json`), and in `config/deployed-artifacts.sha256` the engine, every file next to it, and the config. If the venv changes too, write `config/strata-venv.txt` again in that commit, with the command in its header: `scripts/check-drift.sh` compares each package of the venv, and the sha256 of its `RECORD`, with the file. Its preflight refuses the old config, so deploy it by hand ([deploy.md](deploy.md)), in the same window as the switch.
   - Before the window, a clean worktree of the release, with the engine directory of the tree that was measured (the same binary):
     ```bash
     git -C ~/dev/strata-git worktree add --detach ~/dev/strata-<version>-prod <commit>
     cp -a ~/dev/strata-<version>/engine-cuda12 ~/dev/strata-<version>-prod/
     cp -a ~/dev/strata-<version>/third_party/llama.cpp ~/dev/strata-<version>-prod/third_party/   # for bench/build-engine.sh
     ```
   - The window. `git worktree move` reads a relative target from the directory `-C` names, so give absolute paths:
     ```bash
     sudo systemctl stop strata-q3-health.timer
     git -C ~/dev/p100-q3-deployment pull --ff-only origin main && ~/dev/p100-q3-deployment/scripts/test.sh
     ~/dev/p100-q3-deployment/scripts/install-system-files.sh       # when systemd/ changed; then restart a changed oneshot
     sudo systemctl stop strata-q3                                   # when /metrics says idle
     mv ~/dev/strata ~/dev/strata-<old version>
     git -C ~/dev/strata-git worktree move ~/dev/strata-<version>-prod ~/dev/strata
     cp -a ~/dev/strata-<old version>/.venv ~/dev/strata/.venv       # the same path as before
     cp -p ~/dev/strata-<old version>/strata-iq3_xxs.json ~/dev/strata/
     cd ~/dev/p100-q3-deployment && python3 tools/configure_strata_q3.py ~/dev/strata/strata-iq3_xxs.json $(sed -e 's/#.*//' config/profile.conf)
     cmp ~/dev/strata/strata-iq3_xxs.json config/strata-iq3_xxs.deployed.json && scripts/check-drift.sh
     sudo systemctl start strata-q3 && sudo systemctl start strata-q3-health.timer
     scripts/check-deployment.sh
     ```
     Expected: `no drift` before the start, `no problems found` after it. The engine's start-up lines show the new version.

## Rollback

Keep the old tree; do not delete it. The deployed commit's preflight refuses the old tree's config, so the commit goes
back with the tree:

1. `sudo systemctl stop strata-q3-health.timer strata-q3`
2. `git -C ~/dev/strata-git worktree move ~/dev/strata ~/dev/strata-<version>-prod` and `mv ~/dev/strata-<old version> ~/dev/strata`
3. Revert the deployment commit on GitHub (`git revert <commit>` in a clone, then push), wait for its `test` run, and run
   `git -C ~/dev/p100-q3-deployment pull --ff-only origin main` and `scripts/install-system-files.sh` (the old units).
   To start sooner, check out the previous commit here first (`git -C ~/dev/p100-q3-deployment switch --detach <previous commit>`),
   and switch back to `main` once the revert is pulled; `scripts/deploy.sh` refuses a checkout off `main`.
4. `sudo systemctl start strata-q3 strata-q3-health.timer`, then `scripts/check-drift.sh` and `scripts/check-deployment.sh`.
