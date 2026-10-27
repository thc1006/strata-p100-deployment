#!/bin/bash -p
# Read-only drift check: does this machine have and run what this repo says?
#   1. the files installed outside the repo (systemd units and drop-ins, logrotate, the root-owned watchdog) equal the
#      repo's copies and are root's with the modes install-system-files.sh gives them, and the drop-in directories of
#      the repo's (systemd/<unit>.d) have no other .conf installed; what systemd has LOADED of each unit (its
#      FragmentPath, its drop-ins, and every setting the repo's unit file and drop-ins have, read with systemctl show)
#      is what they say, so a drop-in of another's, an override in another directory, or a change not yet reloaded
#      shows (for a unit the repo has only drop-ins for, user-1003.slice, the system's own file and drop-ins are its
#      base, and a copy of the repo's values that systemctl set-property --runtime made is noted; one that sets
#      nothing, or a link to /dev/null, masks a drop-in of its name and is drift); every drop-in directory that
#      systemd reads for the unit (<unit>.d, each dash prefix's and <type>.d, in each directory of its search path)
#      holds no .conf that it has not loaded (a new directory too: daemon-reload); the memory limits in force (the
#      cgroup's memory.max and memory.swap.max) are the ones they give; the ZFS ARC limits are what zfs-arc-max.service
#      writes.  An empty assignment (ExecStart=, Environment=) empties the list before it, as systemd does; a drop-in
#      of the repo that sets for strata-q3.service or zfs-arc-max.service what the rest of the check reads from the
#      unit file alone (ExecStart=; strata-q3.service's ExecStartPre= and the settings of its process) is drift: set
#      it in the unit file
#   2. the host's units are in the states config/host-units.conf gives (scripts/host-units.sh check)
#   3. the live Strata config (the one the repo's unit starts server.py with) equals
#      config/strata-iq3_xxs.deployed.json, and that snapshot is what configure_strata_q3.py makes from setup's
#      original config with the arguments in config/profile.conf
#   4. the deployed files match config/deployed-artifacts.sha256 (paths under ~/dev, or absolute).  Files over 1 GiB
#      are hashed only with --full (they are read from disk: tens of minutes; run it under nice -n 19 ionice -c3)
#   5. the Strata tree is the commit the list pins, file by file (hashed here, not taken from git status; its text
#      files as the commit's own git attributes say, and none from elsewhere); where server.py imports from, there is
#      no module, package or namespace package (a directory) that the commit does not have and the list does not
#      name (a name Python can import: an identifier, with a module's suffix), and no cached bytecode that Python
#      would run instead of its source; the venv's python is the system's,
#      and the venv holds the packages config/strata-venv.txt lists, with the RECORDs it pins, their files as those
#      give them (by a sha256 or stronger hash; and their bytecode what their sources compile to), and no other file
#      but what python -m venv makes and what the list names; the service user's site-packages hold no .pth,
#      usercustomize or sitecustomize; no shared Chat settings file overrides the config
#   6. the checkout the service runs its scripts from (the repo's unit names it) is its commit, file by file, with no
#      other file and no ignored one where the service runs code from.  So run this check after a commit: before it,
#      the change is drift.  Run from another checkout, the two must be at one commit, unless --candidate says that
#      this tree is a commit to be deployed (scripts/deploy.sh checks one so)
#   7. the service manager gives every service only the locale and a PATH that only root can change; while strata-q3
#      is active: server.py runs as its unit says (command line, program, directory, user, limits, the file system
#      its ProtectSystem=, PrivateTmp= and ProtectHome= give, environment, and nothing else in its environment but
#      what systemd sets, with HOME, USER, LOGNAME and SHELL from the user's passwd entry); it runs one engine (or none,
#      with the model unloaded on purpose) and no other engine runs; the engine is the listed binary with the command
#      line, the directory and the environment server.py makes of the live config, maps the listed CUDA libraries,
#      maps no file outside the system's directories that the list does not name or that was replaced since it
#      started, and no file on its library path comes before the one it maps under that library's SONAME (in each
#      directory, the subdirectories that this CPU's loader searches first: glibc-hwcaps/x86-64-v3 ... x86_64); what
#      listens on the server's port is server.py's, and only then does it get the key (looked at again right before
#      each request); the server has no shared settings (GET /settings).  A file the service read at its start (its
#      code, its venv's files, the listed files, its installed unit) that changed after it is drift (restart pending):
#      its modification time, or its inode alone (a link, its mode or its owner, or a content put back with its old
#      time, as cp -p puts one back); but one that the engine maps, the very file (its device and inode), whose inode
#      alone changed and whose content is what the list says, is noted (over 1 GiB it is hashed only with --full: run
#      it).  An entry added, removed or renamed after the start where the service imports from is noted (the first 8
#      such directories, with up to 4 entries of each that are newer than the start; a module added there is drift,
#      above); so is a start script of the service's checkout that changed, or a directory of the engine's library path
#      (it takes effect at the next start)
# Exit 0 when everything matches, 1 when anything drifted, 2 on bad usage.  A file it reads that is not a regular file
# (a FIFO, a device) is drift, and it does not read it (git, which it runs, reads a repository's own files as they
# are; no git variable of the caller's reaches it).  So is a line of a unit that ends with a backslash, which goes on
# on the next line: systemd joins the two, this check does not (nor does scripts/test.sh).  It reads a unit's lines as
# systemd does: the blanks it skips and strips are " \t\n\r" only, so "\f# x \" is no comment to it either.  A value
# is compared as systemd shows it: a boolean in any of its spellings (true, on, 1) as yes or no, a mode as octal, a
# limit's soft:hard pair, a size's K, M, G, a time span's units (a bare number in the setting's own), a command's
# @ (the next word is argv[0]) and -, and Environment= from systemd's bus (busctl), item by item.  Not a specifier
# (%n) or a C escape (\t, \x41) in a value, or $VAR in a command, which systemd expands and this check takes as
# written: such a value is drift, though systemd may hold just what it means.  After an intended change, update the
# repo copy (and the snapshot / the sha256 list / the venv list) in the same commit as the change.  It prints the
# names of environment variables, never their values, never a command line of the service, and of an error of the
# server's only its kind.
# Usage: scripts/check-drift.sh [--full] [--candidate]
# (Test hooks: DRIFT_SYSROOT puts /etc, /usr/local/sbin, /sys (the cgroups too), the directories systemd reads drop-ins
# from (/run/systemd/..., /usr/lib/systemd/system, ...) and the service user's home under another root, DRIFT_OWNER is
# their owner (uid:gid, 0:0), DRIFT_ENGINE_RE the pattern of the engine's command line, DRIFT_URL the server's address,
# which gets the config's key only when it is 127.0.0.1, ::1 or localhost, DRIFT_GIT_TIMEOUT the seconds a git command
# may take (300; above 0, at most 86400).  One of a value the check cannot use (a time that is no such number, a pattern
# that does not compile or that matches its own text) stops it with drift.  A run with one of them set says so in a
# note, as it does for host-units.sh's own (HOST_UNITS_*), also for one that a BASH_ENV file set without export; and
# a run with BASH_ENV set says so.  scripts/deploy.sh clears them.)
# (bash also takes its options from the environment, SHELLOPTS and BASHOPTS, and functions, BASH_FUNC_*.  Run by its
# path, as scripts/deploy.sh runs it, this script is under bash -p (the first line), which takes none of them and runs
# no BASH_ENV file; the functions stay in the environment of the programs it runs, of which the one shell script,
# scripts/host-units.sh, it runs under bash -p too, which takes none of them either (a note names them).  Run as
# "bash scripts/check-drift.sh", it takes them: see below.  Two of those options cannot be
# undone from inside: noexec, under which no command runs, and onecmd, under which bash stops after the first line, a
# comment.  Under either, this script prints nothing and exits 0.  Nor can a function that the environment exports
# under the name of a builtin that the lines below run before they remove the functions (set, shopt, compgen, unset,
# declare): one named set or shopt is removed, and the options are turned off again after that; one named unset or
# compgen, or one of any name that a BASH_ENV file made readonly, is left, which stops the check; one named compgen
# together with one named declare goes unnoticed, and the functions stay.  That takes an environment made so on
# purpose.)
set -u -o pipefail
# the options that the environment (SHELLOPTS, BASHOPTS) turned on and that change how this script runs, off: under
# noglob, systemd/*.d would match nothing and the drop-ins would go unchecked, errexit would end it at the first test
# that fails, under keyword, export would print every variable's value, and under history, bash would write these
# lines to a HISTFILE.  The shells it starts get the options as they are then (SHELLOPTS and BASHOPTS hold them).
# xtrace and verbose stay: they change what goes to standard error, not what the check says.  Every function (one
# exported, BASH_FUNC_*, or one that a BASH_ENV file made) is removed: one could stand in for cmp or python3.  A note
# names them below (a removed function is no longer in the environment of what the check runs).  The names come from
# a list, not from read, which one named read would hold in a loop for ever
set +e +f +a +k +C +o posix +o history
shopt -u nullglob failglob dotglob nocaseglob nocasematch extglob globstar lastpipe xpg_echo expand_aliases
shell_env=
for v in $(compgen -e); do case $v in SHELLOPTS|BASHOPTS) shell_env+=" $v" ;; esac; done
for f in $(compgen -A function); do shell_env+=" $f()"; unset -f -- "$f"; done
if [[ -n $(declare -F) || -n $(compgen -A function) ]]; then    # one stood in for unset or compgen, or a readonly one
  printf 'DRIFT  %s\n\ndrift found: see DRIFT lines above\n' "functions from the environment or a BASH_ENV file are left after their removal (one is readonly, or has the name of a builtin that removes them): nothing checked"
  exit 1
fi
set -u -o pipefail +e +f +a +k +C +o posix +o history     # again, now that no function stands in for set or shopt
shopt -u nullglob failglob dotglob nocaseglob nocasematch extglob globstar lastpipe xpg_echo expand_aliases
D=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)     # without CDPATH, under which cd would print the directory
FULL=0; CANDIDATE=0
for a in "$@"; do
  case $a in
    --full) FULL=1 ;;
    --candidate) CANDIDATE=1 ;;
    *) echo "usage: scripts/check-drift.sh [--full] [--candidate]" >&2; exit 2 ;;
  esac
done
ROOT=${DRIFT_SYSROOT:-}
OWNER=${DRIFT_OWNER:-0:0}
ENGINE_RE=${DRIFT_ENGINE_RE:-[e]ngine-cuda12/strata --serve}
URL=${DRIFT_URL:-http://127.0.0.1:8080}
UNIT=strata-q3.service
# this check writes nothing and runs no program that a repository's settings name: no index refresh, no replaced
# object, no file system monitor; and no git attributes from outside a commit decide how its files compare: not the
# user's attributes file, nor the system's (a repository's info/attributes is drift, below).  No git variable of the
# caller's reaches git either: GIT_DIR or GIT_INDEX_FILE would point it at other files, and GIT_CONFIG_PARAMETERS (what
# git -c passes on) wins over GIT_CONFIG_COUNT
for v in $(compgen -e); do case $v in GIT_*) unset "$v" ;; esac; done
export GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 GIT_ATTR_NOSYSTEM=1 GIT_CONFIG_COUNT=3
export GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false GIT_CONFIG_KEY_1=core.untrackedCache GIT_CONFIG_VALUE_1=false
export GIT_CONFIG_KEY_2=core.attributesFile GIT_CONFIG_VALUE_2=/dev/null
# the repo's files that this check reads: a FIFO or a device in the place of one would hang it or do something when
# it is read, so it stops here (the comparison of the checkout with its commit, below, says what else differs)
for f in "systemd/$UNIT" systemd/strata-q3-health.service systemd/strata-q3-health.timer systemd/zfs-arc-max.service \
         systemd/strata-q3.logrotate scripts/strata-health.sh tools/configure_strata_q3.py config/profile.conf \
         config/host-units.conf config/deployed-artifacts.sha256 config/strata-venv.txt \
         config/strata-iq3_xxs.deployed.json config/strata-iq3_xxs.setup.json; do
  if [ -e "$D/$f" ] && [ ! -f "$D/$f" ]; then
    printf 'DRIFT  %s\n\ndrift found: see DRIFT lines above\n' "$D/$f is not a regular file: nothing else checked"
    exit 1
  fi
done
# the test hooks (above): one of a value that this check cannot use stops it too.  DRIFT_GIT_TIMEOUT is a number of
# seconds above 0, at most a day: a bound chosen here (Python 3.10's subprocess would wait up to 2147483 s, 24 days).
# DRIFT_ENGINE_RE must compile, and must not match its own text, as the default does not ([e]ngine): one that does
# finds this check's own processes, whose command lines hold it, among the engines
GIT_TIMEOUT=${DRIFT_GIT_TIMEOUT:-300}
unusable=$(python3 -I -S - "$GIT_TIMEOUT" "$ENGINE_RE" <<'EOF'
import re, sys
try:
    t = float(sys.argv[1])
except ValueError:
    t = float("nan")
if not 0 < t <= 86400:                            # not so for nan, nor for inf
    print("DRIFT_GIT_TIMEOUT is not a number of seconds above 0 and at most 86400")
try:
    if re.search(sys.argv[2], sys.argv[2]):
        print("DRIFT_ENGINE_RE matches its own text, so it would match this check's own processes: write a letter of it "
              "in brackets, as the default does ([e]ngine)")
except Exception:
    print("DRIFT_ENGINE_RE is not a pattern that Python's re compiles")
EOF
)
if [ -n "$unusable" ]; then
  while IFS= read -r l; do printf 'DRIFT  %s: nothing checked\n' "$l"; done <<< "$unusable"
  printf '\ndrift found: see DRIFT lines above\n'
  exit 1
fi
# what the repo's unit names: the live config server.py starts with, and the checkout of the preflight
# (blanks may stand around the key and before the value, as systemd strips them)
LIVE_CFG=$(sed -n 's/^[[:blank:]]*ExecStart[[:blank:]]*=.* --config \([^ ]*\).*/\1/p' "$D/systemd/$UNIT" 2>/dev/null)
LIVE_REPO=$(sed -n 's#^[[:blank:]]*ExecStartPre[[:blank:]]*=[[:blank:]]*[-@:+!]*\(/[^ ]*\)/scripts/strata-preflight\.sh.*#\1#p' \
  "$D/systemd/$UNIT" 2>/dev/null)
