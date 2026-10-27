"""bench/switch_model.sh against stubs of the service manager, the journal and sudo, with a fake server on a free port
(tests/switch_model_stubs.sh, run by tests/harness_run.py).  It changes no file outside its temporary directory, but
while it runs its fake engines make strata-preflight.sh refuse to start strata-q3, so it does not run beside an
experiment, a scheduled restore or the service.  CI runs it (scripts/test.sh --repo-only --harnesses); on this
machine it runs only with scripts/test.sh --harnesses, and only while strata-q3 is stopped (tests/harness_run.py:
gate, not_now)."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_run  # noqa: E402

TIMEOUT = 900
# every check of the harness, in order: a check that stops running, or a new one not listed here, fails the test
EXPECTED = [
    '--check changes nothing and does not restart',
    '--check names the model, its cuts and its files',
    'a switch to iq3_s exits 0',
    'profile.conf names --model iq3_s',
    "the live config is IQ3_S's, with its cuts",
    'one restart, whose preflight accepts the config',
    'the service answers as qwen3.8-flash-next-iq3_s, with one engine',
    "record: the restart's exit and state",
    'record: health',
    "record: the engine's arguments",
    "record: the engine's start-up lines",
    "record: the journal's lines",
    "record: check-deployment.sh's output",
    'no host name in the record or its check',
    'host names are written as <host>',
    'a restart on the same model leaves both files as they were',
    "a switch back to iq3_xxs: profile.conf, the config's cuts, the service",
    'refuses: to-s.txt exists',
    'refuses: usage: bench/switch_model.sh',
    'refuses: unknown model iq3_m',
    'refuses: strata-q3.service is failed',
    'refuses: with job 4711 queued',
    'refuses: but its model is not loaded',
    'refuses: has been serving requests for 0 s',
    'refuses: a measurement, evaluation, pack build or switch is running',
    '... and it names the process',
    'refuses: a measurement, evaluation, pack build or switch is running',
    'a remote evaluation pinned to one CPU may run beside',
    'refuses: a measurement, evaluation, pack build or switch is running',
    'refuses: strata-preflight.sh would refuse the start while these run',
    'refuses: zfs_arc_max is 8589934592',
    'refuses: sudo -n does not work',
    'refuses: scripts/check-deployment.sh finds a problem now',
    "refuses: config/profile.conf holds '--profile deployed --reserve-mib 2048'",
    'refuses: missing or empty: [^ ]*/packs/iq3_s/index.txt',
    'refuses: missing or empty: [^ ]*/models/IQ3_S/[^ ]*-IQ3_S-00001-of-00002.gguf',
    'refuses: the pack was built from',
    "refuses: experts.bin has 506 bytes; the pack's record says 505",
    "refuses: the tool cannot write iq3_s's config on a copy of the live config",
    'refuses: on a copy of the live config the tool does not give iq3_s with the cuts \\[13, 25, 38\\]',
    'a request that comes in before the restart: both files put back, no restart',
    'TERM before the restart: both files put back, no restart',
    'INT before the restart: both files put back, no restart',
    'QUIT before the restart: both files put back, no restart',
    'files that cannot be put back: exit 5, with their copies named, no restart',
    'a stray that starts before the restart: both files put back, no restart',
    'a measurement that starts before the restart: both files put back, no restart',
    'another start while the script waits for an idle service: refused, no restart',
    'another start after the files are written: both put back, no restart',
    'a Ctrl-C during the restart: the restart finishes and counts, no rollback',
    "the output's reader gone before the restart: the switch still finishes",
    'four starts within the hour before it: the switch still comes up',
    'an IQ3_S start that fails is switched back to IQ3_XXS (exit 3)',
    '... with one more restart, each after a reset-failed',
    "record: the failed start (systemd's auto-restart pending)",
    'record: the switch back',
    'when IQ3_XXS fails too, it prints the commands to bring it back (exit 4)',
    'a switch to iq3_xxs that does not come up prints the commands (exit 4)',
    'a restart systemctl calls failed is no switch, though the unit came up (exit 3)',
    'IQ3_S with a problem check-deployment.sh finds is switched back (exit 3)',
    'a check-deployment.sh that does not answer after the restart is a problem: switched back (exit 3)',
    'a start with no answer within SWITCH_RESTART_WAIT_S is switched back (exit 3)',
    'an engine left behind by a failed start: no second restart, the commands are printed (exit 4)',
    "no command line of the switch holds the preflight's text",
]


class Switch(unittest.TestCase):
    def test_against_stubs(self):
        harness_run.gate(self, ("gcc", "setsid", "taskset", "pgrep", "curl"))
        result = harness_run.run(ROOT / "tests" / "switch_model_stubs.sh", ROOT, TIMEOUT)
        harness_run.check(self, result, EXPECTED, TIMEOUT)


if __name__ == "__main__":
    unittest.main()
