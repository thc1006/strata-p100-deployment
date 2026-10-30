#!/bin/bash
# bench/rehearsal_selftest.sh > RECORD: R0 of the rebuild rehearsal, the sandbox's self-test (its two Python
# programs are bench/rehearsal_probe.py and bench/rehearsal_settings.py; its record is
# results/rebuild-rehearsal-r0-20261030.txt).
# Part of the rebuild rehearsal of 2026-10-30 (docs/runbooks/rebuild.md; results/rebuild-rehearsal-*-20261030.txt).
# It is in bench/: run it only when the maintainer asks (AGENTS.md).  Run it on the host, from a checkout of this
# repository, on a new sandbox made as bench/rehearsal_rr.sh's header says, with the guard running as rrg-guard.scope.
# Through bench/rehearsal_rr.sh it shows, with harmless probes: the namespace's mounts and the job's limits; that a
# file cannot be created, changed, renamed or deleted in production's tree, data, checkout, toolkit, ~/.config/strata,
# /etc and /usr/local/sbin (the delete names a file that does not exist and the rename is to the same name, so neither
# could change anything if the kernel allowed it; it refuses both on a read-only mount); that markers written through
# the mapped paths land under ~/rebuild-rehearsal and not in production; that no /dev/nvidia* exists and the stand-ins
# answer; where setup.py's own settings_path() and load_settings() (taken from production's setup.py with ast, no other
# code of it run) lead inside; that no host process, bus or abstract unix socket is visible; and one forced freeze and
# thaw of a dummy job by bench/rehearsal_guard.sh, on that job only.  It compares 33 production paths (inode, links,
# size, mtime, ctime) before and after.  It stops (exit 1) when a probe of section 2 is not refused as expected or a
# production name exists inside in section 3, before section 4 writes through the mapped paths.
# What it writes: copies of bench/rehearsal_probe.py and bench/rehearsal_settings.py into ~/rebuild-rehearsal/verify
# (for the jobs, which cannot see the repository), the probe token into ~/rebuild-rehearsal/verify/r0-token (the jobs
# get no variable of the caller), the lists ~/rebuild-rehearsal/verify/r0-prod-{before,after}.txt (it refuses when any
# of these five exists or is a symlink), the markers (removed again) and two directories for them
# under ~/rebuild-rehearsal/root, and the record on its standard output (the caller replaces the host name with
# <host>).  Every removal goes through rm_sandbox, which takes bench/rehearsal_rr.sh's lock on ~/rebuild-rehearsal (so
# no job runs), refuses while an rr-* scope exists or when the scopes cannot be listed, resolves each path (links
# followed) and refuses one that is not under ~/rebuild-rehearsal/root.  Section 4 removes the two directories it
# makes, ~/dev/strata and ~/dev/Strata-data of the sandbox, so the script refuses to start when either exists, and
# while an rr-* scope exists.
# It is not the copy that the records ran.  The R0 record ran ~/rebuild-rehearsal/bin/r0-selftest.sh, which had none
# of these refusals and removed with rm -rf; after R11 a second run of it removed the rehearsal's tree and data folder
# from the sandbox (results/rebuild-rehearsal-selftest-rerun-20261030.txt).  The version of commit 2731c92 added
# rm_sandbox and the refusals to start; it ran at 04:14 on 2026-10-30, with the bench/rehearsal_rr.sh of that commit,
# on the sandbox those two directories were gone from (results/rebuild-rehearsal-selftest-committed-20261030.txt).
# This version also takes the lock, stops after a failed section, writes no file that exists, acts on the dummy job
# only, passes the token through a file, and needs the bench/rehearsal_rr.sh of the same commit; it has run only on
# scratch directories.
set -u
export PATH=/usr/bin:/bin
SB=/home/thc1006/rebuild-rehearsal; R=$SB/root/home/thc1006; SBROOT=$SB/root
[ ! -e /home/thc1006/dev/.rehearsal-sandbox ] || { echo "rehearsal_selftest: this is the sandbox: run it on the host" >&2; exit 2; }
D=$(cd "$(dirname -- "$0")/.." && pwd -P) || exit 2
case $D/ in "$SB"/*) echo "rehearsal_selftest: run it from a checkout of the repository, not from $SB" >&2; exit 2 ;; esac
RR=$D/bench/rehearsal_rr.sh; GUARD=$D/bench/rehearsal_guard.sh
rr_scopes() {   # the rr-* scopes, one a line; fails when systemctl cannot list them
  local l; l=$(systemctl --user list-units --type=scope --plain --no-legend 'rr-*') || return 1
  printf '%s' "$l"
}
rm_sandbox() {   # rm -rf of paths that resolve (links followed) under ~/rebuild-rehearsal/root, under the lock, no rr-* scope
  (
    exec 8<"$SB" && flock -n 8 || { echo "refusing to remove $*: a job holds the lock on $SB"; exit 1; }
    s=$(rr_scopes) || { echo "refusing to remove $*: cannot list the rr-* scopes"; exit 1; }
    [ -z "$s" ] || { echo "refusing to remove $*: an rr-* scope exists"; exit 1; }
    for p in "$@"; do
      t=$(realpath -m -- "$p") || { echo "refusing: cannot resolve $p"; exit 1; }
      case $t in "$SBROOT"/?*) ;; *) echo "refusing to remove $p: it resolves to $t, not under $SBROOT"; exit 1 ;; esac
    done
    rm -rf -- "$@"
  )
}
for d in "$R/dev/strata" "$R/dev/Strata-data"; do   # section 4 makes these two for its markers and removes them
  [ -e "$d" ] || [ -L "$d" ] || continue
  echo "rehearsal_selftest: $d exists: section 4 would remove it; R0 runs only on a new sandbox" >&2; exit 1
done
for f in r0-prod-before.txt r0-prod-after.txt r0-token rehearsal_probe.py rehearsal_settings.py; do
  [ ! -e "$SB/verify/$f" ] && [ ! -L "$SB/verify/$f" ] && continue
  echo "rehearsal_selftest: $SB/verify/$f exists: R0 runs only on a new sandbox" >&2; exit 1
done
[ -d "$SB/verify" ] && [ ! -L "$SB/verify" ] && [ "$(realpath -e -- "$SB/verify")" = "$SB/verify" ] ||
  { echo "rehearsal_selftest: $SB/verify is not a directory at its own path" >&2; exit 1; }
s=$(rr_scopes) || { echo "rehearsal_selftest: cannot list the rr-* scopes: not starting" >&2; exit 1; }
[ -z "$s" ] || { echo "rehearsal_selftest: an rr-* scope exists: not starting" >&2; exit 1; }
set -o noclobber   # every file it writes is new: > refuses one that exists, and does not follow a symlink
TOK=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
run() { local rc; printf '\n$'; printf ' %q' "$@"; printf '\n'; "$@" 2>&1; rc=$?; printf '[exit %s]\n' "$rc"; return $rc; }
stop() { echo "# stopped: $*"; exit 1; }
prod_stat() {   # production paths that the probes aim at: inode, links, size, mtime, ctime (read only)
  stat -c '%n %i %h %s %Y %Z' ~/dev/strata ~/dev/strata/strata-iq3_xxs.json ~/dev/strata/engine-cuda12 \
    ~/dev/strata/engine-cuda12/strata ~/dev/strata/.venv ~/dev/strata/.venv/pyvenv.cfg ~/dev/Strata-data \
    ~/dev/Strata-data/models ~/dev/Strata-data/models/IQ3_S ~/dev/Strata-data/models/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf \
    ~/dev/Strata-data/models/IQ3_XXS ~/dev/Strata-data/models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf \
    ~/dev/Strata-data/packs ~/dev/Strata-data/packs/iq3_s ~/dev/Strata-data/packs/iq3_s/experts.bin ~/dev/Strata-data/packs/iq3_xxs \
    ~/dev/Strata-data/packs/iq3_xxs/index.txt ~/dev/Strata-data/mtp ~/dev/Strata-data/mtp/mtp-inventory.json ~/dev/Strata-data/mtp/rt \
    ~/dev/Strata-data/mtp/rt/experts.bin ~/dev/p100-q3-deployment ~/dev/p100-q3-deployment/config/profile.conf ~/cuda-12.9 \
    ~/cuda-12.9/version.json ~/.config/strata ~/.config/strata/settings.json /etc /etc/hostname /etc/systemd/system \
    /etc/systemd/system/strata-q3.service /usr/local/sbin /usr/local/sbin/strata-q3-health \
    | sed "s|$HOME|~|"
}
echo "# R0 of the rebuild rehearsal: the bubblewrap sandbox's self-test, on <host>, $(date '+%F %T %z')."
echo "# Every job ran through bench/rehearsal_rr.sh: its own user scope (MemoryMax, MemorySwapMax=0), choom -n 500,"
echo "# nice -n 19, chrt -i 0, ionice -c3, taskset -c 7, then bwrap.  No sudo was run.  Production was read only."
echo "# Probe token (in every probe file name): $TOK.  The host name is written <host>; everything else is the output, unchanged."
echo; echo "== 0. the state before"
run systemctl is-active strata-q3.service strata-q3-health.timer
run systemctl --user is-active rrg-guard.scope
run cat /sys/fs/cgroup/user.slice/user-1003.slice/memory.current
run df --output=avail,pcent /
run sha256sum "$RR" "$GUARD" "$0" "$D/bench/rehearsal_probe.py" "$D/bench/rehearsal_settings.py" "$SB/shim/sudo" "$SB/shim/nvidia-smi"
prod_stat > "$SB/verify/r0-prod-before.txt" || stop "cannot write $SB/verify/r0-prod-before.txt"
echo "$TOK" > "$SB/verify/r0-token" || stop "cannot write $SB/verify/r0-token"   # the jobs get no variable of the caller
install -m 755 "$D/bench/rehearsal_probe.py" "$D/bench/rehearsal_settings.py" "$SB/verify/" || stop "cannot copy the probes"
echo; echo "== 1. the namespace and the job's limits, seen from inside"
run "$RR" -n rr-r0-props -m 256M -- sh -c 'id; echo "pid $$"; chrt -p $$; echo "nice $(nice)"; ionice -p $$; echo "oom_score_adj $(cat /proc/self/oom_score_adj)"; taskset -pc $$
  cg=$(sed -n "s/^0:://p" /proc/self/cgroup); echo "cgroup $cg"; echo "memory.max $(cat /sys/fs/cgroup$cg/memory.max) memory.swap.max $(cat /sys/fs/cgroup$cg/memory.swap.max)"
  for p in / /etc /usr/local/sbin /home/thc1006 /home/thc1006/.config/strata /home/thc1006/rebuild-rehearsal /home/thc1006/rebuild-rehearsal/prod/dev/strata \
           /home/thc1006/rebuild-rehearsal/prod/cuda-12.9 /home/thc1006/dev /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp /run/user/1003 /run/dbus /proc; do
    printf "%-45s %s\n" "$p" "$(findmnt -no SOURCE,OPTIONS -T $p | cut -c1-90)"; done' || stop "section 1"
echo; echo "== 2. writes into production paths, from inside (each must be refused)"
run "$RR" -n rr-r0-probe -m 256M -- "$SB/verify/rehearsal_probe.py" "$TOK" || stop "a probe was not refused as expected"
echo; echo "== 3. the production paths at their own names, inside: they are the sandbox's"
run "$RR" -n rr-r0-names -m 256M -- sh -c 'ls -A /home/thc1006/dev /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp; bad=0; for p in /home/thc1006/dev/strata /home/thc1006/dev/Strata-data /home/thc1006/dev/p100-q3-deployment /home/thc1006/cuda-12.9/bin/nvcc; do if [ -e $p ]; then echo "$p exists"; bad=1; else echo "$p does not exist inside"; fi; done; exit $bad' ||
  stop "a production name exists inside"
echo; echo "== 4. markers written through the mapped paths inside, found under ~/rebuild-rehearsal outside"
run "$RR" -n rr-r0-mark -m 256M -- sh -c 'set -e; T=$(cat /home/thc1006/rebuild-rehearsal/verify/r0-token); mkdir -p /home/thc1006/dev/strata /home/thc1006/dev/Strata-data/models /home/thc1006/dev/Strata-data/packs /home/thc1006/dev/Strata-data/mtp
  for p in /home/thc1006/dev /home/thc1006/dev/strata /home/thc1006/dev/Strata-data/models /home/thc1006/dev/Strata-data/packs /home/thc1006/dev/Strata-data/mtp /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp "$XDG_CONFIG_HOME"; do
    echo "written inside through $p" > $p/.rr-marker-$T && echo "wrote $p/.rr-marker-$T"; done'
run find "$SB" -name ".rr-marker-$TOK" -printf '%p\n'
printf '\n$ ls -d <the same names outside, in production>\n'
for p in ~/dev ~/dev/strata ~/dev/Strata-data/models ~/dev/Strata-data/packs ~/dev/Strata-data/mtp ~/cuda-12.9 ~/downloads /tmp ~/.config; do
  [ -e "$p/.rr-marker-$TOK" ] && echo "FOUND $p/.rr-marker-$TOK" || echo "absent ${p/#$HOME/~}/.rr-marker-$TOK"; done
echo "# cleanup of the markers and of the two directories made for them, by their sandbox paths, outside:"
run rm_sandbox "$R/dev/strata" "$R/dev/Strata-data"
run rm_sandbox "$R/dev/.rr-marker-$TOK" "$R/cuda-12.9/.rr-marker-$TOK" "$R/downloads/.rr-marker-$TOK" "$SB/root/tmp/.rr-marker-$TOK" \
  "$R/.config/.rr-marker-$TOK"
run find "$SB" -name ".rr-marker-$TOK" -print
echo; echo "== 5. no GPU inside; the stand-ins"
run "$RR" -n rr-r0-gpu -m 256M -- sh -c 'ls -l /dev/nvidia* 2>&1; ls /dev | grep -c nvidia; command -v nvidia-smi sudo; nvidia-smi --query-gpu=index,name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits; nvidia-smi -L; echo "nvidia-smi -L exit $?"; sudo -n true; echo "sudo exit $?"'
echo; echo "== 6. setup.py's settings inside: its own settings_path() and load_settings()"
run "$RR" -n rr-r0-settings -m 256M -- "$SB/verify/rehearsal_settings.py"
run "$RR" -n rr-r0-dirs -m 256M -- sh -c 'echo "strata* in /home/thc1006 (setup.py other_installs, grandparent of the tree):"; ls -d /home/thc1006/[Ss]trata* 2>&1; echo "in /home/thc1006/dev (its parent):"; ls -A /home/thc1006/dev'
echo; echo "== 7. routes out of the namespace, closed"
run "$RR" -n rr-r0-routes -m 256M -- sh -c 'echo "processes visible: $(ls /proc | grep -c "^[0-9]")"; pgrep -fa "[e]ngine-cuda12/strata|[s]erver.py" || echo "no Strata engine or server process visible"; ls -A /run/user/1003 /run/dbus; systemctl --user is-system-running 2>&1 | head -2; systemctl is-active strata-q3.service 2>&1 | head -2; echo "abstract unix sockets of the CUDA driver visible: $(grep -c cuda-uvmfd /proc/net/unix)"; ls -A /home/thc1006'
echo; echo "== 8. the guard: one freeze and thaw of a dummy job (sleep), then one unforced round"
"$RR" -n rr-r0-dummy -m 64M -- sleep 600 > /dev/null 2>&1 &
sleep 3
cg=$(systemctl --user show -p ControlGroup --value rr-r0-dummy.scope)
run systemctl --user show -p ActiveState -p FreezerState rr-r0-dummy.scope
run env RR_GUARD_FORCE=freeze RR_GUARD_ONLY=rr-r0-dummy.scope "$GUARD" --once
sleep 1
run systemctl --user show -p FreezerState rr-r0-dummy.scope
run cat "/sys/fs/cgroup$cg/cgroup.events"
run sh -c "ps -o pid,stat,cmd -p \$(tr '\n' , < /sys/fs/cgroup$cg/cgroup.procs | sed 's/,\$//') | cut -c1-200"
run env RR_GUARD_FORCE=thaw RR_GUARD_ONLY=rr-r0-dummy.scope "$GUARD" --once
sleep 1
run systemctl --user show -p FreezerState rr-r0-dummy.scope
run cat "/sys/fs/cgroup$cg/cgroup.events"
run "$GUARD" --once
run systemctl --user stop rr-r0-dummy.scope
wait
run sh -c "systemctl --user list-units --type=scope --plain --no-legend 'rr-*' | grep -c '^rr-' || true"
echo; echo "== 9. the state after"
prod_stat > "$SB/verify/r0-prod-after.txt" || stop "cannot write $SB/verify/r0-prod-after.txt"
run diff "$SB/verify/r0-prod-before.txt" "$SB/verify/r0-prod-after.txt"
printf '\n$ <the %s production paths of section 2, before = after>\n' "$(wc -l < "$SB/verify/r0-prod-before.txt")"
cmp -s "$SB/verify/r0-prod-before.txt" "$SB/verify/r0-prod-after.txt" && echo "inode, link count, size, mtime and ctime unchanged for every path"
run sh -c "find ~/dev/strata ~/dev/Strata-data ~/dev/p100-q3-deployment ~/cuda-12.9 ~/.config/strata /etc /etc/systemd/system /usr/local/sbin ~/dev ~/downloads /tmp -maxdepth 1 -name '.rr-*' 2>/dev/null | wc -l"
run systemctl is-active strata-q3.service strata-q3-health.timer
run cat /sys/fs/cgroup/user.slice/user-1003.slice/memory.current
