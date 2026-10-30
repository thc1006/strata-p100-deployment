#!/usr/bin/env bash
# bench/ab_engine.sh against stubs: sudo, systemctl, systemd-run, journalctl, nvidia-smi and hostname are scripts in a
# sandbox; "strata-q3" and the test unit are tests/fake_strata.py servers on a free port, each with a fake engine (a
# compiled program that only waits) whose command line is the tree's engine's.  The real service, journal, sudo and
# port 8080 are never touched.  Needs gcc, setsid, pgrep and ss.
# Usage: tests/ab_engine_stubs.sh [checkout]   (prints PASS / FAIL lines, exits 1 on a FAIL; KEEP_SB=1 keeps the sandbox)
set -u
W=${1:-$(cd "$(dirname "$0")/.." && pwd)}
export SB; SB=$(mktemp -d "${TMPDIR:-/tmp}/abeng.XXXXXX")
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
# pkill -f, but only to the sandbox's own processes, those with SB=$SB in their environment: another process of this
# user, the service's engine among them, may match the pattern too.  $1: the signal, $2: the pattern
sb_pkill() { local p; for p in $(pgrep -f -- "$2"); do
               tr '\0' '\n' < "/proc/$p/environ" 2> /dev/null | grep -qxF -- "SB=$SB" && kill "-$1" "$p" 2> /dev/null
             done; return 0; }
