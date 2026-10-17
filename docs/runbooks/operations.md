# Daily operations for the Strata service on 4× Tesla P100

- When to use: checking the service status, starting and stopping the service, reading the logs, sending a test request, when the service enters failed, and before a long test.
- Prerequisites: work on this machine with an account that has sudo. Run all commands below from the repo root, `~/dev/p100-q3-deployment`.
- Last verified: 2026-10-18, one restart after the directory reorganization; preflight and wait-ready worked normally.

## Check the status

```bash
scripts/check-deployment.sh   # service, API, startup log, GPU, memory, watchdog records
scripts/check-drift.sh        # whether the files on the system, the live config and the engine match the repo
```

Expected: the last line of `check-deployment.sh` is `no problems found`, and the last line of `check-drift.sh` is `no drift`. When either one finds a problem, it lists `PROBLEM` or `DRIFT` and exits with 1. You need sudo to read the records of the watchdog and of systemd itself: run `sudo -v` first; only then does `check-deployment.sh` list the events of the last 2 hours.

## Start and stop the service

Use only systemctl (for the reason, see [section 5 of the deployment record](../deployment-record.md)):

```bash
sudo systemctl restart strata-q3   # start/stop also work; usable after about 7–9 minutes
sudo journalctl -u strata-q3 --since -15min | grep -E 'strata-preflight|strata-wait-ready'
```

Expected: `strata-preflight: ok: 4 GPUs, … config matches (--profile deployed) …`, then `strata-wait-ready: loaded after N s`. After that, `systemctl is-active strata-q3` is `active`. While the service loads, the state is `activating`.

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

The watchdog retries automatically after 30 minutes, and after that it doubles the interval (up to 8 hours). If preflight still refuses when the watchdog retries, systemd tries again after 2 minutes, as set by `Restart=on-failure`. Counting the watchdog's attempt, the service starts at most 4 times in one hour (`StartLimitBurst=4`), and then it returns to failed. On 2026-10-17, the attempt at 22:13 was the watchdog and the one at 22:15 was systemd. At 22:17 a test script stopped the service, and there were no more attempts after that. To retry immediately:

```bash
sudo systemctl reset-failed strata-q3 && sudo systemctl start strata-q3
```

First, read `sudo journalctl -u strata-q3 -n 50` to find the cause of the failure. Preflight states why it refused to start (not enough GPUs, not enough memory, a config that does not match `config/profile.conf`, another engine already running, or `:8080` already in use). When the config does not match, it also prints the command that fixes it.

## Prepare for a long test

If the service is stopped while it is loading, it enters failed, and the watchdog starts it automatically after 30 minutes. The service can then compete with the test for the GPUs. Stop the watchdog for the duration of the test:

```bash
sudo systemctl stop strata-q3-health.timer    # start it again after the test
```

## Rerun the performance and robustness tests

```bash
bench/run_profile.sh <new-tag>                     # performance, about 24–42 minutes
bench/run_robustness.sh <new-tag> <soak-minutes>   # robustness, about 60 minutes for a soak of 30 minutes
python3 bench/report.py results > results/SUMMARY.md
```

The tag must not be the same as a tag that already exists in `results/`.
