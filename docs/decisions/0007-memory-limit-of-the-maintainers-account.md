---
status: accepted
date: 2026-10-21
decision-makers: thc1006
---

# Limit the maintainer's account to 6 GiB of RAM and no swap, so that none of its jobs can push the service out of memory

## Context and Problem Statement

On 2026-10-21 at 03:10:38 the kernel's OOM killer ended the engine of `strata-q3` (`results/oom-incident-20261021.txt`, sections 1 and 2). A review job in the maintainer's login session had built a test input that grew to 29 GiB of resident memory. With `OOMPolicy=stop` the service stopped (`Result=oom-kill`), `Restart=on-failure` started it again after `RestartSec=120`, and it was ready at 03:21:45: 11 minutes without the service.

- Nothing bounded what the account's processes use: a process in its login session grew to 29.1 GiB of resident memory (RSS 30,565,052 KiB, section 2), `user-1003.slice` had no `memory.max` until the limit of 09:22 (section 6), `user.slice` has none (section 3), and systemd-oomd watches only `user@1003.service`, not the login sessions where shells, tmux and Claude Code run (section 3, `oomctl`).
- The engine is the OOM killer's victim with or without its `OOMScoreAdjust=500`. Its `oom_score` of 1361 is (1000 + p) × 2/3, rounded down, with p = 1042 per mille of RAM and swap, 500 of them the adjustment (section 3). Its own 542 is above the about 380 that another job reaches when it takes all that the kernel can give it without a kill (MemAvailable 22.8 GiB and 1.8 GiB of free swap, of 64.8 GiB).
- The room the service leaves: in the heaviest measured run, MemAvailable fell to 16.8 GiB (`results/AB-iq3s-ab3.md`, ab3-iq3s-1). Parking can take up to its budget of 8 GiB more (`--conversation-cache-mib 8192` in `config/strata-iq3_xxs.deployed.json`), and Strata 0.1.45 checks its 6 GiB floor only when it parks a conversation or saves or restores a session: nothing gives parked memory back when other processes take memory later (`src/program/generate.cpp` at the pinned commit, the parking admission). So about 8.8 GiB can be left.
- What the account uses: 0.73 GiB at 09:40 (section 3); a full `scripts/test.sh` peaks at 0.12 GiB (section 5).
- Where page cache is charged: on this ZFS, `read()` charges nothing to the reader's cgroup, while `mmap` charges what it touches, which stays charged after `munmap` (section 4).

## Considered Options

1. A memory limit on the maintainer's account, `user-1003.slice`
2. A memory limit on every account, `user.slice`
3. Memory protection for the service (`MemoryMin=` or `MemoryLow=` on `strata-q3.service`)
4. A lower `OOMScoreAdjust` for the service
5. A limit on each job when it is started (`systemd-run --user --scope -p MemoryMax=...`)

## Decision Outcome

Chosen option: 1, `MemoryMax=6G` and `MemorySwapMax=0` on `user-1003.slice`, with option 5 as the way jobs are run, because the kernel then enforces the limit for everything the account runs in its login sessions and its user manager, by hand or by an agent, and a job that passes it is killed inside the account, never the engine. The service's own settings do not change, so it needs no restart, and no other account is limited.

- 6 GiB of the about 8.8 GiB that the service can leave keeps about 2.8 GiB; 8 GiB would keep 0.8. It is about eight times the 0.73 GiB that the account used at 09:40.
- No swap for the account: a job at the limit is killed at once instead of filling the 2 GiB swap partition (section 3), which is on the disk the engine reads its experts from.
- The drop-in is `systemd/user-1003.slice.d/50-memory.conf`, installed by `scripts/install-system-files.sh` as `/etc/systemd/system/user-1003.slice.d/50-memory.conf`. Until it was installed, the same settings were set at 09:22 with `sudo systemctl set-property --runtime`, which lasts until a reboot (section 6).

### Consequences

- Good, because no job of the account can push the service out of RAM, however it fails: a process of the account that allocated 7 GiB was killed inside the account in 8 s, and the engine, its PID and the service's OOM count did not change (section 6).
- Good, because nothing changes for the service or for the other accounts.
- Bad, because a job that needs more than 6 GiB fails here. A CUDA build with `-j 6` may: lower `-j`, or lift the limit for the build (`sudo systemctl set-property --runtime user-1003.slice MemoryMax=...`) while MemAvailable allows it, and set it back after. That lasts only until the next `daemon-reload`: systemd applies drop-ins in the order of their names, and the installed `50-memory.conf` comes after the `50-MemoryMax.conf` that `set-property` writes (capital letters sort first), so a reload during the build puts the 6 GiB back. An engine started by hand needs a system unit, as `bench/` starts its engines.
- Bad, because pages that the account maps first stay charged to it. If the account maps the model's files with mmap (a GGUF reader, a pack tool) while the service is stopped, and the service then starts and locks those pages, up to 6 GiB of locked memory counts against the account's limit until the engine stops, and the account's processes are killed or cannot allocate. A plain read does not do this (section 4). `scripts/check-deployment.sh` reports locked memory charged to the account.
- Bad, because jobs that cron starts for the account run in `cron.service`, outside the slice and without its limit: Ubuntu 22.04's `/etc/pam.d/cron` does not use `pam_systemd`, so they get no login session. The account had no crontab and no user timer on 2026-10-28 (`results/cron-account-20261028.txt`, with cron's PAM files); a job added there needs a limit of its own (`systemd-run --user --scope -p MemoryMax=...`), or a user timer.
- Bad, because when the account is full, the OOM killer takes the account's process with the highest `oom_score`, its size plus its `oom_score_adj`. A job started under `choom -n 500`, as the agents' jobs are, goes before the maintainer's shell.

### Confirmation

- `scripts/check-drift.sh` compares the installed drop-in with the repository's, and with its values the limits that systemd has loaded (`systemctl show -p MemoryMax,MemorySwapMax user-1003.slice`) and that the cgroup holds (`memory.max`, `memory.swap.max`).
- `scripts/check-deployment.sh` reports a limit that is not in force, and more than 256 MiB of locked memory charged to the account.
- Section 6 of `results/oom-incident-20261021.txt`: a 7 GiB allocation in the account is killed inside it.

## Pros and Cons of the Options

Option 2: Bad, because it limits the other accounts too, while `strata-q3.service` says that the service is the one to go under memory pressure, not another user's job.

Option 3: Bad, because protection does not choose the OOM killer's victim: when the unprotected memory is gone, the engine is still the largest process and goes. It also needs protection on `system.slice` to take effect.

Option 4: Bad, because the adjustment would have to go below about -160 to spare the engine (its own 542 points, less the adjustment, under the about 380 that another job reaches; see Context), which reverses the service's documented choice toward other users' jobs, and it takes effect only at a restart, about 9 minutes without the service (550-556 s for IQ3_S: `results/ab3-iq3s-*-switch.txt`).

Option 5: Good, and kept for every job that the agents and the tests start; bad alone, because a job that nobody wraps has no limit, as on 2026-10-21.

## More Information

- Re-evaluate when the service's memory changes (another model, a larger parking budget), when the account's work needs more, or when another account starts jobs on the machine.
