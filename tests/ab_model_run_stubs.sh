#!/usr/bin/env bash
# bench/ab_model_run.sh against stubs: systemctl, curl, sudo, journalctl, nvidia-smi and hostname are scripts in a
# sandbox, the engine is a compiled program that only waits (its /proc exe, cmdline, environ and fd 2 are real), the
# service's checkout is a git copy of the worktree with run_profile.sh and host-units.sh replaced by stubs.
# Nothing here touches the real service, journal or sudo.  Needs gcc, git, setsid and pgrep.
# Usage: tests/ab_model_run_stubs.sh [checkout]   (default: this one; prints PASS / FAIL lines, exits 1 on a FAIL)
set -u
W=${1:-$(cd "$(dirname "$0")/.." && pwd)}
export SB; SB=$(mktemp -d "${TMPDIR:-/tmp}/abwrap.XXXXXX")
export AB_ARC_FILE=$SB/zfs_arc_max            # the wrapper's ZFS ARC cap, instead of /sys/module/zfs/parameters
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
cleanup() { [ -f "$SB/server.pid" ] && kill -- -"$(cat "$SB/server.pid")" 2>/dev/null; rm -rf "$SB"; }
trap cleanup EXIT
[ -n "${KEEP_SB:-}" ] && trap - EXIT          # KEEP_SB=1 keeps the sandbox (and its fake engine) to look at

# --- the sandbox
mkdir -p "$SB/bin" "$SB/strata/engine-cuda12" "$SB/strata/serve" "$SB/strata/tools"
for d in bench scripts tools systemd config; do cp -r "$W/$d" "$SB/repo-src-$d"; done
mkdir "$SB/repo"; for d in bench scripts tools systemd config; do mv "$SB/repo-src-$d" "$SB/repo/$d"; done
mkdir "$SB/repo/results"
cat > "$SB/repo/bench/run_profile.sh" <<'EOF'
#!/usr/bin/env bash
set -u
TAG=$1; R=$(cd "$(dirname "$0")/.." && pwd)/results
echo "serve_load nonce ${SERVE_LOAD_NONCE:-unset}" > "$R/$TAG-perf.log"
sleep "${STUB_SLEEP:-0}"
[ -n "${STUB_RESTART:-}" ] && "$SB/start_service.sh"      # the engine started again during the run
echo "strata serve: engine error: CUDA error on TESTHOST-MACHINE" >> "$STUB_LOG"
if [ -z "${STUB_CONC_OFF:-}" ]; then for nr in 852:0 16384:40; do    # a read with no slot decoding, and one beside slots
  echo "strata batch: read ${nr%:*} tokens on a thread of its own in 3793 ms (concurrent, to ${nr%:*} of ${nr%:*}); the slots decoded ${nr#*:} rows meanwhile (10.5 rows/s; stage 1: 40 windows, 0.0 ms host each)" >> "$STUB_LOG"
done; fi
python3 -c 'import json, os, sys; p = sys.argv[1]; d = json.load(open(p)); d["totals"]["requests"] += int(os.environ.get("STUB_REQUESTS", "98"))
if os.environ.get("STUB_END_STATE"): d["live"].update(state=os.environ["STUB_END_STATE"], running=1)
json.dump(d, open(p, "w"))' "$SB/metrics"
echo "finished $TAG"
EOF
cat > "$SB/repo/bench/run_robustness.sh" <<'EOF'
#!/usr/bin/env bash
set -u
TAG=$1; R=$(cd "$(dirname "$0")/.." && pwd)/results
{ echo "robustness set ran for $TAG"; echo "Traceback (most recent call last):"; } > "$R/$TAG-robustness.log"
python3 -c 'import json, sys; p = sys.argv[1]; d = json.load(open(p)); d["totals"]["requests"] += 50
json.dump(d, open(p, "w"))' "$SB/metrics"
echo "finished $TAG robustness"
EOF
printf '#!/bin/bash\nexit "$(cat "$SB/hostunits_rc" 2>/dev/null || echo 0)"\n' > "$SB/repo/scripts/host-units.sh"
chmod +x "$SB/repo/bench/run_profile.sh" "$SB/repo/bench/run_robustness.sh" "$SB/repo/scripts/host-units.sh"
(cd "$SB/repo" && git init -q && git add -A && git -c user.name=t -c user.email=t@t.invalid commit -qm sandbox) || exit 1
printf '# comment\n--profile deployed --model iq3_xxs\n' > "$SB/repo/config/profile.conf"   # changed, as a switch leaves it

