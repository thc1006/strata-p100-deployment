#!/usr/bin/env bash
# One run of the IQ3_XXS / IQ3_S A/B (docs/decisions/0004-iq3-s.md): the standard perf set (bench/run_profile.sh),
# with a record of what the service ran before and after it and of everything that went wrong during it.
# It starts and stops nothing: the restarts between runs are done by hand, in the decision record's order.
# It refuses to measure when it could not record the run completely, when the engine started less than SETTLE_S
# (default 300) seconds ago, or when a request is in flight.
#   <tag>-arm.txt     before: the deployment commit, the sha256 of the config, the shared settings, server.py and the
#                     clients, the engine's pid, start time (/proc), exe, args and environment, /health, the version
#                     line, the ZFS ARC cap, MemAvailable and every GPU's ECC error counters
#   <tag>-after.txt   after: whether the same engine answered, the error lines the engine log, the service journal
#                     (systemd's own lines included, read with sudo) and the kernel log (NVIDIA Xid, OOM) got during
#                     the run, the journal's "[strata] done:" lines by finish (run_profile.sh sends 98 requests: more
#                     means other traffic, fewer lost requests), the perf log's Traceback lines, the ECC counters,
#                     whether the run was interrupted.  run_profile.sh itself always exits 0.
#   <tag>-engine-errors.txt, <tag>-journal-errors.txt, <tag>-kernel-errors.txt   those lines
# Every name of this host (hostname, -f, -A) is written as <host>.
# Usage: bench/ab_model_run.sh <tag> <arm letter>
set -u -o pipefail
TAG=${1:?tag}; ARM=${2:?arm letter}
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
URL=http://127.0.0.1:8080
STRATA=${STRATA_DIR:-$HOME/dev/strata}
CFG=$STRATA/strata-iq3_xxs.json
SHARED=$STRATA/strata-iq3_xxs.shared-settings.json
SETTLE_S=${SETTLE_S:-300}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT        # raw journal copies never go near results/
say() { echo "ab_model_run: $*"; }
for f in arm.txt after.txt engine-errors.txt journal-errors.txt kernel-errors.txt; do
  [ -e "$R/$TAG-$f" ] && { say "refusing: $R/$TAG-$f exists (pick a new tag)"; exit 2; }
done

# every name of this host, longest first (a name that contains another is replaced whole)
names=$({ hostname; hostname -f; hostname -A; } 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sort -u | awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)
[ -n "$names" ] || { say "refusing: no host name to redact"; exit 1; }
redact_args=(); while IFS= read -r n; do redact_args+=(-e "s/$(printf '%s' "$n" | sed 's/[.[\*^$/]/\\&/g')/<host>/g"); done <<< "$names"
redact() { sed "${redact_args[@]}"; }

# the service's own engine: the child of strata-q3's main process, as scripts/strata-health.sh finds it
main=$(systemctl show -p MainPID --value strata-q3.service)
[[ "$main" =~ ^[1-9][0-9]*$ ]] || { say "refusing: strata-q3 has no main process"; exit 1; }
engine() { pgrep -P "$1" -f "^$STRATA/engine-cuda12/strata --serve" | head -1; }
pid=$(engine "$main")
[ -n "$pid" ] && [ "$(readlink -f "/proc/$pid/exe")" = "$STRATA/engine-cuda12/strata" ] ||
  { say "refusing: no engine under strata-q3's main process $main"; exit 1; }
start_ticks() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }
ticks0=$(start_ticks "$pid")
age=$(( $(awk '{print int($1)}' /proc/uptime) - ticks0 / $(getconf CLK_TCK) ))
[ "$age" -ge "$SETTLE_S" ] || { say "refusing: the engine started $age s ago (wait until it has run $SETTLE_S s)"; exit 1; }
LOG=$(readlink -f "/proc/$pid/fd/2")
[ -r "$LOG" ] || { say "refusing: cannot read the engine's log ($LOG)"; exit 1; }
health=$(curl -fsS -m 10 "$URL/health") || { say "refusing: /health does not answer"; exit 1; }
grep -q '"loaded": true' <<< "$health" || { say "refusing: the model is not loaded"; exit 1; }
busy=$(curl -fsS -m 10 "$URL/metrics" | python3 -c 'import json,sys; l=json.load(sys.stdin)["live"]; print("idle" if l.get("state") == "idle" and not l.get("running") and not l.get("queued") else "busy")') ||
  { say "refusing: /metrics does not answer"; exit 1; }
[ "$busy" = idle ] || { say "refusing: requests are in flight"; exit 1; }
sudo -n journalctl -n 1 --no-pager -q > /dev/null 2>&1 || { say "refusing: the system journal needs sudo -n"; exit 1; }
ecc() { nvidia-smi --query-gpu=index,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv,noheader | tr '\n' ';'; }
sha() { if [ -e "$1" ]; then sha256sum < "$1" | cut -c1-16; else echo absent; fi; }