live=${LIVE_CFG:-/nonexistent/strata-iq3_xxs.json}
STRATA_DIR=$(dirname "$live")
DEV=$(dirname "$STRATA_DIR")
bad=0
ok()    { printf 'ok     %s\n' "$1"; }
drift() { printf 'DRIFT  %s\n' "$1"; bad=1; }
note()  { printf 'note   %s\n' "$1"; }
tmp=$(mktemp -d) || { drift "cannot create a temporary directory: nothing checked"; echo; echo "drift found: see DRIFT lines above"; exit 1; }
trap 'rm -rf -- "$tmp"' EXIT
# the listed files whose sha256 is the list's, and those it did not hash (over 1 GiB, without --full): a file that the
# engine maps and whose inode alone changed after the service's start is weighed with them, below
verified() { printf '%s\0' "$1" >> "$tmp/verified"; }
unhashed() { printf '%s\0' "$1" >> "$tmp/unhashed"; }
same()  {   # repo copy, installed file, mode
  local own; own=$(stat -c '%u:%g %a' "$2" 2>/dev/null)
  if [ ! -f "$1" ]; then drift "$1 is missing, or not a regular file"
  elif [ ! -r "$1" ]; then drift "$1 cannot be read: $2 is not compared with it"
  elif [ ! -e "$2" ]; then drift "$2 is missing"
  # cmp would block on a FIFO: only a regular file is compared.  One put there between this test and cmp still blocks
  # it; in the directories of these files, only root can do that
  elif [ ! -f "$2" ]; then drift "$2 is not a regular file"
  elif [ ! -r "$2" ]; then drift "$2 cannot be read: it is not compared with the repo copy"
  else
    cmp -s "$1" "$2"
    case $? in                                    # 1: they differ; 2: one could not be read after all
      0) if [ "$own" != "$OWNER $3" ]; then drift "$2 is owner:group mode $own, not $OWNER $3"; else ok "$2"; fi ;;
      1) drift "$2 differs from the repo copy (diff it)" ;;
      *) drift "$1 or $2 cannot be read: they are not compared" ;;
    esac
  fi
}
# the sha256 of a file, read only when it is a regular file: stat before the open (a device is not opened), O_NONBLOCK
# (a FIFO put there meanwhile does not block the open) and fstat after it.  "not-regular" when it is not one, nothing
# when it cannot be read
sha256_regular() {
  python3 -I -S -c 'import hashlib, os, stat, sys
try:
    if not stat.S_ISREG(os.stat(sys.argv[1]).st_mode):
        print("not-regular")
        sys.exit()
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)
    with open(fd, "rb", buffering=0) as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            print("not-regular")
            sys.exit()
        h, buf = hashlib.sha256(), bytearray(1 << 20)
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(memoryview(buf)[:n])
    print(h.hexdigest())
except OSError:
    pass' "$1" < /dev/null
}
[ -n "$LIVE_CFG" ] || drift "systemd/$UNIT names no --config: the live config is not known"
[ -n "$LIVE_REPO" ] || drift "systemd/$UNIT runs no .../scripts/strata-preflight.sh: the service's checkout is not known"
# the test hooks set (host-units.sh's too): the shell's own variables, not only its environment's, so that one that a
# BASH_ENV file set without export is named too.  A BASH_ENV file runs before this script, and can change anything in it
hooks=$(compgen -v | grep -E '^(DRIFT|HOST_UNITS)_[A-Z_]*$' | sort | tr '\n' ' ')
[ -z "$hooks" ] || note "test hooks set: ${hooks% }"
if [[ $- == *p* ]]; then                          # bash -p: run by its path
  # the functions of its environment, which bash -p neither takes nor removes: named from the environment it started
  # with (one entry a line: an entry's value may hold a line break)
  for f in $(sed -zn 's/^BASH_FUNC_\([^=]*\)%%=.*/\1/p' "/proc/$$/environ" 2>/dev/null | tr '\0\n\t ' '\n???'); do
    shell_env+=" $f()"
  done
  [ -z "${BASH_ENV:-}" ] || note "BASH_ENV is set: bash ran this check, and runs scripts/host-units.sh, under -p, which does not run that file"
  [ -z "$shell_env" ] || note "bash had options or functions in its environment (${shell_env# }): it ran this check, and runs scripts/host-units.sh, under -p, which does not take them"
else
  [ -z "${BASH_ENV:-}" ] || note "BASH_ENV is set: bash ran that file before this check, and what it set there can change what the check says"
  [ -z "$shell_env" ] || note "bash had options or functions from its environment or a BASH_ENV file (${shell_env# }): this check turned off the options that change what it says, and removed the functions, before it read anything"
fi
# its own python: the system's.  A venv's (an activated venv on PATH) is drift, and -S keeps its site, and so its
# .pth files, from running here (without site, python 3.10 does not take the venv for its prefix: its pyvenv.cfg
# beside it or one directory up tells)
python3 -I -S -c 'import os, sys; d = os.path.dirname(sys.executable)
sys.exit(any(os.path.exists(os.path.join(x, "pyvenv.cfg")) for x in (d, os.path.dirname(d))))' ||
  drift "python3 here is a venv's ($(command -v python3)): run this check with the system's python3"

echo "== installed files, and what systemd loaded of each unit"
# the repo's units, and each unit the repo has only drop-ins for (systemd/<unit>.d/*.conf: user-1003.slice)
units=(strata-q3.service strata-q3-health.service strata-q3-health.timer zfs-arc-max.service)
repo_units=" ${units[*]} "                        # the repo's own units: each has its file, also beside a drop-in
for dir in "$D"/systemd/*.d; do
  [ -d "$dir" ] || continue
  u=${dir##*/}; u=${u%.d}
  case " ${units[*]} " in *" $u "*) ;; *) units+=("$u") ;; esac
done
for u in "${units[@]}"; do
  file=-
  if [[ $repo_units == *" $u "* ]] || [ -e "$D/systemd/$u" ] || [ -L "$D/systemd/$u" ] || [ ! -d "$D/systemd/$u.d" ]; then
    file=$D/systemd/$u
    if [ ! -f "$file" ]; then                      # neither read nor compared: a FIFO would hang cmp
      if [ -e "$file" ] || [ -L "$file" ]; then drift "$file is not a regular file"; else drift "$file is missing"; fi
      continue
    fi
    same "$file" "$ROOT/etc/systemd/system/$u" 644
  fi
  dropins=()
  for f in "$D/systemd/$u.d"/*.conf; do
    if [ ! -f "$f" ]; then                         # not read: a FIFO would hang it (no match leaves the pattern)
      if [ -e "$f" ] || [ -L "$f" ]; then drift "$f is not a regular file"; fi
      continue
    fi
    same "$f" "$ROOT/etc/systemd/system/$u.d/${f##*/}" 644
    dropins+=("$f")
  done
  # every property systemctl show has of the settings of the repo's file and drop-ins; a list (Wants=, After=) may
  # hold more, which systemd adds itself
  python3 -I -S - "$u" "$file" "$ROOT" "${dropins[@]}" <<'EOF' || bad=1
import io, json, os, re, shlex, stat, subprocess, sys
unit, path, sysroot, dropins = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
class NotRegular(OSError):
    pass
def open_regular(p):
    """p opened to read, only when it is a regular file: stat before the open (a device is not opened), O_NONBLOCK (a
    FIFO put there meanwhile does not block the open) and fstat after it.  NotRegular otherwise."""
    if not stat.S_ISREG(os.stat(p).st_mode):
        raise NotRegular(0, "not a regular file", p)
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotRegular(0, "not a regular file", p)
    return open(fd, "rb")
SPAN = {"us": 1, "usec": 1, "ms": 1000, "msec": 1000, "s": 10**6, "sec": 10**6, "second": 10**6, "seconds": 10**6,
        "m": 60 * 10**6, "min": 60 * 10**6, "minute": 60 * 10**6, "minutes": 60 * 10**6, "h": 3600 * 10**6,
        "hr": 3600 * 10**6, "hour": 3600 * 10**6, "hours": 3600 * 10**6, "d": 86400 * 10**6, "day": 86400 * 10**6,
        "days": 86400 * 10**6, "w": 604800 * 10**6, "week": 604800 * 10**6, "weeks": 604800 * 10**6,
        "M": 2629800 * 10**6, "month": 2629800 * 10**6, "months": 2629800 * 10**6, "y": 31557600 * 10**6,
        "year": 31557600 * 10**6, "years": 31557600 * 10**6}
