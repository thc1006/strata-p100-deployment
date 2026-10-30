#!/bin/bash
# bench/rehearsal_rr.sh [-m MEM] [-c CPUS] [-n NAME] [--net] [--sysbus] [--home] -- COMMAND...
# Runs one job of the rebuild rehearsal in the sandbox ~/rebuild-rehearsal.
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).  Run it on the host from a checkout of this
# repository; it refuses inside the sandbox and from a copy under ~/rebuild-rehearsal.
# The job gets a user scope of its own: MemoryMax=MEM (2G; a number and K, M or G), MemorySwapMax=0, oom_score_adj
# +500, nice 19, SCHED_IDLE, ionice idle, the CPUs CPUS (7), the unit name NAME (rr-<pid>; rr- and then letters,
# digits, _ . or -, the scopes that bench/rehearsal_guard.sh freezes and thaws).  It runs under bubblewrap, in which
#   - the root file system is read-only, and /home/thc1006 is an empty read-only tmpfs, so the account's files and
#     credentials (~/.ssh, ~/.config/gh, ~/.claude ...) are not visible in either mode; ~/.config/strata, read-only at
#     its own path (not with --home), is the one directory of the real home shown (its settings.json names data_dir
#     and installs);
#   - a job can write only ~/rebuild-rehearsal/root/tmp and ~/rebuild-rehearsal/root/home/thc1006/{dev,cuda-12.9,
#     downloads,.config,.cache}, each at its own path, the first three also at /home/thc1006/{dev,cuda-12.9,
#     downloads} and root/tmp at /tmp, so the paths that setup.py, the CUDA runfile and the repository's scripts
#     hard-code lead into the sandbox; the rest of ~/rebuild-rehearsal (shim, logs, verify, prod and the directories
#     above these) is read-only, so a job cannot rename what is bound or change what the host runs or writes there;
#   - production's ~/dev/{strata,Strata-data,p100-q3-deployment} and ~/cuda-12.9 are visible only read-only, at
#     ~/rebuild-rehearsal/prod/{dev/...,cuda-12.9};
#   - /dev is a minimal one without /dev/nvidia*: no GPU device can be opened;
#   - PID and IPC namespaces and /proc of its own, and an empty read-only /run with an empty /run/user/1003: no user
#     bus (no systemctl --user or systemd-run from inside), no system bus and no socket of the host's daemons, such as
#     NetBird's, Tailscale's, nvidia-persistenced's or the journal's (--sysbus adds the host's /run/dbus and an empty
#     /run/systemd/system, for systemctl is-active; --net adds /run/systemd/resolve, where /etc/resolv.conf leads);
#   - a network namespace with only a loopback of its own (--net: the host's, for downloads; it also reaches every
#     listener of the host on the loopback, such as the production server on 127.0.0.1:8080, the engine's abstract
#     unix socket @cuda-uvmfd-... and the VS Code server, and the tailnet and NetBird networks, which no job of the
#     rehearsal uses);
#   - of the caller's open files only standard input, output and error: every descriptor above 2 is closed just
#     before bwrap;
#   - the working directory /, and an environment of HOME, USER, LOGNAME, LANG, TERM, XDG_RUNTIME_DIR, XDG_CONFIG_HOME
#     and XDG_CACHE_HOME (in the sandbox), TMPDIR=/tmp, PYTHONNOUSERSITE=1, PATH first through ~/rebuild-rehearsal/shim
#     (sudo that refuses, nvidia-smi that prints the recorded GPUs: bench/rehearsal_shim_*.sh) and PWD=/ (bwrap sets
#     it), and nothing else of the caller's.
# --home: /home/thc1006 is the sandbox's root/home/thc1006, writable, with ~/rebuild-rehearsal read-only on top and
# the writable directories above (for the CUDA runfile, which refuses a /home/thc1006 it cannot write).
# Before the job it refuses, with a line "rehearsal_rr: refused (N): ..." on standard error: (2) a wrong option; (97)
# unless it gets the lock on ~/rebuild-rehearsal, no rr-* scope exists, rrg-guard.scope holds a bash running
# bench/rehearsal_guard.sh and bench/rehearsal_guard.sh --check passes; (98) unless every directory it binds is a
# directory at its own path (no symlink on the way), and first bwraps with the same arguments, started the same way as
# the job's and running /usr/bin/stat, test, find, ls and env by absolute path with no shell, find each writable path
# inside on the device and inode that the host saw, the sandbox marker there, /etc, /usr/local/sbin, the views and the
# read-only parts of ~/rebuild-rehearsal not writable, no /dev/nvidia*, nothing in /run but what the options add, only
# the file descriptors 0-2 (and ls's own 3), and only the variables above; (99) unless the sandbox marker is there
# outside.  Then it waits for the job, holding the lock, and exits with the job's status.  The job does not get the
# lock's descriptor, and its bwrap ends when the runner ends (--die-with-parent).
# What it writes: nothing itself; systemd keeps the scope as a transient unit of the user manager.  A job writes its
# logs under /tmp (~/rebuild-rehearsal/root/tmp), not into ~/rebuild-rehearsal/logs.
# Run every step on the sandbox's paths as a job, not on the host: a job can leave a link such as
# root/home/thc1006/dev/strata -> /home/thc1006/dev, which leads into the sandbox inside and into production outside, so
# an rm, cp, mv or ln on the host would follow it.
# Limits: inside, root's files show as uid 65534, and checks that look at owners report them (R11 in
# results/rebuild-rehearsal-r8-r11-20261030.txt).  The sandbox's place is fixed.
# It is not the copy that the records ran, ~/rebuild-rehearsal/bin/rr (results/rebuild-rehearsal-r0-20261030.txt gives
# its sha256 at R0, 2009c269...; --home was added to it after R0).  That copy bound all of ~/rebuild-rehearsal
# writable (so a job could have swapped a bound directory for a symlink, or rewritten bin/ and shim/), showed all of
# the real home read-only, credentials too (with --home only ~/dev and ~/cuda-12.9 of it, through the views), gave
# every job the host's network, the host's /run but for its user bus and system bus, the caller's environment and the
# caller's open files, took no lock, did not check the guard, the service,
# memory or disk, accepted any option and any name starting with rr-, and checked the namespace with a shell inside
# the job's own namespace, whose ls came through PATH.  This version has not run in a rehearsal; it was tried on
# scratch directories only (the commits that made it say how).
# The sandbox, made by hand before R0:
#   SB=~/rebuild-rehearsal; R=$SB/root/home/thc1006
#   mkdir -p $SB/{logs,verify,shim} $SB/prod/dev/{strata,Strata-data,p100-q3-deployment} $SB/prod/cuda-12.9 $SB/root/tmp \
#            $R/{dev,cuda-12.9,downloads/cuda/tmp,.config,.cache,rebuild-rehearsal}
#   touch $R/dev/.rehearsal-sandbox
#   install -m 755 bench/rehearsal_shim_sudo.sh $SB/shim/sudo
#   install -m 755 bench/rehearsal_shim_nvidia_smi.sh $SB/shim/nvidia-smi
#   systemd-run --user --scope -q --unit=rrg-guard -p MemoryMax=64M -- bench/rehearsal_guard.sh >> $SB/logs/guard.log 2>&1 &
set -u
export PATH=/usr/bin:/bin
SB=/home/thc1006/rebuild-rehearsal; R=$SB/root/home/thc1006
PROD_DEV=/home/thc1006/dev; PROD_CUDA=/home/thc1006/cuda-12.9; PROD_CFG=/home/thc1006/.config/strata
MEM=2G; CPUS=7; NAME=rr-$$; NET=0; SYSBUS=0; HOMEBIND=0
die() { echo "rehearsal_rr: refused ($1): $2" >&2; exit "$1"; }
usage="usage: bench/rehearsal_rr.sh [-m MEM] [-c CPUS] [-n NAME] [--net] [--sysbus] [--home] -- COMMAND..."
while [ $# -gt 0 ]; do
  case $1 in
    -m|-c|-n) [ $# -ge 2 ] || die 2 "$1 needs a value"
       case $1 in -m) MEM=$2 ;; -c) CPUS=$2 ;; -n) NAME=$2 ;; esac; shift 2 ;;
    --net) NET=1; shift ;; --sysbus) SYSBUS=1; shift ;; --home) HOMEBIND=1; shift ;;
    --) shift; break ;;
    *) die 2 "unknown option $1; $usage" ;;
  esac
