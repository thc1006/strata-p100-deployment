#!/bin/bash
# bench/rehearsal_memwatch.sh UNIT LOG: the rebuild rehearsal's memory sampler (the rehearsal's
# ~/rebuild-rehearsal/bin/rr-memwatch).
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# It waits up to 120 s for the user scope UNIT (rr-<name>.scope) to appear, then appends to LOG every 5 s while the
# scope exists: the time, memory.current, memory.peak, oom and oom_kill (memory.events), anon and file (memory.stat,
# added before R9);
# a last line says when the scope ended.  memory.peak includes page cache, which the scope's MemoryMax reclaims, so
# anon is what a job needs (results/rebuild-rehearsal-memory-20261030.txt).
# What it writes: LOG only (the rehearsal gave ~/rebuild-rehearsal/logs/<unit>.mem).  It reads the cgroup files.
set -u
U=$1; LOG=$2; C=/sys/fs/cgroup/user.slice/user-1003.slice/user@1003.service/app.slice/$U
for i in $(seq 120); do [ -d "$C" ] && break; sleep 1; done
while [ -d "$C" ]; do
  printf '%s current %s peak %s %s\n' "$(date +%T)" "$(cat $C/memory.current 2>/dev/null)" "$(cat $C/memory.peak 2>/dev/null)" \
    "$(grep -E '^(oom|oom_kill) ' $C/memory.events 2>/dev/null | tr '\n' ' ')$(grep -E '^(anon|file) ' $C/memory.stat 2>/dev/null | tr '\n' ' ')" >> "$LOG"
  sleep 5
done
echo "$(date +%T) $U ended" >> "$LOG"
