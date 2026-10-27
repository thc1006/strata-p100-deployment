---
status: accepted
date: 2026-10-18
decision-makers: thc1006
---

# Keep the operations repository separate from Strata and use Strata at a pinned version

## Context and Problem Statement

This machine runs upstream Strata 0.1.43.1 (commit `a0116b2`), unpacked from a zip into `~/dev/strata`. This repository holds the configuration, scripts, systemd units, documents and test results. The question was whether to switch to a fork of Strata as the base of this project.

## Considered Options

1. Keep the operations repository separate and use Strata at a pinned commit (the existing approach)
2. Use the fork `thc1006/Strata` as the base and put the operations files in the fork
3. Include Strata in the operations repository as a git submodule

## Decision Outcome

Chosen option: 1, for the reasons below (checked on 2026-10-18):

- The production service has not changed any Strata code. The per-layer embedding (PLE) read-ahead hint problem on ZFS is solved with `--no-ple-prefetch` ([deployment record, section 4](../deployment-record.md)), so a fork has nothing to hold at present.
- On GitHub, a fork of a public repository can only be public, and `thc1006/Strata` is public; this repository was private. (After the 2026-10-18 decision to make this repository public, this point no longer holds. The other reasons are unchanged.)
- Upstream changes quickly, and with the operations files mixed into a fork, every upgrade would require a merge from upstream.
- Upstream rewrote its git history once (#1276, 2026-10-06), so a pinned commit can lose its reference. A tag on the fork can handle this risk; the whole project does not need to become a fork.
- Upstream's `update.sh` runs `git pull` to the latest main. Production needs a pinned version that is tested before it replaces the current one.

### Consequences

- Good: an upgrade is "change the pinned version, rebuild, run the A/B" ([Evaluating and upgrading Strata](../runbooks/upgrade-strata.md)); the history of this repository holds only changes to this machine.
- Bad: Strata's source code is not in this repository. A rebuild has to download it from upstream ([Rebuilding from scratch](../runbooks/rebuild.md)). If upstream removes that commit, a tag on the fork or a separately saved source zip is needed.

### Confirmation

`scripts/check-drift.sh` compares the hashes of the live engine and config. The Strata version is recorded in `README.md` and `config/deployed-artifacts.sha256`.

## Pros and Cons of the Options

Option 2, fork as the base:
- Good: patches can go in directly, and the code and configuration are in one place.
- Bad: it can only be public; every release requires a merge from upstream; there are no patches at present.

Option 3, git submodule:
- Good: every commit records the exact Strata commit.
- Bad: the service path is hard-coded to `~/dev/strata`, but the submodule would sit under the repository, so the path would have to change; build artifacts and the venv would end up inside the submodule; it adds one more layer to operate.

## More Information

- Uses of `thc1006/Strata`: sending upstream PRs (#2918) and holding patch branches when needed. The deployed commit is not tagged there: the rebuild runbook downloads it from upstream by its commit hash (decided on 2026-10-18).
- When to re-evaluate: when the production service must run a change that upstream has not released, for example if #2918 is not accepted and a code fix should replace the flag. The approach then is to open a deployment branch on the fork and to record that branch's commit in this repository.
- At the next upgrade, make `~/dev/strata` a git checkout (of a specific tag), so that `git status` shows local changes.
