#!/bin/bash
# bench/rehearsal_log.sh COMMAND...: the rebuild rehearsal's step logger.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# It runs COMMAND, and appends "$ COMMAND" (each word as printf %q gives it), its output and "[exit N]" to $RLOG
# (default ~/rebuild-rehearsal/logs/rehearsal.raw), and shows them.  -s TITLE appends a section title with the time;
# -n TEXT a note line.  The records in results/rebuild-rehearsal-*-20261030.txt are such logs, with the host name
# replaced by <host>.
# It refuses (exit 2), before COMMAND runs, a log that is a symlink, is not an absolute path without symlinks, lies
# under ~/rebuild-rehearsal/root (which jobs can write), or cannot be written.
# What it writes: $RLOG only; COMMAND writes what it writes (the rehearsal ran its jobs through bench/rehearsal_rr.sh).
# It is not the copy that the records ran (~/rebuild-rehearsal/bin/rlog): that copy's default log was logs/R1-R5.raw,
# it ran COMMAND when the log could not be written, and it did not check where the log was.
set -u
L=${RLOG:-/home/thc1006/rebuild-rehearsal/logs/rehearsal.raw}
die() { echo "rehearsal_log: $*" >&2; exit 2; }
[ $# -gt 0 ] || die "usage: bench/rehearsal_log.sh COMMAND... | -s TITLE | -n TEXT"
[ ! -L "$L" ] && [ "$(/usr/bin/realpath -m -- "$L")" = "$L" ] || die "$L is a symlink or not an absolute path without symlinks"
case $L in /home/thc1006/rebuild-rehearsal/root|/home/thc1006/rebuild-rehearsal/root/*) die "$L is where jobs write" ;; esac
[ -w "$L" ] || { [ ! -e "$L" ] && [ -w "$(/usr/bin/dirname -- "$L")" ]; } || die "cannot write $L"
case ${1:-} in
  -s) printf '\n== %s (%s)\n' "${2:-}" "$(/usr/bin/date '+%F %T')" | /usr/bin/tee -a -- "$L"; exit 0 ;;
  -n) printf '# %s\n' "${2:-}" | /usr/bin/tee -a -- "$L"; exit 0 ;;
esac
{ printf '\n$'; printf ' %q' "$@"; printf '\n'; } | /usr/bin/tee -a -- "$L"
"$@" 2>&1 | /usr/bin/tee -a -- "$L"; rc=${PIPESTATUS[0]}
printf '[exit %s]\n' "$rc" | /usr/bin/tee -a -- "$L"
exit "$rc"
