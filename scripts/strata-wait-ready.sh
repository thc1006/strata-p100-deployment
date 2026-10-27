#!/usr/bin/env bash
# ExecStartPost of strata-q3.service: the unit becomes "active" only once the model is loaded (about 9 minutes after a
# start here with IQ3_S: 538-548 s in results/ab2-iq3s-*-switch.txt and results/ab3-iq3s-*-switch.txt), not when
# server.py starts.  Fails when the server process dies during the load.
set -u
URL=${STRATA_URL:-http://127.0.0.1:8080}
uptime_s() { local u; read -r u _ < /proc/uptime; echo "${u%.*}"; }   # monotonic: NTP may step the clock at boot
t0=$(uptime_s)
# curl -q, first: no ~/.curlrc of the service's user changes the request (an "output" there would hide the answer);
# --noproxy '*': no proxy that the environment names answers for the server
while [ $(( $(uptime_s) - t0 )) -lt 1800 ]; do              # 30 min, polled every 2 s
  if curl -q --noproxy '*' -fsS -m 5 "$URL/health" 2>/dev/null | grep -q '"loaded": *true'; then
    echo "strata-wait-ready: loaded after $(( $(uptime_s) - t0 )) s"
    exit 0
  fi
  [ -n "${MAINPID:-}" ] && ! kill -0 "$MAINPID" 2>/dev/null && { echo "strata-wait-ready: server.py exited"; exit 1; }
  sleep 2
done
echo "strata-wait-ready: not loaded after 30 min"
exit 1