def span(text, unit="s"):           # a systemd time span, in microseconds; a bare number has the setting's unit
    text = text.strip()
    if text == "infinity":
        return "infinity"
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return round(float(text) * SPAN[unit])
    if not re.fullmatch(r"(\s*\d+(?:\.\d+)?\s*[a-zA-Z]+)+\s*", text):
        raise ValueError(f"not a time span: {text!r}")
    total = 0
    for num, u in re.findall(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", text):
        if u not in SPAN:
            raise ValueError(f"a time unit this check does not know: {text!r}")
        total += round(float(num) * SPAN[u])
    return total
def size(text):                     # a systemd size (MemoryMax=), in bytes: K, M, G, T, P and E are powers of 1024
    text = text.strip()
    if text == "infinity":
        return "infinity"
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?\s*([KMGTPEB]?)", text)
    if not m:
        raise ValueError(f"a size this check does not read: {text!r}")
    mult = 1024 ** "BKMGTPE".index(m.group(3) or "B")
    frac = m.group(2) or "0"
    return int(m.group(1)) * mult + int(frac) * mult // 10 ** len(frac)
EXACT = {"Description", "Type", "User", "Group", "WorkingDirectory", "LimitMEMLOCK", "LimitNOFILE", "OOMScoreAdjust",
         "NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp", "KillMode", "Restart",
         "RuntimeDirectory", "RuntimeDirectoryMode", "RuntimeDirectoryPreserve", "RemainAfterExit", "StartLimitBurst"}
# what systemctl show prints otherwise than a unit may write it: a boolean in any of systemd's spellings as yes or no
# (ProtectHome= and the like take a word of their own too), a mode as four octal digits, a limit as the hard one
# (LimitX) and the soft one (LimitXSoft), a soft:hard pair or one value for both, a size with K, M, G... in bytes
BOOLEAN = {"NoNewPrivileges", "PrivateTmp", "RemainAfterExit"}
BOOLISH = {"ProtectHome", "ProtectSystem", "RuntimeDirectoryPreserve"}
MODES = {"RuntimeDirectoryMode"}
LIMITS = {"LimitMEMLOCK": size, "LimitNOFILE": int}
def boolean(text):                  # parse_boolean(): 1/yes/y/true/t/on and 0/no/n/false/f/off, in any case
    t = text.strip().lower()
    return "yes" if t in ("1", "yes", "y", "true", "t", "on") else "no" if t in ("0", "no", "n", "false", "f", "off") else text
def limit(key, text):               # (soft, hard)
    parts = text.split(":")
    if len(parts) > 2:
        raise ValueError(f"a limit this check does not read: {text!r}")
    one = lambda p: "infinity" if p.strip() == "infinity" else LIMITS[key](p.strip())     # noqa: E731
    return one(parts[0]), one(parts[-1])
SUBSET = {"Wants", "After", "Before", "WantedBy"}
SPANS = {"TimeoutStartSec": "TimeoutStartUSec", "TimeoutStopSec": "TimeoutStopUSec", "RestartSec": "RestartUSec",
         "StartLimitIntervalSec": "StartLimitIntervalUSec", "AccuracySec": "AccuracyUSec"}
SIZES = {"MemoryMax": "memory.max", "MemorySwapMax": "memory.swap.max", "MemoryHigh": "memory.high",
         "MemoryLow": "memory.low", "MemoryMin": "memory.min"}          # the setting, and the cgroup file it writes
TIMERS = {"OnBootSec": "OnBootUSec", "OnUnitActiveSec": "OnUnitActiveUSec"}
EXEC = {"ExecStart", "ExecStartPre", "ExecStartPost"}
FILE_ONLY = {"ConditionPathExists"}            # systemctl show prints no value for it: the file comparison covers it
VENDOR = ("/usr/lib/systemd/system/", "/lib/systemd/system/")     # the system's own units and drop-ins
class Continued(ValueError):
    """A line that systemd continues on the next one: it ends with a backslash that no backslash escapes (systemd joins
    the two lines; this check does not, nor does scripts/test.sh, which refuses such a line)."""
class Nul(ValueError):
    """A line with a NUL byte, where systemd ends the line and reads what follows as the next one (this check does not)."""
WS = " \t\n\r"                                    # systemd's WHITESPACE: the only blanks it skips before a comment's
def settings(f):                                  # # or ; and strips from a line, a key and a value (not \f or \v)
    bom = False
    for n, raw in enumerate(io.TextIOWrapper(open_regular(f), encoding="utf-8"), 1):
        if not bom and raw.startswith("\ufeff"):    # a byte order mark: systemd skips the first one, at the start of
            raw, bom = raw[1:], True              # whichever line it is (another is part of its line)
        if "\0" in raw:
            raise Nul(n)
        if raw.lstrip(WS).startswith(("#", ";")):     # a comment, whatever its end
            continue
        end = raw.rstrip("\r\n")
        if (len(end) - len(end.rstrip("\\"))) % 2:    # a section's line too: systemd joins it
            raise Continued(n)
        line = raw.strip(WS)
        if line and not line.startswith("["):
            key, _, value = line.partition("=")
            yield key.strip(WS), value.strip(WS)
# a list that an empty assignment empties, as systemd does: the commands of an Exec key, Environment (not a
# dependency's, After=: systemd adds nothing for one, and empties none)
RESET = EXEC | {"Environment"}
# what the rest of this check reads of these units from their files alone (scripts/check-drift.sh above and below:
# the live config and the service's checkout of strata-q3.service's ExecStart= and ExecStartPre=, what runs of its
# ExecStart= and the settings of its process, the ZFS values of zfs-arc-max.service's ExecStart=): a drop-in of the
# repo that sets one of them would change what systemd runs, not what this check compares it with
FILE_READ = {"strata-q3.service": {"ExecStart", "ExecStartPre", "WorkingDirectory", "User", "Group", "LimitMEMLOCK",
                                   "LimitNOFILE", "OOMScoreAdjust", "NoNewPrivileges", "ProtectSystem", "PrivateTmp",
                                   "ProtectHome"},
             "zfs-arc-max.service": {"ExecStart"}}
want, elsewhere = {}, []
# the repo's file, then its drop-ins in the order of their names, as systemd reads them: the last value of a setting
# is the one that counts
for f in ([] if path == "-" else [path]) + sorted(dropins, key=os.path.basename):
    try:
        for key, value in settings(f):
            if f != path and key in FILE_READ.get(unit, ()):
                elsewhere.append(f"DRIFT  {unit}: {f} sets {key}=, which the rest of this check reads from "
                                 f"systemd/{unit} alone: set it there")
            if key in RESET and not value:
                want[key] = []
            else:
                want.setdefault(key, []).append(value)
    except Continued as e:
        print(f"DRIFT  {unit}: {f}: its line {e.args[0]} ends with a backslash, which goes on on the next line; this check "
              "does not join lines, so what systemd loaded of it is not compared")
        sys.exit(1)
    except Nul as e:
        print(f"DRIFT  {unit}: {f}: its line {e.args[0]} holds a NUL byte, where systemd ends a line; this check does not, "
              "so what systemd loaded of it is not compared")
        sys.exit(1)
    except (OSError, ValueError) as e:
        print(f"DRIFT  {unit}: {f} cannot be read ({'not a regular file' if isinstance(e, NotRegular) else e.__class__.__name__}): "
              "what systemd loaded of it is not compared")
        sys.exit(1)
props = ["FragmentPath", "LoadState", "DropInPaths", "NeedDaemonReload", "TimersMonotonic",
         "ActiveState", "ControlGroup"]
props += [k for k in want if k in EXACT | SUBSET | EXEC | set(SIZES)] + [SPANS[k] for k in want if k in SPANS]
props += [f"{k}Soft" for k in want if k in LIMITS]
try:
    out = subprocess.run(["systemctl", "show", unit, "-p", ",".join(dict.fromkeys(props))], capture_output=True,
                         text=True, timeout=30).stdout
except (OSError, subprocess.TimeoutExpired) as e:
    print(f"DRIFT  {unit}: systemctl show failed ({e.__class__.__name__})"); sys.exit(1)
have = {}
for l in out.splitlines():
    k, _, v = l.partition("=")
    have.setdefault(k, []).append(v)
bad, notes = [], []
one = lambda k: (have.get(k) or [""])[-1]
def same_value(key, a, b):
    if key in BOOLEAN | BOOLISH:
        return boolean(a) == boolean(b)
    if key in MODES:
        return int(a, 8) == int(b, 8)
    if key in LIMITS:
        return limit(key, a) == limit(key, b)
    if key in SIZES:
        return size(a) == size(b)
    if key in SPANS:
        return span(a) == span(b)
    return a == b
if one("LoadState") != "loaded":
    bad.append(f"LoadState is {one('LoadState') or 'unreadable'}")
frag = one("FragmentPath")
if path != "-" and frag != f"/etc/systemd/system/{unit}":
    bad.append(f"systemd loads it from {frag or 'nowhere'}, not /etc/systemd/system/{unit}")
elif path == "-" and frag and not frag.startswith(VENDOR):     # a unit the repo has only drop-ins for: the system's
    bad.append(f"systemd loads it from {frag}, a unit file the repo does not have")
if one("NeedDaemonReload") != "no":
    bad.append("its file or a drop-in changed on disk since the last daemon-reload")
# its drop-ins: the repo's, installed in /etc/systemd/system/<unit>.d, and no other .conf there; for a unit the repo
# has no file of, the system's own too; and a copy of the repo's values that systemctl set-property --runtime made
# in /run/systemd/system.control.  Nothing else
mine = {f"/etc/systemd/system/{unit}.d/{os.path.basename(f)}" for f in dropins}
here = f"{sysroot}/etc/systemd/system/{unit}.d"
try:
    others = sorted(n for n in os.listdir(here) if n.endswith(".conf") and not n.startswith(".")
                    and f"/etc/systemd/system/{unit}.d/{n}" not in mine)
except FileNotFoundError:
    others = []
except OSError as e:
    others = [f"(what it holds cannot be read: {e.strerror})"]
if others:
    bad.append(f"{here} has {', '.join(others)}, which the repo's systemd/{unit}.d does not have")
loaded = one("DropInPaths").split()
# every drop-in directory that systemd reads for the unit, in its order (systemd.unit(5)): in each directory of its
# search path, <unit>.d and then each dash prefix's (strata-q3-health.service: strata-q3-.service.d, strata-.service.d),
# and after those, in each directory, <type>.d.  Of the .conf files of one name, systemd reads the first in that order,
# which masks the others.  One that DropInPaths does not list came after the last daemon-reload: systemd reads only the
# directories that were there then (so it does not see a new one, and NeedDaemonReload stays "no").  A name that starts
# with "." (an editor's lock, .#90-x.conf) systemd skips, as it skips hidden and backup files.  Shown as systemd shows
# it: with the links of its directories resolved (/lib/systemd/system is /usr/lib/systemd/system where /usr is merged:
# a file found by both is masked by itself, which says nothing).  The order is systemd-analyze unit-paths's
SEARCH = ("/etc/systemd/system.control", "/run/systemd/system.control", "/run/systemd/transient",
          "/run/systemd/generator.early", "/etc/systemd/system", "/etc/systemd/system.attached", "/run/systemd/system",
          "/run/systemd/system.attached", "/run/systemd/generator", "/usr/local/lib/systemd/system",
          "/lib/systemd/system", "/usr/lib/systemd/system", "/run/systemd/generator.late")
def prefixes(name):
    """The dash prefixes of a unit's name without its type, as systemd takes them: foo-bar-baz gives foo-bar-, foo-."""
    out = []
    while True:
        i = name.rfind("-")
        if i > 0 and i == len(name) - 1:           # a trailing dash: taken off once, then the dash before it
            name = name[:-1]
            i = name.rfind("-")
        if i <= 0:
            return out
        name = name[:i + 1]
        out.append(name)
base, _, typ = unit.rpartition(".")
first, masked = {}, []                            # first: the .conf systemd reads of each name
top = os.path.realpath(sysroot) if sysroot else ""
for d in [f"{s}/{n}.d" for s in SEARCH for n in [unit] + [f"{p}.{typ}" for p in prefixes(base)]] + \
        [f"{s}/{typ}.d" for s in SEARCH]:
    real = os.path.realpath(sysroot + d)
    try:
        names = sorted(n for n in os.listdir(real) if n.endswith(".conf") and not n.startswith("."))
    except (FileNotFoundError, NotADirectoryError):
        continue
    except OSError as e:
        bad.append(f"{d} cannot be read ({e.strerror}): what systemd reads from it is not known")
        continue
    shown = real[len(top):] if top and real.startswith(top + "/") else real
    for n in names:
        if n in first:
            masked.append((f"{shown}/{n}", first[n]))
        else:
            first[n] = f"{shown}/{n}"
masks = {p: by for p, by in masked if p in mine}
for p in sorted(mine - set(loaded)):
    if p in masks:
        bad.append(f"{p} is installed, but {masks[p]} masks it (systemd reads that one, of its name, instead)")
    elif os.path.lexists(sysroot + p):             # one that is not there is said above
        bad.append(f"{p} is installed, but systemd has not loaded it (daemon-reload)")
for p in sorted(set(first.values()) - set(loaded)):
    if os.path.dirname(p) != f"/etc/systemd/system/{unit}.d":       # the repo's directory: said above
        bad.append(f"{p} is not loaded: run daemon-reload")
def runtime_copy(p):
    """Why the drop-in p is not a copy of the repo's values, or None.  systemctl set-property writes one setting in
    each; one that sets nothing (empty, or a link to /dev/null) masks a drop-in of its name later in systemd's order"""
    if os.path.realpath(sysroot + p) == "/dev/null":
        return "is a link to /dev/null: it masks a drop-in of its name"
    try:
        got = list(settings(sysroot + p))
    except NotRegular:
        return "is not a regular file"
    except Continued as e:
        return f"has a line ({e.args[0]}) that ends with a backslash, which this check does not join with the next one"
    except Nul as e:
        return f"has a line ({e.args[0]}) with a NUL byte, where systemd ends a line and this check does not"
    except (OSError, ValueError) as e:
        return f"cannot be read ({e.__class__.__name__})"
    if not got:
        return "sets nothing: it masks a drop-in of its name"
    for key, value in got:
        try:
            if not want.get(key) or not same_value(key, value, want[key][-1]):
                return f"sets {key} otherwise than the repo's files"
        except ValueError:
            return f"sets {key} to a value this check does not read"
    return None
odd = []
for p in loaded:
    if p in mine or (path == "-" and p.startswith(VENDOR)):
        continue
    if p.startswith(f"/run/systemd/system.control/{unit}.d/"):
        why_not = runtime_copy(p)
        if why_not is None:
            notes.append(f"{unit}: {p} (systemctl set-property --runtime) holds the repo's values")
        else:
            bad.append(f"{p} (systemctl set-property --runtime) {why_not}")
        continue
    odd.append(p)
if odd:
    bad.append(f"drop-ins override it: {' '.join(odd)}")
def env_of(words):
    return dict(w.split("=", 1) for w in words if "=" in w)
def bus_environment(unit):
    """The loaded Environment= of unit, each item as systemd holds it, read from its bus property with busctl (read
    only, as any user).  systemctl show prints an item with a blank in it as [unprintable], and one with a quote as it
    is, which no split reads back"""
    path = "/org/freedesktop/systemd1/unit/" + "".join(        # sd_bus_path_encode(): _xx for all but a letter or
        c if c.isascii() and c.isalnum() and (i or not c.isdigit()) else f"_{ord(c):02x}"    # a digit (not first)
        for i, c in enumerate(unit))
    iface = "org.freedesktop.systemd1." + unit.rpartition(".")[2].capitalize()
    r = subprocess.run(["busctl", "--json=short", "get-property", "org.freedesktop.systemd1", path, iface, "Environment"],
                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    got = json.loads(r.stdout) if r.returncode == 0 else None
    if not (isinstance(got, dict) and got.get("type") == "as" and isinstance(got.get("data"), list)
            and all(isinstance(x, str) for x in got["data"])):
        raise OSError("busctl get-property failed, or printed no list of strings")
    return got["data"]
for key, values in want.items():
    try:
        if key in LIMITS:
            loaded = f"{one(key + 'Soft')}:{one(key)}"
            if not same_value(key, loaded, values[-1]):
                bad.append(f"{key} is {loaded!r} (soft:hard), the repo says {values[-1]!r}")
        elif key in EXACT:
            if not same_value(key, one(key), values[-1]):
                bad.append(f"{key} is {one(key)!r}, the repo says {values[-1]!r}")
        elif key in SUBSET:
            missing = set(" ".join(values).split()) - set(one(key).split())
            if missing:
                bad.append(f"{key} lacks {' '.join(sorted(missing))}")
        elif key == "Environment":      # the names only: a value can be a secret
            try:
                w = env_of(kv for v in values for kv in shlex.split(v))
            except ValueError as e:     # an unbalanced quote: systemd ignores the line
                bad.append(f"Environment: a line of the repo's that this check cannot split into words ({e}): "
                           "not compared")
                continue
            try:
                h = env_of(bus_environment(unit))
            except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                bad.append(f"Environment: the loaded one cannot be read ({e.__class__.__name__}: busctl): not compared")
                continue
            differ = sorted(k for k in set(w) | set(h) if w.get(k) != h.get(k))
            if differ:
                bad.append(f"Environment differs from the repo's in {differ}")
        elif key in SPANS or key in SIZES:
            if not same_value(key, values[-1], one(SPANS.get(key, key))):
                bad.append(f"{key} is {one(SPANS.get(key, key))!r}, the repo says {values[-1]!r}")
        elif key in TIMERS:
            got = [m.group(1) for t in have.get("TimersMonotonic", []) for m in [re.search(rf"\b{TIMERS[key]}=([^;]+);", t)] if m]
            if [span(values[-1])] != [span(g) for g in got]:
                bad.append(f"{key}: the loaded timer has {got or 'none'}, the repo says {values[-1]!r}")
        elif key in EXEC:      # one line per command; systemctl show joins argv with blanks, unquoted
            got = [(m.group(1), m.group(2), m.group(3)) for line in have.get(key, [])
                   for m in [re.search(r"path=(.*?) ; argv\[\]=(.*?) ; ignore_errors=(\w+)", line)] if m]
            exp = []
            for v in values:            # the program, its argv, and whether a failure is ignored (-); with @, the
                prefix = re.match(r"[-@:+!]*", v).group(0)               # second word is argv[0]
                words = shlex.split(v[len(prefix):])
                exp.append((words[0] if words else "", " ".join(words[1:] if "@" in prefix else words),
                            "yes" if "-" in prefix else "no"))
            same = lambda g, e: g[1:] == e[1:] and (g[0] == e[0] or not e[0].startswith("/"))  # noqa: E731
            if len(got) != len(exp) or not all(map(same, got, exp)):     # a bare name: systemd finds its path
                i = next((i for i, (g, e) in enumerate(zip(got, exp)) if not same(g, e)), min(len(got), len(exp)))
                bad.append(f"{key}: the loaded commands are not the repo's ({len(got)} loaded, {len(exp)} in the "
                           f"repo; command {i + 1} differs)")
        elif key not in FILE_ONLY:
            bad.append(f"{key}= is in the repo's unit, and this check does not compare it yet")
    except ValueError as e:
        bad.append(f"{key}: {e}")
# the memory limits in force: what the kernel holds in the unit's cgroup, which it has while it runs
limits = [k for k in want if k in SIZES]
cgroup = one("ControlGroup") if one("ActiveState") in ("active", "activating", "reloading", "deactivating") else None
if limits and cgroup is not None and not cgroup.startswith("/"):
    bad.append(f"its control group is not known: whether its {', '.join(limits)} are in force cannot be read")
elif limits and cgroup is not None:
    page = os.sysconf("SC_PAGE_SIZE")
    for key in limits:
        f = f"{sysroot}/sys/fs/cgroup{cgroup}/{SIZES[key]}"
        try:
            with open_regular(f) as fh:
                got = fh.read().decode().strip()
            w = size(want[key][-1])
        except (OSError, ValueError) as e:
            bad.append(f"{key}: {f} " + ("is not a regular file" if isinstance(e, NotRegular) else
                                         f"cannot be read ({e.__class__.__name__})") + ": whether it is in force is not known")
            continue
        if got != ("max" if w == "infinity" else str(w // page * page)):        # the kernel keeps whole pages
            bad.append(f"{key}: its cgroup's {SIZES[key]} is {got}, the repo says {want[key][-1]}")
for b in elsewhere:
    print(b)
for b in bad:
    print(f"DRIFT  {unit} (loaded): {b}")
for n in notes:
    print(f"note   {n}")
if not bad and not elsewhere:
    what = "file" if not dropins else "drop-ins" if path == "-" else "file and drop-ins"
    force = f"; its cgroup holds its {', '.join(limits)}" if limits and cgroup is not None else ""
    print(f"ok     {unit} (loaded: as the repo's {what}, {len(want)} settings{force})")
sys.exit(1 if bad or elsewhere else 0)
EOF
done
same "$D/systemd/strata-q3.logrotate" "$ROOT/etc/logrotate.d/strata-q3" 644
same "$D/scripts/strata-health.sh" "$ROOT/usr/local/sbin/strata-q3-health" 755     # the timer runs it as root
# the values that the unit file's ExecStart lines write (a drop-in of the repo that sets ExecStart= is drift above),
# read as systemd reads them: a comment (# or ; first, but for blanks) writes nothing, and blanks may stand around the
# key.  A line that goes on on the next one is drift above, and none is read here then: this check does not join
# them.  A byte order mark before the key hides the line from it (systemd skips the first one): no value read, which
# is drift.  Two values for one parameter are drift: which one counts is not known
continued=$(grep -vE '^[[:blank:]]*[#;]' "$D/systemd/zfs-arc-max.service" 2>/dev/null | grep -cE '(^|[^\\])(\\\\)*\\'$'\r''?$')
if [ "${continued:-0}" != 0 ]; then
  drift "the ZFS ARC values are not compared: systemd/zfs-arc-max.service has a line that goes on on the next one (above)"
else
  for param in zfs_arc_max zfs_arc_shrinker_limit; do
    want=$(grep -E '^[[:blank:]]*ExecStart[[:blank:]]*=' "$D/systemd/zfs-arc-max.service" 2>/dev/null |
           grep -o "echo [0-9]* > /sys/module/zfs/parameters/$param\b" | cut -d' ' -f2)
    f=$ROOT/sys/module/zfs/parameters/$param       # sysfs holds regular files only: cat would block on a FIFO
    have=$([ -f "$f" ] && cat "$f" 2>/dev/null)
    if [ -e "$f" ] && [ ! -f "$f" ]; then drift "$f is not a regular file"
    elif [ "$(grep -c . <<< "$want")" -gt 1 ]; then
      drift "zfs-arc-max.service writes $param more than once ($(paste -sd, - <<< "$want")): which value counts is not known"
    elif [ -z "$want" ]; then drift "systemd/zfs-arc-max.service has no ExecStart line that writes $param (echo N > ...): it is not compared"
    elif [ "$have" = "$want" ]; then ok "$param = $have"
    else drift "$param is ${have:-unreadable}, zfs-arc-max.service writes $want (did it run at boot?)"; fi
  done
fi

echo "== host units (config/host-units.conf)"
# its own ok/DRIFT lines; a unit it cannot read counts as drift, and so does a failure that it says no DRIFT line of
if [ ! -f "$D/scripts/host-units.sh" ]; then
  drift "$D/scripts/host-units.sh is missing, or not a regular file: the host units are not checked"
else
  hu=$(/bin/bash -p "$D/scripts/host-units.sh" check); hu_rc=$?     # -p: no function or option of the environment
  [ -z "$hu" ] || printf '%s\n' "$hu"
  if [ "$hu_rc" != 0 ]; then
    bad=1
    grep -q -E '^(DRIFT|BAD) ' <<< "$hu" || drift "scripts/host-units.sh check failed (exit $hu_rc) and said no DRIFT line"
  fi
fi

echo "== Strata config"
PROFILE_ARGS=$(sed -e 's/#.*//' "$D/config/profile.conf" 2>/dev/null | tr -d '\r' | xargs)     # parsed as strata-preflight.sh does
[ -n "$PROFILE_ARGS" ] || drift "config/profile.conf has no arguments (strata-preflight.sh refuses to start)"
# the live config, read only when it is a regular file (stat, O_NONBLOCK, fstat: a FIFO there would hang the check).  An
# api_key lives in the live config only, never in the repo (deployment record, section 5): with one, the two compare
# as equal JSON text without it, not as equal Python values (4.0 == 4 and 1 == True in Python; the server tells them
# apart); without one, byte for byte
cfg_is=$(python3 -I -S - "$live" "$D/config/strata-iq3_xxs.deployed.json" <<'EOF'
import json, os, stat, sys
class NotRegular(Exception):
    pass
def read(p):
    if not stat.S_ISREG(os.stat(p).st_mode):
        raise NotRegular
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    with open(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegular
        return f.read()
data, wrong = {}, []
for name, p in (("live", sys.argv[1]), ("snapshot", sys.argv[2])):
    try:
        data[name] = read(p)
    except NotRegular:
        wrong.append(f"{name}:not-regular")
    except FileNotFoundError:
        wrong.append(f"{name}:missing")
    except OSError:
        wrong.append(f"{name}:unreadable")
try:                                              # its key: the list's sha256 of it cannot hold then
    cfg = json.loads(data["live"].decode("utf-8-sig")) if "live" in data else None
    key = isinstance(cfg, dict) and cfg.get("api_key") not in (None, "")
except ValueError:
    key = False
if wrong:                                          # each of the two said, and nothing compared
    print(" ".join(wrong + (["key"] if key else [])))
    sys.exit()
live, snap = data["live"], data["snapshot"]
if key:
    cfg.pop("api_key")
    c = lambda x: json.dumps(x, sort_keys=True)       # noqa: E731
    try:
        print("key-same" if c(cfg) == c(json.loads(snap)) else "key-differs")
    except ValueError:
        print("key-differs")
else:
    print("same" if live == snap else "differs")
EOF
)
has_key=0
for w in ${cfg_is:-failed}; do                    # one word, or one for each of the two files that went wrong
  case $w in
    key) has_key=1 ;;                             # beside a file that went wrong
    key-same) has_key=1; ok "$live (api_key set: compared without it)" ;;
    key-differs) has_key=1; drift "$live differs from config/strata-iq3_xxs.deployed.json (api_key ignored)" ;;
    same) ok "$live" ;;
    live:not-regular) drift "$live is not a regular file" ;;
    live:missing) drift "$live is missing" ;;
    live:unreadable) drift "$live cannot be read" ;;
    snapshot:missing) drift "config/strata-iq3_xxs.deployed.json is missing: the live config is not compared with it" ;;
    snapshot:*) drift "config/strata-iq3_xxs.deployed.json cannot be read: the live config is not compared with it" ;;
    *) drift "$live differs from config/strata-iq3_xxs.deployed.json" ;;
  esac
