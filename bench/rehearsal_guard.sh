#!/bin/bash
# bench/rehearsal_guard.sh [--once]: the rebuild rehearsal's guard (the rehearsal's ~/rebuild-rehearsal/bin/rr-guard).
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# Every 30 s it freezes the user scopes rr-*.scope (the jobs of bench/rehearsal_rr.sh) while strata-q3 is not active
# (a restart's load reads its model files from disk), while MemAvailable is under 12 GiB, or while / is more than 80 %
# full, and thaws them when all three hold; it prints one line a round.  --once: one round, then exit.
# RR_GUARD_FORCE=freeze|thaw: that action, for a test (bench/rehearsal_selftest.sh).
# What it writes: nothing (its output goes where the caller sends it).  It reads systemd, /proc/meminfo and df, and acts
# only on rr-* scopes of this user.  Run it in a scope whose name does not start with rr- (the rehearsal used
# rrg-guard), or it freezes itself.  Limits: systemd 249's freeze needs the cgroup v2 freezer; MemAvailable does not
# count the ZFS ARC.
set -u
once=0; [ "${1:-}" = --once ] && once=1
while :; do
  why=()
  systemctl is-active --quiet strata-q3.service || why+=("strata-q3 is $(systemctl is-active strata-q3.service)")
  avail=$(awk '/^MemAvailable:/ {print int($2/1048576)}' /proc/meminfo)
  [ "${avail:-0}" -ge 12 ] || why+=("MemAvailable ${avail:-?} GiB < 12")
  pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
  [ "${pct:-100}" -le 80 ] || why+=("/ is ${pct:-?} % full")
  act=thaw; [ ${#why[@]} -gt 0 ] && act=freeze
  case ${RR_GUARD_FORCE:-} in freeze|thaw) act=$RR_GUARD_FORCE; why+=("forced by RR_GUARD_FORCE") ;; esac
  units=$(systemctl --user list-units --type=scope --plain --no-legend 'rr-*' | grep -o '^rr-[A-Za-z0-9_.-]*\.scope' | tr '\n' ' ')
  echo "rr-guard $(date '+%F %T'): service $(systemctl is-active strata-q3.service), MemAvailable ${avail} GiB, / ${pct} %: $act${why:+ (${why[*]})}; scopes: ${units:-none}"
  for u in $units; do
    case $u in rr-*.scope) systemctl --user "$act" "$u" 2>&1 | sed 's/^/  /' ;; esac
  done
  [ $once = 1 ] && exit 0
  sleep 30
done
