#!/usr/bin/env bash
# Deploy origin/main to this checkout, the live deployment, when that needs no step from the maintainer.  It stops at
# the first of these checks that fails:
#   1. this checkout is on main with no local changes, and origin/main is a fast-forward of it
#   2. the commits change nothing that applies outside this checkout: systemd/, config/, scripts/strata-health.sh and
#      scripts/install-system-files.sh; nor the checks themselves, so that a commit cannot vouch for itself:
#      scripts/test.sh, scripts/check-drift.sh and scripts/host-units.sh, this script, tests/harness_run.py (which
#      decides whether a test may start fake engines here), .github/ (the workflow CI runs) and any .gitattributes
#      (git archive below follows it).  A commit's other tests and its tools are still checked by its own tests
#   3. strata-q3.service is not starting or stopping
#   4. the newest run of .github/workflows/test.yml for the push of that commit to main passed
#   5. scripts/test.sh and scripts/check-drift.sh --candidate of that commit pass here, run with nothing of this
#      script's environment but PATH, HOME, the locale, USER, LOGNAME, XDG_RUNTIME_DIR and TERM.  So even without
#      --apply, the script runs code of origin/main on this machine.  While they run, what strata-preflight.sh refuses
#      a start of strata-q3 for (a process with its engine pattern, a listener on :8080) fails the check, and when the
#      check started it (the checks' TMPDIR in its environment, in any cgroup), it is killed.  A check also takes
#      memory, which a start of strata-q3 waits for and the engine needs: this account's memory limit bounds it
#      (decision 0007), and the checks run with oom_score_adj 1000 (choom), so that at that limit the OOM killer takes
#      their processes first, before the maintainer's shell or tools.  The checks and the watch run at nice 19, so
#      that the service comes first for the CPU
# Commits that check 2 stops are deployed by hand (docs/runbooks/deploy.md, decision 0006).  With --apply it checks
# the checkout and the service again and fast-forwards.  It never runs sudo and restarts nothing: strata-q3.service
# runs the new preflight, wait-ready and tools/ at its next start.  The fast-forward runs in the foreground in a
# session of its own: a Ctrl-C does not reach git, and an interrupt is acted on once git has finished, saying whether
# the commit was deployed.  The waits for a check, for the fetch and for the watch are bounded, also when a check
# stopped what is waited for (SIGSTOP): what does not end is killed.  Once the outcome is known (bad usage; a refusal,
# also for a check that failed once its reason is known; up to date; and once the last check has ended, also before
# the script has seen it end, whether it passed or failed: check-drift.sh --candidate in a dry run, the check after the
# fast-forward with --apply), an interrupt (SIGINT, SIGTERM, SIGHUP; a Ctrl-\ ends nothing) no longer changes what the
# script says or its exit status.
# Limits: a check that stops (SIGSTOP) this script's own foreground processes, the sleep of a nap or the sweep's
# Python, hangs it until they run again (that takes intent); the checks, at nice 19, take longer while the machine is
# busy (LIMIT bounds them); a SIGTERM to this script alone takes effect at the end of the current nap of the wait for a
# check (a signal to its process group, as a Ctrl-C or a terminal that hangs up sends, at once): within a second while
# an earlier check runs, within 0.1 s while the last one does, so that a last check that ends within that 0.1 s is
# reported as it ended; a signal to the process group while a command runs in the foreground outside the waits for a
# check (git, curl, the sweep) can still make bash note on stderr that it ended that command, and one during the fetch
# can make git say why it stopped (as "fetch-pack: unexpected disconnect"); a fetch killed outright (SIGKILL: GRACE s
# after its SIGTERM, or the OOM killer) can leave git's .git/refs/remotes/origin/main.lock, and the next run then
# refuses ("git fetch origin main failed", after git's message, which names the file): remove it once no git runs in
# this checkout (this script does not, as another git may hold it; at SIGTERM, SIGINT and SIGHUP git removes its locks
# itself); and a signal in the moment between the end of the last check and bash's reaping of it (at its SIGCHLD)
# still counts as an interrupt of that check.
# Exit 0 when the checks pass (and with --apply, when deployed) or when up to date; 1 when it refuses or is interrupted,
# or when the check after the fast-forward fails; 2 on bad usage.  A signal in its first moments, before it has set its
# traps (main's first lines), ends it as the signal does (exit 130, 143, 129): it has made and locked nothing by then.
# Usage: scripts/deploy.sh [--apply]
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
UNIT=strata-q3.service
WORKFLOW=test.yml                                 # in .github/workflows/
MANUAL=(systemd/ config/ scripts/strata-health.sh scripts/install-system-files.sh scripts/test.sh scripts/check-drift.sh
        scripts/host-units.sh scripts/deploy.sh tests/harness_run.py .github/ ':(glob)**/.gitattributes')
