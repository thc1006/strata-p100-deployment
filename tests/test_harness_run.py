"""tests/harness_run.py with harnesses of a few lines: what it reports when one passes, prints a FAIL line, misses or
adds a check, has no count line, runs past its time or leaves a process behind, when it will not start one, and what
an interrupt leaves.  None of these harnesses starts a fake engine, and Run's and Interrupt's are not named
*_stubs.sh, so they run anywhere; RunGate's are, and do nothing but write a file."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run as H  # noqa: E402

# a process of its own session that names the harness's directory, as a fake engine does, with a child that names
# only the marker after it
LEAK = 'setsid bash -c \'(exec -a "$1-child" sleep 60) & wait\' "$TMPDIR/fake" "{marker}" > /dev/null 2>&1 &'
# what a harness leaves in a session of its own, naming nothing of its directory (so beyond the sweep): it writes its
# pid to respawn.pid in argv[2], starts a process that names the directory (RESPAWN_DIR) and, each time that is killed,
# another one argv[3] s later, argv[4] in all (2 when not given)
RESPAWN = """import os, subprocess, sys, time
open(os.path.join(sys.argv[2], "respawn.pid"), "w").write(str(os.getpid()))
for n in range(1, int(sys.argv[4] if sys.argv[4:] else 2) + 1):
    if n > 1:
        time.sleep(float(sys.argv[3]))
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(61)",
                          os.environ["RESPAWN_DIR"] + f"/respawn-{n}", sys.argv[1]])
    if n == 1:
        open(os.path.join(sys.argv[2], "respawn.ready"), "w").close()
    c.wait()
time.sleep(60)
"""
# what a harness leaves in a session of its own, naming nothing of its directory (LATE_DIR, in its environment only):
# it makes late.ready in the directory argv[2], waits there for go (at most 30 s), and execs into a process that names
# the directory and makes late.exec there
LATE = """import os, sys, time
d, end = sys.argv[2], time.monotonic() + 30
open(os.path.join(d, "late.ready"), "w").close()
while not os.path.exists(os.path.join(d, "go")):
    if time.monotonic() > end:
        sys.exit(1)
    time.sleep(0.005)
os.execvp(sys.executable, [sys.executable, "-c", 'import sys, time; open(sys.argv[3], "w"); time.sleep(60)',
                           sys.argv[1], os.environ["LATE_DIR"] + "/late", os.path.join(d, "late.exec")])