done
[ $# -gt 0 ] || die 2 "$usage"
[[ $MEM =~ ^[1-9][0-9]*[KMG]$ ]] || die 2 "-m $MEM: give a number and K, M or G"
[[ $CPUS =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || die 2 "-c $CPUS: give a CPU list such as 7 or 1-7"
[[ $NAME =~ ^rr-[A-Za-z0-9_.-]+$ ]] || die 2 "-n $NAME: rr- and then letters, digits, _ . or -"
[ ! -e "$PROD_DEV/.rehearsal-sandbox" ] || die 2 "this is the sandbox: run it on the host"
D=$(cd "$(dirname -- "$0")/.." && pwd -P) || die 2 "cannot find the repository"
case $D/ in "$SB"/*) die 2 "run it from a checkout of the repository, not from a copy under $SB" ;; esac
[ -e "$R/dev/.rehearsal-sandbox" ] || die 99 "no sandbox marker in $R/dev"
# One job at a time: this script holds the lock on fd 9 until the job ends; no bwrap gets fd 9 (see box).
exec 9<"$SB" || die 97 "cannot open $SB"
flock -n 9 || die 97 "another job of the rehearsal holds the lock on $SB"
units=$(systemctl --user list-units --type=scope --plain --no-legend 'rr-*') || die 97 "cannot list the rr-* scopes"
[ -z "$units" ] || die 97 "an rr-* scope exists: ${units%% *}"
# The guard: rrg-guard.scope must hold a bash that runs bench/rehearsal_guard.sh with no argument (its loop).
gcg=$(systemctl --user show -p ControlGroup --value rrg-guard.scope) && [ -n "$gcg" ] &&
  systemctl --user is-active --quiet rrg-guard.scope || die 97 "the guard does not run as rrg-guard.scope"
guard=0
while read -r pid; do
  mapfile -d '' -t a 2>/dev/null < "/proc/$pid/cmdline" || continue
  [ ${#a[@]} -eq 2 ] && [[ ${a[0]} =~ (^|/)bash$ ]] && [[ ${a[1]} =~ (^|/)bench/rehearsal_guard\.sh$ ]] && guard=1
done < "/sys/fs/cgroup$gcg/cgroup.procs"
[ $guard = 1 ] || die 97 "rrg-guard.scope runs no bench/rehearsal_guard.sh"
"$D/bench/rehearsal_guard.sh" --check >&2 9<&- || die 97 "the guard's check fails: nothing run"
# The directories bound: each must be a directory at its own path, and its device and inode are kept for the check.
W=("$R/dev" "$R/cuda-12.9" "$R/downloads" "$R/.config" "$R/.cache" "$SB/root/tmp")
RO=("$SB" "$SB/shim" "$SB/logs" "$SB/verify" "$SB/prod/dev/strata" "$SB/prod/dev/Strata-data"
    "$SB/prod/dev/p100-q3-deployment" "$SB/prod/cuda-12.9")
for d in "$SB" "$SB/root" "$SB/root/home" "$R" "${W[@]}" "${RO[@]}"; do
  [ -d "$d" ] && [ ! -L "$d" ] && [ "$(realpath -e -- "$d")" = "$d" ] || die 98 "$d is not a directory at its own path"
done
expect=$(stat -c %d:%i -- "$R/dev" "$R/cuda-12.9" "$R/downloads" "$SB/root/tmp" "${W[@]}") || die 98 "cannot stat the sandbox"
ns=(--die-with-parent --new-session --unshare-pid --unshare-ipc)
[ $NET = 1 ] || ns+=(--unshare-net)
ns+=(--ro-bind / / --dev /dev --proc /proc --bind "$SB/root/tmp" /tmp)
if [ $HOMEBIND = 1 ]; then
  ns+=(--bind "$R" /home/thc1006 --ro-bind "$SB" "$SB")
else
  ns+=(--tmpfs /home/thc1006 --ro-bind "$SB" "$SB" --ro-bind "$PROD_CFG" "$PROD_CFG")
fi
for d in "${W[@]}"; do ns+=(--bind "$d" "$d"); done
ns+=(--bind "$R/dev" /home/thc1006/dev --bind "$R/cuda-12.9" /home/thc1006/cuda-12.9
     --bind "$R/downloads" /home/thc1006/downloads)
for p in strata Strata-data p100-q3-deployment; do ns+=(--ro-bind "$PROD_DEV/$p" "$SB/prod/dev/$p"); done
ns+=(--ro-bind "$PROD_CUDA" "$SB/prod/cuda-12.9")
[ $HOMEBIND = 1 ] || ns+=(--remount-ro /home/thc1006)
# /run: an empty tmpfs, read-only, with what the options need.  keep: allowed names; prune: allowed, not looked into.
ns+=(--tmpfs /run --dir /run/user/1003); keep=(/run/user /run/user/1003); prune=()
[ $NET = 1 ] && { ns+=(--ro-bind /run/systemd/resolve /run/systemd/resolve); keep+=(/run/systemd); prune+=(/run/systemd/resolve); }
[ $SYSBUS = 1 ] && { ns+=(--ro-bind /run/dbus /run/dbus --dir /run/systemd/system); keep+=(/run/systemd /run/systemd/system); prune+=(/run/dbus); }
ns+=(--remount-ro /run)
ns+=(--chdir / --clearenv --setenv HOME /home/thc1006 --setenv USER thc1006 --setenv LOGNAME thc1006
     --setenv LANG "${LANG:-C.UTF-8}" --setenv TERM "${TERM:-dumb}" --setenv XDG_RUNTIME_DIR /run/user/1003
     --setenv XDG_CONFIG_HOME "$R/.config" --setenv XDG_CACHE_HOME "$R/.cache" --setenv TMPDIR /tmp
     --setenv PATH "$SB/shim:/usr/local/bin:/usr/bin:/bin" --setenv PYTHONNOUSERSITE 1)
envs="HOME LANG LOGNAME PATH PWD PYTHONNOUSERSITE TERM TMPDIR USER XDG_CACHE_HOME XDG_CONFIG_HOME XDG_RUNTIME_DIR"
# Every bwrap, the job's too, starts through this: it closes every file descriptor above 2, then runs bwrap.
CLOSE=(/usr/bin/python3 -I -S -c 'import os, sys
for f in os.listdir("/proc/self/fd"):
    if int(f) > 2:
        try:
            os.close(int(f))
        except OSError:
            pass
os.execv(sys.argv[1], sys.argv[1:])')
box() { "${CLOSE[@]}" /usr/bin/bwrap "${ns[@]}" "$@" 9<&-; }
# The namespace check: programs by absolute path, no shell, and the host compares what they print.
seen=$(box /usr/bin/stat -c %d:%i -- /home/thc1006/dev /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp "${W[@]}") &&
  [ "$seen" = "$expect" ] || die 98 "namespace check: a writable path is not the sandbox's: nothing run"
nw=(-a ! -w /etc -a ! -w /usr/local/sbin); for d in "${RO[@]}" "$SB/root" "$R"; do nw+=(-a ! -w "$d"); done
[ $HOMEBIND = 1 ] || nw+=(-a ! -w /home/thc1006 -a ! -w "$PROD_CFG")
box /usr/bin/test -e /home/thc1006/dev/.rehearsal-sandbox "${nw[@]}" ||
  die 98 "namespace check: no marker, or a path that must be read-only is writable: nothing run"
[ $HOMEBIND = 1 ] && [ "$(stat -c %d:%i -- "$R")" != "$(box /usr/bin/stat -c %d:%i -- /home/thc1006)" ] &&
  die 98 "namespace check: /home/thc1006 is not the sandbox's: nothing run"
found=$(box /usr/bin/find /dev -mindepth 1 -maxdepth 1 -name 'nvidia*' -print) && [ -z "$found" ] ||
  die 98 "namespace check: a GPU device is there (${found:-find failed}): nothing run"
fx=(/run -mindepth 1)
if [ ${#prune[@]} -gt 0 ]; then fx+=('('); for p in "${prune[@]}"; do fx+=(-path "$p" -o); done; fx[-1]=')'; fx+=(-prune -o); fi
fx+=('('); for p in "${keep[@]}"; do fx+=(-path "$p" -o); done; fx[-1]=')'; fx+=(-o -print)
found=$(box /usr/bin/find "${fx[@]}") && [ -z "$found" ] ||
  die 98 "namespace check: /run holds more than the options add ($(head -3 <<< "${found:-find failed}" | tr '\n' ' ')...): nothing run"
fds=$(box /usr/bin/ls -1 /proc/self/fd | tr '\n' ' ') && [ "$fds" = "0 1 2 3 " ] ||
  die 98 "namespace check: a bwrap gets file descriptors ${fds:-?} (0 1 2 and ls's 3 expected): nothing run"
seen=$(box /usr/bin/env | sed 's/=.*//' | LC_ALL=C sort | tr '\n' ' ') && [ "$seen" = "$envs " ] ||
  die 98 "namespace check: the environment inside is not the listed one ($seen): nothing run"
systemd-run --user --scope -q --unit="$NAME" --description="rebuild rehearsal job $NAME" -p MemoryMax="$MEM" -p MemorySwapMax=0 -- \
  /usr/bin/choom -n 500 -- /usr/bin/nice -n 19 /usr/bin/chrt -i 0 /usr/bin/ionice -c3 /usr/bin/taskset -c "$CPUS" \
  "${CLOSE[@]}" /usr/bin/bwrap "${ns[@]}" "$@" 9<&-
exit $?
