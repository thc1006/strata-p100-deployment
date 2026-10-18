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
killall_sb() { for f in "$SB"/units/*.pid; do [ -e "$f" ] && kill -- -"$(cat "$f")" 2>/dev/null; done; rm -f "$SB"/units/*.pid
               pkill -f "^$SB/stray/engine-cuda12/strata" 2>/dev/null; }
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
         setsid python3 "$SB/prod/serve/server.py" --engine strata --config "$SB/prod/prod.json" --port "$PORT" > /dev/null 2>&1 &
         echo $! > "$SB/units/strata-q3.service.pid" ;;
  *) echo "systemctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat > "$SB/bin/systemd-run" <<'EOF'
#!/bin/bash
unit=""; envs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --unit=*) unit=${1#--unit=}; shift ;;
    --collect|--uid=*|--gid=*) shift ;;
    -p) shift 2 ;;
    -E) envs+=("$2"); shift 2 ;;
    *) break ;;
  esac
done
echo "systemd-run $unit $*" >> "$SB/actions"
setsid env "${envs[@]}" "$@" > /dev/null 2>&1 &
echo $! > "$SB/units/$unit.pid"
EOF
cat > "$SB/bin/journalctl" <<'EOF'
#!/bin/bash
case "$*" in
  *--show-cursor*) echo "x"; echo "-- cursor: s=CUR1" ;;
  *"-u strata-ab-engine --after-cursor s=CUR1"*) cat "$SB/journal.txt" 2>/dev/null ;;
  *"-k --after-cursor s=CUR1"*) cat "$SB/kernel.txt" 2>/dev/null ;;
  *) echo "journalctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat > "$SB/bin/nvidia-smi" <<'EOF'
#!/bin/bash
# VRAM in use while a sandbox engine lives
used=0; pgrep -f "^$SB/(tree|prod)/engine-cuda12/strata --serve" > /dev/null && used=5000
case "$*" in *memory.used*) for i in 0 1 2 3; do echo $used; done ;; *memory.free*) for i in 0 1 2 3; do echo 900; done ;; esac
EOF
printf '#!/bin/bash\ncase "$*" in "") echo testhost-machine ;; -f) echo testhost-machine ;; -A) echo "nbname.netbird.cloud " ;; esac\n' > "$SB/bin/hostname"
chmod +x "$SB"/bin/*

prod_up() { PATH="$SB/bin:$PATH" systemctl start strata-q3.service; for _ in $(seq 100); do
              curl -fsS -m 1 "http://127.0.0.1:$PORT/health" > /dev/null 2>&1 && return 0; sleep 0.1; done; return 1; }
drive() {   # drive <name> <arms> <steps...>: the driver in the sandbox, output in $SB/<name>.out
  local name=$1 arms=$2; shift 2
  env PATH="$SB/bin:$PATH" AB_ENGINE_PORT="$PORT" AB_ENGINE_PY=python3 AB_ENGINE_PROD_TREE="$SB/prod" \
      AB_ENGINE_CORPUS="$SB/corpus.txt" AB_ENGINE_ADMIT_DELAY=0.2 AB_ENGINE_IDLE_WAIT_S=${IDLE:-20} \
      AB_ENGINE_BUSY_UNDER="$SB" AB_ENGINE_PREFLIGHT_RE="^$SB/[^ ]*engine-cuda12/strata --serve" \
      AB_ENGINE_STRAY_WAIT_S=${STRAY_WAIT:-20} bash "$SB/repo/bench/ab_engine.sh" "$arms" "$@" > "$SB/$name.out" 2>&1
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
grep -q "prompt MMQ sorts the full expert groups" "$R/c1-1-arm.txt" && pass "C1's line, printed at its first prompt read, is in its record" ||
  fail "C1's group-sort line is missing from its record"
! grep -q "prompt MMQ sorts the full expert groups" "$R/c0-1-arm.txt" && pass "C0 has no group-sort line" || fail "C0 has a group-sort line"
grep -q "STRATA_MMQ_GROUP_SORT=1" "$R/c1-1-arm.txt" && pass "C1's engine environment recorded" || fail "C1's environment not recorded"
[ "$(grep -c '"tag": "c0-1"' "$R/engine-screen.jsonl")" -gt 5 ] && pass "c0-1's screen records" || fail "c0-1 has no screen records"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "strata-q3 runs again at the end" || fail "strata-q3 is not running at the end"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-ab-engine && fail "the test unit still runs" || pass "the test unit is stopped"
python3 -B "$SB/repo/bench/ab_engine_summary.py" --results "$R" --baseline C0 c0-1 c1-1 > "$SB/sum.out"; rc=$?
[ $rc = 0 ] && grep -q "differ from c0-1 (C0): 0" "$SB/sum.out" && pass "the summary: usable, C1's answers equal C0's" ||
  { fail "the summary exit $rc"; tail -5 "$SB/sum.out"; }
if grep -rqi -e testhost -e nbname "$R"; then fail "a host name is left in results/"; else pass "no host name in results/"; fi

# 2. an arms file whose config does not say what the arm says: refused before anything is stopped
before=$(prod_pid)
drive badspec "$SB/arms-bad.json" c0-9:C0; rc=$?
[ $rc = 2 ] && grep -q "does not check out" "$SB/badspec.out" && [ "$(prod_pid)" = "$before" ] &&
  pass "a spec that does not match its config is refused, strata-q3 untouched" || fail "bad spec: exit $rc, $(tail -2 "$SB/badspec.out")"

# 3. a switch that did not take (no group-sort line): the step stops, strata-q3 comes back
FAKE_NO_GS=1 drive nogs "$SB/arms.json" c1-2:C1; rc=$?
[ $rc = 1 ] && grep -q "STOP: c1-2 is not the arm C1" "$SB/nogs.out" && pass "a switch that did not take stops the step" ||
  fail "no group-sort line: exit $rc, $(tail -3 "$SB/nogs.out")"
! grep -q '"tag": "c1-2"' "$R/engine-screen.jsonl" && pass "... and nothing was measured" || fail "c1-2 was measured"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "... and strata-q3 runs again" || fail "strata-q3 not restored after a STOP"

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
PATH="$SB/bin:$PATH" systemctl is-active -q strata-ab-engine && fail "the test unit runs after an interrupt" || pass "... the test unit is stopped"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "... and strata-q3 runs again" || fail "strata-q3 not restored after an interrupt"

# 7. a tag that is used: refused
drive reuse "$SB/arms.json" c0-1:C0; rc=$?
[ $rc = 2 ] && grep -q "exists (pick a new tag)" "$SB/reuse.out" && pass "a used tag is refused" || fail "reuse: exit $rc"

# 8. an engine without the arm's environment (a server.py that dropped it; its log still follows the config): the
# environment check alone stops the step before anything is measured
FAKE_ENV_DROP=STRATA_MMQ_GROUP_SORT drive envdrop "$SB/arms.json" c1-6:C1; rc=$?
[ $rc = 1 ] && grep -q "STOP: c1-6 is not the arm C1; env STRATA_MMQ_GROUP_SORT is ''" "$SB/envdrop.out" &&
  pass "an engine without the arm's environment stops the step" || fail "env drop: exit $rc, $(tail -3 "$SB/envdrop.out")"
! grep -q '"tag": "c1-6"' "$R/engine-screen.jsonl" && pass "... and nothing was measured" || fail "c1-6 was measured"

# 9. the engine restarts during the measurement: the step stops, and its record says so
FAKE_RESTART=4 drive restart "$SB/arms.json" c0-7:C0; rc=$?
[ $rc = 1 ] && grep -q "STOP: the engine restarted during c0-7" "$SB/restart.out" &&
  grep -q "^same engine as before: NO" "$R/c0-7-after.txt" 2>/dev/null &&
  pass "an engine restart during the measurement stops the step" || fail "restart: exit $rc, $(tail -3 "$SB/restart.out")"
PATH="$SB/bin:$PATH" systemctl is-active -q strata-q3.service && pass "... and strata-q3 runs again" || fail "strata-q3 not restored after a restart"

# 10. a measurement running (a driver2.py of this sandbox): refused before anything is stopped
printf 'import time\ntime.sleep(300)\n' > "$SB/driver2.py"
python3 "$SB/driver2.py" & bpid=$!
before=$(prod_pid)
drive measuring "$SB/arms.json" c0-8:C0; rc=$?
kill "$bpid" 2>/dev/null; wait "$bpid" 2>/dev/null
[ $rc = 1 ] && grep -q "a measurement or an evaluation is running" "$SB/measuring.out" && [ "$(prod_pid)" = "$before" ] &&
  pass "a running measurement is refused, strata-q3 untouched" || fail "measuring: exit $rc, $(tail -2 "$SB/measuring.out")"

# 11. a stray engine command line (a harness's fake engine left behind) when the experiment would start: the preflight
# would not start strata-q3 again, so nothing is stopped
mkdir -p "$SB/stray/engine-cuda12"; cp "$SB/tree/engine-cuda12/strata" "$SB/stray/engine-cuda12/strata"
setsid "$SB/stray/engine-cuda12/strata" --serve --fake > /dev/null 2>&1 & spid=$!
before=$(prod_pid)
drive stray "$SB/arms.json" c0-9:C0; rc=$?
[ $rc = 1 ] && grep -q "strata-preflight.sh would not start strata-q3 again" "$SB/stray.out" && [ "$(prod_pid)" = "$before" ] &&
  ! grep -q '"tag": "c0-9"' "$R/engine-screen.jsonl" && pass "a stray engine command line is refused, strata-q3 untouched" ||
  fail "stray: exit $rc, $(tail -2 "$SB/stray.out")"

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

# 13. a stray that stays: strata-q3 cannot start, and the driver names the process and the command to run
( for _ in $(seq 900); do last_phase c0-11 && break; sleep 0.1; done
  setsid "$SB/stray/engine-cuda12/strata" --serve --fake > /dev/null 2>&1 & echo $! > "$SB/stray2.pid" ) &
STRAY_WAIT=2 drive straystays "$SB/arms.json" c0-11:C0; rc=$?
wait; kill "$(cat "$SB/stray2.pid")" 2>/dev/null
grep -q "will refuse to start strata-q3 while these run" "$SB/straystays.out" && grep -q "$SB/stray/engine-cuda12/strata" "$SB/straystays.out" &&
  grep -q "preflight refused" "$SB/actions" && pass "a stray that stays is named with the command to start strata-q3" ||
  fail "stray stays: exit $rc, $(tail -3 "$SB/straystays.out")"
prod_up > /dev/null 2>&1 || true

echo "$fails failed"
exit $((fails > 0))
