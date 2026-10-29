#!/usr/bin/env bash
# bench/switch_model.sh against stubs: systemctl, sudo, journalctl and hostname are scripts in a sandbox, and
# scripts/check-deployment.sh is one in the sandbox's checkout.  strata-q3 is a tests/fake_strata.py server on a free
# port whose engine (a compiled program that only waits) has the config's arguments on its command line; a restart
# runs the preflight's checks that matter here (no second engine; the live config as config/profile.conf describes
# it, by the checkout's tool) and waits for /health, as strata-wait-ready.sh does.  As systemd 249 does, a failed start
# leaves the unit activating (Restart=on-failure), and a fifth start since the last reset-failed fails at once (the
# start limit; failed).  The models and packs are small files.  The real service, journal, sudo and port 8080 are
# never touched.  Needs gcc, setsid, taskset and pgrep.
# Usage: tests/switch_model_stubs.sh [checkout]   (prints PASS / FAIL lines, exits 1 on a FAIL; KEEP_SB=1 keeps the sandbox)
set -u
W=${1:-$(cd "$(dirname "$0")/.." && pwd)}
export SB; SB=$(mktemp -d "${TMPDIR:-/tmp}/swm.XXXXXX")
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
# pkill -f, but only to the sandbox's own processes, those with SB=$SB in their environment: another process of this
# user, the service's engine among them, may match the pattern too.  $1: the signal, $2: the pattern
sb_pkill() { local p; for p in $(pgrep -f -- "$2"); do
               tr '\0' '\n' < "/proc/$p/environ" 2> /dev/null | grep -qxF -- "SB=$SB" && kill "-$1" "$p" 2> /dev/null
             done; return 0; }
killall_sb() { [ -e "$SB/units/strata-q3.service.pid" ] && kill -- -"$(cat "$SB/units/strata-q3.service.pid")" 2>/dev/null
               sb_pkill TERM "$SB/"; rm -f "$SB/units/strata-q3.service.pid"; }
cleanup() { killall_sb; rm -rf "$SB"; }
trap cleanup EXIT
[ -n "${KEEP_SB:-}" ] && trap killall_sb EXIT

export PORT; PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export CFG=$SB/strata/strata-iq3_xxs.json
mkdir -p "$SB/bin" "$SB/units" "$SB/results" "$SB/x" "$SB/strata/engine-cuda12" "$SB/strata/serve" "$SB/stray/engine-cuda12"
mkdir "$SB/repo"; for d in bench scripts tools systemd config; do cp -r "$W/$d" "$SB/repo/$d"; done
mkdir "$SB/repo/tests"; cp "$W/tests/fake_strata.py" "$SB/repo/tests/"
cat > "$SB/repo/scripts/check-deployment.sh" <<'EOF'
#!/bin/bash
# a problem (or no answer) while the service answers as a model named in $SB/deploy_fail_models ($SB/deploy_hang_models)
echo "== service"; echo "strata-q3.service on testhost-machine: active / enabled"
m=$(curl -fsS -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["model"].rsplit("-", 1)[-1])' 2>/dev/null)
case " $(cat "$SB/deploy_hang_models" 2>/dev/null) " in *" ${m:-none} "*) exec python3 -c 'import time; time.sleep(60)' "$SB/check-hang" ;; esac
case " $(cat "$SB/deploy_fail_models" 2>/dev/null) " in *" ${m:-none} "*) echo "PROBLEM: /v1/models answered 500"; exit 1 ;; esac
echo "no problems found"
EOF
chmod +x "$SB/repo/scripts/check-deployment.sh"
printf '#include <unistd.h>\nint main(void) { for (;;) pause(); }\n' > "$SB/engine.c"
gcc -O2 -o "$SB/strata/engine-cuda12/strata" "$SB/engine.c" || exit 1
cp "$SB/strata/engine-cuda12/strata" "$SB/stray/engine-cuda12/strata"
cat > "$SB/strata/serve/server.py" <<EOF
import os, sys
a = sys.argv[1:]
cfg, port = a[a.index("--config") + 1], a[a.index("--port") + 1]
os.execv(sys.executable, [sys.executable, "$SB/repo/tests/fake_strata.py", "--port", port, "--config", cfg,
                          "--engine", "$SB/strata/engine-cuda12/strata"])
EOF
# both models' shards and packs, small; each pack's record names the shards it was built from and its size.  The live
# config is the deployed snapshot with its tree and its data in the sandbox.
python3 - "$SB" "$W/config/strata-iq3_xxs.deployed.json" <<'EOF' || exit 1
import json, os, sys
sb, snapshot = sys.argv[1], sys.argv[2]
for q in ("IQ3_XXS", "IQ3_S"):
    m, shards = q.lower(), []
    os.makedirs(f"{sb}/data/models/{q}")
    for i, size in ((1, 3000 + len(q)), (2, 2000)):
        name = f"Qwen3.8-Flash-Next-GSQ-RCO-{q}-0000{i}-of-00002.gguf"
        open(f"{sb}/data/models/{q}/{name}", "wb").write(b"g" * size)
        shards.append({"name": name, "size": size})
    pack = f"{sb}/data/packs/{m}"
    os.makedirs(f"{pack}/tokenizer")
    for f in ("native_experts.txt", "index.txt", "dense.bin", "tokenizer/vocab.json"):
        open(f"{pack}/{f}", "w").write(f"{m} {f}\n")
    open(f"{pack}/experts.bin", "wb").write(b"e" * (500 + len(q)))
    json.dump({"schema": 1, "shards": shards, "native_experts_sha256": "0" * 64, "bytes": 500 + len(q)},
              open(f"{pack}/experts.bin.src.json", "w"), indent=2)