done
snapshot=$D/config/strata-iq3_xxs.deployed.json
# shellcheck disable=SC2086  # PROFILE_ARGS holds the tool's arguments, one word each
if [ ! -f "$snapshot" ] || [ ! -r "$snapshot" ]; then
  :                                               # said above: there is nothing to compare with
elif ! cp "$D/config/strata-iq3_xxs.setup.json" "$tmp/strata-iq3_xxs.json" 2>/dev/null; then
  drift "config/strata-iq3_xxs.setup.json cannot be read: what configure_strata_q3.py makes of it is not compared"
elif ! python3 -I -S "$D/tools/configure_strata_q3.py" "$tmp/strata-iq3_xxs.json" $PROFILE_ARGS > /dev/null; then
  drift "configure_strata_q3.py with config/profile.conf no longer makes the deployed snapshot from setup's config"
else
  cmp -s "$tmp/strata-iq3_xxs.json" "$snapshot"
  case $? in                                      # as same(): 1, they differ; 2, one could not be read after all
    0) ok "setup's config + configure_strata_q3.py $PROFILE_ARGS = the deployed snapshot" ;;
    1) drift "configure_strata_q3.py with config/profile.conf no longer makes the deployed snapshot from setup's config" ;;
    *) drift "config/strata-iq3_xxs.deployed.json cannot be read: what configure_strata_q3.py makes is not compared with it" ;;
  esac
fi

echo "== deployed files (sha256, config/deployed-artifacts.sha256)"
entries=0
live_rel=${live#"$DEV"/}
list=$D/config/deployed-artifacts.sha256
[ -e "$list" ] || { drift "config/deployed-artifacts.sha256 is missing"; list=/dev/null; }
while IFS= read -r line || [ -n "$line" ]; do                 # also a last line without a newline
  case "$line" in ''|'#'*) continue ;; esac
  if ! [[ "$line" =~ ^([0-9a-f]{64})\ \ ([^[:space:]]+)$ ]]; then drift "config/deployed-artifacts.sha256: not a '<sha256>  <path>' line: $line"; continue; fi
  sum=${BASH_REMATCH[1]}; path=${BASH_REMATCH[2]}; entries=$((entries + 1))
  case "$path" in /*) file=$path ;; *) file=$DEV/$path ;; esac
  [ "$path" = "$live_rel" ] && [ "$has_key" = 1 ] && { echo "skip   $path (api_key set: checked above)"; continue; }
  if [ ! -e "$file" ]; then drift "$file is missing"; continue; fi
  size=$(stat -L -c %s "$file" 2>/dev/null || echo 0)
  if [ $FULL = 0 ] && [ "$size" -gt 1073741824 ]; then
    echo "skip   $path (--full: $((size >> 30)) GiB)"; unhashed "$file"; continue
  fi
  got=$(sha256_regular "$file")
  if [ "$got" = not-regular ]; then drift "$file is not a regular file"
  elif [ "$got" = "$sum" ]; then ok "$path"; verified "$file"
  else drift "$path (sha256 changed, or unreadable)"; fi
done < "$list"
[ "$entries" -gt 0 ] || drift "config/deployed-artifacts.sha256 lists no files"

echo "== the Strata tree, its venv and its shared settings; the service's checkout (${LIVE_REPO:-unknown})"
pin=$(sed -n 's/^# strata source pin: \([0-9a-f]\{40\}\).*/\1/p' "$D/config/deployed-artifacts.sha256" 2>/dev/null | head -1)
python3 -I -S - "$STRATA_DIR" "$pin" "$D/config/deployed-artifacts.sha256" "$DEV" "$D/config/strata-venv.txt" "$live" \
  "${LIVE_REPO:-}" "$D" "$CANDIDATE" "$D/systemd/$UNIT" "$ROOT" "$tmp" "$GIT_TIMEOUT" <<'EOF' || bad=1
import base64, collections, csv, glob, hashlib, importlib.machinery, importlib.util, io, itertools, marshal, os, pwd, \
    re, stat, subprocess, sys
strata, pin, manifest, dev, venv_list, live, live_repo, here, candidate, unit_file, sysroot, tmp, git_limit = sys.argv[1:14]
bad, oks, notes = [], [], []
SUFFIXES = tuple(importlib.machinery.all_suffixes())     # .py, .pyc and the extension modules' suffixes
TAG = sys.implementation.cache_tag
MAGIC = importlib.util.MAGIC_NUMBER
class NotRegular(OSError):
    pass
def open_regular(p, follow=True):
    """p opened to read, only when it is a regular file: stat before the open (a device is not opened), O_NONBLOCK (a
    FIFO put there meanwhile does not block the open) and fstat after it.  NotRegular otherwise."""
    if not stat.S_ISREG((os.stat if follow else os.lstat)(p).st_mode):
        raise NotRegular(0, "not a regular file", p)
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotRegular(0, "not a regular file", p)
    return open(fd, "rb")
def unreadable(p, e):
    """What an OSError of reading p says, for a DRIFT line."""
    return f"{p} is not a regular file" if isinstance(e, NotRegular) else f"{p} cannot be read ({e.strerror})"
def importable(name, package):
    """Whether an import statement can reach a package (a directory) or a module (a file) of that name: an identifier,
    with a module's suffix for a module (.pytest_cache/ and foo.bar.py are neither)."""
    if package:
        return name.isidentifier()
    return any(name.endswith(s) and name[:-len(s)].isidentifier() for s in SUFFIXES)
def repo_text(p, said=False):
    """A file of the repo's (the checked tree's), or "" when it cannot be read: drift (but for one missing that the
    check said is missing above, when said)."""
    try:
        with io.TextIOWrapper(open_regular(p), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        if not said:
            bad.append(f"{p} is missing")
    except (OSError, ValueError) as e:
        bad.append(unreadable(p, e) if isinstance(e, OSError) else f"{p} cannot be read ({e.__class__.__name__})")
    return ""
listed = {m.group(1) for m in re.finditer(r"^[0-9a-f]{64}  (\S+)$", repo_text(manifest, said=True), re.M)}
listed_real = {os.path.realpath(p if p.startswith("/") else os.path.join(dev, p)) for p in listed}

GIT_LIMIT = float(git_limit)                     # DRIFT_GIT_TIMEOUT, a test hook, or 300: checked above
timed_out = []
def git(repo, *args, data=None, env=None):
    """git's result; one that did not end within GIT_LIMIT s is a failure, said below too (git reads its own files,
    .gitignore and .git/info/exclude among them, as they are: a FIFO there blocks it)."""
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=GIT_LIMIT, input=data,
                              env=None if env is None else {**os.environ, **env})
    except subprocess.TimeoutExpired:
        timed_out.append(f"git {args[0]} in {repo}")
        return subprocess.CompletedProcess(args, -1, b"", b"")
def top_is(repo):
    r = git(repo, "rev-parse", "--show-toplevel")
    return r.returncode == 0 and os.path.realpath(r.stdout.decode().strip()) == os.path.realpath(repo)
def head_of(repo):
    r = git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    return r.stdout.decode().strip() if r.returncode == 0 else ""
def tree_of(repo, commit):
    """{path: (mode, type, sha)} of the commit's files, or None."""
    r = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    if r.returncode:
        return None
    out = {}
    for rec in r.stdout.split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, typ, sha = meta.decode().split()
            out[os.fsdecode(path)] = (mode, typ, sha)
    return out
CONVERTS = ("filter", "ident", "working-tree-encoding")
INDEXES = itertools.count()
def attributes(repo, commit, paths):
    """{path: {attribute: value}} of the git attributes that change how git stores a file (unset ones left out), as
    the commit's .gitattributes files give them: read from an index of the commit made here, not from the working
    tree, which can hold others (the user's and the system's attributes files are off, above)."""
    index = {"GIT_INDEX_FILE": os.path.join(tmp, f"commit-{next(INDEXES)}.index")}
    if git(repo, "read-tree", commit, env=index).returncode:
        raise OSError("git read-tree of its commit failed")
    r = git(repo, "check-attr", "--cached", "-z", "--stdin", "text", "eol", *CONVERTS, env=index,
            data=b"".join(os.fsencode(p) + b"\0" for p in paths))
    if r.returncode:
        raise OSError("git check-attr failed")
    f, out = r.stdout.split(b"\0"), {}
    for i in range(0, len(f) - 2, 3):
        if f[i + 2] not in (b"unspecified", b"unset"):
            try:
                value = f[i + 2].decode()
            except UnicodeDecodeError:                 # a value that is not UTF-8: compare() says so
                value = None
            out.setdefault(os.fsdecode(f[i]), {})[f[i + 1].decode()] = value
    return out
def blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
def parents(p):
    """The directories above the path p, to its top."""
    while os.path.dirname(p) not in ("", p):
        p = os.path.dirname(p)
        yield p
