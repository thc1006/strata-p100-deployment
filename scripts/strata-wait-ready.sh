#!/usr/bin/env bash
# ExecStartPost of strata-q3.service: the unit becomes "active" only once the model is loaded (about 7.5 min after a
# start here), not when server.py starts.  Fails when the server process dies during the load.
set -u
URL=${STRATA_URL:-http://127.0.0.1:8080}
uptime_s() { local u; read -r u _ < /proc/uptime; echo "${u%.*}"; }   # monotonic: NTP may step the clock at boot
t0=$(uptime_s)
while [ $(( $(uptime_s) - t0 )) -lt 1800 ]; do              # 30 min, polled every 2 s
  if curl -fsS -m 5 "$URL/health" 2>/dev/null | grep -q '"loaded": *true'; then
    echo "strata-wait-ready: loaded after $(( $(uptime_s) - t0 )) s"
    exit 0
  fi
  [ -n "${MAINPID:-}" ] && ! kill -0 "$MAINPID" 2>/dev/null && { echo "strata-wait-ready: server.py exited"; exit 1; }
  sleep 2
done
echo "strata-wait-ready: not loaded after 30 min"
exit 1
