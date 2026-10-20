#!/usr/bin/env bash
# bench/run_robustness.sh against stubs: the sets it runs (stress_matrix.py, latency_suite.py, monitor.py and Strata's
# tools), journalctl and hostname are scripts in a sandbox, and HOME is the sandbox, so the engine log is a file there.
# Nothing here touches the service, the journal or the GPUs, and no fake engine runs.  Needs setsid.
# Usage: tests/run_robustness_stubs.sh [checkout]   (default: this one; prints PASS / FAIL lines, exits 1 on a FAIL)
set -u
W=${1:-$(cd "$(dirname "$0")/.." && pwd)}
SB=$(mktemp -d "${TMPDIR:-/tmp}/robust.XXXXXX")
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
trap 'rm -rf "$SB"' EXIT
[ -n "${KEEP_SB:-}" ] && trap - EXIT          # KEEP_SB=1 keeps the sandbox to look at
running() {  # running <text>: the pids of the processes whose command line holds <text> (procps is not in CI)
  python3 - "$1" <<'EOF'
import os, sys
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        continue
    if int(pid) != os.getpid() and sys.argv[1].encode() in cmd:
        print(pid)
EOF
}

# --- the sandbox
mkdir -p "$SB/bin" "$SB/repo/bench" "$SB/home/dev/strata/.venv/bin" "$SB/home/dev/strata/tools"
cp "$W/bench/run_robustness.sh" "$SB/repo/bench/"
R=$SB/repo/results
ENGLOG=$SB/home/dev/strata/strata-iq3_xxs.log
cat > "$SB/repo/bench/stress_matrix.py" <<'EOF'
import json, os, sys, time
args = sys.argv[1:]
out = args[args.index("--out") + 1]
tag = args[args.index("--tag") + 1]
for name in args[:args.index("--tag")]:
    print(f"[00:00:00] {name} ({tag})", flush=True)
    with open(out, "a") as f:
        f.write(json.dumps({"tag": tag, "scenario": name}) + "\n")
    if name == "boundary":           # the engine logs during the run, a host name in one of its lines
        with open(os.environ["STUB_ENGLOG"], "a") as f:
            f.write("strata serve: prompt 27942 tokens\nstrata serve: engine error: CUDA error on TESTHOST-MACHINE\n")
    if name == "soak":
        time.sleep(float(os.environ.get("STUB_SOAK_S", "0")))
EOF
cat > "$SB/repo/bench/latency_suite.py" <<'EOF'
import json, sys
args = sys.argv[1:]
print(f"[00:00:00] poisson ({args[args.index('--tag') + 1]})", flush=True)
print(f"  rate {args[args.index('--rate') + 1]}: goodput 87%, errors 0", flush=True)
with open(args[args.index("--out") + 1], "a") as f:
    f.write(json.dumps({"tag": args[args.index("--tag") + 1], "scenario": "poisson"}) + "\n")
EOF
cat > "$SB/repo/bench/monitor.py" <<'EOF'
import sys, time
if sys.argv[1] == "record":
    with open(sys.argv[2], "w") as f:
        f.write("t,gpu0_mem\n")
    while True:
        time.sleep(0.2)
print('{"samples": 1}')
EOF
cat > "$SB/home/dev/strata/.venv/bin/python" <<'EOF'
#!/bin/bash
tool=$1; shift
case $tool in
  tools/serve_load.py) echo "clients  total tok/s"; while [ $# -gt 0 ]; do [ "$1" = --json ] && echo '{}' > "$2"; shift; done ;;
  tools/needle_bench.py) echo "3 of 3 found"; while [ $# -gt 0 ]; do [ "$1" = --out ] && echo '{}' > "$2"; shift; done ;;
  tools/early_close_test.py) echo "solo: OK -> 'Blue '"; echo "batch: OK -> 'Blue '" ;;
  *) echo "stub python: unknown $tool" >&2; exit 1 ;;