c = json.load(open(snapshot))
tree = os.path.dirname(os.path.dirname(c["exe"]))
data = os.path.dirname(os.path.dirname(c["args"][c["args"].index("--pack") + 1]))
open(f"{sb}/strata/strata-iq3_xxs.json", "w").write(
    json.dumps(c, indent=2).replace(data, f"{sb}/data").replace(tree, f"{sb}/strata") + "\n")
EOF
grep -o 'echo [0-9]* > /sys/module/zfs/parameters/zfs_arc_max' "$SB/repo/systemd/zfs-arc-max.service" | cut -d' ' -f2 > "$SB/arc_want"
# the profile.conf every case starts from: the committed file's comments and IQ3_XXS's arguments, whichever model the
# checkout deploys (the cases start on IQ3_XXS)
{ grep '^#' "$W/config/profile.conf"; echo '--profile deployed'; } > "$SB/profile.base"
cp "$SB/arc_want" "$SB/zfs_arc_max"

# --- the stubs
cat > "$SB/bin/sudo" <<'EOF'
#!/bin/bash
[ "$1" = -n ] && shift
[ -e "$SB/nosudo" ] && exit 1
exec "$@"
EOF
cat > "$SB/bin/systemctl" <<'EOF'
#!/bin/bash
# strata-q3 is a pid file: active while its process group lives, unless $SB/state says otherwise (failed, activating)
U=strata-q3.service
alive() { [ -e "$SB/units/$U.pid" ] && kill -0 "$(cat "$SB/units/$U.pid")" 2>/dev/null; }
state() { if [ -e "$SB/state" ]; then cat "$SB/state"; elif alive; then echo active; else echo inactive; fi; }
fail() { echo "$1" >> "$SB/actions"; echo "${2:-activating}" > "$SB/state"; exit 1; }   # activating: the auto-restart
stop() { if [ -e "$SB/units/$U.pid" ]; then kill -- -"$(cat "$SB/units/$U.pid")" 2>/dev/null
           for _ in $(seq 50); do alive || break; sleep 0.1; done; rm -f "$SB/units/$U.pid"; fi; }
case "$*" in
  "show -p MainPID --value $U") alive && cat "$SB/units/$U.pid" || echo 0 ;;
  "show -p ActiveEnterTimestampMonotonic --value $U") alive && cat "$SB/units/$U.started" || echo 0 ;;
  "show -p ActiveState --value $U") state ;;
  "show -p Job --value $U") cat "$SB/job" 2>/dev/null; echo ;;
  "show -p ExecStartPre --value $U")
    echo "{ path=$SB/repo/scripts/strata-preflight.sh ; argv[]=$SB/repo/scripts/strata-preflight.sh ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }" ;;
  "is-active $U") s=$(state); echo "$s"; [ "$s" = active ] ;;
  "reset-failed $U") echo "reset-failed" >> "$SB/actions"; rm -f "$SB/starts"
                     [ "$(cat "$SB/state" 2>/dev/null)" = failed ] && rm -f "$SB/state"; exit 0 ;;
  "restart $U")
    echo "restart" >> "$SB/actions"; echo activating > "$SB/state"
    stop
    n=$(( $(cat "$SB/starts" 2>/dev/null || echo 0) + 1 )); echo $n > "$SB/starts"
    [ $n -gt 4 ] && fail "start-limit-hit" failed
    # strata-preflight.sh: no second engine, and the live config as config/profile.conf describes it
    pgrep -f "^$SB/[^ ]*engine-cuda12/strata --serve" > /dev/null && fail "preflight refused: a second engine"
    args=$(sed -e 's/#.*//' "$SB/repo/config/profile.conf" | tr -d '\r' | xargs)
    python3 -B "$SB/repo/tools/configure_strata_q3.py" "$CFG" $args --dry-run 2>/dev/null |
      python3 -c 'import json, sys; sys.exit(1 if json.load(sys.stdin)["critical_changes"] else 0)' ||
      fail "preflight refused: the config is not as profile.conf ($args) describes it"
    echo "preflight ok: $args" >> "$SB/actions"
    model=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["model_name"])' "$CFG"); model=${model##*-}
    case " $(cat "$SB/fail_models" 2>/dev/null) " in *" $model "*)
      [ -e "$SB/fail_leaves_stray" ] && setsid "$SB/stray/engine-cuda12/strata" --serve --left-behind > /dev/null 2>&1 &
      fail "the start of $model failed" ;; esac
    case " $(cat "$SB/hang_models" 2>/dev/null) " in *" $model "*)
      echo "the start of $model hangs" >> "$SB/actions"; exec python3 -c 'import time; time.sleep(60)' "$SB/hang" ;; esac
    setsid env $(cat "$SB/fake_env" 2>/dev/null) FAKE_ENGINE_ARGS=1 python3 "$SB/strata/serve/server.py" --engine strata \
      --config "$CFG" --port "$PORT" > /dev/null 2>&1 &
    echo $! > "$SB/units/$U.pid"
    for _ in $(seq 300); do      # strata-wait-ready.sh
      if curl -fsS -m 1 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"loaded": *true'; then
        rm -f "$SB/state"; echo "up $model" >> "$SB/actions"; date +%s%N > "$SB/units/$U.started"
        case " $(cat "$SB/rc1_models" 2>/dev/null) " in *" $model "*) exit 1 ;; esac    # up, but the job said failed
        exit 0
      fi
      sleep 0.1
    done
    fail "not loaded" ;;
  *) echo "systemctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat > "$SB/bin/journalctl" <<'EOF'
