# Deploying a commit from GitHub

`scripts/deploy.sh` brings this checkout up to `origin/main`. Why it works this way: [decision 0003](../decisions/0003-ci-on-github-deploy-by-pull.md).

- When to use: `main` on GitHub has commits that this checkout does not, for example a merged pull request or a change pushed from another clone. A commit made in this checkout is already in place: the service runs it at its next start.
- Prerequisites: the `test` workflow has finished on the newest commit (`gh run list -w test.yml -L 3`).
- Last verified: not yet against GitHub. `tests/test_deploy.py` runs the script against stubs.

## Steps

1. Check what would be deployed:
   ```bash
   scripts/deploy.sh
   ```
   Expected: the new commits, one `ok` line per check, and `dry run: scripts/deploy.sh --apply deploys <commit>`. This already runs the new commit's `scripts/test.sh` and `scripts/check-drift.sh`. When a check fails, the script prints `not deployed:` and the reason, and leaves the checkout as it was.
2. Deploy:
   ```bash
   scripts/deploy.sh --apply
   ```
   Expected: `deployed <commit>: <subject>`. If the commits change the preflight, wait-ready or `tools/`, a `note` line lists them: the service runs them at its next start. To run them now, restart when the service can be down for 7–9 minutes, with `sudo systemctl restart strata-q3`, and then run `scripts/check-deployment.sh`.

## When the script refuses

| Reason starts with | What to do |
|---|---|
| `this checkout is not on branch main` | `git switch main`, then run the script again. |
| `this checkout has local changes`, `this checkout hides files from git status` | Commit and push the work, or remove it (`git status`, `git ls-files -v`). |
| `origin/main (…) is not a fast-forward` | This checkout has commits that GitHub does not. Push them. If `main` on GitHub was rewritten, find out why first. |
| `the commits change …, which apply outside this checkout` | Deploy by hand, as below. |
| `strata-q3.service is …` (activating, reloading, with a job queued, …) | Wait until the service has started or stopped. If the state stays unknown, look at `systemctl status strata-q3`. |
| `the newest run of test.yml for … is queued`, `in_progress`, or `missing` right after a push | Wait for the run. A commit whose message has `[skip ci]` gets no run, so `missing` stays: push another commit. |
| `the newest run of test.yml for … is completed failure` | Fix the commit. For another conclusion, such as `cancelled`, run the workflow again on GitHub. |
| `cannot read the runs of test.yml`, `… is unreadable` | No network, or GitHub's limit of 60 requests an hour without a token, or `remote.origin.url` no longer names the repository. |
| `scripts/test.sh of … failed`, `scripts/check-drift.sh of … failed` | The output above the reason shows the failing check. If check-drift.sh reports drift in files that the commits do not change, the machine has drifted: find the cause with `scripts/check-drift.sh` first. |
| `another scripts/deploy.sh is running` | Wait until it finishes. |
| `interrupted` | Ctrl-C, SIGTERM or SIGHUP stopped the script before the fast-forward, and the check it was running. Run it again. |
| `this checkout moved while the checks ran` | Someone committed or switched branches here during the checks. Run the script again. |
| anything else, such as `git fetch origin main failed` or `origin is not a GitHub repository` | The reason names the command or the setting that failed. |

`deployed, but scripts/check-drift.sh now finds drift` means that the commits are deployed and that the machine changed while the script ran. Find the cause with `scripts/check-drift.sh`.

## Deploy by hand

Commits that change `systemd/`, `config/`, `scripts/strata-health.sh`, `scripts/install-system-files.sh` or `scripts/check-drift.sh` need steps outside this checkout, so the script stops before its other checks. Make them yourself:

1. Check that the newest `test` run of `main` passed (`gh run list -w test.yml -L 3`) and that the service is not starting or stopping (`systemctl is-active strata-q3`).
2. Run `git pull --ff-only origin main` and `scripts/test.sh`.
3. Right after that, follow the steps for what changed:
   - the units or the watchdog: `scripts/install-system-files.sh` ([Changing the configuration](change-config.md#change-the-systemd-units-or-the-watchdog))
   - `config/profile.conf`: steps 2–4 of [Changing the configuration](change-config.md)
   - the engine or the model in `config/deployed-artifacts.sha256`: [Evaluating and upgrading Strata](upgrade-strata.md)

   Until they are done, a start of the service, also one by the watchdog, uses the new files of this checkout with the old live config and system files, and the preflight can refuse to start.
4. Run `scripts/check-drift.sh`. It must report `no drift`.

## Rollback

`scripts/deploy.sh` only moves forward. To undo a deployed commit, revert it on GitHub (`git revert <commit>` in a clone, then push), wait for its `test` workflow, and run `scripts/deploy.sh --apply` again.
