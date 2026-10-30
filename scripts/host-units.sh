#!/bin/bash -p
# The systemd units this box does not run (config/host-units.conf).
#   scripts/host-units.sh [check]   read-only: one line per unit; exits 1 when a state differs (or cannot be read);
#                                   a unit that is not installed meets disabled and masked
#   scripts/host-units.sh apply     sets them (sudo for the system units), then checks
# A masked unit cannot be started at all, not even by D-Bus or a socket; undo one with `sudo systemctl unmask <unit>`
# (and `enable` it again if it was enabled: results/host-units-before-20261018.txt has the states before the change).
# Run by its path (scripts/check-drift.sh runs it with bash -p too), it is under bash -p (the first line), which takes
# no function (BASH_FUNC_*) and no option (SHELLOPTS, BASHOPTS) from the environment and runs no BASH_ENV file.  Run as
# "bash scripts/host-units.sh", it takes them: the options that change what it does are turned off and every function
# is removed (one named systemctl or sudo would answer for them), and one that is left (readonly, or named after a
# builtin that removes them) stops it.  One named compgen together with one named declare goes unnoticed: that takes
# an environment made so on purpose.  The names come from a list, not from read, which one named read would hold
set -u
set +e +f +a +k +C +o posix +o history
shopt -u nullglob failglob dotglob nocaseglob nocasematch extglob globstar lastpipe xpg_echo expand_aliases
for f in $(compgen -A function); do unset -f -- "$f"; done
if [[ -n $(declare -F) || -n $(compgen -A function) ]]; then
  echo "DRIFT  functions from the environment or a BASH_ENV file are left after their removal (one is readonly, or has the name of a builtin that removes them): no unit checked"
  exit 1
fi
set -u +e +f +a +k +C +o posix +o history         # again, now that no function stands in for set or shopt
shopt -u nullglob failglob dotglob nocaseglob nocasematch extglob globstar lastpipe xpg_echo expand_aliases
D=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)     # without CDPATH, under which cd would print the directory
CONF=${HOST_UNITS_CONF:-$D/config/host-units.conf}
MODE=${1:-check}
case $MODE in check|apply) ;; *) echo "usage: $0 [check|apply]" >&2; exit 2 ;; esac
# read only when it is a regular file (a FIFO would hang the loop below); one that is not is a DRIFT line, as a unit is
{ [ -f "$CONF" ] && [ -r "$CONF" ]; } || { echo "DRIFT  $CONF cannot be read, or is not a regular file: no unit checked"; exit 1; }

# the user instance is reached through XDG_RUNTIME_DIR, which a cron job or a timer's environment lacks (test hook:
# HOST_UNITS_RUN_USER for /run/user)
run_user=${HOST_UNITS_RUN_USER:-/run/user}/$(id -u)
[ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "$run_user" ] && export XDG_RUNTIME_DIR=$run_user
ctl() { if [ "$1" = user ]; then shift; systemctl --user "$@"; else shift; systemctl "$@"; fi; }
sudo_ctl() { if [ "$1" = user ]; then shift; systemctl --user "$@"; else shift; sudo systemctl "$@"; fi; }

bad=0 n=0
# the list is read on descriptor 3, so that what the loop runs (sudo, which may log its input, systemctl) gets this
# script's standard input, not the rest of the list
while read -r -u 3 state scope unit _ || [ -n "${state:-}" ]; do       # also a last line without a newline
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
  # is-enabled and is-active print the state and exit non-zero for most: read the text, not the code; an empty
  # answer (no systemd, no user instance) is a failure, not a pass.  Not running is inactive or failed only
  # (activating, reloading and deactivating are running).  is-enabled prints nothing for a unit that has no unit file
  # either: systemd's LoadState not-found tells that one apart, and a unit that does not exist cannot start, so it
  # meets disabled and masked (a new machine without docker or cups; apply's mask makes the link anyway)
  have=$(ctl "$scope" is-enabled "$unit" 2>/dev/null)
  active=$(ctl "$scope" is-active "$unit" 2>/dev/null)
  load=
  [ -n "$have" ] || load=$(ctl "$scope" show -p LoadState --value "$unit" 2>/dev/null)
  if [ "$have" = "$state" ] && { [ "$active" = inactive ] || [ "$active" = failed ]; }; then
    echo "ok     $scope $unit $have, $active"
  elif [ -z "$have" ] && [ "$load" = not-found ] && { [ "$active" = inactive ] || [ "$active" = failed ]; }; then
    echo "ok     $scope $unit not installed, $active"
  else
    echo "DRIFT  $scope $unit is ${have:-unreadable} / ${active:-unreadable}, want $state and not running"; bad=1
  fi
done 3< "$CONF"
[ "$n" -gt 0 ] || { echo "DRIFT  $CONF lists no units"; bad=1; }
exit $bad
