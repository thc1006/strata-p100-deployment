#!/usr/bin/env bash
# The same performance set for every configuration under test, with the resource monitor around it.
# Usage: bench/run_profile.sh <tag>      (the service must be running the configuration <tag> names)
# A tag is used once: its result files are refused if they exist, so two runs never mix.
set -u
TAG=${1:?tag}
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
LOG=$R/$TAG-perf.log
# the engine log of the server under test (the 0.1.44.1 A/B runs a second tree); the load clients stay the
# deployed tree's, so both arms are measured by the same client code
ENGLOG=${STRATA_LOG:-$HOME/dev/strata/strata-iq3_xxs.log}
mkdir -p "$R"
for f in "$LOG" "$R/$TAG-monitor.csv" "$R/$TAG-serve_load.json" "$R/$TAG-four_user.jsonl"; do
  [ -e "$f" ] && { echo "refusing: $f exists (pick a new tag)"; exit 2; }
done
START_LINE=$(wc -l < "$ENGLOG")
python3 "$D/bench/monitor.py" record "$R/$TAG-monitor.csv" --every 2 &
MON=$!
trap 'kill $MON 2>/dev/null' EXIT
{
  echo "[$(date +%T)] serve_load 1,2,4 ($TAG)"
  (cd "$HOME/dev/strata" && .venv/bin/python tools/serve_load.py http://127.0.0.1:8080 --clients 1,2,4 --rounds 3 \
     --max-tokens 512 --json "$R/$TAG-serve_load.json" | tail -5)
  python3 "$D/bench/stress_matrix.py" replicate think4 worst --runs 3 --tag "$TAG" --out "$R/stress.jsonl"
  for t in 8192 16384; do
    echo "[$(date +%T)] burst 4x$t"
    python3 "$D/bench/four_user_test.py" burst --clients 4 --tokens $t --json "$R/$TAG-four_user.jsonl" |
      jq -c '{ttft_first_s, ttft_last_s, decode_tok_s_median, aggregate_tok_s, wall_s}'
  done
  echo "[$(date +%T)] admit 3 decoders + 16K"
  python3 "$D/bench/four_user_test.py" admit --decoders 3 --tokens 16384 --delay 25 --json "$R/$TAG-four_user.jsonl" |
    jq -c '{newcomer: .newcomer|{prompt_tokens, ttft_s}, before: .decoder_stream_rate_10s_before, during: .decoder_stream_rate_during_admission, during_window_s}'
  # upstream's own stall metrics (#2050 / #2326) and open-loop arrivals with the fixed SLO
  python3 "$D/bench/latency_suite.py" coldjoin preempt --tag "$TAG" --out "$R/latency.jsonl"
  python3 "$D/bench/latency_suite.py" poisson --rate 0.1 --requests 30 --tag "$TAG" --out "$R/latency.jsonl"
  echo "[$(date +%T)] done"
} > "$LOG" 2>&1
kill $MON 2>/dev/null; wait $MON 2>/dev/null
W=$(jq -r --arg t "$TAG" 'select(.tag==$t and .scenario=="worst") | "\(.start) \(.end)"' "$R/stress.jsonl" | tail -1)
if [ -n "$W" ]; then
  set -- $W
  python3 "$D/bench/monitor.py" summary "$R/$TAG-monitor.csv" --since "$1" --until "$2" > "$R/$TAG-monitor-worst.json"
fi
python3 "$D/bench/monitor.py" summary "$R/$TAG-monitor.csv" > "$R/$TAG-monitor-all.json"
# the engine's own lines for exactly this run: per-request prompt/decode rates and the (ZFS: ARC-inclusive) read counter
tail -n +"$((START_LINE + 1))" "$ENGLOG" | grep -E '^strata serve: (prompt [0-9]+ tokens|file tier I/O this request)|^strata batch \(' \
  > "$R/$TAG-engine-lines.txt"
echo "finished $TAG"