#!/bin/bash
case "$*" in
  "-u strata-q3.service --since "*" --no-pager -q")
    echo " 十  19 21:04:43 testhost-machine strata-preflight.sh[1]: strata-preflight: ok: 4 GPUs, 57 GiB usable"
    echo " 十  19 21:12:20 TESTHOST-machine strata-wait-ready.sh[2]: strata-wait-ready: loaded after 455 s" ;;
  "-u strata-q3.service -n 30 --no-pager")
    echo " 十  19 21:12:20 nbname systemd[1]: strata-q3.service: Failed with result 'exit-code'." ;;
  *) echo "journalctl stub: unexpected: $*" >&2; exit 99 ;;
esac
EOF
printf '#!/bin/bash\ncase "$*" in "") echo testhost-machine ;; -f) echo testhost-machine ;; -A) echo "nbname.netbird.cloud " ;; esac\n' > "$SB/bin/hostname"
chmod +x "$SB"/bin/*


ctl() { PATH="$SB/bin:$PATH" systemctl "$@"; }
SWENV=(PATH="$SB/bin:$PATH" STRATA_DIR="$SB/strata" SWITCH_URL="http://127.0.0.1:$PORT" SWITCH_ARC_FILE="$SB/zfs_arc_max"
       SWITCH_PREFLIGHT_RE="^$SB/[^ ]*engine-cuda12/strata --serve" SWITCH_BUSY_UNDER="$SB" SWITCH_IDLE_WAIT_S=0)
sw() {   # sw <name> <args...>: the script in the sandbox; its output in $SB/<name>.out, its exit status in $SB/<name>.rc
  local name=$1; shift
  env "${SWENV[@]}" SWITCH_RESTART_WAIT_S="${WAIT:-60}" SWITCH_CHECK_WAIT_S="${CHECKWAIT:-20}" \
    bash "$SB/repo/bench/switch_model.sh" "$@" > "$SB/$name.out" 2>&1
  echo $? > "$SB/$name.rc"
}
# a background job of this script starts with INT and QUIT ignored, which a shell cannot trap: this starts the switch
# with them as a terminal's foreground command has them
SIGDFL=(python3 -c 'import os, signal, sys
for s in (signal.SIGINT, signal.SIGQUIT):
    signal.signal(s, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])')
rc() { cat "$SB/$1.rc"; }
said() { grep -q -- "$2" "$SB/$1.out"; }
profile() { sed -e 's/#.*//' "$SB/repo/config/profile.conf" | xargs; }
model_of() {   # the live config's model and cuts as the tool reads them, and whether it would change anything for $1
  python3 -B "$SB/repo/tools/configure_strata_q3.py" "$CFG" --profile deployed --model "$1" --dry-run 2>/dev/null |
    python3 -c 'import json, sys; d = json.load(sys.stdin); print(d["model"], d["layer_split"], "changed" if d["changed"] else "settled")'
}
sums() { sha256sum "$SB/repo/config/profile.conf" "$CFG" | cut -c1-64 | tr '\n' ' '; }
restarts() { grep -c '^restart$' "$SB/actions" 2>/dev/null; }
engines() { pgrep -f "^$SB/strata/engine-cuda12/strata --serve" | wc -l; }
health_model() { curl -fsS -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin)["model"])' 2>/dev/null; }
reset() {   # the service up on IQ3_XXS, profile.conf as profile.base, no fault set
  rm -f "$SB/state" "$SB/job" "$SB/nosudo" "$SB/fail_models" "$SB/hang_models" "$SB/rc1_models" "$SB/fail_leaves_stray" \
        "$SB/deploy_fail_models" "$SB/deploy_hang_models" "$SB/fake_env"
  cp "$SB/profile.base" "$SB/repo/config/profile.conf"; cp "$SB/arc_want" "$SB/zfs_arc_max"
  python3 -B "$SB/repo/tools/configure_strata_q3.py" "$CFG" --profile deployed --model iq3_xxs > /dev/null || echo "reset: the tool failed" >&2
  ctl reset-failed strata-q3.service > /dev/null 2>&1
  ctl restart strata-q3.service > /dev/null 2>&1 || echo "reset: the stub service did not come up" >&2
}
metrics_wait() {   # until the switch (pid $1) asks /metrics, or it ended
  for _ in $(seq 400); do
    pgrep -f "^curl -fsS -m 10 http://127.0.0.1:$PORT/metrics" > /dev/null && return 0
    kill -0 "$1" 2>/dev/null || return 1
    sleep 0.05
  done
  return 1
}
ended() { for _ in $(seq 600); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.05; done; return 1; }   # 30 s at most
refused() {   # refused <name> <rc> <text> <args...>: that exit status and text, and nothing changed or restarted
  local name=$1 want=$2 text=$3 before n0; shift 3
  before=$(sums); n0=$(restarts)
  sw "$name" "$@"
  if [ "$(rc "$name")" = "$want" ] && said "$name" "$text" && [ "$(sums)" = "$before" ] && [ "$(restarts)" = "$n0" ]; then
    pass "refuses: $text"
  else fail "refusal '$text': exit $(rc "$name"), restarts $n0 -> $(restarts), $(tail -3 "$SB/$name.out")"; fi
}
in_record() {   # in_record <file> <label> <extended regex>
  grep -Eq -- "$3" "$1" && pass "record: $2" || fail "record lacks $2 ($3): $(head -5 "$1" 2>/dev/null)"
}
R=$SB/results
reset

