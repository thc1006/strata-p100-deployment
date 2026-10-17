#!/usr/bin/env bash
# ple_hint_probe.py under perf and ftrace: the engine's madvise / fadvise64 calls and context switches (perf, this
# engine's threads only) and the kernel functions of the hint's path on all CPUs (ftrace's function profiler):
#   do_madvise -> vfs_fadvise -> zpl_fadvise -> dmu_prefetch -> dbuf_prefetch(_impl) -> arc_read -> arc_get_data_impl
#   -> arc_wait_for_eviction (ZFS; the last one sleeps while the ARC is too far over its target), or
#   do_madvise -> vfs_fadvise -> generic_fadvise -> force_page_cache_ra -> page_cache_ra_unbounded (ext4 and others).
# SLEEP=1 (default) counts the time a thread was scheduled out inside a function, SLEEP=0 only its time on a CPU; the
# difference is time spent waiting (eviction, locks, a busy CPU).  arc_read and dbuf_prefetch also serve ZFS's own
# reads and read-ahead, so only the fadvise functions count hints.
# Usage: bench/traced_hint.sh <tag> <prompt|decode> [offset ...]      (sudo; a tag is used once)
set -u
TAG=${1:?tag}; MODE=${2:?mode}; shift 2
SLEEP=${SLEEP:-1}
OFFS=(); [ $# -gt 0 ] && OFFS=(--offsets "$@")
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
OUT=$R/$TAG-trace.txt
[ -e "$OUT" ] && { echo "refusing: $OUT exists (pick a new tag)"; exit 2; }
PID=$(pgrep -f '[e]ngine-cuda12/strata --serve' | head -1)
[ -n "$PID" ] || { echo "no engine process"; exit 1; }
T=/sys/kernel/tracing
FUNCS="do_madvise vfs_fadvise zpl_fadvise dmu_prefetch dbuf_prefetch dbuf_prefetch_impl arc_read arc_get_data_impl* arc_wait_for_eviction generic_fadvise force_page_cache_ra page_cache_ra_unbounded"
off() { sudo sh -c "echo 0 > $T/function_profile_enabled; echo > $T/set_ftrace_filter; echo 1 > $T/options/sleep-time"; }
trap off EXIT
sudo sh -c "echo 0 > $T/function_profile_enabled && echo $SLEEP > $T/options/sleep-time && echo '$FUNCS' > $T/set_ftrace_filter && echo 1 > $T/function_profile_enabled" ||
  { echo "ftrace profiler unavailable"; exit 1; }
sudo perf stat -x, -e syscalls:sys_enter_madvise,syscalls:sys_enter_fadvise64,context-switches,task-clock -p "$PID" \
  -o "$R/$TAG-perf-stat.csv" &
PERF=$!
sleep 1
{
  echo "tag $TAG  mode $MODE  sleep-time $SLEEP  engine pid $PID  exe $(readlink -f /proc/$PID/exe)  at $(date '+%F %T')"
  echo "engine args: $(tr '\0' ' ' < /proc/$PID/cmdline | grep -o -- '--ple-io [a-z]*\|--ple-gguf [^ ]*\|--no-ple-prefetch' | tr '\n' ' ')"
  python3 "$D/bench/ple_hint_probe.py" --tag "$TAG" --mode "$MODE" --json "$R/ple_hint_probe.jsonl" "${OFFS[@]}"
} > "$OUT" 2>&1
# SIGINT to perf itself (the child of sudo): sudo does not always relay a signal from this script's process group
sudo pkill -INT -P "$PERF" 2>/dev/null
for i in $(seq 1 30); do [ -d "/proc/$PERF" ] || break; sleep 1; done
[ -d "/proc/$PERF" ] && { echo "perf did not stop on SIGINT; killed (no counts)" >> "$OUT"; sudo pkill -KILL -P "$PERF"; }
wait "$PERF" 2>/dev/null
sudo sh -c "echo 0 > $T/function_profile_enabled"
{
  echo "--- engine during the probe (perf stat -p $PID)"
  grep -v '^#' "$R/$TAG-perf-stat.csv" | awk -F, 'NF > 2 {printf "%-34s %s\n", $3, $1}'
  echo "--- kernel functions, all CPUs (ftrace function profiler; time includes callees; sleep-time $SLEEP)"
  sudo sh -c "cat $T/trace_stat/function*" | awk '
    $1 != "Function" && $1 !~ /^-/ && NF >= 4 { hits[$1] += $2; us[$1] += $3 }
    END { for (f in hits) printf "%-32s calls %10d  total %10.1f ms  mean %.2f us\n", f, hits[f], us[f] / 1000, (hits[f] ? us[f] / hits[f] : 0) }' | sort
} >> "$OUT"
cat "$OUT"
