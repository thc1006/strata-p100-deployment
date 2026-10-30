#!/usr/bin/env bash
# ExecStartPre of strata-q3.service.  It waits where waiting helps and refuses only what a start cannot fix:
#   - no other Strata engine or :8080 listener (a manual start would load a second copy)
#   - the four P100s are there (the unit can be dispatched before the driver has bound them at boot): up to 120 s
#   - enough RAM for the locked PLE table (26.8 GiB, results/iq3s-memory-budget-20261019.txt) + resident experts + the
#     engine: waits up to 25 min (another user's job may be finishing), counting what the ZFS ARC can give back above
#     its minimum.  Failing fast here would use up the unit's start limit in minutes and leave the service down.
#   - the engine binary is the deployed one, the model loads at start (no lazy_load), and the config has no CRITICAL
#     difference from config/profile.conf (setup.sh's start and update.sh rewrite it): every difference is critical
#     except the tuning steps and the web Settings fields listed in configure_strata_q3.py NOTE_ONLY_KEYS, which warn
set -u
D=$(cd "$(dirname "$0")/.." && pwd)
STRATA=${STRATA_DIR:-/home/thc1006/dev/strata}
CFG=$STRATA/strata-iq3_xxs.json
# what a start needs usable: what IQ3_S's starts took of the 58 GiB they saw (35.6 GiB at most: 22.4-22.7 GiB
# were available after them, results/ab3-iq3s-*-switch.txt), plus the 6 GiB floor of decision 0004.  36 GiB, the
# gate until cc4f7bf (2026-10-27), was set for IQ3_XXS, which takes about 31 GiB (results/deploy-0.1.45-20261019.txt).
MIN_AVAIL_GIB=${STRATA_MIN_AVAIL_GIB:-42}
MEM_WAIT_S=${STRATA_MEM_WAIT_S:-1500}
say() { echo "strata-preflight: $*"; }
[[ "$MIN_AVAIL_GIB" =~ ^[0-9]+$ && "$MEM_WAIT_S" =~ ^[0-9]+$ ]] ||
  { say "STRATA_MIN_AVAIL_GIB and STRATA_MEM_WAIT_S must be whole numbers"; exit 1; }

listeners=$(ss -ltnH '( sport = :8080 )') || { say "ss failed: cannot check :8080; not starting"; exit 1; }
if pgrep -f '[e]ngine-cuda12/strata --serve' >/dev/null || [ -n "$listeners" ]; then
  say "another Strata engine or a listener on :8080 is already running; not starting a second copy"
  exit 1
fi

gpus_ready() { n=$(timeout 10 nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)
               [ "$n" -ge 4 ] && [ -e /dev/nvidia3 ]; }
uptime_s() { local u; read -r u _ < /proc/uptime; echo "${u%.*}"; }   # monotonic: NTP may step the clock at boot
deadline=$(( $(uptime_s) + 120 ))                         # the driver at boot
until gpus_ready; do
  [ "$(uptime_s)" -ge "$deadline" ] && { say "only $n GPUs (or no /dev/nvidia3) after 120 s; not starting"; exit 1; }
  sleep 5
done

usable_gib() {   # MemAvailable + the ARC above its minimum (MemAvailable does not count ARC on this ZFS); 0 if unreadable
  local m r
  m=$(awk '/^MemAvailable:/ {printf "%d", $2 / 1048576}' /proc/meminfo 2>/dev/null)
  r=$(awk '$1 == "size" {s = $3} $1 == "c_min" {c = $3} END {r = s - c; if (r < 0) r = 0; printf "%d", r / 1073741824}' \
      /proc/spl/kstat/zfs/arcstats 2>/dev/null)
  [[ "$m" =~ ^[0-9]+$ ]] || m=0; [[ "$r" =~ ^[0-9]+$ ]] || r=0      # fail closed: unknown counts as nothing free
  echo $((m + r))
}
waited=0
while [ "$(usable_gib)" -lt "$MIN_AVAIL_GIB" ]; do
  [ "$waited" -ge "$MEM_WAIT_S" ] && { say "only $(usable_gib) GiB usable after ${waited} s (need ${MIN_AVAIL_GIB}); not starting"; exit 1; }
  [ $((waited % 300)) -eq 0 ] && say "waiting for memory: $(usable_gib) GiB usable, need ${MIN_AVAIL_GIB} (the PLE table is locked)"
  sleep 15; waited=$((waited + 15))
done

# the ARC cap only warns: without zfs-arc-max.service the figure above counts an ARC that can grow back
arc_want=$(grep -E '^[[:blank:]]*ExecStart[[:blank:]]*=' /etc/systemd/system/zfs-arc-max.service 2>/dev/null |
           grep -o 'echo [0-9]* > /sys/module/zfs/parameters/zfs_arc_max' | cut -d' ' -f2)
arc_have=$(cat /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null)
if [ -z "$arc_want" ] || [ "$arc_have" != "$arc_want" ]; then
  say "note: zfs_arc_max is ${arc_have:-unreadable}, zfs-arc-max.service writes ${arc_want:-?} (did it run?)"
fi

[ -f "$D/config/profile.conf" ] || { say "config/profile.conf is missing"; exit 1; }
PROFILE_ARGS=$(sed -e 's/#.*//' "$D/config/profile.conf" | tr -d '\r' | xargs)
[ -n "$PROFILE_ARGS" ] || { say "config/profile.conf has no arguments"; exit 1; }
err=$(mktemp); trap 'rm -f "$err"' EXIT
# python3 -I: no user site-packages (a .pth there would run here) and no PYTHON* variables; the tool imports only
# the standard library
# shellcheck disable=SC2086  # PROFILE_ARGS holds the tool's arguments, one word each
out=$(python3 -I "$D/tools/configure_strata_q3.py" "$CFG" $PROFILE_ARGS --dry-run 2>"$err") || {
  say "config check failed: $(cat "$err")"; exit 1; }
# fail closed: no verdict (a JSON or Python error) is a refusal, not a pass
verdict=$(printf '%s' "$out" | python3 -I -c '
import json, sys
d = json.load(sys.stdin)
cfg = json.load(open(sys.argv[1], encoding="utf-8-sig"))     # server.py reads it the same way
want_exe = sys.argv[2] + "/engine-cuda12/strata"
crit = list(d["critical_changes"])
if cfg.get("exe") != want_exe:
    crit.append("exe is not the deployed engine (" + want_exe + ")")
for c in crit:
    print("CRITICAL " + c)
if d["semantic_change"]:
    for c in d["changes"]:
        if c not in d["critical_changes"]:
            print("WARN " + c)
elif d["changed"]:
    print("WARN " + d["changes"][0])
' "$CFG" "$STRATA") || { say "config verdict failed (tool output was not the expected JSON); not starting"; exit 1; }
if printf '%s\n' "$verdict" | grep -q '^CRITICAL'; then
  say "the live config cannot start as config/profile.conf ($PROFILE_ARGS) describes it:"
  printf '%s\n' "$verdict" | grep '^CRITICAL' | sed 's/^/  /'
  say "fix: python3 $D/tools/configure_strata_q3.py $CFG $PROFILE_ARGS   (or change config/profile.conf for an experiment);"
  say "     the tool does not change exe or lazy_load: correct those in $CFG by hand"
  exit 1
fi
printf '%s\n' "$verdict" | grep '^WARN' | sed 's/^WARN /strata-preflight: note: differs from the profile (allowed): /'
say "ok: 4 GPUs, $(usable_gib) GiB usable, config matches ($PROFILE_ARGS) in everything that decides memory and placement"