# 1. --check: every check, nothing changed, no restart
before=$(sums); n0=$(restarts)
sw check --check iq3_s "$R/check.txt"
[ "$(rc check)" = 0 ] && said check "every check passed for iq3_s; nothing was changed" && [ "$(sums)" = "$before" ] &&
  [ "$(restarts)" = "$n0" ] && [ ! -e "$R/check.txt" ] && pass "--check changes nothing and does not restart" ||
  fail "--check: exit $(rc check), $(tail -3 "$SB/check.out")"
said check "on a copy of the live config: model iq3_s split \[13, 25, 38\]" &&
  said check "the model's files: the pack iq3_s and Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf with Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf - complete" &&
  pass "--check names the model, its cuts and its files" || fail "--check output: $(cat "$SB/check.out")"

# 2. a switch to iq3_s
n0=$(restarts)
sw to-s iq3_s "$R/to-s.txt"
[ "$(rc to-s)" = 0 ] && pass "a switch to iq3_s exits 0" || fail "a switch to iq3_s: exit $(rc to-s), $(tail -5 "$SB/to-s.out")"
[ "$(profile)" = "--profile deployed --model iq3_s" ] && pass "profile.conf names --model iq3_s" || fail "profile.conf: $(profile)"
[ "$(model_of iq3_s)" = "iq3_s [13, 25, 38] settled" ] && pass "the live config is IQ3_S's, with its cuts" || fail "the live config: $(model_of iq3_s)"
[ "$(restarts)" = $((n0 + 1)) ] && grep -q "^preflight ok: --profile deployed --model iq3_s$" "$SB/actions" &&
  pass "one restart, whose preflight accepts the config" || fail "actions: $(tail -5 "$SB/actions")"
[ "$(health_model)" = qwen3.8-flash-next-iq3_s ] && [ "$(engines)" = 1 ] && pass "the service answers as qwen3.8-flash-next-iq3_s, with one engine" ||
  fail "health: $(health_model), engines $(engines)"
in_record "$R/to-s.txt" "the restart's exit and state" "systemctl restart exit 0 after [0-9]+ s .*, state active$"
in_record "$R/to-s.txt" "health" '^health: .*"model": "qwen3.8-flash-next-iq3_s"'
in_record "$R/to-s.txt" "the engine's arguments" "^engine args: [^ ]*/strata/engine-cuda12/strata --serve --pack [^ ]*/data/packs/iq3_s --native [^ ]*/data/models/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf"
in_record "$R/to-s.txt" "the engine's start-up lines" "^\[strata\] [0-9-]+ [0-9:]+ engine started: "
in_record "$R/to-s.txt" "the journal's lines" "strata-wait-ready: loaded after 455 s"
in_record "$R/to-s.txt.check" "check-deployment.sh's output" "^no problems found$"
if grep -qi -e testhost -e nbname "$R/to-s.txt" "$R/to-s.txt.check"; then fail "a host name is left in the record"
else pass "no host name in the record or its check"; fi
grep -q "<host>" "$R/to-s.txt" && grep -q "<host>" "$R/to-s.txt.check" && pass "host names are written as <host>" || fail "no <host>"

# 3. a restart on the same model: both files as they were
before=$(sums); n0=$(restarts)
sw again iq3_s "$R/again.txt"
[ "$(rc again)" = 0 ] && [ "$(sums)" = "$before" ] && [ "$(restarts)" = $((n0 + 1)) ] &&
  pass "a restart on the same model leaves both files as they were" || fail "again: exit $(rc again), $(tail -3 "$SB/again.out")"

# 4. back to iq3_xxs
sw to-xxs iq3_xxs "$R/to-xxs.txt"
[ "$(rc to-xxs)" = 0 ] && [ "$(profile)" = "--profile deployed --model iq3_xxs" ] && [ "$(model_of iq3_xxs)" = "iq3_xxs [13, 26, 38] settled" ] &&
  [ "$(health_model)" = qwen3.8-flash-next-iq3_xxs ] && pass "a switch back to iq3_xxs: profile.conf, the config's cuts, the service" ||
  fail "to iq3_xxs: exit $(rc to-xxs), $(profile), $(model_of iq3_xxs), $(health_model)"

