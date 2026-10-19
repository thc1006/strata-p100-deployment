#!/usr/bin/env bash
# Switch strata-q3 to one GSQ-RCO model and restart it, as docs/runbooks/change-config.md does it by hand, with every
# step checked before and after.  The IQ3_XXS / IQ3_S A/B (docs/decisions/0004-iq3-s.md) restarts the service with it
# before every run, also when the model stays.  It writes what the restart did to RECORD (its exit status and time,
# /health, the engine's arguments and start-up lines, the service journal) and scripts/check-deployment.sh's output to
# RECORD.check, every name of this host as <host>.
# Usage: bench/switch_model.sh [--check] iq3_s|iq3_xxs RECORD
#   --check   every check below, the tool's run on a copy of the live config included; nothing is changed
# The checkout it changes is the one strata-q3 runs (its ExecStartPre is <checkout>/scripts/strata-preflight.sh): the
# preflight checks the live config against that checkout's config/profile.conf with its tools/configure_strata_q3.py.
# Nothing is changed until all of these hold (exit 1 otherwise; 2 on bad usage or when a record file exists):
#   - strata-q3 is active and loaded with no job queued, and serves nothing: /metrics idle (waited for, up to
#     SWITCH_IDLE_WAIT_S, default 600); and nobody else started it again meanwhile (its main process and the time it
#     became active, checked again before the files are written and before the restart)
#   - config/profile.conf holds "--profile deployed" and at most a --model: this script writes only those
#   - no measurement, evaluation, pack build or other switch runs: a process with one of their scripts as an argument,
#     in any argument order; only the remote evaluation (driver2.py whose every --endpoint is remote, or run_side.sh
#     remote) pinned to one CPU may run beside
#   - no process but strata-q3's own engine has "engine-cuda12/strata --serve" in its command line: strata-preflight.sh
#     takes any such process for a second engine and refuses the start, which would leave the service down
#   - the ZFS ARC cap is what the checkout's systemd/zfs-arc-max.service writes, and sudo -n works
#   - scripts/check-deployment.sh finds no problem: the switch needs it to pass after the restart, so a problem that is
#     there before (strata-q3-health.timer stopped, an API key) would fail a model that came up
#   - on a copy of the live config, the tool writes the model with its cuts, after which a dry run finds nothing left
#     to change; the pack and the two shards that config names are there, and they are the files the pack was built
#     from (the names and sizes in its experts.bin.src.json), with experts.bin of the size it records
# Then it writes config/profile.conf and the live config; when a step fails before the restart, or an INT, TERM, HUP,
# QUIT or PIPE comes, both are put back as they were and the service is not touched (exit 5 when they cannot be: their
# copies are kept and named).  Exit 0 only when the service came up loaded with the requested model's files and
# scripts/check-deployment.sh found no problem (it is given SWITCH_CHECK_WAIT_S seconds, default 180; no answer is a
# problem).  A switch to iq3_s that does not come up is switched back to iq3_xxs (exit 3 when IQ3_XXS came up, with its
# record in RECORD.rollback); when that fails too, or a switch to iq3_xxs does not come up, it prints the commands that
# bring IQ3_XXS back by hand (exit 4).  From just before the restart on, those signals are ignored, so that the restart
# and its checks finish (timeout, in a process group of its own, gets no Ctrl-C either).  Each restart is preceded by
# systemctl reset-failed, which also resets the unit's start limit (4 starts an hour): earlier starts do not make it
# fail.  It is waited for up to SWITCH_RESTART_WAIT_S seconds (default 4800: the unit's TimeoutStartSec is 75 min; a
# start takes 7-9 minutes, and the preflight may wait up to 25 for memory).
# (Test hooks: STRATA_DIR replaces ~/dev/strata, SWITCH_URL the service's URL, SWITCH_ARC_FILE
# /sys/module/zfs/parameters/zfs_arc_max, SWITCH_PREFLIGHT_RE the preflight's pattern; SWITCH_BUSY_UNDER counts only
# processes with an argument under that directory, so that test sandboxes running at the same time do not see each
# other.)
set -u -o pipefail
CHECK=0; [ "${1:-}" = --check ] && { CHECK=1; shift; }
[ $# = 2 ] || { echo "usage: bench/switch_model.sh [--check] iq3_s|iq3_xxs RECORD" >&2; exit 2; }
MODEL=$1; OUT=$2
case $MODEL in
  iq3_s) Q=IQ3_S; WANT_SPLIT='[13, 25, 38]' ;;
  iq3_xxs) Q=IQ3_XXS; WANT_SPLIT='[13, 26, 38]' ;;
  *) echo "unknown model $MODEL (iq3_s or iq3_xxs)" >&2; exit 2 ;;
