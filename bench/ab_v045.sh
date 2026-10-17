#!/usr/bin/env bash
# The PLE-hint A/B for the "skip the per-row hint when the table is locked" patch, on 0.1.45:
#   V  ~/dev/strata-0.1.45         the release (477ac13)
#   L  ~/dev/strata-0.1.45-locked  the release + the patch (src/kernels/ngram.cpp)
# Usage: bench/ab_v045.sh <tag>:<V|L>:<config name>[:sleep0] ...    e.g.  z-off-1:V:cfg-zfs-off
#        a step "ext4-up" copies the PLE shard to an ext4 file system (a loop file) for the cfg-ext4-* configs
# Every run is a fresh start under a transient unit with the deployed unit's limits; the deployed service is started
# again at the end (also after a failure), and the ext4 copy is removed.  Per run: three fresh ~16K prompts and three
# decodes (ple_hint_probe.py), then the same under perf + ftrace (traced_hint.sh); ":sleep0" adds a traced prompt pass
# with ftrace's sleep-time off (CPU time only).
set -u
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
PY=$HOME/dev/strata/.venv/bin/python                     # requirements.txt is the same in 0.1.43.1 and 0.1.45
UNIT=strata-ab-v045
URL=http://127.0.0.1:8080
SHARD=$HOME/dev/Strata-data/models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf
IMG=$HOME/dev/ple-ext4.img
MNT=/mnt/ple-ext4
say() { echo "[$(date +%T)] $*"; }

stop_all() {
  sudo systemctl stop "$UNIT" 2>/dev/null
  sudo systemctl stop strata-q3.service
  for i in $(seq 1 90); do
    busy=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | awk '$1 > 300' | wc -l)
    if ! pgrep -f '[e]ngine-cuda12/strata --serve' >/dev/null && ! ss -ltn '( sport = :8080 )' | grep -q LISTEN &&
       [ "$busy" -eq 0 ]; then
      say "stopped: no engine, :8080 free, all GPUs < 300 MiB; MemAvailable $(awk '/MemAvailable/ {printf "%.1f", $2/1048576}' /proc/meminfo) GiB"
      return 0
    fi
    sleep 2
  done
  say "not free after 180 s"; return 1
}

wait_loaded() {
  for i in $(seq 1 900); do
    curl -fsS -m 5 "$URL/health" 2>/dev/null | grep -q '"loaded": *true' && { say "loaded after $((i * 2)) s"; return 0; }
    [ -n "${1:-}" ] && ! systemctl is-active -q "$1" && { say "$1 is not active"; return 1; }
    sleep 2
  done
  say "not loaded after 30 min"; return 1
}

start_tree() {   # $1 tree $2 config
  sudo systemd-run --unit="$UNIT" --collect --uid=thc1006 --gid=thc1006 \
    -p WorkingDirectory="$1" -p LimitMEMLOCK=infinity -p LimitNOFILE=65536 -p OOMScoreAdjust=500 \
    -p MemorySwapMax=0 -p KillMode=mixed -p TimeoutStopSec=120 -p NoNewPrivileges=yes -p ProtectSystem=full \
    -p PrivateTmp=yes -p Restart=no \
    -E PYTHONUNBUFFERED=1 -E OMP_NUM_THREADS=1 -E OPENBLAS_NUM_THREADS=1 -E MKL_NUM_THREADS=1 \
    "$PY" "$1/serve/server.py" --engine strata --config "$2" --port 8080 &&
  wait_loaded "$UNIT"
}

ext4_up() {   # a 29 GiB ext4 file system in a loop file on the pool, holding a copy of the PLE shard
  [ -e "$IMG" ] && { say "refusing: $IMG exists"; return 1; }
  truncate -s 29G "$IMG" && mkfs.ext4 -F -q -m 0 -E lazy_itable_init=0,lazy_journal_init=0 "$IMG" || return 1
  sudo mkdir -p "$MNT" && sudo mount -o loop,noatime "$IMG" "$MNT" && sudo chown thc1006:thc1006 "$MNT" || return 1
  local t0=$(date +%s)
  dd if="$SHARD" of="$MNT/$(basename "$SHARD")" bs=64M status=none conv=fsync || return 1
  say "ext4 copy done in $(( $(date +%s) - t0 )) s: $(stat -c %s "$MNT/$(basename "$SHARD")") bytes (source $(stat -c %s "$SHARD"));" \
      "loop $(findmnt -n -o SOURCE "$MNT"), $(findmnt -n -o FSTYPE,OPTIONS "$MNT")"
  cmp -n $((1 << 30)) "$SHARD" "$MNT/$(basename "$SHARD")" && say "first GiB identical" || return 1
  cmp -i $(( $(stat -c %s "$SHARD") - (1 << 30) )) "$SHARD" "$MNT/$(basename "$SHARD")" && say "last GiB identical" || return 1
}