# 5. refusals: that exit status and text, nothing changed, no restart
refused exists 2 "to-s.txt exists" iq3_s "$R/to-s.txt"
refused usage 2 "usage: bench/switch_model.sh" iq3_s
refused unknown 2 "unknown model iq3_m" iq3_m "$R/r.txt"
echo failed > "$SB/state"; refused failed 1 "strata-q3.service is failed" iq3_s "$R/r.txt"; rm -f "$SB/state"
echo 4711 > "$SB/job"; refused job 1 "with job 4711 queued" iq3_s "$R/r.txt"; rm -f "$SB/job"
kill -- -"$(cat "$SB/units/strata-q3.service.pid")"; sleep 0.3; echo active > "$SB/state"
refused unloaded 1 "but its model is not loaded" iq3_s "$R/r.txt"
reset
echo FAKE_BUSY=1 > "$SB/fake_env"; ctl restart strata-q3.service > /dev/null 2>&1
refused serving 1 "has been serving requests for 0 s" iq3_s "$R/r.txt"
reset
python3 -c 'import time; time.sleep(120)' "$SB/x/run_profile.sh" & bpid=$!
refused measuring 1 "a measurement, evaluation, pack build or switch is running" iq3_s "$R/r.txt"
grep -q "^$bpid python3 " "$SB/measuring.out" && pass "... and it names the process" || fail "busy process not named: $(cat "$SB/measuring.out")"
kill "$bpid"; wait "$bpid" 2>/dev/null
python3 -c 'import time; time.sleep(120)' "$SB/x/switch_model.sh" iq3_s & bpid=$!
refused another 1 "a measurement, evaluation, pack build or switch is running" iq3_s "$R/r.txt"
kill "$bpid"; wait "$bpid" 2>/dev/null
taskset -c 0 python3 -c 'import time; time.sleep(120)' "$SB/x/run_side.sh" remote runs/x 4 & bpid=$!
sw pinned --check iq3_s "$R/r.txt"
[ "$(rc pinned)" = 0 ] && pass "a remote evaluation pinned to one CPU may run beside" || fail "pinned remote: $(tail -3 "$SB/pinned.out")"
kill "$bpid"; wait "$bpid" 2>/dev/null
python3 -c 'import time; time.sleep(120)' "$SB/x/run_side.sh" remote runs/x 4 & bpid=$!
refused unpinned 1 "a measurement, evaluation, pack build or switch is running" iq3_s "$R/r.txt"
kill "$bpid"; wait "$bpid" 2>/dev/null
setsid "$SB/stray/engine-cuda12/strata" --serve --stray > /dev/null 2>&1 & spid=$!
refused stray 1 "strata-preflight.sh would refuse the start while these run" iq3_s "$R/r.txt"
kill "$spid"; wait "$spid" 2>/dev/null
echo 8589934592 > "$SB/zfs_arc_max"; refused arc 1 "zfs_arc_max is 8589934592" iq3_s "$R/r.txt"; cp "$SB/arc_want" "$SB/zfs_arc_max"
touch "$SB/nosudo"; refused nosudo 1 "sudo -n does not work" iq3_s "$R/r.txt"; rm -f "$SB/nosudo"
echo iq3_xxs > "$SB/deploy_fail_models"
refused deploy-before 1 "scripts/check-deployment.sh finds a problem now" iq3_s "$R/r.txt"; rm -f "$SB/deploy_fail_models"
printf '%s\n' '--profile deployed --reserve-mib 2048' > "$SB/repo/config/profile.conf"
refused profile-args 1 "config/profile.conf holds '--profile deployed --reserve-mib 2048'" iq3_s "$R/r.txt"
cp "$SB/profile.base" "$SB/repo/config/profile.conf"
P=$SB/data/packs/iq3_s
mv "$P/index.txt" "$SB/x/index.txt"; refused nopack 1 "missing or empty: [^ ]*/packs/iq3_s/index.txt" iq3_s "$R/r.txt"; mv "$SB/x/index.txt" "$P/index.txt"
S1=$SB/data/models/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf
mv "$S1" "$SB/x/shard1"; refused noshard 1 "missing or empty: [^ ]*/models/IQ3_S/[^ ]*-IQ3_S-00001-of-00002.gguf" iq3_s "$R/r.txt"; mv "$SB/x/shard1" "$S1"
cp "$P/experts.bin.src.json" "$SB/x/src.json"
python3 -c 'import json, sys; p = sys.argv[1]; d = json.load(open(p)); d["shards"][0]["size"] += 1; json.dump(d, open(p, "w"))' "$P/experts.bin.src.json"
refused srcsize 1 "the pack was built from" iq3_s "$R/r.txt"; cp "$SB/x/src.json" "$P/experts.bin.src.json"
cp "$P/experts.bin" "$SB/x/experts.bin"; printf x >> "$P/experts.bin"
refused binsize 1 "experts.bin has 506 bytes; the pack's record says 505" iq3_s "$R/r.txt"; cp "$SB/x/experts.bin" "$P/experts.bin"
cp "$CFG" "$SB/x/cfg.json"
python3 -c 'import json, sys; p = sys.argv[1]; c = json.load(open(p)); c["args"] += ["--no-ple"]; json.dump(c, open(p, "w"), indent=2)' "$CFG"
refused tool 1 "the tool cannot write iq3_s's config on a copy of the live config" iq3_s "$R/r.txt"; cp "$SB/x/cfg.json" "$CFG"
T=$SB/repo/tools/configure_strata_q3.py
cp "$T" "$SB/x/tool.py"; sed -i 's/"iq3_s": "iq3_s-light"}/"iq3_s": "last-light"}/' "$T"
grep -q '"iq3_s": "last-light"}' "$T" || fail "the tool's IQ3_S cuts could not be changed for the next case"
refused cuts 1 "on a copy of the live config the tool does not give iq3_s with the cuts \[13, 25, 38\]" iq3_s "$R/r.txt"
cp "$SB/x/tool.py" "$T"

