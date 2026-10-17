# System changes made by the Strata deployment and how to undo them

The table lists everything that this deployment changed outside the repo. `scripts/install-system-files.sh` installs `systemd/` and `scripts/strata-health.sh`; `scripts/check-drift.sh` checks whether the installed versions are the same as the repo.

| Change | Location | How to undo |
|---|---|---|
| CUDA 12.9.1 toolkit (toolkit only; the driver was not changed) | `~/cuda-12.9` (the engine depends on it; *do not delete it*) | Delete the directory (the engine will no longer run) |
| CUDA installer | `~/downloads/cuda/cuda_12.9.1_575.57.08_linux.run` (5.5 GiB, can be deleted; *do not install the 575 driver in it*) | `rm` |
| apt packages | `python3-venv`, `python3-pip`: setup.sh installed them with `sudo apt-get install -y python3 python3-venv python3-pip`, which also brought in 16 dependency packages (for example `python3-dev`). `gh`: installed separately on 2026-10-16 to work with GitHub; the service does not need it | `sudo apt remove python3-venv python3-pip gh`, then `sudo apt autoremove` |
| Strata source code and venv | `~/dev/strata` (a zip of a pinned commit, not a git checkout) | Delete the directory |
| Config backups | `~/dev/strata/strata-iq3_xxs.json.before-p100-q3*` (the backups that `tools/configure_strata_q3.py` makes before each write; 11 in total on 2026-10-16) | Delete the files after you confirm that you do not need to roll back |
| Old copy of the tool | `~/dev/strata/configure_strata_q3.py` (the version from 2026-10-16 13:43; neither the service nor the scripts use it; the current version is in `tools/`) | Can be deleted |
| Model data | `~/dev/Strata-data` (118.5 GiB: models 70.6, packs 41.4, mtp 6.5 GiB) | Delete the directory |
| Strata trees for version testing | `~/dev/strata-git` (partial clone of upstream), `~/dev/strata-0.1.44.1`, `~/dev/strata-0.1.45`, `~/dev/strata-0.1.45-locked` (the branch of upstream PR #2918), `~/dev/strata-pr2050` (PR #2050, not adopted) | Delete the directories; the production service does not depend on them |
| systemd units | `/etc/systemd/system/strata-q3.service`, `strata-q3-health.{service,timer}`, `zfs-arc-max.service` | `sudo systemctl disable --now <unit>`, then delete the files and run `sudo systemctl daemon-reload` |
| Watchdog script | `/usr/local/sbin/strata-q3-health` (a root-owned copy of `scripts/strata-health.sh`); the counter file is in `/run/strata-q3-health` (tmpfs, cleared at reboot) | Delete the file |
| Log rotation | `/etc/logrotate.d/strata-q3` (rotates when the log is larger than 50 MiB, keeps 8 copies) | Delete the file |
| ZFS Adaptive Replacement Cache (ARC) | `zfs_arc_max=8 GiB`, `zfs_arc_shrinker_limit=0` (runtime settings, applied by `zfs-arc-max.service`) | `sudo systemctl disable zfs-arc-max`, then reboot |
| setup settings | `~/.config/strata/settings.json` (records the data directory) | Delete the file |
