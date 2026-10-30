"""Running a stub harness (tests/*_stubs.sh) from a unit test.

Three harnesses (tests/ab_engine_stubs.sh, tests/ab_model_run_stubs.sh and tests/switch_model_stubs.sh) start fake
engines with the command line that strata-preflight.sh looks for: while one lives, a start of strata-q3 is refused.
run() starts any tests/*_stubs.sh but the ones that start no fake engine (NO_ENGINES) only when harness tests were
asked for (scripts/test.sh --harnesses, or STRATA_TEST_HARNESSES=1), and only where no start of strata-q3 can come
(not_now): on a machine that systemd runs, while strata-q3 is stopped with no job queued and no experiment driver
(bench/*.sh) or scheduled restore is under way, since while the service runs a restart of it after a crash can come at
any time; where systemd does not run (CI's container), only with STRATA_TEST_NO_SERVICE=1, which says that no strata-q3
runs on the host: a container on the machine has no systemd either, and the host sees its processes.  So the flag is
not taken where the kernel has the NVIDIA driver loaded, as the machine's does, also seen from a container on it.  The
caller cannot say otherwise: the harness's file decides (whether the file its path leads to names the engine, then
the name of the path and of that file, in any case of letters).
What the gate does not catch: a harness whose file does not name the engine's directory but builds the engine's path
from parts (e=engine-cuda; "${e}12/strata") or reads it from another file, when neither its name nor the name of the
file a link leads to is a *_stubs.sh outside NO_ENGINES.  Only a harness written to get around the gate does that,
and code review is the gate for such a one.
While a harness that starts fake engines runs, it holds a lock (lock_path): with scripts/test.sh --jobs, tests run
side by side, and a test whose check watches for any process with the engine's command line holds the lock alone
(alone()), so that it sees no fake engine of a harness in another worker.
A harness runs in a session and a temporary directory of its own (its sandbox is made under TMPDIR, and its command
line names the directory).  A run past its time, and a run whose test is interrupted (SIGINT, SIGTERM, SIGHUP: a
Ctrl-C, or a timeout), gets SIGTERM, so that its EXIT trap stops what it started, then SIGKILL STOP_WAIT s later.
Under the time limit of scripts/deploy.sh there is no such wait: scripts/test.sh ends at the SIGTERM, and deploy.sh
then kills what names its temporary directory, the harness among them.
Then, and after a normal end too, every process still in its session, and every process whose command line names the
directory as a path (with its process group when it leads one: its children need not name it), is killed and
reported, and so is one it cannot kill, pass after pass until one finds none and another 0.2 s later finds none
either (one that has left the session with setsid and is in the middle of its exec has a command line that reads
empty).  A process that started a session of its own and names nothing of the directory is beyond this, so a harness
names its directory in the command line of what it starts with setsid.  The harness's own command line names the
directory from its exec on."""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest

ASK = "STRATA_TEST_HARNESSES"
NO_SERVICE = "STRATA_TEST_NO_SERVICE"
NO_ENGINES = {"run_robustness_stubs.sh"}    # the tests/*_stubs.sh that start no fake engine
BOOTED = "/run/systemd/system"          # there when systemd runs the machine (sd_booted(3)); CI's container has none
MODULES = "/sys/module"                 # the kernel's modules: a container sees those of the machine it runs on
# a driver inside a harness's sandbox (a stubs.* directory of run() below) starts the sandbox's stub service only
SANDBOX = re.compile(rb"/stubs\.[A-Za-z0-9_]+/")
STOP_WAIT = 60                          # seconds a harness has for its EXIT trap after SIGTERM
SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
LOCK = "STRATA_TEST_ENGINES_LOCK"      # the lock's path, which scripts/test.sh gives (lock_path)