# 6. a failure before the restart: both files put back, no restart
reset; echo FAKE_BUSY_FROM=2 > "$SB/fake_env"; ctl restart strata-q3.service > /dev/null 2>&1
before=$(sums); n0=$(restarts)
sw came iq3_s "$R/came.txt"
[ "$(rc came)" = 1 ] && said came "a request came in" && said came "are as they were; the service was not restarted" &&
  [ "$(sums)" = "$before" ] && [ "$(restarts)" = "$n0" ] && pass "a request that comes in before the restart: both files put back, no restart" ||
  fail "a request came in: exit $(rc came), $(tail -3 "$SB/came.out")"
for sig in TERM INT QUIT; do
  reset; echo FAKE_METRICS_SLOW_FROM=2 > "$SB/fake_env"; ctl restart strata-q3.service > /dev/null 2>&1
  before=$(sums); n0=$(restarts)
  "${SIGDFL[@]}" env "${SWENV[@]}" bash "$SB/repo/bench/switch_model.sh" iq3_s "$R/int-$sig.txt" > "$SB/int-$sig.out" 2>&1 &
  spid=$!
  for _ in $(seq 400); do [ "$(profile)" = "--profile deployed --model iq3_s" ] && break; sleep 0.05; done
  kill -"$sig" "$spid"; ended "$spid"; wait "$spid"; r=$?
  [ "$r" = 1 ] && said "int-$sig" "interrupted" && [ "$(sums)" = "$before" ] && [ "$(restarts)" = "$n0" ] &&
    pass "$sig before the restart: both files put back, no restart" || fail "$sig before the restart: exit $r, $(tail -3 "$SB/int-$sig.out")"
done

# a TERM after profile.conf was written, with its directory read-only by then: nothing can be put back (exit 5)
reset; echo FAKE_METRICS_SLOW_FROM=2 > "$SB/fake_env"; ctl restart strata-q3.service > /dev/null 2>&1; n0=$(restarts)
env "${SWENV[@]}" bash "$SB/repo/bench/switch_model.sh" iq3_s "$R/ro.txt" > "$SB/ro.out" 2>&1 &
spid=$!
for _ in $(seq 400); do [ "$(profile)" = "--profile deployed --model iq3_s" ] && break; sleep 0.05; done
chmod a-w "$SB/repo/config"; kill -TERM "$spid"; ended "$spid"; wait "$spid"; r=$?; chmod u+w "$SB/repo/config"
[ "$r" = 5 ] && said ro "ERROR: could not put config/profile.conf and" && [ "$(restarts)" = "$n0" ] &&
  pass "files that cannot be put back: exit 5, with their copies named, no restart" || fail "read-only: exit $r, $(tail -3 "$SB/ro.out")"

# a stray engine or a measurement that starts while the script waits for an idle service: found before the restart
for what in stray measurement; do
  reset; echo FAKE_METRICS_SLOW_FROM=1 > "$SB/fake_env"; ctl restart strata-q3.service > /dev/null 2>&1
  before=$(sums); n0=$(restarts)
  env "${SWENV[@]}" bash "$SB/repo/bench/switch_model.sh" iq3_s "$R/late-$what.txt" > "$SB/late-$what.out" 2>&1 &
  spid=$!
  metrics_wait "$spid"
  if [ $what = stray ]; then
    setsid "$SB/stray/engine-cuda12/strata" --serve --late > /dev/null 2>&1 & lpid=$!; text="strata-preflight.sh would refuse the start while these run: "
  else
    python3 -c 'import time; time.sleep(120)' "$SB/x/stress_matrix.py" & lpid=$!; text="a measurement, evaluation, pack build or switch started: "
  fi
  # the script's end first (30 s at most), then the late process's: a wait for the script beside a live child of
  # this shell can hang in bash 5.1 when the script ended long before (a mutant that refuses at once)
  ended "$spid"
  kill "$lpid"; wait "$lpid" 2>/dev/null
  wait "$spid"; r=$?
  [ "$r" = 1 ] && said "late-$what" "$text" && said "late-$what" "are as they were; the service was not restarted" &&
    [ "$(sums)" = "$before" ] && [ "$(restarts)" = "$n0" ] && pass "a $what that starts before the restart: both files put back, no restart" ||
    fail "a late $what: exit $r, $(tail -3 "$SB/late-$what.out")"
