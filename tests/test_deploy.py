"""scripts/deploy.sh in throwaway repositories: a bare origin, a clone that plays this checkout, and stubs for GitHub's
API (curl), systemctl, timeout, tar, scripts/test.sh and scripts/check-drift.sh.  Needs git; no network, no service, no
sudo."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import termios
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run  # noqa: E402

DEPLOY = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
URL = "https://github.com/example/ops.git"   # never contacted: url.<origin>.insteadOf sends git to the bare repo
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}   # not a caller's GIT_DIR or GIT_INDEX_FILE
GIT_OPTS = "-c gc.auto=0 -c maintenance.auto=false -c core.quotePath=true"

STUBS = {
    # GET .../actions/workflows/test.yml/runs?head_sha=<sha>&...: runs/<sha>.json; without it, or with runs/<sha>.http
    # and -f (an HTTP error), curl exits 22
    "bin/curl": """#!/bin/sh
echo "$*" >> "$STUB_DIR/curl.log"
fail=0
for a; do url=$a; case $a in --fail) fail=1 ;; --*) ;; -*f*) fail=1 ;; esac; done
sha=${url#*head_sha=}; sha=${sha%%&*}
[ -e "$STUB_DIR/runs/$sha.json" ] || exit 22
[ ! -e "$STUB_DIR/runs/$sha.http" ] || [ $fail = 0 ] || exit 22
cat "$STUB_DIR/runs/$sha.json"
""",
    # ActiveState: the next word of $STUB_STATES at each call (then the last one again); Job: $STUB_JOB
    "bin/systemctl": """#!/bin/sh
case "$*" in
  "show -p ActiveState --value strata-q3.service")
    [ -z "${STUB_STATE_FAIL-}" ] || exit 1
    n=$(( $(cat "$STUB_DIR/states.n" 2>/dev/null || echo 0) + 1 )); echo $n > "$STUB_DIR/states.n"
    set -- ${STUB_STATES-active}
    if [ $# = 0 ]; then echo; else [ $n -le $# ] || n=$#; eval "echo \\"\\${$n}\\""; fi ;;
  "show -p Job --value strata-q3.service") [ -z "${STUB_JOB_FAIL-}" ] || exit 1; echo "${STUB_JOB-}" ;;
  *) exit 1 ;;
esac
""",
    # the real timeout, except for a command named $STUB_TIMEOUT_FOR: that one "runs out of time" (124), or ends with
    # $STUB_TIMEOUT_RC, at once or, with $STUB_TIMEOUT_LATE, once its time is up (the arguments are -k 10 <time> ...)
    "bin/timeout": """#!/bin/sh
echo "$*" >> "$STUB_DIR/timeout.log"
if [ -n "${STUB_TIMEOUT_FOR-}" ]; then
  for a; do case $a in */"$STUB_TIMEOUT_FOR") [ -z "${STUB_TIMEOUT_LATE-}" ] || sleep "$3"; exit "${STUB_TIMEOUT_RC-124}" ;; esac; done