def starts_engines(script: Path) -> bool:
    """Whether run() treats the harness as one that starts fake engines: any whose file (the one a link leads to) names
    the engine's directory, whatever its name (one in NO_ENGINES too), or cannot be read; and every other *_stubs.sh
    but those in NO_ENGINES, by its own name or by the name of the file a link leads to, in any case of letters
    (X_STUBS.SH too; a name in NO_ENGINES only as it is written there)."""
    real = Path(os.path.realpath(script))
    try:
        named = b"engine-cuda12" in real.read_bytes()
    except OSError:
        return True                             # it cannot be read: as one that does
    return named or any(n.lower().endswith("_stubs.sh") and n not in NO_ENGINES for n in (Path(script).name, real.name))


def _cmdlines():
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            yield int(pid), Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:                         # gone, or not ours to read
            continue


def environment_has(pid: int, entry: bytes) -> bool:
    """Whether entry (b"NAME=value") is one of the entries of the process's environment, as it was at its exec; False
    when it cannot be read (gone, another user's, or not dumpable)."""
    try:
        return entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False


def kill_own(pids, entry: bytes, sig: int = signal.SIGKILL) -> list[int]:
    """For the clean-up of a test that finds its processes by their command line: sig to those of pids that have entry
    in their environment (environment_has), and to no other; the pids it signalled.  The service's engine runs as this
    user, and its command line can hold what a test looks for, such as a port number; entry is what the test gives
    only to what it starts (its HOME, its TMPDIR, a variable of its own).  The signal goes through a pidfd opened
    before the environment is read, so that a process whose pid is taken by another meanwhile does not get it in that
    one's place (os.kill where pidfds are not allowed)."""
    done = []
    for pid in pids:
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        except OSError:                         # no pidfd here (an old kernel, a seccomp filter)
            fd = None
        try:
            if not environment_has(pid, entry):
                continue
            if fd is None:
                os.kill(pid, sig)
            else:
                signal.pidfd_send_signal(fd, sig)
            done.append(pid)
        except ProcessLookupError:
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return done


def _names(path: str):
    """A matcher of an argument that names path as a path of its own (as scripts/deploy.sh's sweep matches)."""
    paths = {os.fsencode(path), os.fsencode(os.path.realpath(path))}
    return re.compile(rb"(?:^|[\s'\"=:,])(?:" + b"|".join(re.escape(p) for p in paths) + rb")(?=[/\s'\":,;)]|$)").search


def sweep(path: str, session: int | None = None) -> list[str]:
    """Kill every process whose command line names path, and every process of the session (when given), with its
    process group when it leads one (its children need not name it), pass after pass until one finds none and another
    0.2 s later finds none either (one that has left the session with setsid and is in the middle of its exec has a
    command line that reads empty), at most 20 passes; the command lines killed, and those it could not kill (said
    so)."""
    out, refused, quiet = [], set(), 0
    me, mine, names = os.getpid(), os.getpgrp(), _names(path)
    for _ in range(20):
        found = 0
        for pid, cmd in _cmdlines():
            try:
                ours = session is not None and os.getsid(pid) == session
            except OSError:
                ours = False
            if pid == me or pid in refused or not (ours or any(names(part) for part in cmd)):
                continue
            line = b" ".join(cmd).decode(errors="replace")[:200]
            try:
                if os.getpgid(pid) == pid != mine:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError as e:                # another user's: reported, and not looked at again
                refused.add(pid)
                out.append(f"{line} (cannot be killed: {e.strerror})")
                continue
            found += 1
            out.append(line)
        quiet = 0 if found else quiet + 1
        if quiet == 2:
            break
        time.sleep(0.2)
    return out


def _state(unit: str) -> str:
    """The unit's state (systemctl is-active), "unreadable" when systemctl gives none."""
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unreadable"
    return r.stdout.strip() or "unreadable"