cat > "$SB/bin/systemctl" <<'EOF'
#!/bin/bash
case "$*" in
  "show -p MainPID --value strata-q3.service") cat "$SB/server.pid" ;;
  "show -p ActiveState --value strata-q3.service") echo active ;;
  "show -p ActiveEnterTimestampMonotonic --value strata-q3.service")
    python3 -c "import time; print(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000 - int(open('$SB/ready_s').read()) * 1000000)" ;;
  "show -p ExecStartPre --value strata-q3.service")
    echo "{ path=$SB/repo/scripts/strata-preflight.sh ; argv[]=$SB/repo/scripts/strata-preflight.sh ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }" ;;
  *) echo "systemctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat > "$SB/bin/curl" <<'EOF'
#!/bin/bash
case "${@: -1}" in */health) cat "$SB/health" ;; */metrics) cat "$SB/metrics" ;; *) exit 7 ;; esac
EOF
printf '#!/bin/bash\n[ "$1" = -n ] && shift\nexec "$@"\n' > "$SB/bin/sudo"
cat > "$SB/bin/journalctl" <<'EOF'
#!/bin/bash
case "$*" in
  "-n 1 --show-cursor --no-pager -q") echo " 十  19 01:00:00 testhost-machine kernel: x"; echo "-- cursor: s=CURSOR42" ;;
  "-n 1 --no-pager -q") echo " 十  19 01:00:00 testhost-machine kernel: x" ;;
  "-u strata-q3.service --after-cursor s=CURSOR42 --no-pager -q") cat "$SB/journal.txt" ;;
  "-k --after-cursor s=CURSOR42 --no-pager -q") cat "$SB/kernel.txt" ;;
  *) echo "journalctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