"""


def named(text: str) -> list[int]:
    return [pid for pid, cmd in H._cmdlines() if any(text.encode() in part for part in cmd)]


OWNER = "HARNESS_TEST_OWNER"


def owned(test: unittest.TestCase, marker: str) -> None:
    """What the test starts from now on has OWNER=marker in its environment; after the test, what names marker and has
    that is killed (H.kill_own), and no other process that names it."""
    test.addCleanup(lambda: H.kill_own(named(marker), f"{OWNER}={marker}".encode()))
    patch = mock.patch.dict(os.environ, {OWNER: marker})
    patch.start()
    test.addCleanup(patch.stop)


class Run(unittest.TestCase):
    def result(self, body: str, timeout: float = 20) -> dict:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        script = d / "harness.sh"
        script.write_text("set -u\n" + body + "\n")
        return H.run(script, d, timeout)

    def refused(self, result: dict, expected: list[str], says: str) -> None:
        with self.assertRaises(AssertionError) as e:
            H.check(self, result, expected, 20)
        self.assertIn(says, str(e.exception))

    def test_a_harness_that_passes(self):
        H.check(self, self.result('echo "PASS a"; echo "PASS b"; echo "0 failed"'), ["a", "b"], 20)

    def test_a_fail_line_under_exit_0(self):           # a fail() whose count a subshell lost
        self.refused(self.result('echo "PASS a"; echo "FAIL b"; echo "0 failed"'), ["a"], "lines other than PASS")

    def test_a_check_that_stopped_running_or_a_new_one(self):
        self.refused(self.result('echo "PASS a"; echo "0 failed"'), ["a", "b"], "not the expected ones")
        self.refused(self.result('echo "PASS a"; echo "PASS c"; echo "0 failed"'), ["a"], "not the expected ones")
        self.refused(self.result('echo "PASS b"; echo "PASS a"; echo "0 failed"'), ["a", "b"], "not the expected ones")

    def test_no_count_of_failures(self):
        self.refused(self.result('echo "PASS a"'), ["a"], "count of failures")

    def test_a_failing_exit(self):
        self.refused(self.result('echo "PASS a"; echo "1 failed"; exit 1'), ["a"], "")
        self.refused(self.result('echo "PASS a"; echo "0 failed"; exit 3'), ["a"], "")   # it died after its count

    def test_past_its_time_nothing_is_left(self):
        marker = f"harness-left-{os.getpid()}-time"
        r = self.result("trap 'echo \"its EXIT trap ran\" >&2' EXIT\n" + LEAK.format(marker=marker) + "\nsleep 60",
                        timeout=1)
        self.assertIsNone(r["status"])
        self.assertIn("its EXIT trap ran", r["err"])      # SIGTERM first: a harness stops what it started
        self.refused(r, [], "ran past")
        time.sleep(0.2)
        self.assertEqual(named(r["dir"]) + named(marker), [])

    def test_a_process_left_behind_is_killed_and_reported(self):
        marker = f"harness-left-{os.getpid()}-end"
        r = self.result(LEAK.format(marker=marker) + '\necho "PASS a"; echo "0 failed"')
        self.assertEqual(len(r["left"]), 1, r["left"])
        self.refused(r, ["a"], "outlived the harness")
        time.sleep(0.2)
        self.assertEqual(named(r["dir"]) + named(marker), [], "the leader or its child is left")

    def test_what_stays_in_its_session_is_killed_and_reported(self):
        # a process of the harness's session that names nothing of its directory, in a process group of its own
        marker = f"harness-left-{os.getpid()}-session"
        r = self.result(f'python3 -c "import os, time; os.setpgid(0, 0); time.sleep(60)" {marker} &\n'
                        'sleep 0.3; echo "PASS a"; echo "0 failed"')
        self.assertEqual(len(r["left"]), 1, r["left"])
        self.assertIn(marker, r["left"][0])
        time.sleep(0.2)
        self.assertEqual(named(marker), [])

    def test_one_that_starts_another_when_it_is_killed(self):
        # the sweep goes on until a pass finds nothing: what the harness leaves in a session of its own, naming nothing
        # of its directory, starts a process that names it, and a second one once that is killed: the next pass kills
        # the second.  Also when the second starts 0.3 s later, after a pass that found nothing: the sweep looks once
        # more 0.2 s after such a pass (one in the middle of its exec is like that)
        for delay in ("0", "0.3"):
            with self.subTest(delay=delay):
                marker = f"harness-left-{os.getpid()}-respawn-{delay}"
                owned(self, marker)
                d = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, d, True)
                (d / "respawn.py").write_text(RESPAWN)
                (d / "harness.sh").write_text(
                    f'RESPAWN_DIR=$TMPDIR setsid python3 "$1/respawn.py" {marker} "$1" {delay} > /dev/null 2>&1 '
                    '< /dev/null &\n'
                    'n=0; while [ ! -e "$1/respawn.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done\n'
                    'echo "PASS a"; echo "0 failed"\n')
                r = H.run(d / "harness.sh", d, 20)
                self.assertEqual([line for line in r["left"] if " -c import time; time.sleep(61) " in line], r["left"])
                self.assertEqual(len(r["left"]), 2, r["left"])
                time.sleep(0.2)
                self.assertEqual(named(marker), [int((d / "respawn.pid").read_text())],
                                 "one that names its directory is left")

    def test_it_looks_once_more_after_a_first_pass_that_finds_nothing(self):
        # what the harness leaves (beyond the sweep) execs into one that names its directory as the sweep sleeps after
        # its first pass, which so finds nothing: the second look finds it
        marker = f"harness-left-{os.getpid()}-late"
        owned(self, marker)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "late.py").write_text(LATE)
        (d / "harness.sh").write_text(
            f'LATE_DIR=$TMPDIR setsid python3 "$1/late.py" {marker} "$1" > /dev/null 2>&1 < /dev/null &\n'
            'n=0; while [ ! -e "$1/late.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done\n'
            'echo "PASS a"; echo "0 failed"\n')

        class Clock:
            """time for harness_run: at the sweep's first sleep, the late one execs, and the sleep starts once it
            has"""
            def __getattr__(self, name):
                return getattr(time, name)

            @staticmethod
            def sleep(seconds):
                if not (d / "go").exists():
                    (d / "go").write_text("")
                    end = time.monotonic() + 10
                    while not (d / "late.exec").exists() and time.monotonic() < end:
                        time.sleep(0.01)
                time.sleep(seconds)
        with mock.patch.object(H, "time", Clock()):
            r = H.run(d / "harness.sh", d, 20)
        self.assertEqual(len(r["left"]), 1, r["left"])
        self.assertIn(f" -c import sys, time; open(sys.argv[3], \"w\"); time.sleep(60) {marker} ", r["left"][0])
        time.sleep(0.2)
        self.assertEqual(named(marker), [])

    def test_the_sweep_ends_after_its_passes(self):
        # one that starts another each time one is killed, 40 in all: the sweep kills one a pass, and gives up after
        # its 20 passes instead of going on for as long as they come
        marker = f"harness-left-{os.getpid()}-many"
        owned(self, marker)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "respawn.py").write_text(RESPAWN)
        (d / "harness.sh").write_text(
            f'RESPAWN_DIR=$TMPDIR setsid python3 "$1/respawn.py" {marker} "$1" 0 40 > /dev/null 2>&1 < /dev/null &\n'
            'n=0; while [ ! -e "$1/respawn.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done\n'
            'echo "PASS a"; echo "0 failed"\n')
        r = H.run(d / "harness.sh", d, 20)
        self.assertEqual(len(r["left"]), 20, r["left"])

    def test_its_command_line_names_its_directory(self):
        # from its exec on, so that a sweep finds the harness also when an interrupt comes before run() has its pid
        r = self.result('tr "\\0" "\\n" < /proc/$$/cmdline >&2; echo "PASS a"; echo "0 failed"')
        names = H._names(r["dir"])
        self.assertTrue(any(names(arg.encode()) for arg in r["err"].splitlines()), r["err"])

    def test_what_only_looks_like_its_directory_is_not_killed(self):
        r = self.result('echo "$TMPDIR" > "$1/tmpdir"; echo "PASS a"; echo "0 failed"')
        d = Path(r["dir"])
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"/x{d}/y", f"{d}x"],
                             start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        H.sweep(str(d))
        time.sleep(0.2)
        self.assertIsNone(p.poll(), "a process that names /x<dir>/y and <dir>x was killed")

    def test_one_it_cannot_kill_is_reported(self):
        def refuse(pid, sig):
            raise PermissionError(1, "Operation not permitted")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "/tmp/stubs.reportme/x"],
                             start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.2)
        with mock.patch.object(H.os, "killpg", refuse), mock.patch.object(H.os, "kill", refuse):
            left = H.sweep("/tmp/stubs.reportme")
        self.assertEqual(len(left), 1, left)
        self.assertIn("(cannot be killed: Operation not permitted)", left[0])

    def test_its_process_group_gets_the_sigterm(self):
        # past its time, the harness's process group gets the SIGTERM, also what bash waits for in the foreground: bash
        # runs a trap only once that command has ended.  With the SIGTERM to bash alone, its trap would wait for sleep
        # 60, until the SIGKILL STOP_WAIT s later, and would not run
        with mock.patch.object(H, "STOP_WAIT", 10):
            t0 = time.monotonic()
            r = self.result("trap 'echo \"its TERM trap ran\" >&2; exit 1' TERM\nsleep 60", timeout=1)
        self.assertIsNone(r["status"])
        self.assertIn("its TERM trap ran", r["err"])
        self.assertLess(time.monotonic() - t0, 10)

    def test_what_names_its_directory_through_a_link_or_by_its_real_path_is_killed(self):
        # its directory is under a TMPDIR that is a link: what the harness leaves in a session of its own, naming the
        # directory by its real path or by the path through the link, is killed and reported
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        (base / "real").mkdir()
        (base / "link").symlink_to(base / "real")
        marker = f"harness-left-{os.getpid()}-link"
        owned(self, marker)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        leave = ('setsid python3 -c \'import sys, time; open(sys.argv[3], "w").close(); time.sleep(60)\' '
                 f'{marker}-{{0}} "{{1}}/x" "$1/{{0}}" > /dev/null 2>&1 < /dev/null &\n')
        (d / "harness.sh").write_text(
            'real=$(cd -P "$TMPDIR" && pwd)\n' + leave.format("real", "$real") + leave.format("link", "$TMPDIR") +
            'n=0; while { [ ! -e "$1/real" ] || [ ! -e "$1/link" ]; } && [ $n -lt 200 ]; do\n'
            '  n=$((n + 1)); sleep 0.05\ndone\n'
            'echo "PASS a"; echo "0 failed"\n')
        with mock.patch.object(H.tempfile, "tempdir", str(base / "link")):
            r = H.run(d / "harness.sh", d, 20)
        self.assertTrue(r["dir"].startswith(str(base / "link") + "/"), r["dir"])
        self.assertTrue((d / "real").exists() and (d / "link").exists(), r["err"])
        self.assertEqual(sorted(kind for kind in ("real", "link") for line in r["left"] if f"{marker}-{kind} " in line),
                         ["link", "real"], r["left"])
        time.sleep(0.2)
        self.assertEqual(named(marker), [])

    def test_a_harness_that_ignores_sigterm_is_killed(self):
        with mock.patch.object(H, "STOP_WAIT", 1):
            t0 = time.monotonic()
            r = self.result("trap '' TERM\nsleep 60", timeout=1)
        self.assertIsNone(r["status"])
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(named(r["dir"]), [])


class KillOwn(unittest.TestCase):
    def test_it_signals_only_the_processes_with_the_entry(self):
        # of processes with the same command line, only the one with the entry in its environment gets the signal,
        # also where no pidfd can be opened; one that has ended is passed over
        for pidfd in (True, False):
            with self.subTest(pidfd=pidfd):
                marker = f"kill-own-{os.getpid()}-{pidfd}"
                args = [sys.executable, "-c", "import time; time.sleep(60)", marker]
                other = subprocess.Popen(args, start_new_session=True)
                mine = subprocess.Popen(args, env={**os.environ, OWNER: marker}, start_new_session=True)
                gone = subprocess.Popen(["true"], env={**os.environ, OWNER: marker})
                gone.wait()
                for q in (other, mine):
                    self.addCleanup(lambda q=q: (q.kill(), q.wait()))
                deadline = time.monotonic() + 20
                while len(named(marker)) < 2:
                    self.assertLess(time.monotonic(), deadline, "they did not start")
                    time.sleep(0.02)
                with contextlib.nullcontext() if pidfd else \
                        mock.patch.object(H.os, "pidfd_open", side_effect=OSError(38, "Function not implemented")):
                    done = H.kill_own([other.pid, mine.pid, gone.pid], f"{OWNER}={marker}".encode())
                self.assertEqual(done, [mine.pid])
                self.assertEqual(mine.wait(timeout=10), -signal.SIGKILL)
                time.sleep(0.2)
                self.assertIsNone(other.poll(), "a process without the entry got the signal")


def sb_pkill(stub: str) -> str:
    """The lines of sb_pkill() in the stub: from the one that starts "sb_pkill() {" to the first that ends "; }"."""
    lines = (Path(__file__).resolve().parent / stub).read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("sb_pkill() {"))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip().endswith("; }"))
    return "\n".join(lines[start:end + 1]) + "\n"


class SbPkill(unittest.TestCase):
    def test_it_signals_only_the_sandboxs_processes(self):
        # the stubs' pkill -f: of processes whose command lines match the pattern, only the one with SB=$SB in its
        # environment gets the signal, not one without SB, nor one with SB=$SB/x (the service's engine runs as this
        # user, and may match a pattern).  The harnesses run only in CI and on a stopped service, where no process
        # outside a sandbox matches
        texts = {stub: sb_pkill(stub) for stub in ("ab_engine_stubs.sh", "switch_model_stubs.sh")}
        self.assertEqual(len(set(texts.values())), 1, texts)
        sb = tempfile.mkdtemp(prefix="sb-pkill.")
        self.addCleanup(shutil.rmtree, sb, True)
        base = {k: v for k, v in os.environ.items() if k != "SB"}
        args = [sys.executable, "-c", "import time; time.sleep(60)", f"{sb}/stray"]
        procs = {}
        for name, env in (("SB=$SB", {**base, "SB": sb}), ("no SB", base), ("SB=$SB/x", {**base, "SB": f"{sb}/x"})):
            procs[name] = p = subprocess.Popen(args, env=env, start_new_session=True)
            self.addCleanup(lambda p=p: (p.kill(), p.wait()))
        deadline = time.monotonic() + 20
        while len(named(f"{sb}/stray")) < 3:
            self.assertLess(time.monotonic(), deadline, "they did not start")
            time.sleep(0.02)
        r = subprocess.run(["bash", "-c", texts["ab_engine_stubs.sh"] + 'sb_pkill TERM "$SB/"'],
                           env={**base, "SB": sb}, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(procs["SB=$SB"].wait(timeout=10), -signal.SIGTERM)
        time.sleep(0.2)
        self.assertEqual({name: p.poll() for name, p in procs.items()},
                         {"SB=$SB": -signal.SIGTERM, "no SB": None, "SB=$SB/x": None})


class RunGate(unittest.TestCase):
    """run() itself starts a harness that may start fake engines (any *_stubs.sh but those that start none) only when
    asked for and only when not_now() has no reason; the caller cannot say otherwise."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.script = self.d / "ab_engine_stubs.sh"
        self.script.write_text('touch "$1/started"\n')

    def test_not_asked_for(self):
        with mock.patch.dict(os.environ, {H.ASK: "0"}):
            with self.assertRaises(unittest.SkipTest) as e:
                H.run(self.script, self.d, 10)
        self.assertTrue(str(e.exception).startswith("harness:"), str(e.exception))
        self.assertFalse((self.d / "started").exists())

    def test_not_now(self):
        with mock.patch.dict(os.environ, {H.ASK: "1"}), mock.patch.object(H, "not_now", lambda: "strata-q3 is active"):
            with self.assertRaises(RuntimeError) as e:
                H.run(self.script, self.d, 10)
        self.assertIn("not run now: strata-q3 is active", str(e.exception))
        self.assertFalse((self.d / "started").exists())

    def test_a_new_harness_is_gated_too(self):
        script = self.d / "new_stubs.sh"
        script.write_text('touch "$1/started"\n')
        with mock.patch.dict(os.environ, {H.ASK: "0"}):
            with self.assertRaises(unittest.SkipTest):
                H.run(script, self.d, 10)
        self.assertFalse((self.d / "started").exists())

    def test_a_link_or_a_harness_that_names_the_engine_is_gated(self):
        os.symlink(self.script, self.d / "harness.sh")
        (self.d / "other.sh").write_text('touch "$1/started"  # engine-cuda12/strata\n')
        for name in ("harness.sh", "other.sh"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {H.ASK: "0"}):
                with self.assertRaises(unittest.SkipTest):
                    H.run(self.d / name, self.d, 10)
        self.assertFalse((self.d / "started").exists())

    def test_a_harness_that_cannot_be_read_is_gated(self):
        # whether it starts fake engines cannot be read, so it counts as one that does, under a name that would run
        # anywhere: a link that leads nowhere, and a file of mode 000 (not for root, who reads it all the same)
        os.symlink(self.d / "nowhere.sh", self.d / "dangling.sh")
        closed = self.d / "closed.sh"
        closed.write_text('touch "$1/started"\n')
        closed.chmod(0)
        for name in ["dangling.sh"] + ([] if os.geteuid() == 0 else ["closed.sh"]):
            with self.subTest(name=name), mock.patch.dict(os.environ, {H.ASK: "0"}):
                with self.assertRaises(unittest.SkipTest):
                    H.run(self.d / name, self.d, 10)
        self.assertFalse((self.d / "started").exists())

    def test_a_link_with_the_name_of_a_harness_is_gated(self):
        # the link's own name counts, not only the name of the file it leads to
        (self.d / "plain.sh").write_text('touch "$1/started"\n')
        os.symlink(self.d / "plain.sh", self.d / "link_stubs.sh")
        with mock.patch.dict(os.environ, {H.ASK: "0"}):
            with self.assertRaises(unittest.SkipTest):
                H.run(self.d / "link_stubs.sh", self.d, 10)
        self.assertFalse((self.d / "started").exists())

    def test_a_name_in_other_case_is_gated_too(self):
        # *_stubs.sh in any case of letters, as the name of the path and as the name of the file a link leads to; a
        # name in NO_ENGINES is exempt only as it is written there
        for name in ("X_STUBS.SH", "y_Stubs.Sh", "Target_STUBS.sh", "RUN_ROBUSTNESS_STUBS.SH", "plain.sh"):
            (self.d / name).write_text('touch "$1/started"\n')
        os.symlink(self.d / "plain.sh", self.d / "LINK_STUBS.SH")
        os.symlink(self.d / "Target_STUBS.sh", self.d / "plain-link.sh")
        # the name of one in NO_ENGINES, in upper case, as a link's own name, and as the name of the file it leads to
        (self.d / "links").mkdir()
        os.symlink(self.d / "plain.sh", self.d / "links" / "RUN_ROBUSTNESS_STUBS.SH")
        os.symlink(self.d / "RUN_ROBUSTNESS_STUBS.SH", self.d / "upper-link.sh")
        for name in ("X_STUBS.SH", "y_Stubs.Sh", "Target_STUBS.sh", "RUN_ROBUSTNESS_STUBS.SH", "LINK_STUBS.SH",
                     "plain-link.sh", "links/RUN_ROBUSTNESS_STUBS.SH", "upper-link.sh"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {H.ASK: "0"}):
                with self.assertRaises(unittest.SkipTest):
                    H.run(self.d / name, self.d, 10)
        self.assertFalse((self.d / "started").exists())

    def test_one_that_names_the_engine_is_gated_whatever_its_name(self):
        # what the file says comes first: under the name of one that starts no engine (NO_ENGINES) too, and through a
        # link with a *_stubs.sh name
        other = self.d / "other"
        other.mkdir()
        (other / "run_robustness_stubs.sh").write_text('touch "$1/started"  # engine-cuda12/strata\n')
        os.symlink(other / "run_robustness_stubs.sh", self.d / "link_stubs.sh")
        for script in (other / "run_robustness_stubs.sh", self.d / "link_stubs.sh"):
            with self.subTest(script=script.name), mock.patch.dict(os.environ, {H.ASK: "0"}):
                with self.assertRaises(unittest.SkipTest):
                    H.run(script, self.d, 10)
        self.assertFalse((self.d / "started").exists())

    def test_one_that_starts_no_engine_runs(self):
        for name in ("run_robustness_stubs.sh", "harness.sh"):
            with self.subTest(name=name):
                script = self.d / name
                script.write_text('touch "$1/started-$(basename "$0")"\n')
                with mock.patch.dict(os.environ, {H.ASK: "0"}), \
                        mock.patch.object(H, "not_now", lambda: "strata-q3 is active"):
                    try:
                        H.run(script, self.d, 10)
                    except unittest.SkipTest as e:      # a skip would pass this test
                        self.fail(f"gated: {e}")
                self.assertTrue((self.d / f"started-{name}").exists())
        self.assertEqual(sorted(H.NO_ENGINES), ["run_robustness_stubs.sh"])