# what the checks of the new commit get of this environment: a caller's test hooks (DRIFT_*, STRATA_TEST_HARNESSES ...)
# or BASH_ENV and PYTHONPATH must not change what they look at or run
KEEP=(PATH HOME LANG LANGUAGE USER LOGNAME XDG_RUNTIME_DIR TERM)
AT_START=(scripts/strata-preflight.sh scripts/strata-wait-ready.sh tools/)
LIMIT=1800                                        # seconds for each check of the new commit: a bound for one that
                                                  # hangs, above what scripts/test.sh takes here (1504 s on
                                                  # 2026-10-30: results/test-sh-20261030.txt)
SLACK=30                                          # seconds past the limit of a check or of the fetch after which this
                                                  # script ends it itself: its timeout (-k 10) has ended it by then,
                                                  # unless the timeout was stopped (SIGSTOP), say by the check
GRACE=15                                          # seconds a job of this script has after its SIGTERM, before SIGKILL
# no background gc that keeps the lock open after the script; file names quoted when they hold control characters
GIT_OPTS=(-c gc.auto=0 -c maintenance.auto=false -c core.quotePath=true)
tmp=
errfd=                                            # this script's stderr, kept for stop() (pause)
why=
child=
watcher=
ff=
merged=
# set for the last check, whose result is the outcome (check-drift.sh --candidate in a dry run, the check after the
# fast-forward): from its end on, an interrupt no longer changes that (limited, stop)
final=
target=
short=
status=

