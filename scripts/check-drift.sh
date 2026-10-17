#!/usr/bin/env bash
# Read-only drift check: is the machine still running what this repo says?
#   1. the files installed outside the repo (systemd units, logrotate, the root-owned watchdog) equal the repo's copies,
#      are root's with the modes install-system-files.sh gives them, and no drop-in overrides a unit; the ZFS ARC
#      limits are what zfs-arc-max.service writes
#   2. the live Strata config equals config/strata-iq3_xxs.deployed.json, and that snapshot is what
#      configure_strata_q3.py makes from setup's original config with the arguments in config/profile.conf
#   3. the engine and the other deployed files match config/deployed-artifacts.sha256
#      (--full also hashes the model shards and the MTP files, 77 GB: 590 s on 2026-10-18, run under nice -n 19 ionice -c3)
# Exit 0 when everything matches, 1 when anything drifted.  After an intended change, update the repo copy (and the
# snapshot / the sha256 list) in the same commit as the change.
# Usage: scripts/check-drift.sh [--full]
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
STRATA_DIR=${STRATA_DIR:-$HOME/dev/strata}
DEV=$(dirname "$STRATA_DIR")
FULL=0; [ "${1:-}" = --full ] && FULL=1
bad=0
ok()    { printf 'ok     %s\n' "$1"; }
drift() { printf 'DRIFT  %s\n' "$1"; bad=1; }
same()  {   # repo copy, installed file, mode
  local own; own=$(stat -c '%U:%G %a' "$2" 2>/dev/null)
  if ! cmp -s "$1" "$2"; then drift "$2 differs from the repo copy (diff it)"
  elif [ "$own" != "root:root $3" ]; then drift "$2 is $own, not root:root $3"
  else ok "$2"; fi
}

echo "== installed files"
for u in strata-q3.service strata-q3-health.service strata-q3-health.timer zfs-arc-max.service; do
  same "$D/systemd/$u" "/etc/systemd/system/$u" 644
  # every drop-in systemd applies (/etc, /run, systemctl set-property ...), not only the directories named here
  dropins=$(systemctl show -p DropInPaths --value "$u" 2>/dev/null)
  [ -z "$dropins" ] || drift "$u has drop-ins that override the repo's unit: $dropins"
  # a unit file or drop-in changed on disk but not loaded yet (no daemon-reload): DropInPaths cannot show it
  [ "$(systemctl show -p NeedDaemonReload --value "$u" 2>/dev/null)" = no ] ||
    drift "$u changed on disk since the last daemon-reload (systemctl cat $u)"
done
same "$D/systemd/strata-q3.logrotate" /etc/logrotate.d/strata-q3 644
same "$D/scripts/strata-health.sh" /usr/local/sbin/strata-q3-health 755     # the timer runs it as root
for param in zfs_arc_max zfs_arc_shrinker_limit; do
  want=$(grep -o "echo [0-9]* > /sys/module/zfs/parameters/$param\b" "$D/systemd/zfs-arc-max.service" | cut -d' ' -f2)
  have=$(cat "/sys/module/zfs/parameters/$param" 2>/dev/null)
  if [ -n "$want" ] && [ "$have" = "$want" ]; then ok "$param = $have"
  else drift "$param is ${have:-unreadable}, zfs-arc-max.service writes ${want:-?} (did it run at boot?)"; fi
done

echo "== Strata config"
live=$STRATA_DIR/strata-iq3_xxs.json
PROFILE_ARGS=$(sed -e 's/#.*//' "$D/config/profile.conf" | tr -d '\r' | xargs)     # parsed as strata-preflight.sh does
[ -n "$PROFILE_ARGS" ] || drift "config/profile.conf has no arguments (strata-preflight.sh refuses to start)"
# an api_key lives in the live config only, never in the repo (deployment record, section 5): compare without it
has_key=$(python3 -c 'import json, sys; print(0 if json.load(open(sys.argv[1], encoding="utf-8-sig")).get("api_key") in (None, "") else 1)' "$live" 2>/dev/null || echo 0)
if [ "$has_key" = 1 ]; then
  # equal JSON text, not equal Python values (4.0 == 4 and 1 == True in Python; the server tells them apart)
  if python3 -c 'import json, sys; a = json.load(open(sys.argv[1], encoding="utf-8-sig")); a.pop("api_key", None); c = lambda x: json.dumps(x, sort_keys=True); sys.exit(c(a) != c(json.load(open(sys.argv[2]))))' \
       "$live" "$D/config/strata-iq3_xxs.deployed.json"; then
    ok "$live (api_key set: compared without it)"
  else
    drift "$live differs from config/strata-iq3_xxs.deployed.json (api_key ignored)"
  fi
else
  if cmp -s "$D/config/strata-iq3_xxs.deployed.json" "$live"; then ok "$live"
  else drift "$live differs from config/strata-iq3_xxs.deployed.json"; fi
fi
tmp=$(mktemp -d); trap 'rm -r -- "$tmp"' EXIT
cp "$D/config/strata-iq3_xxs.setup.json" "$tmp/strata-iq3_xxs.json"
# shellcheck disable=SC2086  # PROFILE_ARGS holds the tool's arguments, one word each
if python3 "$D/tools/configure_strata_q3.py" "$tmp/strata-iq3_xxs.json" $PROFILE_ARGS > /dev/null &&
   cmp -s "$tmp/strata-iq3_xxs.json" "$D/config/strata-iq3_xxs.deployed.json"; then
  ok "setup's config + configure_strata_q3.py $PROFILE_ARGS = the deployed snapshot"
else
  drift "configure_strata_q3.py with config/profile.conf no longer makes the deployed snapshot from setup's config"
fi

echo "== deployed files (sha256, config/deployed-artifacts.sha256)"
entries=0
while IFS= read -r line || [ -n "$line" ]; do                 # also a last line without a newline
  case "$line" in ''|'#'*) continue ;; esac
  if ! [[ "$line" =~ ^([0-9a-f]{64})\ \ ([^[:space:]]+)$ ]]; then drift "config/deployed-artifacts.sha256: not a '<sha256>  <path>' line: $line"; continue; fi
  sum=${BASH_REMATCH[1]}; path=${BASH_REMATCH[2]}; entries=$((entries + 1))
  case "$path" in Strata-data/models/*|Strata-data/mtp/*) [ $FULL = 1 ] || { echo "skip   $path (--full)"; continue; } ;; esac
  [ "$path" = strata/strata-iq3_xxs.json ] && [ "$has_key" = 1 ] && { echo "skip   $path (api_key set: checked above)"; continue; }
  if [ ! -e "$DEV/$path" ]; then drift "$DEV/$path is missing"; continue; fi
  if [ "$(sha256sum < "$DEV/$path" | cut -d' ' -f1)" = "$sum" ]; then ok "$path"; else drift "$path (sha256 changed)"; fi
done < "$D/config/deployed-artifacts.sha256"
[ "$entries" -gt 0 ] || drift "config/deployed-artifacts.sha256 lists no files"

echo
if [ $bad = 0 ]; then echo "no drift"; else echo "drift found: see DRIFT lines above"; fi
exit $bad
