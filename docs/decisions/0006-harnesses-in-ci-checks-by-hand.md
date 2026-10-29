---
status: accepted
date: 2026-10-20
decision-makers: thc1006
---

# Run the stub harnesses that start fake engines in CI, and deploy commits that change the checks by hand

## Context and Problem Statement

On 2026-10-20 the maintainer asked for a review of `scripts/test.sh` and `scripts/check-drift.sh`, and then for every finding to be fixed. Two findings are about where the checks run:

- `scripts/test.sh` ran three stub harnesses on this machine: `tests/ab_engine_stubs.sh`, `tests/ab_model_run_stubs.sh` and `tests/switch_model_stubs.sh`, which `tests/test_ab_engine.py`, `tests/test_ab_model_run.py` and `tests/test_switch_model.py` start. Each of them starts fake engines with the command line that `scripts/strata-preflight.sh` takes for a second engine. While one ran, no start of `strata-q3` could succeed: not a restart after a crash, and not a start by the watchdog.
  - The unit tests of commit e5d7908 took 585.9 s here (`results/test-sh-20261020.txt`), and `scripts/deploy.sh` gave each check 600 s.
  - A harness runs in a session of its own. The time limit of `deploy.sh`, a Ctrl-C or a SIGTERM stopped the test, but the harness and its fake engines ran on (`tests/test_harness_run.py` fails against the harness runner of 136470d, the `origin/main` the review started from: `results/review-fixes-before-20261027.txt`).
  - CI did not run these harnesses: its container had no gcc, so the tests skipped themselves.
- `scripts/deploy.sh` runs the new commit's own `scripts/test.sh` and `scripts/check-drift.sh`, and accepts the newest run of the workflow that the commit itself defines. A commit that made `scripts/test.sh` print `all checks passed` would have passed both CI and the checks here, since both run that script. A commit that made the workflow skip the harnesses would have passed CI without them.

## Considered Options

1. The harnesses run in CI, and here only when asked for while the service is stopped; commits that change the checks are deployed by hand
2. The harnesses stay in `scripts/test.sh` here; `deploy.sh` gets a longer time limit and cleans up after each check
3. The fake engines get another command line, and the drivers' engine pattern becomes a setting

## Decision Outcome

Chosen option: "The harnesses run in CI, and here only when asked for while the service is stopped; commits that change the checks are deployed by hand", because the harnesses still run for every commit, with the engine pattern that the drivers use on the machine, and no check that runs here by default starts a fake engine.

- `scripts/test.sh` runs the harness tests only with `--harnesses` (`STRATA_TEST_HARNESSES=1`). Without it, they are skipped with a reason that starts with `harness:`. A run fails when a test is skipped for any other reason (`machine:` skips are allowed with `--repo-only`), when no test ran, when a test expects to fail, or when the tests do not finish.
- `tests/harness_run.py` does not start a harness with fake engines unless harness tests were asked for and nothing can start `strata-q3` meanwhile. The harness's file decides whether it is one. It is one when its file (the file a link leads to) names the engine's directory (`engine-cuda12`) or cannot be read, whatever its name. It is also one when its name, or the name of the file a link leads to, ends in `_stubs.sh` in any case of letters and is not `run_robustness_stubs.sh`. So a new `*_stubs.sh` is one until it is listed, and no argument of a test turns the gate off. A harness under another name that builds the engine's path from parts is not recognized; the review of its commit has to catch it.
  - On a machine that systemd runs, `strata-q3` must be inactive (not failed: the watchdog starts a failed one again) with no job queued, `strata-ab-rescue.timer` must be inactive, and no `bench/*.sh` driver may run.
  - Where systemd does not run, `STRATA_TEST_NO_SERVICE=1` must say that no `strata-q3` runs on the host; CI sets it. A container on this machine has no systemd either, and the host sees its processes, so a container alone does not count. The flag is not taken where the kernel has the NVIDIA driver loaded (`/sys/module/nvidia`), as this machine's does, also seen from a container on it.