ok()     { printf 'ok     %s\n' "$1"; }
# says how the run ended: from here on an interrupt does not change that (the trap, stop, is off)
refuse() { trap '' INT TERM HUP; printf 'not deployed: %s\n' "$1" >&2; exit 1; }
git()    { command git "${GIT_OPTS[@]}" "$@"; }
# the EXIT trap, set before $tmp is made: it removes $tmp, and nothing cuts that short
finish() { trap '' INT TERM HUP; [ -z "${tmp:-}" ] || rm -rf -- "$tmp"; }
# sleeps $1 s in the foreground, with this script's stderr on /dev/null meanwhile.  A signal to the process group (a
# Ctrl-C, a terminal that hangs up, kill -- -PGID) ends the sleep too, and bash runs the trap at once; the note that
# bash writes about a command that a signal ended ("Hangup", "Terminated") goes to /dev/null.  bash runs the trap
# (stop) inside this redirection: stop() first puts stderr back ($errfd).  A signal to this script alone waits for the
# end of the nap.  Before: a sleep that ignored SIGHUP and SIGTERM still had them for a moment after its fork, and bash
# noted it when one ended it then; a sleep in the background cut short by wait made bash once lose the record of a
# later child ("wait_for: No record of process"); bash's read -t on a FIFO hung for good when a SIGTERM came at a bad
# moment (results of the probes in the round of this change)
pause() { { sleep "$1"; } 2> /dev/null; }
# whether this script's job $1 ends within $2 s, its exit status then in $status.  bash's wait cannot be bounded, so it
# polls: at first often, then each second; for the last check (final) each 0.1 s, so that a signal to this script
# alone, which acts at the end of a nap (pause), finds the check running when it came while it ran, but for one within
# 0.1 s of its end
reaped() {
  local p=$1 end=$((SECONDS + $2)) nap=0.02
  while kill -0 "$p" 2> /dev/null; do
    [ $SECONDS -lt $end ] || return 1
    pause $nap
    case $nap in 0.02) nap=0.05 ;; 0.05) nap=0.1 ;; 0.1) nap=0.2 ;; 0.2) nap=0.5 ;; *) nap=1 ;; esac
    [ -z "$final" ] || nap=0.1
  done
  wait "$p"; status=$?
}
# ends this script's jobs (pids): SIGTERM to each and to its process group, then SIGCONT, since one that is stopped
# (SIGSTOP; a check can do that to its timeout or to the watch) sees the SIGTERM only once it runs again, as timeout
# does it; SIGKILL to what is left of each $GRACE s later.  The exit status of the last one in $status (137 when it was
# killed); false when one outlives even its SIGKILL (in the kernel, uninterruptible)
end_jobs() {
  local p rc=0
  for p; do kill -TERM -- "-$p" "$p" 2> /dev/null; kill -CONT -- "-$p" "$p" 2> /dev/null; done
  for p; do
    reaped "$p" "$GRACE" && continue
    kill -KILL -- "-$p" "$p" 2> /dev/null
    reaped "$p" 5 || rc=1
  done
  return $rc
}
# what a check of the new commit left behind: every process whose command line names this script's temporary
# directory (the exported tree, and the TMPDIR the checks run with, where a harness's sandbox lives) as a path of its
# own (in an argument, at its start or after a blank, a quote, "=", ":" or ",", and not followed by more of a name:
# /tmp/tmp.abc, not /tmp/tmp.abcd or /x/tmp/tmp.abc), and every process that the watch takes for the check's (with the
# checks' TMPDIR or one under it in its environment, in any cgroup: also one that moved to a scope of its own with
# systemd-run --user --scope), each with its process group when it leads one (its children need not name the
# directory); passes until one finds nothing and another, 0.2 s later, finds nothing either (one in the middle of its
# exec has a command line and an environment that read empty), at most 20 passes.  A note names each process it
# kills.  Before, a check stopped by the time limit or Ctrl-C could leave them, and so could one that starts what
# strata-preflight.sh refuses a start for only after the watch's last look.  One of another user's is named, not
# killed, and one whose environment cannot be read (another user's, or one that is not dumpable, as ssh-agent and the
# sshd of a login are) counts only by its command line.  What it cannot find: a process that the check started without
# the checks' TMPDIR (env -u TMPDIR, or the environment of the user's service manager, as with systemd-run --user
# without --scope), in a session of its own and naming nothing of the directory; a check does that only on purpose, and
# code review is the gate for that.  The command lines come from the commit: printable only.  A Ctrl-C does not cut it
# short, nor a Ctrl-\ (main), nor a terminal that hung up (a note it cannot write is skipped); its exit status says
# whether it finished (4: the passes ran out while they still found something).  Python starts with SIGINT ignored
# (env), so that it never sets a handler of its own: with one, a Ctrl-C in its first milliseconds, before the program
# could ignore it, printed a traceback (KeyboardInterrupt)
sweep()  {
  [ -n "${tmp:-}" ] && [ -d "$tmp" ] || return 0
  WATCH_DIR=$tmp env --ignore-signal=INT python3 -I -S - <<'EOF'
import os, re, signal, sys, time, unicodedata
d = os.environ["WATCH_DIR"]
names = re.compile(rb"(?:^|[\s'\"=:,])(?:" + b"|".join(re.escape(os.fsencode(x)) for x in {d, os.path.realpath(d)})
                   + rb")(?=[/\s'\":,;)]|$)")
run = b"TMPDIR=" + os.fsencode(os.path.join(d, "run"))
def checks(pid):
    """Whether the watch takes the process for the check's: with the TMPDIR that the checks run with
    (TMPDIR=$tmp/run), or one under it, in its environment, in any cgroup.  Not one whose environment cannot be
    read."""
    try:
        env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    except OSError:
        return False
    return any(e == run or e.startswith(run + b"/") for e in env)
me, mine = os.getpid(), os.getpgrp()
refused = set()
def shown(cmd):
    line = cmd.replace(b"\0", b" ").decode(errors="replace")
    return "".join(c for c in line if unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))[:120]
def note(line):
    """A line on this script's output, unbuffered; one that cannot be written (a terminal that hung up: EIO) is
    skipped, and the killing goes on."""
    try:
        os.write(1, (line + "\n").encode(errors="replace"))
    except OSError:
        pass
quiet = 0
for _ in range(20):
    found = 0
    for p in filter(str.isdigit, os.listdir("/proc")):
        pid = int(p)
        try:
            cmd = open(f"/proc/{p}/cmdline", "rb").read()
        except OSError:
            continue
        if pid == me or pid in refused or not (any(names.search(a) for a in cmd.split(b"\0")) or checks(pid)):
            continue
        try:
            if os.getpgid(pid) == pid != mine:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError as e:                    # another user's: said once, and not looked for again
            refused.add(pid)
            note(f"note   cannot kill what a check left behind ({e.strerror}): {shown(cmd)}")
            continue
        found += 1
        note(f"note   killed what a check left behind: {shown(cmd)}")
    quiet = 0 if found else quiet + 1
    if quiet == 2:
        sys.exit(0)
    time.sleep(0.2)
