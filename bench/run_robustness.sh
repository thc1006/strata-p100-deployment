#!/usr/bin/env bash
# Robustness set for the configuration that stays deployed (after run_profile.sh picked it).
# Usage: bench/run_robustness.sh <tag> [soak-minutes]
set -u
TAG=${1:?tag}
SOAK=${2:-30}
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
LOG=$R/$TAG-robustness.log
ENGLOG=$HOME/dev/strata/strata-iq3_xxs.log
mkdir -p "$R"
for f in "$LOG" "$R/$TAG-robust-monitor.csv" "$R/$TAG-overload.json" "$R/$TAG-needles.json"; do
  [ -e "$f" ] && { echo "refusing: $f exists (pick a new tag)"; exit 2; }
done
START_LINE=$(wc -l < "$ENGLOG")
T_START=$(date '+%Y-%m-%d %H:%M:%S')
python3 "$D/bench/monitor.py" record "$R/$TAG-robust-monitor.csv" --every 2 &
MON=$!
trap 'kill $MON 2>/dev/null' EXIT
{
  python3 "$D/bench/stress_matrix.py" boundary disconnect multiturn budget qa --tag "$TAG" --out "$R/stress.jsonl"
  echo "[$(date +%T)] overload: 8 and 12 clients on 4 slots"
  (cd "$HOME/dev/strata" && .venv/bin/python tools/serve_load.py http://127.0.0.1:8080 --clients 8,12 --rounds 1 \
     --max-tokens 256 --json "$R/$TAG-overload.json" | tail -4)
  echo "[$(date +%T)] needle 30k at depths 10/50/90"
  (cd "$HOME/dev/strata" && .venv/bin/python tools/needle_bench.py --lengths 30k --depths 10,50,90 \
     --out "$R/$TAG-needles.json" | tail -5)
  echo "[$(date +%T)] early close"
  (cd "$HOME/dev/strata" && .venv/bin/python tools/early_close_test.py http://127.0.0.1:8080 | tail -5)
  for rate in 0.05 0.2; do
    python3 "$D/bench/latency_suite.py" poisson --rate $rate --requests 30 --tag "$TAG" --out "$R/latency.jsonl"
  done
  python3 "$D/bench/stress_matrix.py" soak --minutes "$SOAK" --clients 4 --tag "$TAG" --out "$R/stress.jsonl"
  echo "[$(date +%T)] engine log since this run: errors, refusals, watchdog, warnings"
  tail -n +"$((START_LINE + 1))" "$ENGLOG" | grep -c -E '^strata serve: prompt' | sed 's/^/engine prompt lines (batched requests log more than one): /'
  tail -n +"$((START_LINE + 1))" "$ENGLOG" | grep -i -E 'error|refused slot|no progress|watchdog|out of memory|WARNING|restart' |
    sed -E 's/[0-9]+(\.[0-9]+)?/N/g' | sort | uniq -c | sort -rn | head -20
  echo "[$(date +%T)] server journal since this run: HTTP errors, refusals, restarts"
  journalctl -u strata-q3.service --since "$T_START" --no-pager 2>/dev/null |
    grep -E '" (4|5)[0-9]{2} |\[strata\] (4|5)[0-9]{2} |ERR|error|refused|restart|had stopped' | tail -15
  echo "[$(date +%T)] done"
} > "$LOG" 2>&1
kill $MON 2>/dev/null; wait $MON 2>/dev/null
python3 "$D/bench/monitor.py" summary "$R/$TAG-robust-monitor.csv" > "$R/$TAG-robust-monitor-all.json"
echo "finished $TAG robustness"
