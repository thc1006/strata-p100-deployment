"""bench/ab_engine.sh against stubs of the service manager, the journal, sudo and the GPUs, with fake servers on a free
port (tests/ab_engine_stubs.sh): nothing on the machine is read or changed."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Driver(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(p) for p in ("gcc", "setsid", "pgrep", "ss")), "needs gcc, setsid, pgrep and ss")
    def test_against_stubs(self):
        r = subprocess.run(["bash", str(ROOT / "tests" / "ab_engine_stubs.sh"), str(ROOT)], capture_output=True,
                           text=True, timeout=900)
        failed = [line for line in r.stdout.splitlines() if not line.startswith("PASS")]
        self.assertEqual(r.returncode, 0, "\n".join(failed) + r.stderr[-2000:])
        self.assertGreaterEqual(sum(line.startswith("PASS") for line in r.stdout.splitlines()), 52)


if __name__ == "__main__":
    unittest.main()