def info_attributes(repo):
    """The repository's info/attributes when it sets an attribute (it wins over the commit's .gitattributes), else
    None."""
    r = git(repo, "rev-parse", "--git-path", "info/attributes")
    if r.returncode:
        raise OSError("git rev-parse --git-path failed")
    p = os.path.join(repo, os.fsdecode(r.stdout.rstrip(b"\n")))
    try:
        with io.TextIOWrapper(open_regular(p), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise OSError(unreadable(p, e))
    return p if any(l.strip() and not l.lstrip().startswith("#") for l in lines) else None
def compare(repo, tree, commit):
    """The commit's files as they are in repo, hashed here as git hashes a file it stores (a text file's CRLF line
    ends as LF, as the commit's attributes say): the differences."""
    info = info_attributes(repo)                 # first: git check-attr reads it too, and would block on a FIFO
    attrs = attributes(repo, commit, [p for p, (_, typ, _) in tree.items() if typ == "blob"])
    diffs = [f"{info} sets git attributes from outside the commit, which this check does not follow"] if info else []
    for path, (mode, typ, sha) in sorted(tree.items()):
        full = os.path.join(repo, path)
        a = attrs.get(path, {})
        try:
            st = os.lstat(full)
            if typ != "blob":
                diffs.append(f"{path} is a {typ}, which this check does not compare")
            elif None in a.values():
                diffs.append(f"{path} has a git attribute whose value is not UTF-8 "
                             f"({', '.join(k for k, v in a.items() if v is None)}), which this check does not follow")
            elif any(k in a for k in CONVERTS):
                diffs.append(f"{path} has git attributes this check does not follow ({', '.join(k for k in CONVERTS if k in a)})")
            elif (mode == "120000") != stat.S_ISLNK(st.st_mode):
                diffs.append(f"{path} is {'a link' if stat.S_ISLNK(st.st_mode) else 'not a link'} here")
            elif mode == "120000":
                if blob(os.fsencode(os.readlink(full))) != sha:
                    diffs.append(f"{path} links elsewhere")
            elif not stat.S_ISREG(st.st_mode):
                diffs.append(f"{path} is not a regular file")
            else:
                if bool(st.st_mode & 0o100) != (mode == "100755"):
                    diffs.append(f"{path}: its executable bit differs")
                with open_regular(full, follow=False) as f:
                    data = f.read()
                text = a.get("text") not in (None, "unset") or ("eol" in a and a.get("text") != "unset")
                if blob(data) != sha and not (text and blob(data.replace(b"\r\n", b"\n")) == sha):
                    diffs.append(f"{path} differs")
        except FileNotFoundError:
            diffs.append(f"{path} is missing")
        except OSError as e:
            diffs.append(unreadable(path, e))
    return diffs
def others(repo, tree):
    """The files under repo that its commit does not have: (not ignored, ignored), as git check-ignore says."""
    found = []
    for top, dirs, files in os.walk(repo, onerror=lambda e: found.append(None)):
        dirs[:] = [d for d in dirs if not (top == repo and d == ".git")]
        for name in files + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
            rel = os.path.relpath(os.path.join(top, name), repo)
            if rel not in tree and rel != ".git":         # a worktree's .git is a file
                found.append(rel)
    if None in found:
        raise OSError(f"a directory under {repo} cannot be read")
    if not found:
        return [], []
    r = git(repo, "check-ignore", "-z", "--stdin", data=b"".join(os.fsencode(p) + b"\0" for p in found))
    if r.returncode not in (0, 1):
        raise OSError("git check-ignore failed")
    ignored = {os.fsdecode(p) for p in r.stdout.split(b"\0") if p}
    return [p for p in found if p not in ignored], sorted(ignored)
def root_only(path):
    """Only root can change path: it and each directory above it (links resolved) are root's, and no group or other
    may write to them."""
    p = os.path.realpath(path)
    while True:
        try:
            st = os.stat(p)
        except OSError:
            return False
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False
        if p == "/":
            return True
        p = os.path.dirname(p)
def pyc_wrong(src, base):
    """Why the cached bytecode of src is wrong when Python would run it instead of src (it does when the cache's
    header matches src: its time and size, or its hash), else None; its path relative to base."""
    d, name = os.path.split(src)
    pyc = os.path.join(d, "__pycache__", f"{name[:-3]}.{TAG}.pyc")
    try:
        with open_regular(pyc) as f:
            data = f.read()
        with open_regular(src) as f:
            source = f.read()
            st = os.fstat(f.fileno())
    except FileNotFoundError:
        return None
    except OSError as e:
        return unreadable(os.path.relpath(e.filename or pyc, base), e)
    flags = int.from_bytes(data[4:8], "little")
    if len(data) < 16 or data[:4] != MAGIC or flags & ~0b11:
        return None                                    # Python compiles the source instead
    if flags & 0b1:                                    # by hash: checked against the source, or not at all
        if flags & 0b10 and data[8:16] != importlib.util.source_hash(source):
            return None
    elif (int.from_bytes(data[8:12], "little"), int.from_bytes(data[12:16], "little")) != \
            (int(st.st_mtime) & 0xFFFFFFFF, st.st_size & 0xFFFFFFFF):
        return None
    try:
        if marshal.loads(data[16:]) == compile(source, src, "exec", dont_inherit=True, optimize=0):
            return None
    except Exception as e:                             # bad marshal data, or a source that does not compile
        return f"{os.path.relpath(pyc, base)} cannot be compared ({e.__class__.__name__}), and Python would run it"
    return f"{os.path.relpath(pyc, base)} is not what its source compiles to, and Python would run it instead"

# --- the Strata tree
# what the service read at its start, for the check of what runs (below): the files, and the directories it imports
# from, whose times tell an entry added, removed or renamed after its start
started, watched, complete = [], [], True
venv_python = os.path.join(strata, ".venv", "bin", "python")
same_python = os.path.realpath(sys.executable) == os.path.realpath(venv_python)
if not same_python:
    bad.append(f"this check's python ({os.path.realpath(sys.executable)}) is not the service's "
               f"({os.path.realpath(venv_python)}): the cached bytecode can be checked only with that one")
tree = None
if not top_is(strata):
    bad.append(f"{strata} is not the top of a git checkout of its own")
else:
    head = head_of(strata)
    if not pin:
        bad.append("config/deployed-artifacts.sha256 has no '# strata source pin: <commit>' line")
    elif head != pin:
        bad.append(f"{strata} is at {head or 'an unreadable commit'}, the list pins {pin}")
    else:
        oks.append(f"{strata} is at {pin}")
    tree = tree_of(strata, pin or "HEAD")
    if tree is None:
        bad.append(f"{strata}: git ls-tree of the pinned commit failed")
if tree is None:
    complete = False
else:
    started += [os.path.join(strata, p) for p in sorted(tree)
                if p.startswith(("serve/", "tools/")) or ("/" not in p and p.endswith(".py"))]
    try:
        diffs = compare(strata, tree, pin or "HEAD")
    except OSError as e:
        diffs = [str(e)]
    if diffs:
        bad.append(f"{strata} differs from the pinned commit in {len(diffs)} files: " + "; ".join(diffs[:5]))
    else:
        oks.append(f"{strata}: its {len(tree)} files are the pinned commit's")
    # server.py puts the tree's root and tools/ first on its import path, serve/ is its own directory, and the
    # preflight's python3 -c runs in the root: there and in the packages under them, a module the commit does not
    # have shadows one (json.pyc, json.cpython-310-x86_64-linux-gnu.so, a package), ignored or not, and a directory
    # the commit has no file in is a namespace package (jsonschema/validators.py, without __init__.py, is what
    # serve/structured.py imports when jsonschema is not installed), unless it holds a listed file (engine-cuda12/)
    # or is the venv, compared below; and Python runs a module's cached bytecode instead of its source when the
    # cache's header matches the source.  A name that is no identifier (.pytest_cache/, foo.bar.py) no import reaches
    dirs = {"", "tools", "serve"} | {os.path.dirname(p) for p in tree if os.path.basename(p) == "__init__.py"}
    tree_dirs = {d for p in tree for d in parents(p)}
    listed_dirs = {d for p in listed_real for d in parents(p)}
    odd, caches = [], []
    for sub in sorted(dirs):
        base = os.path.join(strata, sub)
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as e:
            bad.append(f"{base} cannot be read ({e.strerror})")
            continue
        watched += [os.path.normpath(base)] + [e.path for e in entries if e.name == "__pycache__" and e.is_dir()]
        for e in entries:
            rel = os.path.join(sub, e.name)
            try:
                if e.is_dir() and e.name != "__pycache__":
                    if not importable(e.name, package=True):
                        continue
                    odd += [f"{rel}/{n}" for n in sorted(os.listdir(e.path)) if n.startswith("__init__.")
                            and n.endswith(SUFFIXES) and f"{rel}/{n}" not in tree
                            and os.path.realpath(os.path.join(e.path, n)) not in listed_real]
                    if (rel not in tree and rel not in tree_dirs and rel not in dirs
                            and os.path.realpath(e.path) not in listed_dirs
                            and not (sub == "" and e.name in (".git", ".venv"))):
                        odd.append(f"{rel}/ (a namespace package)")
                elif importable(e.name, package=False) and rel not in tree and \
                        os.path.realpath(e.path) not in listed_real:
                    odd.append(rel)
            except OSError:
                odd.append(f"{rel} (cannot be read)")
    for p in sorted(p for p in tree if p.endswith(".py") and os.path.dirname(p) in dirs):
        w = pyc_wrong(os.path.join(strata, p), strata) if same_python else None
        if w:
            caches.append(w)
    if odd:
        bad.append(f"{strata} has modules where server.py imports from that the pinned commit does not have and the "
                   f"list does not name: {', '.join(odd[:8])}")
    if caches:
        bad.append(f"{strata}: " + "; ".join(caches[:4]))
    if not odd and not caches:
        oks.append(f"{strata}: where server.py imports from, the commit's modules and the list's only, and their "
                   "cached bytecode is what they compile to")

# --- its venv: its python, the packages, and every file of it: one that python -m venv makes, one a package's RECORD
# gives (as it gives it, by a sha256 or stronger hash), or one the list names; and the bytecode of the packages.  Each
# RECORD is pinned by its sha256 in config/strata-venv.txt, so that a file changed together with its row, a row
# without a hash, or another dist-info directory shows.  A file elsewhere in the venv runs too (a .pth in
# lib/python3.10/dist-packages, which Debian's python reads, or a bin/pyvenv.cfg, which wins over the venv's own).
# Nothing of the venv runs here
venv = os.path.join(strata, ".venv")
if not root_only(venv_python):
    bad.append(f"{venv_python} is not the system's python: it resolves to {os.path.realpath(venv_python)}, which others "
               "than root can change")
sites = glob.glob(os.path.join(strata, ".venv", "lib", "python3*", "site-packages"))
if len(sites) != 1:
    bad.append(f"{strata}/.venv has {len(sites)} site-packages directories, not one")
    complete = False
else:
    site = sites[0]
    pins, recorded, wrong = [], {}, []
    for info in sorted(glob.glob(os.path.join(site, "*.dist-info"))):
        own = os.path.normpath(os.path.join(info, "RECORD"))
        try:
            with io.TextIOWrapper(open_regular(os.path.join(info, "METADATA")), encoding="utf-8") as f:
                head = f.read().split("\n\n", 1)[0]
            meta = dict(l.split(": ", 1) for l in head.splitlines() if ": " in l)
            with open_regular(own) as f:
                raw = f.read()
            rows = [r for r in csv.reader(io.StringIO(raw.decode("utf-8"), newline="")) if r]
            pins.append((meta["Name"], f"{meta['Name']}=={meta['Version']}  {hashlib.sha256(raw).hexdigest()}"))
        except (OSError, KeyError, ValueError, csv.Error) as e:
            wrong.append(unreadable(os.path.relpath(e.filename, venv), e) if isinstance(e, NotRegular)
                         else f"{os.path.basename(info)} cannot be read ({e.__class__.__name__})")
            continue
        for row in rows:
            p = os.path.normpath(os.path.join(site, row[0]))
            if not p.startswith(venv + os.sep):
                continue                                  # outside the venv: not one of its files
            recorded[p] = h = row[1] if len(row) > 1 else ""
            if os.path.basename(os.path.dirname(p)) == "__pycache__" and p.endswith(".pyc"):
                continue                                  # bytecode: pip compiles it again (so a hash can be stale)
            if not h:
                if p != own:                              # only the RECORD itself has no hash of its own
                    wrong.append(f"{os.path.relpath(p, venv)} has no hash in its RECORD")
                continue
            algo, _, digest = h.partition("=")
            if algo not in ("sha256", "sha384", "sha512"):
                wrong.append(f"{os.path.relpath(p, venv)} has a hash in its RECORD that is not sha256 or stronger")
                continue
            started.append(p)                             # what the service may have read at its start
            try:
                with open_regular(p) as f:
                    h = hashlib.new(algo)
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode() != digest:
                    wrong.append(f"{os.path.relpath(p, venv)} differs from its RECORD")
            except FileNotFoundError:
                wrong.append(f"{os.path.relpath(p, venv)} is missing")
            except NotRegular as e:
                wrong.append(unreadable(os.path.relpath(p, venv), e))
            except (OSError, ValueError) as e:
                wrong.append(f"{os.path.relpath(p, venv)} cannot be checked ({e.__class__.__name__})")
    # what python -m venv makes besides the packages: the activate scripts, and links of its own
    version = os.path.basename(os.path.dirname(site))     # python3.10
    made = {"pyvenv.cfg", "bin/activate", "bin/activate.csh", "bin/activate.fish", "bin/Activate.ps1"}
    links = {"lib64": "lib", "bin/python": "python3", f"bin/{version}": "python3"}   # bin/python3: the system's
    unrecorded = []
    for top, dirs, files in os.walk(venv, onerror=lambda e: unrecorded.append("(a directory that cannot be read)")):
        watched.append(top)
        for name in files + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
            p = os.path.join(top, name)
            rel = os.path.relpath(p, venv)
            if os.path.islink(p):
                if not (links.get(rel) == os.readlink(p) or rel == "bin/python3"):
                    unrecorded.append(f"{rel} (a link)")
            elif os.path.basename(top) == "__pycache__" and name.endswith(".pyc"):
                continue                                  # bytecode: checked against its source below
            elif p not in recorded and rel not in made and os.path.realpath(p) not in listed_real:
                unrecorded.append(rel)
    caches = []
    if same_python:
        for p in sorted(recorded):
            if p.endswith(".py"):
                w = pyc_wrong(p, venv)
                if w:
                    caches.append(w)
    # the list: one "<name>==<version>  <sha256 of its RECORD>" line for each dist-info directory
    want = []
    for l in repo_text(venv_list).splitlines():
        if re.fullmatch(r"\S+==\S+  [0-9a-f]{64}", l):
            want.append(l)
        elif l.strip() and not l.startswith("#"):
            bad.append(f"config/strata-venv.txt: not a '<name>==<version>  <sha256 of its RECORD>' line: {l}")
    have = [line for _, line in pins]
    extra = sorted((collections.Counter(have) - collections.Counter(want)).elements())
    missing = sorted((collections.Counter(want) - collections.Counter(have)).elements())
    twice = sorted(n for n, c in collections.Counter(re.sub(r"[-_.]+", "-", n).lower() for n, _ in pins).items() if c > 1)
    if extra or missing:
        bad.append(f"{strata}/.venv does not hold the packages config/strata-venv.txt lists, as it pins their RECORDs: "
                   f"the venv has {extra[:4]}, the list has {missing[:4]}")
    if twice:
        bad.append(f"{strata}/.venv has more than one dist-info directory of {', '.join(twice)}")
    if wrong or unrecorded or caches:
        bad.append(f"{strata}/.venv: " + "; ".join(wrong[:4] + [f"{p} was installed by no package" for p in sorted(unrecorded)[:4]] + caches[:4]))
    if not (extra or missing or twice or wrong or unrecorded or caches):
        oks.append(f"{strata}/.venv: the packages config/strata-venv.txt lists, with the RECORDs it pins, file by file "
                   "as their RECORDs give them, no other file, and their cached bytecode what their sources compile to")

# --- the service user's own site-packages (~/.local/lib/python3.10/site-packages), which a python3 that the user
# starts without -I or -s reads at its start, as the start path's python3 may: a .pth file there runs its import
# lines, and usercustomize and sitecustomize are imported
user = (re.findall(r"(?m)^\s*User\s*=\s*(\S+)", repo_text(unit_file, said=True)) or [""])[-1]
try:
    home = pwd.getpwnam(user).pw_dir
except KeyError:
    home = None
    bad.append(f"the unit's User ({user or 'none'}) has no passwd entry: its user's site-packages cannot be checked")
if home is not None:
    lib = os.path.join(sysroot + home, ".local", "lib")
    hooks, not_read = [], []                      # (not named unreadable: that is the function above)
    try:
        versions = sorted(n for n in os.listdir(lib) if n.startswith("python"))
    except FileNotFoundError:
        versions = []
    except OSError as e:
        versions, not_read = [], [f"{lib} ({e.strerror})"]
    for v in versions:
        d = os.path.join(lib, v, "site-packages")
        try:
            hooks += [os.path.join(d, n) for n in sorted(os.listdir(d))
                      if n.endswith(".pth") or n.split(".")[0] in ("usercustomize", "sitecustomize")]
        except FileNotFoundError:
            pass
        except OSError as e:
            not_read.append(f"{d} ({e.strerror})")
    if not_read:
        bad.append(f"the service user's site-packages cannot be read: {', '.join(not_read)}")
    if hooks:
        bad.append("the service user's site-packages hold code that a python3 started without -I runs at its start: "
                   + ", ".join(hooks[:4]))
    if not hooks and not not_read:
        oks.append(f"no .pth, usercustomize or sitecustomize in the service user's site-packages ({lib}/python*)")
shared = os.path.splitext(live)[0] + ".shared-settings.json"
if os.path.lexists(shared):
    bad.append(f"{shared} exists: it overrides the config's reasoning and sampling defaults for every client")
else:
    oks.append(f"no shared settings file ({shared})")

# --- the service's checkout, and this one
def checkout(repo, what):
    """repo's commit, after its files were compared with it ("" when it has none)."""
    if not top_is(repo):
        bad.append(f"{repo} is not the top of a git checkout of its own: what {what} cannot be tied to a commit")
        return ""
    head = head_of(repo)
    t = tree_of(repo, head) if head else None
    if t is None:
        bad.append(f"{repo}: its commit cannot be read")
        return ""
    try:
        diffs = compare(repo, t, head)
        untracked, ignored = others(repo, t)
    except OSError as e:
        bad.append(f"{repo}: {e}")
        return head
    # an ignored file where the service runs code from can shadow a module (tools/json.py); caches cannot
    risky = [p for p in ignored if "__pycache__" not in p.split(os.sep)
             and (os.sep not in p or p.split(os.sep)[0] in ("scripts", "tools", "config", "systemd"))]
    if diffs or untracked or risky:
        bad.append(f"{repo} has local changes: " + "; ".join(
            (diffs + [f"{p} is not in its commit" for p in untracked]
             + [f"{p} is ignored, but where the service runs code from" for p in risky])[:6]))
    else:
        oks.append(f"{repo}: its commit {head[:7]}, file by file, and no other file where the service runs code from")
    return head
if live_repo:
    live_head = checkout(live_repo, "the service runs")
    if os.path.realpath(here) != os.path.realpath(live_repo):
        if candidate == "1":
            notes.append(f"this check runs from {here}, which the service does not run: --candidate, a commit to be deployed")
        elif checkout(here, "this check runs") != live_head:
            bad.append(f"this check runs from {here}, at another commit than the service's checkout {live_repo}: run it "
                       "there, or with --candidate for a commit to be deployed")
        else:
            notes.append(f"this check runs from {here}, which the service does not run (at the same commit)")
if complete:                                      # else the check of what runs says it cannot list them
    for name, paths in (("started-files", started), ("started-dirs", watched)):
        with open(os.path.join(tmp, name), "wb") as f:
            f.write(b"\0".join(os.fsencode(p) for p in paths))
if timed_out:
    bad.append(f"git did not end within {GIT_LIMIT:g} s, and was taken to have failed: " + ", ".join(timed_out[:3])
               + (f", and {len(timed_out) - 3} more" if len(timed_out) > 3 else ""))
for o in oks:
    print(f"ok     {o}")
for n in notes:
    print(f"note   {n}")
for b in bad:
    print(f"DRIFT  {b}")
sys.exit(1 if bad else 0)
EOF

echo "== the running service"
python3 -I -S - "$UNIT" "$D/systemd/$UNIT" "$ENGINE_RE" "$live" "$D/config/deployed-artifacts.sha256" "$DEV" "$URL" \
  "${LIVE_REPO:-/nonexistent}" "$STRATA_DIR" "$pin" "$ROOT" "$tmp" <<'EOF' || bad=1
import datetime, grp, hashlib, io, json, locale, os, pwd, re, shlex, stat, struct, subprocess, sys, time, urllib.parse, \
    urllib.request
unit, unit_file, engine_re, live, manifest, dev, url, repo, strata, pin, sysroot, tmp = sys.argv[1:13]
bad, notes = [], []
class NotRegular(OSError):
    pass
def open_regular(p):
    """p opened to read, only when it is a regular file: stat before the open (a device is not opened), O_NONBLOCK (a
    FIFO put there meanwhile does not block the open) and fstat after it.  NotRegular otherwise."""
    if not stat.S_ISREG(os.stat(p).st_mode):
        raise NotRegular(0, "not a regular file", p)
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotRegular(0, "not a regular file", p)
    return open(fd, "rb")
def report(ok_line=None):
    for n in notes:
        print(f"note   {n}")
    for b in bad:
        print(f"DRIFT  {b}")
    if not bad and ok_line:
        print(f"ok     {ok_line}")
    sys.exit(1 if bad else 0)

# the service manager's own environment, which every start gets: the locale, and a PATH only root can change
try:
    r = subprocess.run(["systemctl", "show-environment"], capture_output=True, text=True, timeout=30)
    manager_env = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l) if r.returncode == 0 else None