printf '#!/bin/bash\necho "0, [N/A], [N/A]"; echo "1, [N/A], [N/A]"\n' > "$SB/bin/nvidia-smi"
cat > "$SB/bin/hostname" <<'EOF'
#!/bin/bash
case "$*" in "") echo testhost-machine ;; -f) echo testhost-machine ;; -A) echo "testhost-machine.lan nbname.netbird.cloud testhost-machine.lan " ;; esac
EOF
chmod +x "$SB"/bin/*
printf '#include <unistd.h>\nint main(void) { for (;;) pause(); }\n' > "$SB/engine.c"
gcc -O2 -o "$SB/strata/engine-cuda12/strata" "$SB/engine.c" || exit 1
echo "# server" > "$SB/strata/serve/server.py"; echo "# serve_load" > "$SB/strata/tools/serve_load.py"
export STUB_LOG="$SB/strata/strata-iq3_xxs.log"
cfg() {  # the config, its model fields for $1
  local q=$1 Q; Q=$(echo "$1" | tr a-z A-Z)
  cat > "$SB/strata/strata-iq3_xxs.json" <<EOF
{"exe": "$SB/strata/engine-cuda12/strata", "args": ["--pack", "/d/packs/$q", "--native", "/d/models/$Q/Qwen3.8-Flash-Next-GSQ-RCO-$Q-00001-of-00002.gguf", "--ple-gguf", "/d/models/$Q/Qwen3.8-Flash-Next-GSQ-RCO-$Q-00002-of-00002.gguf", "--conversation-cache-min-free-mib", "6144", "--layer-split", "$2", "--batch", "4"], "tokenizer": "/d/packs/$q/tokenizer", "model_name": "qwen3.8-flash-next-$q", "layer_split": [$2], "parallel": 4, "reasoning_close_retry": true}
EOF
}
# a fake server.py (a bash) with the engine as its child, the engine's stderr the log, in a session of its own (the
# one before it ended): start_service.sh [resident headroom GiB] [parking floor MiB]; the stub perf set runs it too
cat > "$SB/start_service.sh" <<'EOF'
#!/bin/bash
[ -f "$SB/server.pid" ] && kill -- -"$(cat "$SB/server.pid")" 2>/dev/null
cat > "$STUB_LOG" <<'LOG'
strata serve: prompt 15 tokens = 8 reused + 7 read in 112 ms (62.2 tok/s), of an older engine
strata batch: STRATA_PIPED_CONCURRENT is off beside STRATA_PREFILL_HELP
[strata] 2026-10-19 00:50:00 engine started: /x/engine-cuda12/strata --serve --pack /d/packs/iq3_xxs
strata generate: memory guard: limit 62.8 GiB (source: meminfo), current 11.3 GiB; available 51.5 GiB (MemAvailable 51.5 GiB), floor 6144 MiB
strata generate: PCIe probe: 11.7 GB/s host->device (best of 11.7 11.6 11.4 11.4) -> pcie_frac 0.32 (default 0.55)
strata generate: expert cache 6656 slots, 9.41 GiB of VRAM; policy is
strata generate: layer split: CUDA1 runs layers 13-25, expert cache 6656 slots (9.88 GiB), 6656 of its 6656 profiled pairs; slot 0 verified
strata generate: session is up (engine 0.1.43.1)
strata serve: layer split: 100% of the experts resident, the prompt path's streamed ring 96 slots
FileExpertSource: allocating 1.12 GiB page-locked cache complement
FileExpertSource: mapped pinned cache complement ready: resident 1.12 GiB, pinned 1.12 GiB in 6 s; 1.12 GiB page-locked after taking the pages in steps;
strata generate: resident RAM mode: 1.12 GiB of experts in RAM (page-locked), 6656 in the GPU cache; adaptive swaps exchange them with the GPU cache (no file reads)
strata serve: prompt 15 tokens = 8 reused + 7 read in 112 ms (62.2 tok/s), the warm-up
strata generate: expert cache 9999 slots, a line after the first request
LOG
[ -n "${STUB_CONC_OFF:-}" ] && echo "strata batch: STRATA_PIPED_CONCURRENT is off beside this expert source" >> "$STUB_LOG"
setsid bash -c 'exec 2>>"$STUB_LOG"; env -i STRATA_RESIDENT_HEADROOM_GIB="$1" CUDA_VISIBLE_DEVICES=0,1,2,3 OTHER=1 \
  "$SB/strata/engine-cuda12/strata" --serve --pack /d/packs/iq3_xxs \
  --native /d/models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf --layer-split 13,26,38 \
  --conversation-cache-min-free-mib "$2" & wait' _ "${1:-6}" "${2:-6144}" > /dev/null &
echo $! > "$SB/server.pid"
sleep 0.5
EOF
chmod +x "$SB/start_service.sh"
start_service() { "$SB/start_service.sh" "$@"; }
reset() {  # a clean state: the service idle, the journal and kernel log of a clean run
  echo '{"status": "ok", "model": "qwen3.8-flash-next-iq3_xxs", "loaded": true}' > "$SB/health"
  echo '{"live": {"state": "idle", "running": 0, "queued": 0}, "totals": {"requests": 142},
         "conversation_cache": {"enabled": true, "parked": 2, "bytes": 1048576}}' > "$SB/metrics"
  echo 1 > "$SB/ready_s"; rm -f "$SB/hostunits_rc"
  # the ARC cap the sandbox's systemd/zfs-arc-max.service writes, as protocol step 1 deploys it
  grep -o 'echo [0-9]* > /sys/module/zfs/parameters/zfs_arc_max' "$SB/repo/systemd/zfs-arc-max.service" | cut -d' ' -f2 > "$AB_ARC_FILE"
  { echo " 十  19 01:00:01 testhost-machine python[1]: [strata] parallel: a request has waited 104 s for a free slot (busy: [0, 1, 2, 3], in use: [0, 1, 2, 3], ending: []) (issue #1603)"
    for i in $(seq 54); do echo " 十  19 01:00:02 testhost-machine python[1]: [strata] done: 100 tokens in 5 s (20.0 tok/s) (stop, cancel=False)"; done
    for i in $(seq 44); do echo " 十  19 01:00:02 testhost-machine python[1]: [strata] done: 512 tokens in 15 s (34.0 tok/s) (length, cancel=False)"; done
    echo " 十  19 01:00:03 NBNAME python[1]: [strata] parallel: slot(s) 2 were marked busy with no request using them (issue #1603); freed, the waiting request goes on"
  } > "$SB/journal.txt"
  printf '%s\n' " 十  19 01:00:04 testhost-machine kernel: NVRM: Xid (PCI:0000:3b:00): 79, pid=1, name=strata, GPU has fallen off the bus." \
                " 十  19 01:00:05 testhost-machine kernel: usb 1-1: new high-speed USB device" > "$SB/kernel.txt"
  cfg iq3_xxs 13,26,38
}
wrap() {  # run the wrapper in the sandbox: [MODE=robustness] wrap <tag> [env ...]
  local tag=$1; shift
  env PATH="$SB/bin:$PATH" STRATA_DIR="$SB/strata" SETTLE_S=0 "$@" bash "$SB/repo/bench/ab_model_run.sh" "$tag" A ${MODE:+"$MODE"} \
    > "$SB/$tag.out" 2>&1
}
R=$SB/repo/results
NAP="29.$$"          # the stub perf set's sleep: its command line is this run's own, never a leftover's
start_service; reset

# 1. a clean run: every record, the windows by cursor, the lines sorted out by the wrapper's greps, the names redacted
wrap t-clean; rc=$?
[ $rc = 0 ] && pass "clean run exits 0" || { fail "clean run exits $rc"; cat "$SB/t-clean.out"; }
for want in "service ready for 1 s" "deployment code sha256 [0-9a-f]{16} " "perf set sha256 [0-9a-f]{16} " \
            "profile.conf: --profile deployed --model iq3_xxs" "without the model's fields [0-9a-f]{16}" \
            "serve_load nonce ab-0004" "engine env: CUDA_VISIBLE_DEVICES=0,1,2,3 STRATA_RESIDENT_HEADROOM_GIB=6 \$" \
            "tracked files changed: config/profile.conf" "metrics requests before the run: 142" "host units: ok" \
            "load: strata generate: expert cache 6656 slots" "load: strata serve: layer split: 100% of the experts resident" \
            "load: strata generate: session is up" "^set: bench/run_profile.sh$" \
            "^conversation cache before the run: parked 2 bytes 1048576$" \
            "load: FileExpertSource: mapped pinned cache complement ready: resident 1.12 GiB, pinned 1.12 GiB" \
            "load: strata generate: resident RAM mode: 1.12 GiB of experts in RAM"; do
  grep -Eq -- "$want" "$R/t-clean-arm.txt" && pass "arm record: $want" || fail "arm record lacks: $want"
done
grep -q -e "prompt 15 tokens" -e "9999 slots" "$R/t-clean-arm.txt" && fail "arm record: a line after the first request taken as placement" ||
  pass "placement stops at the first request"
grep -q "serve_load nonce ab-0004" "$R/t-clean-perf.log" && pass "run_profile.sh got the nonce" || fail "run_profile.sh did not get the nonce"
for want in "run_profile exit 0  interrupted no" "same engine as before: yes" "idle at the end: yes" \
            "metrics requests during the run: 98" "engine log lines during the run: 1" "journal: system; lines during the run: 1" \
            "journal done lines during the run: 98 \(length,cancel=False 44, stop,cancel=False 54\)" "kernel lines during the run: 1" \
            "^concurrent read lines during the run: 2, with slots decoding: 1$" "^concurrent read off beside: no such line$"; do
  grep -Eq -- "$want" "$R/t-clean-after.txt" && pass "after record: $want" || fail "after record lacks: $want"
done
grep -q "marked busy with no request" "$R/t-clean-journal-errors.txt" && ! grep -q "has waited" "$R/t-clean-journal-errors.txt" &&
  pass "journal: the slot leak kept, the slot wait left out" || fail "journal lines: $(cat "$R/t-clean-journal-errors.txt")"
grep -q "NVRM: Xid" "$R/t-clean-kernel-errors.txt" && ! grep -q usb "$R/t-clean-kernel-errors.txt" && pass "kernel: the Xid kept" ||
  fail "kernel lines: $(cat "$R/t-clean-kernel-errors.txt")"
if grep -rqi -e testhost -e nbname "$R"/t-clean-{arm,after,engine-errors,journal-errors,kernel-errors}.txt; then
  fail "a host name is left: $(grep -rhi -e testhost -e nbname "$R"/t-clean-*.txt | head -3)"
else pass "every host name redacted, in any case, the short netbird label too"; fi
grep -q "<host>" "$R/t-clean-journal-errors.txt" && grep -q "<host>" "$R/t-clean-engine-errors.txt" && pass "redacted as <host>" ||
  fail "no <host> in the redacted lines"

# 2. the code hash: the same after a results commit, another after a change to a tracked file
code1=$(grep -o 'deployment code sha256 [0-9a-f]*' "$R/t-clean-arm.txt")
(cd "$SB/repo" && git add results && git -c user.name=t -c user.email=t@t.invalid commit -qm results)
reset; wrap t-after-commit
code2=$(grep -o 'deployment code sha256 [0-9a-f]*' "$R/t-after-commit-arm.txt")
[ -n "$code1" ] && [ "$code1" = "$code2" ] && pass "a results commit leaves the code hash" || fail "code hash $code1 then $code2"
printf '# comment\n--profile deployed --model iq3_s\n' > "$SB/repo/config/profile.conf"
reset; wrap t-profile
code_p=$(grep -o 'deployment code sha256 [0-9a-f]*' "$R/t-profile-arm.txt")
[ "$code_p" = "$code1" ] && pass "a model switch in profile.conf leaves the code hash" || fail "code hash $code1 then $code_p after profile.conf"
grep -q "profile.conf: --profile deployed --model iq3_s" "$R/t-profile-arm.txt" && pass "the switched profile.conf is recorded" ||
  fail "profile.conf not recorded"
printf '# comment\n--profile deployed --model iq3_xxs\n' > "$SB/repo/config/profile.conf"
echo "# a change" >> "$SB/repo/tools/configure_strata_q3.py"
reset; wrap t-after-change
code3=$(grep -o 'deployment code sha256 [0-9a-f]*' "$R/t-after-change-arm.txt")
[ "$code3" != "$code2" ] && pass "a code change changes the code hash" || fail "the code hash missed a change"
(cd "$SB/repo" && git checkout -q -- tools/configure_strata_q3.py)

# 3. the config hash without the model's fields: the same for IQ3_S's files and cuts, another for another setting
reset; cfg iq3_s 13,25,38; wrap t-cfg-s
a=$(grep -o "without the model's fields [0-9a-f]*" "$R/t-clean-arm.txt"); b=$(grep -o "without the model's fields [0-9a-f]*" "$R/t-cfg-s-arm.txt")
wa=$(grep -o "^config [^ ]* sha256 [0-9a-f]*" "$R/t-clean-arm.txt"); wb=$(grep -o "^config [^ ]* sha256 [0-9a-f]*" "$R/t-cfg-s-arm.txt")
[ "$a" = "$b" ] && [ "$wa" != "$wb" ] && pass "the model's fields are left out of the config hash" || fail "config hashes: $a $b / $wa $wb"
reset; sed -i 's/"reasoning_close_retry": true/"reasoning_close_retry": false/' "$SB/strata/strata-iq3_xxs.json"; wrap t-cfg-other
c=$(grep -o "without the model's fields [0-9a-f]*" "$R/t-cfg-other-arm.txt")
[ "$a" != "$c" ] && pass "another server setting changes the model-free config hash" || fail "the model-free hash missed a setting"

# 4. refusals: nothing written
check_refusal() {  # check_refusal <tag> <want rc> <text> [env...]
  local tag=$1 want=$2 text=$3; shift 3
  wrap "$tag" "$@"; local rc=$?
  if [ $rc = "$want" ] && grep -q -- "$text" "$SB/$tag.out" && ! [ -e "$R/$tag-arm.txt" ]; then pass "refuses: $text"
  else fail "refusal '$text': exit $rc, $(cat "$SB/$tag.out")"; fi
}
reset; echo 120 > "$SB/ready_s"; check_refusal t-settle 1 "became ready 120 s ago" SETTLE_S=300
reset; echo 1000 > "$SB/ready_s"; check_refusal t-young 1 "is younger than the service's ready time"
reset; echo '{"live": {"state": "decoding", "running": 1, "queued": 0}, "totals": {"requests": 142}}' > "$SB/metrics"
check_refusal t-busy 1 "the service is not idle"
reset; echo '{"live": {"state": "idle", "running": 0, "queued": 0}}' > "$SB/metrics"; check_refusal t-nototal 1 "the service is not idle"
reset; touch "$R/t-leftover-perf.log"; check_refusal t-leftover 2 "t-leftover-perf.log exists"
reset; echo '{"tag": "t-used", "scenario": "worst"}' >> "$R/stress.jsonl"; check_refusal t-used 2 "is in results/stress.jsonl"
# what protocol step 1 deploys, not in place: refused before the run (the summary would reject the run afterwards)
reset; echo 1 > "$SB/hostunits_rc"; check_refusal t-units 1 "the host units are not as config/host-units.conf sets them"
reset; echo 8589934592 > "$AB_ARC_FILE"; check_refusal t-arc 1 "the ZFS ARC cap is 8589934592"
start_service 8 6144; reset; check_refusal t-headroom 1 "STRATA_RESIDENT_HEADROOM_GIB 8 and"
start_service 6 8192; reset; check_refusal t-park 1 "conversation-cache-min-free-mib 8192,"
start_service
reset; MODE=bogus check_refusal t-mode 2 "usage: bench/ab_model_run.sh"

# 5. other traffic: more requests counted, a request in flight at the end
reset; wrap t-traffic STUB_REQUESTS=99
grep -q "metrics requests during the run: 99" "$R/t-traffic-after.txt" && pass "a 99th request is counted" || fail "$(cat "$R/t-traffic-after.txt")"

reset; wrap t-busyend STUB_END_STATE=decoding
grep -q "idle at the end: no" "$R/t-busyend-after.txt" && pass "a request in flight at the end is recorded" ||
  fail "$(grep idle "$R/t-busyend-after.txt")"

# 6. an interrupt: exit 130, the perf set's process group gone, the after record written
reset
env PATH="$SB/bin:$PATH" STRATA_DIR="$SB/strata" SETTLE_S=0 STUB_SLEEP="$NAP" bash "$SB/repo/bench/ab_model_run.sh" t-int A > "$SB/t-int.out" 2>&1 &
wpid=$!
# the wrapper takes a few seconds to record the arm on a busy machine: up to 60 s for the stub to start
for _ in $(seq 600); do pgrep -fx "sleep $NAP" > /dev/null && break; sleep 0.1; done
stub=$(pgrep -fx "sleep $NAP" | head -1)
[ -n "$stub" ] || fail "the stub run_profile.sh never started"
kill -TERM "$wpid"; wait "$wpid"; rc=$?
sleep 0.5
[ $rc = 130 ] && pass "an interrupt exits 130" || fail "an interrupt exits $rc"
[ -n "$stub" ] && ! kill -0 "$stub" 2>/dev/null && pass "the perf set's processes are gone" || fail "the stub ($stub) still runs"
grep -q "interrupted yes" "$R/t-int-after.txt" && pass "the after record says interrupted" || fail "no interrupted line"

# 7. an interrupt the moment the arm record appears (before or as the perf set starts): the same
reset
env PATH="$SB/bin:$PATH" STRATA_DIR="$SB/strata" SETTLE_S=0 STUB_SLEEP="$NAP" bash "$SB/repo/bench/ab_model_run.sh" t-int2 A > "$SB/t-int2.out" 2>&1 &
wpid=$!
for _ in $(seq 6000); do [ -e "$R/t-int2-arm.txt" ] && break; sleep 0.01; done
kill -TERM "$wpid"; wait "$wpid"; rc=$?
sleep 0.5
[ $rc = 130 ] && pass "an early interrupt exits 130" || fail "an early interrupt exits $rc: $(tail -3 "$SB/t-int2.out")"
pgrep -fx "sleep $NAP" > /dev/null && fail "an early interrupt left the perf set running" || pass "an early interrupt leaves nothing running"
grep -q "interrupted yes" "$R/t-int2-after.txt" 2>/dev/null && pass "an early interrupt writes the after record" ||
  fail "an early interrupt wrote no after record"

# 8. the robustness run: bench/run_robustness.sh instead of the perf set, with the same records; Tracebacks of its log
reset; MODE=robustness wrap t-robust; rc=$?
[ $rc = 0 ] && pass "a robustness run exits 0" || { fail "a robustness run exits $rc"; cat "$SB/t-robust.out"; }
grep -q "^set: bench/run_robustness.sh$" "$R/t-robust-arm.txt" && pass "arm record: set: bench/run_robustness.sh" ||
  fail "arm record of the robustness run: $(grep '^set' "$R/t-robust-arm.txt" 2>/dev/null)"
grep -q "run_robustness exit 0  interrupted no" "$R/t-robust-after.txt" && pass "after record: run_robustness exit 0" ||
  fail "after record of the robustness run: $(head -1 "$R/t-robust-after.txt" 2>/dev/null)"
grep -q "^perf log Traceback lines: 1$" "$R/t-robust-after.txt" && pass "the Tracebacks are counted in the robustness log" ||
  fail "Tracebacks of the robustness run: $(grep Traceback "$R/t-robust-after.txt" 2>/dev/null)"
grep -q "robustness set ran for t-robust" "$R/t-robust-robustness.log" 2>/dev/null && pass "run_robustness.sh got the tag" ||
  fail "run_robustness.sh did not run for t-robust"
[ ! -e "$R/t-robust-perf.log" ] && pass "the perf set did not run" || fail "the perf set ran in a robustness run"

# 9. the engine started again during the run (by server.py, or systemd's restart): not the same engine
reset; wrap t-restart STUB_RESTART=1
grep -q "^same engine as before: NO$" "$R/t-restart-after.txt" 2>/dev/null && pass "an engine started again during the run is not the same" ||
  fail "an engine restart: $(grep 'same engine' "$R/t-restart-after.txt" 2>/dev/null || cat "$SB/t-restart.out")"

# 10. an engine whose configuration keeps the concurrent prompt read off: no read, and the reason it gave at its start
STUB_CONC_OFF=1 start_service; reset; wrap t-conc-off STUB_CONC_OFF=1
grep -q "^concurrent read lines during the run: 0, with slots decoding: 0$" "$R/t-conc-off-after.txt" 2>/dev/null &&
  grep -q "^concurrent read off beside: this expert source$" "$R/t-conc-off-after.txt" &&
  pass "an engine that keeps the concurrent read off: no reads, and its reason" ||
  fail "concurrent read off: $(grep concurrent "$R/t-conc-off-after.txt" 2>/dev/null || cat "$SB/t-conc-off.out")"

echo "$fails failed"
exit $((fails > 0))
