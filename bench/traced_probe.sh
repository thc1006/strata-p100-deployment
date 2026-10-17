#!/usr/bin/env bash
# The PLE probe (ple_prefetch_probe.py) with the kernel's count of the hint: the engine's madvise / fadvise64 system
# calls (perf, this engine's process only) and how often ZFS's fadvise path ran (ftrace's function profiler,
# system-wide): madvise(MADV_WILLNEED) on a file mapping -> vfs_fadvise -> zpl_fadvise -> dmu_prefetch -> dbuf_prefetch.
# The profiler adds a little time to every traced call, so prompt speeds come from an untraced probe; this run gives
# the counts.  dbuf_prefetch is also ZFS's own read-ahead (dmu_zfetch), so only zpl_fadvise / dmu_prefetch count hints.
# Usage: bench/traced_probe.sh <tag> [offset ...]     (sudo; a tag is used once)
set -u
TAG=${1:?tag}; shift
OFFS=(); [ $# -gt 0 ] && OFFS=(--offsets "$@")
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
OUT=$R/$TAG-trace.txt
[ -e "$OUT" ] && { echo "refusing: $OUT exists (pick a new tag)"; exit 2; }
PID=$(pgrep -f '[e]ngine-cuda12/strata --serve' | head -1)
[ -n "$PID" ] || { echo "no engine process"; exit 1; }
T=/sys/kernel/tracing
FUNCS="zpl_fadvise dmu_prefetch dbuf_prefetch vfs_fadvise generic_fadvise"
off() { sudo sh -c "echo 0 > $T/function_profile_enabled; echo > $T/set_ftrace_filter"; }
trap off EXIT
sudo sh -c "echo 0 > $T/function_profile_enabled && echo '$FUNCS' > $T/set_ftrace_filter && echo 1 > $T/function_profile_enabled" ||
  { echo "ftrace profiler unavailable"; exit 1; }
sudo perf stat -x, -e syscalls:sys_enter_madvise,syscalls:sys_enter_fadvise64 -p "$PID" -o "$R/$TAG-perf-stat.csv" &
PERF=$!
sleep 1
{
  echo "tag $TAG  engine pid $PID  exe $(readlink -f /proc/$PID/exe)  at $(date '+%F %T')"
  echo "engine args: $(tr '\0' ' ' < /proc/$PID/cmdline | grep -o -- '--ple-io [a-z]*\|--no-ple-prefetch' | tr '\n' ' ')"
  python3 "$D/bench/ple_prefetch_probe.py" --tag "$TAG" --json "$R/ple_prefetch_probe.jsonl" "${OFFS[@]}"
} > "$OUT" 2>&1
# SIGINT to perf itself (the child of sudo): sudo does not always relay a signal from this script's process group,
# and perf then counts on until killed (seen once: perf ran 27 min, 26 of them after the probe had ended)
sudo pkill -INT -P "$PERF" 2>/dev/null
for i in $(seq 1 30); do [ -d "/proc/$PERF" ] || break; sleep 1; done     # sudo runs as root: no kill -0 from here
[ -d "/proc/$PERF" ] && { echo "perf did not stop on SIGINT; killed (no counts)" >> "$OUT"; sudo pkill -KILL -P "$PERF"; }
wait "$PERF" 2>/dev/null
sudo sh -c "echo 0 > $T/function_profile_enabled"
{
  echo "--- engine system calls during the probe (perf stat -p $PID)"
  grep -v '^#' "$R/$TAG-perf-stat.csv" | awk -F, 'NF > 2 {printf "%-34s %s\n", $3, $1}'
  echo "--- kernel functions, all CPUs (ftrace function profiler; time includes callees)"
  sudo sh -c "cat $T/trace_stat/function*" | awk '
    $1 != "Function" && $1 !~ /^-/ && NF >= 4 { hits[$1] += $2; us[$1] += $3 }
    END { for (f in hits) printf "%-18s calls %10d  total %10.1f ms  mean %.2f us\n", f, hits[f], us[f] / 1000, (hits[f] ? us[f] / hits[f] : 0) }' | sort
} >> "$OUT"
cat "$OUT"
