---
status: accepted
date: 2026-10-18
decision-makers: thc1006
---

# Test every commit on a GitHub-hosted runner and let this machine pull the commits it deploys

## Context and Problem Statement

Until 2026-10-18 the repository had no CI. `scripts/test.sh` ran only on this machine, before each commit made in the live checkout, and a pull request from someone else had no automatic test. The repository has been public since 2026-10-18.

The maintainer asked for CI, and for deployment to be ready for automation: a commit that has passed the checks should be able to reach this machine without manual steps. The constraints:

- The machine runs Ubuntu 22.04, with Python 3.10 and systemd 249. The checks should run with the same versions.
- The units run programs that exist only on this machine (`~/dev/strata/.venv/bin/python`, `/usr/local/sbin/strata-q3-health`), so the last two checks of `scripts/test.sh` fail anywhere else.
- A start of the service takes 7–9 minutes and other people use the service. The system files need root.

## Considered Options

1. CI on a GitHub-hosted runner; this machine pulls the commits that passed it
2. CI on a GitHub-hosted runner; a self-hosted runner on this machine deploys from a workflow
3. No CI; `scripts/test.sh` by hand, as before

## Decision Outcome

Chosen option: 1, because then code from GitHub runs on the production machine only after it is on `main`, and the machine needs no inbound connection and no GitHub credential to deploy.

- `.github/workflows/test.yml` runs `scripts/test.sh --repo-only` on every push to `main` and every pull request, as a user without privileges, with the repository mounted read-only. It uses no actions and no token.
- The job runs in Canonical's Ubuntu Base 22.04.5, a root file system that the job fetches from cdimage.ubuntu.com, checks against the release's SHA256SUMS entry and imports into Docker.
  - It does not use the `ubuntu-22.04` runner label: GitHub began deprecating that image on 2026-09-17 and stops supporting it on 2027-04-17.
  - It does not pull a container image: on 2026-10-18, Amazon ECR Public refused the anonymous pull of Canonical's `lts/ubuntu:22.04` from a GitHub runner with `toomanyrequests: Data limit exceeded` (run 39100153814), 55 minutes after the same pull had worked (run 39096952832).
- `scripts/deploy.sh` fast-forwards this checkout to `origin/main` after five checks:
  1. The checkout is on `main`, has no local changes, and can fast-forward.
  2. The commits change nothing that applies outside the checkout: `systemd/`, `config/`, the watchdog, the installer, or `scripts/check-drift.sh`, which CI cannot run.
  3. The service is not starting or stopping.
  4. The newest run of `test.yml` for the push of that commit to `main` passed.
  5. `scripts/test.sh` and `scripts/check-drift.sh` of that commit pass on this machine.
- Without `--apply`, the script stops after the checks. It never runs sudo and restarts nothing.

### Consequences

- Good: every pull request and every commit on `main` is tested with the machine's versions of Python and systemd.
- Good: a commit that changes the units, the watchdog, the Strata config or the deployed files still goes through the runbooks by hand, as before.
- Bad: CI does not check the programs outside the repository, and it skips the server integration test, which needs the Strata checkout. Only `scripts/test.sh` on this machine runs them.
- Bad: even without `--apply`, `deploy.sh` runs the `test.sh` and `check-drift.sh` of the new commit on this machine.
- Bad: a changed preflight or wait-ready first runs at the next start of the service. Neither CI nor `deploy.sh` runs them.
- Bad: each CI run downloads Ubuntu Base from cdimage.ubuntu.com and its packages from archive.ubuntu.com, so a run depends on both. The package versions follow Ubuntu's updates, as they do on the machine.
- Bad: when the machine moves to a newer Ubuntu release, the Ubuntu Base in the workflow has to move with it.

### Confirmation

- The `test` workflow on each pull request and each commit on `main`.
- `tests/test_deploy.py` runs `scripts/deploy.sh` in throwaway repositories, with stubs for GitHub's API and systemctl. CI runs it. On 2026-10-18 these tests failed on 92 of 94 copies of the script that each had one deliberate bug. The other two remove defensive branches that no test can reach (`results/deploy-sh-mutants-20261018.txt`).

## Pros and Cons of the Options

Option 2, a self-hosted runner on this machine:
- Good: a commit on `main` would deploy as soon as its run passed.
- Bad: GitHub's documentation says that self-hosted runners "should almost never be used for public repositories on GitHub, because any user can open pull requests against the repository and compromise the environment". Here, that environment is the production machine. In 2023, researchers ran code on PyTorch's self-hosted runners with a pull request from a fork, and published how in January 2024.

Option 3, no CI:
- Bad: pull requests stay untested, and nothing records which commits passed the checks.

## More Information

- [GitHub Docs: Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use): self-hosted runners in public repositories, and the least privilege for `GITHUB_TOKEN`
- [John Stawinski: Playing with Fire – How We Executed a Critical Supply Chain Attack on PyTorch](https://johnstawinski.com/2024/01/11/playing-with-fire-how-we-executed-a-critical-supply-chain-attack-on-pytorch/) (reported to Meta on 2023-08-09)
- [OpenGitOps principles](https://opengitops.dev/): "Software agents automatically pull the desired state declarations from the source."
- [actions/runner-images#14254](https://github.com/actions/runner-images/issues/14254): the deprecation of the Ubuntu 22.04 runner image
- [Ubuntu Base 22.04 releases](https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/), with `SHA256SUMS` signed by the Ubuntu CD image signing key
- The next step toward automatic deployment is a timer that runs `scripts/deploy.sh --apply`. It needs a unit in `systemd/` and a decision on when the service may restart. Until then, `deploy.sh` runs by hand ([Deploying a commit from GitHub](../runbooks/deploy.md)).
- When to re-evaluate: when the machine moves to a newer Ubuntu release, or when deployment should run on a timer.