def _job(unit: str) -> str:
    """The id of the unit's queued job, "" when it has none, "unreadable" when systemctl does not say."""
    try:
        r = subprocess.run(["systemctl", "show", "-p", "Job", "--value", unit], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unreadable"
    return r.stdout.strip() if r.returncode == 0 else "unreadable"


def _drivers() -> list[int]:
    """The processes that run an experiment driver: any bench/*.sh anywhere in their command line (they stop and start
    strata-q3, or measure it; nice, taskset or flock may come first), by the path it was given, resolved against the
    process's working directory, and by the file that path leads to (a link to a driver, also one whose name has no
    .sh, or a path through a linked directory); not one in a harness's sandbox."""
    found = []
    for pid, cmd in _cmdlines():
        for part in cmd:
            path = part
            if not part.startswith(b"/"):
                try:
                    path = os.path.join(os.fsencode(os.readlink(f"/proc/{pid}/cwd")), part)
                except OSError:                 # a relative path stays as it is: it is not resolved against this cwd
                    pass
            absolute = path.startswith(b"/")
            if not (part.endswith(b".sh") or absolute and os.path.islink(path)):
                continue
            paths = [os.path.normpath(path)] + ([os.path.realpath(path)] if absolute else [])
            if any(p.endswith(b".sh") and os.path.basename(os.path.dirname(p)) == b"bench" and not SANDBOX.search(p)
                   for p in paths):
                found.append(pid)
                break
    return found


def not_now() -> str | None:
    """Why a harness that starts fake engines must not run now, or None."""
    if not os.path.isdir(BOOTED):
        if not os.path.isdir(MODULES) or os.path.exists(os.path.join(MODULES, "nvidia")):
            return ("systemd does not run here, and the kernel has the NVIDIA driver loaded, or its modules cannot be "
                    "read: this can be a container on a machine that runs strata-q3, which sees its processes; run the "
                    "harnesses there while strata-q3 is stopped, or in CI")
        if os.environ.get(NO_SERVICE) == "1":
            return None
        return ("systemd does not run here, so whether a strata-q3 on this host could start cannot be read (a container "
                f"on the machine is like that too): {NO_SERVICE}=1 says that none runs on the host")
    running = _drivers()
    if running:
        return (f"an experiment driver runs (pids {', '.join(map(str, running))}); it starts strata-q3, which a fake "
                "engine would block")
    rescue = _state("strata-ab-rescue.timer")
    if rescue != "inactive":
        return f"strata-ab-rescue.timer is {rescue}: it would start strata-q3, which a fake engine would block"
    state = _state("strata-q3.service")
    if state == "failed":
        return "strata-q3 is failed: strata-health.sh starts it again after its cool-down, and a fake engine would block that"
    if state != "inactive":
        return (f"strata-q3 is {state}: a start of it while a harness runs, also a restart after a crash, would see "
                "the fake engines; run the harnesses while it is stopped, or in CI")
    job = _job("strata-q3.service")
    if job:
        return f"strata-q3 has a job queued ({job}): a start of it would see the fake engines"
    return None


def gate(test: unittest.TestCase, tools: tuple[str, ...]) -> None:
    """For a harness test: skip it unless harness tests were asked for; when they were, a missing tool, or a reason
    not to run now (not_now), fails it.  run() checks the same again before it starts the harness."""
    if os.environ.get(ASK) != "1":
        raise unittest.SkipTest("harness: runs with scripts/test.sh --harnesses (STRATA_TEST_HARNESSES=1)")
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        test.fail("needs " + ", ".join(missing))
    why = not_now()
    if why:
        test.fail(f"not run now: {why}")


def lock_path() -> str:
    """The lock that a harness that starts fake engines holds shared while it runs, and that alone() holds alone: the
    one that scripts/test.sh gives its workers, or one of this user's in the shared temporary directory.  That one
    (when the tests run without scripts/test.sh, as with python3 -m unittest) stays there after the tests: removing a
    lock file that another run still holds open would let a third run lock a new file beside it."""
    return os.environ.get(LOCK) or os.path.join(tempfile.gettempdir(), f"strata-test-engines-{os.getuid()}.lock")


@contextlib.contextmanager
def _locked(mode: int):
    # O_NONBLOCK: a FIFO in its place does not hang the open (a regular file's lock still waits)
    fd = os.open(lock_path(), os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, mode)
        yield
    finally:
        os.close(fd)


def alone():
    """For a test whose check watches for any process with the engine's command line (as strata-preflight.sh does):
    while it holds this, no harness that starts fake engines runs, also not in another worker of scripts/test.sh
    --jobs.  It waits for those that run."""
    return _locked(fcntl.LOCK_EX)


class Interrupted(KeyboardInterrupt):
    """SIGTERM or SIGHUP while a harness ran.  A KeyboardInterrupt, so that unittest stops the whole run, as for SIGINT:
    with an Exception it would go on with the next test, which could start the next harness."""


def _stop(p: subprocess.Popen) -> None:
    """SIGTERM to the harness's process group (bash runs its EXIT trap: it stops what it started), then SIGKILL."""
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=STOP_WAIT)
    except (OSError, subprocess.TimeoutExpired):
        for kill in (lambda: os.killpg(p.pid, signal.SIGKILL), p.kill):
            try:
                kill()
            except OSError:
                pass
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass


