#!/bin/sh
# bench/rehearsal_shim_sudo.sh: the rebuild rehearsal's stand-in for sudo, installed as ~/rebuild-rehearsal/shim/sudo,
# which bench/rehearsal_rr.sh puts first on the jobs' PATH: setup.sh and setup.py call sudo for missing packages, and
# nothing in the rehearsal may run it.  It prints what it refused and exits 1.  It writes nothing.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
echo "rehearsal: sudo refused: $*" >&2
exit 1
