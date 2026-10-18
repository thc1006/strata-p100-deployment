"""bench/ab_model_run.sh against stubs of the service, the journal, sudo and the GPUs (tests/ab_model_run_stubs.sh):
nothing on the machine is read or changed."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Wrapper(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(p) for p in ("gcc", "git", "setsid", "pgrep")), "needs gcc, git, setsid and pgrep")
    def test_against_stubs(self):
        r = subprocess.run(["bash", str(ROOT / "tests" / "ab_model_run_stubs.sh"), str(ROOT)], capture_output=True,
                           text=True, timeout=600)
        failed = [line for line in r.stdout.splitlines() if not line.startswith("PASS")]
        self.assertEqual(r.returncode, 0, "\n".join(failed) + r.stderr)
        # every check of the harness: a harness that stops early passes fewer
        self.assertGreaterEqual(sum(line.startswith("PASS") for line in r.stdout.splitlines()), 64)


if __name__ == "__main__":
    unittest.main()
