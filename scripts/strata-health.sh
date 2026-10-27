#!/usr/bin/env bash
# strata-q3-health.service (its timer, every 2 min, as root).  Installed as a root-owned copy:
#   sudo install -o root -g root -m 0755 scripts/strata-health.sh /usr/local/sbin/strata-q3-health
# (the copy in the repo is writable by the service's user, so root must not run that one).
#
#   active, HTTP answers, this unit's engine process exists (serving, or reloading)  -> healthy
#   active, HTTP answers, no engine, model not unloaded on purpose                   -> POST /v1/load: server.py's own
#                                                                                       reload, no systemd start used
#   active, HTTP does not answer for 3 checks (6 min), or no engine for 8 (16 min)   -> systemctl try-restart
#   failed (start limit, preflight gave up, ...)                                     -> reset-failed + start after a
#                                                       cool-down that doubles per attempt: 30 min, 1 h, 2 h ... 8 h
#   activating / deactivating / inactive (an admin stopped it)                       -> nothing
# A 401/403 means the server is up (a key the script could not read): healthy, with a warning; the engine check is
# skipped then, because it needs /v1/models.
# The key is read as the service's user (setpriv, no PAM): root does not open a path that user can point elsewhere.
# It never goes on a command line (ps shows those) nor into curl's config syntax (-K would run directives hidden in
# a crafted key): a key with control characters is not sent, any other is written to a 0600 header file (-H @file).
# curl runs with -q, its first option, so that no .curlrc changes its requests or where their answers go, and with
# --noproxy '*', so that no proxy that the environment names (http_proxy, all_proxy) gets the requests and the key.
set -u
export LC_ALL=C                                           # systemctl timestamps and date -d in one locale
UNIT=${STRATA_UNIT:-strata-q3.service}       # overridable only to test this script on a throwaway unit
URL=${STRATA_URL:-http://127.0.0.1:8080}
CFG=${STRATA_CFG:-/home/thc1006/dev/strata/strata-iq3_xxs.json}
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
RUN=/run/strata-q3-health                                 # tmpfs: every counter starts from 0 after a reboot
# a dry run reads the counters and changes nothing there: its temporary files go to a directory of its own
if [ $DRY = 1 ]; then work=$(mktemp -d) || exit 1; else mkdir -p "$RUN"; chmod 0700 "$RUN"; work=$RUN; fi
trap '[ "$work" = "$RUN" ] || rm -rf "$work"' EXIT
COOLDOWN_S=${STRATA_FAILED_COOLDOWN_S:-1800}
COOLDOWN_MAX_S=28800
log() { [ $DRY = 1 ] || logger -t strata-health -p "user.$1" -- "$2" 2>/dev/null; echo "strata-health [$1]: $2"; }
act() { if [ $DRY = 1 ]; then echo "strata-health: (dry run) would run: $*"; else "$@"; fi; }
num() { local v; v=$(cat "$RUN/$1" 2>/dev/null); [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0; }
put() { if [ $DRY = 1 ]; then echo "strata-health: (dry run) would set $1 to $2"; else echo "$2" > "$RUN/$1"; fi; }
drop() { if [ $DRY = 1 ]; then echo "strata-health: (dry run) would clear $*"; return 0; fi; local f; for f in "$@"; do rm -f "$RUN/$f"; done; }

state=$(systemctl show -p ActiveState --value "$UNIT")
case "$state" in
  active) ;;
  failed)
    entered=$(date -d "$(systemctl show -p InactiveEnterTimestamp --value "$UNIT")" +%s 2>/dev/null || date +%s)
    age=$(( $(date +%s) - entered ))
    tries=$(num failed_retries)
    wait_s=$(( COOLDOWN_S << (tries < 5 ? tries : 5) )); [ "$wait_s" -gt "$COOLDOWN_MAX_S" ] && wait_s=$COOLDOWN_MAX_S
    if [ "$age" -ge "$wait_s" ]; then
      log err "$UNIT has been failed for ${age} s: reset-failed and start again (automatic attempt $((tries + 1)))"
      put failed_retries $((tries + 1))
      act systemctl reset-failed "$UNIT"
      act systemctl start --no-block "$UNIT"
    else
      log warning "$UNIT is failed (${age} s); next automatic attempt after ${wait_s} s"
    fi
    exit 0 ;;
  *) drop http_failures engine_failures; exit 0 ;;
esac

# the first key of "api_key" as server.py's key_list reads it (a list: its items; a string: comma-separated, the
# blanks around each dropped), from a regular file of at most 1 MiB; nothing if it holds a control character
as_user=()
if [ "$(id -u)" = 0 ]; then
  u=$(systemctl show -p User --value "$UNIT"); g=$(systemctl show -p Group --value "$UNIT")
  as_user=(setpriv --reuid="${u:-nobody}" --regid="${g:-nogroup}" --clear-groups --no-new-privs)
fi
key=$("${as_user[@]}" python3 -I - "$CFG" <<'EOF' 2>/dev/null
import json, os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    sys.exit()
v = json.loads(os.read(fd, 1 << 20) or b"{}").get("api_key")
keys = [str(k) for k in v] if isinstance(v, (list, tuple)) else ("" if v is None else str(v)).split(",")
keys = [k.strip() for k in keys if k.strip()]
k = keys[0] if keys else ""
print("" if any(ord(c) < 32 or ord(c) == 127 for c in k) else k)
EOF
)
hdr=$(mktemp -p "$work"); body=$(mktemp -p "$work")
trap 'rm -f "$hdr" "$body"; [ "$work" = "$RUN" ] || rm -rf "$work"' EXIT
chmod 0600 "$hdr"
[ -n "$key" ] && printf 'Authorization: Bearer %s\n' "$key" > "$hdr"
# at most 1 MiB of body in /run (a tmpfs): whatever listens on :8080 cannot fill it.  SIGXFSZ is ignored, so a larger
# body is cut (the write fails) and curl still reports the status instead of being killed without one
code=$( (trap '' XFSZ; ulimit -f 1024; curl -q --noproxy '*' -s -o "$body" -w '%{http_code}' -m 20 -H @"$hdr" "$URL/v1/models") 2>/dev/null)
mainpid=$(systemctl show -p MainPID --value "$UNIT")
engine=$(pgrep -P "${mainpid:-0}" -f '[e]ngine-cuda12/strata --serve' | head -1)
if [ "$code" = 401 ] || [ "$code" = 403 ]; then
  log warning "/v1/models answered $code: the server is up but the check cannot read its key; counted healthy, engine not checked"
  drop http_failures engine_failures failed_retries; exit 0
fi
if [ "$code" = 200 ]; then
  drop http_failures
  if [ -n "$engine" ] || grep -q '"unloaded"' "$body"; then
    drop engine_failures failed_retries; exit 0
  fi
  name=engine_failures; limit=8
  n=$(( $(num $name) + 1 ))
  log warning "no engine process under server.py (check $n/$limit): asking server.py to load it (POST /v1/load)"
  act curl -q --noproxy '*' -s -o /dev/null -m 3 -X POST -H 'Content-Type: application/json' -H @"$hdr" -d '{}' "$URL/v1/load"
else
  name=http_failures; limit=3
  n=$(( $(num $name) + 1 ))
  log warning "the HTTP server does not answer (code ${code:-none}, check $n/$limit)"
fi
put $name "$n"
if [ "$n" -ge "$limit" ]; then
  drop http_failures engine_failures
  log err "restarting $UNIT after $n failed checks"
  act systemctl try-restart --no-block "$UNIT"           # never starts a unit an admin is stopping
fi
