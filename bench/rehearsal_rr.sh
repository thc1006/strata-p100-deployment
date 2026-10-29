#!/bin/bash
# bench/rehearsal_rr.sh [-m MEM] [-c CPUS] [-n NAME] [--sysbus] [--home] -- COMMAND...
# Runs one job of the rebuild rehearsal in the sandbox ~/rebuild-rehearsal (the rehearsal's ~/rebuild-rehearsal/bin/rr).
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).
# The job gets a user scope of its own: MemoryMax=MEM (2G), MemorySwapMax=0, oom_score_adj +500, nice 19, SCHED_IDLE,
# ionice idle, the CPUs CPUS (7), the unit name NAME (rr-<pid>; it must start with rr-, the scopes that
# bench/rehearsal_guard.sh freezes and thaws).  It runs under bubblewrap, in which
#   - the whole root file system is read-only;
#   - /home/thc1006/dev, /home/thc1006/cuda-12.9, /home/thc1006/downloads and /tmp are directories of the sandbox
#     (~/rebuild-rehearsal/root/...), so the paths that setup.py, the CUDA runfile and the repository's scripts hard-code
#     lead into the sandbox, and what they write carries production's paths;
#   - production's ~/dev and ~/cuda-12.9 are visible only read-only, at ~/rebuild-rehearsal/prod/{dev,cuda-12.9};
#   - /dev is a minimal one without /dev/nvidia*: no GPU can be reached;
#   - a PID namespace and /proc of its own (no /proc/<pid>/root of a host process), an empty /run/user/1003 (no user
#     bus: no systemctl --user or systemd-run from inside), and no system bus unless --sysbus is given;
#   - XDG_CONFIG_HOME and XDG_CACHE_HOME in the sandbox, and PATH first through ~/rebuild-rehearsal/shim (sudo that
#     refuses, nvidia-smi that prints the recorded GPUs: bench/rehearsal_shim_*.sh).
# --home: the sandbox's root/home/thc1006 is all of /home/thc1006 (production's home is then not visible but through
# the two read-only views and ~/rebuild-rehearsal), for the CUDA runfile, which refuses a /home/thc1006 it cannot write.
# The command runs only when the sandbox marker is there and a first bwrap with the same arguments finds /etc and the
# views not writable and no /dev/nvidiactl or /dev/nvidia0 (test, without a shell); else exit 98 (99: no marker
# outside).  In the rehearsal of 2026-10-30 a shell inside the job's own namespace made these checks.
# What it writes: nothing itself.  The job can write only under ~/rebuild-rehearsal, the rest is read-only for it;
# systemd keeps the scope as a transient unit of the user manager.
# Limits: the network is the host's (the downloads need it), so a job could reach the production server on
# 127.0.0.1:8080; nothing that the rehearsal runs does.  Inside, root's files show as uid 65534, and checks that look
# at owners report them (R11 in results/rebuild-rehearsal-r8-r11-20261030.txt).  The sandbox's place is fixed.
# The sandbox, made by hand before R0 (results/rebuild-rehearsal-r0-20261030.txt shows the result):
#   SB=~/rebuild-rehearsal; R=$SB/root/home/thc1006
#   mkdir -p $SB/{logs,verify,shim,prod/dev,prod/cuda-12.9,root/tmp,root/empty-run-user,root/empty-dbus} \
#            $R/{dev,cuda-12.9,downloads/cuda/tmp,.config,.cache,rebuild-rehearsal}
#   touch $R/dev/.rehearsal-sandbox
#   install -m 755 bench/rehearsal_shim_sudo.sh $SB/shim/sudo
#   install -m 755 bench/rehearsal_shim_nvidia_smi.sh $SB/shim/nvidia-smi
set -u
SB=/home/thc1006/rebuild-rehearsal; R=$SB/root/home/thc1006
MEM=2G; CPUS=7; NAME=rr-$$; SYSBUS=0; HOMEBIND=0
while [ $# -gt 0 ]; do
  case $1 in
    -m) MEM=$2; shift 2 ;; -c) CPUS=$2; shift 2 ;; -n) NAME=$2; shift 2 ;; --sysbus) SYSBUS=1; shift ;; --home) HOMEBIND=1; shift ;;
    --) shift; break ;; *) break ;;
  esac
done
[ $# -gt 0 ] || { echo "usage: bench/rehearsal_rr.sh [-m MEM] [-c CPUS] [-n NAME] [--sysbus] [--home] -- COMMAND..." >&2; exit 2; }
[ -e "$R/dev/.rehearsal-sandbox" ] || { echo "rehearsal_rr: no sandbox marker in $R/dev" >&2; exit 99; }
case $NAME in rr-*) ;; *) echo "rehearsal_rr: the scope name must start with rr-" >&2; exit 2 ;; esac
bus=(--bind "$SB/root/empty-dbus" /run/dbus)
[ $SYSBUS = 1 ] && bus=()
binds=(--bind "$SB" "$SB" --ro-bind /home/thc1006/dev "$SB/prod/dev" --ro-bind /home/thc1006/cuda-12.9 "$SB/prod/cuda-12.9"
       --bind "$R/dev" /home/thc1006/dev --bind "$R/cuda-12.9" /home/thc1006/cuda-12.9 --bind "$R/downloads" /home/thc1006/downloads)
[ $HOMEBIND = 1 ] && binds=(--bind "$R" /home/thc1006 --bind "$SB" "$SB" --ro-bind /home/thc1006/dev "$SB/prod/dev"
       --ro-bind /home/thc1006/cuda-12.9 "$SB/prod/cuda-12.9")
# the namespace, as an array: a first bwrap with it checks the namespace (test, no shell), the second runs the job
ns=(--die-with-parent --new-session --unshare-pid
    --ro-bind / / --dev /dev --proc /proc
    "${binds[@]}" --bind "$SB/root/tmp" /tmp
    --bind "$SB/root/empty-run-user" /run/user/1003 "${bus[@]}"
    --unsetenv DBUS_SESSION_BUS_ADDRESS --setenv XDG_RUNTIME_DIR /run/user/1003
    --setenv XDG_CONFIG_HOME "$R/.config" --setenv XDG_CACHE_HOME "$R/.cache" --setenv TMPDIR /tmp
    --setenv PATH "$SB/shim:/usr/local/bin:/usr/bin:/bin" --setenv PYTHONNOUSERSITE 1)
bwrap "${ns[@]}" test -e /home/thc1006/dev/.rehearsal-sandbox -a ! -w /etc -a ! -w "$SB/prod/dev" \
  -a ! -w "$SB/prod/cuda-12.9" -a ! -e /dev/nvidiactl -a ! -e /dev/nvidia0 ||
  { echo "rehearsal_rr: namespace check failed: nothing run" >&2; exit 98; }
exec systemd-run --user --scope -q --unit="$NAME" --description="rebuild rehearsal job $NAME" -p MemoryMax="$MEM" -p MemorySwapMax=0 -- \
  choom -n 500 -- nice -n 19 chrt -i 0 ionice -c3 taskset -c "$CPUS" bwrap "${ns[@]}" "$@"