done

# another start of strata-q3 (its main process and the time it became active change) after the checks: before the
# files are written, a refusal; after, both files put back, no restart
for when in waiting written; do
  reset; [ $when = waiting ] && echo FAKE_METRICS_SLOW_FROM=1 > "$SB/fake_env" || echo FAKE_METRICS_SLOW_FROM=2 > "$SB/fake_env"
  ctl restart strata-q3.service > /dev/null 2>&1
  before=$(sums); n0=$(restarts)
  env "${SWENV[@]}" bash "$SB/repo/bench/switch_model.sh" iq3_s "$R/other-$when.txt" > "$SB/other-$when.out" 2>&1 &
  spid=$!
  [ $when = written ] && for _ in $(seq 400); do [ "$(profile)" = "--profile deployed --model iq3_s" ] && break; sleep 0.05; done
  metrics_wait "$spid"; echo 1 > "$SB/units/strata-q3.service.started"      # as another start leaves it
  ended "$spid"; wait "$spid"; r=$?
  if [ $when = waiting ]; then text="was started again while this script checked and waited"; label="while the script waits for an idle service: refused"
  else text="was started again by someone else after the checks"; label="after the files are written: both put back"; fi
  [ "$r" = 1 ] && said "other-$when" "$text" && [ "$(sums)" = "$before" ] && [ "$(restarts)" = "$n0" ] &&
    pass "another start $label, no restart" ||
    fail "another start ($when): exit $r, $(tail -3 "$SB/other-$when.out")"
done

# 7. an interrupt to the script's process group during the restart (a Ctrl-C) does not stop the restart
reset; echo FAKE_LOAD_S=4 > "$SB/fake_env"; n0=$(restarts)
setsid "${SIGDFL[@]}" env "${SWENV[@]}" SWITCH_RESTART_WAIT_S=60 bash "$SB/repo/bench/switch_model.sh" iq3_s "$R/tstp.txt" > "$SB/tstp.out" 2>&1 &
spid=$!
for _ in $(seq 600); do [ "$(restarts)" -gt "$n0" ] && break; sleep 0.05; done
sleep 0.5; for sig in INT TERM HUP QUIT; do kill -"$sig" -- -"$spid"; done
ended "$spid"; wait "$spid"; r=$?
[ "$r" = 0 ] && [ "$(restarts)" = $((n0 + 1)) ] && [ ! -e "$R/tstp.txt.rollback" ] && [ "$(health_model)" = qwen3.8-flash-next-iq3_s ] &&
  pass "a Ctrl-C during the restart: the restart finishes and counts, no rollback" ||
  fail "interrupt during the restart: exit $r, restarts $n0 -> $(restarts), $(tail -3 "$SB/tstp.out")"
# the output's reader gone after the checks (a tee ended by a Ctrl-C): the switch still finishes
reset; n0=$(restarts)
env "${SWENV[@]}" bash "$SB/repo/bench/switch_model.sh" iq3_s "$R/pipe.txt" 2>&1 |
  { while IFS= read -r l; do case $l in *"the model's files:"*) exit 0 ;; esac; done; }
r=${PIPESTATUS[0]}
[ "$r" = 0 ] && [ "$(restarts)" = $((n0 + 1)) ] && [ -e "$R/pipe.txt" ] && [ "$(health_model)" = qwen3.8-flash-next-iq3_s ] &&
  pass "the output's reader gone before the restart: the switch still finishes" ||
  fail "the reader gone: exit $r, restarts $n0 -> $(restarts), $(health_model)"
# four starts within the hour already (the start limit is 4): the switch's reset-failed lets its restart through
reset; for _ in 1 2 3; do ctl restart strata-q3.service > /dev/null 2>&1; done
sw limit iq3_s "$R/limit.txt"
[ "$(rc limit)" = 0 ] && [ "$(health_model)" = qwen3.8-flash-next-iq3_s ] &&
  pass "four starts within the hour before it: the switch still comes up" || fail "start limit: exit $(rc limit), $(tail -3 "$SB/limit.out")"

# 8. a start that does not come up
reset; echo iq3_s > "$SB/fail_models"; n0=$(restarts); f0=$(grep -c '^reset-failed$' "$SB/actions")
sw fail-s iq3_s "$R/fail-s.txt"
[ "$(rc fail-s)" = 3 ] && said fail-s "strata-q3 is back on IQ3_XXS" && [ -e "$R/fail-s.txt.rollback" ] &&
  [ "$(profile)" = "--profile deployed --model iq3_xxs" ] && [ "$(model_of iq3_xxs)" = "iq3_xxs [13, 26, 38] settled" ] &&
  [ "$(health_model)" = qwen3.8-flash-next-iq3_xxs ] && pass "an IQ3_S start that fails is switched back to IQ3_XXS (exit 3)" ||
  fail "IQ3_S failing: exit $(rc fail-s), $(profile), $(model_of iq3_xxs), $(health_model), $(tail -3 "$SB/fail-s.out")"
