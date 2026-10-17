#!/usr/bin/env bash
# Where the PLE hint's time goes, from the engine's own threads only: kprobes on the entry and return of do_madvise,
# zpl_fadvise and arc_wait_for_eviction (ZFS; the ARC allocation path's wait for eviction) or generic_fadvise (FS=ext4),
# plus sched:sched_switch, recorded with `perf record -p <engine pid>` while ple_hint_probe.py sends fresh prompts.
# mem_parse.py pairs entry / return per thread: time per function, the eviction waits nested inside zpl_fadvise, and
# how the threads left the CPU while inside the hint (asleep or preempted).  A separate pass: the probes slow the hint.
# Usage: bench/mem_hint.sh <tag> <zfs|ext4> [offset ...]     (sudo; a tag is used once)
set -u
TAG=${1:?tag}; FS=${2:?zfs|ext4}; shift 2
OFFS=(); [ $# -gt 0 ] && OFFS=(--offsets "$@")
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
OUT=$R/$TAG-mem.txt
DATA=$R/$TAG-mem.perf.data
[ -e "$OUT" ] && { echo "refusing: $OUT exists (pick a new tag)"; exit 2; }
PID=$(pgrep -f '[e]ngine-cuda12/strata --serve' | head -1)
[ -n "$PID" ] || { echo "no engine process"; exit 1; }
T=/sys/kernel/tracing
if [ "$FS" = zfs ]; then
  PROBES="p:strata/madv_e do_madvise
r32:strata/madv_r do_madvise
p:strata/zplf_e zfs:zpl_fadvise
r32:strata/zplf_r zfs:zpl_fadvise
p:strata/awfe_e zfs:arc_wait_for_eviction
r32:strata/awfe_r zfs:arc_wait_for_eviction"
else
  PROBES="p:strata/madv_e do_madvise
r32:strata/madv_r do_madvise
p:strata/genf_e generic_fadvise
r32:strata/genf_r generic_fadvise"
fi
clean() {   # remove this script's kprobes (strata/*), if any
  local del
  del=$(sudo sed -n 's/^[pr][0-9]*:strata\/\([a-z_]*\) .*/-:strata\/\1/p' $T/kprobe_events)
  [ -n "$del" ] && printf '%s\n' "$del" | sudo sh -c "cat >> $T/kprobe_events"   # (tee -a gets EINVAL here; >> works)
  return 0
}
trap clean EXIT
clean
printf '%s\n' "$PROBES" | sudo sh -c "cat >> $T/kprobe_events" || { echo "kprobe set-up failed"; exit 1; }
sudo perf record -o "$DATA" -m 4096 -p "$PID" -e 'strata:*' -e sched:sched_switch 2> "$R/$TAG-mem-perf-record.err" &
PERF=$!
sleep 2
{
  echo "tag $TAG  fs $FS  engine pid $PID  exe $(readlink -f /proc/$PID/exe)  at $(date '+%F %T')"
  echo "engine args: $(tr '\0' ' ' < /proc/$PID/cmdline | grep -o -- '--ple-io [a-z]*\|--ple-gguf [^ ]*\|--no-ple-prefetch' | tr '\n' ' ')"
  python3 "$D/bench/ple_hint_probe.py" --tag "$TAG-mem" --mode prompt --json "$R/ple_hint_probe.jsonl" "${OFFS[@]}"
} > "$OUT" 2>&1
PROBE_RC=$?
sudo pkill -INT -P "$PERF" 2>/dev/null
for i in $(seq 1 120); do [ -d "/proc/$PERF" ] || break; sleep 1; done
[ -d "/proc/$PERF" ] && { echo "perf did not stop on SIGINT; killed" >> "$OUT"; sudo pkill -KILL -P "$PERF"; }
wait "$PERF" 2>/dev/null
{
  echo "--- probe exit $PROBE_RC"
  echo "--- kprobe_profile (hits, misses; all processes)"; sudo grep -E '^ +(madv|zplf|awfe|genf)_[er] ' $T/kprobe_profile
  echo "--- perf record stderr"; cat "$R/$TAG-mem-perf-record.err"
  echo "--- perf script | mem_parse.py"
  sudo perf script -i "$DATA" -F tid,time,event,trace 2> "$R/$TAG-mem-perf-script.err" | python3 "$D/bench/mem_parse.py"
  echo "--- perf script stderr (lost events show here)"; head -5 "$R/$TAG-mem-perf-script.err"
} >> "$OUT" 2>&1
sudo chown thc1006:thc1006 "$DATA"   # hundreds of MB, kept for re-parsing; the parsed result is in $OUT
cat "$OUT"