sys.exit(4)
EOF
}
# while a check of the new commit runs: what strata-preflight.sh refuses a start of strata-q3 for, a process with its
# engine pattern or a listener on :8080, other than the service's own.  One that appears meanwhile with the checks'
# TMPDIR, or one under it, in its environment is the check's, in any cgroup (also when it left the check's session),
# and is killed at once; any other is only named, also one that someone else started meanwhile, and a listener of a
# process this user cannot read (another user's; the service runs as this one).  Either fails the check: the watch
# then ends with exit 3 (and $tmp/engine says what it was), and with exit 0 when it found none.  The check starts once
# it has noted what ran before ($tmp/watch.ready).  SIGTERM (unwatch) makes it look once more, with a scan that starts
# after the signal, and end; so does the end of this script.  Only its first SIGTERM acts (it blocks the others), and
# it ignores SIGHUP (it is in this script's process group, which a terminal that hangs up signals): a second SIGTERM,
# or a SIGHUP, after the outcome is known must not end it with another exit status (Python puts the default action
# back for a signal with a handler of its own while it exits).  It looks every 0.1 s, at nice 19: at the command line of each process, and at
# the listening sockets as ss -ltn sees them (sock_diag, which costs much less here than /proc/net/tcp).  While a check
# keeps it stopped (SIGSTOP), it does not look: a check does that only on purpose
watch()  {
  local n=0
  rm -f "$tmp/watch.ready"
  WATCH_DIR=$tmp WATCH_PARENT=$$ nice -n 19 python3 -I -S - {errfd}>&- <<'EOF' &
import os, re, signal, socket, struct, sys, time, unicodedata
d, parent, me = os.environ["WATCH_DIR"], int(os.environ["WATCH_PARENT"]), os.getpid()
engine = re.compile(rb"engine-cuda12/strata --serve")       # strata-preflight.sh's pgrep -f
PORT = 8080                                                 # and its ss -ltn 'sport = :8080'
SERVICE = "/system.slice/strata-q3.service"
stop = []
def term(*a):
    stop.append(1)
    # the first one only: the others stay pending, blocked.  Not SIG_IGN: one that came just as this handler set it,
    # after Python's last look for signals, would make Python print "Signal 15 ignored due to race condition" (one
    # that comes earlier in this handler runs it again, nested, which changes nothing)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
signal.signal(signal.SIGTERM, term)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
def cgroup(pid):
    try:
        return open(f"/proc/{pid}/cgroup").read().split("\n")[0].partition("::")[2]
    except OSError:
        return None
def cmdline(pid):
    """All of it: os.read, which costs less than open().read() for what the watch reads every 0.1 s."""
    try:
        fd = os.open(f"/proc/{pid}/cmdline", os.O_RDONLY)
    except OSError:
        return b""
    try:
        parts = [os.read(fd, 65536)]
        while len(parts[-1]) == 65536:
            parts.append(os.read(fd, 65536))
        return b"".join(parts)
    except OSError:
        return b""
    finally:
        os.close(fd)
def checks(pid):
    """Whether the process has the TMPDIR that the checks run with (TMPDIR=$tmp/run), or one under it, in its
    environment, in any cgroup.  Not one whose environment cannot be read."""
    try:
        env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    except OSError:
        return False
    run = b"TMPDIR=" + os.fsencode(os.path.join(d, "run"))
    return any(e == run or e.startswith(run + b"/") for e in env)
def shown(cmd):
    line = cmd.replace(b"\0", b" ").decode(errors="replace")
    return "".join(c for c in line if unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))[:120]
def diagnosed():
    """The inodes of the TCP sockets that listen on PORT, from the kernel's socket diagnostics (NETLINK_SOCK_DIAG, as
    ss asks); OSError when it does not answer as it should."""
    found = set()
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as s:          # NETLINK_SOCK_DIAG
        s.settimeout(5)
        for family in (socket.AF_INET, socket.AF_INET6):
            # struct inet_diag_req_v2: family, protocol, ext, pad, states (TCP_LISTEN only), an empty inet_diag_sockid
            req = struct.pack("=BBBBI", family, socket.IPPROTO_TCP, 0, 0, 1 << 10) + bytes(48)
            # struct nlmsghdr: length, SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST | NLM_F_DUMP, sequence, port
            s.sendto(struct.pack("=IHHII", 16 + len(req), 20, 0x301, 1, 0) + req, (0, 0))
            done = False
            while not done:
                data, off = s.recv(65536), 0
                if not data:
                    raise OSError("sock_diag: no answer")
                while off + 16 <= len(data):
                    size, kind = struct.unpack_from("=IH", data, off)
                    if kind == 3:                                       # NLMSG_DONE
                        done = True
                        break
                    if kind == 2 or size < 16 + 72:                     # NLMSG_ERROR, or no struct inet_diag_msg
                        raise OSError("sock_diag: an error or a short message")
                    # struct inet_diag_msg: family, state, ..., the local port (big-endian) at 4, ..., the inode at 68
                    if data[off + 17] == 10 and struct.unpack_from(">H", data, off + 20)[0] == PORT:
                        found.add(str(struct.unpack_from("=I", data, off + 16 + 68)[0]))
                    off += (size + 3) & ~3
    return found