[ "$(restarts)" = $((n0 + 2)) ] && [ "$(grep -c '^reset-failed$' "$SB/actions")" = $((f0 + 2)) ] &&
  pass "... with one more restart, each after a reset-failed" || fail "rollback actions: $(tail -8 "$SB/actions")"
in_record "$R/fail-s.txt" "the failed start (systemd's auto-restart pending)" "systemctl restart exit 1 after [0-9]+ s .*, state activating$"
in_record "$R/fail-s.txt.rollback" "the switch back" "^switch to iq3_xxs at .*systemctl restart exit 0 .*, state active$"
reset; echo "iq3_s iq3_xxs" > "$SB/fail_models"
sw fail-both iq3_s "$R/fail-both.txt"
[ "$(rc fail-both)" = 4 ] && said fail-both "these commands bring it back on IQ3_XXS" &&
  said fail-both "sudo systemctl reset-failed strata-q3.service; sudo systemctl restart strata-q3.service" &&
  pass "when IQ3_XXS fails too, it prints the commands to bring it back (exit 4)" || fail "both failing: exit $(rc fail-both), $(tail -3 "$SB/fail-both.out")"
reset; echo iq3_xxs > "$SB/fail_models"
sw fail-xxs iq3_xxs "$R/fail-xxs.txt"
[ "$(rc fail-xxs)" = 4 ] && [ ! -e "$R/fail-xxs.txt.rollback" ] && said fail-xxs "these commands bring it back on IQ3_XXS" &&
  pass "a switch to iq3_xxs that does not come up prints the commands (exit 4)" || fail "IQ3_XXS failing: exit $(rc fail-xxs)"
reset; echo iq3_s > "$SB/rc1_models"
sw rc1 iq3_s "$R/rc1.txt"
[ "$(rc rc1)" = 3 ] && grep -q "systemctl restart exit 1 after [0-9]* s .*, state active$" "$R/rc1.txt" &&
  [ "$(health_model)" = qwen3.8-flash-next-iq3_xxs ] &&
  pass "a restart systemctl calls failed is no switch, though the unit came up (exit 3)" ||
  fail "restart exit 1: exit $(rc rc1), $(head -1 "$R/rc1.txt" 2>/dev/null)"
reset; echo iq3_s > "$SB/deploy_fail_models"
sw check-fails iq3_s "$R/cf.txt"
[ "$(rc check-fails)" = 3 ] && said check-fails "check-deployment.sh found problems" && grep -q "^PROBLEM" "$R/cf.txt.check" &&
  pass "IQ3_S with a problem check-deployment.sh finds is switched back (exit 3)" || fail "check fails: exit $(rc check-fails), $(tail -3 "$SB/check-fails.out")"
reset; echo iq3_s > "$SB/deploy_hang_models"
CHECKWAIT=2 sw check-hang iq3_s "$R/ch.txt"
[ "$(rc check-hang)" = 3 ] && grep -q "no answer within 2 s" "$R/ch.txt.check" && [ "$(health_model)" = qwen3.8-flash-next-iq3_xxs ] &&
  pass "a check-deployment.sh that does not answer after the restart is a problem: switched back (exit 3)" ||
  fail "check hangs: exit $(rc check-hang), $(tail -3 "$SB/check-hang.out")"
reset; echo iq3_s > "$SB/hang_models"
WAIT=3 sw hang iq3_s "$R/hang.txt"
[ "$(rc hang)" = 3 ] && grep -q "systemctl restart exit 124 " "$R/hang.txt" && [ "$(health_model)" = qwen3.8-flash-next-iq3_xxs ] &&
  pass "a start with no answer within SWITCH_RESTART_WAIT_S is switched back (exit 3)" || fail "hang: exit $(rc hang), $(head -1 "$R/hang.txt" 2>/dev/null)"
reset; echo iq3_s > "$SB/fail_models"; touch "$SB/fail_leaves_stray"; n0=$(restarts)
sw stray-left iq3_s "$R/sl.txt"
[ "$(rc stray-left)" = 4 ] && said stray-left "ERROR: strata-preflight.sh would refuse the start while these run" &&
  said stray-left "these commands bring it back on IQ3_XXS" && [ "$(restarts)" = $((n0 + 1)) ] &&
  pass "an engine left behind by a failed start: no second restart, the commands are printed (exit 4)" ||
  fail "stray left: exit $(rc stray-left), restarts $n0 -> $(restarts), $(tail -3 "$SB/stray-left.out")"
sb_pkill TERM "^$SB/stray/engine-cuda12/strata --serve --left-behind"

# 9. no command the script runs holds the text strata-preflight.sh's pgrep looks for (its own pgreps write [e]ngine)
if grep -v '^[[:space:]]*#' "$W/bench/switch_model.sh" | grep 'engine-cuda12/strata[ ]--serve' | grep -qv '\[e\]ngine'; then
  fail "a command line of bench/switch_model.sh holds the preflight's text"
else pass "no command line of the switch holds the preflight's text"; fi

echo "$fails failed"
exit $((fails > 0))
