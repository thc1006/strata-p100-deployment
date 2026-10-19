#!/usr/bin/env bash
# One run of the IQ3_XXS / IQ3_S A/B (docs/decisions/0004-iq3-s.md): the standard perf set (bench/run_profile.sh)
# with serve_load's prompts fixed, so that every run decodes the same text, and a record of what the service ran
# before and after it and of every line that may tell of a fault during it.  bench/ab_model_summary.py judges them.
# With "robustness" it runs the robustness set (bench/run_robustness.sh) instead: the robustness run of criterion 1.
# It starts and stops nothing: the restarts between runs are done by hand, in the decision record's order.
# It refuses to measure when it could not record the run completely, when the service became ready (the model
# loaded) less than SETTLE_S (default 300) seconds ago, when a request is in flight, or when what protocol step 1
# deploys is not in place, as the summary would find afterwards: the ZFS ARC cap of systemd/zfs-arc-max.service, the
# two RAM floors of tools/configure_strata_q3.py in the engine's environment and arguments (both read from this
# repository, as the summary reads them), and the host units of config/host-units.conf.
#   <tag>-arm.txt     before: the checkout the service runs from (its commit, the sha256 of its code and of this perf
#                     set), the set that runs, profile.conf, the config's sha256 (whole, and without the model's own
#                     fields), the shared settings, server.py and serve_load.py, serve_load's nonce, the engine's pid,
#                     start time, exe, args, environment and placement lines at its load (the expert complement
#                     page-locked in RAM included), how long the service has been ready, /health, the requests
#                     /metrics has counted since the service started and the conversations parked (other clients'
#                     work before the run), the ZFS ARC cap, MemAvailable, the host units check and every GPU's ECC
#                     error counters
#   <tag>-after.txt   after: the set's exit status, whether it was interrupted, whether the same engine answered,
#                     whether the service was idle at the end, the requests /metrics counted during the run
#                     (run_profile.sh sends 98), the journal's "[strata] done:" lines by finish, the number of lines in
#                     each file below, the engine's concurrent-read lines (STRATA_PIPED_CONCURRENT: one per stretch of
#                     a prompt read on a thread of its own, the ones with slots decoding meanwhile counted apart) and
#                     the reason it gave, if any, for keeping such reads off, the set's log's Traceback lines, the ECC
#                     counters
#   <tag>-engine-errors.txt, <tag>-journal-errors.txt, <tag>-kernel-errors.txt   the lines of the engine log, of the
#                     service journal (systemd's own lines included) and of the kernel log that may tell of a fault,
#                     from exactly the run (journal cursors); the summary sorts them into faults and other traffic
# Every name of this host (hostname, -f, -A and the first label of each), in any case, is written as <host>.
# Usage: bench/ab_model_run.sh <tag> <arm letter> [robustness]
# (Test hook: AB_ARC_FILE replaces /sys/module/zfs/parameters/zfs_arc_max.)
set -u -o pipefail
TAG=${1:?tag}; ARM=${2:?arm letter}
case "${3:-}" in
  '') SET=run_profile.sh; SETLOG=perf.log ;;
  robustness) SET=run_robustness.sh; SETLOG=robustness.log ;;
  *) echo "ab_model_run: usage: bench/ab_model_run.sh <tag> <arm letter> [robustness]" >&2; exit 2 ;;
esac
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
URL=http://127.0.0.1:8080
UNIT=strata-q3.service
STRATA=${STRATA_DIR:-$HOME/dev/strata}
CFG=$STRATA/strata-iq3_xxs.json
SHARED=$STRATA/strata-iq3_xxs.shared-settings.json
SETTLE_S=${SETTLE_S:-300}
ARC_FILE=${AB_ARC_FILE:-/sys/module/zfs/parameters/zfs_arc_max}
export SERVE_LOAD_NONCE=${SERVE_LOAD_NONCE:-ab-0004}   # run_profile.sh passes it to serve_load: the same prompts in every run
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT        # raw journal copies never go near results/
say() { echo "ab_model_run: $*"; }
refuse() { say "refusing: $*"; exit 1; }
for f in arm.txt after.txt engine-errors.txt journal-errors.txt kernel-errors.txt perf.log monitor.csv serve_load.json \
         four_user.jsonl robustness.log robust-monitor.csv robust-monitor-all.json overload.json needles.json; do
  [ -e "$R/$TAG-$f" ] && { say "refusing: $R/$TAG-$f exists (pick a new tag)"; exit 2; }