def listening():
    """The inodes of the sockets that listen on PORT, as ss -ltn sees them: from sock_diag, or from /proc/net/tcp and
    tcp6 when that fails."""
    try:
        return diagnosed()
    except OSError:
        pass
    found = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(name).read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            f = row.split()
            if len(f) > 9 and f[3] == "0A" and f[1].rpartition(":")[2] == f"{PORT:04X}":
                found.add(f[9])
    return found
def owner(inode):
    target = f"socket:[{inode}]"
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            if any(os.readlink(f"/proc/{p}/fd/{fd}") == target for fd in os.listdir(f"/proc/{p}/fd")):
                return int(p)
        except OSError:
            continue
    return None
before = set(map(int, filter(str.isdigit, os.listdir("/proc"))))
judged, sockets, found = set(), set(), []
open(os.path.join(d, "watch.ready"), "w").close()
def record(line):
    """What was found: the exit status says that there was something, $tmp/engine what it was (the check can reach
    that file)."""
    found.append(line)
    try:
        with open(os.path.join(d, "engine"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
def judge(pid, what):
    cg = cgroup(pid)
    if pid in judged or cg is None or cg == SERVICE or cg.startswith(SERVICE + "/"):
        return
    judged.add(pid)
    cmd, killed = cmdline(pid), ""
    if pid not in before and checks(pid):                  # the check's
        try:
            os.kill(pid, signal.SIGKILL)
            killed = " (killed)"
        except OSError:
            pass
    record(f"{what}{killed}: {shown(cmd)}")
while True:
    # decided before the scan: a SIGTERM that comes during a scan, whose list of processes may be older than what the
    # check started last, is followed by one more scan
    last = bool(stop) or os.getppid() != parent     # the check has ended, or this script is gone
    for p in filter(str.isdigit, os.listdir("/proc")):
        if int(p) != me and engine.search(cmdline(p).replace(b"\0", b" ")):
            judge(int(p), "an engine")
    for inode in listening() - sockets:
        pid = owner(inode)
        if pid is not None:
            sockets.add(inode)
            judge(pid, "a listener on :8080")
        elif inode in listening():                  # there still: of a process this user cannot read
            sockets.add(inode)
            record("a listener on :8080, of a process this user cannot read")
    if last:
        break
    time.sleep(0.1)
sys.exit(3 if found else 0)
EOF
  watcher=$!
  until [ -e "$tmp/watch.ready" ]; do
    n=$((n + 1))
    [ $n -le 400 ] && kill -0 "$watcher" 2> /dev/null || return 1
    pause 0.05
  done
}
# ends the watch (end_jobs): its exit status (0: it found nothing, 3: it found something, 137: it did not end within
# $GRACE s of its SIGTERM and was killed), and 1 when none runs or it outlives its SIGKILL
unwatch() {
  local rc=1
  if [ -n "$watcher" ]; then end_jobs "$watcher" && rc=$status; watcher=; fi
  return $rc
}
# shellcheck disable=SC2329  # the trap in main calls it
stop()   {
  trap '' INT TERM HUP                            # the clean-up is not cut short
  [ -z "$errfd" ] || exec 2>&"$errfd"             # stderr back: this trap can run inside a nap (pause)
  # the last check has ended, before reaped has seen it (it naps between its looks): it was not interrupted, so the
  # run goes on, with the trap off, to say what it found.  One that has ended but that bash has not reaped yet (at its
  # SIGCHLD) still counts as running, and so does one that ends while this trap looks
  if [ -n "$final" ] && [ -n "$child" ] && ! kill -0 "$child" 2> /dev/null; then return 0; fi
  # first, what runs in the background ends (end_jobs), and only that is waited for: the timeout of a check or of the
  # fetch, with its process group (the check's), and the watch.  jobs finds them, also one that started just before
  # the signal, before $! was noted.  The fast-forward runs in the foreground, so bash runs this trap only after git
  # has ended
  local p s pids=()
  for p in $(jobs -pr); do pids+=("$p"); done
  end_jobs "${pids[@]}"
  child= watcher=
  sweep; s=$?
  [ $s = 0 ] || echo "note   the clean-up after the check did not finish (exit $s): what it started may still run" >&2
  if [ -n "$merged" ] || { [ -n "$ff" ] && [ "$(git rev-parse -q --verify HEAD)" = "$ff" ]; }; then
    echo "deployed $short, but the check after it was interrupted: run scripts/check-drift.sh" >&2; exit 1
  fi
  refuse "interrupted"
}
# commit subjects and test output come from other people: no control or bidi characters on the terminal.  Python
# starts with SIGINT ignored, as the sweep does: a Ctrl-C ends what writes into it (git, sed), and it ends at the end of
# its input; the trap (stop) says why
printable() {
  env --ignore-signal=INT python3 -I -S -c 'import sys, unicodedata
text = sys.stdin.buffer.read().decode("utf-8", "replace")
sys.stdout.write("".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp")))'
}
# seconds, output file (- for the output of this script), command...: timeout runs the command in a process group of
# its own, which a Ctrl-C on the terminal does not reach; so it runs in the background, where the trap can stop it.
# When the timeout has not ended it $SLACK s past its time (it was stopped, say), this script ends both (end_jobs), and
# the exit status is timeout's for a command past its time, 124.  The last check (final), once it has ended or is past
# its time, decides the outcome: the trap goes off before anything else (until then, stop() takes it for one that was
# not interrupted when it finds it ended)
limited() {
  local seconds=$1 out=$2 rc; shift 2
  if [ "$out" = - ]; then timeout -k 10 "$seconds" "$@" < /dev/null 9>&- {errfd}>&- &
  else timeout -k 10 "$seconds" "$@" > "$out" 2>&1 < /dev/null 9>&- {errfd}>&- &
  fi
  child=$!
  reaped "$child" $((seconds + SLACK)); rc=$?
  [ -z "$final" ] || trap '' INT TERM HUP
  if [ $rc = 0 ]; then rc=$status
  else end_jobs "$child"; rc=124
  fi
  child=
  return $rc
}
checkout_clean() {   # refuses unless this checkout is on main with no local changes
  local st
  [ "$(git symbolic-ref -q --short HEAD)" = main ] || refuse "this checkout is not on branch main"
  st=$(git status --porcelain --untracked-files=all) || refuse "git status failed"
  [ -z "$st" ] || refuse "this checkout has local changes (git status)"
  st=$(git ls-files -v) || refuse "git ls-files failed"
  if grep -q -E '^([a-z]|S) ' <<< "$st"; then
    refuse "this checkout hides files from git status (skip-worktree or assume-unchanged: git ls-files -v)"
  fi
}
service_idle() {   # prints the unit's state; fails while it starts or stops or has a job queued
  local state job
  state=$(systemctl show -p ActiveState --value "$UNIT") || { echo "in an unknown state"; return 1; }
  job=$(systemctl show -p Job --value "$UNIT") || { echo "in an unknown state"; return 1; }
  [ -z "$job" ] || { echo "$state, with job $job queued"; return 1; }
  echo "${state:-in an unknown state}"
  case $state in active|inactive|failed) ;; *) return 1 ;; esac
}
# script, its last line on success, log, its arguments: within $LIMIT s, with a TMPDIR of its own, of this environment
# only what KEEP names, at nice 19, first in line for the OOM killer (oom_score_adj 1000), and watched (watch); sets
# $why.  Exit 137 (SIGKILL) at or after the limit is timeout's -k; before it, something else killed the check, such as
# the OOM killer at the account's memory limit.  Once it knows why the check failed, an interrupt no longer changes
# the outcome (the trap, stop, is off before $why is set): the caller prints the check's output and says why.  For the
# last check (final), the trap is off from the check's end on (limited)
check_of_target() {
  local rc w s t v r keep=()
  mkdir -p "$tmp/run" || { trap '' INT TERM HUP; why="mkdir failed"; return 1; }
  for v in "${KEEP[@]}" $(compgen -e | grep '^LC_'); do [ -z "${!v+x}" ] || keep+=("$v=${!v}"); done
  rm -f "$tmp/engine"
  if ! watch; then
    unwatch; trap '' INT TERM HUP
    why="what strata-preflight.sh refuses a start for cannot be watched (the watch did not start)"; return 1
  fi
  t=$SECONDS
  limited "$LIMIT" "$3" nice -n 19 choom -n 1000 -- env -i "${keep[@]}" TMPDIR="$tmp/run" "$1" "${@:4}"; rc=$?
  t=$((SECONDS - t))
  unwatch; w=$?
  sweep; s=$?
  # the watch's exit status decides, also when the check removed $tmp/engine, or ended the watch
  if [ $w = 3 ] || [ -s "$tmp/engine" ]; then
    v=$(head -n 1 "$tmp/engine" 2> /dev/null)
    [ -n "$v" ] || v="what the watch found (its record is missing)"
    r="strata-preflight.sh would refuse a start of strata-q3 while it ran: $v"
  elif [ $w != 0 ]; then r="the watch did not finish (exit $w): what ran meanwhile is not known"
  elif [ $s != 0 ]; then r="the clean-up after the check did not finish (exit $s): what it started may still run"
  elif [ $rc = 124 ] || { [ $rc = 137 ] && [ $t -ge $LIMIT ]; }; then r="it did not finish in $LIMIT s"
  elif [ $rc = 137 ]; then
    r="it was killed (SIGKILL) after $t s, for example by the OOM killer at the account's memory limit: see"
    r+=" memory.events of user-$UID.slice"
  elif [ $rc != 0 ]; then r="exit $rc"
  elif [ "$(tail -n 1 "$3")" != "$2" ]; then r="exit 0, but no \"$2\" at the end"
  else
    grep -q '^DRIFT' "$3"; rc=$?
    case $rc in 0) r="exit 0, but with DRIFT lines" ;; 1) return 0 ;; *) r="its output cannot be read" ;; esac
  fi
  trap '' INT TERM HUP
  why=$r
  return 1
}