- A harness test that is interrupted or runs past its time stops the harness's process group: SIGTERM, then SIGKILL 60 s later. Then, and after a normal end too, it kills every process still in the harness's session and every process whose command line names the harness's directory as a path, with its process group when it leads one. After a normal end the test fails on them, and one it cannot kill is reported; a run past its time fails as such; after an interrupt the run ends. It looks once more 0.2 s after a pass that finds nothing, since a process in the middle of its exec, after its `setsid`, has left the session and its command line reads empty. The harness's own command line names its directory from its start, and a harness names it in the command line of what it starts with `setsid`. Under the time limit of `deploy.sh` the harness does not get those 60 s: `scripts/test.sh` ends at the SIGTERM, and `deploy.sh` then kills what names its temporary directory, the harness among them (below).
- `.github/workflows/test.yml` installs gcc, libc6-dev, procps, iproute2, curl and shellcheck, and runs `scripts/test.sh --repo-only --harnesses` with `STRATA_TEST_NO_SERVICE=1` (since 2026-10-30 with `--jobs 6`: see More Information).
- `scripts/deploy.sh` stops before its other checks when the commits change one of these: `scripts/test.sh`, `scripts/host-units.sh`, `scripts/deploy.sh`, `tests/harness_run.py`, `.github/` or any `.gitattributes` (`git archive`, which exports the commit for its checks, follows `.gitattributes`). It already did so for `systemd/`, `config/`, `scripts/strata-health.sh`, `scripts/install-system-files.sh` and `scripts/check-drift.sh`. Such commits are deployed by hand ([Deploying a commit from GitHub](../runbooks/deploy.md)), after a person has read what they change in the checks.
- `deploy.sh` runs the checks of the new commit with nothing of its own environment but `PATH`, `HOME`, the locale, `USER`, `LOGNAME`, `XDG_RUNTIME_DIR` and `TERM`, so that a caller's test hooks, `BASH_ENV` or `PYTHONPATH` do not reach them, and with a TMPDIR under its own temporary directory.
  - While a check runs, it watches for what the preflight refuses a start for: a process with the preflight's engine pattern, or a listener on port 8080, that is not the service's. Either fails the check. One that has the checks' TMPDIR, or one under it, in its environment is the check's, in any cgroup and also when it left the check's session, and is killed at once. One that someone else started in the same login session is not, nor one whose environment cannot be read. The watch looks once more after the check ends, with a scan that starts after the end. A watch that does not finish, or that the check keeps stopped, fails the check.
  - After each check, and on an interrupt, it kills every process whose command line names its temporary directory as a path, and every process with the checks' TMPDIR, or one under it, in its environment, in any cgroup, with its process group when it leads one. It looks once more 0.2 s after a pass that finds nothing, and goes on when the terminal has hung up. A clean-up that does not finish, or whose 20 passes still find something, fails the check. A process that drops that TMPDIR, leaves the session and names nothing of the directory is not found: that takes intent, and code review is the gate against intent.
  - On an interrupt it first ends the running check and the watch, with SIGTERM and SIGCONT and, 15 s later, SIGKILL, so a check that stops its timeout or the watch does not hang the script. Once the outcome is known, an interrupt (SIGINT, SIGTERM or SIGHUP) no longer changes the message or the exit status. For the last check (`check-drift.sh --candidate` in a dry run, the check after the fast-forward with `--apply`) that holds from its end on, also before the script has seen it end; only a signal in the moment between that end and the shell's reaping of the check still counts as an interrupt. The watch ignores SIGHUP and acts on its first SIGTERM only.
  - A check also takes memory, which the engine needs and a start of `strata-q3` waits for. The account's limit bounds it ([decision 0007](0007-memory-limit-of-the-maintainers-account.md)), and the checks run with `oom_score_adj` 1000 (`choom`), so that at that limit the OOM killer takes their processes first. The checks and the watch run at nice 19.
  - The tests of the watch use copies of `deploy.sh` with a harmless pattern of their own, which holds a random part, and a port of their own, so that two runs side by side do not take each other's processes; a test checks that the script's pattern and port are the preflight's. The one test with the preflight's own pattern starts such a process for a moment, so it runs only as a harness test does (`harness_run.gate`).

### Consequences

- Good, because no check that runs here by default starts a fake engine, and `deploy.sh` fails and stops a check of a new commit that starts what the preflight refuses a start for.
- Bad, because `scripts/test.sh` still takes about 22 minutes here without the harnesses (1303 s, its unit tests 1300 s, on 2026-10-27: `results/test-sh-20261027.txt`); the unit tests of e5d7908 took 585.9 s with them (`results/test-sh-20261020.txt`). `deploy.sh` gives each check 1800 s (`LIMIT`, in git since d5dbb6c of 2026-10-27).
- Good, because CI runs the harnesses on every pull request and every push to `main`, with Ubuntu 22.04's bash, gcc and procps, as on the machine. Before, they ran only here.
- Bad, because a harness that passes in CI but fails with the machine's own programs shows only in `scripts/test.sh --harnesses` on a stopped service.
- Bad, because commits that change the checks need the steps by hand, among them a reading of the change.
- Bad, because the commit's own tests still check its other tests in `tests/` and its tools, so a commit that weakens one of them together with its tests passes. Only a review of the pull request catches that.

### Confirmation

