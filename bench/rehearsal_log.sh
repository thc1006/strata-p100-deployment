#!/bin/bash
# bench/rehearsal_log.sh COMMAND...: the rebuild rehearsal's step logger (the rehearsal's ~/rebuild-rehearsal/bin/rlog,
# whose default log was logs/R1-R5.raw).
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# It runs COMMAND, and appends "$ COMMAND" (each word as printf %q gives it), its output and "[exit N]" to $RLOG
# (default ~/rebuild-rehearsal/logs/rehearsal.raw), and shows them.  -s TITLE appends a section title with the time;
# -n TEXT a note line.  The records in results/rebuild-rehearsal-*-20261030.txt are such logs, with the host name
# replaced by <host>.
# What it writes: $RLOG only; COMMAND writes what it writes (the rehearsal ran its jobs through bench/rehearsal_rr.sh).
set -u
L=${RLOG:-/home/thc1006/rebuild-rehearsal/logs/rehearsal.raw}
case ${1:-} in
  -s) printf '\n== %s (%s)\n' "$2" "$(date '+%F %T')" | tee -a "$L"; exit 0 ;;
  -n) printf '# %s\n' "$2" | tee -a "$L"; exit 0 ;;
esac
{ printf '\n$'; printf ' %q' "$@"; printf '\n'; } | tee -a "$L"
"$@" 2>&1 | tee -a "$L"; rc=${PIPESTATUS[0]}
printf '[exit %s]\n' "$rc" | tee -a "$L"
exit "$rc"
