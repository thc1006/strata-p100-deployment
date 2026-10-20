"""bench/run_robustness.sh against stubs of the sets it runs, the journal and the host's names
(tests/run_robustness_stubs.sh, run by tests/harness_run.py).  No fake engine runs and nothing outside the harness's
temporary directory changes, so it runs at any time, in CI too."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run  # noqa: E402

TIMEOUT = 120
# every check of the harness, in order: a check that stops running, or a new one not listed here, fails the test
EXPECTED = [
    'a run exits 0',
    'the log has every section, in order',
    'the monitor is summarized',
    'the monitor is stopped',
    'the journal is read from the start of the run',
    "the journal's lines are kept, every host name written as <host>",
    'no host name is left in what the run wrote',
    'the engine log counts only the lines of this run',
    'refuses: the tag exists',
    'refuses: no host name to redact; no log, no monitor left',
    'a stopped run keeps the lines written until then',
    'a stopped run leaves nothing running',
]


class Robustness(unittest.TestCase):
    @unittest.skipUnless(shutil.which("setsid"), "needs setsid")
    def test_against_stubs(self):
        result = harness_run.run(ROOT / "tests" / "run_robustness_stubs.sh", ROOT, TIMEOUT)
        harness_run.check(self, result, EXPECTED, TIMEOUT)


if __name__ == "__main__":
    unittest.main()
