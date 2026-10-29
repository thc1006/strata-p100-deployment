# Deploying a commit from GitHub

`scripts/deploy.sh` brings this checkout up to `origin/main`. Why it works this way: [decision 0003](../decisions/0003-ci-on-github-deploy-by-pull.md) and [decision 0006](../decisions/0006-harnesses-in-ci-checks-by-hand.md).

- When to use: `main` on GitHub has commits that this checkout does not, for example a merged pull request or a change pushed from another clone. A commit made in this checkout is already in place: the service runs it at its next start.
- Prerequisites: the `test` workflow has finished on the newest commit (`gh run list -R thc1006/strata-p100-deployment -w test.yml -L 3`; this checkout has two GitHub remotes, so `gh` needs `-R`).
- Last verified: on 2026-10-18, steps 1 and 2 in a clone of this repository deployed `ab95f6c`, with the real `test` run on GitHub and that commit's checks on this machine (`results/deploy-sh-first-run-20261018.txt`). On 2026-10-27, the steps to deploy by hand deployed `7886a39` to this checkout without a restart of the service; step 2 was not run as a separate command (`results/deploy-review-fixes-20261027.txt`).

## Steps

1. Check what would be deployed:
   ```bash
   scripts/deploy.sh
   ```
   Expected: the new commits, one `ok` line per check, and `dry run: scripts/deploy.sh --apply deploys <commit>`. This already runs the new commit's `scripts/test.sh` and `scripts/check-drift.sh --candidate` on this machine, which takes most of its time. When a check fails, the script prints `not deployed:` and the reason, and leaves the checkout as it was.