done
# the sets append their records to stress.jsonl and latency.jsonl: the tag must not be there yet
python3 - "$R" "$TAG" <<'EOF' || { say "refusing: the tag $TAG is in results/stress.jsonl or latency.jsonl, or they cannot be read"; exit 2; }
import json, sys
from pathlib import Path
for name in ("stress.jsonl", "latency.jsonl"):
    p = Path(sys.argv[1]) / name
    if p.exists() and any(json.loads(l).get("tag") == sys.argv[2] for l in p.read_text().splitlines() if l.strip()):
        sys.exit(1)
EOF

# every name of this host and the first label of each (4 characters or more), longest first, so that a name that
# holds another is replaced whole; in any case
names=$({ hostname; hostname -f; hostname -A; } 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sort -u)
[ -n "$names" ] || refuse "no host name to redact"
names=$({ printf '%s\n' "$names"; printf '%s\n' "$names" | cut -d. -f1 | awk 'length >= 4'; } | sort -u |
        awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)
redact_args=(); while IFS= read -r n; do redact_args+=(-e "s/$(printf '%s' "$n" | sed 's/[.[\*^$/]/\\&/g')/<host>/gI"); done <<< "$names"
redact() { sed "${redact_args[@]}"; }

# the service's own engine: the child of strata-q3's main process, as scripts/strata-health.sh finds it
main=$(systemctl show -p MainPID --value $UNIT)
[[ "$main" =~ ^[1-9][0-9]*$ ]] || refuse "$UNIT has no main process"
[ "$(systemctl show -p ActiveState --value $UNIT)" = active ] || refuse "$UNIT is not active"
engine() { pgrep -P "$1" -f "^$STRATA/engine-cuda12/strata --serve" | head -1; }
pid=$(engine "$main")
[ -n "$pid" ] && [ "$(readlink -f "/proc/$pid/exe")" = "$STRATA/engine-cuda12/strata" ] ||
  refuse "no engine under $UNIT's main process $main"
start_ticks() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }
ticks0=$(start_ticks "$pid")
[[ "$ticks0" =~ ^[0-9]+$ ]] || refuse "cannot read the engine's start time"
engine_age=$(( $(awk '{print int($1)}' /proc/uptime) - ticks0 / $(getconf CLK_TCK) ))
# the settle time counts from when the unit became active: strata-wait-ready.sh returns once the model is loaded.
# An engine younger than that was started again by server.py after the unit became ready: not a fresh start either.
active_us=$(systemctl show -p ActiveEnterTimestampMonotonic --value $UNIT)
now_us=$(python3 -c 'import time; print(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000)')
[[ "$active_us" =~ ^[1-9][0-9]*$ ]] && [[ "$now_us" =~ ^[0-9]+$ ]] || refuse "cannot tell when $UNIT became active"
ready=$(( (now_us - active_us) / 1000000 ))
[ "$ready" -ge "$SETTLE_S" ] || refuse "$UNIT became ready $ready s ago (wait until it has been ready $SETTLE_S s)"
[ $((engine_age + 5)) -ge "$ready" ] || refuse "the engine ($engine_age s) is younger than the service's ready time ($ready s)"
LOG=$(readlink -f "/proc/$pid/fd/2")
[ -r "$LOG" ] || refuse "cannot read the engine's log ($LOG)"