class Lock(unittest.TestCase):
    """A harness that starts fake engines holds the lock shared while it runs, so such harnesses run side by side;
    alone() holds it alone, so that with scripts/test.sh --jobs a test whose check watches for the engine's pattern
    runs beside none of them.  A harness that starts no fake engine takes no lock.  The lock is the test's own here."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.lock = self.d / "lock"
        for patcher in (mock.patch.object(H, "lock_path", lambda: str(self.lock)),
                        mock.patch.object(H, "not_now", lambda: None), mock.patch.dict(os.environ, {H.ASK: "1"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def harness(self, name: str, text: str) -> Path:
        script = self.d / name
        script.write_text(text)
        return script

    def test_what_a_harness_holds(self):
        # what flock(1) can take of the lock while the harness runs: not the lock alone when it starts fake engines
        probe = (f"flock -n -x {self.lock} true && echo alone || echo not-alone\n"
                 f"flock -n -s {self.lock} true && echo shared || echo not-shared\n")
        for name, expected in (("ab_engine_stubs.sh", "not-alone\nshared\n"), ("x.sh", "alone\nshared\n"),
                               ("run_robustness_stubs.sh", "alone\nshared\n")):
            with self.subTest(harness=name):
                r = H.run(self.harness(name, probe), self.d, 30)
                self.assertEqual((r["status"], r["out"]), (0, expected), r["err"])

    def test_alone_waits_for_a_harness_and_a_harness_for_alone(self):
        go, ran = self.d / "go", self.d / "ran"
        script = self.harness("ab_engine_stubs.sh", f'touch {ran}\nfor _ in $(seq 100); do [ -e {go} ] && exit 0; '
                                                    f'sleep 0.1; done; exit 1\n')
        with H.alone():                     # a harness waits for alone() ...
            t = threading.Thread(target=H.run, args=(script, self.d, 30))
            t.start()
            time.sleep(0.5)
            self.assertFalse(ran.exists(), "the harness ran beside alone()")
        for _ in range(100):
            if ran.exists():
                break
            time.sleep(0.05)
        self.assertTrue(ran.exists(), "the harness did not run once alone() ended")
        held = []                           # ... and alone() for a harness that runs
        def take():
            with H.alone():
                held.append(1)
        u = threading.Thread(target=take)
        u.start()
        time.sleep(0.5)
        self.assertEqual(held, [], "alone() did not wait for the harness")
        go.touch()
        t.join(30)
        u.join(30)
        self.assertEqual((t.is_alive(), u.is_alive(), len(held)), (False, False, 1))


class LockPath(unittest.TestCase):
    def test_scripts_test_sh_s_or_one_of_this_users(self):
        # the one that scripts/test.sh gives its workers; without it, one in the temporary directory that only this
        # user's runs share, so that another user's file of that name does not stop them
        with mock.patch.dict(os.environ, {H.LOCK: "/run/x/engines.lock"}):
            self.assertEqual(H.lock_path(), "/run/x/engines.lock")
        with mock.patch.dict(os.environ, {H.LOCK: ""}):
            self.assertEqual(H.lock_path(),
                             os.path.join(tempfile.gettempdir(), f"strata-test-engines-{os.getuid()}.lock"))


class NotNow(unittest.TestCase):
    def setUp(self):
        # as on a machine that systemd runs, also in CI's container: the drivers are looked for first
        booted = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, booted, True)
        patcher = mock.patch.object(H, "BOOTED", str(booted))
        patcher.start()
        self.addCleanup(patcher.stop)

    def driver(self, d: Path, name: str, relative: bool, prefix: list | None = None) -> subprocess.Popen:
        (d / "bench").mkdir(parents=True, exist_ok=True)
        (d / "bench" / name).write_text("sleep 30\n")
        cmd = (prefix or []) + (["bash", f"./{name}"] if relative else ["bash", str(d / "bench" / name)])
        p = subprocess.Popen(cmd, cwd=d / "bench", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.addCleanup(lambda: (os.killpg(p.pid, signal.SIGKILL), p.wait()))
        return p

    def test_a_running_driver_stops_a_harness(self):
        for name, relative, prefix in (("ab_engine.sh", False, []), ("ab_engine.sh", True, []), ("ab_versions.sh", False, []),
                                       ("ab_engine.sh", False, ["nice", "-n", "19", "ionice", "-c3", "taskset", "-c", "0"])):
            with self.subTest(name=name, relative=relative, prefix=prefix):
                d = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, d, True)
                p = self.driver(d, name, relative, prefix)
                for _ in range(50):
                    why = H.not_now()
                    if why and str(p.pid) in why:
                        break
                    time.sleep(0.1)
                self.assertRegex(why or "", rf"an experiment driver runs \(pids ([0-9]+, )*{p.pid}\b")

    def test_a_driver_started_through_a_link_stops_a_harness(self):
        # by the file that its path leads to: a link elsewhere, also one without .sh in its name, or a path through a
        # linked directory
        for link, relative in (("run-ab.sh", False), ("run-ab.sh", True), ("run-ab", False), ("run-ab", True),
                               ("b/ab_x.sh", False)):
            with self.subTest(link=link, relative=relative):
                d = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, d, True)
                (d / "repo" / "bench").mkdir(parents=True)
                (d / "repo" / "bench" / "ab_x.sh").write_text("sleep 30\n")
                (d / "other").mkdir()
                if link.startswith("b/"):
                    os.symlink(d / "repo" / "bench", d / "other" / "b")
                else:
                    os.symlink(d / "repo" / "bench" / "ab_x.sh", d / "other" / link)
                p = subprocess.Popen(["bash", f"./{link}" if relative else str(d / "other" / link)], cwd=d / "other",
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                self.addCleanup(lambda p=p: (os.killpg(p.pid, signal.SIGKILL), p.wait()))
                for _ in range(50):
                    why = H.not_now()
                    if why and str(p.pid) in why:
                        break
                    time.sleep(0.1)
                self.assertRegex(why or "", rf"an experiment driver runs \(pids ([0-9]+, )*{p.pid}\b")

    def test_a_link_to_another_file_of_bench_does_not_stop_a_harness(self):
        # a driver is a bench/*.sh, also by where a link leads
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "repo" / "bench").mkdir(parents=True)
        (d / "repo" / "bench" / "notes.txt").write_text("notes\n")
        os.symlink(d / "repo" / "bench" / "notes.txt", d / "notes")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(d / "notes")],
                             start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.3)
        self.assertNotRegex(H.not_now() or "", rf"\b{p.pid}\b")

    def test_a_driver_in_a_harness_sandbox_does_not_stop_a_harness(self):
        # what a harness running beside (another test.sh) starts: it drives the sandbox's stub service, not strata-q3
        d = Path(tempfile.mkdtemp(prefix="stubs."))
        self.addCleanup(shutil.rmtree, d, True)
        p = self.driver(d / "x", "ab_engine.sh", relative=False)
        time.sleep(0.3)
        self.assertNotRegex(H.not_now() or "", rf"\b{p.pid}\b")

    def not_now_with(self, states: dict[str, str], booted: bool = True, no_service: str | None = None,
                     job: str = "", nvidia: bool | None = False) -> str | None:
        """not_now() with a systemctl that gives these units these states (inactive for the others) and strata-q3 this
        job (none: ""; None: systemctl show fails), on a machine that systemd runs or not, with a kernel that has the
        NVIDIA driver loaded or not (None: no modules to read), and no driver, whatever runs here."""
        stub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, stub, True)
        (stub / "modules" / "ext4").mkdir(parents=True)
        if nvidia:
            (stub / "modules" / "nvidia").mkdir()
        cases = "".join(f'  "is-active {u}") echo "{s}" ;;\n' for u, s in states.items())
        cases += ('  "show -p Job --value strata-q3.service") exit 1 ;;\n' if job is None else
                  f'  "show -p Job --value strata-q3.service") echo "{job}" ;;\n')
        (stub / "systemctl").write_text(f'#!/bin/bash\ncase "$*" in\n{cases}  *) echo inactive ;;\nesac\n')
        (stub / "systemctl").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{stub}:{os.environ['PATH']}"}), \
                mock.patch.object(H, "BOOTED", str(stub) if booted else str(stub / "none")), \
                mock.patch.object(H, "MODULES", str(stub / "modules") if nvidia is not None else str(stub / "none")), \
                mock.patch.object(H, "_cmdlines", lambda: iter(())):
            os.environ.pop(H.NO_SERVICE, None)
            if no_service is not None:
                os.environ[H.NO_SERVICE] = no_service
            return H.not_now()

    def test_states_of_strata_q3(self):
        # only a stopped service is safe: while it runs, a restart after a crash can come at any time
        for state in ("failed", "activating", "deactivating", "reloading", "active", "something else"):
            with self.subTest(state=state):
                self.assertIn(f"strata-q3 is {state}", self.not_now_with({"strata-q3.service": state}) or "")
        self.assertIn("strata-q3 is unreadable", self.not_now_with({"strata-q3.service": ""}) or "")
        self.assertIsNone(self.not_now_with({"strata-q3.service": "inactive"}))

    def test_a_queued_job_of_strata_q3(self):
        self.assertIn("strata-q3 has a job queued (17)", self.not_now_with({}, job="17") or "")
        self.assertIn("strata-q3 has a job queued (unreadable)", self.not_now_with({}, job=None) or "")
        self.assertIsNone(self.not_now_with({}, job=""))

    def test_the_rescue_timer_of_an_experiment(self):
        for state in ("active", "activating", "reloading", "failed", ""):
            with self.subTest(state=state):
                self.assertIn("strata-ab-rescue.timer is", self.not_now_with({"strata-ab-rescue.timer": state}) or "")

    def test_where_systemd_does_not_run(self):
        # CI's container, and a container on the machine, look alike to systemd: the flag tells them apart, and the
        # NVIDIA driver, which a container on the machine sees loaded
        busy = {"strata-q3.service": "active", "strata-ab-rescue.timer": "active"}
        self.assertIn("systemd does not run here", self.not_now_with(busy, booted=False) or "")
        self.assertIn("systemd does not run here", self.not_now_with(busy, booted=False, no_service="0") or "")
        self.assertIsNone(self.not_now_with(busy, booted=False, no_service="1"))
        for nvidia in (True, None):
            with self.subTest(nvidia=nvidia):
                self.assertIn("the kernel has the NVIDIA driver loaded, or its modules cannot be read",
                              self.not_now_with(busy, booted=False, no_service="1", nvidia=nvidia) or "")
        self.assertIsNone(self.not_now_with({}, nvidia=True))      # where systemd runs, what it says decides

    def test_no_systemctl(self):
        with mock.patch.dict(os.environ, {"PATH": str(Path(tempfile.gettempdir()) / "no-such-dir")}):
            self.assertEqual(H._state("strata-q3.service"), "unreadable")


class Gate(unittest.TestCase):
    def test_not_asked_for_is_a_skip_with_the_harness_reason(self):
        with mock.patch.dict(os.environ, {H.ASK: "0"}):
            with self.assertRaises(unittest.SkipTest) as e:
                H.gate(self, ("sh",))
        self.assertTrue(str(e.exception).startswith("harness:"), str(e.exception))

    def test_asked_for_a_missing_tool_fails(self):
        with mock.patch.dict(os.environ, {H.ASK: "1"}):
            with self.assertRaises(AssertionError) as e:
                H.gate(self, ("sh", "no-such-tool-for-this-test"))
        self.assertIn("needs no-such-tool-for-this-test", str(e.exception))

    def test_asked_for_but_not_now_fails(self):
        with mock.patch.dict(os.environ, {H.ASK: "1"}), mock.patch.object(H, "not_now", lambda: "strata-q3 is active"):
            with self.assertRaises(AssertionError) as e:
                H.gate(self, ("sh",))
        self.assertIn("not run now: strata-q3 is active", str(e.exception))


DRIVER = """import signal, sys
from pathlib import Path
signal.signal(signal.SIGINT, signal.default_int_handler)   # also when started with SIGINT ignored
sys.path.insert(0, sys.argv[1])
import harness_run as H
H.run(Path(sys.argv[2]) / 'harness.sh', Path(sys.argv[2]), 300)
"""


class Interrupt(unittest.TestCase):
    """A test that runs a harness, interrupted as a timeout that signals its process group or a Ctrl-C does: the
    harness's session is stopped and nothing that names its directory is left (before, the harness and its fake engines
    ran on).  Under scripts/deploy.sh, scripts/test.sh ends at the timeout's SIGTERM, and deploy.sh's own clean-up kills
    the harness by its directory (tests/test_deploy.py)."""

    def start(self, how, trap='echo "EXIT trap" >> "$1/trap"', more=""):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.marker = f"harness-left-{os.getpid()}-{how}"
        (d / "harness.sh").write_text(
            f"set -u\ntrap '{trap}' EXIT\n" + LEAK.format(marker=self.marker) + "\n" + more.format(marker=self.marker) +
            '\necho "$TMPDIR" > "$1/tmpdir"; sleep 120\n')
        (d / "driver.py").write_text(DRIVER)
        cmd = [sys.executable, str(d / "driver.py"), str(Path(H.__file__).parent), str(d)]
        if how == "timeout":                                # as in deploy.sh, timeout signals its process group
            cmd = ["timeout", "-k", "20", "3"] + cmd
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (os.killpg(p.pid, signal.SIGKILL) if p.poll() is None else None))
        for _ in range(100):
            if (d / "tmpdir").exists() and (d / "tmpdir").read_text().strip():
                break
            time.sleep(0.1)
        tmp = (d / "tmpdir").read_text().strip()
        return d, p, tmp

    def finish(self, d, p, tmp):
        p.wait(timeout=90)
        self.assertNotEqual(p.returncode, 0)
        time.sleep(0.3)
        self.assertEqual(named(tmp) + named(self.marker), [])
        self.assertIn("EXIT trap", (d / "trap").read_text())
        self.assertFalse(Path(tmp).exists())

    def test_sigterm(self):
        d, p, tmp = self.start("term")
        os.kill(p.pid, signal.SIGTERM)
        self.finish(d, p, tmp)

    def test_sighup(self):
        d, p, tmp = self.start("hup")
        os.kill(p.pid, signal.SIGHUP)
        self.finish(d, p, tmp)

    def test_sigint(self):
        d, p, tmp = self.start("int")
        os.kill(p.pid, signal.SIGINT)
        self.finish(d, p, tmp)

    def test_a_second_interrupt_during_the_clean_up(self):
        # a Ctrl-C, and another one while the harness's EXIT trap still runs: the clean-up is not cut short
        d, p, tmp = self.start("second", trap='sleep 3; echo "EXIT trap" >> "$1/trap"')
        os.kill(p.pid, signal.SIGINT)
        time.sleep(0.5)
        os.kill(p.pid, signal.SIGINT)
        self.finish(d, p, tmp)

    def test_a_timeout_that_signals_its_process_group(self):
        d, p, tmp = self.start("timeout")
        self.finish(d, p, tmp)

    def test_what_stays_in_its_session_is_killed_too(self):
        # a process of the harness's session, in a process group of its own and naming nothing of its directory: the
        # harness's SIGTERM (its process group) misses it, and the sweep after the interrupt kills it by the session
        d, p, tmp = self.start("session", more=(
            'python3 -c "import os, sys, time; os.setpgid(0, 0); open(sys.argv[1], \'w\'); time.sleep(60)" '
            '"$1/group.ready" {marker}-own-group > /dev/null 2>&1 &\n'
            'n=0; while [ ! -e "$1/group.ready" ] && [ $n -lt 200 ]; do n=$((n + 1)); sleep 0.05; done'))
        os.kill(p.pid, signal.SIGTERM)
        self.finish(d, p, tmp)

    def test_an_interrupt_ends_the_whole_run(self):
        # unittest records an Exception and goes on with the next test, which would start the next harness
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(H.sweep, str(d))
        (d / "harness.sh").write_text('set -u\necho "$TMPDIR" >> "$1/started"\nsleep 120\n')
        (d / "suite.py").write_text(
            "import sys, unittest\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\nimport harness_run as H\n"
            "D = Path(sys.argv[2])\n"
            "class T(unittest.TestCase):\n"
            "    def test_1(self):\n        H.run(D / 'harness.sh', D, 300)\n"
            "    def test_2(self):\n        H.run(D / 'harness.sh', D, 300)\n"
            "unittest.main(argv=['suite'])\n")
        p = subprocess.Popen([sys.executable, str(d / "suite.py"), str(Path(H.__file__).parent), str(d)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (os.killpg(p.pid, signal.SIGKILL) if p.poll() is None else None))
        for _ in range(100):
            if (d / "started").exists():
                break
            time.sleep(0.1)
        os.kill(p.pid, signal.SIGTERM)
        deadline = time.monotonic() + 60
        while p.poll() is None and time.monotonic() < deadline:
            if len((d / "started").read_text().splitlines()) > 1:
                break
            time.sleep(0.1)
        self.assertEqual(len((d / "started").read_text().splitlines()), 1, "the next test started a harness")
        self.assertIsNotNone(p.poll(), "the run did not end")


if __name__ == "__main__":
    unittest.main()