esac
EOF
# the journal: lines of this host's names, a short label alone, in other cases too, and a line the filter leaves out
cat > "$SB/bin/journalctl" <<'EOF'
#!/bin/bash
echo "$*" > "$SB/journalctl.args"
echo " 十  20 08:45:29 testhost-machine python[1]: [strata] 400 invalid request: request exceeds the context window"
echo " 十  20 08:45:30 TESTHOST-MACHINE.lan python[1]: [strata] 500 internal error"
echo " 十  20 08:45:31 nbname.netbird.cloud systemd[1]: strata-q3.service: Scheduled restart job, restart counter is at 1."
echo " 十  20 08:45:32 testhost-machine python[1]: [strata] done: 3 tokens in 37 s (27.1 tok/s) (stop, cancel=False)"
echo " 十  20 08:45:33 NBNAME python[1]: [strata] 503 overloaded"
EOF
cat > "$SB/bin/hostname" <<'EOF'
#!/bin/bash
[ -n "${STUB_NO_HOST:-}" ] && exit 1
case "$*" in "") echo testhost-machine ;; -f) echo testhost-machine ;; -A) echo "testhost-machine.lan nbname.netbird.cloud testhost-machine.lan " ;; esac
EOF
chmod +x "$SB/bin"/* "$SB/home/dev/strata/.venv/bin/python"
export SB STUB_ENGLOG=$ENGLOG
run() {  # run <tag> [env ...]: the script in the sandbox, its output in $SB/<tag>.out
  local tag=$1; shift
  env HOME="$SB/home" PATH="$SB/bin:$PATH" "$@" bash "$SB/repo/bench/run_robustness.sh" "$tag" 1 > "$SB/$tag.out" 2>&1
}
reset() { rm -rf "$R"; printf 'strata serve: prompt 100 tokens\nstrata serve: engine error: before this run\n' > "$ENGLOG"; }

# 1. a run: every section in order, the monitor stopped and summarized
reset; run t-run; rc=$?
[ $rc = 0 ] && grep -qx "finished t-run robustness" "$SB/t-run.out" && pass "a run exits 0" ||
  { fail "a run exits $rc"; cat "$SB/t-run.out"; }
got=$(grep -o -E '^\[[0-9:]{8}\] [a-z]+' "$R/t-run-robustness.log" 2>/dev/null | cut -d' ' -f2 | tr '\n' ' ')
want="boundary disconnect multiturn budget qa overload needle early poisson poisson soak engine server done "
[ "$got" = "$want" ] && pass "the log has every section, in order" || fail "the log's sections: $got"
grep -qx '{"samples": 1}' "$R/t-run-robust-monitor-all.json" 2>/dev/null && pass "the monitor is summarized" ||
  fail "monitor summary: $(cat "$R/t-run-robust-monitor-all.json" 2>/dev/null)"
[ -z "$(running "$SB/repo/bench/monitor.py")" ] && pass "the monitor is stopped" || fail "the monitor is still running"
[ "$(cut -d' ' -f1-3,6 "$SB/journalctl.args" 2>/dev/null)" = "-u strata-q3.service --since --no-pager" ] &&
  grep -qE -- '--since [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} ' "$SB/journalctl.args" &&
  pass "the journal is read from the start of the run" || fail "journalctl got: $(cat "$SB/journalctl.args" 2>/dev/null)"

# 2. the journal's lines are kept with every name of this host as <host>, whole and in any case; no name is left
want=' 十  20 08:45:29 <host> python[1]: [strata] 400 invalid request: request exceeds the context window
 十  20 08:45:30 <host> python[1]: [strata] 500 internal error
 十  20 08:45:31 <host> systemd[1]: strata-q3.service: Scheduled restart job, restart counter is at 1.
 十  20 08:45:33 <host> python[1]: [strata] 503 overloaded'
got=$(sed -n '/server journal since this run/,/\] done$/p' "$R/t-run-robustness.log" 2>/dev/null | sed '1d;$d')
[ "$got" = "$want" ] && pass "the journal's lines are kept, every host name written as <host>" ||
  fail "the journal's lines: $got"
if grep -rqi -e testhost -e nbname "$R" "$SB/t-run.out"; then
  fail "a host name is left: $(grep -rli -e testhost -e nbname "$R" "$SB/t-run.out" | tr '\n' ' ')"
else pass "no host name is left in what the run wrote"; fi

# 3. the engine log: only the lines written during the run are counted
grep -qx "engine prompt lines (batched requests log more than one): 1" "$R/t-run-robustness.log" &&
  grep -qx "      1 strata serve: engine error: CUDA error on <host>" "$R/t-run-robustness.log" &&
  ! grep -q "before this run" "$R/t-run-robustness.log" &&
  pass "the engine log counts only the lines of this run" ||
  fail "engine log section: $(sed -n '/engine log since/,/server journal/p' "$R/t-run-robustness.log")"

# 4. refusals: a tag that exists; no host name to redact
run t-run; rc=$?
[ $rc = 2 ] && grep -q "refusing: .*t-run-robustness.log exists" "$SB/t-run.out" && pass "refuses: the tag exists" ||
  fail "a second run of t-run exits $rc: $(cat "$SB/t-run.out")"
reset; run t-nohost STUB_NO_HOST=1; rc=$?
[ $rc = 1 ] && grep -qx "refusing: no host name to redact" "$SB/t-nohost.out" && [ ! -e "$R/t-nohost-robustness.log" ] &&
  [ -z "$(running "$SB/repo/bench/monitor.py")" ] && pass "refuses: no host name to redact; no log, no monitor left" ||
  fail "without a host name: exit $rc, $(cat "$SB/t-nohost.out")"

# 5. a run stopped as bench/ab_model_run.sh stops it (TERM to its process group) keeps the lines written until then
reset
setsid env HOME="$SB/home" PATH="$SB/bin:$PATH" STUB_SOAK_S=60 bash "$SB/repo/bench/run_robustness.sh" t-stop 1 \
  > "$SB/t-stop.out" 2>&1 &
spid=$!
for _ in $(seq 100); do grep -q "soak (t-stop)" "$R/t-stop-robustness.log" 2>/dev/null && break; sleep 0.1; done
kill -TERM -- -"$spid" 2>/dev/null
for _ in $(seq 100); do kill -0 "$spid" 2>/dev/null || break; sleep 0.1; done
kill -0 "$spid" 2>/dev/null && { kill -KILL -- -"$spid" 2>/dev/null; fail "a stopped run did not end"; }
wait "$spid" 2>/dev/null
grep -q "rate 0.2: goodput" "$R/t-stop-robustness.log" 2>/dev/null && grep -q "soak (t-stop)" "$R/t-stop-robustness.log" &&
  pass "a stopped run keeps the lines written until then" ||
  fail "the log of a stopped run: $(cat "$R/t-stop-robustness.log" 2>/dev/null)"
[ -z "$(running "$SB/repo/bench/")" ] && pass "a stopped run leaves nothing running" || fail "a stopped run left processes"

echo "$fails failed"
exit $((fails > 0))