start_line=$(wc -l < "$LOG"); start_size=$(stat -c %s "$LOG"); start_inode=$(stat -c %i "$LOG")
t_start=$(date '+%F %T')
ecc0=$(ecc)
{
  echo "tag $TAG  arm $ARM  at $t_start"
  commit=$(git -C "$D" rev-parse HEAD 2>/dev/null) || commit=unknown
  if [ "$commit" = unknown ]; then dirty=""
  elif git -C "$D" diff --quiet HEAD -- bench scripts tools config 2>/dev/null; then dirty=""
  else dirty=" (uncommitted changes)"; fi
  echo "deployment commit $commit$dirty"
  echo "config $CFG sha256 $(sha "$CFG")"
  echo "shared settings $SHARED sha256 $(sha "$SHARED")"
  echo "server.py sha256 $(sha "$STRATA/serve/server.py")  serve_load.py sha256 $(sha "$STRATA/tools/serve_load.py")"
  echo "engine pid $pid exe $(readlink -f /proc/$pid/exe) sha256 $(sha256sum < /proc/$pid/exe | cut -c1-16)"
  echo "engine start ticks $ticks0  engine age ${age} s  settle $SETTLE_S s"
  echo "engine args: $(tr '\0' ' ' < /proc/$pid/cmdline)"
  echo "engine env: $(tr '\0' '\n' < /proc/$pid/environ | grep -E '^(STRATA|CUDA)_' | sort | tr '\n' ' ')"
  echo "engine log: $LOG"
  echo "engine started: $(ps -o lstart= -p "$pid")"
  echo "health: $health"
  echo "version line: $(grep -a -o 'session is up (engine [0-9.]*)' "$LOG" | tail -1)"
  echo "zfs_arc_max $(cat /sys/module/zfs/parameters/zfs_arc_max)  MemAvailable $(awk '/MemAvailable/{print $2}' /proc/meminfo) kB"
  echo "gpu ecc (index, corrected, uncorrected): $ecc0"
} | redact > "$R/$TAG-arm.txt"
for want in '^tag ' '^engine args: ' '^health: ' '^engine start ticks ' '^gpu ecc '; do
  grep -q "$want" "$R/$TAG-arm.txt" || { say "refusing: the arm record lacks ${want#^}"; exit 1; }
done

# the perf set in a process group of its own: an interrupt stops all of it, and the after record still gets written
interrupted=no
setsid "$D/bench/run_profile.sh" "$TAG" &
child=$!
trap 'interrupted=yes; kill -TERM -- -"$child" 2>/dev/null' INT TERM HUP
while kill -0 "$child" 2>/dev/null; do wait "$child"; done
trap - INT TERM HUP
wait "$child" 2>/dev/null; rc=$?

# what happened during the run.  The graph captures log "upload no error, sync no error": not an error.
patterns='error|warn|refused|no progress|watchdog|out of memory|restart|fail|abort|timeout|non-?finite|nonfinite|repeated|said nothing|has waited'
if [ -r "$LOG" ] && [ "$(stat -c %i "$LOG")" = "$start_inode" ] && [ "$(stat -c %s "$LOG")" -ge "$start_size" ]; then
  tail -n +"$((start_line + 1))" "$LOG" | grep -a -i -E "$patterns" | grep -v 'upload no error, sync no error' | redact > "$R/$TAG-engine-errors.txt"
  engine_errors=$(wc -l < "$R/$TAG-engine-errors.txt")
else
  : > "$R/$TAG-engine-errors.txt"; engine_errors="unknown (the log was replaced, truncated or removed during the run)"
fi
if sudo -n journalctl -u strata-q3.service --since "$t_start" --no-pager -q > "$TMP/journal" 2>/dev/null; then
  grep -i -E '\[strata\] [45][0-9]{2}\b|Traceback|ERROR|error:|refused|restart|had stopped|non-?finite|repeated|said nothing|has waited|cancel=True|Failed with result|Main process exited|code=killed|Scheduled restart' "$TMP/journal" |
    redact > "$R/$TAG-journal-errors.txt"
  journal_errors=$(wc -l < "$R/$TAG-journal-errors.txt")
  done_lines=$(grep -c '\[strata\] done:' "$TMP/journal")
  done_finish=$(grep '\[strata\] done:' "$TMP/journal" | sed -E 's/.*\(([a-z_]+), cancel=([A-Za-z]+)\).*/\1,cancel=\2/' | sort | uniq -c |
                awk '{printf "%s%s %s", (NR > 1 ? ", " : ""), $2, $1}')
else
  : > "$R/$TAG-journal-errors.txt"; journal_errors=unknown; done_lines=unknown; done_finish=
fi
if sudo -n journalctl -k --since "$t_start" --no-pager -q > "$TMP/kernel" 2>/dev/null; then
  grep -i -E 'NVRM: Xid|out of memory|oom-kill|oom_reaper|segfault|general protection|BUG:|soft lockup|hung_task' "$TMP/kernel" |
    redact > "$R/$TAG-kernel-errors.txt"
  kernel_errors=$(wc -l < "$R/$TAG-kernel-errors.txt")
else
  : > "$R/$TAG-kernel-errors.txt"; kernel_errors=unknown
fi
pid2=$(engine "$(systemctl show -p MainPID --value strata-q3.service)")
same=NO; [ -n "$pid2" ] && [ "$pid2" = "$pid" ] && [ "$(start_ticks "$pid2")" = "$ticks0" ] && same=yes
{
  echo "tag $TAG  ended at $(date '+%F %T')  run_profile exit $rc  interrupted $interrupted"
  echo "engine pid ${pid2:-none}  start ticks ${pid2:+$(start_ticks "$pid2")}"
  echo "same engine as before: $same"
  echo "engine log error lines during the run: $engine_errors"
  echo "journal: $([ "$journal_errors" = unknown ] && echo unreadable || echo system); error lines during the run: $journal_errors"
  echo "journal done lines during the run: $done_lines (${done_finish})"
  echo "kernel error lines during the run: $kernel_errors"
  # grep -c prints the count and exits 1 when it is 0: read the count, not the status
  if [ -r "$R/$TAG-perf.log" ]; then echo "perf log Traceback lines: $(grep -c 'Traceback' "$R/$TAG-perf.log")"
  else echo "perf log Traceback lines: unknown (no perf log)"; fi
  echo "gpu ecc (index, corrected, uncorrected): $(ecc)"
} | redact > "$R/$TAG-after.txt"
cat "$R/$TAG-after.txt"
[ $interrupted = yes ] && exit 130
exit $rc