def run(script: Path, root: Path, timeout: float) -> dict:
    """The harness's exit status (None past its time), output and error output, the processes it left behind (killed),
    and the directory it ran in (removed).  The output goes to files, not pipes: a process left behind that holds a
    pipe would keep a reader waiting.  An interrupt stops the harness and sweeps its session and directory, with
    further signals held until that is done, then goes on up."""
    engines = starts_engines(script)
    if engines:
        if os.environ.get(ASK) != "1":
            raise unittest.SkipTest("harness: runs with scripts/test.sh --harnesses (STRATA_TEST_HARNESSES=1)")
        why = not_now()
        if why:
            raise RuntimeError(f"not run now: {why}")
    with _locked(fcntl.LOCK_SH) if engines else contextlib.nullcontext():     # not beside alone()
        return _run(script, root, timeout)


def _run(script: Path, root: Path, timeout: float) -> dict:
    main = threading.current_thread() is threading.main_thread()
    held = False
    try:
        with tempfile.TemporaryDirectory(prefix="stubs.") as tmp:
            out_path, err_path = Path(tmp) / "harness.out", Path(tmp) / "harness.err"
            previous = {}
            if main:
                def interrupted(signum, frame):
                    raise Interrupted(signal.Signals(signum).name)
                for s in (signal.SIGTERM, signal.SIGHUP):
                    previous[s] = signal.signal(s, interrupted)
            p = None
            try:
                with open(out_path, "w") as out, open(err_path, "w") as err:
                    # the harness's command line names the directory from its exec on, so that a sweep finds it
                    # also when an interrupt comes before p is set
                    p = subprocess.Popen([f"harness {tmp}", str(script), str(root)], executable="/bin/bash",
                                         stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                         env={**os.environ, "TMPDIR": tmp}, start_new_session=True)
                    try:
                        status = p.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        status = None
                        _stop(p)
                left = sweep(tmp, p.pid)
            except BaseException:
                if main:                     # a second Ctrl-C must not cut the clean-up short
                    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
                    held = True
                if p is not None and p.poll() is None:
                    _stop(p)
                sweep(tmp, p.pid if p is not None else None)      # its second look sees a harness that was
                raise                                             # between its fork and its exec
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)
            result = {"status": status, "out": out_path.read_text(errors="replace"),
                      "err": err_path.read_text(errors="replace"), "left": left, "dir": tmp}
    finally:
        if held:                             # what came meanwhile is delivered now, to the handlers of before
            signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
    return result


def check(test, result: dict, expected: list[str], timeout: float) -> None:
    """The harness ran to its end, left nothing behind, and printed exactly the expected checks, each PASS, in order."""
    lines = result["out"].splitlines()
    tail = "\n" + result["err"][-2000:]
    test.assertIsNotNone(result["status"], f"the harness ran past {timeout} s" + tail)
    test.assertEqual(result["left"], [], "processes outlived the harness")
    test.assertEqual(result["status"], 0, "\n".join(l for l in lines if not l.startswith("PASS ")) + tail)
    test.assertEqual(lines[-1:], ["0 failed"], "the harness did not end with its count of failures" + tail)
    test.assertEqual([l for l in lines[:-1] if not l.startswith("PASS ")], [], "lines other than PASS")
    test.assertEqual([l[len("PASS "):] for l in lines[:-1]], expected,
                     "the checks that passed are not the expected ones (one stopped running, or one is new: list it)")