- `tests/test_harness_run.py` tests the gate and the clean-up:
  - which harnesses count (a new `*_stubs.sh`, a link to one, a link with the name of one, a name in another case of letters, and one that names the engine's directory under any name), and that `run()` refuses one that was not asked for or when `not_now()` gives a reason;
  - every reason `not_now()` gives: `strata-q3` in any state but `inactive` or with a job queued, `strata-ab-rescue.timer` in any state but `inactive`, a `bench/*.sh` driver (also after `nice` or `taskset`, through a link or by a path through a linked directory), and no systemd without `STRATA_TEST_NO_SERVICE=1`, or with it where the NVIDIA driver is loaded or the kernel's modules cannot be read;
  - that an interrupt (SIGTERM, SIGHUP, SIGINT, a second one during the clean-up, or a `timeout` that signals its process group, as the one in `deploy.sh` does) leaves no process and no directory behind and ends the whole run, and that a run past its time leaves no process behind, its whole process group getting the SIGTERM so that bash runs its trap while it waits for a command in the foreground (`test_its_process_group_gets_the_sigterm`);
  - that the sweep kills and reports what is left in the harness's session or names its directory (also through a link or by its real path, and one that another starts 0.3 s after the first is killed), and leaves alone what only looks like it (`/x<dir>/y`, `<dir>x`).
- `tests/test_test_sh.py`: copies of `scripts/test.sh` fail on small trees with no test, with a test file that discovery does not load, with a skip for another reason, with a test that expects to fail, and with a test that ends the run.
- `tests/test_deploy.py`, against copies of `scripts/deploy.sh` in throwaway repositories:
  - commits that change each path in `MANUAL` are refused;
  - a check that starts a process with the engine pattern (also in a session of its own and naming nothing of the directory, also as the check ends, also when it ends during a scan of the watch) or a listener on the port (IPv4 or IPv6) fails, and the check's process is killed, also in a scope of its own; one that the check leaves to become an engine or a listener only after the watch's last look, also in a scope of its own, is killed after the check; one without the checks' TMPDIR, and a listener whose process cannot be read, fail the check and are left alone, and a process whose environment cannot be read is not taken for the check's;
  - what a check leaves behind is killed after a passing check, a failing one and an interrupt, and what only looks like it (`/x<dir>/y`, `<dir>x/y`) is not; a check that ends the watch or keeps it stopped fails, and so does one whose process the watch found, also when the check removes the watch's record of it, and a clean-up whose passes run out fails the check; a check that stops its timeout or the watch, and an interrupt just as the watch or a check starts, do not leave the script waiting; the clean-up goes on when the terminal hangs up and finds a process started 0.3 s after one is killed;
  - the checks run with `oom_score_adj` 1000 and see none of the caller's test hooks, `BASH_ENV` or `PYTHONPATH`;
  - SIGINT, SIGTERM, and a SIGHUP to the script's process group, stop the run while a check runs, and change nothing once the outcome is known, also after the last check of a dry run; a signal to the process group acts at once while the script waits between its looks at a check, and puts nothing on stderr but the script's message; an interrupt during the fast-forward, also a second one, lets it finish and says once whether the commit is deployed;
  - a SIGTERM to the script alone acts at the end of its current wait, within 0.1 s while the last check runs, so only a last check that ends within that 0.1 s is reported as it ended (`test_a_sigterm_to_the_script_during_a_nap_of_the_last_check`).
- In CI, `--harnesses` fails the run when a harness test is skipped.
- `results/review-fixes-before-20261027.txt`: these tests fail against the scripts as they were before the change. The reviews' and fixers' mutation runs, against broken copies of the scripts, are not kept in `results/`.

## Pros and Cons of the Options

Option 2:
- Good, because the harnesses would run with the machine's own programs before each deployment.
- Bad, because a start of the service would still fail while they run, and they run for most of each `scripts/test.sh`.

Option 3:
- Bad, because the drivers would look for the engine with one pattern in the harnesses and with another on the machine, so a wrong pattern on the machine would pass the harnesses.

## More Information

- This decision changes two parts of [decision 0003](0003-ci-on-github-deploy-by-pull.md): what CI runs, and the paths in check 2 of `deploy.sh`. Before it, a commit that changed `scripts/deploy.sh` was deployed by the old one without a person reading it, and its new `deploy.sh` then decided on the next commits.
- The steps for deploying by hand: [Deploying a commit from GitHub](../runbooks/deploy.md).
- 2026-10-30: CI runs the unit tests in six worker processes (`scripts/test.sh --jobs 6`), so the harnesses now run beside other tests. Two things follow. A harness that starts fake engines holds a lock shared while it runs, and the one test with the preflight's own pattern (`test_a_check_that_starts_what_the_preflight_takes_for_an_engine`) holds it alone (`harness_run.alone`). The copies of `deploy.sh` in the other tests watch for a harmless pattern and port: those of the class, or, in the tests of the watch, their own. On this machine `scripts/test.sh` still runs the unit tests in one worker process.