killall_sb() { for f in "$SB"/units/*.pid; do [ -e "$f" ] && kill -- -"$(cat "$f")" 2>/dev/null; done; rm -f "$SB"/units/*.pid
               sb_pkill TERM "^$SB/stray/engine-cuda12/strata"; sb_pkill TERM "$SB/bin/nvidia-smi dmon"; }
cleanup() { killall_sb; rm -rf "$SB"; }
trap cleanup EXIT
[ -n "${KEEP_SB:-}" ] && trap killall_sb EXIT

export PORT; PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
mkdir -p "$SB/bin" "$SB/units" "$SB/repo/results" "$SB/tree/engine-cuda12" "$SB/tree/serve" "$SB/prod/engine-cuda12" "$SB/prod/serve"
cp -r "$W/bench" "$SB/repo/bench"; mkdir -p "$SB/repo/tests"; cp "$W/tests/fake_strata.py" "$SB/repo/tests/"
printf '#include <unistd.h>\nint main(void) { for (;;) pause(); }\n' > "$SB/engine.c"
gcc -O2 -o "$SB/tree/engine-cuda12/strata" "$SB/engine.c" || exit 1
cp "$SB/tree/engine-cuda12/strata" "$SB/prod/engine-cuda12/strata"
for t in tree prod; do       # server.py: the fake server, with the tree's engine as its child
  cat > "$SB/$t/serve/server.py" <<EOF
import os, sys
a = sys.argv[1:]
cfg, port = a[a.index("--config") + 1], a[a.index("--port") + 1]
os.execv(sys.executable, [sys.executable, "$SB/repo/tests/fake_strata.py", "--port", port, "--config", cfg,
                          "--engine", "$SB/$t/engine-cuda12/strata"])
EOF
done
python3 - "$SB" <<'EOF'
import json, sys
sb = sys.argv[1]
def cfg(path, tree, env):
    json.dump({"exe": f"{sb}/{tree}/engine-cuda12/strata", "cwd": f"{sb}/{tree}", "log": f"{sb}/{tree}/{path}.log",
               "model_name": "fake", "args": ["--batch", "4"], "env": env}, open(f"{sb}/{tree}/{path}.json", "w"))
cfg("prod", "prod", {"STRATA_X": "0"})
cfg("cfg-gs0", "tree", {"STRATA_MMQ_GROUP_SORT": "0", "STRATA_MMQ_RESIDENT_SORT_NE": "0"})
cfg("cfg-gs1", "tree", {"STRATA_MMQ_GROUP_SORT": "1", "STRATA_MMQ_RESIDENT_SORT_NE": "0"})
arms = json.load(open(f"{sb}/repo/bench/arms-group-sort.json"))
arms.update(tree=f"{sb}/tree", settle_s=1, screen_n=1)
arms["arms"]["C0"]["must"] = arms["arms"]["C1"]["must"] = [m.replace("0\\.1\\.45", "0\\.1\\.45") for m in arms["arms"]["C1"]["must"]][:1]
arms["arms"]["C1"]["must"] = arms["arms"]["C1"]["must"] + [
    "strata: prompt MMQ sorts the full expert groups \\(STRATA_MMQ_GROUP_SORT=1\\): [1-9][0-9]* of [0-9]+ layers"]
json.dump(arms, open(f"{sb}/arms.json", "w"))
bad = json.loads(json.dumps(arms)); bad["arms"]["C1"]["env"]["STRATA_MMQ_GROUP_SORT"] = "2"
json.dump(bad, open(f"{sb}/arms-bad.json", "w"))
open(f"{sb}/corpus.txt", "w").write("\n".join(f"Paragraph {i}: chunks are read and replies decoded." for i in range(4000)))
EOF

# --- the stubs
cat > "$SB/bin/sudo" <<'EOF'
#!/bin/bash
[ "$1" = -n ] && shift
exec "$@"
EOF
cat > "$SB/bin/systemctl" <<'EOF'
#!/bin/bash
# units are pid files; a unit is active while its process group lives
alive() { [ -e "$SB/units/$1.pid" ] && kill -0 "$(cat "$SB/units/$1.pid")" 2>/dev/null; }
stop() { if [ -e "$SB/units/$1.pid" ]; then kill -- -"$(cat "$SB/units/$1.pid")" 2>/dev/null; for _ in $(seq 50); do
           alive "$1" || break; sleep 0.1; done; rm -f "$SB/units/$1.pid"; fi; echo "stop $1" >> "$SB/actions"; }
case "$1" in
  show) u=${@: -1}; p=$2; [ "$p" = -p ] && p=$3
        case $p in MainPID) alive "$u" && cat "$SB/units/$u.pid" || echo 0 ;; ActiveState) alive "$u" && echo active || echo inactive ;; esac ;;
  is-active) u=${@: -1}; alive "$u" && { [ "$2" = -q ] || echo active; exit 0; } || { [ "$2" = -q ] || echo inactive; exit 3; } ;;
  stop) stop "$2" ;;
  start) echo "start $2" >> "$SB/actions"
         [ "$2" = strata-q3.service ] || exit 1
         # the preflight: any (sandbox) process with an engine's command line refuses the start
         if pgrep -f "^$SB/[^ ]*engine-cuda12/strata --serve" > /dev/null; then echo "preflight refused" >> "$SB/actions"; exit 1; fi
         # systemd starts a unit with its own environment: the fake load delay is the test unit's only
         setsid env -u FAKE_LOAD_S python3 "$SB/prod/serve/server.py" --engine strata --config "$SB/prod/prod.json" --port "$PORT" > /dev/null 2>&1 &
         echo $! > "$SB/units/strata-q3.service.pid" ;;
  *) echo "systemctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat > "$SB/bin/systemd-run" <<'EOF'
#!/bin/bash
unit=""; envs=(); active=""
while [ $# -gt 0 ]; do
  case "$1" in
    --unit=*) unit=${1#--unit=}; shift ;;
    --on-active=*) active=${1#--on-active=}; shift ;;
    --collect|--uid=*|--gid=*|--timer-property=*) shift ;;
    -p) shift 2 ;;
    -E) envs+=("$2"); shift 2 ;;
    *) break ;;
  esac
done
if [ -n "$active" ]; then        # a timer: the command runs after that many seconds (a span like 45min: an hour)
  [[ "$active" =~ ^[0-9]+$ ]] || active=3600
  echo "timer $unit ${active}s: $*" >> "$SB/actions"
  # its command line names the sandbox, so that harness_run.py's sweep finds it (it leaves the harness's session)
  setsid bash -c 'sleep "$1"; shift; echo "rescue fired" >> "$SB/actions"; "$@"' "$SB/rescue-timer" "$active" "$@" \
    > /dev/null 2>&1 &
  echo $! > "$SB/units/$unit.timer.pid"
  exit 0
fi
echo "systemd-run $unit $*" >> "$SB/actions"
setsid env "${envs[@]}" "$@" > /dev/null 2>&1 &
echo $! > "$SB/units/$unit.pid"
EOF
# logger writes the driver's mark; journalctl -t finds it (not with STUB_NO_MARK).  A step's windows: from the mark
# with --cursor, every line; with --after-cursor, as systemd 249 does after a cursor whose entry does not match the
# filter, all but the first
cat > "$SB/bin/logger" <<'EOF'
#!/bin/bash
[ "$1" = -t ] && [ "$3" = -- ] && [ $# = 4 ] || { echo "logger stub: unexpected: $*" >&2; exit 99; }
[ -n "${STUB_NO_MARK:-}" ] || echo "$2" >> "$SB/marks"
EOF
cat > "$SB/bin/journalctl" <<'EOF'
#!/bin/bash
mark_ok() { grep -qxF "${1#s=MARK;}" "$SB/marks" 2>/dev/null; }
case "$*" in *" --cursor "*|*" --after-cursor "*) [ -e "$SB/journal_fail" ] && exit 1 ;; esac
case "$*" in
  "-t "*" -n 1 --show-cursor --no-pager -q")
    grep -qxF "$2" "$SB/marks" 2>/dev/null && { echo "x"; echo "-- cursor: s=MARK;$2"; } ;;
  "-u strata-ab-engine --cursor "*" --no-pager -q") mark_ok "$4" || exit 1; cat "$SB/journal.txt" 2>/dev/null; exit 0 ;;
  "-k --cursor "*" --no-pager -q") mark_ok "$3" || exit 1; cat "$SB/kernel.txt" 2>/dev/null; exit 0 ;;
  "-u strata-ab-engine --after-cursor "*" --no-pager -q") mark_ok "$4" || exit 1; tail -n +2 "$SB/journal.txt" 2>/dev/null; exit 0 ;;
  "-k --after-cursor "*" --no-pager -q") mark_ok "$3" || exit 1; tail -n +2 "$SB/kernel.txt" 2>/dev/null; exit 0 ;;
  *) echo "journalctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat > "$SB/bin/nvidia-smi" <<'EOF'
#!/bin/bash
# VRAM in use while a sandbox engine lives; gpu_stuck: GPU 0 stays at 500 MiB; gpu_other: one more GPU process
used=0; pgrep -f "^$SB/(tree|prod)/engine-cuda12/strata --serve" > /dev/null && used=5000
case "$*" in
  *compute-apps*) pgrep -f "^$SB/(tree|prod)/engine-cuda12/strata --serve"; cat "$SB/gpu_other" 2>/dev/null; exit 0 ;;
  *memory.used*) for i in 0 1 2 3; do if [ "$i" = 0 ] && [ -e "$SB/gpu_stuck" ]; then echo 500; else echo $used; fi; done ;;
  *memory.free*) for i in 0 1 2 3; do echo 900; done ;;
  # it waits with the builtin read on a FIFO that no one writes, not with sleep: a sleep it ran would outlive it
  # when the driver ends it, and the harness would find it left behind
  *dmon*) echo "#Time gpu sm mem"; [ -p "$SB/never" ] || mkfifo "$SB/never" 2> /dev/null; exec 9<> "$SB/never"
          while :; do for i in 0 1 2 3; do echo "$(date +%T) $i 50 10"; done; read -r -t 1 -u 9 _ || :; done ;;
esac
EOF
printf '#!/bin/bash\ncase "$*" in "") echo testhost-machine ;; -f) echo testhost-machine ;; -A) echo "nbname.netbird.cloud " ;; esac\n' > "$SB/bin/hostname"
chmod +x "$SB"/bin/*

prod_up() { PATH="$SB/bin:$PATH" systemctl start strata-q3.service; for _ in $(seq 100); do
              curl -fsS -m 1 "http://127.0.0.1:$PORT/health" > /dev/null 2>&1 && return 0; sleep 0.1; done; return 1; }
up() { PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service || prod_up; }   # the stub strata-q3 back up
drive() {   # drive <name> <arms> <steps...>: the driver in the sandbox, output in $SB/<name>.out
  local name=$1 arms=$2; shift 2
  env PATH="$SB/bin:$PATH" AB_ENGINE_PORT="$PORT" AB_ENGINE_PY=python3 AB_ENGINE_PROD_TREE="$SB/prod" \
      AB_ENGINE_CORPUS="$SB/corpus.txt" AB_ENGINE_ADMIT_DELAY=0.8 AB_ENGINE_IDLE_WAIT_S=${IDLE:-20} \
      AB_ENGINE_BUSY_UNDER="$SB" AB_ENGINE_PREFLIGHT_RE="^$SB/[^ ]*engine-cuda12/strata --serve" \
      AB_ENGINE_STRAY_WAIT_S=${STRAY_WAIT:-20} AB_ENGINE_FREE_WAIT_S=${FREE_WAIT:-20} \
      ${RESCUE:+AB_ENGINE_RESCUE_S=$RESCUE} bash "$SB/repo/bench/ab_engine.sh" "$arms" "$@" > "$SB/$name.out" 2>&1
}
R=$SB/repo/results
prod_pid() { cat "$SB/units/strata-q3.service.pid" 2>/dev/null; }

# 1. two clean runs: the checks pass after the warm-up, the records, strata-q3 back at the end
prod_up || { fail "the fake strata-q3 did not start"; exit 1; }
drive clean "$SB/arms.json" c0-1:C0 c1-1:C1; rc=$?
[ $rc = 0 ] && pass "two clean runs exit 0" || { fail "two clean runs exit $rc"; tail -5 "$SB/clean.out"; }
for t in c0-1 c1-1; do
  grep -q "^arm check: ok" "$R/$t-arm.txt" 2>/dev/null && pass "$t: arm check ok" || fail "$t: $(grep 'arm check' "$R/$t-arm.txt" 2>/dev/null)"
  grep -q "^same engine as before: yes" "$R/$t-after.txt" 2>/dev/null && pass "$t: same engine" || fail "$t: same engine"
  grep -qE "^requests counted by /metrics: ([0-9]+)  sent: \1$" "$R/$t-after.txt" && pass "$t: counted = sent" ||
    fail "$t: $(grep 'requests counted' "$R/$t-after.txt")"
done
grep -q "prompt MMQ sorts the full expert groups" "$R/c1-1-arm.txt" && pass "C1's group-sort line is in its record" ||
  fail "C1's group-sort line is missing from its record"
! grep -q "prompt MMQ sorts the full expert groups" "$R/c0-1-arm.txt" && pass "C0 has no group-sort line" || fail "C0 has a group-sort line"
grep -q "STRATA_MMQ_GROUP_SORT=1" "$R/c1-1-arm.txt" && pass "C1's engine environment recorded" || fail "C1's environment not recorded"
[ "$(grep -c '"text"' "$R/c0-1-texts.jsonl" 2>/dev/null)" = 24 ] && pass "every answer's text is recorded" || fail "texts: $(wc -l < "$R/c0-1-texts.jsonl" 2>/dev/null)"
[ "$(grep -c ' 0 50 10$' "$R/c0-1-gpu.txt" 2>/dev/null)" -ge 1 ] && ! pgrep -f "$SB/bin/nvidia-smi dmon" > /dev/null 2>&1 &&
  pass "each GPU's use is recorded during the screen, and the recorder is stopped" || fail "no GPU record, or the recorder still runs"
[ "$(grep -c '"tag": "c0-1"' "$R/engine-screen.jsonl")" -gt 5 ] && pass "c0-1's screen records" || fail "c0-1 has no screen records"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "strata-q3 runs again at the end" || fail "strata-q3 is not running at the end"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-ab-engine && fail "the test unit still runs" || pass "the test unit is stopped"
grep -q "^timer strata-ab-rescue" "$SB/actions" && grep -q "^stop strata-ab-rescue.timer" "$SB/actions" &&
  ! { [ -e "$SB/units/strata-ab-rescue.timer.pid" ] && kill -0 "$(cat "$SB/units/strata-ab-rescue.timer.pid")" 2>/dev/null; } &&
  pass "the rescue timer was armed and is stopped after a normal run" || fail "the rescue timer: $(grep rescue "$SB/actions" | tail -3)"
python3 -B "$SB/repo/bench/ab_engine_summary.py" --results "$R" --baseline C0 c0-1 c1-1 > "$SB/sum.out"; rc=$?
[ $rc = 0 ] && grep -q "differ from the baseline on 0 of the" "$SB/sum.out" && pass "the summary: usable, C1's answers equal C0's" ||
  { fail "the summary exit $rc"; tail -5 "$SB/sum.out"; }
if grep -rqi -e testhost -e nbname "$R"; then fail "a host name is left in results/"; else pass "no host name in results/"; fi

# 2. an arms file whose config does not say what the arm says: refused before anything is stopped
before=$(prod_pid)
drive badspec "$SB/arms-bad.json" c0-9:C0; rc=$?
[ $rc = 2 ] && grep -q "does not check out" "$SB/badspec.out" && [ "$(prod_pid)" = "$before" ] &&
  pass "a spec that does not match its config is refused, strata-q3 untouched" || fail "bad spec: exit $rc, $(tail -2 "$SB/badspec.out")"

up
# 3. a switch that did not take (no group-sort line): the step stops, strata-q3 comes back
FAKE_NO_GS=1 drive nogs "$SB/arms.json" c1-2:C1; rc=$?
[ $rc = 1 ] && grep -q "STOP: c1-2 is not the arm C1" "$SB/nogs.out" && pass "a switch that did not take stops the step" ||
  fail "no group-sort line: exit $rc, $(tail -3 "$SB/nogs.out")"
! grep -q '"tag": "c1-2"' "$R/engine-screen.jsonl" && pass "... and nothing was measured" || fail "c1-2 was measured"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "... and strata-q3 runs again" || fail "strata-q3 not restored after a STOP"

up
# 4. another client's request inside the measurement: counted > sent, and the summary leaves the run out
FAKE_EXTRA=1 drive extra "$SB/arms.json" c0-3:C0; rc=$?
grep -qE "^requests counted by /metrics: ([0-9]+)  sent: ([0-9]+)$" "$R/c0-3-after.txt" &&
  ! grep -qE "^requests counted by /metrics: ([0-9]+)  sent: \1$" "$R/c0-3-after.txt" && pass "other traffic shows as counted != sent" ||
  fail "other traffic: $(grep 'requests counted' "$R/c0-3-after.txt")"
python3 -B "$SB/repo/bench/ab_engine_summary.py" --results "$R" --baseline C0 c0-3 > "$SB/sum2.out"; rc=$?
[ $rc = 2 ] && grep -q "/metrics counted" "$SB/sum2.out" && pass "the summary leaves that run out" || fail "summary of other traffic: exit $rc"

# 5. a busy service: refused after the idle wait, strata-q3 untouched
PATH="$SB/bin:$PATH" systemctl stop strata-q3.service
FAKE_BUSY=1 prod_up
before=$(prod_pid)
IDLE=2 drive busy "$SB/arms.json" c0-4:C0; rc=$?
[ $rc = 1 ] && grep -q "has been busy" "$SB/busy.out" && [ "$(prod_pid)" = "$before" ] &&
  pass "a busy service is not stopped" || fail "busy: exit $rc, $(tail -2 "$SB/busy.out")"
PATH="$SB/bin:$PATH" systemctl stop strata-q3.service; prod_up

up
# 6. an interrupt during the measurement: exit 130, the test unit stopped, strata-q3 back
env PATH="$SB/bin:$PATH" AB_ENGINE_PORT="$PORT" AB_ENGINE_PY=python3 AB_ENGINE_PROD_TREE="$SB/prod" \
    AB_ENGINE_CORPUS="$SB/corpus.txt" AB_ENGINE_ADMIT_DELAY=3 AB_ENGINE_IDLE_WAIT_S=20 AB_ENGINE_BUSY_UNDER="$SB" \
    AB_ENGINE_PREFLIGHT_RE="^$SB/[^ ]*engine-cuda12/strata --serve" AB_ENGINE_STRAY_WAIT_S=20 \
    bash "$SB/repo/bench/ab_engine.sh" "$SB/arms.json" c0-5:C0 > "$SB/int.out" 2>&1 &
wpid=$!
for _ in $(seq 600); do grep -q '"tag": "c0-5"' "$R/engine-screen.jsonl" 2>/dev/null && break; sleep 0.1; done
kill -TERM "$wpid"
for _ in $(seq 150); do kill -0 "$wpid" 2>/dev/null || break; sleep 0.2; done     # at most 30 s, not forever
if kill -0 "$wpid" 2>/dev/null; then
  kill -9 "$wpid"; wait "$wpid"; rc=timeout
else
  wait "$wpid"; rc=$?
fi
[ "$rc" = 130 ] && pass "an interrupt exits 130" || fail "an interrupt exits $rc: $(tail -2 "$SB/int.out")"
sleep 1; ! pgrep -f "$SB/bin/nvidia-smi dmon" > /dev/null 2>&1 && pass "... the GPU recorder is stopped" || fail "the GPU recorder runs after an interrupt"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-ab-engine && fail "the test unit runs after an interrupt" || pass "... the test unit is stopped"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "... and strata-q3 runs again" || fail "strata-q3 not restored after an interrupt"

up
# 7. a tag that is used: refused
drive reuse "$SB/arms.json" c0-1:C0; rc=$?
[ $rc = 2 ] && grep -q "exists (pick a new tag)" "$SB/reuse.out" && pass "a used tag is refused" || fail "reuse: exit $rc"

up
# 8. an engine without the arm's environment (a server.py that dropped it; its log still follows the config): the
# environment check alone stops the step before anything is measured
FAKE_ENV_DROP=STRATA_MMQ_GROUP_SORT drive envdrop "$SB/arms.json" c1-6:C1; rc=$?
[ $rc = 1 ] && grep -q "STOP: c1-6 is not the arm C1; env STRATA_MMQ_GROUP_SORT is ''" "$SB/envdrop.out" &&
  pass "an engine without the arm's environment stops the step" || fail "env drop: exit $rc, $(tail -3 "$SB/envdrop.out")"
! grep -q '"tag": "c1-6"' "$R/engine-screen.jsonl" && pass "... and nothing was measured" || fail "c1-6 was measured"

up
# 9. the engine restarts during the measurement: the step stops, and its record says so
FAKE_RESTART=4 drive restart "$SB/arms.json" c0-7:C0; rc=$?
[ $rc = 1 ] && grep -q "STOP: the engine restarted during c0-7" "$SB/restart.out" &&
  grep -q "^same engine as before: NO" "$R/c0-7-after.txt" 2>/dev/null &&
  pass "an engine restart during the measurement stops the step" || fail "restart: exit $rc, $(tail -3 "$SB/restart.out")"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "... and strata-q3 runs again" || fail "strata-q3 not restored after a restart"

up
# 10. a measurement running (a driver2.py of this sandbox): refused before anything is stopped
printf 'import time\ntime.sleep(300)\n' > "$SB/driver2.py"
python3 "$SB/driver2.py" & bpid=$!
before=$(prod_pid)
drive measuring "$SB/arms.json" c0-8:C0; rc=$?
kill "$bpid" 2>/dev/null; wait "$bpid" 2>/dev/null
[ $rc = 1 ] && grep -q "a measurement or an evaluation is running" "$SB/measuring.out" && [ "$(prod_pid)" = "$before" ] &&
  pass "a running measurement is refused, strata-q3 untouched" || fail "measuring: exit $rc, $(tail -2 "$SB/measuring.out")"

up
# 11. a stray engine command line (a harness's fake engine left behind) when the experiment would start: the preflight
# would not start strata-q3 again, so nothing is stopped
mkdir -p "$SB/stray/engine-cuda12"; cp "$SB/tree/engine-cuda12/strata" "$SB/stray/engine-cuda12/strata"
setsid "$SB/stray/engine-cuda12/strata" --serve --fake > /dev/null 2>&1 & spid=$!
before=$(prod_pid)
drive stray "$SB/arms.json" c0-9:C0; rc=$?
[ $rc = 1 ] && grep -q "strata-preflight.sh would not start strata-q3 again" "$SB/stray.out" && [ "$(prod_pid)" = "$before" ] &&
  ! grep -q '"tag": "c0-9"' "$R/engine-screen.jsonl" && pass "a stray engine command line is refused, strata-q3 untouched" ||
  fail "stray: exit $rc, $(tail -2 "$SB/stray.out")"

up
# 12. a stray that appears at the last phase of the measurement and is gone 6 s later: the restart waits for it, then
# strata-q3 starts
kill "$spid" 2>/dev/null; wait "$spid" 2>/dev/null
last_phase() { grep "\"tag\": \"$1\"" "$R/engine-screen.jsonl" 2>/dev/null | grep -q '"phase": "turns"'; }
( for _ in $(seq 900); do last_phase c0-10 && break; sleep 0.1; done
  setsid "$SB/stray/engine-cuda12/strata" --serve --fake > /dev/null 2>&1 & p=$!; sleep 6; kill "$p" ) &
drive straylate "$SB/arms.json" c0-10:C0; rc=$?
wait
[ $rc = 0 ] && PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && ! grep -q "preflight refused" "$SB/actions" &&
  pass "a passing stray is waited for, then strata-q3 starts" || fail "late stray: exit $rc, $(tail -3 "$SB/straylate.out")"

up
# 13. a stray that stays: strata-q3 cannot start, and the driver names the process and the command to run
( for _ in $(seq 900); do last_phase c0-11 && break; sleep 0.1; done
  setsid "$SB/stray/engine-cuda12/strata" --serve --fake > /dev/null 2>&1 & echo $! > "$SB/stray2.pid" ) &
STRAY_WAIT=2 drive straystays "$SB/arms.json" c0-11:C0; rc=$?
wait; kill "$(cat "$SB/stray2.pid")" 2>/dev/null
grep -q "will refuse to start strata-q3 while these run" "$SB/straystays.out" && grep -q "$SB/stray/engine-cuda12/strata" "$SB/straystays.out" &&
  grep -q "preflight refused" "$SB/actions" && pass "a stray that stays is named with the command to start strata-q3" ||
  fail "stray stays: exit $rc, $(tail -3 "$SB/straystays.out")"
prod_up > /dev/null 2>&1 || true
summary_of() { python3 -B "$SB/repo/bench/ab_engine_summary.py" --results "$R" --baseline C0 "$@" > "$SB/sum-$1.out" 2>&1; }
lastp() { last_phase "$1"; }

# 14. strata-q3 not running at the start: someone else's state, nothing is stopped or started
PATH="$SB/bin:$PATH" systemctl stop strata-q3.service
n0=$(wc -l < "$SB/actions")
drive down "$SB/arms.json" c0-12:C0; rc=$?
[ $rc = 1 ] && grep -q "refusing: strata-q3 is inactive" "$SB/down.out" && [ "$(wc -l < "$SB/actions")" = "$n0" ] &&
  ! PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "a stopped strata-q3 is refused and left as it is" ||
  fail "strata-q3 down: exit $rc, $(tail -2 "$SB/down.out")"
up

# 15. another process on the GPUs: refused before anything is stopped
echo 4242 > "$SB/gpu_other"; before=$(prod_pid)
drive gpuother "$SB/arms.json" c0-13:C0; rc=$?
rm -f "$SB/gpu_other"
[ $rc = 1 ] && grep -q "other processes use the GPUs" "$SB/gpuother.out" && [ "$(prod_pid)" = "$before" ] &&
  pass "another GPU process is refused, strata-q3 untouched" || fail "gpu other: exit $rc, $(tail -2 "$SB/gpuother.out")"

# 16. a tag given twice in one call, and arms whose configs differ in more than the arm's settings: refused
drive twice "$SB/arms.json" c0-14:C0 c0-14:C1; rc=$?
[ $rc = 2 ] && grep -q "given twice" "$SB/twice.out" && pass "a tag given twice is refused" || fail "twice: exit $rc"
python3 - "$SB" <<'PYEOF'
import json, sys
sb = sys.argv[1]
c = json.load(open(f"{sb}/tree/cfg-gs1.json")); c["args"] = c["args"] + ["--prefill", "1024"]
json.dump(c, open(f"{sb}/tree/cfg-gs1x.json", "w"))
a = json.load(open(f"{sb}/arms.json")); a["arms"]["C1"]["config"] = "cfg-gs1x.json"
json.dump(a, open(f"{sb}/arms-diff.json", "w"))
PYEOF
drive diffcfg "$SB/arms-diff.json" c0-15:C0; rc=$?
[ $rc = 2 ] && grep -q "differ in more than" "$SB/diffcfg.out" && pass "arms whose configs differ elsewhere are refused" ||
  fail "configs differ: exit $rc, $(tail -2 "$SB/diffcfg.out")"

# 17. the journal cannot be read: the record says so and the summary leaves the run out
touch "$SB/journal_fail"
drive nojournal "$SB/arms.json" c0-16:C0; rc=$?
rm -f "$SB/journal_fail"
summary_of c0-16; src=$?
[ $rc = 0 ] && grep -q "^unreadable" "$R/c0-16-after.txt" && [ $src = 2 ] && grep -q "journal unreadable" "$SB/sum-c0-16.out" &&
  pass "an unreadable journal is recorded and makes the run unusable" || fail "journal: exit $rc, summary $src"
up

# 18. another client's long reply (8 s, from the admission phase on) still running when the screen ends: counted once
# the unit is idle, so it shows
admit_phase() { grep "\"tag\": \"$1\"" "$R/engine-screen.jsonl" 2>/dev/null | grep -q '"phase": "admit"'; }
( for _ in $(seq 900); do admit_phase c0-17 && break; sleep 0.1; done
  curl -s -m 20 -H 'Content-Type: application/json' -d '{"messages": [{"role": "user", "content": "[slow] hello"}], "stream": true}' \
    "http://127.0.0.1:$PORT/v1/chat/completions" > /dev/null ) &
drive late "$SB/arms.json" c0-17:C0; rc=$?
wait
summary_of c0-17; src=$?
grep -q "^idle before the count: yes" "$R/c0-17-after.txt" && [ $src = 2 ] && grep -q "/metrics counted" "$SB/sum-c0-17.out" &&
  pass "a reply still running at the end is counted and the run left out" ||
  fail "late reply: exit $rc, summary $src, $(grep 'requests counted' "$R/c0-17-after.txt")"
up

# 19. an engine error line after the warm-up: counted (the start-up verify line is not), and the run left out
FAKE_ENGINE_ERR=6 drive engerr "$SB/arms.json" c0-18:C0; rc=$?
summary_of c0-18; src=$?
grep -q "^engine log error lines: 1$" "$R/c0-18-after.txt" && grep -q "^engine log error lines: 0$" "$R/c0-1-after.txt" &&
  [ $src = 2 ] && grep -q "engine error lines" "$SB/sum-c0-18.out" &&
  pass "an engine error line is counted, the verify line is not, and the run is left out" ||
  fail "engine error: exit $rc, summary $src, $(grep 'engine log error' "$R/c0-18-after.txt")"
up

# 20. a GPU that stays busy after the stop: strata-q3 is started anyway, with the reason printed
( for _ in $(seq 900); do lastp c0-19 && break; sleep 0.1; done; touch "$SB/gpu_stuck" ) &
FREE_WAIT=4 drive stuck "$SB/arms.json" c0-19:C0; rc=$?
wait; rm -f "$SB/gpu_stuck"
grep -q "ERROR: not everything is free" "$SB/stuck.out" && PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service &&
  pass "a GPU still busy at the end does not keep strata-q3 down" || fail "stuck GPU: exit $rc, $(tail -3 "$SB/stuck.out")"
up

# 21. a second signal while the restore runs: ignored, and strata-q3 runs at the end
env PATH="$SB/bin:$PATH" AB_ENGINE_PORT="$PORT" AB_ENGINE_PY=python3 AB_ENGINE_PROD_TREE="$SB/prod" \
    AB_ENGINE_CORPUS="$SB/corpus.txt" AB_ENGINE_ADMIT_DELAY=0.8 AB_ENGINE_IDLE_WAIT_S=20 AB_ENGINE_BUSY_UNDER="$SB" \
    AB_ENGINE_PREFLIGHT_RE="^$SB/[^ ]*engine-cuda12/strata --serve" AB_ENGINE_STRAY_WAIT_S=30 AB_ENGINE_FREE_WAIT_S=20 \
    bash "$SB/repo/bench/ab_engine.sh" "$SB/arms.json" c0-20:C0 > "$SB/twosig.out" 2>&1 &
wpid=$!
for _ in $(seq 900); do lastp c0-20 && break; sleep 0.1; done
setsid "$SB/stray/engine-cuda12/strata" --serve --fake > /dev/null 2>&1 & spid=$!   # keeps the restore waiting
kill -TERM "$wpid"; sleep 2; kill -TERM "$wpid"; sleep 1; kill "$spid" 2>/dev/null
for _ in $(seq 300); do kill -0 "$wpid" 2>/dev/null || break; sleep 0.2; done
kill -0 "$wpid" 2>/dev/null && { kill -9 "$wpid"; rc=timeout; } || { wait "$wpid"; rc=$?; }
[ "$rc" = 130 ] && PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "a second signal does not stop the restore" ||
  fail "two signals: exit $rc, strata-q3 $(PATH="$SB/bin:$PATH" systemctl is-active strata-q3.service), $(tail -2 "$SB/twosig.out")"
up

# 22. the driver killed outright during the measurement: the rescue timer stops the test unit and starts strata-q3
env PATH="$SB/bin:$PATH" AB_ENGINE_PORT="$PORT" AB_ENGINE_PY=python3 AB_ENGINE_PROD_TREE="$SB/prod" \
    AB_ENGINE_CORPUS="$SB/corpus.txt" AB_ENGINE_ADMIT_DELAY=0.8 AB_ENGINE_IDLE_WAIT_S=20 AB_ENGINE_BUSY_UNDER="$SB" \
    AB_ENGINE_PREFLIGHT_RE="^$SB/[^ ]*engine-cuda12/strata --serve" AB_ENGINE_RESCUE_S=8 \
    bash "$SB/repo/bench/ab_engine.sh" "$SB/arms.json" c0-21:C0 > "$SB/killed.out" 2>&1 &
wpid=$!
for _ in $(seq 900); do grep -q '"tag": "c0-21"' "$R/engine-screen.jsonl" 2>/dev/null && break; sleep 0.1; done
kill -9 "$wpid"; wait "$wpid" 2>/dev/null
sb_pkill TERM "^python3 $SB/repo/bench/engine_screen.py"                # its screen, orphaned
for _ in $(seq 150); do PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && break; sleep 0.2; done
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && ! PATH="$SB/bin:$PATH" systemctl is-active -q strata-ab-engine &&
  grep -q "rescue fired" "$SB/actions" && pass "a killed driver is rescued by the timer" || fail "rescue: $(tail -3 "$SB/actions")"
up

# 23. an arm with engine arguments of its own, declared as args_add: accepted (case 16: undeclared, refused)
python3 - "$SB" <<'PYEOF'
import json, sys
sb = sys.argv[1]
a = json.load(open(f"{sb}/arms-diff.json")); a["arms"]["C1"]["args_add"] = ["--prefill", "1024"]
json.dump(a, open(f"{sb}/arms-args.json", "w"))
PYEOF
drive argsok "$SB/arms-args.json" c1-22:C1; rc=$?
[ $rc = 0 ] && grep -q '"tag": "c1-22"' "$R/engine-screen.jsonl" && pass "an arm's declared args_add is accepted" ||
  fail "args_add: exit $rc, $(tail -2 "$SB/argsok.out")"
up

# 24. the remote side of the evaluation beside a run: allowed on one CPU, refused unpinned
printf 'import time\ntime.sleep(300)\n' > "$SB/driver2.py"
taskset -c 0 python3 "$SB/driver2.py" --endpoint remote & rpid=$!
drive pinned "$SB/arms.json" c0-23:C0; rc=$?
kill "$rpid" 2>/dev/null; wait "$rpid" 2>/dev/null
[ $rc = 0 ] && pass "a remote evaluation pinned to one CPU may run beside" || fail "pinned remote: exit $rc, $(tail -2 "$SB/pinned.out")"
up
python3 "$SB/driver2.py" --endpoint remote & rpid=$!
drive unpinned "$SB/arms.json" c0-24:C0; rc=$?
kill "$rpid" 2>/dev/null; wait "$rpid" 2>/dev/null
[ $rc = 1 ] && grep -q "a measurement or an evaluation is running" "$SB/unpinned.out" &&
  pass "an unpinned remote evaluation is refused" || fail "unpinned remote: exit $rc, $(tail -2 "$SB/unpinned.out")"
up

# 25. a screen with the opt-in phases: the driver counts what the screen says it sent, the summary takes the runs
ENGINE_SCREEN_PHASES=prefill,decode,four,admit,turns,decode4,eval4,admit28 drive extphases "$SB/arms.json" c0-25:C0 c1-25:C1; rc=$?
python3 -B "$SB/repo/bench/ab_engine_summary.py" --results "$R" --baseline C0 c0-25 c1-25 > "$SB/sum-ext.out"; src=$?
[ $rc = 0 ] && grep -qE "^requests counted by /metrics: 45  sent: 45$" "$R/c0-25-after.txt" && grep -q "^screen phases: prefill,decode,four,admit,turns,decode4,eval4,admit28" "$R/c0-25-arm.txt" &&
  [ $src = 0 ] && grep -q "4 users decoding, short prompts" "$SB/sum-ext.out" && pass "a screen with the opt-in phases is counted and summarized" ||
  fail "opt-in phases: exit $rc, summary $src, $(grep 'requests counted' "$R/c0-25-after.txt" 2>/dev/null)"
up

# 26. the arms file's fields: a missing must or env, or a misspelt field, is refused before anything is stopped; an arm
# without the optional must_not runs, with no error output (it once printed a KeyError and checked nothing more)
python3 - "$SB" <<'PYEOF'
import json, sys
sb = sys.argv[1]
a = json.load(open(f"{sb}/arms.json"))
def variant(name, edit):
    b = json.loads(json.dumps(a)); edit(b["arms"]["C1"]); json.dump(b, open(f"{sb}/arms-{name}.json", "w"))
variant("nomust", lambda c: c.pop("must"))
variant("noenv", lambda c: c.pop("env"))
variant("typo", lambda c: c.__setitem__("must-not", c.pop("must_not")))
variant("nomustnot", lambda c: c.pop("must_not"))
PYEOF
for v in nomust noenv typo; do
  before=$(prod_pid)
  drive "spec-$v" "$SB/arms-$v.json" c1-26:C1; rc=$?
  [ $rc = 2 ] && grep -q "does not check out: C1: " "$SB/spec-$v.out" && [ "$(prod_pid)" = "$before" ] &&
    pass "an arms file with $v is refused, strata-q3 untouched" || fail "arms $v: exit $rc, $(tail -2 "$SB/spec-$v.out")"
done
drive nomustnot "$SB/arms-nomustnot.json" c1-26:C1; rc=$?
[ $rc = 0 ] && ! grep -q Traceback "$SB/nomustnot.out" && grep -q "^arm check: ok" "$R/c1-26-arm.txt" &&
  pass "an arm without must_not runs, with no error output" || fail "no must_not: exit $rc, $(grep -m1 -A2 Traceback "$SB/nomustnot.out")"
up

# 27. an interrupt while the test unit is still loading (not listening): it is stopped at once, with no error output,
# and strata-q3 comes back (it once waited 60 s for a reply that could not exist, printing a traceback per poll:
# results/ab_engine-combo-20261019.log)
n0=$(grep -c '^systemd-run strata-ab-engine' "$SB/actions" 2>/dev/null)
( for _ in $(seq 300); do [ "$(grep -c '^systemd-run strata-ab-engine' "$SB/actions")" -gt "${n0:-0}" ] && break; sleep 0.1; done
  sleep 2; sb_pkill TERM "^bash $SB/repo/bench/ab_engine[.]sh" ) &       # the test unit started, still loading
t0=$(date +%s)
FAKE_LOAD_S=30 drive loading "$SB/arms.json" c0-27:C0; rc=$?
took=$(( $(date +%s) - t0 )); wait
[ $rc = 130 ] && grep -q "does not answer (still loading" "$SB/loading.out" && ! grep -q Traceback "$SB/loading.out" &&
  [ "$took" -lt 45 ] && PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service &&
  pass "an interrupt during the load stops the test unit at once, with no error output" ||
  fail "interrupt during the load: exit $rc, ${took} s, $(grep -m1 -E 'Traceback|still serving' "$SB/loading.out")"
up

# 28. the test unit's journal lines and the kernel's, from the driver's own mark in the journal: the step's first lines
# are kept (--after-cursor lost them)
printf '%s\n' " 十  20 01:00:01 testhost-machine python[2]: [strata] 500 the step's first journal line" \
              " 十  20 01:00:02 testhost-machine python[2]: [strata] 503 the step's second journal line" > "$SB/journal.txt"
printf '%s\n' " 十  20 01:00:03 testhost-machine kernel: NVRM: Xid (PCI:0000:3b:00): 79, the step's first kernel line" \
              " 十  20 01:00:04 testhost-machine kernel: NVRM: Xid (PCI:0000:3b:00): 79, the step's second kernel line" > "$SB/kernel.txt"
drive window "$SB/arms.json" c0-28:C0; rc=$?
rm -f "$SB/journal.txt" "$SB/kernel.txt"
w=$(sed -n "/the test unit's journal lines/,\$p" "$R/c0-28-after.txt" 2>/dev/null)
grep -q "500 the step's first journal line" <<< "$w" && grep -q "503 the step's second journal line" <<< "$w" &&
  grep -q "the step's first kernel line" <<< "$w" && grep -q "the step's second kernel line" <<< "$w" &&
  pass "the step's journal and kernel lines are read from the driver's own mark: the first ones kept" ||
  fail "the step's windows: exit $rc, $w"
up

# 29. no mark of its own in the journal: the step stops before the test unit starts, and strata-q3 comes back
n0=$(grep -c '^systemd-run strata-ab-engine' "$SB/actions" 2>/dev/null)
STUB_NO_MARK=1 drive nomark "$SB/arms.json" c0-29:C0; rc=$?
[ $rc = 1 ] && grep -q "cannot mark the journal" "$SB/nomark.out" &&
  [ "$(grep -c '^systemd-run strata-ab-engine' "$SB/actions")" = "${n0:-0}" ] &&
  PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service &&
  pass "no mark in the journal: the step stops before the test unit starts, strata-q3 comes back" ||
  fail "no mark: exit $rc, $(tail -3 "$SB/nomark.out")"
up

# 30. an arm that sets the value of a flag every config holds once (args_set): accepted, and its engine gets the value.
# Refused before anything is stopped: its config holds another value; a config does not hold the flag, or holds it
# twice; the key is not a flag; an arm that does not set the flag holds another value than the others that do not;
# the configs differ elsewhere too; an args_add repeats a flag its config holds otherwise
python3 - "$SB" <<'PYEOF'
import json, sys
sb = sys.argv[1]
def cfg(name, args):
    c = json.load(open(f"{sb}/tree/cfg-gs1.json")); c["args"] = args; json.dump(c, open(f"{sb}/tree/{name}.json", "w"))
cfg("cfg-gs1b", ["--batch", "2"])
cfg("cfg-gs1twice", ["--batch", "2", "--batch", "2"])
cfg("cfg-gs1else", ["--batch", "2", "--prefill", "1024"])
cfg("cfg-gs1add", ["--batch", "2", "--prefill", "1024", "--prefill", "512"])
c = json.load(open(f"{sb}/tree/cfg-gs0.json")); c["args"] = ["--batch", "3"]; json.dump(c, open(f"{sb}/tree/cfg-gs0c.json", "w"))
a = json.load(open(f"{sb}/arms.json")); a["arms"]["C1"]["config"] = "cfg-gs1b.json"
def variant(name, aset, edit=None):
    b = json.loads(json.dumps(a)); b["arms"]["C1"]["args_set"] = aset
    if edit:
        edit(b)
    json.dump(b, open(f"{sb}/arms-set-{name}.json", "w"))
variant("ok", {"--batch": "2"})
variant("value", {"--batch": "3"})
variant("absent", {"--batch": "2", "--prefill": "1024"})
variant("noflag", {"batch": "2"})
variant("twice", {"--batch": "2"}, lambda b: b["arms"]["C1"].__setitem__("config", "cfg-gs1twice.json"))
variant("unsetdiffer", {"--batch": "2"}, lambda b: b["arms"].__setitem__("C2", {**b["arms"]["C0"], "config": "cfg-gs0c.json"}))
variant("elsewhere", {"--batch": "2"}, lambda b: b["arms"]["C1"].__setitem__("config", "cfg-gs1else.json"))
variant("addrepeat", {"--batch": "2"}, lambda b: b["arms"]["C1"].update(config="cfg-gs1add.json", args_add=["--prefill", "512"]))
PYEOF
FAKE_ENGINE_ARGS=1 drive setok "$SB/arms-set-ok.json" c1-30:C1; rc=$?
[ $rc = 0 ] && grep -q '"tag": "c1-30"' "$R/engine-screen.jsonl" && grep -q -- "--serve --batch 2 *$" "$R/c1-30-arm.txt" &&
  pass "an arm's declared args_set is accepted, and its engine gets the value" ||
  fail "args_set: exit $rc, $(grep 'engine args' "$R/c1-30-arm.txt" 2>/dev/null; tail -2 "$SB/setok.out")"
up
for v in "value:C1: the config's --batch is 2, not 3" "absent:C0: the config's args hold --prefill 0 times" \
         "noflag:C1: args_set must map flags to values" "twice:C1: the config's args hold --batch 2 times" \
         "unsetdiffer:the arms that do not set --batch hold it with different values" \
         "elsewhere:the arms' configs differ in more than" \
         "addrepeat:C1: its args_add's --prefill is in the config's args once more"; do
  n=${v%%:*}; before=$(prod_pid)
  drive "set-$n" "$SB/arms-set-$n.json" c1-30r:C1; rc=$?
  [ $rc = 2 ] && grep -q -- "${v#*:}" "$SB/set-$n.out" && [ "$(prod_pid)" = "$before" ] &&
    pass "an args_set with $n is refused, strata-q3 untouched" || fail "args_set $n: exit $rc, $(tail -2 "$SB/set-$n.out")"
done
up

# 31. an arm's config changed while an earlier step ran: the next step stops before it starts, nothing of it is
# measured, and strata-q3 comes back
cp "$SB/tree/cfg-gs1b.json" "$SB/cfg-gs1b.saved"
( for _ in $(seq 600); do grep -q '"tag": "c0-31"' "$R/engine-screen.jsonl" 2>/dev/null && break; sleep 0.1; done
  python3 -c 'import json, sys; p = sys.argv[1]; c = json.load(open(p)); c["model_name"] = "changed"; json.dump(c, open(p, "w"))' \
    "$SB/tree/cfg-gs1b.json" ) &
drive changed "$SB/arms-set-ok.json" c0-31:C0 c1-31:C1; rc=$?
wait
[ $rc = 1 ] && grep -q "STOP: the arms file or an arm's config changed since the check at the start" "$SB/changed.out" &&
  ! grep -q '"tag": "c1-31"' "$R/engine-screen.jsonl" && PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service &&
  pass "a config changed during an earlier step stops the next step, strata-q3 comes back" ||
  fail "changed config: exit $rc, $(tail -3 "$SB/changed.out")"
cp "$SB/cfg-gs1b.saved" "$SB/tree/cfg-gs1b.json"
up

echo "$fails failed"
exit $((fails > 0))
