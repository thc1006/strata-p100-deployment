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
# code of it run) lead inside; that no host process, user or system bus, socket in /run, /tmp, /dev or /home, abstract
# unix socket, open file of the caller (a file descriptor above 2) or variable outside the runner's list is there
# inside; and one forced freeze and thaw of a dummy job by bench/rehearsal_guard.sh, on that job only.  It compares 33
# production paths (inode, links, size, mtime, ctime) before and after.
# Its exit status: 0 when every check passed, 1 when one failed ("# FAILED: ..." says which); 2 when it did not start.
# It stops at once (exit 1) when the runner refuses a job, when a job of sections 1-4 fails, when a probe of section 2
# is not refused as expected, when a production name exists inside in section 3 (before section 4 writes through the
# mapped paths), when a marker is found in production in section 4, and when the dummy job of section 8 does not start.
# What it writes: copies of bench/rehearsal_probe.py and bench/rehearsal_settings.py into ~/rebuild-rehearsal/verify
# (for the jobs, which cannot see the repository), the probe token into ~/rebuild-rehearsal/verify/r0-token, the lists
# ~/rebuild-rehearsal/verify/r0-prod-{before,after}.txt (it refuses when any of these five exists or is a symlink), the
# markers and two directories for them under ~/rebuild-rehearsal/root, which a job removes again, and the record on its
# standard output (the caller replaces the host name with <host>).  It changes nothing under ~/rebuild-rehearsal/root
# from the host, where an rm would follow a link that a job left.  Section 4 makes and removes ~/dev/strata and
# ~/dev/Strata-data of the sandbox, so the script refuses to start when either exists, and while an rr-* scope exists.
# It is not the copy that the records ran.  The R0 record ran ~/rebuild-rehearsal/bin/r0-selftest.sh, which had none
# of these refusals and removed with rm -rf; after R11 a second run of it removed the rehearsal's tree and data folder
# from the sandbox (results/rebuild-rehearsal-selftest-rerun-20261030.txt).  The version of commit 2731c92 added
# rm_sandbox (an rm -rf on the host, under the runner's lock, of paths that resolve under ~/rebuild-rehearsal/root) and
# the refusals to start; it ran at 04:14 on 2026-10-30, with the bench/rehearsal_rr.sh of that commit, on the sandbox
# those two directories were gone from (results/rebuild-rehearsal-selftest-committed-20261030.txt), and exited 0 when a
# check after section 3 failed.  This version also stops after a failed section, writes no file that exists, acts on
# the dummy job only, passes the token through a file, removes through a job, gives the exit status above, and needs
# the bench/rehearsal_rr.sh of the same commit; it has run only on scratch directories.
set -u
export PATH=/usr/bin:/bin
SB=/home/thc1006/rebuild-rehearsal; R=$SB/root/home/thc1006
[ ! -e /home/thc1006/dev/.rehearsal-sandbox ] || { echo "rehearsal_selftest: this is the sandbox: run it on the host" >&2; exit 2; }
D=$(cd "$(dirname -- "$0")/.." && pwd -P) || exit 2
case $D/ in "$SB"/*) echo "rehearsal_selftest: run it from a checkout of the repository, not from $SB" >&2; exit 2 ;; esac
RR=$D/bench/rehearsal_rr.sh; GUARD=$D/bench/rehearsal_guard.sh
rr_scopes() {   # the rr-* scopes, one a line; fails when systemctl cannot list them
  local l; l=$(systemctl --user list-units --type=scope --plain --no-legend 'rr-*') || return 1
  printf '%s' "$l"
}
for d in "$R/dev/strata" "$R/dev/Strata-data"; do   # section 4 makes these two for its markers and removes them
  [ -e "$d" ] || [ -L "$d" ] || continue
  echo "rehearsal_selftest: $d exists: section 4 would remove it; R0 runs only on a new sandbox" >&2; exit 2
done
for f in r0-prod-before.txt r0-prod-after.txt r0-token rehearsal_probe.py rehearsal_settings.py; do
  [ ! -e "$SB/verify/$f" ] && [ ! -L "$SB/verify/$f" ] && continue
  echo "rehearsal_selftest: $SB/verify/$f exists: R0 runs only on a new sandbox" >&2; exit 2
done
[ -d "$SB/verify" ] && [ ! -L "$SB/verify" ] && [ "$(realpath -e -- "$SB/verify")" = "$SB/verify" ] ||
  { echo "rehearsal_selftest: $SB/verify is not a directory at its own path" >&2; exit 2; }
s=$(rr_scopes) || { echo "rehearsal_selftest: cannot list the rr-* scopes: not starting" >&2; exit 2; }
[ -z "$s" ] || { echo "rehearsal_selftest: an rr-* scope exists: not starting" >&2; exit 2; }
set -o noclobber   # every file it writes is new: > refuses one that exists, and does not follow a symlink
TOK=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
fail=0
run() { local rc; printf '\n$'; printf ' %q' "$@"; printf '\n'; "$@" 2>&1; rc=$?; printf '[exit %s]\n' "$rc"; return $rc; }
bad() { echo "# FAILED: $*"; fail=1; }
stop() { echo "# FAILED: $*"; echo "# stopped"; exit 1; }
job() {   # one job through the runner: stops when the runner refuses it; else the job's status
  local out rc; printf '\n$'; printf ' %q' "$RR" "$@"; printf '\n'
  out=$("$RR" "$@" 2>&1); rc=$?; [ -z "$out" ] || printf '%s\n' "$out"; printf '[exit %s]\n' "$rc"
  grep -q '^rehearsal_rr: refused' <<< "$out" && stop "the runner refused the job"
  return $rc
}
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
job -n rr-r0-props -m 256M -- sh -c 'id; echo "pid $$"; chrt -p $$; echo "nice $(nice)"; ionice -p $$; echo "oom_score_adj $(cat /proc/self/oom_score_adj)"; taskset -pc $$
  cg=$(sed -n "s/^0:://p" /proc/self/cgroup); echo "cgroup $cg"; echo "memory.max $(cat /sys/fs/cgroup$cg/memory.max) memory.swap.max $(cat /sys/fs/cgroup$cg/memory.swap.max)"
  for p in / /etc /usr/local/sbin /home/thc1006 /home/thc1006/.config/strata /home/thc1006/rebuild-rehearsal /home/thc1006/rebuild-rehearsal/prod/dev/strata \
           /home/thc1006/rebuild-rehearsal/prod/cuda-12.9 /home/thc1006/dev /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp /run /run/user/1003 /proc; do
    printf "%-45s %s\n" "$p" "$(findmnt -no SOURCE,OPTIONS -T $p | cut -c1-90)"; done' || stop "section 1"
echo; echo "== 2. writes into production paths, from inside (each must be refused)"
job -n rr-r0-probe -m 256M -- "$SB/verify/rehearsal_probe.py" "$TOK" || stop "a probe was not refused as expected"
echo; echo "== 3. the production paths at their own names, inside: they are the sandbox's"
job -n rr-r0-names -m 256M -- sh -c 'ls -A /home/thc1006/dev /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp; bad=0; for p in /home/thc1006/dev/strata /home/thc1006/dev/Strata-data /home/thc1006/dev/p100-q3-deployment /home/thc1006/cuda-12.9/bin/nvcc; do if [ -e $p ]; then echo "$p exists"; bad=1; else echo "$p does not exist inside"; fi; done; exit $bad' ||
  stop "a production name exists inside"
echo; echo "== 4. markers written through the mapped paths inside, found under ~/rebuild-rehearsal outside"
job -n rr-r0-mark -m 256M -- sh -c 'set -e; T=$(cat /home/thc1006/rebuild-rehearsal/verify/r0-token); mkdir -p /home/thc1006/dev/strata /home/thc1006/dev/Strata-data/models /home/thc1006/dev/Strata-data/packs /home/thc1006/dev/Strata-data/mtp
  for p in /home/thc1006/dev /home/thc1006/dev/strata /home/thc1006/dev/Strata-data/models /home/thc1006/dev/Strata-data/packs /home/thc1006/dev/Strata-data/mtp /home/thc1006/cuda-12.9 /home/thc1006/downloads /tmp "$XDG_CONFIG_HOME"; do
    echo "written inside through $p" > $p/.rr-marker-$T && echo "wrote $p/.rr-marker-$T"; done' || stop "section 4: the markers were not all written"
run find "$SB" -name ".rr-marker-$TOK" -printf '%p\n'
[ "$(find "$SB" -name ".rr-marker-$TOK" | wc -l)" = 9 ] || bad "not all 9 markers are under $SB"
printf '\n$ ls -d <the same names outside, in production>\n'
found=0
for p in ~/dev ~/dev/strata ~/dev/Strata-data/models ~/dev/Strata-data/packs ~/dev/Strata-data/mtp ~/cuda-12.9 ~/downloads /tmp ~/.config; do
  if [ -e "$p/.rr-marker-$TOK" ] || [ -L "$p/.rr-marker-$TOK" ]; then echo "FOUND $p/.rr-marker-$TOK"; found=1; else echo "absent ${p/#$HOME/~}/.rr-marker-$TOK"; fi
done
[ $found = 0 ] || stop "a marker is in production"
echo "# cleanup of the markers and of the two directories made for them, by a job:"
job -n rr-r0-clean -m 256M -- /usr/bin/rm -rf -- /home/thc1006/dev/strata /home/thc1006/dev/Strata-data /home/thc1006/dev/.rr-marker-"$TOK" \
  /home/thc1006/cuda-12.9/.rr-marker-"$TOK" /home/thc1006/downloads/.rr-marker-"$TOK" /tmp/.rr-marker-"$TOK" "$R/.config/.rr-marker-$TOK" ||
  bad "the cleanup job failed"
run find "$SB" -name ".rr-marker-$TOK" -print
[ -z "$(find "$SB" -name ".rr-marker-$TOK")" ] || bad "a marker is left under $SB"
echo; echo "== 5. no GPU inside; the stand-ins"
job -n rr-r0-gpu -m 256M -- sh -c 'bad=0; ls -l /dev/nvidia* 2>&1 && bad=1; echo "nvidia names in /dev: $(ls /dev | grep -c nvidia)"; command -v nvidia-smi sudo; nvidia-smi --query-gpu=index,name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits; nvidia-smi -L; echo "nvidia-smi -L exit $?"; sudo -n true && bad=1; echo "sudo exit $?"; exit $bad' ||
  bad "section 5: a GPU device is there, or sudo ran"
echo; echo "== 6. setup.py's settings inside: its own settings_path() and load_settings()"
job -n rr-r0-settings -m 256M -- "$SB/verify/rehearsal_settings.py" || bad "section 6"
job -n rr-r0-dirs -m 256M -- sh -c 'echo "strata* in /home/thc1006 (setup.py other_installs, grandparent of the tree):"; ls -d /home/thc1006/[Ss]trata* 2>&1; echo "in /home/thc1006/dev (its parent):"; ls -A /home/thc1006/dev'
echo; echo "== 7. routes out of the namespace, closed"
job -n rr-r0-routes -m 256M -- sh -c 'bad=0
  echo "processes visible: $(ls /proc | grep -c "^[0-9]")"
  if pgrep -fa "[e]ngine-cuda12/strata|[s]erver.py"; then bad=1; else echo "no Strata engine or server process visible"; fi
  echo "in /run: $(find /run -mindepth 1 | tr "\n" " ")"; [ "$(find /run -mindepth 1 | tr "\n" " ")" = "/run/user /run/user/1003 " ] || bad=1
  s=$(find /run /tmp /dev /home -type s 2>/dev/null); echo "sockets in /run, /tmp, /dev and /home: ${s:-none}"; [ -z "$s" ] || bad=1
  echo "unix sockets in the network namespace: $(($(wc -l < /proc/net/unix) - 1)), of them the CUDA driver'"'"'s: $(grep -c cuda-uvmfd /proc/net/unix)"; [ "$(wc -l < /proc/net/unix)" = 1 ] || bad=1
  systemctl --user show -p Version 2>&1 | head -1; systemctl --user show -p Version >/dev/null 2>&1 && bad=1
  systemctl show -p Version 2>&1 | head -1; systemctl show -p Version >/dev/null 2>&1 && bad=1
  echo "file descriptors of this shell:"; ls -l /proc/$$/fd
  for f in /proc/$$/fd/*; do [ -L "$f" ] && [ "${f##*/}" -gt 2 ] && bad=1; done
  e=$(env | sed "s/=.*//" | LC_ALL=C sort | tr "\n" " "); echo "variables: $e"
  [ "$e" = "HOME LANG LOGNAME PATH PWD PYTHONNOUSERSITE TERM TMPDIR USER XDG_CACHE_HOME XDG_CONFIG_HOME XDG_RUNTIME_DIR " ] || bad=1
  echo "in /home/thc1006: $(ls -A /home/thc1006 | tr "\n" " ")"; exit $bad' || bad "section 7: a route out of the namespace is open"
