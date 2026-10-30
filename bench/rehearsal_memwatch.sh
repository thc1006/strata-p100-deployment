#!/bin/bash
# bench/rehearsal_memwatch.sh UNIT LOG [MAX_S]: the rebuild rehearsal's memory sampler.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# UNIT is a job's scope, rr-<name>.scope (bench/rehearsal_rr.sh).  It waits up to 120 s for that user scope to appear,
# then appends to LOG every 5 s while the scope exists, for at most MAX_S seconds (21600): the time, memory.current,
# memory.peak, oom and oom_kill (memory.events), anon and file (memory.stat, added before R9); a last line says when
# the scope ended or the time ran out.  memory.peak includes page cache, which the scope's MemoryMax reclaims, so anon
# is what a job needs (results/rebuild-rehearsal-memory-20261030.txt).
# It refuses (exit 2) another UNIT, and a LOG that is a symlink, is not an absolute path without symlinks, or lies under
# ~/rebuild-rehearsal/root (which jobs can write).
# What it writes: LOG only (the rehearsal gave ~/rebuild-rehearsal/logs/<unit>.mem).  It reads the cgroup files.
# It is not the copy that the records ran (~/rebuild-rehearsal/bin/rr-memwatch): that copy checked neither UNIT nor
# LOG and had no time limit, so a wrong UNIT kept it running.
set -u
export PATH=/usr/bin:/bin
U=${1:-}; LOG=${2:-}; MAX=${3:-21600}
die() { echo "rehearsal_memwatch: $*" >&2; exit 2; }
[[ $U =~ ^rr-[A-Za-z0-9_.-]+\.scope$ ]] || die "UNIT must be rr-<name>.scope"
[[ $MAX =~ ^[1-9][0-9]*$ ]] || die "MAX_S must be a number of seconds"
[ -n "$LOG" ] && [ ! -L "$LOG" ] && [ "$(realpath -m -- "$LOG")" = "$LOG" ] || die "LOG must be an absolute path without symlinks"
case $LOG in /home/thc1006/rebuild-rehearsal/root|/home/thc1006/rebuild-rehearsal/root/*) die "$LOG is where jobs write" ;; esac
C=/sys/fs/cgroup/user.slice/user-1003.slice/user@1003.service/app.slice/$U
for _ in $(seq 120); do [ -d "$C" ] && break; sleep 1; done
end=$(( $(date +%s) + MAX ))
while [ -d "$C" ] && [ "$(date +%s)" -lt "$end" ]; do
  printf '%s current %s peak %s %s\n' "$(date +%T)" "$(cat "$C/memory.current" 2>/dev/null)" "$(cat "$C/memory.peak" 2>/dev/null)" \
    "$(grep -E '^(oom|oom_kill) ' "$C/memory.events" 2>/dev/null | tr '\n' ' ')$(grep -E '^(anon|file) ' "$C/memory.stat" 2>/dev/null | tr '\n' ' ')" >> "$LOG"
  sleep 5
done
if [ -d "$C" ]; then echo "$(date +%T) $U still runs: stopped after $MAX s" >> "$LOG"; else echo "$(date +%T) $U ended" >> "$LOG"; fi
