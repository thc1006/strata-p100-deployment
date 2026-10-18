"""scripts/host-units.sh check, against a stub systemctl: no systemd, root or network needed."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "host-units.sh"
STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    # systemctl [--user] is-enabled|is-active UNIT, get-default: answers from $STUB_STATES ("scope unit enabled active")
    scope=system; [ "$1" = --user ] && { scope=user; shift; }
    case $1 in
      get-default) cat "$STUB_DEFAULT"; exit 0 ;;
      is-enabled) col=3 ;;
      is-active) col=4 ;;
      *) exit 1 ;;
    esac
    v=$(awk -v s="$scope" -v u="$2" -v c="$col" '$1 == s && $2 == u {print $c}' "$STUB_STATES")
    [ -n "$v" ] && echo "$v"
    [ "$v" = enabled ] || [ "$v" = active ]
    """)


class HostUnitsCheck(unittest.TestCase):
    def run_check(self, conf: str, states: str, default: str = "multi-user.target"):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "systemctl").write_text(STUB)
            (t / "systemctl").chmod(0o755)
            (t / "host-units.conf").write_text(conf)
            (t / "states").write_text(states)
            (t / "default").write_text(default + "\n")
            env = dict(os.environ, PATH=f"{t}:{os.environ['PATH']}", HOST_UNITS_CONF=str(t / "host-units.conf"),
                       STUB_STATES=str(t / "states"), STUB_DEFAULT=str(t / "default"))
            return subprocess.run(["bash", str(SCRIPT), "check"], env=env, capture_output=True, text=True, timeout=30)

    CONF = textwrap.dedent("""\
        # comment
        default system multi-user.target   # no desktop
        disabled system cups.service
        masked system fwupd.service
        masked user pipewire.socket
        """)
    GOOD = "system cups.service disabled inactive\nsystem fwupd.service masked inactive\nuser pipewire.socket masked inactive\n"

    def test_all_in_their_states_passes(self):
        r = self.run_check(self.CONF, self.GOOD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("ok "), 4, r.stdout)

    def test_each_kind_of_difference_is_drift(self):
        cases = {
            "enabled again": self.GOOD.replace("cups.service disabled", "cups.service enabled"),
            "disabled but running": self.GOOD.replace("cups.service disabled inactive", "cups.service disabled active"),
            "unmasked": self.GOOD.replace("fwupd.service masked", "fwupd.service static"),
            "user unit unreadable": self.GOOD.replace("user pipewire.socket masked inactive\n", ""),
        }
        for name, states in cases.items():
            with self.subTest(name):
                r = self.run_check(self.CONF, states)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("DRIFT", r.stdout)
        r = self.run_check(self.CONF, self.GOOD, default="graphical.target")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DRIFT  default target is graphical.target", r.stdout)

    def test_bad_or_empty_lists_fail_closed(self):
        for conf in ("enabled system cups.service\n", "masked elsewhere cups.service\n", "# nothing\n", ""):
            with self.subTest(conf=conf):
                self.assertEqual(self.run_check(conf, self.GOOD).returncode, 1)

    def test_the_repository_list_parses(self):
        """Every line of config/host-units.conf is a known state and scope (no stub answers: all are drift)."""
        conf = (SCRIPT.parents[1] / "config" / "host-units.conf").read_text()
        r = self.run_check(conf, "")
        self.assertNotIn("BAD", r.stdout)
        lines = [l for l in conf.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(r.stdout.count("DRIFT") + r.stdout.count("ok "), len(lines), r.stdout)


if __name__ == "__main__":
    unittest.main()