# the checkout the unit runs (its ExecStartPre is <checkout>/scripts/strata-preflight.sh), whichever this copy is
pre=$(systemctl show -p ExecStartPre --value $UNIT | grep -o 'path=[^ ;]*' | head -1); pre=${pre#path=}
[ -n "$pre" ] && CHECKOUT=$(cd "$(dirname "$pre")/.." 2>/dev/null && pwd) || refuse "cannot find the checkout $UNIT runs"
git -C "$CHECKOUT" rev-parse --git-dir > /dev/null 2>&1 || refuse "$CHECKOUT is not a git checkout"
# the sha256 of the working-tree content (and names) of the tracked files under the given directories, profile.conf
# aside: results committed between runs do not change it, any change to the code does
code_sha() {
  local dir=$1 out; shift
  out=$(cd "$dir" && git ls-files -z -- "$@" | grep -zv '^config/profile\.conf$' | LC_ALL=C sort -z |
        xargs -0 -r sha256sum -- | sha256sum) || { echo unknown; return; }
  echo "${out:0:16}"
}
# the config's sha256 without the fields the model sets (its files, cuts, tokenizer and name)
cfg_sha() {
  python3 - "$1" <<'EOF' 2>/dev/null || echo unknown
import hashlib, json, sys
c = json.load(open(sys.argv[1]))
args = list(c["args"])
for flag in ("--pack", "--native", "--ple-gguf", "--layer-split"):
    while flag in args:
        i = args.index(flag)
        del args[i:i + 2]
c["args"] = args
for k in ("tokenizer", "model_name", "layer_split"):
    c.pop(k, None)
print(hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16])
EOF
}
sha() { if [ -e "$1" ]; then sha256sum < "$1" | cut -c1-16; else echo absent; fi; }
ecc() { nvidia-smi --query-gpu=index,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv,noheader | tr '\n' ';'; }
# "<state> <running> <queued> <requests counted since the start> <conversations parked> <their bytes>" from /metrics
metrics() {
  curl -fsS -m 10 "$URL/metrics" | python3 -c 'import json, sys
d = json.load(sys.stdin); l = d["live"]; c = d.get("conversation_cache") or {}
print(l.get("state"), l.get("running"), l.get("queued"), d["totals"]["requests"], c.get("parked", "unknown"), c.get("bytes", "unknown"))'
}
cursor() { sudo -n journalctl -n 1 --show-cursor --no-pager -q 2>/dev/null | sed -n 's/^-- cursor: //p'; }

health=$(curl -fsS -m 10 "$URL/health") || refuse "/health does not answer"
grep -q '"loaded": true' <<< "$health" || refuse "the model is not loaded"
read -r state0 running0 queued0 total0 parked0 pbytes0 <<< "$(metrics)"
[ "${state0:-}" = idle ] && [ "${running0:-}" = 0 ] && [ "${queued0:-}" = 0 ] && [[ "${total0:-}" =~ ^[0-9]+$ ]] ||
  refuse "the service is not idle, or /metrics does not say (${state0:-no answer} ${running0:-} ${queued0:-} ${total0:-})"
sudo -n journalctl -n 1 --no-pager -q > /dev/null 2>&1 || refuse "the system journal needs sudo -n"
# what protocol step 1 deploys, as the summary checks it afterwards: a run without it would be measured for nothing
"$CHECKOUT/scripts/host-units.sh" check > /dev/null 2>&1 ||
  refuse "the host units are not as config/host-units.conf sets them ($CHECKOUT/scripts/host-units.sh check; its user units need this user's session bus, XDG_RUNTIME_DIR)"
hu=ok
arc_want=$(grep -o 'echo [0-9]* > /sys/module/zfs/parameters/zfs_arc_max' "$D/systemd/zfs-arc-max.service" 2>/dev/null | cut -d' ' -f2)
arc_have=$(cat "$ARC_FILE" 2>/dev/null)
[[ "$arc_want" =~ ^[0-9]+$ ]] && [ "$arc_have" = "$arc_want" ] ||
  refuse "the ZFS ARC cap is ${arc_have:-unreadable}, $D/systemd/zfs-arc-max.service writes ${arc_want:-?}"
