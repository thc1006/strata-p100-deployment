#!/usr/bin/env bash
# Install the repo's copies of the files this deployment keeps outside the repo, the way they were installed on
# 2026-10-16: the systemd units, their drop-ins (systemd/<unit>.d/*.conf: the memory limit of the maintainer's
# account, decision 0007) and the logrotate rule (root, 0644), and the watchdog as a root-owned copy (0755).  Only
# files whose content, owner or mode differ are installed, and an installed unit or drop-in triggers a daemon-reload.
# Nothing is restarted: a changed strata-q3.service takes effect at its next start.
# Usage: scripts/install-system-files.sh [--dry-run] [--enable]
#   --enable   also enable zfs-arc-max.service (now), strata-q3.service and strata-q3-health.timer: a fresh machine
#              (docs/runbooks/rebuild.md)
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
DRY=0; ENABLE=0
for a in "$@"; do
  case $a in --dry-run) DRY=1 ;; --enable) ENABLE=1 ;; *) echo "unknown option: $a"; exit 2 ;; esac
done
run() { if [ $DRY = 1 ]; then echo "  would run: $*"; else "$@" || exit 1; fi; }
reload=0
put() {   # source target mode
  if cmp -s "$1" "$2" && [ "$(stat -c '%U:%G %a' "$2")" = "root:root ${3#0}" ]; then echo "same     $2"; return; fi
  echo "install  $2"
  [ -d "$(dirname "$2")" ] || run sudo install -d -o root -g root -m 0755 "$(dirname "$2")"
  run sudo install -o root -g root -m "$3" "$1" "$2"
  case $2 in /etc/systemd/system/*) reload=1 ;; esac
}
for u in strata-q3.service strata-q3-health.service strata-q3-health.timer zfs-arc-max.service; do
  put "$D/systemd/$u" "/etc/systemd/system/$u" 0644
done
for f in "$D"/systemd/*.d/*.conf; do
  [ -f "$f" ] && put "$f" "/etc/systemd/system/${f#"$D"/systemd/}" 0644
done
put "$D/systemd/strata-q3.logrotate" /etc/logrotate.d/strata-q3 0644
put "$D/scripts/strata-health.sh" /usr/local/sbin/strata-q3-health 0755
[ $reload = 1 ] && run sudo systemctl daemon-reload
if [ $ENABLE = 1 ]; then
  run sudo systemctl enable --now zfs-arc-max.service
  run sudo systemctl enable strata-q3.service strata-q3-health.timer
fi
exit 0