ext4_down() {
  findmnt -n "$MNT" >/dev/null 2>&1 && { sudo umount "$MNT" && say "unmounted $MNT"; }
  [ -e "$IMG" ] && rm -f "$IMG" && say "removed $IMG"
  sudo rmdir "$MNT" 2>/dev/null
  return 0
}

record() {   # $1 tag $2 tree $3 config $4 engine log $5 log offset at start
  {
    echo "tag $1  tree $2  at $(date '+%F %T')"
    echo "tree head $(git -C "$2" rev-parse --short=9 HEAD)  src diff: $(git -C "$2" diff --stat HEAD -- src | tail -1)"
    echo "config $3 sha256 $(sha256sum < "$3" | cut -c1-16)"
    pid=$(pgrep -f '[e]ngine-cuda12/strata --serve' | head -1)
    echo "engine pid $pid exe $(readlink -f /proc/$pid/exe) sha256 $(sha256sum < /proc/$pid/exe | cut -c1-16)"
    echo "engine args: $(tr '\0' ' ' < /proc/$pid/cmdline)"
    echo "VmLck: $(awk '/VmLck/ {print $2, $3}' /proc/$pid/status)"
    echo "health: $(curl -s -m 5 $URL/health)"
    echo "--- start-up lines"
    tail -c +$(( $5 + 1 )) "$4" 2>/dev/null | grep -a -E 'session is up|PLE table|layer split|ple-io|prefetch|slots' | head -20
  } > "$R/$1-arm.txt"
}

finish() {
  rc=$?
  say "finishing (exit $rc)"
  if systemctl is-active -q "$UNIT" || ! systemctl is-active -q strata-q3.service; then
    stop_all
  fi
  ext4_down
  systemctl is-active -q strata-q3.service || { sudo systemctl start strata-q3.service; wait_loaded strata-q3.service; }
  say "strata-q3: $(systemctl is-active strata-q3.service)"
}
trap finish EXIT

for step in "$@"; do
  if [ "$step" = ext4-up ]; then
    say "=== ext4-up"; stop_all || exit 1; ext4_up || { say "ext4 set-up failed"; exit 1; }; continue
  fi
  IFS=: read -r tag which cfgname extra <<< "$step"
  case $which in
    V) TREE=$HOME/dev/strata-0.1.45 ;;
    L) TREE=$HOME/dev/strata-0.1.45-locked ;;
    *) say "unknown tree $which"; exit 2 ;;
  esac
  CFG=$TREE/$cfgname.json; LOG=$TREE/$cfgname.log
  [ -e "$R/$tag-arm.txt" ] && { say "refusing: tag $tag used"; exit 2; }
  say "=== $tag ($which $cfgname${extra:+ $extra})"
  stop_all || exit 1
  OFF=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  start_tree "$TREE" "$CFG" || exit 1
  record "$tag" "$TREE" "$CFG" "$LOG" "$OFF"
  python3 "$D/bench/ple_hint_probe.py" --tag "$tag-p" --mode prompt --json "$R/ple_hint_probe.jsonl" > "$R/$tag-p.txt" 2>&1
  python3 "$D/bench/ple_hint_probe.py" --tag "$tag-d" --mode decode --json "$R/ple_hint_probe.jsonl" > "$R/$tag-d.txt" 2>&1
  "$D/bench/traced_hint.sh" "$tag-tp" prompt 555555 444444 111111 > /dev/null 2>&1 || say "traced prompt $tag failed"
  "$D/bench/traced_hint.sh" "$tag-td" decode 135791 975310 > /dev/null 2>&1 || say "traced decode $tag failed"
  [ "${extra:-}" = sleep0 ] && { SLEEP=0 "$D/bench/traced_hint.sh" "$tag-tp0" prompt 666666 888888 999999 > /dev/null 2>&1 ||
                                 say "traced prompt (sleep-time 0) $tag failed"; }
  NOW=$(stat -c %s "$LOG" 2>/dev/null || echo 0); [ "$NOW" -lt "$OFF" ] && OFF=0
  tail -c +$(( OFF + 1 )) "$LOG" | grep -a -E 'strata (serve|generate): (prompt|file tier|PLE|session|layer split)' > "$R/$tag-engine-lines.txt"
  say "done $tag: $(grep -c . "$R/$tag-p.txt") prompt rows, $(grep -c . "$R/$tag-d.txt") decode rows"
done
