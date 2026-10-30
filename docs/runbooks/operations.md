# Daily operations for the Strata service on 4× Tesla P100

- When to use: checking the service, starting and stopping it, reading the logs, sending a test request, recovering from the failed state, and before a long test.
- Prerequisites: an account on this machine with sudo. Run the commands below from the repo root, `~/dev/p100-q3-deployment`.
- Last verified: 2026-10-18, one restart after the directory reorganization; the preflight and wait-ready worked normally (`results/journal-20261018-0023.txt`).

## Check the status

```bash
scripts/check-deployment.sh   # service, API, startup log, GPU, memory, watchdog records
scripts/check-drift.sh        # whether the files on the system, the live config, the engine and the running service match the repo
```

Expected: the last line of `check-deployment.sh` is `no problems found`, and the last line of `check-drift.sh` is `no drift`. When either one finds a problem, it prints `PROBLEM` or `DRIFT` lines and exits with 1. `check-deployment.sh` also lists the last 12 events of the last 2 hours (the watchdog's findings, the preflight, wait-ready, starts and failures, not the routine lines of every health check). It reads them from the system journal, which needs sudo: if your sudo asks for a password, run `sudo -v` first.

## Start and stop the service

Use only systemctl (the reason is in [section 5 of the deployment record](../deployment-record.md)):

```bash
sudo systemctl restart strata-q3   # start/stop also work; usable after about 9 minutes (550–556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`)
sudo journalctl -u strata-q3 --since -15min | grep -E 'strata-preflight|strata-wait-ready'
```

Expected: `strata-preflight: ok: 4 GPUs, … config matches (--profile deployed --model iq3_s) …`, then `strata-wait-ready: loaded after N s`. `systemctl is-active strata-q3` is `activating` while the service loads, and `active` after that.

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

Expected: every example request sets `"stream": true`, so the response is a series of `data: {…"choices"…}` lines (possibly with `: keep-alive` lines between them), and the last line is `data: [DONE]`. `request-fast.json` sets `max_tokens` to 2048, so long answers are cut off (deployment record, section 5).

## Recover from the failed state

The watchdog starts a failed service again after 30 minutes, then after doubling intervals up to 8 hours. If the preflight refuses that start, systemd tries again 2 minutes later (`Restart=on-failure`). Counting the watchdog's attempt, the service starts at most 4 times in one hour (`StartLimitBurst=4`), and then stays failed. `results/journal-20261017-2140.txt` shows one of each on 2026-10-17: the watchdog's attempt at 22:13 and systemd's at 22:15, both refused by the preflight, and no third because the service was stopped at 22:17.

First read `sudo journalctl -u strata-q3 -n 50` to find the cause. The preflight says why it refused, for example not enough GPUs, not enough memory, a config that does not match `config/profile.conf`, another engine already running, or `:8080` already in use. When the config does not match, it also prints the command that fixes it. To retry at once:

```bash
sudo systemctl reset-failed strata-q3 && sudo systemctl start strata-q3
```

## Services this box does not run

The box serves only Strata: it boots into `multi-user.target` (no desktop), and the idle daemons are disabled or masked. `config/host-units.conf` lists each unit with the reason, and `scripts/check-drift.sh` checks them.

```bash
scripts/host-units.sh          # read-only: one ok or DRIFT line per unit, exit 1 on any DRIFT
scripts/host-units.sh apply    # sets them again with sudo, for example after a release upgrade enabled one
```

A `disabled` unit that the machine does not have passes as `not installed`. The unit tests check each name of the list against the records of their states before the change, `results/host-units-before-*.txt`, so a misspelt name fails `scripts/test.sh`, and a unit added to the list needs such a record.

To bring one back: `sudo systemctl unmask <unit>`, then `sudo systemctl enable --now <unit>` if it was enabled before (the states before the change are in `results/host-units-before-20261018.txt`), and remove its line from `config/host-units.conf` in the same commit. Remote access (ssh, tailscaled, netbird), the network, ZFS, NVIDIA, cron, logging and unattended security updates are not in the list and must stay on.

## The memory limit of the maintainer's account

The account `thc1006` may use at most 6 GiB of RAM and no swap, so that none of its jobs can push the service out of memory ([decision 0007](../decisions/0007-memory-limit-of-the-maintainers-account.md)). A job that passes the limit is killed by the kernel inside the account (`Killed`, exit status 137): the account's process with the highest `oom_score` (its size plus its `oom_score_adj`) first. The service runs in `system.slice` and is not limited, and neither are jobs that cron starts for the account.

```bash
systemctl show -p MemoryMax,MemorySwapMax user-1003.slice                 # 6442450944 and 0
cat /sys/fs/cgroup/user.slice/user-1003.slice/memory.{current,events}    # bytes in use; oom_kill counts the kills
```

- Start a long or heavy job with a limit of its own, so that it is killed before your shell: `systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 -- choom -n 500 -- <command>`.
- A job that needs more, such as a CUDA build with `-j 6`: lift the limit for it while the service can spare the memory (MemAvailable minus what the job takes stays above 6 GiB, the service's floor), and set it back after:
  ```bash
  sudo systemctl set-property --runtime user-1003.slice MemoryMax=12G
  sudo systemctl set-property --runtime user-1003.slice MemoryMax=6G
  ```
  The lifted limit lasts until the next `daemon-reload`, which puts the 6 GiB back: systemd applies drop-ins in the order of their names, and the installed `50-memory.conf` comes after the `50-MemoryMax.conf` that `set-property` writes (capital letters sort first). After a reload during the job, lift it again.
- Do not map the model's or the pack's files with mmap (a GGUF reader, a pack tool) while the service is stopped: if it then starts and locks those pages, they count against the account's limit until the engine stops. `scripts/check-deployment.sh` reports locked memory charged to the account.

## Prepare for a long test

If the service is stopped while it is loading, it enters failed (`results/journal-20261017-2140.txt`, 21:42), and the watchdog starts it automatically after 30 minutes. The service can then compete with the test for the GPUs. Stop the watchdog for the duration of the test:

```bash
sudo systemctl stop strata-q3-health.timer    # start it again after the test
```

## Rerun the performance and robustness tests

```bash
bench/run_profile.sh <new-tag>                     # performance, about 24–42 minutes (results/*-perf.log)
bench/run_robustness.sh <new-tag> <soak-minutes>   # robustness, about 60 minutes for a soak of 30 minutes (results/*-robustness.log)
python3 bench/report.py results > results/SUMMARY.md
```

Use a tag that no file in `results/` has yet: the scripts refuse a tag whose result files exist.