esac
UNIT=strata-q3.service
STRATA=${STRATA_DIR:-$HOME/dev/strata}
CFG=$STRATA/strata-iq3_xxs.json
LOG=$STRATA/strata-iq3_xxs.log
URL=${SWITCH_URL:-http://127.0.0.1:8080}
ARC_FILE=${SWITCH_ARC_FILE:-/sys/module/zfs/parameters/zfs_arc_max}
IDLE_WAIT_S=${SWITCH_IDLE_WAIT_S:-600}
RESTART_WAIT_S=${SWITCH_RESTART_WAIT_S:-4800}
CHECK_WAIT_S=${SWITCH_CHECK_WAIT_S:-180}
# strata-preflight.sh's test for a second engine (an unanchored pgrep), and the tree's engine; both written [e]ngine,
# so that the command line of a pgrep that looks for them does not hold the text the preflight looks for
PREFLIGHT_RE=${SWITCH_PREFLIGHT_RE:-[e]ngine-cuda12/strata --serve}
ENGINE_RE="^$(printf '%s' "$STRATA" | sed 's/[.[\*^$/]/\\&/g')/[e]ngine-cuda12/strata --serve"
PROFILE_HEAD=('# The configure_strata_q3.py arguments the deployed config must match (checked by strata-preflight.sh at every start).'
              '# For a tuning experiment: change this line, re-run the tool with the same arguments, then restart the service.')
say() { echo "$(date +%T) $*"; }
refuse() { say "refusing: $*"; exit 1; }
# every name of this host and the first label of each, longest first, in any case (as bench/ab_model_run.sh)
names=$({ hostname; hostname -f; hostname -A; } 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sort -u)
[ -n "$names" ] || refuse "no host name to redact"
names=$({ printf '%s\n' "$names"; printf '%s\n' "$names" | cut -d. -f1 | awk 'length >= 4'; } | sort -u |
        awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)
redact_args=(); while IFS= read -r n; do redact_args+=(-e "s/$(printf '%s' "$n" | sed 's/[.[\*^$/]/\\&/g')/<host>/gI"); done <<< "$names"
redact() { sed "${redact_args[@]}"; }
for f in "$OUT" "$OUT.check" "$OUT.rollback" "$OUT.rollback.check"; do
  [ -e "$f" ] && { say "refusing: $f exists"; exit 2; }
done
[ -d "$(dirname "$OUT")" ] && [ -w "$(dirname "$OUT")" ] || refuse "cannot write $OUT"
pre=$(systemctl show -p ExecStartPre --value $UNIT 2>/dev/null | grep -o 'path=[^ ;]*' | head -1); pre=${pre#path=}
R=; [ -n "$pre" ] && R=$(cd "$(dirname "$pre")/.." 2>/dev/null && pwd)
[ -n "$R" ] && [ -f "$R/config/profile.conf" ] && [ -f "$R/tools/configure_strata_q3.py" ] ||
  refuse "cannot find the checkout $UNIT runs (its ExecStartPre: ${pre:-none})"
TOOL=$R/tools/configure_strata_q3.py
tmp=$(mktemp -d) || refuse "mktemp failed"
keep=0; trap '[ $keep = 1 ] || rm -rf "$tmp"' EXIT

unit_id() { echo "$(systemctl show -p MainPID --value $UNIT) $(systemctl show -p ActiveEnterTimestampMonotonic --value $UNIT)"; }
deployment_ok() {   # scripts/check-deployment.sh, its output (to a file: a child it leaves hanging holds no pipe) in $1
  timeout -k 10 "$CHECK_WAIT_S" "$R/scripts/check-deployment.sh" > "$tmp/check.raw" 2>&1; local rc=$?
  redact < "$tmp/check.raw" > "$1"
  [ "$rc" = 124 ] && echo "(no answer within $CHECK_WAIT_S s)" >> "$1"
  [ "$rc" = 0 ]
}
own_engine() {   # strata-q3's engine: the child of its main process that runs the tree's engine
  local main; main=$(systemctl show -p MainPID --value $UNIT)
  [[ "$main" =~ ^[1-9][0-9]*$ ]] && pgrep -P "$main" -f "$ENGINE_RE" | head -1
}
strays() {       # every process strata-preflight.sh would take for a second engine, strata-q3's own engine aside
  local p own; own=$(own_engine)
  for p in $(pgrep -f -- "$PREFLIGHT_RE"); do
    [ "$p" = "$own" ] && continue
    echo "  $p $(tr '\0' ' ' 2>/dev/null < "/proc/$p/cmdline" | cut -c1-160)"
  done
}
busy() {         # one line per measurement, evaluation, pack build or other switch running; fails when it cannot look
  python3 - "$$" "${SWITCH_BUSY_UNDER:-}" <<'EOF'
import os
import sys
# a process with one of these as an argument of its own (a shell whose -c string merely names one is not: the string
# has spaces); the remote side of the evaluation pinned to one CPU talks to the remote gateway only, and may run beside
NAMES = {"run_profile.sh", "run_robustness.sh", "stress_matrix.py", "latency_suite.py", "four_user_test.py",
         "serve_load.py", "engine_screen.py", "ab_model_run.sh", "ab_engine.sh", "ab_versions.sh", "ab_v045.sh",
         "ab_v045b.sh", "conversation_cache_probe.py", "ab_eval.py", "run_ab_eval.sh", "run_queue.sh", "driver.py",
         "driver2.py", "run_side.sh", "grade_lcb.py", "latency_probe.py", "iq_pack.py", "switch_model.sh"}
SELF, UNDER = int(sys.argv[1]), sys.argv[2]


def ours(pid):
    """This script, or a shell it forked (one has its command line)."""
    for _ in range(64):
        if pid == SELF:
            return True
        if pid <= 1:
            return False
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def endpoints(args):
    """driver2.py's --endpoint values: argparse takes "--endpoint X", "--endpoint=X" and a prefix ("--end X") alike."""
    out = []
    for k, a in enumerate(args):
        name, eq, value = a.partition("=")
        if len(name) >= 3 and "--endpoint".startswith(name):
            out.append(value if eq else (args[k + 1] if k + 1 < len(args) else ""))
    return out


def remote_only(argv):
    for k, a in enumerate(argv):
        base = os.path.basename(a)
        if base == "driver2.py":
            eps = endpoints(argv[k + 1:])
            return bool(eps) and all(e == "remote" for e in eps)
        if base == "run_side.sh":
            return argv[k + 1:k + 2] == ["remote"]
    return False


def one_cpu(pid):
    try:
        cpus = next(l.split(":", 1)[1].strip() for l in open(f"/proc/{pid}/status") if l.startswith("Cpus_allowed_list:"))
    except (OSError, StopIteration):
        return False
    return cpus.isdigit()


for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        argv = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        continue
    if not any(" " not in a and os.path.basename(a) in NAMES for a in argv):
        continue
    if UNDER and not any(a.startswith(UNDER + "/") for a in argv):
        continue
    if (remote_only(argv) and one_cpu(pid)) or ours(int(pid)):
        continue
    print(pid, " ".join(argv)[:160])
EOF
}
idle() {         # /metrics: idle, nothing running, nothing queued (no answer is not idle)
  curl -fsS -m 10 "$URL/metrics" 2>/dev/null | python3 -c 'import json, sys
l = json.load(sys.stdin)["live"]
sys.exit(0 if l.get("state") == "idle" and not l.get("running") and not l.get("queued") else 1)' 2>/dev/null
}
wait_idle() { local waited=0; until idle; do [ "$waited" -ge "$IDLE_WAIT_S" ] && return 1; sleep 10; waited=$((waited + 10)); done; }
profile_args() { sed -e 's/#.*//' "$R/config/profile.conf" | tr -d '\r' | xargs; }      # as strata-preflight.sh reads it
write_profile() {   # config/profile.conf naming the model, replaced in one step
  printf '%s\n' "${PROFILE_HEAD[@]}" "--profile deployed --model $1" > "$R/config/.profile.conf.$$" &&
    mv -f "$R/config/.profile.conf.$$" "$R/config/profile.conf" && [ "$(profile_args)" = "--profile deployed --model $1" ]
}
settled() {   # the tool finds nothing left to change, critical or not, in the file given for the model given
  python3 -B "$TOOL" "$1" --profile deployed --model "$2" --dry-run 2>/dev/null |
    python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(1 if d["changed"] or d["critical_changes"] else 0)'
}
checks() {    # every precondition but the model's files; refuses at the first that fails, changes nothing
  local state job b s arc_want arc_have
  ID0=$(unit_id)
  state=$(systemctl show -p ActiveState --value $UNIT); job=$(systemctl show -p Job --value $UNIT)
  [ "$state" = active ] && [ -z "$job" ] ||
    refuse "$UNIT is ${state:-in an unknown state}${job:+ with job $job queued}: switch it when it is active and nothing is queued"
  case "$(profile_args)" in
    "--profile deployed"|"--profile deployed --model iq3_s"|"--profile deployed --model iq3_xxs") ;;
    *) refuse "config/profile.conf holds '$(profile_args)', and this script writes only '--profile deployed --model <model>': switch by hand" ;;
  esac
  curl -fsS -m 10 "$URL/health" 2>/dev/null | grep -q '"loaded": *true' || refuse "$UNIT is active but its model is not loaded"
  b=$(busy) || refuse "cannot list the processes"
  [ -z "$b" ] || { say "refusing: a measurement, evaluation, pack build or switch is running:"; echo "$b"; exit 1; }
  s=$(strays)
  [ -z "$s" ] || { say "refusing: strata-preflight.sh would refuse the start while these run (it takes any command line with the engine's path and --serve for a second engine):"
                   echo "$s"; exit 1; }
  arc_want=$(grep -o 'echo [0-9]* > /sys/module/zfs/parameters/zfs_arc_max' "$R/systemd/zfs-arc-max.service" 2>/dev/null | cut -d' ' -f2)
  arc_have=$(cat "$ARC_FILE" 2>/dev/null)
  [[ "$arc_want" =~ ^[0-9]+$ ]] && [ "$arc_have" = "$arc_want" ] ||
    refuse "zfs_arc_max is ${arc_have:-unreadable}, $R/systemd/zfs-arc-max.service writes ${arc_want:-?}"
  sudo -n true 2>/dev/null || refuse "sudo -n does not work (the restart needs it)"
  python3 -B "$TOOL" --help 2>/dev/null | grep -q -- '--model' || refuse "$TOOL has no --model"
  deployment_ok "$tmp/check-before.txt" ||
    { say "refusing: scripts/check-deployment.sh finds a problem now; the switch would fail on it after the restart:"
      grep -E '^PROBLEM|no answer within' "$tmp/check-before.txt" || tail -3 "$tmp/check-before.txt"; exit 1; }
}
on_copy() {   # the tool on a copy of the live config: the model with its cuts, then nothing more to change; its files
  mkdir "$tmp/copy" && cp -p "$CFG" "$tmp/copy/cfg.json" || refuse "cannot copy $CFG"
  python3 -B "$TOOL" "$tmp/copy/cfg.json" --profile deployed --model "$MODEL" > "$tmp/copy/out.json" 2> "$tmp/copy/err" ||
    refuse "the tool cannot write $MODEL's config on a copy of the live config: $(head -c 300 "$tmp/copy/err")"
  python3 - "$tmp/copy/out.json" "$MODEL" "$WANT_SPLIT" <<'EOF' || refuse "on a copy of the live config the tool does not give $MODEL with the cuts $WANT_SPLIT"
import json, sys
d = json.load(open(sys.argv[1]))
print("on a copy of the live config: model", d["model"], "split", d["layer_split"])
for c in d["changes"]:
    print("  change:", c)
sys.exit(0 if d["model"] == sys.argv[2] and str(d["layer_split"]) == sys.argv[3] else 1)
EOF
  settled "$tmp/copy/cfg.json" "$MODEL" || refuse "after writing $MODEL's config on a copy, the tool still finds something to change"
  python3 - "$tmp/copy/cfg.json" <<'EOF' || refuse "the files that config names are not all there, or not the files the pack was built from"
import json, os, sys
args = json.load(open(sys.argv[1]))["args"]


def one(flag):
    values = [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]
    if len(values) != 1:
        sys.exit(f"  not one {flag}: {values}")
    return values[0]


pack, native, ple = one("--pack"), one("--native"), one("--ple-gguf")
files = [os.path.join(pack, f) for f in ("native_experts.txt", "experts.bin", "experts.bin.src.json", "index.txt",
                                         "dense.bin", "tokenizer/vocab.json")] + [native, ple]
bad = [f"  missing or empty: {p}" for p in files if not (os.path.isfile(p) and os.path.getsize(p) > 0)]
if not bad:
    src = json.load(open(os.path.join(pack, "experts.bin.src.json")))
    built = [(s.get("name"), s.get("size")) for s in src.get("shards") or []]
    named = [(os.path.basename(p), os.path.getsize(p)) for p in (native, ple)]
    if built != named:
        bad.append(f"  the pack was built from {built}; the config names {named}")
    size = os.path.getsize(os.path.join(pack, "experts.bin"))
    if src.get("bytes") != size:
        bad.append(f"  experts.bin has {size} bytes; the pack's record says {src.get('bytes')}")
print("the model's files: the pack", os.path.basename(pack), "and", os.path.basename(native), "with",
      os.path.basename(ple), "-", "complete" if not bad else "NOT complete")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
EOF
}
restore() {   # config/profile.conf and the live config as they were before this script (each one only if it changed)
  if { cmp -s "$tmp/profile.conf.before" "$R/config/profile.conf" ||
       { cp -p "$tmp/profile.conf.before" "$R/config/.profile.conf.$$" && mv -f "$R/config/.profile.conf.$$" "$R/config/profile.conf"; }; } &&
     { cmp -s "$tmp/cfg.before" "$CFG" || { cp -p "$tmp/cfg.before" "$CFG.restore.$$" && mv -f "$CFG.restore.$$" "$CFG"; }; }; then
    say "config/profile.conf and the live config are as they were; the service was not restarted"
  else
    keep=1
    say "ERROR: could not put config/profile.conf and $CFG back; their copies: $tmp/profile.conf.before, $tmp/cfg.before"
  fi
}
undo() {      # a failure or a signal before the restart: nothing interrupts the restore
  trap '' INT TERM HUP QUIT PIPE
  say "$1"; restore
  [ $keep = 1 ] && exit 5
  exit 1
}
switch_to() {   # switch_to <model> <Q> <file>: restart, record the start in <file> and <file>.check; 0 when <model> came up
  local m=$1 q=$2 file=$3 rc secs state health pid args ok=yes avail_before avail_after start_line t0 t_start
  avail_before=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  start_line=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  say "restarting strata-q3 on $m; MemAvailable ${avail_before} MiB; waiting at most $RESTART_WAIT_S s"
  t0=$(date +%s); t_start=$(date '+%F %T')
  sudo -n systemctl reset-failed $UNIT 2>/dev/null     # its failed state and start limit: earlier starts do not count
  timeout -k 30 "$RESTART_WAIT_S" sudo -n systemctl restart $UNIT; rc=$?
  secs=$(( $(date +%s) - t0 ))
  state=$(systemctl is-active $UNIT)
  avail_after=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  health=$(curl -s -m 10 "$URL/health")
  pid=$(own_engine)
  args=$([ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline")
  {
    echo "switch to $m at $t_start: systemctl restart exit $rc after $secs s (124: no answer within $RESTART_WAIT_S s), state $state"
    echo "MemAvailable before ${avail_before} MiB, after ${avail_after} MiB"
    echo "health: $health"
    echo "engine args: $args"
    echo "--- engine start-up lines"
    tail -n +"$((start_line + 1))" "$LOG" | grep -a -v -E 'capturing the|verify: window up to|^strata serve: prompt|^strata batch' | head -200
    echo "--- service journal since the restart"
    sudo -n journalctl -u $UNIT --since "$t_start" --no-pager -q 2>/dev/null |
      grep -E 'strata-preflight|strata-wait-ready|Started|Stopped|Failed|error|loaded after' | head -20
  } | redact > "$file"
  [ "$rc" = 0 ] && [ "$state" = active ] || ok=no
  grep -q "\"model\": \"qwen3.8-flash-next-$m\"" <<< "$health" || { say "health does not name qwen3.8-flash-next-$m"; ok=no; }
  grep -q -- "--native [^ ]*/models/$q/Qwen3.8-Flash-Next-GSQ-RCO-$q-00001-of-00002.gguf" <<< "$args" ||
    { say "the engine did not load the $q shard"; ok=no; }
  deployment_ok "$file.check" || { say "check-deployment.sh found problems or did not answer (see $file.check)"; ok=no; }
  say "restart exit $rc after $secs s; state $state; MemAvailable ${avail_before} -> ${avail_after} MiB; $m came up: $ok"
  [ $ok = yes ]
}
journal_tail() { sudo -n journalctl -u $UNIT -n 30 --no-pager 2>/dev/null | redact | tail -30; }
by_hand() {   # the commands that bring the service back on IQ3_XXS
  say "strata-q3 is not running as switched; these commands bring it back on IQ3_XXS (the restart takes 7-9 minutes):"
  echo "  # first: systemctl status $UNIT. 120 s after a failed start systemd starts it again by itself, with the"
  echo "  # files as they are (Restart=on-failure); a restart during that load cuts it short"
  printf "  printf '%%s\\\\n' '%s' '%s' '--profile deployed --model iq3_xxs' > %s\n" "${PROFILE_HEAD[@]}" "$R/config/profile.conf"
  echo "  python3 -B $TOOL $CFG --profile deployed --model iq3_xxs"
  echo "  pgrep -af '[e]ngine-cuda12/strata --serve'    # only strata-q3's own engine, or none: the preflight refuses a second one"
  echo "  sudo systemctl reset-failed $UNIT; sudo systemctl restart $UNIT"
  echo "  $R/scripts/check-deployment.sh"
}

# 1. the checks (with --check, the end)
checks
wait_idle || refuse "$UNIT has been serving requests for $IDLE_WAIT_S s (someone's request is running)"
on_copy
[ "$(unit_id)" = "$ID0" ] || refuse "$UNIT was started again while this script checked and waited; run it again"
[ $CHECK = 1 ] && { say "every check passed for $MODEL; nothing was changed (--check)"; exit 0; }

# 2. profile.conf and the live config, put back as they were if a step fails before the restart
cp -p "$R/config/profile.conf" "$tmp/profile.conf.before" && cp -p "$CFG" "$tmp/cfg.before" ||
  refuse "cannot save config/profile.conf and the live config"
trap 'undo "interrupted"' INT TERM HUP QUIT PIPE
write_profile "$MODEL" || undo "cannot write config/profile.conf"
python3 -B "$TOOL" "$CFG" --profile deployed --model "$MODEL" > /dev/null || undo "the tool could not write the live config"
settled "$CFG" "$MODEL" || undo "the written config still differs from config/profile.conf"
# the moment before the restart: still nothing running beside, nothing in flight
b=$(busy) && [ -z "$b" ] || undo "a measurement, evaluation, pack build or switch started: $(echo $b | cut -c1-200)"
s=$(strays); [ -z "$s" ] || undo "strata-preflight.sh would refuse the start while these run: $(echo $s | cut -c1-200)"
idle || undo "a request came in"
[ "$(unit_id)" = "$ID0" ] ||
  undo "$UNIT was started again by someone else after the checks; its start may have read the files this script wrote: see /health"
trap '' INT TERM HUP QUIT PIPE            # from here on, these are ignored: the restart and its checks finish

# 3. the restart, and when the model does not come up, IQ3_XXS again
switch_to "$MODEL" "$Q" "$OUT" && exit 0
journal_tail
if [ "$MODEL" = iq3_s ]; then
  say "the switch to iq3_s did not come up (see $OUT): switching back to iq3_xxs"
  if write_profile iq3_xxs && python3 -B "$TOOL" "$CFG" --profile deployed --model iq3_xxs > /dev/null && settled "$CFG" iq3_xxs; then
    s=$(strays)
    if [ -z "$s" ]; then
      switch_to iq3_xxs IQ3_XXS "$OUT.rollback" && { say "strata-q3 is back on IQ3_XXS (see $OUT.rollback)"; exit 3; }
      journal_tail
    else
      say "ERROR: strata-preflight.sh would refuse the start while these run:"; echo "$s"
    fi
  else
    say "ERROR: cannot write IQ3_XXS's config/profile.conf and live config"
  fi
fi
by_hand
exit 4
