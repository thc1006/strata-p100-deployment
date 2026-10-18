#!/usr/bin/env bash
# The systemd units this box does not run (config/host-units.conf).
#   scripts/host-units.sh [check]   read-only: one line per unit; exits 1 when a state differs (or cannot be read)
#   scripts/host-units.sh apply     sets them (sudo for the system units), then checks
# A masked unit cannot be started at all, not even by D-Bus or a socket; undo one with `sudo systemctl unmask <unit>`
# (and `enable` it again if it was enabled: results/host-units-before-20261018.txt has the states before the change).
set -u
D=$(cd "$(dirname "$0")/.." && pwd)
CONF=${HOST_UNITS_CONF:-$D/config/host-units.conf}
MODE=${1:-check}
case $MODE in check|apply) ;; *) echo "usage: $0 [check|apply]" >&2; exit 2 ;; esac
[ -r "$CONF" ] || { echo "cannot read $CONF" >&2; exit 1; }

ctl() { if [ "$1" = user ]; then shift; systemctl --user "$@"; else shift; systemctl "$@"; fi; }
sudo_ctl() { if [ "$1" = user ]; then shift; systemctl --user "$@"; else shift; sudo systemctl "$@"; fi; }

bad=0 n=0
while read -r state scope unit _; do
  case $state in ''|'#'*) continue ;; esac
  n=$((n + 1))
  case "$state:$scope" in default:system|disabled:system|disabled:user|masked:system|masked:user) ;;
    *) echo "BAD    line: $state $scope $unit"; bad=1; continue ;; esac
  if [ "$MODE" = apply ]; then
    case $state in
      default)  sudo systemctl set-default "$unit" >/dev/null ;;
      disabled) sudo_ctl "$scope" disable --now "$unit" >/dev/null 2>&1 ;;
      masked)   sudo_ctl "$scope" mask --now "$unit" >/dev/null 2>&1 ;;
    esac
  fi
  if [ "$state" = default ]; then
    have=$(systemctl get-default 2>/dev/null)
    [ "$have" = "$unit" ] && echo "ok     default target $unit" || { echo "DRIFT  default target is ${have:-unreadable}, not $unit"; bad=1; }
    continue
  fi
  # is-enabled prints the state and exits non-zero for anything but enabled: read the text, not the code; an
  # empty answer (no systemd, no user instance) is a failure, not a pass
  have=$(ctl "$scope" is-enabled "$unit" 2>/dev/null)
  active=$(ctl "$scope" is-active "$unit" 2>/dev/null)
  if [ "$have" = "$state" ] && [ "$active" != active ] && [ "$active" != activating ]; then
    echo "ok     $scope $unit $have, $active"
  else
    echo "DRIFT  $scope $unit is ${have:-unreadable} / ${active:-unreadable}, want $state and not running"; bad=1
  fi
done < "$CONF"
[ "$n" -gt 0 ] || { echo "DRIFT  $CONF lists no units"; bad=1; }
exit $bad
