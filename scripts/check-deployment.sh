#!/usr/bin/env bash
# Read-only health and acceptance check for the Strata IQ3_XXS service on 4x P100.  Exits 1 when the service, its
# timer, the ARC cap or the API is not as deployed (a problem line says which), 0 otherwise.
# Usage: scripts/check-deployment.sh [base-url]   (default http://127.0.0.1:8080; STRATA_KEY=<key> if one is set)
set -u -o pipefail
URL=${1:-http://127.0.0.1:8080}
STRATA_DIR=${STRATA_DIR:-/home/thc1006/dev/strata}
LOG=${STRATA_LOG:-$STRATA_DIR/strata-iq3_xxs.log}
AUTH=()
[ -n "${STRATA_KEY:-}" ] && AUTH=(-H "Authorization: Bearer $STRATA_KEY")

section() { printf '\n== %s ==\n' "$1"; }
problems=()
problem() { problems+=("$1"); echo "PROBLEM: $1"; }

section "service"
for u in strata-q3.service strata-q3-health.timer zfs-arc-max.service; do
  a=$(systemctl is-active "$u" 2>/dev/null); e=$(systemctl is-enabled "$u" 2>/dev/null)
  printf '%-26s %s / %s\n' "$u" "$a" "$e"
  if [ "$a" != active ] || [ "$e" != enabled ]; then problem "$u is $a / $e, not active / enabled"; fi
done

section "API"
health=$(curl -fsS -m 10 "${AUTH[@]}" "$URL/health" 2>/dev/null) || problem "/health does not answer"
[ -n "$health" ] && jq -c '{status, loaded, model, max_context}' <<< "$health"
[ -n "$health" ] && ! jq -e '.loaded == true' <<< "$health" >/dev/null && problem "/health: the model is not loaded"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${AUTH[@]}" "$URL/v1/models")
case $code in
  200) ;;
  401|403) problem "/v1/models answered $code: the server has an API key; set STRATA_KEY=<key>" ;;
  *) problem "/v1/models answered ${code:-nothing}" ;;
esac
[ "$code" = 200 ] && curl -fsS -m 10 "${AUTH[@]}" "$URL/v1/models" | jq -r '.data[].id'
curl -fsS -m 10 "${AUTH[@]}" "$URL/v1/status" |
  jq -c '{engine, concurrency, context: .context.native, activity}' || true

section "engine log (last start)"
if [ -f "$LOG" ]; then
  # the last start is the server's "engine started:" line; keep its start-up lines (not the per-request ones)
  start=$(grep -n 'engine started:' "$LOG" | tail -1 | cut -d: -f1)
  tail -n +"${start:-1}" "$LOG" | grep -v -E '^strata (serve|batch): |^strata batch \(' | grep -E \
    'engine started|layer split|batch.groups|batch_slots|--batch|dense weights|expert cache|resident|lend sizing|PLE table|ple-io|pool workers|WARNING|error|failed|out of memory' |
    grep -v 'upload no error, sync no error' |
    sed "s|$HOME|~|g" | cut -c1-220 | head -n 60
  echo "-- requests since this start, errors/warnings in their lines:"
  tail -n +"${start:-1}" "$LOG" | grep -c '^strata serve: prompt' | sed 's/^/   prompt lines: /'
  tail -n +"${start:-1}" "$LOG" | grep '^strata serve' | grep -i -c -E 'error|warning|refused' | sed 's/^/   error or warning lines: /'
else
  echo "no log at $LOG"
fi

section "GPU"
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu,clocks.sm,power.draw \
  --format=csv,noheader || problem "nvidia-smi failed"

section "RAM / ARC / swap"
grep -E 'MemTotal|MemAvailable|SwapTotal|SwapFree|Mlocked' /proc/meminfo
awk '$1=="size"{printf "ARC size: %.2f GiB\n", $3/2^30} $1=="c_max"{printf "ARC max:  %.2f GiB\n", $3/2^30}' \
  /proc/spl/kstat/zfs/arcstats 2>/dev/null
[ "$(cat /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null)" = 8589934592 ] || problem "zfs_arc_max is not 8 GiB"
pid=$(pgrep -P "$(systemctl show -p MainPID --value strata-q3.service)" -f '[e]ngine-cuda12/strata --serve' | head -1)
if [ -n "$pid" ]; then
  grep -E 'VmRSS|VmLck|VmPin' "/proc/$pid/status"
  awk '/Max locked memory/ {print "engine memlock limit:", $4, $5}' "/proc/$pid/limits"
fi

section "health checks and service events (last 2 h; needs sudo for the system journal)"
if sudo -n true 2>/dev/null; then
  ev=$(sudo -n journalctl -u strata-q3.service -u strata-q3-health.service --since "-2h" --no-pager 2>/dev/null |
       grep -E 'strata-health|preflight|wait-ready|Started|Failed|start-limit' | tail -n 12)
  echo "${ev:-(no health-check or service events in the last 2 h)}"
else
  echo "(sudo asks for a password: run 'sudo -v' first to include the system journal)"
fi

echo
if [ ${#problems[@]} = 0 ]; then echo "no problems found"; exit 0; fi
echo "${#problems[@]} problem(s):"; printf '  %s\n' "${problems[@]}"; exit 1