# all of it in one function, which bash reads in full before running it: a pull while it runs may replace this file
main() {
  local apply head url repo state changed manual rc
  local github='^(https://([^/@]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$'
  # the traps first (the functions they run are defined by now): a signal before them ends the script as the signal
  # does, which is right only while it has made and locked nothing.  Before them, a copy of stderr for stop() (pause)
  exec {errfd}>&2
  trap finish EXIT
  trap stop INT TERM HUP
  # a Ctrl-\ ends nothing here: bash ignores it itself, and so, from this, do the sweep and the watch, which the
  # clean-up needs, and the sleep of a nap (pause)
  trap '' QUIT
  case "$*" in
    '') apply=0 ;;
    --apply) apply=1 ;;
    *) trap '' INT TERM HUP                        # the outcome: an interrupt does not change it now
       echo "usage: scripts/deploy.sh [--apply]" >&2; exit 2 ;;
  esac
  command -v git > /dev/null || refuse "git is not installed"
  # shellcheck disable=SC2046  # git's repository variables, one word each: a caller's GIT_DIR (a hook) must not apply
  unset $(command git rev-parse --local-env-vars)
  export GIT_TERMINAL_PROMPT=0
  cd "$D" || exit 1
  exec 9> .git/deploy.lock || refuse "cannot open .git/deploy.lock"
  flock -n 9; case $? in 0) ;; 1) refuse "another scripts/deploy.sh is running" ;; *) refuse "flock failed" ;; esac

  checkout_clean
  limited 120 - git "${GIT_OPTS[@]}" fetch -q origin +refs/heads/main:refs/remotes/origin/main ||
    refuse "git fetch origin main failed"
  head=$(git rev-parse -q --verify 'HEAD^{commit}') || refuse "cannot read HEAD"
  target=$(git rev-parse -q --verify 'refs/remotes/origin/main^{commit}') || refuse "cannot read origin/main"
  if [ "$head" = "$target" ]; then
    trap '' INT TERM HUP                          # the outcome: an interrupt does not change it now
    echo "up to date: $(git log -1 --format='%h %s' | printable)"; exit 0
  fi
  short=$(git rev-parse --short "$target")
  git merge-base --is-ancestor "$head" "$target" ||
    refuse "origin/main ($short) is not a fast-forward: this checkout has commits that origin/main does not"
  echo "origin/main is $(git rev-list --count "$head..$target") commit(s) ahead of this checkout:"
  git log --format='  %h %s' "$head..$target" | printable
  ok "on main, no local changes, and a fast-forward to $short"

  manual=$(git diff --name-only "$head" "$target" -- "${MANUAL[@]}" | tr '\n' ' ') || refuse "git diff failed"
  [ -z "$manual" ] || refuse "the commits change ${manual% }: deploy them by hand (docs/runbooks/deploy.md)"
  ok "nothing in the commits needs a deploy by hand"

  state=$(service_idle) || refuse "$UNIT is $state; deploy when it is not starting or stopping"
  ok "$UNIT is $state"

  url=$(git config --get remote.origin.url); url=${url%/}; url=${url%.git}
  shopt -s nocasematch
  [[ $url =~ $github ]] || refuse "origin is not a GitHub repository: $(sed -E 's#^([a-z+]+://)[^/@]*@#\1#' <<< "$url")"
  shopt -u nocasematch
  repo=${BASH_REMATCH[3]}
  tmp=$(mktemp -d) || refuse "mktemp failed"     # removed by the EXIT trap (finish), set before
  url="https://api.github.com/repos/$repo/actions/workflows/$WORKFLOW/runs"
  curl -q -fsS --max-time 30 -H 'Accept: application/vnd.github+json' \
    "$url?head_sha=$target&event=push&branch=main&per_page=100" > "$tmp/runs.json" ||
    refuse "cannot read the runs of $WORKFLOW for $short from GitHub"
  # Python starts with SIGINT ignored, as the sweep does: it reads a file, and the trap (stop) says why after it
  state=$(env --ignore-signal=INT python3 -I -S - "$tmp/runs.json" "$target" ".github/workflows/$WORKFLOW" <<'EOF'
import json, sys
path, sha, workflow = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    runs = [r for r in data["workflow_runs"] if r["path"] == workflow and r["head_sha"] == sha
            and r["event"] == "push" and r["head_branch"] == "main"]
    if data["total_count"] > len(data["workflow_runs"]):
        print("unreadable: more runs than one page")
    elif not runs:
        print("missing")
    else:
        newest = max(runs, key=lambda r: r["run_number"])
        print(f"{newest['status']} {newest['conclusion'] or ''}".strip())
except (OSError, ValueError, KeyError, TypeError):
    print("unreadable")
EOF
)
  [ "$state" = "completed success" ] || refuse "the newest run of $WORKFLOW for $short is ${state:-unreadable}"
  ok "the newest run of $WORKFLOW for $short passed"

  mkdir "$tmp/tree" || refuse "mkdir failed"
  git archive "$target" | tar -x -C "$tmp/tree" || refuse "cannot export $short"
  if ! check_of_target "$tmp/tree/scripts/test.sh" "all checks passed" "$tmp/test.log"; then
    sed 's/^/  /' "$tmp/test.log" | printable
    refuse "scripts/test.sh of $short failed: $why"
  fi
  ok "scripts/test.sh of $short: all checks passed"
  [ $apply = 1 ] || final=1                       # a dry run: this check decides the outcome
  if ! check_of_target "$tmp/tree/scripts/check-drift.sh" "no drift" "$tmp/drift.log" --candidate; then
    sed 's/^/  /' "$tmp/drift.log" | printable
    refuse "scripts/check-drift.sh of $short failed: $why; docs/runbooks/deploy.md says what to do"
  fi
  ok "scripts/check-drift.sh --candidate of $short: no drift"
  changed=$(git diff --name-only "$head" "$target" -- "${AT_START[@]}" | tr '\n' ' ')
  [ -z "$changed" ] || echo "note   $UNIT runs these from this checkout at its next start: ${changed% }"
  if [ $apply = 0 ]; then                         # the outcome: the trap is off since the check ended (final)
    echo "dry run: scripts/deploy.sh --apply deploys $short"; exit 0
  fi

  # the checks took a while: the checkout and the service must still be as they were
  checkout_clean
  [ "$(git rev-parse HEAD)" = "$head" ] || refuse "this checkout moved while the checks ran"
  state=$(service_idle) || refuse "$UNIT is $state; deploy when it is not starting or stopping"
  ff=$target
  # in the foreground, in a session of its own: a Ctrl-C does not reach git, and bash runs the trap (stop) only once
  # git has ended, so that the checkout is not left half-way
  setsid -w git "${GIT_OPTS[@]}" merge -q --ff-only --no-overwrite-ignore "$target" < /dev/null 9>&- {errfd}>&-; rc=$?
  [ $rc = 0 ] || refuse "git merge --ff-only $short failed"
  merged=$short
  echo "deployed $short: $(git log -1 --format=%s | printable)"
  # a restart with IQ3_S took 550-556 s (results/ab3-iq3s-*-switch.txt)
  [ -z "$changed" ] || echo "to run them now: sudo systemctl restart strata-q3 (ready after about 9 minutes)"
  final=1                                         # this check decides the outcome
  if ! check_of_target scripts/check-drift.sh "no drift" "$tmp/drift.log"; then
    sed 's/^/  /' "$tmp/drift.log" | printable
    echo "deployed $short, but scripts/check-drift.sh after it failed: $why" >&2
    exit 1
  fi
}
main "$@"; exit
