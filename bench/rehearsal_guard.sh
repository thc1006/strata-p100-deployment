#!/bin/bash
# bench/rehearsal_guard.sh [--once|--check]: the rebuild rehearsal's guard.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# Every 30 s it freezes the user scopes rr-*.scope (the jobs of bench/rehearsal_rr.sh) while strata-q3 is not active
# (a restart's load reads its model files from disk), while MemAvailable is under 12 GiB, or while / is more than 80 %
# full, and thaws them when all three hold; it prints one line a round.  A test that cannot read its input counts as
# failed.  The scopes are those that systemd lists and those whose cgroups are in the user manager's app.slice; when
# systemd cannot list them, the round uses the last listing as well and says so, and the guard goes on.  After each
# freeze it reads the scope's FreezerState; a scope that is not frozen within 5 s is stopped, and when processes are
# still in its cgroup, they are killed through cgroup.kill.  --once: one round, then exit.  --check: the three tests
# only, no action; exit 0 when all hold (bench/rehearsal_rr.sh runs it before each job).
# RR_GUARD_FORCE=freeze|thaw: that action, for a test; RR_GUARD_ONLY=UNIT: act on that scope only
# (bench/rehearsal_selftest.sh).  Both are refused (exit 2) without --once.
# What it writes: nothing (its output goes where the caller sends it).  It reads systemd, /proc/meminfo and df, and acts
# only on rr-* scopes of this user.  Run it on the host in the scope rrg-guard.scope, which bench/rehearsal_rr.sh looks
# for (bench/rehearsal_rr.sh's header gives the command); it refuses to run in an rr-* scope, which it would freeze.
# Limits: systemd 249's freeze needs the cgroup v2 freezer; MemAvailable does not count the ZFS ARC.
# It is not the copy that the records ran (~/rebuild-rehearsal/bin/rr-guard, sha256 ff8bc7fb... in
# results/rebuild-rehearsal-r0-20261030.txt).  That copy had no --check, RR_GUARD_ONLY or check of its own scope, kept
# going with "scopes: none" when systemctl could not list the scopes, did not check that a freeze worked, and missed
# scopes whose names hold characters other than letters, digits, _ . and -.
set -u
export PATH=/usr/bin:/bin
mode=${1:-}
case $mode in ''|--once|--check) ;; *) echo "usage: bench/rehearsal_guard.sh [--once|--check]" >&2; exit 2 ;; esac
if [ "$mode" != --once ] && [ -n "${RR_GUARD_FORCE:-}${RR_GUARD_ONLY:-}" ]; then
  echo "rehearsal_guard: RR_GUARD_FORCE and RR_GUARD_ONLY are for --once only: not starting" >&2; exit 2
fi
case $(sed -n 's/^0:://p' /proc/self/cgroup) in
  */rr-*.scope|'') echo "rehearsal_guard: it runs in an rr-* scope (or its cgroup cannot be read): not starting" >&2; exit 2 ;;
esac
APP=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/app.slice
sc() { timeout 30 systemctl --user "$@"; }
procs() { cat "$APP/$1/cgroup.procs" 2>/dev/null; }
freeze_scope() {   # freeze one scope and check it; stop it when it is not frozen, and kill what is left in its cgroup
  local u=$1 st='' i
  sc freeze "$u" 2>&1 | sed 's/^/  /'
  for i in 1 2 3 4 5; do
    st=$(sc show -p FreezerState --value "$u" 2>/dev/null); [ "$st" = frozen ] && return 0; sleep 1
  done
  [ -d "$APP/$u" ] || [ -n "$(sc show -p ActiveState --value "$u" 2>/dev/null | grep -vx 'inactive\|failed')" ] || return 0
  echo "  $u is ${st:-unknown}, not frozen: stopping it"
  sc stop "$u" 2>&1 | sed 's/^/  /'
  if [ -n "$(procs "$u")" ]; then echo "  $u still has processes: killing them"; echo 1 > "$APP/$u/cgroup.kill"; fi
}
last=()
while :; do
  why=()
  systemctl is-active --quiet strata-q3.service || why+=("strata-q3 is $(systemctl is-active strata-q3.service)")
  avail=$(awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo)
  [ -n "$avail" ] && [ "$avail" -ge 12 ] || why+=("MemAvailable ${avail:-?} GiB < 12")
  pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
  [ -n "$pct" ] && [ "$pct" -le 80 ] || why+=("/ is ${pct:-?} % full")
  if [ "$mode" = --check ]; then
    [ ${#why[@]} -eq 0 ] && exit 0
    echo "rehearsal_guard: ${why[*]}"; exit 1
  fi
  act=thaw; [ ${#why[@]} -gt 0 ] && act=freeze
  case ${RR_GUARD_FORCE:-} in freeze|thaw) act=$RR_GUARD_FORCE; why+=("forced by RR_GUARD_FORCE") ;; esac
  note=''
  if list=$(sc list-units --type=scope --plain --no-legend 'rr-*'); then
    mapfile -t units < <(awk 'NF {print $1}' <<< "$list")
  else
    units=("${last[@]}"); note=" (systemd cannot list the rr-* scopes: the last listing and app.slice are used)"
  fi
  for c in "$APP"/rr-*.scope; do [ -d "$c" ] && units+=("${c##*/}"); done
  mapfile -t units < <(printf '%s\n' "${units[@]}" | awk 'NF && !seen[$0]++')
  last=("${units[@]}")
  [ -n "${RR_GUARD_ONLY:-}" ] && units=("$RR_GUARD_ONLY")
  echo "rr-guard $(date '+%F %T'): service $(systemctl is-active strata-q3.service), MemAvailable ${avail:-?} GiB, / ${pct:-?} %: $act${why:+ (${why[*]})}; scopes: ${units[*]:-none}$note"
  for u in "${units[@]}"; do
    case $u in
      rr-*.scope) if [ "$act" = freeze ]; then freeze_scope "$u"; else sc thaw "$u" 2>&1 | sed 's/^/  /'; fi ;;
    esac
  done
  [ "$mode" = --once ] && exit 0
  sleep 30
done