except (OSError, subprocess.TimeoutExpired):
    manager_env = None
if manager_env is None:
    bad.append("systemctl show-environment cannot be read: what every service gets is not known")
    manager_env = {}
odd = sorted(k for k in manager_env if not re.fullmatch(r"LANG|LANGUAGE|LC_[A-Z_]+|PATH", k))
if odd:
    bad.append(f"the service manager gives every service {odd} (systemctl set-environment, or DefaultEnvironment=)")
def root_only(path):
    """Only root can change what path is: it and each directory above it (links resolved) are root's, and no group
    or other may write to them.  A directory that is not there counts as the first one above it that is."""
    p = os.path.realpath(path)
    while True:
        try:
            st = os.stat(p)
            if st.st_uid != 0 or st.st_mode & 0o022:
                return False
        except FileNotFoundError:
            pass
        except OSError:
            return False
        if p == "/":
            return True
        p = os.path.dirname(p)
for entry in manager_env.get("PATH", "").split(":"):
    if not entry.startswith("/") or not root_only(entry):
        bad.append(f"the service manager's PATH has {entry or '(an empty entry)'}, which is not absolute or which others "
                   "than root can change")

try:
    out = subprocess.run(["systemctl", "show", unit, "--timestamp=us+utc", "-p",
                          "ActiveState,MainPID,ExecMainStartTimestamp"], capture_output=True, text=True,
                         timeout=30).stdout
    s = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
except (OSError, subprocess.TimeoutExpired) as e:
    bad.append(f"{unit}: systemctl show failed ({e.__class__.__name__})")
    report()
state = s.get("ActiveState", "")
if not state:
    bad.append(f"{unit}: systemctl show gives no state")
    report()
if state != "active":
    notes.append(f"{unit} is {state}: what runs is not compared (scripts/check-deployment.sh reports the state)")
    report()
main = s.get("MainPID", "0")
def environ(pid):
    raw = open(f"/proc/{pid}/environ", "rb").read().decode(errors="replace")
    return dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
def argv(pid):
    return [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[:-1]]
def program(pid):
    """The file pid runs, and whether it was replaced on disk after it started."""
    exe = os.readlink(f"/proc/{pid}/exe")
    return (exe[:-len(" (deleted)")], True) if exe.endswith(" (deleted)") else (exe, False)
def first_differs(a, b):
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b))) + 1
try:
    server_env, server_argv = environ(main), argv(main)
    server_exe, server_replaced = program(main)
    server_cwd = os.path.realpath(f"/proc/{main}/cwd")
    status = dict(l.split(":", 1) for l in open(f"/proc/{main}/status").read().splitlines() if ":" in l)
    limits = open(f"/proc/{main}/limits").read()
    oom = open(f"/proc/{main}/oom_score_adj").read().strip()
    ticks = int(open(f"/proc/{main}/stat").read().rsplit(")", 1)[1].split()[19])      # its start, after the boot
    mounts, host_mounts = open(f"/proc/{main}/mountinfo").read(), open("/proc/self/mountinfo").read()
except (OSError, ValueError, IndexError):
    bad.append(f"{unit}: its main process ({main}) cannot be read")
    report()
# its start on the wall clock: the earlier of its main process's start by the boot clock, which counts the time of a
# suspend too (the monotonic clock does not), and of what systemd noted then (the wall clock may have been set back
# since, or forward)
since = time.clock_gettime(time.CLOCK_REALTIME) - time.clock_gettime(time.CLOCK_BOOTTIME) + ticks / os.sysconf("SC_CLK_TCK")
m = re.fullmatch(r"\w+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) UTC", s.get("ExecMainStartTimestamp", ""))
if m:
    since = min(since, datetime.datetime.fromisoformat(m.group(1)).replace(tzinfo=datetime.timezone.utc).timestamp())
else:
    bad.append(f"{unit}: its start time (ExecMainStartTimestamp) cannot be read: what changed after it may be missed")

# server.py's process as its unit starts it: the command line, the program, the directory, the user, the limits
u = {}
try:
    with io.TextIOWrapper(open_regular(unit_file), encoding="utf-8") as f:
        unit_lines = f.read().split("\n")           # at its line ends only, as systemd (splitlines() splits at \f too)
except (OSError, ValueError):
    bad.append(f"{unit_file} cannot be read (above): what runs is not compared with it")
    report()
if any("\0" in line for line in unit_lines):      # systemd ends a line there, this check does not
    bad.append(f"{unit_file} holds a NUL byte (above): what runs is not compared with it")
    report()
WS = " \t\n\r"                                    # systemd's WHITESPACE, as in the check of what systemd loaded
def boolean(text):                                # systemd's spellings of a boolean, as in the check of what it loaded
    t = text.strip().lower()
    return "yes" if t in ("1", "yes", "y", "true", "t", "on") else "no" if t in ("0", "no", "n", "false", "f", "off") else text
def size(text):                                   # a size with K, M, G... (powers of 1024), in bytes, as above
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?\s*([KMGTPEB]?)", text.strip())
    if not m:
        raise ValueError(text)
    mult = 1024 ** "BKMGTPE".index(m.group(3) or "B")
    frac = m.group(2) or "0"
    return int(m.group(1)) * mult + int(frac) * mult // 10 ** len(frac)
bom = False
for lineno, line in enumerate(unit_lines, 1):
    if not bom and line.startswith("\ufeff"):     # the first byte order mark at the start of a line: skipped, as
        line, bom = line[1:], True                # systemd skips it
    if line.lstrip(WS).startswith(("#", ";")):
        continue
    if (len(line) - len(line.rstrip("\\"))) % 2:
        bad.append(f"{unit_file}: its line {lineno} ends with a backslash, which goes on on the next line (above): what runs "
                   "is not compared with it")
        report()
    line = line.strip(WS)
    if line and not line.startswith("["):
        k, _, v = line.partition("=")
        u[k.strip(WS)] = v.strip(WS)
start = re.sub(r"^[-@:+!]*", "", u.get("ExecStart", ""))
if re.search(r"[%$\\]", start):
    bad.append(f"{unit}'s ExecStart has a specifier, a variable or an escape, which this check does not expand")
try:
    want_argv = shlex.split(start)
except ValueError as e:                           # an unbalanced quote (its text, not the command line's)
    bad.append(f"{unit}'s ExecStart cannot be split into words ({e}): what runs is not compared with it")
    report()
if server_argv != want_argv:                       # where, not what: a command line can hold a key
    bad.append(f"server.py's command line is not its unit's ExecStart (from word {first_differs(server_argv, want_argv)}): "
               "restart pending")
if server_replaced:
    bad.append(f"the program server.py runs ({server_exe}) was replaced on disk after it started: restart pending")
elif not want_argv or server_exe != os.path.realpath(want_argv[0]):
    bad.append("server.py runs another program than its unit's ExecStart names: restart pending")
if server_cwd != os.path.realpath(u.get("WorkingDirectory", "/")):
    bad.append("server.py runs in another directory than its unit's WorkingDirectory: restart pending")
try:
    if set(status["Uid"].split()) != {str(pwd.getpwnam(u["User"]).pw_uid)}:
        bad.append("server.py runs as another user than its unit's User: restart pending")
    if set(status["Gid"].split()) != {str(grp.getgrnam(u["Group"]).gr_gid)}:
        bad.append("server.py runs as another group than its unit's Group: restart pending")
except KeyError:
    bad.append("the unit's User or Group, or server.py's, cannot be read")
for key, name, conv in (("LimitMEMLOCK", "Max locked memory", size), ("LimitNOFILE", "Max open files", int)):
    if key in u:                                  # soft:hard, or one value for both; /proc/PID/limits: soft, hard
        m = re.search(rf"^{re.escape(name)}\s+(\S+)\s+(\S+)", limits, re.M)
        parts = u[key].split(":")
        try:
            want = tuple("unlimited" if p.strip() == "infinity" else str(conv(p.strip())) for p in (parts[0], parts[-1]))
        except ValueError:
            want = None
        if want is None or len(parts) > 2:
            bad.append(f"{unit}'s {key}= is a limit this check does not read: it is not compared with what server.py has")
        elif not m or m.groups() != want:
            bad.append(f"server.py's {key} is not its unit's: restart pending")
if "OOMScoreAdjust" in u and oom != u["OOMScoreAdjust"]:
    bad.append("server.py's OOMScoreAdjust is not its unit's: restart pending")
if "NoNewPrivileges" in u and status.get("NoNewPrivs", "").strip() != ("1" if boolean(u["NoNewPrivileges"]) == "yes" else "0"):
    bad.append("server.py's NoNewPrivileges is not its unit's: restart pending")
# and the file system its unit gives it, which systemd sets up when it starts it (a unit loaded after the start
# changes nothing of it): ProtectSystem= mounts /usr, /boot, /efi and /etc (full), or all of / (strict), read-only;
# PrivateTmp= gives it a /tmp and a /var/tmp of its own (systemd-private-...); ProtectHome=no leaves /home, /root and
# /run/user as they are.  Compared with this check's own view of the mounts
def mount_table(text):
    """The mounts of a mountinfo, in their order: (mount point, root, device, read-only)."""
    out = []
    unescape = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)       # noqa: E731
    for line in text.splitlines():
        f = line.split()
        if "-" in f[6:]:
            sep = f.index("-", 6)
            ro = "ro" in f[5].split(",") or (len(f) > sep + 3 and "ro" in f[sep + 3].split(","))
            out.append((unescape(f[4]), unescape(f[3]), f[2], ro))
    return out
def mount_of(table, path):
    """The mount path is on: of the longest mount point above it, the last one."""
    best = None
    for m in table:
        if (path == m[0] or path.startswith(m[0].rstrip("/") + "/")) and (best is None or len(m[0]) >= len(best[0])):
            best = m
    return best or ("", "", "", None)
proc, host = mount_table(mounts), mount_table(host_mounts)
READ_ONLY = {"no": set(), "yes": {"/usr", "/boot", "/efi"}, "full": {"/usr", "/boot", "/efi", "/etc"},
             "strict": {"/", "/usr", "/boot", "/efi", "/etc"}}
setting = lambda k: boolean(u.get(k, "no"))                                                   # noqa: E731
if setting("ProtectSystem") not in READ_ONLY:
    bad.append(f"{unit}'s ProtectSystem={u['ProtectSystem']}: this check does not compare it with what server.py runs with")
else:
    wrong = [p for p in sorted(READ_ONLY["strict"]) if os.path.isdir(p)          # what is not there, systemd skips
             and mount_of(proc, p)[3] != (p in READ_ONLY[setting("ProtectSystem")] or mount_of(host, p)[3])]
    if wrong:
        bad.append(f"server.py has {', '.join(wrong)} mounted otherwise (read-only or writable) than its unit's "
                   f"ProtectSystem={u.get('ProtectSystem', 'no')} gives: restart pending")
if setting("PrivateTmp") not in ("yes", "no"):
    bad.append(f"{unit}'s PrivateTmp={u['PrivateTmp']}: this check does not compare it with what server.py runs with")
else:
    for p in ("/tmp", "/var/tmp"):
        m = mount_of(proc, p)
        private = m[0] == p and "/systemd-private-" in m[1]
        if setting("PrivateTmp") == "yes" and not private:
            bad.append(f"server.py's {p} is not the one of its own that its unit's PrivateTmp=yes gives: restart pending")
        elif setting("PrivateTmp") == "no" and m[:3] != mount_of(host, p)[:3]:
            bad.append(f"server.py's {p} is not this machine's, which its unit's PrivateTmp=no gives: restart pending")
if setting("ProtectHome") != "no":
    bad.append(f"{unit}'s ProtectHome={u['ProtectHome']}: this check does not compare it with what server.py runs with")
else:
    wrong = [p for p in ("/home", "/root", "/run/user") if os.path.isdir(p) and mount_of(proc, p)[:3] != mount_of(host, p)[:3]]
    if wrong:
        bad.append(f"server.py does not see {', '.join(wrong)} as this machine has them, which its unit's ProtectHome=no "
                   "gives: restart pending")
