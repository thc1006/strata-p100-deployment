#!/bin/bash
# bench/rehearsal_guard.sh [--once|--check]: the rebuild rehearsal's guard.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# Every 30 s it freezes the user scopes rr-*.scope (the jobs of bench/rehearsal_rr.sh) while strata-q3 is not active
# (a restart's load reads its model files from disk), while MemAvailable is under 12 GiB, or while / is more than 80 %
# full, and thaws them when all three hold; it prints one line a round.  A test that cannot read its input counts as
# failed.  When it cannot list the scopes it prints why and exits 1, so that rrg-guard.scope ends and
# bench/rehearsal_rr.sh starts no job.  --once: one round, then exit.  --check: the three tests only, no action; exit 0
# when all hold (bench/rehearsal_rr.sh runs it before each job).
# RR_GUARD_FORCE=freeze|thaw: that action, for a test; RR_GUARD_ONLY=UNIT: act on that scope only
# (bench/rehearsal_selftest.sh).
# What it writes: nothing (its output goes where the caller sends it).  It reads systemd, /proc/meminfo and df, and acts
# only on rr-* scopes of this user.  Run it on the host in the scope rrg-guard.scope, which bench/rehearsal_rr.sh looks
# for (bench/rehearsal_rr.sh's header gives the command); it refuses to run in an rr-* scope, which it would freeze.
# Limits: systemd 249's freeze needs the cgroup v2 freezer; MemAvailable does not count the ZFS ARC.
# It is not the copy that the records ran (~/rebuild-rehearsal/bin/rr-guard, sha256 ff8bc7fb... in
# results/rebuild-rehearsal-r0-20261030.txt).  That copy had no --check, RR_GUARD_ONLY or check of its own scope, kept
# going with "scopes: none" when systemctl could not list the scopes, and missed scopes whose names hold characters
# other than letters, digits, _ . and -.
set -u
export PATH=/usr/bin:/bin
mode=${1:-}
case $mode in ''|--once|--check) ;; *) echo "usage: bench/rehearsal_guard.sh [--once|--check]" >&2; exit 2 ;; esac
case $(sed -n 's/^0:://p' /proc/self/cgroup) in
  */rr-*.scope|'') echo "rehearsal_guard: it runs in an rr-* scope (or its cgroup cannot be read): not starting" >&2; exit 2 ;;
esac
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
  if ! list=$(systemctl --user list-units --type=scope --plain --no-legend 'rr-*'); then
    echo "rr-guard $(date '+%F %T'): cannot list the rr-* scopes: exiting"; exit 1
  fi
  mapfile -t units < <(awk '{print $1}' <<< "$list")
  [ -n "${RR_GUARD_ONLY:-}" ] && units=("$RR_GUARD_ONLY")
  echo "rr-guard $(date '+%F %T'): service $(systemctl is-active strata-q3.service), MemAvailable ${avail:-?} GiB, / ${pct:-?} %: $act${why:+ (${why[*]})}; scopes: ${units[*]:-none}"
  for u in "${units[@]}"; do
    case $u in rr-*.scope) systemctl --user "$act" "$u" 2>&1 | sed 's/^/  /' ;; esac
  done
  [ "$mode" = --once ] && exit 0
  sleep 30
done
