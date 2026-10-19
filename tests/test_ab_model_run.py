"""bench/ab_model_run.sh against stubs of the service, the journal, sudo and the GPUs (tests/ab_model_run_stubs.sh, run
by tests/harness_run.py).  It changes no file outside its temporary directory, but while it runs its fake engine makes
strata-preflight.sh refuse to start strata-q3, so it does not run beside an experiment, a scheduled restore or a start
of the service.  CI's container has no gcc, so CI skips it; on the machine, scripts/test.sh runs it, also when
scripts/deploy.sh checks a commit."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run  # noqa: E402

TIMEOUT = 600
# every check of the harness, in order: a check that stops running, or a new one not listed here, fails the test
EXPECTED = [
    'clean run exits 0',
    'arm record: service ready for 1 s',
    'arm record: deployment code sha256 [0-9a-f]{16} ',
    'arm record: perf set sha256 [0-9a-f]{16} ',
    'arm record: profile.conf: --profile deployed --model iq3_xxs',
    "arm record: without the model's fields [0-9a-f]{16}",
    'arm record: serve_load nonce ab-0004',
    'arm record: engine env: CUDA_VISIBLE_DEVICES=0,1,2,3 STRATA_RESIDENT_HEADROOM_GIB=6 $',
    'arm record: tracked files changed: config/profile.conf',
    'arm record: metrics requests before the run: 142',
    'arm record: host units: ok',
    'arm record: load: strata generate: expert cache 6656 slots',
    'arm record: load: strata serve: layer split: 100% of the experts resident',
    'arm record: load: strata generate: session is up',
    'arm record: ^set: bench/run_profile.sh$',
    'arm record: ^conversation cache before the run: parked 2 bytes 1048576$',
    'arm record: load: FileExpertSource: mapped pinned cache complement ready: resident 1.12 GiB, pinned 1.12 GiB',
    'arm record: load: strata generate: resident RAM mode: 1.12 GiB of experts in RAM',
    'placement stops at the first request',
    'run_profile.sh got the nonce',
    'after record: run_profile exit 0  interrupted no',
    'after record: same engine as before: yes',
    'after record: idle at the end: yes',
    'after record: metrics requests during the run: 98',
    'after record: engine log lines during the run: 1',
    'after record: journal: system; lines during the run: 1',
    'after record: journal done lines during the run: 98 \\(length,cancel=False 44, stop,cancel=False 54\\)',
    'after record: kernel lines during the run: 1',
    'after record: ^concurrent read lines during the run: 2, with slots decoding: 1$',
    'after record: ^concurrent read off beside: no such line$',
    'journal: the slot leak kept, the slot wait left out',
    'kernel: the Xid kept',
    'every host name redacted, in any case, the short netbird label too',
    'redacted as <host>',
    'a results commit leaves the code hash',
    'a model switch in profile.conf leaves the code hash',
    'the switched profile.conf is recorded',
    'a code change changes the code hash',
    "the model's fields are left out of the config hash",
    'another server setting changes the model-free config hash',
    'refuses: became ready 120 s ago',
    "refuses: is younger than the service's ready time",
    'refuses: the service is not idle',
    'refuses: the service is not idle',
    'refuses: t-leftover-perf.log exists',
    'refuses: is in results/stress.jsonl',
    'refuses: the host units are not as config/host-units.conf sets them',
    'refuses: the ZFS ARC cap is 8589934592',
    'refuses: STRATA_RESIDENT_HEADROOM_GIB 8 and',
    'refuses: conversation-cache-min-free-mib 8192,',
    'refuses: usage: bench/ab_model_run.sh',
    'a 99th request is counted',
    'a request in flight at the end is recorded',
    'an interrupt exits 130',
    "the perf set's processes are gone",
    'the after record says interrupted',
    'an early interrupt exits 130',
    'an early interrupt leaves nothing running',
    'an early interrupt writes the after record',
    'a robustness run exits 0',
    'arm record: set: bench/run_robustness.sh',
    'after record: run_robustness exit 0',
    'the Tracebacks are counted in the robustness log',
    'run_robustness.sh got the tag',
    'the perf set did not run',
    'an engine started again during the run is not the same',
    'an engine that keeps the concurrent read off: no reads, and its reason',
]


class Wrapper(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(p) for p in ("gcc", "git", "setsid", "pgrep", "curl")), "needs gcc, git, setsid, pgrep and curl")
    def test_against_stubs(self):
        why = harness_run.not_now()
        if why:
            self.fail(f"not run now: {why}")
        result = harness_run.run(ROOT / "tests" / "ab_model_run_stubs.sh", ROOT, TIMEOUT)
        harness_run.check(self, result, EXPECTED, TIMEOUT)


if __name__ == "__main__":
    unittest.main()