echo; echo "== 8. the guard: one freeze and thaw of a dummy job (sleep), then one unforced round"
"$RR" -n rr-r0-dummy -m 64M -- sleep 600 > /dev/null 2>&1 &
cg=''
for _ in $(seq 20); do
  [ "$(systemctl --user show -p ActiveState --value rr-r0-dummy.scope)" = active ] && cg=$(systemctl --user show -p ControlGroup --value rr-r0-dummy.scope)
  [ -n "$cg" ] && [ -n "$(cat "/sys/fs/cgroup$cg/cgroup.procs" 2>/dev/null)" ] && break
  cg=''; sleep 1
done
[ -n "$cg" ] || { systemctl --user stop rr-r0-dummy.scope 2>/dev/null; wait; stop "section 8: the dummy job did not start"; }
fz() { local st; st=$(systemctl --user show -p FreezerState --value rr-r0-dummy.scope); echo "FreezerState=$st"; [ "$st" = "$1" ] || bad "section 8: FreezerState $st, not $1"; }
run systemctl --user show -p ActiveState -p FreezerState rr-r0-dummy.scope
run env RR_GUARD_FORCE=freeze RR_GUARD_ONLY=rr-r0-dummy.scope "$GUARD" --once
sleep 1
printf '\n$ systemctl --user show -p FreezerState --value rr-r0-dummy.scope\n'; fz frozen
run cat "/sys/fs/cgroup$cg/cgroup.events"
run sh -c "ps -o pid,stat,cmd -p \$(tr '\n' , < /sys/fs/cgroup$cg/cgroup.procs | sed 's/,\$//') | cut -c1-200"
run env RR_GUARD_FORCE=thaw RR_GUARD_ONLY=rr-r0-dummy.scope "$GUARD" --once
sleep 1
printf '\n$ systemctl --user show -p FreezerState --value rr-r0-dummy.scope\n'; fz running
run cat "/sys/fs/cgroup$cg/cgroup.events"
run "$GUARD" --once || bad "section 8: the unforced round failed"
run systemctl --user stop rr-r0-dummy.scope
wait
run sh -c "systemctl --user list-units --type=scope --plain --no-legend 'rr-*' | grep -c '^rr-' || true"
echo; echo "== 9. the state after"
prod_stat > "$SB/verify/r0-prod-after.txt" || stop "cannot write $SB/verify/r0-prod-after.txt"
run diff "$SB/verify/r0-prod-before.txt" "$SB/verify/r0-prod-after.txt" || bad "a production path changed"
printf '\n$ <the %s production paths of section 2, before = after>\n' "$(wc -l < "$SB/verify/r0-prod-before.txt")"
if cmp -s "$SB/verify/r0-prod-before.txt" "$SB/verify/r0-prod-after.txt"; then echo "inode, link count, size, mtime and ctime unchanged for every path"; else bad "the production paths differ"; fi
n=$(find ~/dev/strata ~/dev/Strata-data ~/dev/p100-q3-deployment ~/cuda-12.9 ~/.config/strata /etc /etc/systemd/system /usr/local/sbin ~/dev ~/downloads /tmp -maxdepth 1 -name '.rr-*' 2>/dev/null | wc -l)
printf '\n$ <.rr-* names in production and /tmp>\n%s\n' "$n"; [ "$n" = 0 ] || bad "a .rr-* name is in production or /tmp"
run systemctl is-active strata-q3.service strata-q3-health.timer
run cat /sys/fs/cgroup/user.slice/user-1003.slice/memory.current
echo
if [ $fail = 0 ]; then echo "# result: every check passed"; else echo "# result: a check failed (the lines # FAILED above)"; fi
exit $fail
