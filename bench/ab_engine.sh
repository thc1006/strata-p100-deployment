#!/usr/bin/env bash
# Engine experiments (the scheduling work, before decision 0004's A/B): each arm is a config of a Strata tree, run in a
# transient unit with the deployed unit's limits instead of strata-q3, measured with bench/engine_screen.py.
#   bench/ab_engine.sh ARMS.json TAG:ARM [TAG:ARM ...]        e.g.  ... c0-1:C0 c1-1:C1 c0-2:C0
# ARMS.json: {"tree": "<Strata tree>", "settle_s": 300, "screen_n": 3,
#             "arms": {"C0": {"config": "<config.json in the tree>", "env": {"STRATA_X": "0"},
#                             "args_add": ["--flag", "value"],      (optional: engine arguments only this arm has)
#                             "must": ["<regex the start-up lines must match>"], "must_not": ["<regex>"],
#                             "engaged": ["<regex an engine line of the screen must match>"], "never": ["<regex>"]}}}
# (engaged / never are judged by bench/ab_engine_summary.py on <tag>-engine-lines.txt: a switch that acts only on some
# requests shows only then.)  The arms' configs must be equal but for exe, cwd, log, their env keys and args_add.
# Before anything is stopped: strata-q3 must be active and loaded (one still loading is waited for; a failed or
# stopped one is someone else's state and is refused), the GPUs must hold no process but its engine, no measurement or
# evaluation may run (it would share the CPUs), and no command line may make strata-preflight.sh refuse the restart.
# A systemd timer (strata-ab-rescue) is armed before strata-q3 is stopped and re-armed per phase: if this script dies
# without restoring the service, the timer stops the test unit and starts strata-q3.
# Per step: the running service is first given up to 15 minutes to finish what it is serving (/metrics idle; else the
# step refuses, so no one's reply is cut), then strata-q3 and any earlier test unit are stopped (no engine, :8080
# free, every GPU under 300 MiB); the arm starts in the unit strata-ab-engine; once the model is loaded and SETTLE
# seconds have passed and the unit serves nothing, one fixed ~2K-token warm-up prompt is read, and the arm is checked
# before anything is measured: the engine's environment must hold the arm's "env" values, and its log since its
# "engine started" line must match every "must" and no "must_not" pattern - a switch that is set but did not take is a
# run that measured nothing, so the step stops there.  (A start-up line shows what a switch planned, not that a
# kernel path ran: a switch that changes no answer and no speed needs its own proof that it ran.)  Then:
#   results/<tag>-arm.txt     the tree's commit and diff, the config's sha256, the engine's exe sha256, args,
#                             environment, VmLck, start-up lines, /health, /metrics' count, ARC, MemAvailable, VRAM
#   results/engine-screen.jsonl   bench/engine_screen.py's records, tagged (ENGINE_SCREEN_PHASES, when set, chooses its
#                             phases for every step of the call)
#   results/<tag>-after.txt   whether the same engine answered, the screen's exit status, the requests counted once
#                             the unit served nothing, the engine's error lines after the warm-up, the test unit's
#                             journal lines and the kernel's (Xid, OOM) during the step ("unreadable" if not read)
#   results/<tag>-engine-lines.txt   the engine's per-request lines (prompt and decode rates, loans, reads)
#   results/<tag>-texts.jsonl   every answer's text (the screen's --texts), to see where two arms' answers part
#   results/<tag>-gpu.txt     each GPU's SM and memory use once a second during the screen (nvidia-smi dmon, the same
#                             for every arm), to see which stage is busy in which phase
# At the end - also after a failure or an interrupt, and ignoring further signals until it is done - the test unit is
# stopped (after up to a minute for a reply it serves) and strata-q3 started again and waited for; if something
# still holds a GPU or makes the preflight refuse, the start is tried anyway and the reason printed.  Every name of
# this host is written as <host>.  Needs sudo -n (systemd-run, systemctl, journalctl).
set -u -o pipefail
SPEC=${1:?ARMS.json}; shift
[ $# -ge 1 ] || { echo "usage: bench/ab_engine.sh ARMS.json TAG:ARM ..." >&2; exit 2; }
D=$(cd "$(dirname "$0")/.." && pwd)
R=$D/results
PY=${AB_ENGINE_PY:-$HOME/dev/strata/.venv/bin/python}  # the deployed tree's venv: the same requirements in 0.1.45
IDLE_WAIT_S=${AB_ENGINE_IDLE_WAIT_S:-900}           # how long the running service may take to finish its replies
# (test hooks: tests/ab_engine_stubs.sh gives a corpus and a short admission delay to bench/engine_screen.py)
SCREEN_ARGS=(); [ -n "${AB_ENGINE_CORPUS:-}" ] && SCREEN_ARGS+=(--corpus "$AB_ENGINE_CORPUS")
[ -n "${AB_ENGINE_ADMIT_DELAY:-}" ] && SCREEN_ARGS+=(--admit-delay "$AB_ENGINE_ADMIT_DELAY")
UNIT=strata-ab-engine
PORT=${AB_ENGINE_PORT:-8080}                         # the service's port (other clients keep it); tests use another
URL=http://127.0.0.1:$PORT
say() { echo "[$(date +%T)] ab_engine: $*"; }

# every name of this host and the first label of each, longest first, in any case (as bench/ab_model_run.sh)
names=$({ hostname; hostname -f; hostname -A; } 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sort -u)
[ -n "$names" ] || { say "refusing: no host name to redact"; exit 1; }
names=$({ printf '%s\n' "$names"; printf '%s\n' "$names" | cut -d. -f1 | awk 'length >= 4'; } | sort -u |
        awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)
redact_args=(); while IFS= read -r n; do redact_args+=(-e "s/$(printf '%s' "$n" | sed 's/[.[\*^$/]/\\&/g')/<host>/gI"); done <<< "$names"
redact() { sed "${redact_args[@]}"; }

# the spec, checked whole before anything is stopped
spec() { python3 - "$SPEC" "$@" <<'EOF'
import json, os, sys
s = json.load(open(sys.argv[1]))
what = sys.argv[2:]
tree = s["tree"]
if what == ["check"]:
    assert os.path.isfile(os.path.join(tree, "engine-cuda12", "strata")), f"no engine in {tree}"
    assert os.path.isfile(os.path.join(tree, "serve", "server.py")), f"no server.py in {tree}"
    rest = {}
    for name, a in s["arms"].items():
        c = json.load(open(os.path.join(tree, a["config"])))
        assert c["exe"] == os.path.join(tree, "engine-cuda12", "strata"), f"{name}: the config's exe is not the tree's"
        for k, v in a.get("env", {}).items():
            assert str((c.get("env") or {}).get(k)) == str(v), f"{name}: the config's env {k} is not {v}"
        env = {k: v for k, v in (c.get("env") or {}).items() if k not in a.get("env", {})}
        args, add = list(c.get("args") or []), list(a.get("args_add") or [])
        if add:
            at = [i for i in range(len(args) - len(add) + 1) if args[i:i + len(add)] == add]
            assert len(at) == 1, f"{name}: the config's args hold its args_add {add} {len(at)} times, not once"
            args = args[:at[0]] + args[at[0] + len(add):]
        rest[name] = json.dumps({**{k: v for k, v in c.items() if k not in ("exe", "cwd", "log", "env", "args")},
                                 "env": env, "args": args}, sort_keys=True)
    assert len(set(rest.values())) == 1, "the arms' configs differ in more than exe, cwd, log, the arms' env keys and args_add"
    print("ok")
elif what[0] == "tree":
    print(tree)
elif what[0] == "num":
    print(s.get(what[1], {"settle_s": 300, "screen_n": 3}[what[1]]))
else:                                   # arm NAME FIELD
    a = s["arms"][what[1]]
    v = a[what[2]] if what[2] != "config" else os.path.join(tree, a["config"])
    if isinstance(v, dict):
        print("\n".join(f"{k}={x}" for k, x in v.items()))
    elif isinstance(v, list):
        print("\n".join(v))
    else:
        print(v)
EOF
}
ok=$(spec check) && [ "$ok" = ok ] || { say "refusing: the arms file does not check out: $ok"; exit 2; }
TREE=$(spec tree); SETTLE=$(spec num settle_s); N=$(spec num screen_n)
seen=" "
for step in "$@"; do
  tag=${step%%:*}; arm=${step#*:}
  case "$seen" in *" $tag "*) say "refusing: the tag $tag is given twice"; exit 2 ;; esac
  seen="$seen$tag "
  spec arm "$arm" config > /dev/null 2>&1 || { say "refusing: no arm $arm in $SPEC"; exit 2; }
  for f in arm.txt after.txt engine-lines.txt; do
    [ -e "$R/$tag-$f" ] && { say "refusing: $R/$tag-$f exists (pick a new tag)"; exit 2; }
  done
  grep -q "\"tag\": \"$tag\"" "$R/engine-screen.jsonl" 2>/dev/null && { say "refusing: $tag is in engine-screen.jsonl"; exit 2; }
done
sudo -n true 2>/dev/null || { say "refusing: needs sudo -n"; exit 1; }
# a measurement or an evaluation running: a process with one of these scripts as an argument of its own (a shell whose
# -c string merely names one is not such a process: the string has spaces).  (Test hook: AB_ENGINE_BUSY_UNDER counts
# only processes with an argument under that directory, so test sandboxes running at the same time do not see each
# other.)
busy=$(python3 - <<'EOF2'
import os
names = {"run_profile.sh", "stress_matrix.py", "latency_suite.py", "four_user_test.py", "serve_load.py",
         "engine_screen.py", "driver2.py", "run_side.sh", "grade_lcb.py", "latency_probe.py", "ab_model_run.sh"}
under = os.environ.get("AB_ENGINE_BUSY_UNDER")
me = {os.getpid(), os.getppid()}


def pinned_remote(pid, argv):
    """The remote side of the evaluation (driver2.py --endpoint remote, or run_side.sh remote) on one CPU: it talks to
    the remote gateway only and stays on its CPU, so it may run beside a measurement (it shows in the arm record)."""
    remote = any(a == "--endpoint" and b == "remote" for a, b in zip(argv, argv[1:])) or (
        any(os.path.basename(a) == "run_side.sh" for a in argv) and "remote" in argv)
    try:
        cpus = next(l.split(":", 1)[1].strip() for l in open(f"/proc/{pid}/status") if l.startswith("Cpus_allowed_list:"))
    except (OSError, StopIteration):
        return False
    return remote and cpus.isdigit()

for pid in filter(str.isdigit, os.listdir("/proc")):
    if int(pid) in me:
        continue
    try:
        argv = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        continue
    if any(" " not in a and os.path.basename(a) in names for a in argv) and (
            not under or any(a.startswith(under + "/") for a in argv)) and not pinned_remote(pid, argv):
        print(pid, " ".join(argv)[:120])
EOF2
)
[ -z "$busy" ] || { say "refusing: a measurement or an evaluation is running:"; echo "$busy"; exit 1; }

# an engine is a process whose command line STARTS with the deployed tree's or the tested tree's engine: a shell or an
# editor whose command line merely mentions it is not one (pgrep -f matches anywhere); any other process on a GPU
# still shows in stop_all's VRAM check
PROD_TREE=${AB_ENGINE_PROD_TREE:-$HOME/dev/strata}
ENGINE_RE="^($(printf '%s' "$PROD_TREE" | sed 's/[.[\*^$/]/\\&/g')|$(printf '%s' "$TREE" | sed 's/[.[\*^$/]/\\&/g'))/engine-cuda12/strata --serve"
engine_pid() { local m; m=$(systemctl show -p MainPID --value "$1"); [[ "$m" =~ ^[1-9][0-9]*$ ]] && pgrep -P "$m" -f "$ENGINE_RE" | head -1; }
# strata-preflight.sh refuses to start strata-q3 while any command line holds "engine-cuda12/strata --serve" (its test
# is an unanchored pgrep): a stray one - a test harness's fake engine, a shell or a watch that names it - would leave
# the service down after this script.  (Test hook: AB_ENGINE_PREFLIGHT_RE replaces the pattern.)
PREFLIGHT_RE=${AB_ENGINE_PREFLIGHT_RE:-[e]ngine-cuda12/strata --serve}
STRAY_WAIT_S=${AB_ENGINE_STRAY_WAIT_S:-300}
FREE_WAIT_S=${AB_ENGINE_FREE_WAIT_S:-180}             # how long a stop may take to free the engine, :8080 and the GPUs
strays() {        # every process the preflight would take for a second engine, but $1 (the service's own engine)
  local p
  for p in $(pgrep -f -- "$PREFLIGHT_RE"); do
    [ "$p" = "${1:-}" ] && continue
    echo "  $p $(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | cut -c1-160)"
  done
}
wait_idle() {     # up to $1 (default IDLE_WAIT_S) seconds for the service to serve nothing; 1 when it does not get there
  for _ in $(seq 1 $(( (${1:-$IDLE_WAIT_S} + 9) / 10 ))); do
    curl -fsS -m 10 "$URL/metrics" 2>/dev/null | python3 -c 'import json,sys
l = json.load(sys.stdin)["live"]
sys.exit(0 if l.get("state") == "idle" and not l.get("running") and not l.get("queued") else 1)' && return 0
    # nothing answers: idle only when neither unit runs (a server that is still loading does not listen yet)
    if ! curl -fsS -m 5 "$URL/health" > /dev/null 2>&1 && ! systemctl is-active -q strata-q3.service &&
       ! systemctl is-active -q "$UNIT"; then
      return 0
    fi
    sleep 10
  done
  return 1
}
stop_all() {
  sudo -n systemctl stop "$UNIT" 2>/dev/null
  sudo -n systemctl stop strata-q3.service
  for _ in $(seq 1 $(( (FREE_WAIT_S + 1) / 2 ))); do
    local used; used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null)
    # four numeric lines, each under 300 MiB: anything else (no answer, an error text) is not free
    if ! pgrep -f "$ENGINE_RE" > /dev/null && ! ss -ltn "( sport = :$PORT )" | grep -q LISTEN &&
       [ "$(grep -c -E '^ *[0-9]+ *$' <<< "$used")" -eq 4 ] && [ "$(awk '$1 > 300' <<< "$used" | wc -l)" -eq 0 ]; then
      return 0
    fi
    sleep 2
  done
  say "not free after $FREE_WAIT_S s: engine $(pgrep -f "$ENGINE_RE" | tr '\n' ' ')  :$PORT $(ss -ltnH "( sport = :$PORT )" | wc -l) listener(s)  GPU MiB used: $(tr '\n' ' ' <<< "$used")"
  return 1
}
# the rescue: a timer that stops the test unit and starts strata-q3 if this script is gone by then (re-armed per
# phase with that phase's longest time; stopped once strata-q3 is started).  (Test hook: AB_ENGINE_RESCUE_S.)
beside_cpu() {     # CPU seconds the remote evaluation's processes have used so far (0 when none runs)
  local t=0 p
  for p in $(pgrep -f 'driver2[.]py --endpoint remote'); do
    t=$(( t + $(awk '{print $14 + $15}' "/proc/$p/stat" 2>/dev/null || echo 0) ))
  done
  echo "$(( t / $(getconf CLK_TCK) ))"
}
rescue() {
  sudo -n systemctl stop strata-ab-rescue.timer 2>/dev/null
  sudo -n systemd-run --unit=strata-ab-rescue --collect --on-active="${AB_ENGINE_RESCUE_S:-$1}" \
    --timer-property=AccuracySec=5s /bin/sh -c "systemctl stop $UNIT; systemctl start strata-q3.service" > /dev/null ||
    { say "cannot arm the rescue timer"; return 1; }
}
wait_loaded() {   # $1 unit: until /health says loaded (at most 30 min), or the unit stops
  for i in $(seq 1 900); do
    curl -fsS -m 5 "$URL/health" 2>/dev/null | grep -q '"loaded": *true' && { say "$1 loaded after $((i * 2)) s"; return 0; }
    systemctl is-active -q "$1" || { say "$1 is not active"; return 1; }
    sleep 2
  done
  say "$1 not loaded after 30 min"; return 1
}
finish() {
  rc=$?
  trap '' INT TERM HUP            # a second signal must not stop the restore halfway (it would leave the service down)
  trap - EXIT
  [ -n "$child" ] && kill "$child" 2>/dev/null
  [ -n "$dmon" ] && kill "$dmon" 2>/dev/null
  rm -rf "$TMP"
  say "finishing (exit $rc): stopping the test unit, starting strata-q3"
  if systemctl is-active -q "$UNIT"; then
    wait_idle 60 || say "the test unit is still serving a reply after 60 s; stopping it"
  fi
  sudo -n systemctl stop "$UNIT" 2>/dev/null
  if ! systemctl is-active -q strata-q3.service; then
    stop_all || say "ERROR: not everything is free (above); starting strata-q3 anyway (its preflight refuses a second engine, and a failed start is retried by the rescue timer and the watchdog)"
    for _ in $(seq 1 $(( (STRAY_WAIT_S + 4) / 5 ))); do [ -z "$(strays)" ] && break; sleep 5; done
    odd=$(strays)
    [ -z "$odd" ] || { say "strata-preflight.sh will refuse to start strata-q3 while these run; stop them, then: sudo systemctl start strata-q3.service"; echo "$odd"; }
    sudo -n systemctl start strata-q3.service && wait_loaded strata-q3.service
  fi
  if systemctl is-active -q strata-q3.service; then
    sudo -n systemctl stop strata-ab-rescue.timer 2>/dev/null
  else
    say "ERROR: strata-q3 is not running; the rescue timer stays armed and tries again"
  fi
  say "strata-q3: $(systemctl is-active strata-q3.service)"
  exit $rc
}
state=$(systemctl is-active strata-q3.service)
case "$state" in
  active) ;;
  activating) say "strata-q3 is starting: waiting for it"; wait_loaded strata-q3.service || { say "refusing: strata-q3 did not come up"; exit 1; } ;;
  *) say "refusing: strata-q3 is $state (someone else's state: not touched)"; exit 1 ;;
