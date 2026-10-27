#!/usr/bin/env bash
# Read-only health and acceptance check for the Strata service on 4x P100 (IQ3_XXS or IQ3_S, config/profile.conf).  Exits 1 when the service, its
# timer, the ARC cap or the API is not as deployed (a problem line says which), 0 otherwise.
# Usage: scripts/check-deployment.sh [base-url]   (default http://127.0.0.1:8080; STRATA_KEY=<key> if one is set)
# The key goes to curl in a header file of its own (0600, in a directory of its own), never on a command line, which
# every account on the machine can read in /proc; a key with a control character is not sent.  curl runs with -q, its
# first option, so that no ~/.curlrc changes the requests or where their answers go, and with --noproxy '*', so that
# no proxy that the environment names (http_proxy, all_proxy) gets the requests and the key.
set -u -o pipefail
URL=${1:-http://127.0.0.1:8080}
STRATA_DIR=${STRATA_DIR:-/home/thc1006/dev/strata}
LOG=${STRATA_LOG:-$STRATA_DIR/strata-iq3_xxs.log}

section() { printf '\n== %s ==\n' "$1"; }
problems=()
problem() { problems+=("$1"); echo "PROBLEM: $1"; }
AUTH=()
keydir=
trap '[ -z "$keydir" ] || rm -rf -- "$keydir"' EXIT
if [ -n "${STRATA_KEY:-}" ]; then
  case $STRATA_KEY in
    *[[:cntrl:]]*) problem "STRATA_KEY holds a control character: it is not sent" ;;
    *) keydir=$(mktemp -d) || exit 1                  # 0700
       (umask 077; printf 'Authorization: Bearer %s\n' "$STRATA_KEY" > "$keydir/header") || exit 1   # a builtin: no command line
       AUTH=(-H "@$keydir/header") ;;
  esac
fi

section "service"
for u in strata-q3.service strata-q3-health.timer zfs-arc-max.service; do
  a=$(systemctl is-active "$u" 2>/dev/null); e=$(systemctl is-enabled "$u" 2>/dev/null)
  printf '%-26s %s / %s\n' "$u" "$a" "$e"
  if [ "$a" != active ] || [ "$e" != enabled ]; then problem "$u is $a / $e, not active / enabled"; fi
done

section "API"
health=$(curl -q --noproxy '*' -fsS -m 10 "${AUTH[@]}" "$URL/health" 2>/dev/null) || problem "/health does not answer"
[ -n "$health" ] && jq -c '{status, loaded, model, max_context}' <<< "$health"
[ -n "$health" ] && ! jq -e '.loaded == true' <<< "$health" >/dev/null && problem "/health: the model is not loaded"
code=$(curl -q --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 10 "${AUTH[@]}" "$URL/v1/models")
case $code in
  200) ;;
  401|403) problem "/v1/models answered $code: the server has an API key; set STRATA_KEY=<key>" ;;
  *) problem "/v1/models answered ${code:-nothing}" ;;
esac
[ "$code" = 200 ] && curl -q --noproxy '*' -fsS -m 10 "${AUTH[@]}" "$URL/v1/models" | jq -r '.data[].id'
curl -q --noproxy '*' -fsS -m 10 "${AUTH[@]}" "$URL/v1/status" |
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
[ "$(cat /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null)" = 2147483648 ] || problem "zfs_arc_max is not 2 GiB"
# the maintainer's account may use 6 GiB and no swap (decision 0007), and pages it locks, or that it mapped first
# and a process then locked (the engine, when it started while they were cached), count against that
acct=/sys/fs/cgroup/user.slice/user-1003.slice
if [ -r "$acct/memory.max" ]; then
  awk '{printf "account (user-1003.slice): %.2f GiB in use", $1/2^30}' "$acct/memory.current"
  awk '$1=="unevictable"{printf ", %.2f GiB of it locked", $2/2^30}' "$acct/memory.stat"; echo
  [ "$(cat "$acct/memory.max")" = 6442450944 ] && [ "$(cat "$acct/memory.swap.max")" = 0 ] ||
    problem "the account's memory limit is not in force: memory.max $(cat "$acct/memory.max"), memory.swap.max $(cat "$acct/memory.swap.max") (decision 0007: 6 GiB, 0)"
  locked=$(awk '$1=="unevictable"{print $2}' "$acct/memory.stat" 2>/dev/null)
  if ! [[ "$locked" =~ ^[0-9]+$ ]]; then
    problem "the account's locked memory cannot be read ($acct/memory.stat)"
  elif [ "$locked" -gt 268435456 ]; then
    problem "$((locked >> 20)) MiB of locked memory counts against the account's 6 GiB limit: pages it mapped first that the engine (or another process) locked; they count until they are unlocked"
  fi
else
  problem "the account's cgroup cannot be read ($acct)"
fi
pid=$(pgrep -P "$(systemctl show -p MainPID --value strata-q3.service)" -f '[e]ngine-cuda12/strata --serve' | head -1)
if [ -n "$pid" ]; then
  grep -E 'VmRSS|VmLck|VmPin' "/proc/$pid/status"
  awk '/Max locked memory/ {print "engine memlock limit:", $4, $5}' "/proc/$pid/limits"
fi

# the last 12 events of the last 2 h: the watchdog's findings, the preflight, wait-ready, starts, failures; not the
# Starting/Finished lines that systemd writes for every health check, which would fill the 12 lines with routine
# checks
section "health checks and service events (the last 12 of the last 2 h; needs sudo for the system journal)"
if sudo -n true 2>/dev/null; then
  ev=$(sudo -n journalctl -u strata-q3.service -u strata-q3-health.service --since "-2h" --no-pager 2>/dev/null |
       grep -E 'strata-health|preflight|wait-ready|Started|Failed|start-limit' |
       grep -v -E 'systemd\[1\]: (Starting|Finished) Strata health check' | tail -n 12)
  echo "${ev:-(no health-check or service events in the last 2 h)}"
else
  echo "(sudo asks for a password: run 'sudo -v' first to include the system journal)"
fi

echo
if [ ${#problems[@]} = 0 ]; then echo "no problems found"; exit 0; fi
echo "${#problems[@]} problem(s):"; printf '  %s\n' "${problems[@]}"; exit 1
