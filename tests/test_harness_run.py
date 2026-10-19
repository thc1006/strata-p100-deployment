"""tests/harness_run.py with harnesses of a few lines: what it reports when one passes, prints a FAIL line, misses or
adds a check, has no count line, runs past its time or leaves a process behind, and when it will not start."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run as H  # noqa: E402


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
        # a process of its own session that names the directory, as a harness's fake engines do
        r = self.result('trap \'echo "its EXIT trap ran" >&2\' EXIT\nsetsid bash -c "sleep 60; :" "$TMPDIR/fake" & sleep 60',
                        timeout=1)
        self.assertIsNone(r["status"])
        self.assertIn("its EXIT trap ran", r["err"])      # SIGTERM first: a harness stops what it started
        self.refused(r, [], "ran past")
        time.sleep(0.2)
        alive = [pid for pid, cmd in H._cmdlines() if any(r["dir"].encode() in part for part in cmd)]
        self.assertEqual(alive, [])

    def test_a_process_left_behind_is_killed_and_reported(self):
        r = self.result('setsid bash -c "sleep 60; :" "$TMPDIR/fake" > /dev/null 2>&1 & echo "PASS a"; echo "0 failed"')
        self.assertEqual(len(r["left"]), 1, r["left"])
        self.refused(r, ["a"], "outlived the harness")
        time.sleep(0.2)
        self.assertEqual([pid for pid, cmd in H._cmdlines() if any(r["dir"].encode() in part for part in cmd)], [])


class NotNow(unittest.TestCase):
    def test_a_running_driver_stops_a_harness(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "bench").mkdir()
        (d / "bench" / "ab_engine.sh").write_text("sleep 30\n")
        p = subprocess.Popen(["bash", str(d / "bench" / "ab_engine.sh")], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (os.killpg(p.pid, signal.SIGKILL), p.wait()))
        for _ in range(50):
            why = H.not_now()
            if why:
                break
            time.sleep(0.1)
        self.assertRegex(why or "", rf"an experiment driver runs \(pids ([0-9]+, )*{p.pid}\b")


if __name__ == "__main__":
    unittest.main()