2. Deploy:
   ```bash
   scripts/deploy.sh --apply
   ```
   Expected: `deployed <commit>: <subject>`. If the commits change the preflight, wait-ready or `tools/`, a `note` line lists them: the service runs them at its next start. To run them now, restart when the service can be down for about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`), with `sudo systemctl restart strata-q3`, and then run `scripts/check-deployment.sh`.

## When the script refuses

| Reason starts with | What to do |
|---|---|
| `this checkout is not on branch main` | `git switch main`, then run the script again. |
| `this checkout has local changes`, `this checkout hides files from git status` | Commit and push the work, or remove it (`git status`, `git ls-files -v`). |
| `origin/main (…) is not a fast-forward` | This checkout has commits that GitHub does not. Push them. If `main` on GitHub was rewritten, find out why first. |
| `the commits change …: deploy them by hand` | Deploy by hand, as below. |
| `strata-q3.service is …` (activating, reloading, with a job queued, …) | Wait until the service has started or stopped. If the state stays unknown, look at `systemctl status strata-q3`. |
| `the newest run of test.yml for … is queued`, `in_progress`, or `missing` right after a push | Wait for the run. A commit whose message has `[skip ci]` gets no run, so `missing` stays: push another commit. |
| `the newest run of test.yml for … is completed failure` | Fix the commit. For another conclusion, such as `cancelled`, run the workflow again on GitHub. |
| `cannot read the runs of test.yml`, `… is unreadable` | No network, or GitHub's limit of 60 requests an hour without a token, or `remote.origin.url` no longer names the repository. |
| `scripts/test.sh of … failed`, `scripts/check-drift.sh of … failed` | See [When a check of the new commit fails](#when-a-check-of-the-new-commit-fails). |
| `another scripts/deploy.sh is running` | Wait until it finishes. |
| `interrupted` | Ctrl-C, SIGTERM or SIGHUP stopped the script before the fast-forward. It ended the check it was running and what it could find of what that check started; a `note` line names each process that this clean-up killed (another user's process is named, not killed). Run it again. An interrupt during the fast-forward lets git finish, and the script then says whether the commit is deployed. Once the script has said how the run ended, an interrupt changes nothing. |
| `this checkout moved while the checks ran` | Someone committed or switched branches here during the checks. Run the script again. |
| anything else, such as `git fetch origin main failed` or `origin is not a GitHub repository` | The reason names the command or the setting that failed. |

`deployed …, but scripts/check-drift.sh after it failed: …` means that the commits are deployed and the check after the fast-forward failed, for one of the reasons below. With `exit 1` and `DRIFT` lines above it, the machine changed while the script ran: find the cause with `scripts/check-drift.sh`. `deployed …, but the check after it was interrupted` means that the commits are deployed and the check after the fast-forward did not finish: run `scripts/check-drift.sh`.

### When a check of the new commit fails

The output above the reason shows the failing check. If `check-drift.sh` reports drift in files that the commits do not change, the machine has drifted: find the cause with `scripts/check-drift.sh` first. The reason after `failed:` says more:

- `strata-preflight.sh would refuse a start of strata-q3 while it ran`: while the check ran, a process that was not the service's had the preflight's engine pattern in its command line, or listened on port 8080. With `(killed)`, the check started it: fix the commit. Without it, the reason names the process: find out what runs it, and run the script again when it is gone.
- `what the watch found (its record is missing)`: the watch found such a process, and the check removed the record of which one. Fix the commit.
- `the watch did not finish (exit …)`: the watch ended before the check did, or the check kept it stopped (exit 137), so what ran meanwhile is not known. Run the script again. If it repeats, the commit's check ends or stops the watch: fix the commit.
- `the clean-up after the check did not finish (exit …)`: what the check started may still run (exit 4: its 20 passes still found something, such as a process that starts another each time one is killed). Look for it (`ps -u $USER -o pid,args`), end it, and run the script again.
- `it was killed (SIGKILL) after … s`: something ended the check before its time limit, for example the OOM killer at the account's memory limit (`oom_kill` in `/sys/fs/cgroup/user.slice/user-1003.slice/memory.events` tells). Look at what the commit's tests take, or at what else ran, and run the script again.
- `it did not finish in 1800 s`: the check hung, or the machine was busy. `scripts/test.sh` takes about 25 minutes here (1504 s on 2026-10-30: `results/test-sh-20261030.txt`). Look at what else runs, and run the script again.

## Deploy by hand

The script stops before its other checks when the commits change one of these, and you deploy them yourself:

- what applies outside this checkout: `systemd/`, `config/`, `scripts/strata-health.sh` and `scripts/install-system-files.sh`;
- the checks themselves, so that a commit cannot vouch for itself ([decision 0006](../decisions/0006-harnesses-in-ci-checks-by-hand.md)): `scripts/test.sh`, `scripts/check-drift.sh`, `scripts/host-units.sh`, `scripts/deploy.sh`, `tests/harness_run.py`, `.github/` and any `.gitattributes`.

1. Check that this checkout is on `main` with no local change, hidden or not; fetch; note the commit you are on and the one you will deploy (`<sha>`); and check that the newest `test` run for the push of `<sha>` to `main` passed:
   ```bash
   git symbolic-ref --short HEAD && git status --porcelain --untracked-files=all && git ls-files -v | grep -E '^([a-z]|S) '
   git fetch origin main && git rev-parse HEAD origin/main
   gh api "repos/thc1006/strata-p100-deployment/actions/workflows/test.yml/runs?head_sha=<sha>&event=push&branch=main" -q '.workflow_runs | max_by(.run_number) | "\(.status) \(.conclusion)"'
   ```
   Expected: `main` and nothing else from the first line, two commits from the second, and `completed success` from the third. `null null` means that there is no run yet: wait for it.
2. Read what the commits change in the checks:
   ```bash
   git diff HEAD <sha> -- scripts/test.sh scripts/check-drift.sh scripts/host-units.sh scripts/deploy.sh tests/harness_run.py .github/ ':(glob)**/.gitattributes'
   ```
   A check that is removed or made weaker needs its reason in the commit message or in a decision record. If it has none, stop here.
3. If the commits change `systemd/`, `scripts/strata-health.sh`, `config/profile.conf`, the config snapshots `config/strata-iq3_xxs.*.json`, or the engine or the model in `config/deployed-artifacts.sha256`, stop the watchdog, so that it does not start the service between the steps below: `sudo systemctl stop strata-q3-health.timer`. Until step 6 starts it again, `scripts/check-deployment.sh` reports the stopped timer as a `PROBLEM`.
4. Check that the service is not starting or stopping (`systemctl is-active strata-q3`: `active`, `inactive` or `failed`). Then fast-forward to exactly that commit, and run the tests:
   ```bash
   git merge --ff-only --no-overwrite-ignore <sha> && scripts/test.sh
   ```
   If `scripts/test.sh` fails, go back with `git reset --hard <the commit you were on>`, start the watchdog again if you stopped it, and look into the failure.
5. Right after that, follow the steps for what changed:
   - the units, the watchdog or the installer: `scripts/install-system-files.sh` ([Changing the configuration](change-config.md#change-the-systemd-units-or-the-watchdog)). If `systemd/zfs-arc-max.service` changed, also `sudo systemctl restart zfs-arc-max.service`: installing a oneshot does not run it again. If the `[Install]` section of a unit changed, also `sudo systemctl reenable <unit>`.
   - `config/host-units.conf`: `scripts/host-units.sh apply`. It does not enable or unmask a unit that the commits take off the list; do that by hand if they mean to bring it back.
   - `config/profile.conf`, or the config snapshots in `config/`: steps 2–4 of [Changing the configuration](change-config.md).
   - the engine or the model in `config/deployed-artifacts.sha256`: [Evaluating and upgrading Strata](upgrade-strata.md).
   - the other entries of `config/deployed-artifacts.sha256`, and `config/strata-venv.txt`: they record files and packages that the machine must already have. Step 7 tells whether it has them.

   Until these steps are done, a start of the service uses the new files of this checkout with the old live config and system files, and the preflight can refuse to start.
6. If you stopped the watchdog, start it again: `sudo systemctl start strata-q3-health.timer`.
7. Run `scripts/check-drift.sh`. It must report `no drift`. A `restart pending` line means that the service runs other settings or files than the repository now gives (step 5 changed the unit, or a file the service read at its start). Restart when the service can be down for about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`) with `sudo systemctl restart strata-q3`, and run the check again.

## Rollback

`scripts/deploy.sh` only moves forward. To undo a deployed commit, revert it on GitHub (`git revert <commit>` in a clone, then push), wait for its `test` workflow, and run `scripts/deploy.sh --apply` again. A revert of a commit that was deployed by hand changes the same paths, so deploy it by hand too, with the steps above.
