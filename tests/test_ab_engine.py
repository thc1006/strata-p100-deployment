"""bench/ab_engine.sh against stubs of the service manager, the journal, sudo and the GPUs, with fake servers on a free
port (tests/ab_engine_stubs.sh, run by tests/harness_run.py).  It changes no file outside its temporary directory, but
while it runs its fake engines make strata-preflight.sh refuse to start strata-q3, so it does not run beside an
experiment, a scheduled restore or a start of the service.  CI's container has no gcc, so CI skips it; on the machine,
scripts/test.sh runs it, also when scripts/deploy.sh checks a commit."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run  # noqa: E402

TIMEOUT = 900
# every check of the harness, in order: a check that stops running, or a new one not listed here, fails the test
EXPECTED = [
    'two clean runs exit 0',
    'c0-1: arm check ok',
    'c0-1: same engine',
    'c0-1: counted = sent',
    'c1-1: arm check ok',
    'c1-1: same engine',
    'c1-1: counted = sent',
    "C1's group-sort line is in its record",
    'C0 has no group-sort line',
    "C1's engine environment recorded",
    "every answer's text is recorded",
    "each GPU's use is recorded during the screen, and the recorder is stopped",
    "c0-1's screen records",
    'strata-q3 runs again at the end',
    'the test unit is stopped',
    'the rescue timer was armed and is stopped after a normal run',
    "the summary: usable, C1's answers equal C0's",
    'no host name in results/',
    'a spec that does not match its config is refused, strata-q3 untouched',
    'a switch that did not take stops the step',
    '... and nothing was measured',
    '... and strata-q3 runs again',
    'other traffic shows as counted != sent',
    'the summary leaves that run out',
    'a busy service is not stopped',
    'an interrupt exits 130',
    '... the GPU recorder is stopped',
    '... the test unit is stopped',
    '... and strata-q3 runs again',
    'a used tag is refused',
    "an engine without the arm's environment stops the step",
    '... and nothing was measured',
    'an engine restart during the measurement stops the step',
    '... and strata-q3 runs again',
    'a running measurement is refused, strata-q3 untouched',
    'a stray engine command line is refused, strata-q3 untouched',
    'a passing stray is waited for, then strata-q3 starts',
    'a stray that stays is named with the command to start strata-q3',
    'a stopped strata-q3 is refused and left as it is',
    'another GPU process is refused, strata-q3 untouched',
    'a tag given twice is refused',
    'arms whose configs differ elsewhere are refused',
    'an unreadable journal is recorded and makes the run unusable',
    'a reply still running at the end is counted and the run left out',
    'an engine error line is counted, the verify line is not, and the run is left out',
    'a GPU still busy at the end does not keep strata-q3 down',
    'a second signal does not stop the restore',
    'a killed driver is rescued by the timer',
    "an arm's declared args_add is accepted",
    'a remote evaluation pinned to one CPU may run beside',
    'an unpinned remote evaluation is refused',
    'a screen with the opt-in phases is counted and summarized',
    'an arms file with nomust is refused, strata-q3 untouched',
    'an arms file with noenv is refused, strata-q3 untouched',
    'an arms file with typo is refused, strata-q3 untouched',
    'an arm without must_not runs, with no error output',
    'an interrupt during the load stops the test unit at once, with no error output',
    "the step's journal and kernel lines are read from the driver's own mark: the first ones kept",
    'no mark in the journal: the step stops before the test unit starts, strata-q3 comes back',
    "an arm's declared args_set is accepted, and its engine gets the value",
    'an args_set with value is refused, strata-q3 untouched',
    'an args_set with absent is refused, strata-q3 untouched',
    'an args_set with noflag is refused, strata-q3 untouched',
    'an args_set with twice is refused, strata-q3 untouched',
    'an args_set with unsetdiffer is refused, strata-q3 untouched',
    'an args_set with elsewhere is refused, strata-q3 untouched',
    'an args_set with addrepeat is refused, strata-q3 untouched',
    'a config changed during an earlier step stops the next step, strata-q3 comes back',
]


class Driver(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(p) for p in ("gcc", "setsid", "pgrep", "ss", "curl")), "needs gcc, setsid, pgrep, ss and curl")
    def test_against_stubs(self):
        why = harness_run.not_now()
        if why:
            self.fail(f"not run now: {why}")
        result = harness_run.run(ROOT / "tests" / "ab_engine_stubs.sh", ROOT, TIMEOUT)
        harness_run.check(self, result, EXPECTED, TIMEOUT)


if __name__ == "__main__":
    unittest.main()
