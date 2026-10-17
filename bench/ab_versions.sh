#!/usr/bin/env bash
# Strata 0.1.43.1 (the deployed service) against 0.1.44.1 (a second tree, ~/dev/strata-0.1.44.1), same config, one
# fresh start per run.  Usage: bench/ab_versions.sh <arm>:<tag> ...      e.g.  A:v431-1 B:v441-1 C:v441pf B:v441-2 A:v431-2
#   A  the deployed service: systemctl restart strata-q3 (preflight, the unit's limits, 0.1.43.1)
#   B  0.1.44.1, its own server.py, the deployed config (only exe / cwd / log point at the second tree)
#   C  B without --no-ple-prefetch (the per-row MADV_WILLNEED on): serve_load and the two probes only
# A and B run run_profile.sh, then the untraced probe on the same three text slices as the earlier probes and the
# traced probe on three others.  B and C run under a transient unit with the deployed unit's limits (memlock, swap,
# OOM score, environment).
# Between runs nothing may hold :8080, an engine process, VRAM or the locked RAM.  The deployed service is started
# again at the end, also after a failure.
set -u
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
V441=$HOME/dev/strata-0.1.44.1
PY=$HOME/dev/strata/.venv/bin/python                     # requirements.txt is the same in both releases
UNIT=strata-ab-v441
URL=http://127.0.0.1:8080
TRACE_OFFSETS="555555 444444 111111"
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

start_v441() {   # $1 config
  sudo systemd-run --unit="$UNIT" --collect --uid=thc1006 --gid=thc1006 \
    -p WorkingDirectory="$V441" -p LimitMEMLOCK=infinity -p LimitNOFILE=65536 -p OOMScoreAdjust=500 \
    -p MemorySwapMax=0 -p KillMode=mixed -p TimeoutStopSec=120 -p NoNewPrivileges=yes -p ProtectSystem=full \
    -p PrivateTmp=yes -p Restart=no \
    -E PYTHONUNBUFFERED=1 -E OMP_NUM_THREADS=1 -E OPENBLAS_NUM_THREADS=1 -E MKL_NUM_THREADS=1 \
    "$PY" "$V441/serve/server.py" --engine strata --config "$1" --port 8080 &&
  wait_loaded "$UNIT"
}

record() {   # $1 tag $2 arm $3 config $4 engine log
  {
    echo "tag $1  arm $2  at $(date '+%F %T')"
    echo "config $3 sha256 $(sha256sum < "$3" | cut -c1-16)"
    pid=$(pgrep -f '[e]ngine-cuda12/strata --serve' | head -1)
    echo "engine pid $pid exe $(readlink -f /proc/$pid/exe) sha256 $(sha256sum < /proc/$pid/exe | cut -c1-16)"
    echo "engine args: $(tr '\0' ' ' < /proc/$pid/cmdline)"
    echo "health: $(curl -s -m 5 $URL/health)"
    echo "version line: $(grep -a -o 'session is up (engine [0-9.]*)' "$4" 2>/dev/null | tail -1)"
  } > "$R/$1-arm.txt"
}

finish() {   # the deployed service back, unless the last run left it serving
  rc=$?
  if ! systemctl is-active -q strata-q3.service || systemctl is-active -q "$UNIT"; then
    say "restoring the deployed service"
    stop_all; sudo systemctl start strata-q3.service
  fi
  say "strata-q3: $(systemctl is-active strata-q3.service); exit $rc"
}
trap finish EXIT

for step in "$@"; do
  arm=${step%%:*}; tag=${step#*:}
  say "=== $arm:$tag"
  stop_all || exit 1
  case $arm in
    A) sudo systemctl start strata-q3.service || exit 1
       CFG=$HOME/dev/strata/strata-iq3_xxs.json; LOG=$HOME/dev/strata/strata-iq3_xxs.log ;;
    B) CFG=$V441/strata-iq3_xxs.json; LOG=$V441/strata-iq3_xxs.log; start_v441 "$CFG" || exit 1 ;;
    C) CFG=$V441/strata-iq3_xxs-prefetch.json; LOG=$V441/strata-iq3_xxs-prefetch.log; start_v441 "$CFG" || exit 1 ;;
    *) say "unknown arm $arm"; exit 2 ;;
  esac
  record "$tag" "$arm" "$CFG" "$LOG"
  if [ "$arm" = C ]; then
    (cd "$HOME/dev/strata" && .venv/bin/python tools/serve_load.py $URL --clients 1,2,4 --rounds 3 \
       --max-tokens 512 --json "$R/$tag-serve_load.json" | tail -5) > "$R/$tag-perf.log" 2>&1
  else
    STRATA_LOG=$LOG "$D/bench/run_profile.sh" "$tag" || say "run_profile $tag failed"
  fi
  python3 "$D/bench/ple_prefetch_probe.py" --tag "$tag" --json "$R/ple_prefetch_probe.jsonl" > "$R/$tag-probe.txt" 2>&1
  "$D/bench/traced_probe.sh" "$tag-traced" $TRACE_OFFSETS > /dev/null 2>&1 || say "traced probe $tag failed"
  say "done $arm:$tag"
done