# server.py's own options: the ones below are all this check follows (--gpu, --replicas, --lazy and the others change
# what it starts), and only the Strata engine is compared
cli, words = {}, server_argv[2:]
while words:
    name, eq, value = words[0].partition("=")
    cli[name], words = (value, words[1:]) if eq else ((words[1:2] or [None])[0], words[2:])
if set(cli) - {"--engine", "--config", "--port", "--host", "--api-key"} or cli.get("--engine") != "strata":
    bad.append("server.py's command line has options this check does not follow, or no --engine strata: compare the "
               "engine with server.py by hand")

# the environment: what the service manager gives every service, the unit's own, and what systemd sets itself for a
# service (systemd.exec(5)); names only (a value can be a key)
def bus_environment(unit):
    """The loaded Environment= of unit, each item as systemd holds it, read from its bus property with busctl (read
    only, as any user).  systemctl show prints an item with a blank in it as [unprintable], and one with a quote as it
    is, which no split reads back"""
    path = "/org/freedesktop/systemd1/unit/" + "".join(        # sd_bus_path_encode(): _xx for all but a letter or
        c if c.isascii() and c.isalnum() and (i or not c.isdigit()) else f"_{ord(c):02x}"    # a digit (not first)
        for i, c in enumerate(unit))
    iface = "org.freedesktop.systemd1." + unit.rpartition(".")[2].capitalize()
    r = subprocess.run(["busctl", "--json=short", "get-property", "org.freedesktop.systemd1", path, iface, "Environment"],
                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    got = json.loads(r.stdout) if r.returncode == 0 else None
    if not (isinstance(got, dict) and got.get("type") == "as" and isinstance(got.get("data"), list)
            and all(isinstance(x, str) for x in got["data"])):
        raise OSError("busctl get-property failed, or printed no list of strings")
    return got["data"]
try:
    unit_env = dict(kv.split("=", 1) for kv in bus_environment(unit) if "=" in kv)
except (OSError, ValueError, subprocess.TimeoutExpired) as e:
    unit_env = None
    bad.append(f"{unit}'s loaded Environment cannot be read ({e.__class__.__name__}: busctl): server.py's environment is "
               "not compared")
SYSTEMD = {"HOME", "LOGNAME", "SHELL", "USER", "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID",
           "RUNTIME_DIRECTORY", "STATE_DIRECTORY", "CACHE_DIRECTORY", "LOGS_DIRECTORY", "CONFIGURATION_DIRECTORY",
           "CREDENTIALS_DIRECTORY", "NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC", "MAINPID", "MANAGERPID"}
given = {**manager_env, **(unit_env or {})}
differ = sorted(k for k, v in given.items() if server_env.get(k) != v)
if differ and unit_env is not None:
    bad.append(f"server.py's {differ} are not what its loaded unit and the service manager give it: restart pending")
extra = sorted(set(server_env) - set(given) - SYSTEMD)
if extra and unit_env is not None:
    bad.append(f"server.py has {extra}, which neither its loaded unit nor systemd gives it: restart pending, or it was "
               "started another way")
try:                                              # what systemd gives a service of a User=, from its passwd entry:
    pw = pwd.getpwnam(u["User"])                  # no HOME for a home of / or none, no SHELL for a nologin one
    nologin = {f"{d}/{n}" for d in ("/bin", "/sbin", "/usr/bin", "/usr/sbin") for n in ("nologin", "false", "true")}
    own = {"HOME": pw.pw_dir if pw.pw_dir.startswith("/") and pw.pw_dir != "/" else None, "USER": pw.pw_name,
           "LOGNAME": pw.pw_name, "SHELL": pw.pw_shell if pw.pw_shell.startswith("/") and pw.pw_shell not in nologin else None}
    differ = sorted(k for k, v in own.items() if k not in given and server_env.get(k) != v)
    if differ:
        bad.append(f"server.py's {differ} are not what systemd gives a service of its User ({u['User']}, by its passwd "
                   "entry): restart pending, or it was started another way")
except KeyError:
    pass                                          # said above

try:
    with io.TextIOWrapper(open_regular(live), encoding="utf-8-sig") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError
except NotRegular:
    bad.append(f"{live} is not a regular file: what the service should run is not known")
    report()
except (OSError, ValueError):
    bad.append(f"{live} cannot be read: what the service should run is not known")
    report()
# the key as server.py takes it (--api-key, else STRATA_API_KEY, else the config's api_key) and reads it (key_list:
# the items of a list, or a string's comma-separated parts, without their blanks): the first one, sent only to this
# machine, and none with a control character
v = None
for i, w in enumerate(server_argv):
    if w == "--api-key" and i + 1 < len(server_argv):
        v = server_argv[i + 1]
    elif w.startswith("--api-key="):
        v = w[len("--api-key="):]
v = v or server_env.get("STRATA_API_KEY") or cfg.get("api_key")
keys = [str(k) for k in v] if isinstance(v, (list, tuple)) else ("" if v is None else str(v)).split(",")
keys = [k.strip() for k in keys if k.strip()]
key = keys[0] if keys and not any(ord(c) < 32 or ord(c) == 127 for c in keys[0]) else ""
if urllib.parse.urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
    key = ""
class Stay(urllib.request.HTTPRedirectHandler):   # a redirect would take the key elsewhere: it is an error
    def redirect_request(self, *args, **kwargs):
        return None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Stay)      # this machine, no proxy
class Moved(Exception):
    """What listens on the server's port is no longer what this check found there, or no longer server.py's."""
def get(path):
    if not still_ours():                          # below: nothing is sent then, with the key or without it
        raise Moved()
    req = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {key}"} if key else {})
    with opener.open(req, timeout=10) as r:
        return r.read()
def why(x):                                       # never the exception's text: a header in it can hold the key
    if isinstance(x, Moved):
        return f"what listens on port {port} changed while this check ran: nothing was sent"
    code = getattr(x, "code", None)
    return f"HTTP {code}" if isinstance(code, int) else x.__class__.__name__

children, strays, kids = [], [], []               # kids: every child of server.py
engine_maps = set()                               # the files the engine maps: (major, minor, inode) of each
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        ppid = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
        if ppid == int(main):
            kids.append(p)
        if re.search(engine_re, " ".join(argv(p))):
            (children if ppid == int(main) else strays).append(int(p))
    except (OSError, ValueError, IndexError):
        continue
if strays:
    bad.append(f"another engine runs (pids {', '.join(map(str, strays))}): strata-preflight.sh refuses a start while it does")
# who listens on the server's port: every socket that listens there must be server.py's (one of its main process's
# open files, or of a child's).  Only then does it get the key, and only then is what it answers taken
def listening(port):
    """The inodes of the TCP sockets (IPv4 and IPv6) that listen on port."""
    found = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(name).read().splitlines()[1:]
        except FileNotFoundError:
            continue                              # no IPv6
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                found.add(f[9])
    return found
def sockets_of(pid):
    out = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except FileNotFoundError:
        if pid == main:
            raise
        return out                                # a child that exited meanwhile
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except FileNotFoundError:
            continue                              # closed meanwhile
        if target.startswith("socket:["):
            out.add(target[len("socket:["):-1])
    return out
try:
    port = urllib.parse.urlsplit(url).port or 80
    socks = listening(port)
    ours = set().union(*(sockets_of(p) for p in [main, *kids]))
except (OSError, ValueError, IndexError) as x:
    socks, ours = None, set()
    bad.append(f"who listens on the server's port cannot be read ({x.__class__.__name__}): it gets no key, and what it "
               "answers is not taken")
if socks is not None and not socks:
    bad.append(f"nothing listens on port {port}: server.py does not answer there")
elif socks is not None and not socks <= ours:
    bad.append(f"another process than server.py (its main process or a child) listens on port {port}: it gets no key, "
               "and what it answers is not taken")
answers = bool(socks) and socks <= ours
if not answers:
    key = ""
def children_of(pid):
    out = []
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            if int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1]) == int(pid):
                out.append(p)
        except (OSError, ValueError, IndexError):
            continue                              # exited meanwhile
    return out
def still_ours():
    """Whether what listens on the port is still what this check found there, and server.py's (its main process's or
    a child's): looked at again right before each request, so that the key does not go to a listener that took the
    port after the first look.  One that takes it between this look and the connect still gets it: that window, as
    long as a connect, this check cannot close."""
    try:
        now = listening(port)
        return now == socks and now <= set().union(*(sockets_of(p) for p in [main, *children_of(main)]))
    except (OSError, ValueError, IndexError):
        return False
listed, unread, records = {}, set(), False      # unread: the entries under a "(not read at start)" heading
try:
    with io.TextIOWrapper(open_regular(manifest), encoding="utf-8") as f:
        manifest_lines = f.read().splitlines()
except (OSError, ValueError):
    manifest_lines = []                           # said above
for line in manifest_lines:
    if line.startswith("#"):
        records = "(not read at start)" in line
        continue
    m = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
    if m:
        path = os.path.realpath(m.group(2) if m.group(2).startswith("/") else os.path.join(dev, m.group(2)))
        listed[path] = m.group(1)
        if records:
            unread.add(path)
DEV = os.path.realpath(dev) + "/"
# the system's files, which its package manager keeps, and memory that is no file
SYSTEM = ("/usr/", "/lib/", "/lib64/", "/bin/", "/sbin/", "/etc/", "/proc/", "/dev/", "/sys/", "/memfd:", "/SYSV")
LIB = re.compile(r"\.so(\.\d+)*$")
libs = {LIB.sub(".so", os.path.basename(p)): p for p in listed if LIB.search(p)}
must_map = sorted(p for p in libs.values() if not p.startswith(DEV))       # the CUDA libraries, outside ~/dev

def split_joined(args):                           # server.py: split_joined_args
    out = []
    for a in args:
        m = re.match(r"^(--[A-Za-z0-9][A-Za-z0-9-]*)(?:\s+|=)(\S.*)$", a) if isinstance(a, str) else None
        prev = out[-1] if out else None
        if m is None or (isinstance(prev, str) and re.match(r"^--[A-Za-z0-9][A-Za-z0-9-]*$", prev)):
            out.append(a)
        else:
            out += [m.group(1), m.group(2).strip()]
    return out
def lift_effort(args):                            # server.py: lift_reasoning_effort_arg (not an engine option)
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--reasoning-effort" and i + 1 < len(args):
            i += 2
        elif isinstance(a, str) and a.startswith("--reasoning-effort="):
            i += 1
        else:
            out.append(a)
            i += 1
    return out
def gpu_list(c):                                  # server.py: gpu_list
    g = c.get("gpu")
    if g is None or g == "":
        return []
    items = g if isinstance(g, (list, tuple)) else str(g).split(",")
    return [int(str(x).strip()) for x in items if str(x).strip() != ""]
def layer_split_value(c):                         # server.py: layer_split_value
    v = c.get("layer_split")
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "auto")):
        return "auto"
    n = len(gpu_list(c))
    items = v if isinstance(v, (list, tuple)) else str(v).split(",") if isinstance(v, (str, int)) else None
    if not items or any(isinstance(x, (bool, float)) for x in items):
        raise ValueError("layer_split")
    vals = [int(str(x).strip()) for x in items]
    if (n > 1 and len(vals) != n - 1) or vals[0] < 2 or any(b <= a for a, b in zip(vals, vals[1:])):
        raise ValueError("layer_split")
    return ",".join(str(x) for x in vals)
def locale_there(name):
    try:
        old = locale.setlocale(locale.LC_CTYPE)
        locale.setlocale(locale.LC_CTYPE, name)
        locale.setlocale(locale.LC_CTYPE, old)
        return True
    except locale.Error:
        return False