esac
curl -fsS -m 10 "$URL/health" 2>/dev/null | grep -q '"loaded": *true' || { say "refusing: strata-q3 is active but not loaded"; exit 1; }
prod=$(engine_pid strata-q3.service)
[ -n "$prod" ] || { say "refusing: no engine under strata-q3"; exit 1; }
odd=$(strays "$prod")
[ -z "$odd" ] || { say "refusing: strata-preflight.sh would not start strata-q3 again while these run (it takes any command line with 'engine-cuda12/strata --serve' for a second engine):"; echo "$odd"; exit 1; }
apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null) ||
  { say "refusing: nvidia-smi cannot list the GPU processes"; exit 1; }
others=$(grep -v -x -e "$prod" -e '' <<< "$apps" | tr '\n' ' ')
[ -z "$others" ] || { say "refusing: other processes use the GPUs (they would keep the cards from being free): $others"; exit 1; }
TMP=$(mktemp -d)
# a long wait (the settle, the screen) runs in the background and is waited for: a trapped signal ends `wait` at once,
# while a foreground command would first run to its end
child=""; dmon=""
trap finish EXIT
trap 'say "interrupted"; [ -n "$child" ] && kill "$child" 2>/dev/null; exit 130' INT TERM HUP

for step in "$@"; do
  tag=${step%%:*}; arm=${step#*:}
  CFG=$(spec arm "$arm" config)
  LOG=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["log"])' "$CFG")
  say "=== $tag ($arm: $CFG)"
  wait_idle || { say "refusing: the service has been busy for $IDLE_WAIT_S s (someone's request is still running)"; exit 1; }
  rescue 45min || exit 1                    # the stop, the load (at most 30 min) and the settle
  stop_all || exit 1
  systemctl is-active -q strata-q3.service && { say "strata-q3 is running again (someone started it): stopping here"; exit 1; }
  cur=$(sudo -n journalctl -n 1 --show-cursor --no-pager -q 2>/dev/null | sed -n 's/^-- cursor: //p')
  [ -n "$cur" ] || { say "cannot read the journal's cursor"; exit 1; }
  OFF=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  sudo -n systemd-run --unit="$UNIT" --collect --uid=thc1006 --gid=thc1006 \
    -p WorkingDirectory="$TREE" -p LimitMEMLOCK=infinity -p LimitNOFILE=65536 -p OOMScoreAdjust=500 \
    -p MemorySwapMax=0 -p KillMode=mixed -p TimeoutStopSec=120 -p NoNewPrivileges=yes -p ProtectSystem=full \
    -p PrivateTmp=yes -p Restart=no \
    -E PYTHONUNBUFFERED=1 -E OMP_NUM_THREADS=1 -E OPENBLAS_NUM_THREADS=1 -E MKL_NUM_THREADS=1 \
    "$PY" "$TREE/serve/server.py" --engine strata --config "$CFG" --port "$PORT" > /dev/null || { say "systemd-run failed"; exit 1; }
  wait_loaded "$UNIT" || exit 1
  loaded_at=$(date +%s)
  say "settling $SETTLE s"
  sleep "$SETTLE" & child=$!; wait "$child"; child=""
  rescue 60min || exit 1                    # the warm-up and the screen
  pid=$(engine_pid "$UNIT")
  [ -n "$pid" ] || { say "no engine under $UNIT"; exit 1; }
  ticks0=$(awk '{print $22}' "/proc/$pid/stat")
  # another client on the port is served by the test unit: wait until it serves nothing, then count from there
  wait_idle || { say "the test unit has been busy for $IDLE_WAIT_S s (another client's request)"; exit 1; }
  total0=$(curl -fsS -m 10 "$URL/metrics" | python3 -c 'import json,sys; print(json.load(sys.stdin)["totals"]["requests"])') ||
    { say "no /metrics"; exit 1; }
  python3 "$D/bench/engine_screen.py" --url "$URL" "${SCREEN_ARGS[@]}" --warm-up > "$TMP/warm" 2>&1 & child=$!
  wait "$child" || { say "the warm-up read failed: $(tail -1 "$TMP/warm")"; exit 1; }
  child=""
  OFF2=$(stat -c %s "$LOG" 2>/dev/null || echo 0)       # the engine's own error lines count from here
  # the engine's lines from its last "engine started" line to now: its start-up and the warm-up read
  start_lines=$(tail -c +$((OFF + 1)) "$LOG" | awk '/^\[strata\] .*engine started: /{buf=""} {buf = buf $0 "\n"} END{printf "%s", buf}')
  # the arm must be what it says: its environment and its start-up lines
  bad=""
  while IFS='=' read -r k v; do
    [ -z "$k" ] && continue
    have=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$k=//p")
    [ "$have" = "$v" ] || bad="$bad; env $k is '${have}', the arm says '$v'"
  done <<< "$(spec arm "$arm" env)"
  while IFS= read -r re; do
    [ -z "$re" ] && continue
    grep -a -E -q -- "$re" <<< "$start_lines" || bad="$bad; no start-up line matches '$re'"
  done <<< "$(spec arm "$arm" must)"
  while IFS= read -r re; do
    [ -z "$re" ] && continue
    grep -a -E -q -- "$re" <<< "$start_lines" && bad="$bad; a start-up line matches '$re'"
  done <<< "$(spec arm "$arm" must_not)"
  {
    echo "tag $tag  arm $arm  at $(date '+%F %T')  loaded $(date -d @"$loaded_at" '+%F %T')  settle $SETTLE s"
    echo "tree $TREE  commit $(git -C "$TREE" rev-parse HEAD 2>/dev/null || echo none)  src diff: $(git -C "$TREE" diff --stat HEAD -- src include 2>/dev/null | tail -1)"
    echo "config $CFG sha256 $(sha256sum < "$CFG" | cut -c1-16)"
    echo "engine pid $pid exe $(readlink -f /proc/$pid/exe) sha256 $(sha256sum < /proc/$pid/exe | cut -c1-16)  start ticks $ticks0"
    echo "engine args: $(tr '\0' ' ' < /proc/$pid/cmdline)"
    echo "engine env: $(tr '\0' '\n' < /proc/$pid/environ | grep -E '^(STRATA|CUDA)_' | sort | tr '\n' ' ')"
    echo "VmLck: $(awk '/VmLck/ {print $2, $3}' /proc/$pid/status)"
    echo "health: $(curl -s -m 5 $URL/health)"
    echo "metrics requests before the warm-up: $total0"
    echo "warm-up: $(head -c 300 "$TMP/warm")"
    echo "screen phases: ${ENGINE_SCREEN_PHASES:-default}"
    echo "shared settings: $(f="${CFG%.json}.shared-settings.json"; [ -e "$f" ] && sha256sum < "$f" | cut -c1-16 || echo none)  load average: $(cut -d' ' -f1-3 /proc/loadavg)"
    echo "beside: $(pgrep -f 'driver2[.]py --endpoint remote' > /dev/null && echo "the remote evaluation, on CPU $(taskset -cp "$(pgrep -f 'driver2[.]py --endpoint remote' | head -1)" 2>/dev/null | sed 's/.*: //')" || echo nothing)"
    echo "beside CPU-s before the screen: $(beside_cpu)"
    echo "zfs_arc_max $(cat /sys/module/zfs/parameters/zfs_arc_max)  ARC c_max $(awk '$1=="c_max"{printf "%.0f", $3}' /proc/spl/kstat/zfs/arcstats)  MemAvailable $(awk '/MemAvailable/{print $2}' /proc/meminfo) kB"
    echo "gpu free MiB: $(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | tr '\n' ' ')"
    echo "arm check: ${bad:-ok}"
    echo "--- start-up lines"
    printf '%s\n' "$start_lines"
  } | redact > "$R/$tag-arm.txt"
  [ -z "$bad" ] || { say "STOP: $tag is not the arm $arm${bad}"; exit 1; }
  say "measuring $tag"
  # at most two hours: a driver killed outright (SIGKILL) cannot stop it, and the rescue timer does not know it
  timeout 7200 nvidia-smi dmon -s um -d 1 -o T > "$R/$tag-gpu.txt" 2>&1 & dmon=$!
  python3 "$D/bench/engine_screen.py" --url "$URL" "${SCREEN_ARGS[@]}" --tag "$tag" --json "$R/engine-screen.jsonl" --n "$N" \
    --texts "$R/$tag-texts.jsonl" > "$R/$tag-screen.txt" 2>&1 &
  child=$!; wait "$child"; src=$?; child=""
  kill "$dmon" 2>/dev/null; wait "$dmon" 2>/dev/null; dmon=""
  # the requests the warm-up and the screen sent (the screen's last line says how many it sent), against what the
  # service counted once it serves nothing (a request is counted when it ends): more means other clients' requests
  # were in the measurement; a screen that did not say is "unknown", which no count matches
  n_screen=$(sed -n 's/^engine_screen: requests sent: \([0-9][0-9]*\)$/\1/p' "$R/$tag-screen.txt" | tail -1)
  sent=unknown; [ -n "$n_screen" ] && sent=$((1 + n_screen))
  idle_end=yes; wait_idle || idle_end=NO
  total1=$(curl -fsS -m 10 "$URL/metrics" | python3 -c 'import json,sys; print(json.load(sys.stdin)["totals"]["requests"])' 2>/dev/null)
  counted=unknown; [[ "${total1:-}" =~ ^[0-9]+$ ]] && [ "$idle_end" = yes ] && counted=$((total1 - total0))
  pid2=$(engine_pid "$UNIT")
  same=NO; [ -n "$pid2" ] && [ "$pid2" = "$pid" ] && [ "$(awk '{print $22}' "/proc/$pid2/stat" 2>/dev/null)" = "$ticks0" ] && same=yes
  NOW=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  {
    echo "tag $tag  ended at $(date '+%F %T')  engine_screen exit $src"
    echo "same engine as before: $same"
    echo "idle before the count: $idle_end"
    echo "requests counted by /metrics: $counted  sent: $sent"
    echo "load average: $(cut -d' ' -f1-3 /proc/loadavg)"
    echo "beside CPU-s after the screen: $(beside_cpu)"
    # after the warm-up; "(upload no error, sync no error)" is the verify line every start prints
    if [ "$NOW" -ge "$OFF2" ]; then
      errs=$(tail -c +$((OFF2 + 1)) "$LOG" | grep -a -i -E 'error|refused|no progress|watchdog|out of memory|abort|non-?finite|said nothing' |
             grep -a -v -F '(upload no error, sync no error)')
      echo "engine log error lines: $(grep -c . <<< "$errs")"
      printf '%s\n' "$errs" | grep . | head -20 | sed 's/^/  /'
    else echo "engine log error lines: unknown (the log shrank)"; fi
    echo "--- the test unit's journal lines"
    if j=$(sudo -n journalctl -u "$UNIT" --after-cursor "$cur" --no-pager -q 2>/dev/null); then
      grep -a -i -E '\[strata\] [45][0-9]{2}\b|Traceback|ERROR|error:|refused|restart|had stopped|non-?finite|repeated|said nothing|marked busy with no request|cancel=True|Failed with result|Main process exited|code=killed' <<< "$j" || true
    else echo "unreadable"; fi
    echo "--- kernel lines"
    if k=$(sudo -n journalctl -k --after-cursor "$cur" --no-pager -q 2>/dev/null); then
      grep -a -i -E 'NVRM: Xid|Out of memory|oom-kill|segfault|BUG:|soft lockup|hung_task|blocked for more than' <<< "$k" || true
    else echo "unreadable"; fi
  } | redact > "$R/$tag-after.txt"
  tail -c +$((OFF + 1)) "$LOG" | grep -a -E '^strata serve: (prompt [0-9]+ tokens|lend sizing|  CUDA[0-9] |file tier I/O)|^strata batch|^strata prefill|^strata: prompt MMQ' |
    redact > "$R/$tag-engine-lines.txt"
  say "done $tag: screen exit $src, same engine $same"
  [ "$same" = yes ] || { say "STOP: the engine restarted during $tag"; exit 1; }
done
say "all steps done"