floors=$(python3 -B - "$D/tools/configure_strata_q3.py" 2>/dev/null <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("configure_strata_q3", sys.argv[1])
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
print(tool.ENV_DEFAULTS["STRATA_RESIDENT_HEADROOM_GIB"], tool.PARK_MIN_FREE_MIB)
EOF
)
read -r head_want park_want <<< "$floors"
head_have=$(tr '\0' '\n' 2>/dev/null < "/proc/$pid/environ" | sed -n 's/^STRATA_RESIDENT_HEADROOM_GIB=//p' | head -1)
park_have=$(tr '\0' '\n' 2>/dev/null < "/proc/$pid/cmdline" | sed -n '/^--conversation-cache-min-free-mib$/{n;p;q}')
[ -n "${head_want:-}" ] && [ -n "${park_want:-}" ] && [ "$head_have" = "$head_want" ] && [ "$park_have" = "$park_want" ] ||
  refuse "the engine runs with STRATA_RESIDENT_HEADROOM_GIB ${head_have:-unset} and --conversation-cache-min-free-mib ${park_have:-unset}, $D/tools/configure_strata_q3.py sets ${head_want:-?} and ${park_want:-?}"
# the engine's placement decisions at this start: from its "engine started" line to the first request
placement=$(awk '/^\[strata\] .*engine started: / { k = 0; stop = 0; on = 1; next }
                 on && /^strata serve: prompt [0-9]+ tokens/ { stop = 1 }
                 on && !stop && /memory guard|pcie_frac|expert cache .*slots|experts resident|runs layers|session is up|resident RAM mode|cache complement ready/ { keep[k++] = $0 }
                 END { for (i = 0; i < k; i++) print keep[i] }' "$LOG")
[ -n "$placement" ] || refuse "no placement lines for this start in $LOG"
commit=$(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null) || commit=unknown
changed=$(git -C "$CHECKOUT" status --porcelain --untracked-files=no 2>/dev/null | awk '{print $NF}' | tr '\n' ' ') || changed=unknown
profile=$(grep -v '^[[:space:]]*#' "$CHECKOUT/config/profile.conf" 2>/dev/null | grep -v '^[[:space:]]*$' | tr '\n' ' ')
cur=$(cursor)
[ -n "$cur" ] || refuse "cannot read the journal's cursor"

# from the arm record on, an interrupt (INT, TERM, HUP) no longer ends this script: it stops the set (or keeps it
# from starting), and the after record is still written
stop_perf_set() { [ -n "$child" ] && kill -TERM -- -"$child" 2>/dev/null; }
interrupted=no; child=
trap 'interrupted=yes; stop_perf_set' INT TERM HUP
start_line=$(wc -l < "$LOG"); start_size=$(stat -c %s "$LOG"); start_inode=$(stat -c %i "$LOG")
t_start=$(date '+%F %T')
ecc0=$(ecc)
{
  echo "tag $TAG  arm $ARM  at $t_start"
  echo "service checkout $CHECKOUT  commit $commit  tracked files changed: ${changed:-none}"
  echo "deployment code sha256 $(code_sha "$CHECKOUT" scripts tools systemd bench config)  (scripts tools systemd bench config, profile.conf aside)"
  echo "perf set sha256 $(code_sha "$D" bench)  ($D/bench)"
  echo "set: bench/$SET"
  echo "profile.conf: ${profile:-unreadable}"
  echo "config $CFG sha256 $(sha "$CFG")  without the model's fields $(cfg_sha "$CFG")"
  echo "shared settings $SHARED sha256 $(sha "$SHARED")"
  echo "server.py sha256 $(sha "$STRATA/serve/server.py")  serve_load.py sha256 $(sha "$STRATA/tools/serve_load.py")"
  echo "serve_load nonce $SERVE_LOAD_NONCE"
  echo "engine pid $pid exe $(readlink -f /proc/$pid/exe) sha256 $(sha256sum < /proc/$pid/exe | cut -c1-16)"
  echo "engine start ticks $ticks0  engine age $engine_age s"
  echo "service ready for $ready s  settle $SETTLE_S s"
  echo "engine args: $(tr '\0' ' ' < /proc/$pid/cmdline)"
  echo "engine env: $(tr '\0' '\n' < /proc/$pid/environ | grep -E '^(STRATA|CUDA)_' | sort | tr '\n' ' ')"
  echo "engine log: $LOG"
  echo "engine started: $(ps -o lstart= -p "$pid")"
  printf '%s\n' "$placement" | sed 's/^/load: /'
  echo "health: $health"
  echo "metrics requests before the run: $total0"
  echo "conversation cache before the run: parked ${parked0:-unknown} bytes ${pbytes0:-unknown}"
  echo "version line: $(grep -a -o 'session is up (engine [0-9.]*)' "$LOG" | tail -1)"
  echo "zfs_arc_max $arc_have  MemAvailable $(awk '/MemAvailable/{print $2}' /proc/meminfo) kB"
  echo "host units: $hu"
  echo "gpu ecc (index, corrected, uncorrected): $ecc0"
} | redact > "$R/$TAG-arm.txt"
for want in '^tag ' '^deployment code sha256 ' '^perf set sha256 ' '^set: ' '^engine args: ' '^health: ' '^engine start ticks ' \
            '^service ready for ' '^serve_load nonce ' '^metrics requests before the run: ' '^load: ' '^gpu ecc '; do
  grep -q "$want" "$R/$TAG-arm.txt" || refuse "the arm record lacks ${want#^}"
done

# the set in a process group of its own, so that an interrupt stops all of it
rc=none
if [ $interrupted = no ]; then
  setsid "$D/bench/$SET" "$TAG" &
  child=$!
  [ $interrupted = yes ] && stop_perf_set        # an interrupt that came before $! was known
  while kill -0 "$child" 2>/dev/null; do wait "$child"; done
  wait "$child" 2>/dev/null; rc=$?
fi
trap - INT TERM HUP

# what happened during the run.  /metrics first: its count is exact in time, the journal's window ends a moment later
read -r state1 running1 queued1 total1 _ <<< "$(metrics)"
if [ -z "${state1:-}" ]; then idle_end=unknown
elif [ "$state1" = idle ] && [ "${running1:-}" = 0 ] && [ "${queued1:-}" = 0 ]; then idle_end=yes
else idle_end=no; fi
counted=unknown; [[ "${total1:-}" =~ ^[0-9]+$ ]] && counted=$((total1 - total0))
# the graph captures log "upload no error, sync no error": not an error
patterns='error|warn|refused|no progress|watchdog|out of memory|restart|fail|abort|timeout|non-?finite|nonfinite|repeated|said nothing'
if [ -r "$LOG" ] && [ "$(stat -c %i "$LOG")" = "$start_inode" ] && [ "$(stat -c %s "$LOG")" -ge "$start_size" ]; then
  tail -n +"$((start_line + 1))" "$LOG" | grep -a -i -E "$patterns" | grep -v 'upload no error, sync no error' | redact > "$R/$TAG-engine-errors.txt"
  engine_errors=$(wc -l < "$R/$TAG-engine-errors.txt")
  read -r conc_lines conc_beside <<< "$(tail -n +"$((start_line + 1))" "$LOG" |
    awk '/^strata batch: read [0-9]+ tokens on a thread of its own/ { n++; if (/the slots decoded [1-9][0-9]* rows meanwhile/) b++ }
         END { print n + 0, b + 0 }')"
else
  : > "$R/$TAG-engine-errors.txt"; engine_errors="unknown (the log was replaced, truncated or removed during the run)"
  conc_lines=unknown; conc_beside=unknown
fi
# the concurrent prompt read (STRATA_PIPED_CONCURRENT, decision 0005): the engine says once, at its first prompt read
# (the warm-up's, perhaps), when its configuration keeps it off; from this engine's start on
conc_off=$(awk '/^\[strata\] .*engine started: / { off = "" }
                /^strata batch: STRATA_PIPED_CONCURRENT is off beside / && off == "" {
                  off = $0; sub(/^strata batch: STRATA_PIPED_CONCURRENT is off beside /, "", off) }
                END { print (off == "" ? "no such line" : off) }' "$LOG" 2>/dev/null)
if sudo -n journalctl -u $UNIT --after-cursor "$cur" --no-pager -q > "$TMP/journal" 2>/dev/null; then
  grep -a -i -E '\[strata\] [45][0-9]{2}\b|Traceback|ERROR|error:|refused|restart|had stopped|non-?finite|repeated|said nothing|marked busy with no request|cancel=True|Failed with result|Main process exited|code=killed|Scheduled restart|Stopping|Stopped|Starting|Started' "$TMP/journal" |
    redact > "$R/$TAG-journal-errors.txt"
  journal_errors=$(wc -l < "$R/$TAG-journal-errors.txt")
  done_lines=$(grep -c '\[strata\] done:' "$TMP/journal")
  done_finish=$(grep '\[strata\] done:' "$TMP/journal" | sed -E 's/.*\(([a-z_]+), cancel=([A-Za-z]+)\).*/\1,cancel=\2/' | sort | uniq -c |
                awk '{printf "%s%s %s", (NR > 1 ? ", " : ""), $2, $1}')
else
  : > "$R/$TAG-journal-errors.txt"; journal_errors=unknown; done_lines=unknown; done_finish=
fi
if sudo -n journalctl -k --after-cursor "$cur" --no-pager -q > "$TMP/kernel" 2>/dev/null; then
  grep -a -i -E 'NVRM: Xid|out of memory|oom-kill|oom_reaper|segfault|general protection|BUG:|soft lockup|hung_task|blocked for more than' "$TMP/kernel" |
    redact > "$R/$TAG-kernel-errors.txt"
  kernel_errors=$(wc -l < "$R/$TAG-kernel-errors.txt")
else
  : > "$R/$TAG-kernel-errors.txt"; kernel_errors=unknown
fi
pid2=$(engine "$(systemctl show -p MainPID --value $UNIT)")
same=NO; [ -n "$pid2" ] && [ "$pid2" = "$pid" ] && [ "$(start_ticks "$pid2")" = "$ticks0" ] && same=yes
{
  echo "tag $TAG  ended at $(date '+%F %T')  ${SET%.sh} exit $rc  interrupted $interrupted"
  echo "engine pid ${pid2:-none}  start ticks ${pid2:+$(start_ticks "$pid2")}"
  echo "same engine as before: $same"
  echo "idle at the end: $idle_end"
  echo "metrics requests during the run: $counted"
  echo "engine log lines during the run: $engine_errors"
  echo "journal: $([ "$journal_errors" = unknown ] && echo unreadable || echo system); lines during the run: $journal_errors"
  echo "journal done lines during the run: $done_lines (${done_finish})"
  echo "kernel lines during the run: $kernel_errors"
  echo "concurrent read lines during the run: $conc_lines, with slots decoding: $conc_beside"
  echo "concurrent read off beside: ${conc_off:-unknown}"
  # grep -c prints the count and exits 1 when it is 0: read the count, not the status
  if [ -r "$R/$TAG-$SETLOG" ]; then echo "perf log Traceback lines: $(grep -c 'Traceback' "$R/$TAG-$SETLOG")"
  else echo "perf log Traceback lines: unknown (no $TAG-$SETLOG)"; fi
  echo "gpu ecc (index, corrected, uncorrected): $(ecc)"
} | redact > "$R/$TAG-after.txt"
cat "$R/$TAG-after.txt"
[ $interrupted = yes ] && exit 130
exit $rc