fi
PATH=${PATH#"$STUB_DIR/bin:"} exec timeout "$@"
""",
    # the real tar, unless $STUB_TAR_FAIL is set
    "bin/tar": """#!/bin/sh
[ -z "${STUB_TAR_FAIL-}" ] || exit 2
PATH=${PATH#"$STUB_DIR/bin:"} exec tar "$@"
""",
}
# the repository's own checks; each run is logged with the directory it ran from, must not hold the deploy lock (fd 9),
# and runs $STUB_DURING first: what someone else does while the checks run.  deploy.sh gives the checks only PATH,
# HOME and a few more, so they find the stubs' directory through HOME, and the test's settings (STUB_*) in its env file
REPO_FILES = {
    "scripts/test.sh": """#!/bin/sh
export STUB_DIR=$HOME/stub
[ ! -e "$STUB_DIR/env" ] || . "$STUB_DIR/env"
d=$(cd "$(dirname "$0")/.." && pwd)
echo "test.sh $d" >> "$STUB_DIR/ran.log"
[ $# = 0 ] || { echo "unexpected arguments: $*"; exit 3; }
[ ! -e /proc/$$/fd/9 ] || { echo "the deploy lock is open here"; exit 4; }
ls -l /proc/$$/fd/ | sed 's/^/test.sh /' >> "$STUB_DIR/fds.log"
env | grep -E '^(DRIFT_|HOST_UNITS_|TEST_SHELLCHECK=|STRATA_TEST_|TEST_REPO_ONLY=|STRATA_DIR=|STRATA_PY=|BASH_ENV=|PYTHONPATH=)' >> "$STUB_DIR/hooks.log"
env | grep -E '^(LC_|LANG=|LANGUAGE=)' | sed 's/^/test.sh /' >> "$STUB_DIR/locale.log"
echo "test.sh $(cat /proc/$$/oom_score_adj) $(ps -o ni= -p $$ | tr -d ' ')" >> "$STUB_DIR/oom.log"
[ -z "${STUB_DURING-}" ] || sh -c "$STUB_DURING"
# PERSON: what a person starts meanwhile, outside the check (its tree, its TMPDIR): test.sh says where deploy.sh's
# directory (the parent of TMPDIR) is, and goes on once the test has started that (deploy's person)
if [ -e "$d/PERSON" ]; then
  dirname "$TMPDIR" > "$STUB_DIR/person.new" && mv "$STUB_DIR/person.new" "$STUB_DIR/person.dir"
  n=0; while [ ! -e "$STUB_DIR/person.done" ] && [ $n -lt 1200 ]; do n=$((n + 1)); sleep 0.05; done
fi
# a process of its own session naming the TMPDIR, whose child names only the marker; on once the child is
if [ -e "$d/LEAK_TEST" ]; then
  setsid bash -c '(exec -a "$1-child" sleep 60) & echo > "$STUB_DIR/leak.ready"; wait' "$TMPDIR/$(cat "$d/LEAK_TEST")" \
    "$(cat "$d/LEAK_TEST")" > /dev/null 2>&1 &
  while [ ! -e "$STUB_DIR/leak.ready" ]; do sleep 0.05; done
fi
# one whose first argument after its code has control characters (it names the TMPDIR after it)
if [ -e "$d/CTRL_LEAK" ]; then
  setsid python3 -c 'import sys, time; open(sys.argv[2], "w").close(); time.sleep(60)' \
    "$(printf 'esc\\033]0;x\\007\\342\\200\\256VISIBLE')" "$TMPDIR/ctrl.ready" > /dev/null 2>&1 &
  while [ ! -e "$TMPDIR/ctrl.ready" ]; do sleep 0.05; done
fi
# GROUP_LEAK: one that the sweep finds (the checks' TMPDIR), leading a process group, with a member that dropped the
# TMPDIR and names nothing of the directory: that one goes with the group
if [ -e "$d/GROUP_LEAK" ]; then
  setsid sh -c '(unset TMPDIR; exec python3 -c "import time; time.sleep(60)" "$1") & echo > "$2"; wait' sh \
    "$(cat "$d/GROUP_LEAK")" "$STUB_DIR/group.ready" > /dev/null 2>&1 < /dev/null &
  n=0; while [ ! -e "$STUB_DIR/group.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
fi
# what strata-preflight.sh takes for an engine, under the TMPDIR
[ ! -e "$d/ENGINE_TEST" ] || { python3 -c 'import time; time.sleep(60)' "$TMPDIR/engine-cuda12/strata --serve" & sleep 1; }
# what a copy of deploy.sh with a harmless pattern (FAKE_ENGINE) and port (FAKE_PORT, FAKE_PORT6 on [::1]) takes for
# an engine or a listener: in a session of its own (it calls setsid itself, so that its command line is the same from
# its exec on), naming nothing of TMPDIR.  FAKE_LATE: as the check ends, once it runs.  FAKE_UNDER: with a TMPDIR
# under the checks' one
if [ -e "$d/FAKE_ENGINE" ]; then
  python3 -c 'import os, time; os.setsid(); time.sleep(60)' "$(cat "$d/FAKE_ENGINE")" > /dev/null 2>&1 < /dev/null &
  if [ -e "$d/FAKE_LATE" ]; then
    n=0
    until grep -q -a -F -e "$(cat "$d/FAKE_ENGINE")" "/proc/$!/cmdline" 2> /dev/null || [ $n -ge 200 ]; do
      n=$((n + 1)); sleep 0.01
    done
  else
    sleep 1
  fi
fi
# FAKE_LONG: as FAKE_ENGINE, with 70000 bytes of command line before the pattern
if [ -e "$d/FAKE_LONG" ]; then
  python3 -c 'import os, time; os.setsid(); time.sleep(60)' "$(head -c 70000 /dev/zero | tr '\\0' x)" "$(cat "$d/FAKE_LONG")" \
    > /dev/null 2>&1 < /dev/null &
  sleep 1
fi
# FAKE_CTRL: as FAKE_ENGINE, with control, format and separator characters in the argument before the pattern
if [ -e "$d/FAKE_CTRL" ]; then
  python3 -c 'import os, time; os.setsid(); time.sleep(60)' \\
    "$(printf 'esc\\033]0;x\\007\\342\\200\\256VISIBLE\\342\\200\\250')" "$(cat "$d/FAKE_CTRL")" \\
    > /dev/null 2>&1 < /dev/null &
  sleep 1
fi
if [ -e "$d/FAKE_UNDER" ]; then
  TMPDIR=$TMPDIR/sub python3 -c 'import os, time; os.setsid(); time.sleep(60)' "$(cat "$d/FAKE_UNDER")" > /dev/null 2>&1 < /dev/null &
  sleep 1
fi
if [ -e "$d/FAKE_PORT" ]; then
  python3 -c 'import os, socket, sys, time; os.setsid(); s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); time.sleep(60)' \
    "$(cat "$d/FAKE_PORT")" > /dev/null 2>&1 < /dev/null &
  sleep 1
fi
if [ -e "$d/FAKE_PORT6" ]; then
  python3 -c 'import os, socket, sys, time; os.setsid(); s = socket.socket(socket.AF_INET6); s.bind(("::1", int(sys.argv[1]))); s.listen(); time.sleep(60)' \
    "$(cat "$d/FAKE_PORT6")" > /dev/null 2>&1 < /dev/null &
  sleep 1
fi
# LATE_ENGINE, LATE_PORT: left in a session of its own and naming nothing of the directory, it becomes an engine or a
# listener on the port in the file 2 s after the check started it, after the watch's last look (LATE_PORT has a TMPDIR
# under the checks' one); until then its command line has the pattern in two parts
if [ -e "$d/LATE_ENGINE" ]; then
  p=$(cat "$d/LATE_ENGINE")
  setsid sh -c 'sleep 2; exec python3 -c "import time; time.sleep(60)" "$1$2"' sh "${p%%-engine*}" "-engine${p#*-engine}" \
    > /dev/null 2>&1 < /dev/null &
fi
if [ -e "$d/LATE_PORT" ]; then
  TMPDIR=$TMPDIR/sub setsid sh -c 'sleep 2; exec python3 -c "import socket, sys, time; s = socket.socket(); s.bind((sys.argv[2], int(sys.argv[1]))); s.listen(); time.sleep(60)" "$1" 127.0.0.1' \
    sh "$(cat "$d/LATE_PORT")" > /dev/null 2>&1 < /dev/null &
fi
# SCOPE_ENGINE: what a copy of deploy.sh takes for an engine, started in a scope of its own (systemd-run --user
# --scope), with the checks' TMPDIR, while the watch looks
if [ -e "$d/SCOPE_ENGINE" ]; then
  setsid systemd-run --user --scope -q python3 -c 'import time; time.sleep(60)' "$(cat "$d/SCOPE_ENGINE")" \
    > /dev/null 2>&1 < /dev/null &
  n=0
  while [ "$(cat /proc/$!/cgroup 2> /dev/null)" = "$(cat /proc/$$/cgroup)" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
  sleep 1
fi
# SCOPE_LATE: as LATE_ENGINE, in a scope of its own (systemd-run --user --scope, as AGENTS.md asks every job here to
# start), with the checks' TMPDIR; test.sh ends once it runs in that scope
if [ -e "$d/SCOPE_LATE" ]; then
  p=$(cat "$d/SCOPE_LATE")
  setsid systemd-run --user --scope -q sh -c 'sleep 2; exec python3 -c "import time; time.sleep(60)" "$1$2"' sh \
    "${p%%-engine*}" "-engine${p#*-engine}" > /dev/null 2>&1 < /dev/null &
  n=0
  while [ "$(cat /proc/$!/cgroup 2> /dev/null)" = "$(cat /proc/$$/cgroup)" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
fi
# LATE_EXEC: in a session of its own, naming nothing of the directory and without the checks' TMPDIR (so beyond the
# sweep), $STUB_DIR/late_exec.py waits for the sweep, and execs into a process that names the directory as the sweep
# sleeps after its first pass, before its second
if [ -e "$d/LATE_EXEC" ]; then
  (LATE_DIR=$(dirname "$TMPDIR"); export LATE_DIR; unset TMPDIR
   exec setsid python3 "$STUB_DIR/late_exec.py" "$(cat "$d/LATE_EXEC")" > /dev/null 2>&1 < /dev/null) &
  n=0; while [ ! -e "$STUB_DIR/late.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
fi
# RESPAWN: in a session of its own, naming nothing of the directory and without the checks' TMPDIR (so beyond the
# sweep), $STUB_DIR/respawn.py starts one that names the directory, and another each time that is killed
if [ -e "$d/RESPAWN" ]; then
  RESPAWN_DIR=$TMPDIR; export RESPAWN_DIR
  (unset TMPDIR; exec setsid python3 "$STUB_DIR/respawn.py" "$(cat "$d/RESPAWN")" > /dev/null 2>&1 < /dev/null) &
  n=0; while [ ! -e "$STUB_DIR/respawn.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
fi
# REAL_LEAK: one without the checks' TMPDIR that names the directory by its real path (deploy.sh's TMPDIR is a link)
if [ -e "$d/REAL_LEAK" ]; then
  real=$(cd -P "$(dirname "$TMPDIR")" && pwd)
  (unset TMPDIR; exec setsid python3 -c 'import sys, time; open(sys.argv[2], "w").close(); time.sleep(60)' "$real/x" \
    "$STUB_DIR/real.ready" "$(cat "$d/REAL_LEAK")" > /dev/null 2>&1 < /dev/null) &
  n=0; while [ ! -e "$STUB_DIR/real.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
fi
# WATCH_KILL: the check ends deploy.sh's watch (the watcher of the check's directory), as the OOM killer could
if [ -e "$d/WATCH_KILL" ]; then
  python3 - "$(dirname "$TMPDIR")" <<'EOF'
import os, signal, sys
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        env = open(f"/proc/{p}/environ", "rb").read().split(b"\\0")
    except OSError:
        continue
    if b"WATCH_DIR=" + os.fsencode(sys.argv[1]) in env and any(e.startswith(b"WATCH_PARENT=") for e in env):
        os.kill(int(p), signal.SIGKILL)
        open(os.environ["STUB_DIR"] + "/watch.killed", "w").write(p)
EOF
fi
# NO_RECORD: the check makes deploy.sh's directory read-only, then starts an engine that the watch kills, and makes it
# writable again; FORGED_RECORD: the check writes a line into the watch's file
if [ -e "$d/NO_RECORD" ]; then
  chmod a-w "$(dirname "$TMPDIR")"
  python3 -c 'import os, time; os.setsid(); time.sleep(60)' "$(cat "$d/NO_RECORD")" > /dev/null 2>&1 < /dev/null &
  sleep 1
  chmod u+w "$(dirname "$TMPDIR")"
fi
[ ! -e "$d/FORGED_RECORD" ] || echo "a line that the check wrote" > "$(dirname "$TMPDIR")/engine"
# RM_RECORD: an engine that the watch kills; then the check removes the watch's record of it
if [ -e "$d/RM_RECORD" ]; then
  python3 -c 'import os, time; os.setsid(); time.sleep(60)' "$(cat "$d/RM_RECORD")" > /dev/null 2>&1 < /dev/null &
  n=0; while [ ! -s "$(dirname "$TMPDIR")/engine" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done
  rm -f "$(dirname "$TMPDIR")/engine"
fi
[ ! -e "$d/SLOW_TEST" ] || { echo $$ > "$STUB_DIR/slow.pid"; sleep 30; }
# CLEAN_TEST: a slow check whose own clean-up at SIGTERM takes a second
if [ -e "$d/CLEAN_TEST" ]; then
  trap 'sleep 1; echo > "$STUB_DIR/cleaned"; exit 1' TERM
  echo $$ > "$STUB_DIR/slow.pid"; sleep 30 & wait
fi
[ ! -e "$d/FAIL_TEST" ] || { echo "FAILED"; exit 1; }
[ ! -e "$d/CTRL_TEST" ] || { printf 'red \\033[31m, reversed \\342\\200\\256, bell \\007\\n'; exit 1; }
[ ! -e "$d/QUIET_TEST" ] || exit 0
[ ! -e "$d/AFTER_TEST" ] || { echo "all checks passed"; echo "a line after it"; exit 0; }
echo "all checks passed"
""",
    "scripts/check-drift.sh": """#!/bin/sh
export STUB_DIR=$HOME/stub
[ ! -e "$STUB_DIR/env" ] || . "$STUB_DIR/env"
d=$(cd "$(dirname "$0")/.." && pwd)
echo "check-drift.sh $d $*" >> "$STUB_DIR/ran.log"
[ ! -e /proc/$$/fd/9 ] || { echo "the deploy lock is open here"; exit 4; }
ls -l /proc/$$/fd/ | sed 's/^/check-drift.sh /' >> "$STUB_DIR/fds.log"
env | grep -E '^(DRIFT_|HOST_UNITS_|TEST_SHELLCHECK=|STRATA_TEST_|TEST_REPO_ONLY=|STRATA_DIR=|STRATA_PY=|BASH_ENV=|PYTHONPATH=)' >> "$STUB_DIR/hooks.log"
env | grep -E '^(LC_|LANG=|LANGUAGE=)' | sed 's/^/check-drift.sh /' >> "$STUB_DIR/locale.log"
echo "check-drift.sh $(cat /proc/$$/oom_score_adj) $(ps -o ni= -p $$ | tr -d ' ')" >> "$STUB_DIR/oom.log"
[ "$d" != "${STUB_SLOW_DRIFT_IN-}" ] || { echo $$ > "$STUB_DIR/slow.pid"; sleep 30; }
[ ! -e "$d/SLOW_DRIFT" ] || { echo $$ > "$STUB_DIR/slow.pid"; sleep 30; }
# WAIT_DRIFT (a file of the commit) or STUB_WAIT_DRIFT_IN (this checkout): a check that ends once $STUB_DIR/end.check
# is there, at most 20 s after it started
if [ -e "$d/WAIT_DRIFT" ] || [ "$d" = "${STUB_WAIT_DRIFT_IN-}" ]; then
  n=0; while [ ! -e "$STUB_DIR/end.check" ] && [ $n -lt 400 ]; do n=$((n + 1)); sleep 0.05; done
fi
# STUB_DEAF_DRIFT_IN: a slow check that ignores SIGTERM (its timeout's too), so that only a SIGKILL ends it
if [ "$d" = "${STUB_DEAF_DRIFT_IN-}" ]; then
  trap '' TERM
  echo $$ > "$STUB_DIR/slow.pid"; sleep 30
fi
# STUB_DRIFT_LEAVES: run from that checkout, it leaves a process in a session of its own that names its TMPDIR, with
# $STUB_LEFT in its command line: something for the sweep after the check after the fast-forward
if [ "$d" = "${STUB_DRIFT_LEAVES-}" ]; then
  setsid python3 -c 'import time; time.sleep(60)' "$TMPDIR/left" "$STUB_LEFT" > /dev/null 2>&1 < /dev/null &
fi
# STUB_CLEAN_DRIFT_IN: a slow check whose own clean-up at SIGTERM takes a second, and leaves $STUB_DIR/cleaned
if [ "$d" = "${STUB_CLEAN_DRIFT_IN-}" ]; then
  trap 'sleep 1; echo > "$STUB_DIR/cleaned"; exit 1' TERM
  echo $$ > "$STUB_DIR/slow.pid"; sleep 30 & wait
fi
if [ -e "$d/DRIFT" ] || [ "$d" = "${STUB_DRIFT_IN-}" ]; then echo "DRIFT  /etc/systemd/system/x.service"; exit 1; fi
[ ! -e "$d/DRIFT_EXIT0" ] || echo "DRIFT  /etc/systemd/system/x.service"    # a check that lost its exit status
echo "no drift"
""",
    "README.md": "base\n",
}
# what RESPAWN leaves behind, from $STUB_DIR: it names nothing of the directory, writes its pid to respawn.pid, starts
# one process that names the directory (RESPAWN_DIR, the checks' TMPDIR) and, each time that is killed, another one
# ($STUB_RESPAWN_DELAY s later, when set), $STUB_RESPAWN_COUNT in all (2 when not set); respawn.<n> once the nth runs
RESPAWN = """import os, subprocess, sys, time
stub = os.environ["STUB_DIR"]
open(os.path.join(stub, "respawn.pid"), "w").write(str(os.getpid()))
for n in range(1, int(os.environ.get("STUB_RESPAWN_COUNT", "2")) + 1):
    if n > 1:
        time.sleep(float(os.environ.get("STUB_RESPAWN_DELAY", "0")))
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(61)",
                          os.environ["RESPAWN_DIR"] + f"/respawn-{n}", sys.argv[1]])
    open(os.path.join(stub, f"respawn.{n}"), "w").close()
    if n == 1:
        open(os.path.join(stub, "respawn.ready"), "w").close()
    c.wait()
time.sleep(60)
"""
# what LATE_EXEC leaves behind, from $STUB_DIR: it names nothing of deploy.sh's directory (LATE_DIR, in its
# environment only); it finds the sweep (WATCH_DIR without WATCH_PARENT in its environment) and, as soon as that sleeps
# after a pass (state S: with no notes to write, it waits nowhere else) or has ended, execs into a process that names
# the directory; late.ready once it looks, and it gives up after 60 s without a sweep
LATE_EXEC = """import os, sys, time
d = os.environ["LATE_DIR"]
open(os.path.join(os.environ["STUB_DIR"], "late.ready"), "w").close()
def sweep():
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            env = open(f"/proc/{p}/environ", "rb").read().split(bytes(1))
        except OSError:
            continue
        if b"WATCH_DIR=" + os.fsencode(d) in env and not any(e.startswith(b"WATCH_PARENT=") for e in env):
            return p
def state(p):
    try:
        return open(f"/proc/{p}/stat", "rb").read().rsplit(b")", 1)[1].split()[0]
    except (OSError, IndexError):
        return b"X"
end, p = time.monotonic() + 60, sweep()
while p is None:
    if time.monotonic() > end:
        sys.exit(1)
    time.sleep(0.005)
    p = sweep()
while state(p) not in (b"S", b"Z", b"X"):
    time.sleep(0.001)
os.execvp(sys.executable, [sys.executable, "-c", "import time; time.sleep(60)", d + "/late", sys.argv[1]])
"""
# what a check runs (STUB_DURING) to stop its own timeout, test.sh's parent
STOP_TIMEOUT = 'kill -STOP "$(ps -o ppid= -p $PPID | tr -d " ")"; echo > "$STUB_DIR/stopped"'
# what a check runs to stop deploy.sh's watch (the watcher of the directory argv[1]), once, or with "again" each time
# it runs again, for as long as it lives
STOP_WATCH = r"""import os, signal, sys, time
watch = None
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        env = open(f"/proc/{p}/environ", "rb").read().split(b"\0")
    except OSError:
        continue
    if b"WATCH_DIR=" + os.fsencode(sys.argv[1]) in env and any(e.startswith(b"WATCH_PARENT=") for e in env):
        watch = int(p)
os.kill(watch, signal.SIGSTOP)
open(os.environ["STUB_DIR"] + "/watch.stopped", "w").write(str(watch))
while sys.argv[2:] == ["again"]:
    time.sleep(0.001)
    try:
        os.kill(watch, signal.SIGSTOP)
    except ProcessLookupError:
        break
"""


def run(*args: str, env: dict, cwd: Path | None = None) -> str:
    return subprocess.run(args, env=env, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def isolated(root: Path) -> dict:
    """An environment in which git reads no configuration of this machine or of its user."""
    return {**CLEAN_ENV, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
            "HOME": str(root), "XDG_CONFIG_HOME": str(root / ".config")}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class DeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """One origin, work clone and live clone, copied for each test."""
        cls.template = Path(tempfile.mkdtemp(prefix="deploy-template-"))
        cls.addClassCleanup(shutil.rmtree, cls.template)
        env = isolated(cls.template)
        t = cls.template
        for name, text in STUBS.items():
            cls.write(t / "stub" / name, text, 0o755)
        run("git", "init", "-q", "--bare", "-b", "main", str(t / "origin.git"), env=env)
        run("git", "init", "-q", "-b", "main", str(t / "work"), env=env)
        for name, text in {"scripts/deploy.sh": DEPLOY.read_text(), **REPO_FILES}.items():
            cls.write(t / "work" / name, text, 0o755 if name.endswith(".sh") else 0o644)
        run("git", "-C", str(t / "work"), "add", "-A", env=env)
        run("git", "-C", str(t / "work"), "-c", "user.name=test", "-c", "user.email=", "commit", "-q", "-m", "base",
            env=env)
        run("git", "-C", str(t / "work"), "push", "-q", str(t / "origin.git"), "HEAD:main", env=env)
        run("git", "clone", "-q", str(t / "origin.git"), str(t / "live"), env=env)
        cls.base = run("git", "-C", str(t / "work"), "rev-parse", "HEAD", env=env)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="deploy-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copytree(self.template, self.root, symlinks=True, dirs_exist_ok=True)
        self.stub, self.origin, self.work, self.live = (self.root / d for d in ("stub", "origin.git", "work", "live"))
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.env = {**isolated(self.root), "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": "test",
                    "GIT_COMMITTER_EMAIL": "", "STUB_DIR": str(self.stub), "TMPDIR": str(self.tmpdir)}
        self.use_origin_url(URL)
        # what a copy of deploy.sh with a harmless watch looks for: a pattern of this test's own (and a port of its own,
        # deploy_with_a_harmless_watch), so that runs side by side, such as two scripts/test.sh, do not take each
        # other's fake engines for their own
        self.fake = self.new_fake()

    @staticmethod
    def write(path: Path, text: str, mode: int = 0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def git(self, repo: Path, *args: str) -> str:
        return run("git", "-C", str(repo), *args, env=self.env)

    def use_origin_url(self, url: str):
        """Give the live clone this origin URL; git still fetches from the bare repository."""
        self.git(self.live, "config", "remote.origin.url", url)
        self.git(self.live, "config", "--replace-all", f"url.{self.origin}.insteadOf", url)

    def commit(self, files: dict[str, str], message: str = "change", parent: str | None = None) -> str:
        """Commit the files in the work clone, on parent if given, and make that commit origin's main."""
        if parent:
            self.git(self.work, "reset", "-q", "--hard", parent)
        for name, text in files.items():
            self.write(self.work / name, text, 0o755 if name.endswith(".sh") else 0o644)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)
        self.git(self.work, "push", "-q", str(self.origin), "+HEAD:main")
        return self.git(self.work, "rev-parse", "HEAD")

    def runs(self, sha: str, *runs: dict, total: int | None = None, http_error: bool = False):
        """GitHub's answer for the workflow runs of sha: by default one passed push run on main; each dict overrides."""
        rows = [{"path": ".github/workflows/test.yml", "head_sha": sha, "head_branch": "main", "event": "push",
                 "status": "completed", "conclusion": "success", "run_number": n, **r}
                for n, r in enumerate(runs or ({},), 1)]
        data = {"total_count": len(rows) if total is None else total, "workflow_runs": rows}
        self.write(self.stub / "runs" / f"{sha}.json", json.dumps(data))
        if http_error:
            self.write(self.stub / "runs" / f"{sha}.http", "")

    def start(self, *args: str, **env: str) -> subprocess.Popen:
        for log in ("curl.log", "ran.log", "timeout.log", "states.n", "slow.pid", "hooks.log", "leak.ready", "merging",
                    "merged", "locale.log", "oom.log", "person.dir", "person.done", "group.ready", "cleaned", "during"):
            (self.stub / log).unlink(missing_ok=True)
        # what the checks need of the test's environment (STUB_*, and git's identity for what STUB_DURING commits)
        mine = {k: v for k, v in self.env.items() if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_"))}
        (self.stub / "env").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in {**mine, **env}.items()
                                               if k.startswith(("STUB_", "GIT_"))))
        path = f"{self.stub / 'bin'}{os.pathsep}{os.environ['PATH']}"
        p = subprocess.Popen([str(self.live / "scripts" / "deploy.sh"), *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env={**self.env, "PATH": path, **env},
                             start_new_session=True)          # its own process group, like a command on a terminal
        self.addCleanup(self.end, p)
        return p

    def end(self, p: subprocess.Popen):
        """After the test: SIGKILL to what is left of the deploy.sh it started, its process group (deploy.sh and its
        watch) and the rest of its session (a check and its timeout), so that none of it outlives the test."""
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        for _ in range(5):
            left = self.in_session(p.pid)
            for q in left:
                try:
                    os.kill(q, signal.SIGKILL)
                except OSError:
                    pass
            if not left:
                break
            time.sleep(0.1)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for f in (p.stdout, p.stderr):
            if f is not None:                       # None: on a terminal
                f.close()

    @staticmethod
    def in_session(sid: int) -> list[int]:
        """The processes of the session sid."""
        found = []
        for q in filter(str.isdigit, os.listdir("/proc")):
            try:
                if os.getsid(int(q)) == sid:
                    found.append(int(q))
            except OSError:
                continue
        return found

    def named(self, text: bytes) -> list[int]:
        """The processes whose command line holds text."""
        return [int(q) for q in os.listdir("/proc") if q.isdigit() and text in self.cmdline(q)]

    @staticmethod
    def new_fake() -> str:
        """A harmless engine pattern for the watch of a copy of deploy.sh (deploy_with_a_harmless_watch), unique."""
        return f"xq-fake-engine-{uuid.uuid4().hex[:16]} --xq"

    @staticmethod
    def free_port(family: int = socket.AF_INET, host: str = "127.0.0.1") -> int:
        with socket.socket(family) as s:
            s.bind((host, 0))
            return s.getsockname()[1]

    def watchers(self, parent: int) -> list[int]:
        """The watches (watch) of the deploy.sh with this pid."""
        found = []
        for q in filter(str.isdigit, os.listdir("/proc")):
            try:
                env = Path(f"/proc/{q}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"WATCH_PARENT={parent}".encode() in env:
                found.append(int(q))
        return found

    def deploy(self, *args: str, person=None, timeout: float = 120, **env: str) -> subprocess.CompletedProcess:
        """deploy.sh run to its end.  With person, a commit with PERSON: while its test.sh runs, this test calls
        person(dir), dir being deploy.sh's temporary directory, and test.sh goes on once it has returned.  So what
        person starts is outside the check (its process tree, its TMPDIR), as a person's processes would be."""
        p = self.start(*args, **env)
        if person is not None:
            deadline = time.monotonic() + 60
            while not (self.stub / "person.dir").exists():
                self.assertTrue(p.poll() is None and time.monotonic() < deadline, "test.sh did not say where it runs")
                time.sleep(0.02)
            person(Path((self.stub / "person.dir").read_text().strip()))
            (self.stub / "person.done").write_text("")
        stdout, stderr = p.communicate(timeout=timeout)
        self.assertEqual(list(self.tmpdir.iterdir()), [], "deploy.sh left files in TMPDIR")
        return subprocess.CompletedProcess(p.args, p.returncode, stdout, stderr)

    def outside(self, *args: str, ready: Path | None = None, executable: str | None = None) -> subprocess.Popen:
        """A process of a person, in a session of its own, with this test's environment (not the checks' TMPDIR), of
        executable when given (args[0] is then only its name); waits until it has made ready, when given.  Killed
        after the test."""
        q = subprocess.Popen(args, executable=executable, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (q.kill(), q.wait()))
        deadline = time.monotonic() + 20
        while ready is not None and not ready.exists():
            self.assertTrue(q.poll() is None and time.monotonic() < deadline, f"{args} did not start")
            time.sleep(0.02)
        return q

    def use_copy(self, changes: dict[str, str]) -> str:
        """Make this checkout's deploy.sh a copy with these changes (old text, which must occur once: new text), as
        the commit origin/main and this checkout are at; that commit."""
        text = DEPLOY.read_text()
        for old, new in changes.items():
            self.assertEqual(text.count(old), 1, old)
            text = text.replace(old, new)
        base = self.commit({"scripts/deploy.sh": text}, parent=self.base)
        self.git(self.live, "fetch", "-q", "origin")
        self.git(self.live, "reset", "-q", "--hard", "origin/main")
        return base

    def log(self, name: str) -> str:
        p = self.stub / name
        return p.read_text() if p.exists() else ""

    def head(self, ref: str = "HEAD") -> str:
        return self.git(self.live, "rev-parse", ref)

    def assertRefused(self, r: subprocess.CompletedProcess, reason: str, head: str):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"not deployed: {reason}", r.stderr)
        self.assertEqual(self.head("main"), head, "a refused deploy moved main")

    def test_up_to_date_asks_nobody(self):
        r = self.deploy("--apply", STUB_STATES="activating")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("up to date: "), r.stdout)
        self.assertEqual(self.log("curl.log") + self.log("ran.log") + self.log("states.n"), "")

    def test_dry_run_reports_and_apply_fast_forwards(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"dry run: scripts/deploy.sh --apply deploys {new[:7]}", r.stdout)
        self.assertEqual(self.head(), self.base)
        curl = self.log("curl.log")
        self.assertTrue(curl.startswith("-q "), curl)                      # first: curl reads no ~/.curlrc
        self.assertIn("--max-time 30 ", curl)
        self.assertIn(f"/repos/example/ops/actions/workflows/test.yml/runs?head_sha={new}&event=push&branch=main"
                      "&per_page=100", curl)
        ran = self.log("ran.log").splitlines()
        self.assertEqual([line.split()[0] for line in ran], ["test.sh", "check-drift.sh"])
        self.assertTrue(ran[1].endswith(" --candidate"), ran)           # the new commit's, from the exported tree
        self.assertNotIn(str(self.live), self.log("ran.log"), "the checks ran from this checkout, not the new commit")
        timeouts = self.log("timeout.log").splitlines()
        self.assertTrue(timeouts[0].startswith(f"-k 10 120 git {GIT_OPTS} fetch "), timeouts)
        self.assertTrue(timeouts[1].startswith("-k 10 1800 ") and timeouts[1].endswith("/scripts/test.sh"), timeouts)
        r = self.deploy("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"deployed {new[:7]}: change", r.stdout)
        self.assertNotIn("to run them now", r.stdout)
        self.assertEqual(self.head(), new)
        self.assertEqual(self.git(self.live, "status", "--porcelain"), "")
        self.assertIn(f"check-drift.sh {self.live.resolve()} \n", self.log("ran.log"))    # this checkout's, as itself
        self.assertEqual(self.deploy("--apply").stdout.split(":")[0], "up to date")

    def test_the_newest_run_decides(self):
        new = self.commit({"docs.md": "a\n"})
        for runs in ([{"conclusion": "failure"}, {}], [{}, {}],
                     [{"run_number": 7}, {"run_number": 3, "conclusion": "failure"}]):
            with self.subTest(runs=runs):
                self.runs(new, *runs)
                r = self.deploy()
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_refuses_without_a_passing_run(self):
        new = self.commit({"docs.md": "a\n"})
        cases = [([{"status": "queued", "conclusion": None}], "queued"),
                 ([{"status": "in_progress", "conclusion": None}], "in_progress"),
                 ([{}, {"status": "in_progress", "conclusion": None}], "in_progress")]
        cases += [([{"conclusion": c}], f"completed {c}") for c in ("failure", "cancelled", "skipped", "neutral")]
        cases += [([{key: value}], "missing") for key, value in (("path", ".github/workflows/other.yml"),
                  ("event", "pull_request"), ("head_branch", "other"), ("head_sha", "0" * 40))]
        for runs, state in cases:
            with self.subTest(state=state, runs=runs):
                self.runs(new, *runs)
                r = self.deploy("--apply")
                self.assertRefused(r, f"the newest run of test.yml for {new[:7]} is {state}", self.base)
                self.assertEqual(self.log("ran.log"), "", "the checks ran before GitHub said yes")
        self.runs(new, {}, total=101)
        self.assertRefused(self.deploy("--apply"), "the newest run of test.yml for "
                           f"{new[:7]} is unreadable: more runs than one page", self.base)
        self.write(self.stub / "runs" / f"{new}.json", "<html>")
        self.assertRefused(self.deploy("--apply"), f"the newest run of test.yml for {new[:7]} is unreadable", self.base)
        self.runs(new, http_error=True)
        self.assertRefused(self.deploy("--apply"), f"cannot read the runs of test.yml for {new[:7]}", self.base)
        (self.stub / "runs" / f"{new}.json").unlink()
        self.assertRefused(self.deploy("--apply"), f"cannot read the runs of test.yml for {new[:7]}", self.base)

    def test_refuses_commits_that_apply_outside_this_checkout(self):
        for path in ("systemd/strata-q3-deploy.timer", "config/profile.conf", "scripts/strata-health.sh",
                     "scripts/install-system-files.sh", "scripts/check-drift.sh", "scripts/host-units.sh",
                     "scripts/test.sh", "scripts/deploy.sh", "tests/harness_run.py", ".github/workflows/test.yml",
                     ".gitattributes", "tests/.gitattributes"):
            with self.subTest(path=path):
                self.runs(self.commit({path: "# new\n", "docs.md": path}, parent=self.base))
                r = self.deploy("--apply")
                self.assertRefused(r, f"the commits change {path}: deploy them by hand (docs/runbooks/deploy.md)",
                                   self.base)
                self.assertEqual(self.log("curl.log") + self.log("ran.log"), "")

    def test_kills_what_a_check_leaves_behind(self):
        # a process the new commit's test.sh left running in the TMPDIR deploy.sh gave it (a harness's sandbox lives
        # there): killed when the check ends, and named
        marker = f"deploy-leak-{os.getpid()}"
        new = self.commit({"docs.md": "a\n", "LEAK_TEST": marker})
        self.runs(new)
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("note   killed what a check left behind: bash -c (exec -a", r.stdout)
        self.assertIn("\nok     scripts/test.sh of ", r.stdout)                 # each note ends its line
        time.sleep(0.3)
        left = [p for p in os.listdir("/proc") if p.isdigit() and marker.encode() in self.cmdline(p)]
        self.assertEqual(left, [], "the leader or its child is left")

    def test_the_sweep_looks_once_more_after_a_first_pass_that_finds_nothing(self):
        # what the check leaves (beyond the sweep: no TMPDIR, naming nothing of the directory, a session of its own)
        # execs into one that names the directory as the sweep sleeps after its first pass, which so finds nothing:
        # the second look, 0.2 s after that pass, finds it
        marker = f"deploy-late-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.kill, lambda: self.named(marker.encode()))
        self.write(self.stub / "late_exec.py", LATE_EXEC)
        (self.stub / "late.ready").unlink(missing_ok=True)
        new = self.commit({"docs.md": "a\n", "LATE_EXEC": marker, "FAIL_TEST": ""})   # no later check sweeps after it
        self.runs(new)
        r = self.deploy()
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: exit 1", self.base)
        self.assertRegex(r.stdout,
                         r"note   killed what a check left behind: \S*python3 -c import time; time\.sleep\(60\) ")
        time.sleep(0.3)
        self.assertEqual(self.named(marker.encode()), [], "it outlived deploy.sh")

    def test_a_failing_check_is_swept_too_and_others_are_not(self):
        marker = f"deploy-leak-fail-{os.getpid()}"
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(self.tmpdir), marker],
                                     start_new_session=True)       # names the caller's TMPDIR, not deploy.sh's
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        new = self.commit({"docs.md": "a\n", "LEAK_TEST": marker, "FAIL_TEST": ""})
        self.runs(new)
        r = self.deploy()
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed", self.base)
        self.assertIn("note   killed what a check left behind", r.stdout)
        time.sleep(0.3)
        left = [p for p in os.listdir("/proc") if p.isdigit() and marker.encode() in self.cmdline(p)]
        self.assertEqual(left, [str(bystander.pid)])

    def test_what_it_prints_of_a_leftover_is_printable(self):
        new = self.commit({"docs.md": "a\n", "CTRL_LEAK": ""})
        self.runs(new)
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("note   killed what a check left behind: python3 -c import sys, time; open(sys.argv[2], \"w\").close(); "
                      "time.sleep(60) esc]0;xVISIBLE ", r.stdout)
        for c in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(c, r.stdout + r.stderr)

    def test_what_names_the_directory_and_what_only_looks_like_it(self):
        # processes of a person, started while the check runs, that name the directory of deploy.sh as a path (taken
        # for the check's: the directory is new), or only something like it
        kinds = {"prefix": "/x{t}/y", "longer": "{t}x/y", "option": "--dir={t}/y", "quoted": "cd '{t}/y'", "exact": "{t}",
                 "blank": "cd {t} && ls"}
        pids = {}

        def person(t: Path):
            for kind, arg in kinds.items():
                pids[kind] = self.outside(sys.executable, "-c", "import time; time.sleep(60)", arg.format(t=t)).pid
        new = self.commit({"docs.md": "a\n", "PERSON": ""})
        self.runs(new)
        r = self.deploy(person=person)
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(0.3)
        self.assertEqual({kind for kind, pid in pids.items() if self.alive(pid)}, {"prefix", "longer"}, r.stdout)

    def test_what_names_the_real_path_of_the_directory_is_swept(self):
        # deploy.sh's TMPDIR is a link: a process that the check leaves, without the checks' TMPDIR, and that names the
        # directory by its real path, is the check's too
        link = self.root / "tmp-link"
        link.symlink_to(self.tmpdir)
        marker = f"deploy-real-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.kill, lambda: self.named(marker.encode()))
        new = self.commit({"docs.md": "a\n", "REAL_LEAK": marker})
        self.runs(new)
        r = self.deploy(TMPDIR=str(link))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("note   killed what a check left behind: python3 -c import sys, time; open(sys.argv[2]", r.stdout)
        time.sleep(0.3)
        self.assertEqual(self.named(marker.encode()), [])

    def test_the_sweep_goes_on_until_a_pass_finds_nothing(self):
        # what the check leaves in a session of its own, naming nothing of the directory and without the checks'
        # TMPDIR (so beyond the sweep), starts one that names the directory, and a second one once that is killed:
        # the next pass kills the second.  Also when the second starts only 0.3 s later, after a pass that found
        # nothing: the sweep looks once more 0.2 s after such a pass (one in the middle of its exec has a command line
        # and an environment that read empty); and with a third 0.3 s after the second, when the passes that find
        # nothing are not in a row
        for delay, count in (("0", 2), ("0.3", 2), ("0.3", 3)):
            with self.subTest(delay=delay, count=count):
                marker = f"deploy-respawn-{uuid.uuid4().hex[:8]}"
                self.addCleanup(self.kill, lambda marker=marker: self.named(marker.encode()))
                self.write(self.stub / "respawn.py", RESPAWN)
                new = self.commit({"docs.md": marker, "RESPAWN": marker, "FAIL_TEST": ""},   # no later check sweeps
                                  parent=self.base)
                self.runs(new)
                r = self.deploy(STUB_RESPAWN_DELAY=delay, STUB_RESPAWN_COUNT=str(count))
                self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: exit 1", self.base)
                self.assertEqual(r.stdout.count(" -c import time; time.sleep(61) "), count, r.stdout)
                time.sleep(0.3)
                self.assertEqual(self.named(marker.encode()), [int(self.log("respawn.pid"))],
                                 "one that names the directory is left")

    def test_a_sweep_whose_passes_run_out_fails_the_check(self):
        # the check leaves one that starts another each time one is killed, more often than the sweep's 20 passes:
        # what it started may still run, so the check fails; before, the sweep ended with exit 0 after its passes
        marker = f"deploy-respawn-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.kill, lambda: self.named(marker.encode()))
        self.write(self.stub / "respawn.py", RESPAWN)
        new = self.commit({"docs.md": "a\n", "RESPAWN": marker})
        self.runs(new)
        r = self.deploy(STUB_RESPAWN_COUNT="25")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: the clean-up after the check did not finish "
                              "(exit 4): what it started may still run", self.base)

    def test_a_check_that_starts_what_the_preflight_takes_for_an_engine(self):
        # while it lives, a start of strata-q3 is refused: it runs only as a harness test does (decision 0006)
        harness_run.gate(self, ("setsid",))
        new = self.commit({"docs.md": "a\n", "ENGINE_TEST": ""})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: strata-preflight.sh would refuse a start of "
                              "strata-q3 while it ran: an engine (killed): python3 -c import time; time.sleep(60) ", self.base)
        self.assertIn("engine-cuda12/strata --serve", r.stderr)
        self.assertEqual([q for q in os.listdir("/proc") if q.isdigit() and b"strata --serve" in self.cmdline(q)
                          and str(self.tmpdir).encode() in self.cmdline(q)], [])

    def test_the_checks_test_hooks_do_not_pass(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        hooks = {"DRIFT_SYSROOT": "/x", "DRIFT_OWNER": "1:1", "DRIFT_ENGINE_RE": "x", "DRIFT_URL": "http://x",
                 "DRIFT_TEST_SPEC": "/x", "HOST_UNITS_CONF": "/x", "HOST_UNITS_RUN_USER": "/x", "TEST_SHELLCHECK": "x",
                 "STRATA_DIR": "/x", "STRATA_TEST_HARNESSES": "1", "STRATA_TEST_NO_SERVICE": "1", "TEST_REPO_ONLY": "1",
                 "STRATA_PY": "/x", "BASH_ENV": "/x", "PYTHONPATH": "/x"}
        r = self.deploy("--apply", **hooks)
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertEqual(self.log("ran.log").count("check-drift.sh"), 2)
        self.assertEqual(self.log("hooks.log"), "")

    def test_the_checks_do_not_get_this_scripts_stderr(self):
        # deploy.sh keeps a copy of its stderr for stop() (pause): the checks and what they start do not get it, so
        # they cannot write past their logs to the terminal (here a pipe to this test, whose inode the stubs' open
        # descriptors must not show)
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply")
        pipe = f"pipe:[{os.fstat(p.stderr.fileno()).st_ino}]"
        stdout, stderr = p.communicate(timeout=120)
        self.assertEqual((p.returncode, self.head()), (0, new), stdout + stderr)
        fds = self.log("fds.log")
        self.assertEqual(sorted({line.split()[0] for line in fds.splitlines()}), ["check-drift.sh", "test.sh"], fds)
        self.assertNotIn(pipe, fds)

    def test_the_checks_get_the_locale(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy(LANG="C.UTF-8", LANGUAGE="en", LC_MESSAGES="C", LC_TIME="POSIX")
        self.assertEqual(r.returncode, 0, r.stderr)
        for script in ("test.sh", "check-drift.sh"):
            for var in ("LANG=C.UTF-8", "LANGUAGE=en", "LC_MESSAGES=C", "LC_TIME=POSIX"):
                self.assertIn(f"{script} {var}\n", self.log("locale.log"))

    def test_the_checks_are_the_first_for_the_oom_killer_and_the_last_for_the_cpu(self):
        # at the account's memory limit (decision 0007), the OOM killer takes a check's processes before the
        # maintainer's shell or tools: choom gives them oom_score_adj 1000; and they run at nice 19, behind the
        # service.  The command line too, since this test may run at 1000 and 19 already (in a check of deploy.sh)
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertEqual(self.log("oom.log").splitlines(),
                         ["test.sh 1000 19", "check-drift.sh 1000 19", "check-drift.sh 1000 19"])
        checks = self.log("timeout.log").splitlines()[1:]
        self.assertEqual([line.split(" env -i ")[0] for line in checks],
                         ["-k 10 1800 nice -n 19 choom -n 1000 --"] * 3, checks)

    def test_an_interrupt_after_the_fast_forward(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply", STUB_SLOW_DRIFT_IN=str(self.live.resolve()))
        deadline = time.monotonic() + 20
        while not (self.stub / "slow.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn(f"deployed {new[:7]}, but the check after it was interrupted: run scripts/check-drift.sh", stderr)
        self.assertNotIn("not deployed", stderr)
        self.assertEqual(self.head(), new)

    def test_an_interrupt_during_the_fast_forward(self):
        # git runs in a session of its own: a Ctrl-C while it updates the ref (its reference-transaction hook runs
        # before the ref moves) lets it finish, and a second one does not cut the clean-up short; then the result is
        # said once
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        self.write(self.live / ".git" / "hooks" / "reference-transaction",       # main's update, not the fetch's
                   f'#!/bin/sh\nrefs=$(cat)\n[ "$1" = prepared ] || exit 0\ncase "$refs" in *" refs/heads/main"*) ;; *) exit 0 ;; esac\n'
                   f'echo > "{self.stub}/merging"\nsleep 2\necho > "{self.stub}/merged"\n', 0o755)
        p = self.start("--apply")
        deadline = time.monotonic() + 30
        while not (self.stub / "merging").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.killpg(p.pid, signal.SIGINT)
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=30)
        self.assertEqual(stderr.count("deployed "), 1, stderr)
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn(f"deployed {new[:7]}, but the check after it was interrupted: run scripts/check-drift.sh", stderr)
        self.assertNotIn("not deployed", stderr)
        self.assertTrue((self.stub / "merged").exists(), "git was stopped, or not waited for")
        self.assertEqual(self.head(), new)
        self.assertEqual(self.git(self.live, "status", "--porcelain"), "")

    def test_an_interrupt_as_the_fast_forward_fails(self):
        # the merge fails (an ignored file of this checkout is in its way) and the signal comes before the refusal that
        # follows, as one during the merge does (bash runs the trap once git has ended): main did not move, so the run
        # says that the commit is not deployed, not that it is
        with open(self.live / ".git" / "info" / "exclude", "a") as f:
            f.write("notes.md\n")
        (self.live / "notes.md").write_text("kept here\n")
        for sig, to in (("INT", "group"), ("TERM", "self")):
            with self.subTest(sig=sig, to=to):
                r, new = self.interrupt_at(("--apply",), {"notes.md": "from the commit\n"},
                                           'refuse "git merge --ff-only $short failed"', "true", 1,
                                           self.signal_to(sig, to))
                self.assertRefused(r, "interrupted", self.base)
                self.assertNotIn(f"deployed {new[:7]}", r.stdout + r.stderr)
                self.assertEqual((self.live / "notes.md").read_text(), "kept here\n")

    def interrupt_just_after_the_start_of(self, assignment: str, first: str = "", **env: str):
        """deploy.sh --apply, with a Ctrl-C (SIGINT) after the fast-forward that comes when a background job has just
        started, before its $! is noted (assignment): a DEBUG trap from BASH_ENV sends it then, after the shell code
        first.  It ends at once, says that the commit is deployed, and leaves nothing of its session: what it started
        has ended before it does (stop() ends every job, not only those whose $! it has, and waits for them)."""
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        hit, hook = self.stub / "hit", self.root / "bash_env.sh"
        hook.write_text("set -T\n"
                        f"hook() {{ if [ \"$BASH_COMMAND\" = '{assignment}' ] && [ -n \"${{ff:-}}\" ] && [ ! -e '{hit}' ]; then\n"
                        f"  : > '{hit}'; {first}\n  kill -INT $$\nfi; }}\ntrap hook DEBUG\n")
        p = self.start("--apply", BASH_ENV=str(hook), **env)
        p.wait(timeout=20 + 2 * 15)
        left = [q for q in self.in_session(p.pid) if self.alive(q)] + self.watchers(p.pid)
        stdout, stderr = p.communicate(timeout=20)
        self.assertTrue(hit.exists(), "the hook did not run")
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertEqual(stderr.count("deployed "), 1, stderr)
        self.assertIn(f"deployed {new[:7]}, but the check after it was interrupted: run scripts/check-drift.sh", stderr)
        self.assertEqual(self.head(), new)
        self.assertEqual(left, [], "what deploy.sh started outlived it")

    def test_an_interrupt_when_the_watch_has_just_started(self):
        # stop() used to end only the jobs whose $! it had, and then to wait for every job (meant for the fast-forward):
        # here for the watch, which runs until its SIGTERM, so for good.  Without its SIGTERM from stop(), the watch
        # would end only after deploy.sh, once it sees that deploy.sh is gone
        self.interrupt_just_after_the_start_of("watcher=$!")

    def test_an_interrupt_when_a_check_has_just_started(self):
        # and here for the check, until it ended (30 s)
        self.interrupt_just_after_the_start_of("child=$!", STUB_SLOW_DRIFT_IN=str(self.live.resolve()))

    def test_an_interrupt_when_a_check_has_just_started_lets_it_clean_up(self):
        # the check runs, with its trap for SIGTERM, when the Ctrl-C comes, still before its $! is noted: stop() ends it
        # with SIGTERM and waits for it, as for a check whose $! it has; without that, only the sweep after it would
        # end it, with SIGKILL
        live = str(self.live.resolve())
        self.interrupt_just_after_the_start_of(
            "child=$!", first=f"n=0; while [ ! -e '{self.stub}/slow.pid' ] && [ $n -lt 400 ]; do n=$((n + 1)); sleep 0.05; done",
            STUB_CLEAN_DRIFT_IN=live)
        self.assertTrue((self.stub / "cleaned").exists(), "the check was killed before its clean-up ended")

    def deploy_with_a_harmless_watch(self, files: dict, port: int | None = None, unreadable: bool = False,
                                     slow: bool = False, sockets: str = "",
                                     person=None) -> tuple[subprocess.CompletedProcess, str]:
        """deploy.sh of a commit whose watch looks for this test's harmless pattern (self.fake) and a port of its own
        (port, or a free one) instead of the preflight's.  With unreadable, the watch reads the process of no socket;
        with slow, each of its scans takes a second more once it has listed the processes, as on a busy machine; with
        sockets "diag" or "proc", it reads the listening sockets only from sock_diag or only from /proc/net/tcp.  The
        commit is this checkout's, so that the copy runs; then the commit with the stub's files is checked, with person
        as deploy() has it."""
        port = self.free_port() if port is None else port
        scan = '    for p in filter(str.isdigit, os.listdir("/proc")):\n        if int(p) != me and engine.search('
        base = self.use_copy({'rb"engine-cuda12/strata --serve"': f'rb"{self.fake}"', "PORT = 8080 ": f"PORT = {port} ",
                              **({"def owner(inode):\n": "def owner(inode):\n    return None\n"} if unreadable else {}),
                              **({scan: '    listed = list(filter(str.isdigit, os.listdir("/proc")))\n    time.sleep(1)\n'
                                        '    for p in listed:\n        if int(p) != me and engine.search('} if slow else {}),
                              **({"    try:\n        return diagnosed()\n    except OSError:\n        pass\n":
                                  "    return diagnosed()\n"} if sockets == "diag" else {}),
                              **({"socket.SOCK_RAW, 4)": "socket.SOCK_RAW, 31)"} if sockets == "proc" else {})})
        new = self.commit({"docs.md": str(files), **files})
        self.runs(new)
        return self.deploy(person=person), base

    def refused_by_the_watch(self, r: subprocess.CompletedProcess, base: str, says: str):
        """test.sh's check failed for what the watch found, and says so; main did not move."""
        self.assertEqual((r.returncode, self.head("main")), (1, base), r.stdout + r.stderr)
        self.assertRegex(r.stderr, r"not deployed: scripts/test\.sh of [0-9a-f]{7,} failed: strata-preflight\.sh would "
                         r"refuse a start of strata-q3 while it ran: " + re.escape(says))

    def test_the_watch_kills_what_the_check_starts_and_fails_it(self):
        # what the preflight refuses a start for, started by the check in a session of its own and naming nothing of
        # TMPDIR, also with a TMPDIR under the checks' one, and as the check ends: killed, and the check fails.  Here
        # with a harmless pattern and port, which test_the_watch_looks_for_what_the_preflight_refuses_a_start_for ties
        # to the preflight's.  As the check ends: the watch's last look, a scan that starts after the check has ended,
        # sees it, also when the check ended during a scan whose list of processes is older than the engine (slow;
        # before, the watch ended after that scan)
        engine = "an engine (killed): python3 -c import os, time; os.setsid(); time.sleep(60) "
        listener = ("a listener on :8080 (killed): python3 -c import os, socket, sys, time; os.setsid(); "
                    "s = socket.socket();")
        for files, slow, says in (({"FAKE_ENGINE": "FAKE"}, False, engine), ({"FAKE_UNDER": "FAKE"}, False, engine),
                                  ({"FAKE_ENGINE": "FAKE", "FAKE_LATE": ""}, False, engine),
                                  ({"FAKE_ENGINE": "FAKE", "FAKE_LATE": ""}, True, engine),
                                  ({"FAKE_LONG": "FAKE"}, False, engine + "x" * 48),
                                  ({"FAKE_PORT": "PORT"}, False, listener)):
            with self.subTest(files=files, slow=slow):
                self.fake, port = self.new_fake(), self.free_port()      # what one leaves does not fail the next
                files = {k: {"FAKE": self.fake, "PORT": str(port)}.get(v, v) for k, v in files.items()}
                self.addCleanup(self.kill, lambda fake=self.fake, port=port:
                                self.named(fake.encode()) + self.with_argument(str(port)))
                r, base = self.deploy_with_a_harmless_watch(files, port, slow=slow)
                self.refused_by_the_watch(r, base, says)
                time.sleep(0.3)
                self.assertEqual(self.named(self.fake.encode()) + self.with_argument(str(port)), [])

    def test_the_watch_sees_a_listener_on_ipv6(self):
        # as the preflight's ss -ltn does
        try:
            port = self.free_port(socket.AF_INET6, "::1")
        except OSError:
            self.skipTest("machine: no IPv6 loopback (::1) here")
        self.addCleanup(self.kill, lambda: self.with_argument(str(port)))
        r, base = self.deploy_with_a_harmless_watch({"FAKE_PORT6": str(port)}, port)
        self.refused_by_the_watch(r, base, "a listener on :8080 (killed): python3 -c import os, socket, sys, time; "
                                           "os.setsid(); s = socket.socket(socket.AF_INET6);")
        time.sleep(0.3)
        self.assertEqual(self.with_argument(str(port)), [])

    def test_the_watch_reads_the_listeners_from_sock_diag_or_from_proc(self):
        # either source alone sees the listener that the check starts (sock_diag costs much less than /proc/net/tcp
        # here; the watch reads /proc/net/tcp when sock_diag fails)
        for sockets in ("diag", "proc"):
            with self.subTest(sockets=sockets):
                port = self.free_port()
                self.addCleanup(self.kill, lambda port=port: self.with_argument(str(port)))
                r, base = self.deploy_with_a_harmless_watch({"FAKE_PORT": str(port)}, port, sockets=sockets)
                self.refused_by_the_watch(r, base, "a listener on :8080 (killed): python3 -c import os, socket, sys, "
                                                   "time; os.setsid(); s = socket.socket();")

    def test_what_has_not_the_checks_tmpdir_fails_the_check_and_is_left_alone(self):
        # an engine that someone else starts meanwhile: it fails the check, but it is not the check's to kill, for the
        # watch nor for the sweep
        person = lambda t: self.outside(sys.executable, "-c", "import time; time.sleep(60)", self.fake)
        r, base = self.deploy_with_a_harmless_watch({"PERSON": ""}, person=person)
        self.refused_by_the_watch(r, base, "an engine: ")
        self.assertIn(" -c import time; time.sleep(60) xq-fake-engine-", r.stderr)
        self.assertEqual(len(self.named(self.fake.encode())), 1, "it was killed, or never started")

    def test_a_process_whose_environment_cannot_be_read_is_not_the_checks(self):
        # an engine that someone else starts meanwhile and that is not dumpable, as ssh-agent and the sshd of a login
        # are: its environment cannot be read, and so it is not taken for the check's, by the watch nor by the sweep.
        # It is not dumpable from its exec on, as a copy of python that it cannot read makes it (with prctl it would be
        # dumpable for a moment, in which the watch could look)
        python = self.root / "unreadable" / "python3"
        python.parent.mkdir()
        shutil.copy(os.path.realpath(sys.executable), python)
        python.chmod(0o111)
        ready = self.root / "undumpable.ready"
        undumpable = []

        def person(t: Path):
            undumpable.append(self.outside("nd-python", "-I", "-S", "-c", "import sys, time; open(sys.argv[1], 'w')."
                                           "close(); time.sleep(60)", str(ready), self.fake, ready=ready,
                                           executable=str(python)).pid)
            with self.assertRaises(PermissionError):
                Path(f"/proc/{undumpable[0]}/environ").read_bytes()
        r, base = self.deploy_with_a_harmless_watch({"PERSON": ""}, person=person)
        self.refused_by_the_watch(r, base, "an engine: ")
        self.assertIn("an engine: nd-python -I -S -c import sys, time;", r.stderr)
        time.sleep(0.3)
        self.assertTrue(self.alive(undumpable[0]), "it was killed")

    def test_a_listener_of_a_process_it_cannot_read_fails_the_check(self):
        # another user's listener on the port (root's docker-proxy, say): the preflight refuses a start for it too.
        # Here the copy's owner() reads no process, so a listener that someone else starts meanwhile stands for one;
        # it is not killed
        port = self.free_port()
        code = "import socket, sys, time; s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); " \
               "open(sys.argv[2], 'w').close(); time.sleep(60)"
        ready = self.root / "listener.ready"
        person = lambda t: self.outside(sys.executable, "-c", code, str(port), str(ready), ready=ready)
        r, base = self.deploy_with_a_harmless_watch({"PERSON": ""}, port, unreadable=True, person=person)
        self.refused_by_the_watch(r, base, "a listener on :8080, of a process this user cannot read")
        self.assertEqual(len(self.with_argument(str(port))), 1, "the listener was killed, or never started")

    def test_what_the_check_leaves_to_start_later_is_swept(self):
        # a process the check leaves in a session of its own, naming nothing of the directory, that becomes an engine
        # or a listener on the port only after the watch's last look: the clean-up after the check kills it, as the
        # check's (the checks' TMPDIR or one under it); before, it outlived deploy.sh
        for name in ("LATE_ENGINE", "LATE_PORT"):
            with self.subTest(name=name):
                self.fake, port = self.new_fake(), self.free_port()
                nonce = self.fake.split()[0].rpartition("-")[2]          # in both parts of the pattern
                self.addCleanup(self.kill, lambda nonce=nonce, port=port:
                                self.named(nonce.encode()) + self.with_argument(str(port)))
                what = self.fake if name == "LATE_ENGINE" else str(port)
                r, base = self.deploy_with_a_harmless_watch({name: what}, port)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("note   killed what a check left behind: sh -c sleep 2; exec python3 -c ", r.stdout)
                time.sleep(3)                       # it would have become one 2 s after it started
                self.assertEqual(self.named(nonce.encode()) + self.with_argument(str(port)), [],
                                 "it outlived deploy.sh")

    def test_what_the_check_moves_to_a_scope_of_its_own_is_the_checks(self):
        # what the check starts in a scope of its own with systemd-run --user --scope, as AGENTS.md asks every job here
        # to start: in another cgroup, with the checks' TMPDIR.  The watch kills it when it is an engine while the
        # check runs, and the sweep after the check when it becomes one only after the watch's last look; before, both
        # looked only in deploy.sh's cgroup, and it outlived deploy.sh.  Only where systemd is PID 1: systemd-run
        # --scope gives systemd its pid, which in a namespace of pids is another's
        try:
            init = Path("/proc/1/comm").read_text().strip()
        except OSError:
            init = ""
        if init != "systemd" or not shutil.which("systemd-run") or subprocess.run(
                ["systemd-run", "--user", "--scope", "-q", "true"], stdin=subprocess.DEVNULL,
                capture_output=True).returncode != 0:
            self.skipTest("machine: no systemd-run --user here")
        for name in ("SCOPE_ENGINE", "SCOPE_LATE"):
            with self.subTest(name=name):
                self.fake = self.new_fake()
                nonce = self.fake.split()[0].rpartition("-")[2]
                self.addCleanup(self.kill, lambda nonce=nonce: self.named(nonce.encode()))
                r, base = self.deploy_with_a_harmless_watch({name: self.fake})
                if name == "SCOPE_ENGINE":
                    self.refused_by_the_watch(r, base, "an engine (killed): ")
                    self.assertIn("python3 -c import time; time.sleep(60) xq-fake-engine-", r.stderr)
                else:
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertRegex(r.stdout, r"note   killed what a check left behind: \S*sh -c sleep 2; exec python3 -c ")
                    time.sleep(3)                       # it would have become one 2 s after it started
                self.assertEqual(self.named(nonce.encode()), [], "it outlived deploy.sh")

    def test_the_sweep_kills_the_process_group_of_what_it_finds(self):
        # the leader has the checks' TMPDIR; its child dropped it and names nothing of the directory
        marker = f"deploy-group-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.kill, lambda: self.named(marker.encode()))
        new = self.commit({"docs.md": "a\n", "GROUP_LEAK": marker})
        self.runs(new)
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.stub / "group.ready").exists())
        self.assertIn("note   killed what a check left behind: sh -c (unset TMPDIR;", r.stdout)
        time.sleep(0.3)
        self.assertEqual(self.named(marker.encode()), [], "the member of the group is left")

    def test_a_watch_that_does_not_finish_fails_the_check(self):
        # the check ends the watch, as the OOM killer could: what ran after that is not known
        new = self.commit({"docs.md": "a\n", "WATCH_KILL": ""})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertTrue((self.stub / "watch.killed").exists(), "the check found no watch to end")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: the watch did not finish (exit 137): what ran "
                              "meanwhile is not known", self.base)

    def test_what_the_watch_found_fails_the_check_when_its_record_is_gone(self):
        # its exit status says that it found something, also when the check removed $tmp/engine
        self.addCleanup(self.kill, lambda: self.named(self.fake.encode()))
        r, base = self.deploy_with_a_harmless_watch({"RM_RECORD": self.fake})
        self.refused_by_the_watch(r, base, "what the watch found (its record is missing)")
        time.sleep(0.3)
        self.assertEqual(self.named(self.fake.encode()), [])

    def test_the_watch_goes_on_when_it_cannot_write_its_record(self):
        # the check makes deploy.sh's directory read-only, then starts an engine: the watch kills it and says that it
        # found something; it does not end at the error
        self.addCleanup(self.kill, lambda: self.named(self.fake.encode()))
        r, base = self.deploy_with_a_harmless_watch({"NO_RECORD": self.fake})
        self.refused_by_the_watch(r, base, "what the watch found (its record is missing)")
        time.sleep(0.3)
        self.assertEqual(self.named(self.fake.encode()), [])

    def test_what_the_watch_found_is_printed_without_control_characters(self):
        # the watch's record names the command line of what it found, which the commit's check started, and refuse
        # prints it: no control or format (bidi) characters and no line separator, as in the sweep's notes
        self.addCleanup(self.kill, lambda: self.named(self.fake.encode()))
        r, base = self.deploy_with_a_harmless_watch({"FAKE_CTRL": self.fake})
        self.refused_by_the_watch(r, base, "an engine (killed): python3 -c import os, time; os.setsid(); "
                                           f"time.sleep(60) esc]0;xVISIBLE {self.fake} \n")
        for c in ("\x1b", "\x07", "‮", " "):
            self.assertNotIn(c, r.stdout + r.stderr)
        time.sleep(0.3)
        self.assertEqual(self.named(self.fake.encode()), [])

    def test_a_record_that_the_watch_did_not_write_fails_the_check_too(self):
        # one that the check wrote into the watch's file: nothing passes on it
        new = self.commit({"docs.md": "a\n", "FORGED_RECORD": ""})
        self.runs(new)
        self.assertRefused(self.deploy("--apply"), f"scripts/test.sh of {new[:7]} failed: strata-preflight.sh would "
                           "refuse a start of strata-q3 while it ran: a line that the check wrote", self.base)

    def test_the_watch_ends_when_deploy_sh_is_killed(self):
        # deploy.sh ends without its trap (SIGKILL): the watch looks once more and ends, instead of scanning /proc for
        # good
        new = self.commit({"SLOW_TEST": "", "docs.md": "a\n"})
        self.runs(new)
        p = self.start()
        deadline = time.monotonic() + 20
        while not (self.stub / "slow.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        watchers = self.watchers(p.pid)
        self.assertEqual(len(watchers), 1, watchers)
        self.assertEqual(Path(f"/proc/{watchers[0]}/stat").read_text().rpartition(")")[2].split()[16], "19",
                         "the watch does not run at nice 19")
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=10)
        deadline = time.monotonic() + 5
        while any(map(self.alive, watchers)) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([w for w in watchers if self.alive(w)], [], "the watch outlived deploy.sh")

    def test_the_watch_looks_for_what_the_preflight_refuses_a_start_for(self):
        preflight = (DEPLOY.parent / "strata-preflight.sh").read_text()
        pattern = re.search(r"pgrep -f '([^']*)'", preflight).group(1).replace("[e]", "e")
        port = int(re.search(r"sport = :(\d+)", preflight).group(1))
        deploy = DEPLOY.read_text()
        self.assertIn(f'engine = re.compile(rb"{pattern}")', deploy)
        self.assertIn(f"PORT = {port} ", deploy)

    @staticmethod
    def cmdline(pid: str) -> bytes:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return b""

    def with_argument(self, arg: str) -> list[int]:
        """The processes that have arg as one of their arguments."""
        return [int(q) for q in os.listdir("/proc") if q.isdigit() and arg.encode() in self.cmdline(q).split(b"\0")]

    @staticmethod
    def kill(pids):
        """For a clean-up: SIGKILL to the processes that pids() gives then."""
        for q in pids():
            try:
                os.kill(q, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def alive(pid: int) -> bool:
        """Whether the process runs: there, and not a zombie."""
        try:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
        except (OSError, IndexError):
            return False

    def test_refuses_local_changes(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        (self.live / "README.md").write_text("edited here\n")
        self.assertRefused(self.deploy("--apply"), "this checkout has local changes", self.base)
        self.git(self.live, "checkout", "--", "README.md")
        self.git(self.live, "config", "status.showUntrackedFiles", "no")
        (self.live / "notes.txt").write_text("untracked\n")
        self.assertRefused(self.deploy("--apply"), "this checkout has local changes", self.base)
        (self.live / "notes.txt").unlink()
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag=flag):
                self.git(self.live, "update-index", flag, "README.md")
                (self.live / "README.md").write_text("edited here, hidden\n")
                self.assertRefused(self.deploy("--apply"), "this checkout hides files from git status", self.base)
                self.git(self.live, "update-index", flag.replace("--", "--no-"), "README.md")
                self.git(self.live, "checkout", "--", "README.md")

    def test_refuses_when_the_checkout_changes_during_the_checks(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        live = self.live
        cases = [(f"git -C {live} checkout -q -b other", "this checkout is not on branch main", "other"),
                 (f"git -C {live} commit -q --allow-empty -m moved", "this checkout moved while the checks ran", ""),
                 (f"echo changed > {live}/README.md", "this checkout has local changes", "")]
        for during, reason, branch in cases:
            with self.subTest(during=during):
                r = self.deploy("--apply", STUB_DURING=during)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"not deployed: {reason}", r.stderr)
                self.assertNotEqual(self.head(), new)
                self.git(live, "checkout", "-q", "-f", "main")
                self.git(live, "reset", "-q", "--hard", self.base)
                if branch:
                    self.git(live, "branch", "-q", "-D", branch)

    def test_refuses_another_branch_or_a_detached_head(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        self.git(self.live, "checkout", "-q", "-b", "other")
        self.assertRefused(self.deploy("--apply"), "this checkout is not on branch main", self.base)
        self.git(self.live, "checkout", "-q", "--detach", "main")
        self.assertRefused(self.deploy("--apply"), "this checkout is not on branch main", self.base)

    def test_refuses_commits_that_origin_does_not_have(self):
        self.write(self.live / "local.md", "a commit made here\n")
        self.git(self.live, "add", "local.md")
        self.git(self.live, "commit", "-q", "-m", "local")
        local = self.head()
        self.assertRefused(self.deploy("--apply"), "origin/main", local)          # ahead of origin/main
        self.runs(self.commit({"docs.md": "a\n"}))
        r = self.deploy("--apply")                                                 # and origin/main moved on
        self.assertRefused(r, "origin/main", local)
        self.assertIn("is not a fast-forward", r.stderr)

    def test_service_state(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        cases = [({"STUB_STATES": "activating"}, "activating"), ({"STUB_STATES": "reloading"}, "reloading"),
                 ({"STUB_STATES": "maintenance"}, "maintenance"), ({"STUB_STATES": ""}, "in an unknown state"),
                 ({"STUB_JOB": "42"}, "active, with job 42 queued"), ({"STUB_JOB_FAIL": "1"}, "in an unknown state"),
                 ({"STUB_STATE_FAIL": "1"}, "in an unknown state")]
        for env, shown in cases:
            with self.subTest(env=env):
                r = self.deploy("--apply", **env)
                self.assertRefused(r, f"strata-q3.service is {shown};", self.base)
                self.assertEqual(self.log("curl.log") + self.log("ran.log"), "")
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                r = self.deploy(STUB_STATES=state)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"ok     strata-q3.service is {state}", r.stdout)
        r = self.deploy("--apply", STUB_STATES="active activating")     # it starts while the checks run
        self.assertRefused(r, "strata-q3.service is activating;", self.base)
        self.assertIn("check-drift.sh", self.log("ran.log"))

    def test_refuses_a_commit_whose_checks_fail_here(self):
        cases = [({"FAIL_TEST": ""}, "scripts/test.sh", "exit 1", "  FAILED"),
                 ({"QUIET_TEST": ""}, "scripts/test.sh", 'exit 0, but no "all checks passed" at the end', ""),
                 ({"AFTER_TEST": ""}, "scripts/test.sh", 'exit 0, but no "all checks passed" at the end',
                  "  all checks passed\n  a line after it"),               # the last line only counts
                 ({"DRIFT": ""}, "scripts/check-drift.sh", "exit 1", "  DRIFT  /etc/systemd/system/x.service"),
                 ({"DRIFT_EXIT0": ""}, "scripts/check-drift.sh", "exit 0, but with DRIFT lines", "  DRIFT")]
        for files, script, why, shown in cases:
            with self.subTest(files=files):
                new = self.commit({**files, "docs.md": str(files)}, parent=self.base)
                self.runs(new)
                r = self.deploy("--apply")
                self.assertRefused(r, f"{script} of {new[:7]} failed: {why}", self.base)
                self.assertIn(shown, r.stdout)
                if script == "scripts/test.sh":
                    self.assertNotIn("check-drift.sh", self.log("ran.log"))
        self.runs(self.commit({"docs.md": "a\n"}, parent=self.base))
        for script in ("test.sh", "check-drift.sh"):
            with self.subTest(timeout=script):
                r = self.deploy("--apply", STUB_TIMEOUT_FOR=script)
                self.assertRefused(r, f"scripts/{script} of", self.base)
                self.assertIn("failed: it did not finish in 1800 s", r.stderr)

    def test_ctrl_c_stops_it_before_the_merge(self):
        new = self.commit({"SLOW_TEST": "", "docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply")
        deadline = time.monotonic() + 20
        while not (self.stub / "slow.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        slow = int((self.stub / "slow.pid").read_text())
        os.killpg(p.pid, signal.SIGINT)                 # what a Ctrl-C on the terminal sends
        stdout, stderr = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(list(self.tmpdir.iterdir()), [])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(slow, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the check of the new commit is still running")

    def test_ctrl_c_lets_the_check_end_as_it_does_at_sigterm(self):
        # stop() waits for the check's timeout, which waits for the check after their SIGTERM: the check's own clean-up
        # ends before the sweep, which would kill it (its command line names the exported tree)
        new = self.commit({"CLEAN_TEST": "", "docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply")
        deadline = time.monotonic() + 20
        while not (self.stub / "slow.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=20)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertTrue((self.stub / "cleaned").exists(), "the check was killed before its clean-up ended")

    def test_ctrl_c_kills_what_the_check_left_behind(self):
        marker = f"deploy-leak-int-{os.getpid()}"
        new = self.commit({"SLOW_TEST": "", "LEAK_TEST": marker, "docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply")
        deadline = time.monotonic() + 20
        while not (self.stub / "slow.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=15)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertIn("note   killed what a check left behind: bash -c (exec -a", stdout)
        time.sleep(0.3)
        self.assertEqual([q for q in os.listdir("/proc") if q.isdigit() and marker.encode() in self.cmdline(q)], [])

    def wait_for(self, path: Path, p: subprocess.Popen, seconds: float = 20):
        deadline = time.monotonic() + seconds
        while not path.exists():
            self.assertTrue(p.poll() is None and time.monotonic() < deadline, f"no {path.name}")
            time.sleep(0.02)

    def test_a_ctrl_c_ends_it_when_a_check_has_stopped_its_timeout(self):
        # the check stops its timeout (SIGSTOP) and ends: a Ctrl-C ends deploy.sh within seconds, and the timeout too
        # (SIGTERM, then SIGCONT); before, stop() waited for the stopped timeout for good, deaf to further signals
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply", STUB_DURING=STOP_TIMEOUT)
        self.wait_for(self.stub / "stopped", p)
        time.sleep(0.5)
        t0 = time.monotonic()
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=20)
        self.assertLess(time.monotonic() - t0, 10, "it waited for the SIGKILL after GRACE s")
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertEqual([q for q in self.in_session(p.pid) if self.alive(q)], [], "what deploy.sh started outlived it")

    def test_a_check_that_stops_its_timeout_is_ended_past_its_time(self):
        # without a Ctrl-C: SLACK s past the time limit, deploy.sh ends the timeout itself, and the check fails as one
        # that did not finish; before, it waited for good.  Here in a copy with a limit of 3 s, a SLACK of 2 s and a
        # GRACE of 2 s
        base = self.use_copy({"LIMIT=1800 ": "LIMIT=3 ", "SLACK=30 ": "SLACK=2 ", "GRACE=15 ": "GRACE=2 "})
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        t0 = time.monotonic()
        r = self.deploy("--apply", STUB_DURING=STOP_TIMEOUT, timeout=60)
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: it did not finish in 3 s", base)
        self.assertLess(time.monotonic() - t0, 30)

    def test_a_check_that_stops_the_watch(self):
        # the watch, stopped by the check, runs again at the SIGCONT after its SIGTERM, looks once more and ends; the
        # check passes.  Before, unwatch waited for it for good.  While it was stopped it did not look: a check does
        # that only on purpose
        self.write(self.stub / "stop_watch.py", STOP_WATCH)
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", STUB_DURING='python3 "$STUB_DIR/stop_watch.py" "$(dirname "$TMPDIR")"', timeout=60)
        self.assertTrue((self.stub / "watch.stopped").exists(), "the check found no watch to stop")
        self.assertEqual((r.returncode, self.head()), (0, new), r.stdout + r.stderr)

    def test_a_check_that_keeps_the_watch_stopped_fails(self):
        # it stops the watch again whenever it runs: GRACE s after its SIGTERM (here 2 s, in a copy) the watch is
        # killed, and the check fails as for a watch that did not finish
        base = self.use_copy({"GRACE=15 ": "GRACE=2 "})
        self.write(self.stub / "stop_watch.py", STOP_WATCH)
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        during = ('python3 "$STUB_DIR/stop_watch.py" "$(dirname "$TMPDIR")" again > /dev/null 2>&1 & n=0; '
                  'while [ ! -e "$STUB_DIR/watch.stopped" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done')
        r = self.deploy("--apply", STUB_DURING=during, timeout=60)
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: the watch did not finish (exit 137): what ran "
                              "meanwhile is not known", base)

    def test_a_ctrl_c_ends_it_while_the_watch_is_stopped(self):
        self.write(self.stub / "stop_watch.py", STOP_WATCH)
        new = self.commit({"SLOW_TEST": "", "docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply", STUB_DURING='python3 "$STUB_DIR/stop_watch.py" "$(dirname "$TMPDIR")"')
        self.wait_for(self.stub / "slow.pid", p)
        self.assertTrue((self.stub / "watch.stopped").exists(), "the check found no watch to stop")
        t0 = time.monotonic()
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=30)
        self.assertLess(time.monotonic() - t0, 10, "it waited for the SIGKILL after GRACE s")
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertEqual([q for q in self.in_session(p.pid) if self.alive(q)], [], "what deploy.sh started outlived it")

    def test_a_check_whose_output_cannot_be_read_fails(self):
        # the check passed, but grep cannot read its log for DRIFT lines (a stand-in grep fails with exit 2 there, as
        # for a log that cannot be read): it fails, it does not pass
        grep = shutil.which("grep")
        body = (f'#!/bin/sh\ncase "$*" in "-q ^DRIFT "*.log) exit 2 ;; esac\nexec {shlex.quote(grep)} "$@"\n')
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", PATH=self.stand_in("grep", body))
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: its output cannot be read", self.base)

    def test_a_check_killed_before_its_time_limit(self):
        # exit 137 (SIGKILL) before the limit is not timeout's -k: the OOM killer at the account's memory limit, say
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", STUB_TIMEOUT_FOR="test.sh", STUB_TIMEOUT_RC="137")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: it was killed (SIGKILL) after ", self.base)
        self.assertRegex(r.stderr, r"after [01] s, for example by the OOM killer at the account's memory limit: see "
                                   rf"memory\.events of user-{os.getuid()}\.slice\n")

    def test_a_check_killed_at_its_time_limit(self):
        # and at or after it, timeout's -k: here in a copy with a limit of 2 s
        base = self.use_copy({"LIMIT=1800 ": "LIMIT=2 "})
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", STUB_TIMEOUT_FOR="test.sh", STUB_TIMEOUT_RC="137", STUB_TIMEOUT_LATE="1")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: it did not finish in 2 s\n", base)

    def test_an_interrupt_just_after_the_directory_is_made(self):
        # before, the EXIT trap that removes it was set only after mktemp: a Ctrl-C between the two left the directory
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        hit, hook = self.stub / "hit", self.root / "bash_env.sh"
        hook.write_text("set -T\n"
                        f"hook() {{ if [ -n \"${{tmp:-}}\" ] && [ ! -e '{hit}' ]; then : > '{hit}'; kill -INT $$; fi; }}\n"
                        "trap hook DEBUG\n")
        r = self.deploy("--apply", BASH_ENV=str(hook))          # deploy() checks that TMPDIR is empty
        self.assertTrue(hit.exists(), "the hook did not run")
        self.assertRefused(r, "interrupted", self.base)

    @staticmethod
    def signal_to(sig: str, to: str) -> str:
        """Shell code that sends sig (INT, TERM, HUP) to deploy.sh alone (to "self") or to its process group (to
        "group", as a terminal sends it), from deploy.sh."""
        return f"kill -{sig} -- {'-' if to == 'group' else ''}$$"

    def interrupt_at(self, args: tuple, files: dict | None, command: str | None, when: str, nth: int, send: str,
                     changes: dict | None = None, extra: str = "",
                     **env: str) -> tuple[subprocess.CompletedProcess, str]:
        """deploy.sh of a commit with these files (None: none, the run is up to date), which runs the shell code send
        (signal_to) at the nth DEBUG trap of command (None: any) while the condition when holds, and the shell code
        extra at each DEBUG trap; this checkout's deploy.sh with changes, when given, and env in its environment.  The
        run, and the commit."""
        base = self.use_copy(changes) if changes else self.base
        self.git(self.live, "reset", "-q", "--hard", base)
        new = base
        if files is not None:
            new = self.commit({"docs.md": f"{command} {nth} {send}", **files}, parent=base)
        else:
            self.git(self.work, "reset", "-q", "--hard", base)
            self.git(self.work, "push", "-q", str(self.origin), "+HEAD:main")
        self.runs(new)
        hit, hook = self.stub / "hit", self.root / "bash_env.sh"
        hit.unlink(missing_ok=True)
        match = "true" if command is None else f'[ "$BASH_COMMAND" = {shlex.quote(command)} ]'
        hook.write_text("set -T\nhook_n=0\n"
                        f"hook() {{ {extra}\n  if {match} && {when}; then\n"
                        f"  hook_n=$((hook_n + 1)); [ $hook_n != {nth} ] || {{ : > '{hit}'; {send}\n  }}\n"
                        "fi; }\ntrap hook DEBUG\n")
        r = self.deploy(*args, BASH_ENV=str(hook), **env)
        self.assertTrue(hit.exists(), "the hook did not run: " + r.stdout + r.stderr)
        return r, new

    def stand_in(self, name: str, body: str) -> str:
        """A PATH with a stand-in for the command name (a shell script of body), after the stubs' bin, which the stub
        timeout takes off PATH, and before the system's."""
        d = self.root / f"stand-in-{name}"
        self.write(d / name, body, 0o755)
        return f"{self.stub / 'bin'}{os.pathsep}{d}{os.pathsep}{os.environ['PATH']}"

    def test_an_interrupt_once_the_outcome_is_known(self):
        # an interrupt (SIGINT or SIGHUP to the group, as a terminal sends them, or SIGTERM to deploy.sh) once the
        # check after the fast-forward has ended (as the watch ends, at the check's verdict, at the script's exit, in
        # its EXIT trap), once a check has failed and why is known, at the end of a dry run, of a refusal or of an
        # up-to-date run (at its exit, in its EXIT trap): the outcome stands, and the directory goes.  Before: "deployed
        # ..., but the check after it was interrupted" or "not deployed: interrupted", exit 1, the directory left
        # when the signal came in the EXIT trap, and "the watch did not finish (exit 129)" when a SIGHUP came as the
        # watch ended.  The signal comes at the nth DEBUG trap of the command while the condition holds; in the EXIT
        # trap bash keeps BASH_COMMAND at the command that exited
        merged, made, why = '[ -n "${merged:-}" ]', '[ -n "${tmp:-}" ]', '[ -n "${why:-}" ]'
        deployed, dry = "deployed {}: change", "dry run: scripts/deploy.sh --apply deploys {}"
        refused = "not deployed: scripts/test.sh of {} failed: exit 1"
        by_hand = "not deployed: the commits change scripts/test.sh: deploy them by hand"
        fail, manual = {"FAIL_TEST": ""}, {"scripts/test.sh": "# changed\n"}
        cases = [(("--apply",), {}, "unwatch", merged, 1, "HUP", "group", 0, deployed),
                 (("--apply",), {}, "unwatch", merged, 1, "TERM", "group", 0, deployed),
                 (("--apply",), {}, "return 0", merged, 1, "INT", "group", 0, deployed),
                 (("--apply",), {}, "exit", merged, 1, "TERM", "self", 0, deployed),
                 (("--apply",), {}, "exit", merged, 2, "HUP", "group", 0, deployed),
                 ((), {}, "exit 0", made, 1, "TERM", "self", 0, dry),
                 ((), {}, "exit 0", made, 1, "HUP", "group", 0, dry),
                 ((), {}, "exit 0", made, 2, "INT", "group", 0, dry),
                 (("--apply",), fail, None, why, 1, "INT", "group", 1, refused),
                 (("--apply",), fail, None, why, 1, "TERM", "self", 1, refused),
                 (("--apply",), fail, None, why, 1, "HUP", "group", 1, refused),
                 (("--apply",), fail, "exit 1", why, 1, "TERM", "self", 1, refused),
                 (("--apply",), fail, "exit 1", why, 1, "HUP", "group", 1, refused),
                 (("--apply",), fail, "exit 1", why, 2, "INT", "group", 1, refused),
                 (("--apply",), manual, "exit 1", '[ -n "${manual:-}" ]', 1, "TERM", "self", 1, by_hand),
                 (("--apply",), manual, "exit 1", '[ -n "${manual:-}" ]', 1, "HUP", "group", 1, by_hand),
                 (("--apply",), None, "exit 0", "true", 1, "TERM", "self", 0, "up to date: "),
                 (("--apply",), None, "exit 0", "true", 1, "HUP", "group", 0, "up to date: "),
                 (("--apply",), None, "exit 0", "true", 2, "INT", "group", 0, "up to date: ")]
        for args, files, command, when, nth, sig, to, rc, says in cases:
            with self.subTest(args=args, files=files, command=command, nth=nth, sig=sig, to=to):
                r, new = self.interrupt_at(args, files, command, when, nth, self.signal_to(sig, to))
                self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
                self.assertNotIn("interrupted", r.stderr)
                self.assertNotIn("after it failed", r.stderr)
                self.assertIn(says.format(new[:7]), r.stdout + r.stderr)
                if files == fail:
                    self.assertIn("  FAILED", r.stdout)               # the check's output, all of it
                if not rc:
                    self.assertEqual(r.stderr, "")

    def test_an_interrupt_once_the_last_check_has_ended(self):
        # the last check decides the outcome (check-drift.sh --candidate in a dry run, the check after the fast-forward
        # with --apply): a signal once it has ended changes nothing, whether it passed or failed, also before the
        # script has seen it end.  So as reaped looks at the check (the hook waits there until the check's
        # timeout is gone), as limited notes the check's exit status, as check_of_target goes on, and in a dry run as
        # it says that the check passed and names what the service runs at its next start.  Before: "deployed ...,
        # but the check after it was interrupted" or "not deployed: interrupted", exit 1, for up to a second after the
        # check's end, and in a dry run until its last line.  An earlier check that has ended still counts as
        # interrupted, since the run would go on after it: the second list
        gone = ('n=0; while kill -0 "$p" 2> /dev/null && [ $n -lt 400 ]; do n=$((n + 1)); sleep 0.05; done; '
                f": > '{self.stub / 'gone'}'; ")
        napping = '[ "${FUNCNAME[1]-}" = reaped ] && [ -n "${child-}" ] && [ "${p-}" = "$child" ]'
        after, candidate = '[ -n "${merged:-}" ]', '[ -e "${tmp:-/nonexistent}/drift.log" ]'
        first = '[ -e "${tmp:-/nonexistent}/test.log" ] && [ ! -e "$tmp/drift.log" ]'
        deployed, dry = "deployed {}: change", "dry run: scripts/deploy.sh --apply deploys {}"
        diff = "changed=$(git diff --name-only \"$head\" \"$target\" -- \"${AT_START[@]}\" | tr '\\n' ' ')"
        drift = {"STUB_DRIFT_IN": str(self.live.resolve())}
        cases = [   # arguments, the commit's files, command, condition, signal, to, environment, exit status, says
            (("--apply",), {}, None, f"{napping} && {after}", "INT", "group", {}, 0, deployed),
            (("--apply",), {}, None, f"{napping} && {after}", "TERM", "self", drift, 1,
             "deployed {}, but scripts/check-drift.sh after it failed: exit 1"),
            ((), {}, None, f"{napping} && {candidate}", "HUP", "group", {}, 0, dry),
            ((), {"DRIFT": ""}, None, f"{napping} && {candidate}", "INT", "group", {}, 1,
             "not deployed: scripts/check-drift.sh of {} failed: exit 1"),
            (("--apply",), {}, "child=", after, "HUP", "group", {}, 0, deployed),
            (("--apply",), {}, "t=$((SECONDS - t))", after, "TERM", "self", {}, 0, deployed),
            ((), {}, "child=", candidate, "TERM", "self", {}, 0, dry),
            ((), {}, 'ok "scripts/check-drift.sh --candidate of $short: no drift"', "true", "INT", "group", {}, 0, dry),
            ((), {}, diff, "true", "HUP", "group", {}, 0, dry),
            ((), {}, "[ $apply = 0 ]", "true", "TERM", "self", {}, 0, dry)]
        for args, files, command, when, sig, to, env, rc, says in cases:
            with self.subTest(args=args, files=files, command=command, sig=sig, to=to, env=env):
                (self.stub / "gone").unlink(missing_ok=True)
                send = (gone if command is None else "") + self.signal_to(sig, to)
                r, new = self.interrupt_at(args, files, command, when, 1, send, **env)
                self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
                self.assertNotIn("interrupted", r.stderr)
                self.assertIn(says.format(new[:7]), r.stdout + r.stderr)
                self.assertEqual(self.head(), new if args else self.base)
                if command is None:
                    self.assertTrue((self.stub / "gone").exists(), "the check had not ended")
                if not rc:
                    self.assertEqual(r.stderr, "")
        last = 'check_of_target "$tmp/tree/scripts/check-drift.sh" "no drift" "$tmp/drift.log" --candidate'
        stopped = [(("--apply",), None, f"{napping} && {first}", "INT", "group"),
                   (("--apply",), None, f'{napping} && {candidate} && [ -z "${{merged:-}}" ]', "TERM", "self"),
                   ((), None, f"{napping} && {first}", "HUP", "group"),
                   (("--apply",), "child=", first, "INT", "group"),
                   (("--apply",), "[ $apply = 0 ]", "true", "TERM", "self"),
                   ((), last, "true", "INT", "group")]               # a dry run's last check has not started
        for args, command, when, sig, to in stopped:
            with self.subTest(args=args, command=command, when=when, sig=sig, to=to):
                (self.stub / "gone").unlink(missing_ok=True)
                send = (gone if command is None else "") + self.signal_to(sig, to)
                r, new = self.interrupt_at(args, {}, command, when, 1, send)
                self.assertRefused(r, "interrupted", self.base)
                if command is None:
                    self.assertTrue((self.stub / "gone").exists(), "the check had not ended")

    def test_a_signal_at_each_step_once_the_last_check_has_ended(self):
        # the nth command that the script runs once the check after the fast-forward has ended (the hook sees its
        # timeout gone), for each of the first 12 (before, the trap went off after the 12th): the commands that note
        # the end and turn the trap off leave no gap, in any order, in which a signal turns the check's result into an
        # interrupt.  The signal rotates
        ended = ('{ [ -n "${hook_end:-}" ] || { [ -n "${child-}" ] && [ -n "${merged:-}" ] && '
                 '! kill -0 "$child" 2> /dev/null && hook_end=1; }; }')
        for n in range(1, 13):
            sig, to = (("INT", "group"), ("TERM", "self"), ("HUP", "group"))[n % 3]
            with self.subTest(n=n, sig=sig, to=to):
                r, new = self.interrupt_at(("--apply",), {}, None, ended, n, self.signal_to(sig, to))
                self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout + r.stderr)
                self.assertIn(f"deployed {new[:7]}: change", r.stdout)

    def test_an_interrupt_once_the_last_check_is_past_its_time(self):
        # the check after the fast-forward ignores the SIGTERM of its timeout, which so has not ended it SLACK s past
        # its time (here in a copy with a limit of 6 s, a SLACK of 1 s and a GRACE of 2 s): that it did not finish is
        # the outcome then, also when a signal comes as the script ends the check
        short = {"LIMIT=1800 ": "LIMIT=6 ", "SLACK=30 ": "SLACK=1 ", "GRACE=15 ": "GRACE=2 "}
        r, new = self.interrupt_at(("--apply",), {}, 'end_jobs "$child"', '[ -n "${merged:-}" ]', 1,
                                   self.signal_to("INT", "group"), changes=short,
                                   STUB_DEAF_DRIFT_IN=str(self.live.resolve()))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"deployed {new[:7]}, but scripts/check-drift.sh after it failed: it did not finish in 6 s\n",
                      r.stderr)
        self.assertNotIn("interrupted", r.stderr)
        self.assertEqual(self.head(), new)

    def test_an_interrupt_while_the_last_check_of_a_dry_run_runs(self):
        # it has not ended, so it is interrupted, as the check after the fast-forward is
        # (test_an_interrupt_after_the_fast_forward)
        new = self.commit({"docs.md": "a\n", "SLOW_DRIFT": ""})
        self.runs(new)
        p = self.start()
        self.wait_for(self.stub / "slow.pid", p)
        slow = int((self.stub / "slow.pid").read_text())
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertNotIn("dry run: ", stdout)
        deadline = time.monotonic() + 5
        while self.alive(slow) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.alive(slow), "the check runs on")

    def test_a_second_signal_as_the_watch_ends_changes_nothing(self):
        # the watch acts on its first SIGTERM only, and ignores SIGHUP: another SIGTERM, or a SIGHUP, which can come
        # while Python exits (Python puts the default action back then for a signal that has a handler of its own),
        # does not end it with exit 143 or 129.  Here the copy's watch takes a second to exit once its loop has ended,
        # and the second signal to the group comes 0.5 s after a SIGTERM to the group, which comes once the check
        # after the fast-forward has ended
        linger = ("sys.exit(3 if found else 0)\n",
                  "class Linger:\n    def __init__(self, nap):\n        self.nap = nap\n    def __del__(self):\n"
                  "        self.nap(1)\nlinger = Linger(time.sleep)\nsys.exit(3 if found else 0)\n")
        for second in ("TERM", "HUP"):
            with self.subTest(second=second):
                then = f"kill -TERM -- -$$; ( sleep 0.5; kill -{second} -- -$$ ) > /dev/null 2>&1 < /dev/null 9>&- &"
                r, new = self.interrupt_at(("--apply",), {}, "unwatch", '[ -n "${merged:-}" ]', 1, then,
                                           changes={linger[0]: linger[1]})
                self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout + r.stderr)
                self.assertIn(f"deployed {new[:7]}: change", r.stdout)

    def test_an_interrupt_once_a_reason_before_the_check_is_known(self):
        # the check cannot start: the watch does not start (a stand-in python3 fails for it), or its TMPDIR cannot be
        # made (a stand-in mkdir fails for it).  A Ctrl-C once that reason is known does not replace it
        python = shlex.quote(sys.executable)
        cases = (("python3", f'#!/bin/sh\n[ -z "${{WATCH_PARENT-}}" ] || exit 9\nexec {python} "$@"\n',
                  "what strata-preflight.sh refuses a start for cannot be watched (the watch did not start)"),
                 ("mkdir", '#!/bin/sh\ncase "$*" in */run) exit 1 ;; esac\nexec /bin/mkdir "$@"\n',
                  "mkdir failed"))
        for name, body, why in cases:
            with self.subTest(name=name):
                r, new = self.interrupt_at(("--apply",), {}, None, '[ -n "${why:-}" ]', 1,
                                           self.signal_to("INT", "group"), PATH=self.stand_in(name, body))
                self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: {why}", self.base)
                self.assertNotIn("interrupted", r.stderr)

    def test_a_signal_from_the_terminal_does_not_cut_the_sweep_short(self):
        # Ctrl-\ (SIGQUIT) or Ctrl-C (SIGINT) to deploy.sh's process group while the sweep kills, one at a time, what a
        # leftover of the check starts: the sweep goes on to its end (before, it ended at once: at SIGQUIT what was
        # still to kill was left, at SIGINT with a traceback), and then a Ctrl-C ends the run.  The check fails, so
        # that no later sweep comes
        for sig, says in ((signal.SIGQUIT, "scripts/test.sh of {} failed: exit 1"), (signal.SIGINT, "interrupted")):
            with self.subTest(signal=sig.name):
                marker = f"deploy-{sig.name}-{uuid.uuid4().hex[:8]}"
                self.addCleanup(self.kill, lambda marker=marker: self.named(marker.encode()))
                self.write(self.stub / "respawn.py", RESPAWN)
                for n in range(1, 7):
                    (self.stub / f"respawn.{n}").unlink(missing_ok=True)
                new = self.commit({"docs.md": marker, "RESPAWN": marker, "FAIL_TEST": ""}, parent=self.base)
                self.runs(new)
                p = self.start(STUB_RESPAWN_COUNT="6")
                self.wait_for(self.stub / "respawn.2", p)       # the sweep has killed the first
                os.killpg(p.pid, sig)
                stdout, stderr = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 1, stdout + stderr)
                self.assertIn("not deployed: " + says.format(new[:7]), stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(stdout.count(" -c import time; time.sleep(61) "), 6, stdout)
                time.sleep(0.3)
                self.assertEqual(self.named(marker.encode()), [int(self.log("respawn.pid"))],
                                 "one that names the directory is left")

    def test_a_ctrl_backslash_while_a_check_runs_ends_nothing(self):
        # bash ignores SIGQUIT, and now what it runs in the foreground while it waits for the check does too
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        p = self.start(STUB_DURING='echo > "$STUB_DIR/during"; sleep 2')
        self.wait_for(self.stub / "during", p)
        time.sleep(0.5)
        os.killpg(p.pid, signal.SIGQUIT)
        stdout, stderr = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, stdout + stderr)
        self.assertIn(f"dry run: scripts/deploy.sh --apply deploys {new[:7]}", stdout)
        self.assertEqual(stderr, "")

    def test_a_sweep_that_does_not_finish_fails_the_check(self):
        # here python3 fails for the sweep (WATCH_DIR without WATCH_PARENT), as when something kills it: what the check
        # started may still run, so the check fails although it passed; after a Ctrl-C, it is said too
        fake = self.root / "pybin"
        self.write(fake / "python3", '#!/bin/sh\n[ -z "${WATCH_DIR-}" ] || [ -n "${WATCH_PARENT-}" ] || exit 9\n'
                                     f'exec {shlex.quote(sys.executable)} "$@"\n', 0o755)
        path = f"{self.stub / 'bin'}:{fake}:{os.environ['PATH']}"      # the timeout stub takes off only its own
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", PATH=path)
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed: the clean-up after the check did not finish "
                              "(exit 9): what it started may still run", self.base)
        new = self.commit({"SLOW_TEST": "", "docs.md": "b\n"})
        self.runs(new)
        p = self.start("--apply", PATH=path)
        self.wait_for(self.stub / "slow.pid", p)
        os.killpg(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn("note   the clean-up after the check did not finish (exit 9): what it started may still run\n"
                      "not deployed: interrupted\n", stderr)

    def test_a_sigterm_or_a_hangup_stops_it_as_a_ctrl_c_does(self):
        # SIGTERM to deploy.sh (kill) or to its process group, or SIGHUP to the group (a terminal that hangs up),
        # while a check runs: the check ends, and deploy.sh says that it was interrupted, as docs/runbooks/deploy.md
        # has it, and nothing else on stderr (before, a signal to the group ended the sleep of reaped's nap, and bash
        # said "Hangup" or "Terminated")
        for sig, to in (("TERM", "self"), ("HUP", "group"), ("TERM", "group")):
            with self.subTest(sig=sig, to=to):
                new = self.commit({"SLOW_TEST": "", "docs.md": f"{sig} {to}"}, parent=self.base)
                self.runs(new)
                p = self.start("--apply")
                self.wait_for(self.stub / "slow.pid", p)
                slow = int((self.stub / "slow.pid").read_text())
                sent = getattr(signal, f"SIG{sig}")
                (os.killpg if to == "group" else os.kill)(p.pid, sent)
                stdout, stderr = p.communicate(timeout=30)
                self.assertEqual((p.returncode, stderr), (1, "not deployed: interrupted\n"), stdout + stderr)
                self.assertEqual(self.head(), self.base)
                self.assertEqual(list(self.tmpdir.iterdir()), [])
                deadline = time.monotonic() + 5
                while self.alive(slow) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(self.alive(slow), "the check runs on")

    def test_a_sigterm_to_the_script_during_a_nap_of_the_last_check(self):
        # a SIGTERM to deploy.sh alone that comes 0.01 s after reaped's sixth nap began, while the last check runs
        # (after the fast-forward, and in a dry run); the check ends 0.5 s later.  bash runs the trap at the end of a
        # nap, and for the last check the naps last 0.1 s: stop() starts within about 0.1 s of the signal (here at
        # most 0.15 s), the check still runs then, so the signal ends it as a Ctrl-C does.  With the naps of an earlier
        # check, the sixth of a second, the trap would run only after the check had ended, and the run would say what
        # the check found; with naps of 0.2 s, stop() would start 0.19 s after the signal
        when = '[ "${FUNCNAME[1]-}" = reaped ] && [ -n "${child-}" ] && [ "${p-}" = "$child" ]'
        sent, started = self.stub / "sent", self.stub / "stop.started"
        send = ("setsid -f bash -c 'sleep 0.01; echo $EPOCHREALTIME > \"$3\"; kill -TERM \"$1\"; sleep 0.5; "
                f": > \"$2\"' bash $$ \"$STUB_DIR/end.check\" '{sent}' > /dev/null 2>&1 < /dev/null")
        extra = ('[ "${FUNCNAME[1]-}" != stop ] || [ -e ' + f"'{started}'" + ' ] || echo $EPOCHREALTIME > '
                 + f"'{started}'")
        live = str(self.live.resolve())
        for args, files, last, env in ((("--apply",), {}, '[ -n "${merged:-}" ]', {"STUB_WAIT_DRIFT_IN": live}),
                                       ((), {"WAIT_DRIFT": ""}, '[ -e "${tmp:-/nonexistent}/drift.log" ]', {})):
            with self.subTest(args=args):
                for f in (self.stub / "end.check", sent, started):
                    f.unlink(missing_ok=True)
                r, new = self.interrupt_at(args, files, "pause $nap", f"{when} && {last}", 6, send, extra=extra, **env)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                says = (f"deployed {new[:7]}, but the check after it was interrupted: run scripts/check-drift.sh\n"
                        if args else "not deployed: interrupted\n")
                self.assertEqual(r.stderr, says, r.stdout + r.stderr)
                self.assertNotIn("dry run: ", r.stdout)
                late = float(started.read_text()) - float(sent.read_text())
                self.assertLess(late, 0.15, f"stop() started {late:.3f} s after the SIGTERM")

    def test_a_second_signal_does_not_cut_the_clean_up_short(self):
        # after a Ctrl-C that came after the fast-forward, stop() says so; a SIGTERM to deploy.sh, or a SIGHUP to its
        # group, as it does (at its exit) does not start stop() again, which would say it twice.  In a trap, bash
        # keeps BASH_COMMAND at the command it interrupted: the hook finds the exit by its line in stop()
        live = str(self.live.resolve())
        said = 'but the check after it was interrupted: run scripts/check-drift.sh" >&2; exit 1'
        line = 1 + [n for n, text in enumerate(DEPLOY.read_text().splitlines()) if said in text][0]
        for sig, to in (("TERM", "self"), ("HUP", "group")):
            with self.subTest(sig=sig, to=to):
                self.git(self.live, "reset", "-q", "--hard", self.base)
                new = self.commit({"docs.md": f"{sig} {to}"}, parent=self.base)
                self.runs(new)
                hit, nap, hook = self.stub / "hit", self.stub / "nap", self.root / "bash_env.sh"
                for f in (hit, nap):
                    f.unlink(missing_ok=True)
                hook.write_text("set -T\nhook_n=0\nhook() {\n"           # the echo, then the exit, on that line
                                f'  if [ "${{FUNCNAME[1]-}}" = stop ] && [ "${{BASH_LINENO[0]}}" = {line} ]; then\n'
                                "    hook_n=$((hook_n + 1))\n"
                                f"    [ $hook_n != 2 ] || {{ : > '{hit}'; {self.signal_to(sig, to)}\n    }}\n"
                                "  fi\n"                  # a nap of that check, once it runs: a Ctrl-C 0.03 s into it
                                """  if [ "$BASH_COMMAND" = 'pause $nap' ] && [ "${nap-}" = 0.1 ] """
                                f"""&& [ -n "${{merged:-}}" ] && [ -e '{self.stub / "slow.pid"}' ] """
                                f"""&& [ ! -e '{nap}' ]; """
                                "then\n"
                                f"    : > '{nap}'; setsid -f bash -c 'sleep 0.03; kill -INT -- -\"$1\"' bash $$ "
                                "> /dev/null 2>&1 < /dev/null\n  fi\n}\ntrap hook DEBUG\n")
                p = self.start("--apply", BASH_ENV=str(hook), STUB_SLOW_DRIFT_IN=live)
                self.wait_for(self.stub / "slow.pid", p)
                # the Ctrl-C comes 0.03 s into a nap of reaped (0.1 s for this check), as bash waits for sleep: one
                # that comes as bash runs the hook (a DEBUG trap) runs stop() there, where bash runs no DEBUG trap, so
                # the hook would not see stop()'s exit
                self.wait_for(nap, p)
                stdout, stderr = p.communicate(timeout=30)
                self.assertTrue(hit.exists(), "the hook did not run")
                self.assertEqual(p.returncode, 1, stdout + stderr)
                self.assertEqual(stderr, f"deployed {new[:7]}, but the check after it was interrupted: run "
                                         "scripts/check-drift.sh\n")

    def test_an_interrupt_as_it_reads_its_arguments(self):
        # the traps are set before anything else in main: a Ctrl-C as it reads its arguments ends it as an interrupt,
        # exit 1 (before, the traps came after, and the signal ended it as SIGINT does, exit 130)
        hit, hook = self.stub / "hit", self.root / "bash_env.sh"
        hook.write_text("set -T\nhook() {\n  case $BASH_COMMAND in\n"
                        f'    "case "*) [ -e \'{hit}\' ] || {{ : > \'{hit}\'; kill -INT -- -$$; }} ;;\n'
                        "  esac\n}\ntrap hook DEBUG\n")
        r = self.deploy("--apply", BASH_ENV=str(hook))
        self.assertTrue(hit.exists(), "the hook did not run")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", "not deployed: interrupted\n"))

    def test_an_interrupt_once_it_has_found_bad_usage(self):
        # bad usage is an outcome: a signal as it says so, as it exits, or as its EXIT trap starts (the trap's command,
        # then the first line of finish(): in a trap, bash keeps BASH_COMMAND at the exit) changes nothing, exit 2 with
        # the usage line alone.  Before, stop() ran there and said "not deployed: interrupted", exit 1
        usage = "usage: scripts/deploy.sh [--apply]"
        for command, nth in ((f'echo "{usage}" 1>&2', 1), ("exit 2", 1), ("exit 2", 2), ("exit 2", 3)):
            for sig, to in (("INT", "group"), ("TERM", "self"), ("HUP", "group")):
                with self.subTest(command=command, nth=nth, sig=sig, to=to):
                    r, _ = self.interrupt_at(("--bogus",), None, command, "true", nth, self.signal_to(sig, to))
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (2, "", usage + "\n"))

    def test_an_interrupt_before_the_directory_is_made(self):
        # with nothing to clean up yet, the clean-up says nothing (before the check, there is no directory to sweep)
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        hit, hook = self.stub / "hit", self.root / "bash_env.sh"
        hook.write_text("set -T\n"
                        f"hook() {{ if [ \"$BASH_COMMAND\" = checkout_clean ] && [ ! -e '{hit}' ]; then\n"
                        f"  : > '{hit}'; kill -INT -- -$$\nfi; }}\ntrap hook DEBUG\n")
        r = self.deploy("--apply", BASH_ENV=str(hook))
        self.assertTrue(hit.exists(), "the hook did not run")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", "not deployed: interrupted\n"))

    def on_a_terminal(self, *args: str, stub_env: str = "", **env: str):
        """deploy.sh started on a pty that is the controlling terminal of its session, with the stubs' settings
        stub_env (their env file); the process, a function that reads what it wrote until a condition holds (so that it
        does not block), and one that hangs the terminal up: the master side closed, so that a write to the terminal
        fails (EIO), and SIGHUP to the group, as the login shell sends it to its jobs."""
        (self.stub / "env").write_text(stub_env)
        master, slave = os.openpty()
        hung_up = []
        self.addCleanup(lambda: hung_up or os.close(master))

        def terminal():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        path = f"{self.stub / 'bin'}{os.pathsep}{os.environ['PATH']}"
        p = subprocess.Popen([str(self.live / "scripts" / "deploy.sh"), *args], stdin=slave, stdout=slave,
                             stderr=slave, env={**self.env, "PATH": path, **env}, start_new_session=True,
                             preexec_fn=terminal)
        os.close(slave)
        self.addCleanup(self.end, p)
        os.set_blocking(master, False)

        def read_until(holds, what: str, seconds: float = 60):
            deadline = time.monotonic() + seconds
            while not holds():
                self.assertTrue(p.poll() is None and time.monotonic() < deadline, what)
                try:
                    os.read(master, 65536)
                except OSError:
                    pass
                time.sleep(0.05)

        def hang_up():
            os.close(master)
            hung_up.append(1)
            os.killpg(p.pid, signal.SIGHUP)
        return p, read_until, hang_up

    def test_the_clean_up_goes_on_when_the_terminal_hangs_up(self):
        # deploy.sh on a terminal that hangs up while a check runs, after the check left processes in sessions of their
        # own.  The sweep goes on killing when it cannot write its notes; before, it ended at its first note, and what
        # it had still to kill was left
        marker = f"hup-leak-{uuid.uuid4().hex[:8]}"
        late = f"hup-late-{uuid.uuid4().hex[:8]}-engine-x"
        mine = lambda: sorted(set(self.named(marker.encode()) + self.named(late.split("-engine")[0].encode())))
        self.addCleanup(self.kill, mine)
        new = self.commit({"docs.md": "hangup", "LEAK_TEST": marker, "LATE_ENGINE": late, "SLOW_TEST": ""})
        self.runs(new)
        for log in ("slow.pid", "leak.ready"):
            (self.stub / log).unlink(missing_ok=True)
        p, read_until, hang_up = self.on_a_terminal("--apply")
        read_until(lambda: (self.stub / "slow.pid").exists(), "no check runs")
        read_until(lambda: len(mine()) >= 3, "the check did not leave what it should", 10)
        self.assertEqual(len(mine()), 3)                    # the leak, its child, the late one
        hang_up()
        p.wait(timeout=90)
        self.assertEqual(p.returncode, 1)
        time.sleep(3)                                       # the late one would be an engine by now
        self.assertEqual(mine(), [], "what the check left outlived deploy.sh")

    def test_the_terminal_hangs_up_once_the_outcome_is_known(self):
        # the check after the fast-forward passes, and leaves a process for the sweep; as the watch is to end (the
        # trap is off: the outcome is known), the terminal hangs up.  The sweep kills the leftover, cannot write its
        # note, and the run ends as deployed, exit 0: the sweep wrote its notes unbuffered, so nothing is left in
        # Python's buffer for it to fail on at its exit (exit 120)
        marker = f"left-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.kill, lambda: self.named(marker.encode()))
        new = self.commit({"docs.md": "hangup after the outcome"})
        self.runs(new)
        hit, closed, hook = self.stub / "hit", self.stub / "closed", self.root / "bash_env.sh"
        for f in (hit, closed):
            f.unlink(missing_ok=True)
        hook.write_text("set -T\nhook() {\n"                # it waits there until the terminal has hung up
                        f"  if [ \"$BASH_COMMAND\" = unwatch ] && [ -n \"${{merged:-}}\" ] && [ ! -e '{hit}' ]; then\n"
                        f"    : > '{hit}'; while [ ! -e '{closed}' ]; do sleep 0.05; done\n  fi\n}}\ntrap hook DEBUG\n")
        settings = f"export STUB_DRIFT_LEAVES={shlex.quote(str(self.live.resolve()))} STUB_LEFT={marker}\n"
        p, read_until, hang_up = self.on_a_terminal("--apply", stub_env=settings, BASH_ENV=str(hook))
        read_until(hit.exists, "the hook did not run")
        self.assertEqual(len(self.named(marker.encode())), 1, "the check after the fast-forward left nothing")
        hang_up()
        closed.write_text("")
        p.wait(timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.head(), new)
        time.sleep(0.3)
        self.assertEqual(self.named(marker.encode()), [], "the leftover outlived deploy.sh")

    def which_python(self, then: str = "") -> str:
        """A PATH with a stand-in python3 that names, in $STUB_DIR/sigign.log, each of deploy.sh's three short Python
        programs it starts (printable, the runs parser, the sweep) with the signals it starts with ignored (SigIgn),
        runs the shell code then for them, and execs the real python3."""
        real = shlex.quote(shutil.which("python3"))
        return self.stand_in("python3", f"""#!/bin/bash
case "$*" in
  *unicodedata*sys.stdin*) w=printable ;;
  *runs.json*) w=parser ;;
  *) w=; [ -z "${{WATCH_DIR-}}" ] || [ -n "${{WATCH_PARENT-}}" ] || w=sweep ;;
esac
if [ -n "$w" ]; then
  echo "$w $(sed -n 's/^SigIgn:[[:space:]]*//p' /proc/$$/status)" >> "$STUB_DIR/sigign.log"
  {then}
fi
exec {real} "$@"
""")

    def test_its_python_programs_start_with_sigint_ignored(self):
        # printable, the runs parser and the sweep: Python sets no handler of its own for SIGINT then, so a Ctrl-C in
        # their first milliseconds prints no traceback (before, each set SIGINT's handler only after its imports)
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        (self.stub / "sigign.log").unlink(missing_ok=True)
        r = self.deploy(PATH=self.which_python())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        seen = [line.split() for line in self.log("sigign.log").splitlines()]
        self.assertEqual(sorted({w for w, mask in seen}), ["parser", "printable", "sweep"], seen)
        self.assertEqual([w for w, mask in seen if not int(mask, 16) & 1 << (signal.SIGINT - 1)], [], seen)

    def test_a_ctrl_c_as_its_python_programs_start(self):
        # a Ctrl-C (SIGINT to the process group) 0 to 0.03 s after printable, the runs parser or the sweep started:
        # the run says that it was interrupted, and nothing else is on stderr (no traceback)
        cases = [(w, d) for w in ("printable", "parser", "sweep") for d in ("0", "0.005", "0.01", "0.02", "0.03")]
        for w, d in cases:
            with self.subTest(program=w, after=d):
                new = self.commit({"docs.md": f"{w} {d}"}, parent=self.base)
                self.runs(new)
                for f in ("sigign.log", "int.sent"):
                    (self.stub / f).unlink(missing_ok=True)
                then = (f'[ "$w" != {w} ] || [ -e "$STUB_DIR/int.sent" ] || {{ : > "$STUB_DIR/int.sent"; '
                        f'g=$(ps -o pgid= -p $$ | tr -d " "); (sleep {d}; kill -INT -- -"$g") & }}')
                r = self.deploy(PATH=self.which_python(then))
                self.assertTrue((self.stub / "int.sent").exists(), f"no {w} started")
                self.assertEqual((r.returncode, r.stderr), (1, "not deployed: interrupted\n"), r.stdout + r.stderr)

    def test_a_signal_to_the_group_as_the_watch_starts(self):
        # watch() waits for the watch's start with naps of 0.05 s (pause): a SIGTERM or SIGHUP to the process group 0.02
        # s into one ends the run as an interrupt, with nothing on stderr but that (before pause, bash wrote
        # "Terminated" or "Hangup" for the sleep that the signal ended).  The watch's start is slowed (a stand-in
        # python3 sleeps a second first), so that the wait naps
        real = shlex.quote(shutil.which("python3"))
        path = self.stand_in("python3", f'#!/bin/sh\n[ -z "${{WATCH_PARENT-}}" ] || sleep 1\nexec {real} "$@"\n')
        for sig in ("TERM", "HUP"):
            with self.subTest(sig=sig):
                send = (f"setsid -f bash -c 'sleep 0.02; kill -{sig} -- -\"$1\"' bash $$ > /dev/null 2>&1 < /dev/null")
                r, new = self.interrupt_at(("--apply",), {}, "pause 0.05", '[ "${FUNCNAME[1]-}" = watch ]', 1, send,
                                           PATH=path)
                self.assertEqual((r.returncode, r.stderr), (1, "not deployed: interrupted\n"), r.stdout + r.stderr)

    def test_names_what_the_service_runs_at_its_next_start(self):
        new = self.commit({"scripts/strata-preflight.sh": "# changed\n", "scripts/strata-wait-ready.sh": "# changed\n",
                           "tools/configure_strata_q3.py": "# changed\n", "docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertIn("note   strata-q3.service runs these from this checkout at its next start: "
                      "scripts/strata-preflight.sh scripts/strata-wait-ready.sh tools/configure_strata_q3.py\n",
                      r.stdout)
        self.assertIn("to run them now: sudo systemctl restart strata-q3", r.stdout)
        self.runs(self.commit({"docs.md": "b\n"}))                             # only a document changes now
        r = self.deploy("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("next start", r.stdout)
        self.assertNotIn("to run them now", r.stdout)

    def test_reports_drift_that_appears_after_the_fast_forward(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", STUB_DRIFT_IN=str(self.live.resolve()))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"deployed {new[:7]}, but scripts/check-drift.sh after it failed: exit 1", r.stderr)
        self.assertEqual(self.head(), new)

    def test_keeps_ignored_files(self):
        self.runs(self.commit({"notes.md": "from the commit\n"}))
        with open(self.live / ".git" / "info" / "exclude", "a") as f:
            f.write("notes.md\n")
        (self.live / "notes.md").write_text("kept here\n")
        self.assertRefused(self.deploy("--apply"), "git merge --ff-only", self.base)
        self.assertEqual((self.live / "notes.md").read_text(), "kept here\n")

    def test_ignores_the_callers_git_variables(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        work_head = self.git(self.work, "rev-parse", "HEAD")
        r = self.deploy("--apply", GIT_DIR=str(self.work / ".git"), GIT_WORK_TREE=str(self.work),
                        GIT_INDEX_FILE=str(self.root / "index-of-a-hook"))
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), work_head)
        self.assertFalse((self.root / "index-of-a-hook").exists())

    def test_git_does_not_ask_for_credentials(self):
        # the fetch runs with GIT_TERMINAL_PROMPT=0, also when the caller's environment says otherwise: an origin that
        # asks for a password makes it fail, instead of waiting at the terminal (here a stand-in git notes the value)
        git = shutil.which("git")
        path = self.stand_in("git", '#!/bin/sh\ncase " $* " in *" fetch "*) echo "${GIT_TERMINAL_PROMPT-unset}" '
                                    f'>> "$STUB_DIR/prompt.log" ;; esac\nexec {shlex.quote(git)} "$@"\n')
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy(PATH=path, GIT_TERMINAL_PROMPT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.log("prompt.log"), "0\n")

    def test_prints_no_control_characters(self):
        # commit subjects (the commits ahead, "deployed", "up to date") and the output of a check that failed come from
        # other people: no control or format (bidi) characters, and no line or paragraph separators (U+2028, U+2029)
        hidden = ("\x1b", "\x9b", "\x07", "‮", " ", " ")
        new = self.commit({"docs.md": "a\n"},
                          message="subject \x1b[8mhidden\x1b[0m \x9b31m ‮reversed \x07end  line paragraph")
        self.runs(new)
        r = self.deploy("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hidden", r.stdout)
        self.assertIn("lineparagraph", r.stdout)
        up = self.deploy("--apply")
        self.assertTrue(up.stdout.startswith(f"up to date: {new[:7]} subject "), up.stdout)
        self.assertIn("lineparagraph", up.stdout)
        for c in hidden:
            self.assertNotIn(c, r.stdout + r.stderr + up.stdout + up.stderr)
        new = self.commit({"CTRL_TEST": ""})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed", self.head())
        self.assertIn("reversed", r.stdout)
        for c in hidden:
            self.assertNotIn(c, r.stdout)

    def test_origin_must_be_github_and_its_password_stays_hidden(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        self.use_origin_url("https://user:fixture-password@gitlab.com/example/ops.git")
        r = self.deploy("--apply")
        self.assertRefused(r, "origin is not a GitHub repository: https://gitlab.com/example/ops", self.base)
        self.assertNotIn("fixture-password", r.stdout + r.stderr)
        for url in ("https://user:fixture-password@github.com/example/ops.git", "https://github.com/example/ops/",
                    "ssh://git@github.com/example/ops.git", "git@github.com:example/ops", "https://GitHub.com/example/ops"):
            with self.subTest(url=url):
                self.use_origin_url(url)
                r = self.deploy()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("/repos/example/ops/actions/", self.log("curl.log"))
                self.assertNotIn("fixture-password", r.stdout + r.stderr)

    def test_refuses_when_git_or_the_lock_fails(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        with open(self.live / ".git" / "deploy.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            r = self.deploy("--apply")
            self.assertRefused(r, "another scripts/deploy.sh is running", self.base)
            self.assertEqual((r.stdout, self.log("curl.log")), ("", ""))
        self.assertRefused(self.deploy("--apply", STUB_TAR_FAIL="1"), "cannot export", self.base)
        (self.live / ".git" / "index.lock").write_text("")
        self.assertRefused(self.deploy("--apply"), "git merge --ff-only", self.base)
        (self.live / ".git" / "index.lock").unlink()
        index = (self.live / ".git" / "index").read_bytes()
        (self.live / ".git" / "index").write_bytes(b"not an index")
        self.assertRefused(self.deploy("--apply"), "git status failed", self.base)
        (self.live / ".git" / "index").write_bytes(index)
        shutil.rmtree(self.origin)
        self.assertRefused(self.deploy("--apply"), "git fetch origin main failed", self.base)

    def test_usage(self):
        for args in (["--force"], ["--apply", "--apply"], ["apply"]):
            with self.subTest(args=args):
                r = self.deploy(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("usage: scripts/deploy.sh [--apply]", r.stderr)
        self.assertEqual(self.log("curl.log"), "")


if __name__ == "__main__":
    unittest.main()
