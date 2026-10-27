# Daily operations for the Strata service on 4× Tesla P100

- When to use: checking the service status, starting and stopping the service, reading the logs, sending a test request, when the service enters failed, and before a long test.
- Prerequisites: work on this machine with an account that has sudo. Run all commands below from the repo root, `~/dev/p100-q3-deployment`.
- Last verified: 2026-10-18, one restart after the directory reorganization; preflight and wait-ready worked normally (`results/journal-20261018-0023.txt`).

## Check the status

```bash
scripts/check-deployment.sh   # service, API, startup log, GPU, memory, watchdog records
scripts/check-drift.sh        # whether the files on the system, the live config, the engine and the running service match the repo
```

Expected: the last line of `check-deployment.sh` is `no problems found`, and the last line of `check-drift.sh` is `no drift`. When either one finds a problem, it lists `PROBLEM` or `DRIFT` and exits with 1. You need sudo to read the records of the watchdog and of systemd itself: `check-deployment.sh` lists the last 12 events of the last 2 hours (the watchdog's findings, the preflight, wait-ready, starts and failures, not the routine lines of every health check) when `sudo -n` works without a password. If your sudo asks for one, run `sudo -v` first.

## Start and stop the service

Use only systemctl (for the reason, see [section 5 of the deployment record](../deployment-record.md)):

```bash
sudo systemctl restart strata-q3   # start/stop also work; usable after about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`)
sudo journalctl -u strata-q3 --since -15min | grep -E 'strata-preflight|strata-wait-ready'
```

Expected: `strata-preflight: ok: 4 GPUs, … config matches (--profile deployed --model iq3_s) …`, then `strata-wait-ready: loaded after N s`. After that, `systemctl is-active strata-q3` is `active`. While the service loads, the state is `activating`.

## Read the logs

```bash
sudo journalctl -u strata-q3 -u strata-q3-health -f   # server and preflight
sudo journalctl -t strata-health -f                    # watchdog (-u and -t cannot be in the same command)
tail -f ~/dev/strata/strata-iq3_xxs.log               # engine
```

When the service is healthy, the watchdog writes no records, and `-u strata-q3-health` shows only one Starting/Finished pair every 2 minutes. While the service is starting (`activating`, which includes preflight waiting for memory and the load, up to 75 minutes), even this pair does not appear: this unit sets `After=strata-q3.service`, so a check that comes due waits until the start has finished. In any case, the watchdog does not act on a service that is starting.

## Send a test request

```bash
curl -N http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' \
  --data-binary @examples/requests/request-normal.json
```

Expected: all example requests set `"stream": true`. The response is a series of `data: {…"choices"…}` lines (possibly with `: keep-alive` in between), and the last line is `data: [DONE]`. In `request-fast.json`, `max_tokens` is only 2048, so long answers are cut off (deployment record, section 5).

## Recover from the failed state

The watchdog retries automatically after 30 minutes, and after that it doubles the interval (up to 8 hours). If preflight still refuses when the watchdog retries, systemd tries again after 2 minutes, as set by `Restart=on-failure`. Counting the watchdog's attempt, the service starts at most 4 times in one hour (`StartLimitBurst=4`), and then it returns to failed. On 2026-10-17, the attempt at 22:13 was the watchdog and the one at 22:15 was systemd. At 22:17 a test script stopped the service, and there were no more attempts after that (`results/journal-20261017-2140.txt`; the script's log: `results/ab-v045b-ext4-driver.log`). To retry immediately:

```bash
sudo systemctl reset-failed strata-q3 && sudo systemctl start strata-q3
```

First, read `sudo journalctl -u strata-q3 -n 50` to find the cause of the failure. Preflight states why it refused to start (not enough GPUs, not enough memory, a config that does not match `config/profile.conf`, another engine already running, or `:8080` already in use). When the config does not match, it also prints the command that fixes it.

## Services this box does not run

The box serves only Strata: it boots into `multi-user.target` (no desktop), and the idle daemons are disabled or masked. `config/host-units.conf` lists each unit with the reason, and `scripts/check-drift.sh` checks them.

```bash
scripts/host-units.sh          # read-only: one ok or DRIFT line per unit, exit 1 on any DRIFT
scripts/host-units.sh apply    # sets them again with sudo, for example after a release upgrade enabled one
```

To bring one back: `sudo systemctl unmask <unit>`, then `sudo systemctl enable --now <unit>` if it was enabled before (the states before the change are in `results/host-units-before-20261018.txt`), and remove its line from `config/host-units.conf` in the same commit. Remote access (ssh, tailscaled, netbird), the network, ZFS, NVIDIA, cron, logging and unattended security updates are not in the list and must stay on.

## The memory limit of the maintainer's account

The account `thc1006` may use at most 6 GiB of RAM and no swap ([decision 0007](../decisions/0007-memory-limit-of-the-maintainers-account.md)), so that none of its jobs can push the service out of memory: a job that passes the limit is killed by the kernel inside the account (`Killed`, exit status 137): the account's process with the highest `oom_score` (its size plus its `oom_score_adj`) first. The service runs in `system.slice` and is not limited, and neither are jobs that cron starts for the account (decision 0007).

```bash
systemctl show -p MemoryMax,MemorySwapMax user-1003.slice                 # 6442450944 and 0
cat /sys/fs/cgroup/user.slice/user-1003.slice/memory.{current,events}    # bytes in use; oom_kill counts the kills
```

- Start a long or heavy job with a limit of its own, so that it goes before your shell: `systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 -- choom -n 500 -- <command>`.
- A job that needs more, such as a CUDA build with `-j 6`: lift the limit for it while the service can spare the memory (MemAvailable minus what the job takes stays above 6 GiB, the service's floor), and set it back after:
  ```bash
  sudo systemctl set-property --runtime user-1003.slice MemoryMax=12G
  sudo systemctl set-property --runtime user-1003.slice MemoryMax=6G
  ```
  The lifted limit lasts until the next `daemon-reload`. systemd applies drop-ins in the order of their names, and the installed `50-memory.conf` comes after the `50-MemoryMax.conf` that `set-property` writes (capital letters sort first), so a reload puts the 6 GiB back. After a reload during the job, lift it again.
- Do not map the model's or the pack's files with mmap (a GGUF reader, a pack tool) while the service is stopped: if it then starts and locks those pages, they count against the account's limit until the engine stops. `scripts/check-deployment.sh` reports locked memory charged to the account.

## Prepare for a long test

If the service is stopped while it is loading, it enters failed, and the watchdog starts it automatically after 30 minutes. The service can then compete with the test for the GPUs. Stop the watchdog for the duration of the test:

```bash
sudo systemctl stop strata-q3-health.timer    # start it again after the test
```

## Rerun the performance and robustness tests

```bash
bench/run_profile.sh <new-tag>                     # performance, about 24–42 minutes (results/*-perf.log)
bench/run_robustness.sh <new-tag> <soak-minutes>   # robustness, about 60 minutes for a soak of 30 minutes (results/*-robustness.log)
python3 bench/report.py results > results/SUMMARY.md
```

The tag must not be the same as a tag that already exists in `results/`.