def soname(path):
    """The name that the loader looks the library at path up by: its DT_SONAME, else its file name.  Read with struct
    from its ELF header, program headers and dynamic section; ValueError when it is no ELF file that reads so."""
    with open_regular(path) as f:
        def at(offset, n):
            f.seek(offset)
            data = f.read(n)
            if len(data) != n:
                raise ValueError("short")
            return data
        head = at(0, 64)
        if head[:4] != b"\x7fELF" or head[4] not in (1, 2) or head[5] not in (1, 2):
            raise ValueError("not ELF")
        wide, e = head[4] == 2, "<" if head[5] == 1 else ">"
        if wide:        # Elf64_Ehdr, Elf64_Phdr: p_type p_flags p_offset p_vaddr p_paddr p_filesz p_memsz p_align
            (phoff,), (phentsize, phnum) = struct.unpack_from(e + "Q", head, 32), struct.unpack_from(e + "HH", head, 54)
            ph, size, pick, dyn = e + "IIQQQQQQ", 56, (0, 2, 3, 5), e + "qQ"
        else:           # Elf32_Ehdr, Elf32_Phdr: p_type p_offset p_vaddr p_paddr p_filesz p_memsz p_flags p_align
            (phoff,), (phentsize, phnum) = struct.unpack_from(e + "I", head, 28), struct.unpack_from(e + "HH", head, 42)
            ph, size, pick, dyn = e + "IIIIIIII", 32, (0, 1, 2, 4), e + "iI"
        if phentsize < size or not 0 < phnum < 0xffff:
            raise ValueError("program headers")
        table = at(phoff, phentsize * phnum)
        loads, dynamic = [], None
        for i in range(phnum):
            v = struct.unpack_from(ph, table, i * phentsize)
            typ, offset, vaddr, filesz = (v[j] for j in pick)
            if typ == 1:                                # PT_LOAD
                loads.append((vaddr, offset, filesz))
            elif typ == 2:                              # PT_DYNAMIC
                dynamic = (offset, filesz)
        if dynamic is None:
            raise ValueError("no dynamic section")
        step = struct.calcsize(dyn)
        data, strtab, name = at(dynamic[0], min(dynamic[1], 1 << 20) // step * step), None, None
        for i in range(0, len(data), step):
            tag, value = struct.unpack_from(dyn, data, i)
            if tag == 0:                                # DT_NULL
                break
            if tag == 5:                                # DT_STRTAB, an address
                strtab = value
            elif tag == 14:                             # DT_SONAME, an offset in it
                name = value
        if name is None:
            return os.path.basename(path)
        start = next((offset + strtab - vaddr for vaddr, offset, filesz in loads
                      if strtab is not None and vaddr <= strtab < vaddr + filesz), None)
        if start is None:
            raise ValueError("no string table")
        f.seek(start + name)
        text = f.read(4096).split(b"\0", 1)
        if len(text) < 2 or not text[0] or b"/" in text[0]:
            raise ValueError("SONAME")
        return os.fsdecode(text[0])
def loader_subdirs():
    """The subdirectories that the system's loader searches in each directory of a library path, in its order,
    before the directory itself (glibc-hwcaps/x86-64-v3, ..., tls/haswell/x86_64, ..., x86_64): what it shows for a
    directory on LD_LIBRARY_PATH under LD_DEBUG=libs, so those this CPU supports and no more.  None when that cannot be
    read."""
    probe = os.path.join(os.path.realpath(tmp), "ld-probe")       # an empty directory of this check's own
    true = next((p for p in ("/bin/true", "/usr/bin/true") if os.path.isfile(p)), "/bin/true")
    try:
        os.mkdir(probe)
        r = subprocess.run([true], env={"LD_DEBUG": "libs", "LD_LIBRARY_PATH": probe}, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stderr.decode(errors="replace").splitlines():
        m = re.search(r"search path=(.*?)\s+\(LD_LIBRARY_PATH\)\s*$", line)
        paths = m.group(1).split(":") if m else []
        if probe in paths and all(p.startswith(probe + "/") for p in paths[:paths.index(probe)]):
            return [p[len(probe) + 1:] for p in paths[:paths.index(probe)]]
    return None
def expected(c):
    """The engine's command line, directory and environment as server.py makes them of the config c (split_joined_args,
    lift_reasoning_effort_arg, engine_args, parallel_args, child_env, StrataEngine), and the config's settings this
    check does not follow."""
    # the shapes server.py's code takes: one of another shape it refuses (an env that is a list: a traceback) or
    # misreads (lib_dirs as a string: each of its letters a directory)
    for shape, ok in (("args is not a list of strings", isinstance(c.get("args"), list)
                       and all(isinstance(a, str) for a in c["args"])),
                      ("env is not a mapping", not c.get("env") or isinstance(c["env"], dict)),
                      ("lib_dirs is not a list of strings", not c.get("lib_dirs") or isinstance(c["lib_dirs"], list)
                       and all(isinstance(d, str) for d in c["lib_dirs"]))):
        if not ok:
            raise TypeError("shape", shape)
    c = {**c, "args": lift_effort(split_joined(c["args"])) if isinstance(c.get("args"), list) else c.get("args")}
    args, gpus = list(c["args"]), gpu_list(c)
    replicas = c.get("replicas")
    odd = [k for k, on in (
        ("vision", isinstance(c.get("vision"), dict)),
        ("backend", c.get("backend") not in (None, "cuda")),
        ("effort_position", c.get("effort_position", "start") != "start"),
        ("expert_profile_save", isinstance(c.get("expert_profile_save"), str) and bool(c["expert_profile_save"].strip())),
        ("replicas", replicas not in (None, False) and not (type(replicas) is int and replicas <= 1)),
        ("gpu_order", len(gpus) > 1 and c.get("gpu_order") != "as_given" and "--peer-device" not in args
         and "--layer-split" not in args and layer_split_value(c) == "auto")) if on]
    split = len(gpus) > 1 and "--peer-device" not in c["args"]
    if split and "--layer-split" not in args:
        args += ["--layer-split", layer_split_value(c)]
    if split and c.get("split_skip_if_fits") and "--split-skip-if-fits" not in args:
        args.append("--split-skip-if-fits")
    if c.get("vram_elastic") is True and "--vram-elastic" not in args:
        args.append("--vram-elastic")
        seg = c.get("vram_segment_mib")
        if type(seg) is int and seg > 0 and "--vram-segment-mib" not in args:
            args += ["--vram-segment-mib", str(seg)]
    n = c.get("parallel")
    if "--batch" not in args and "--slots" not in args and type(n) is int and n > 1:
        args += ["--batch", str(n)]
    exe = c["exe"] if os.path.isabs(c["exe"]) else os.path.normpath(os.path.join(server_cwd, c.get("cwd") or ".", c["exe"]))
    env = dict(server_env)
    # Python sets LC_CTYPE for itself, and so for the engine, when the locale is C or POSIX or is not there (PEP 538)
    ctype = server_env.get("LC_ALL") or server_env.get("LC_CTYPE") or server_env.get("LANG") or "C"
    if not server_env.get("LC_ALL") and server_env.get("PYTHONCOERCECLOCALE") != "0" and \
            (ctype in ("C", "POSIX") or not locale_there(ctype)):
        env["LC_CTYPE"] = "C.UTF-8"
    if gpus:
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpus)
    for k, val in (c.get("env") or {}).items():
        env[str(k)] = str(val)
    dirs = [d for d in c.get("lib_dirs") or [] if os.path.isdir(os.path.join(server_cwd, d))]
    if dirs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return [exe, "--serve", *args], os.path.realpath(os.path.join(server_cwd, c.get("cwd") or ".")), env, odd

if not children:
    try:
        unloaded = answers and b'"unloaded"' in get("/v1/models")
    except Exception:                             # whatever it is, its text is not printed: it can hold the key
        unloaded = False
    if unloaded:
        notes.append("server.py runs no engine: the model is unloaded on purpose (POST /unload), so no engine is compared")
    else:
        bad.append("server.py runs no engine, and /v1/models does not say that the model was unloaded")
elif len(children) > 1:
    bad.append(f"server.py has {len(children)} engine processes, not 1")
else:
    e = children[0]
    try:
        exe, replaced = program(e)
        a, eng_env, eng_cwd = argv(e), environ(e), os.path.realpath(f"/proc/{e}/cwd")
        maps = open(f"/proc/{e}/maps").read()
        for line in maps.splitlines():                # its device as major:minor in hex, and its inode
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[4] != "0" and not parts[5].endswith(" (deleted)"):
                major, _, minor = parts[3].partition(":")
                engine_maps.add((int(major, 16), int(minor, 16), int(parts[4])))
        h = hashlib.sha256()
        with open(f"/proc/{e}/exe", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        got = h.hexdigest()
    except (OSError, ValueError) as x:
        bad.append(f"the engine ({e}) cannot be read, or exited while it was compared ({x.__class__.__name__})")
    else:
        try:
            want, want_cwd, want_env, odd = expected(cfg)
        except Exception as x:                    # its class, not its text: a value in it can be a key
            want, odd = None, [f"a value server.py would refuse or misread ({x.args[1]})" if x.args[:1] == ("shape",)
                               else f"a value server.py would refuse ({x.__class__.__name__})"]
        if replaced:
            notes.append("the engine runs a binary that was replaced on disk after it started: its contents are compared below")
        if odd:
            bad.append(f"the live config has {odd}, which this check does not follow: compare the engine with server.py by hand")
        else:
            if not replaced and exe != os.path.realpath(want[0]):
                bad.append(f"the engine runs {exe}, the live config's exe is {cfg.get('exe')}")
            want_sha = listed.get(os.path.realpath(want[0]))
            if want_sha is None:
                bad.append(f"config/deployed-artifacts.sha256 does not list the config's exe ({cfg.get('exe')})")
            elif got != want_sha:
                bad.append(f"the running engine's binary has sha256 {got[:16]}…, the list says {want_sha[:16]}…")
            if a != want:
                bad.append(f"the engine's command line is not what server.py makes of the live config (from word "
                           f"{first_differs(a, want)}): restart pending, or the config was edited")
            if eng_cwd != want_cwd:
                bad.append("the engine runs in another directory than the live config's cwd: restart pending")
            env_differ = sorted(k for k in set(want_env) | set(eng_env) if want_env.get(k) != eng_env.get(k))
            if env_differ:
                bad.append(f"the engine's environment is not what server.py makes of the live config, in {env_differ}: "
                           "restart pending, or the config was edited")
        # the files it maps: outside the system's directories, each one listed and not replaced (one from a directory
        # of its library path, ~/cuda-12.9/bin first, too); the listed CUDA libraries mapped, and no other copy of one
        seen = set()
        for line in maps.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6 or parts[5] in seen:
                continue
            seen.add(parts[5])
            gone = parts[5].endswith(" (deleted)")
            path = parts[5][:-len(" (deleted)")] if gone else parts[5]
            lib = libs.get(LIB.sub(".so", os.path.basename(path)))
            if lib and path != lib:
                bad.append(f"the engine maps {path}, another copy of the listed {lib}")
            if not path.startswith("/") or path.startswith(SYSTEM) or path == exe:
                continue
            if gone:
                bad.append(f"the engine maps a file that was replaced or deleted after it started: {path}")
            elif os.path.realpath(path) not in listed:
                bad.append(f"the engine maps {path}, which config/deployed-artifacts.sha256 does not list")
        mapped = {p[:-len(" (deleted)")] if p.endswith(" (deleted)") else p for p in seen}
        for p in must_map:
            if p not in mapped:
                bad.append(f"the engine does not map {p}, which the list names: it loads another copy, or none")
        # its library path as the loader (glibc's ld.so) searches it at the next start: the directories of its
        # LD_LIBRARY_PATH (an empty entry is its directory), then each object's RUNPATH, which this check takes to be
        # the directories it found the libraries it maps outside the system's in (the engine's RUNPATH,
        # ~/cuda-12.9/lib64, is where they are; a library found through another name, a link elsewhere, is taken to
        # come from its file's directory), then ld.so.cache and the system's directories, which only root can change
        # and this check does not look in.  In each directory the loader looks first in the subdirectories that this
        # CPU supports (glibc-hwcaps/x86-64-v3 ... x86_64: its own list, from LD_DEBUG), then in the directory, for the
        # name that the object needs: the library's SONAME (its file name when it has none).  A file of that name that
        # comes before the one the engine maps now is what its next start loads instead: the CUDA directories come
        # first, and their owner can write to them.  One of them changed after its start is noted
        ld = [os.path.realpath(os.path.join(eng_cwd, d)) for d in re.split("[:;]", eng_env["LD_LIBRARY_PATH"])] \
            if eng_env.get("LD_LIBRARY_PATH") else []
        found_in = [os.path.dirname(p) for p in sorted(mapped) if p.startswith("/") and LIB.search(p)
                    and not p.startswith(SYSTEM)]
        order = list(dict.fromkeys(ld + found_in))
        subdirs = loader_subdirs()
        if subdirs is None:
            bad.append("the loader's search order cannot be read (LD_DEBUG=libs of /bin/true): the subdirectories of "
                       "the engine's library path that it searches first are not looked in")
            subdirs = []
        search = [w for d in order for w in [os.path.join(d, s) for s in subdirs] + [d]]
        for p in sorted(p for p in mapped if p.startswith("/") and LIB.search(p)):
            try:
                name = soname(p)
            except (OSError, ValueError) as x:
                bad.append(f"{p}, which the engine maps, cannot be read as a library "
                           f"({'not a regular file' if isinstance(x, NotRegular) else x.__class__.__name__}): which "
                           "file its next start loads under its name is not known")
                continue
            for w in search:
                n = os.path.join(w, name)
                if os.path.isfile(n):                   # what is not there, the loader skips
                    if os.path.realpath(n) != os.path.realpath(p):
                        bad.append(f"the engine's next start would load {n}, which comes earlier on its library path, "
                                   f"instead of {p}, which it maps now")
                    break
        for d in order:
            try:
                st = os.stat(d)
                if max(st.st_mtime, st.st_ctime) > since:
                    notes.append(f"{d}, on the engine's library path, changed after it started: what its next start "
                                 "loads from there may differ")
            except FileNotFoundError:
                pass
            except OSError:
                bad.append(f"{d}, on the engine's library path, cannot be read")
try:
    if answers:
        st = json.loads(get("/settings"))
        if st.get("shared") or st.get("defaults"):
            bad.append("the server has shared settings (GET /settings): they override the config's reasoning and "
                       "sampling defaults for every client")
except Exception as x:                            # whatever it is, its text is not printed: it can hold the key
    bad.append(f"GET /settings cannot be read ({why(x)}): whether the server has shared settings is not known")

# what the service read at its start: the live config, the listed files but the records under "(not read at start)",
# the Strata tree's code, its venv's files (those a RECORD hash gives), its installed unit.  One whose modification time
# is after the start can hold another content than the one the service read (also one changed and changed back, and one
# that matches the list again): restart pending.  So can one whose inode alone changed after it (its ctime): a link
# added or removed, its mode or its owner changed, or a content put back with its old modification time (cp -p,
# shutil.copy2) after a change before the start, and then the service runs what it read before; this check cannot tell
# them apart.  But a file that the engine maps now, the very file (the device and the inode of its mapping are the
# file's), whose content is what the list says, is noted (as when the IQ3_XXS name of the shard that both packs share
# goes), and when it is not hashed (over 1 GiB, without --full) the note says to run --full.  (Its mapping shows what
# the file holds now: a content put back in place with its old time, after a change before the start, looks so too,
# though the engine may have copied the changed one at its start.)  In the directories it imports from (the tree's and
# the venv's), an entry added, removed or renamed after the start is noted: one note for each of the first 8 such
# directories, which names up to 4 of its entries newer than the start, and a count of the other directories.  A module
# added there is drift above, one removed or renamed leaves only this note.  The start scripts of its checkout take
# effect at the next start
def changed(f):
    try:
        st = os.stat(f)
        return max(st.st_mtime, st.st_ctime) > since
    except OSError:
        return False                              # not there: said above
def read_paths(name, missing_ok):
    try:
        with open(os.path.join(tmp, name), "rb") as f:
            return [os.fsdecode(p) for p in f.read().split(b"\0") if p]
    except FileNotFoundError:
        if missing_ok:
            return []
        raise
try:
    files, dirs = read_paths("started-files", False), read_paths("started-dirs", False)
except OSError:
    files, dirs = [], []
    bad.append("the Strata tree or its venv cannot be read (above): what the service read at its start cannot be listed")
verified = {os.path.realpath(p) for p in read_paths("verified", True)}
unhashed = {os.path.realpath(p) for p in read_paths("unhashed", True)}
late, relinked, kept, unknown = [], [], [], []
for f in dict.fromkeys([live, *sorted(set(listed) - unread), *files, f"{sysroot}/etc/systemd/system/{unit}"]):
    try:
        st = os.stat(f)
    except OSError:
        continue                                  # not there: said above
    if st.st_mtime > since:
        late.append(f)
    elif st.st_ctime <= since:
        continue
    elif (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino) not in engine_maps:
        relinked.append(f)
    elif os.path.realpath(f) in verified:
        kept.append(f)
    elif os.path.realpath(f) in unhashed:
        unknown.append(f)
    else:
        late.append(f)
if late:
    bad.append(f"{len(late)} files the service read at its start changed after it: restart pending ({', '.join(late[:4])})")
if relinked:
    bad.append(f"{len(relinked)} files the service read at its start changed after it, though not their modification "
               f"time: a link, their mode or their owner changed, or a content was put back with its old modification "
               f"time (cp -p), and then the service runs what it read before: restart pending ({', '.join(relinked[:4])})")
if kept:
    notes.append(f"{len(kept)} files that the service read at its start and that the engine maps, these very files: their "
                 f"inode changed after the start (a link, their mode or their owner), not their content, which is what "
                 f"the list says ({', '.join(kept[:4])})")
if unknown:
    notes.append(f"{len(unknown)} files over 1 GiB that the service read at its start and that the engine maps, these very "
                 f"files: their inode changed after the start (a link, their mode or their owner), and their content is "
                 f"hashed only with --full: run it to know whether a restart is pending ({', '.join(unknown[:4])})")
def newer(d):
    """The entries of the directory d whose own times are after the start."""
    out = []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return out
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue                              # gone meanwhile
        if max(st.st_mtime, st.st_ctime) > since:
            out.append(e.name)
    return sorted(out)
moved = [d for d in dict.fromkeys(dirs) if changed(d)]
for d in moved[:8]:
    newer_ones = newer(d)
    notes.append(f"{d}, where the service imports from, changed after its start ("
                 + (f"newer than it: {', '.join(newer_ones[:4])}" if newer_ones else "no entry there is newer than it")
                 + "): an entry was added, removed or renamed there, or its mode or owner changed; if a module that the "
                 "service had imported was removed or renamed, the service runs code that is no longer on disk (restart)")
if len(moved) > 8:
    notes.append(f"and {len(moved) - 8} more directories where the service imports from changed after its start")
for f in (os.path.join(repo, p) for p in ("config/profile.conf", "tools/configure_strata_q3.py",
                                         "scripts/strata-preflight.sh", "scripts/strata-wait-ready.sh")):
    if changed(f):
        notes.append(f"{f} changed after {unit} started: it takes effect at the next start")
report(f"{unit} runs what the files above say")
EOF

echo
if [ $bad = 0 ]; then echo "no drift"; else echo "drift found: see DRIFT lines above"; fi
exit $bad
