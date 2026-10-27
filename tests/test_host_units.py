"""scripts/host-units.sh check and apply, against stand-ins for systemctl and sudo: no systemd, root or network
needed.  What keeps the system's own systemctl and sudo from running is that none is found: both modes run with a
PATH of the stand-ins and a few programs only (sudo needs no password on this machine), and the script names no
program by its path, sets no PATH and reads no file that could set one (test_it_sets_no_path).  Should one run all the
same, it would run under setpriv --no-new-privs, so that a sudo of the system's could not gain root, and the system
manager would take a change from this user only as polkit allows it: no variable here keeps a systemctl from the
system manager's socket.  The user's manager is kept away by the environment: no D-Bus address, and an
XDG_RUNTIME_DIR of the test's own that is empty (the stand-in systemctl records what it gets of them, and the tests
check it).  Nor does a bash option or function reach the script from the environment.  The stand-in sudo runs the
stand-in systemctl and nothing else; the stand-in systemctl logs every call but is-enabled, is-active and
get-default, and a check must make none of those."""
from __future__ import annotations

import itertools
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "host-units.sh"
STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    # systemctl [--user] is-enabled|is-active UNIT, get-default: answers from $STUB_STATES ("scope unit enabled active");
    # the user instance answers through XDG_RUNTIME_DIR: the one $STUB_XDG names, when it is set.  Every other call, one
    # that may change a unit, is logged to $STUB_LOG ("scope words...") and fails
    [ -z "${STUB_EAT_STDIN:-}" ] || cat > /dev/null          # a stand-in that reads its standard input to the end
    [ -z "${STUB_STATUS:-}" ] || cat /proc/self/status > "$STUB_STATUS"     # what the script's programs run as
    # what it gets of the variables that lead a systemctl to a manager: the names of D-Bus's, and XDG_RUNTIME_DIR
    [ -z "${STUB_SEEN:-}" ] || { for v in $(compgen -e); do case $v in DBUS_*) echo "$v" ;; esac; done
                                 echo "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR-(not set)}"; } >> "$STUB_SEEN"
    scope=system
    [ "$1" = --user ] && { scope=user; shift; }
    case $1 in is-enabled|is-active|get-default) ;; *) echo "$scope $*" >> "$STUB_LOG" ;; esac
    [ "$scope" = system ] || { [ -n "${XDG_RUNTIME_DIR-}" ] && [ "$XDG_RUNTIME_DIR" = "${STUB_XDG:-$XDG_RUNTIME_DIR}" ]; } || exit 1
    case $1 in
      get-default) cat "$STUB_DEFAULT"; exit 0 ;;
      is-enabled) col=3 ;;
      is-active) col=4 ;;
      *) exit 1 ;;
    esac
    v=$(STUB_UNIT=$2 awk -v s="$scope" -v c="$col" '$1 == s && $2 == ENVIRON["STUB_UNIT"] {print $c}' "$STUB_STATES")
    [ -n "$v" ] && echo "$v"
    [ "$v" = enabled ] || [ "$v" = active ]
    """)
# apply's: mask|disable --now UNIT and set-default UNIT then put the unit in that state (the last line of $STUB_STATES
# that names a unit is its state); and a sudo that logs and runs the rest, the stub.  (awk gets the unit's name through
# its environment: -v would read its backslashes as escapes)
def _stub_for_apply(stub: str) -> str:
    for old, new in (("  *) exit 1 ;;\nesac\n",
                      '  mask|disable) [ "$2" = --now ] || exit 1\n'
                      '    if [ "$1" = mask ]; then s=masked; else s=disabled; fi\n'
                      '    echo "$scope $3 $s inactive" >> "$STUB_STATES"\n'
                      '    echo "Removed /etc/systemd/system/x.wants/$3." >&2; echo "(as systemctl says)"; exit 0 ;;\n'
                      '  set-default) echo "$2" > "$STUB_DEFAULT"; exit 0 ;;\n'
                      "  *) exit 1 ;;\nesac\n"),
                     ("'$1 == s && $2 == ENVIRON[\"STUB_UNIT\"] {print $c}'",
                      "'$1 == s && $2 == ENVIRON[\"STUB_UNIT\"] {v = $c} END {if (v != \"\") print v}'")):
        if stub.count(old) != 1:
            raise AssertionError(f"the stub has no {old!r}")
        stub = stub.replace(old, new)
    return stub


APPLY_STUB = _stub_for_apply(STUB)
SUDO = textwrap.dedent("""\
    #!/usr/bin/env bash
    # a stand-in for sudo: it logs what it is asked, and runs the stand-in systemctl beside it, nothing else
    echo "sudo $*" >> "$STUB_LOG"
    [ "${1:-}" = systemctl ] || { echo "the stand-in sudo runs only the stand-in systemctl, not: ${1:-nothing}" >&2; exit 1; }
    shift
    exec "$(dirname "$0")/systemctl" "$@"
    """)
# what bash takes from the environment before it reads a script, and the user's D-Bus: none of it reaches host-units.sh
SHELL_ENV = ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS")


class HostUnitsCheck(unittest.TestCase):
    def run_check(self, conf: str, states: str, default: str = "multi-user.target", env: dict | None = None,
                  mode: str = "check", log: list | None = None, mode_of_list: int | None = None,
                  relative: bool = False, seen: list | None = None, by_path: bool = False):
        """host-units.sh MODE against the stand-ins, with a PATH of them and a few programs only (both modes), under
        setpriv --no-new-privs; log gets what the stand-ins were asked to do, which in check mode must be nothing.
        relative: run as scripts/host-units.sh from the repository, with its own list.  seen gets what the stand-in
        systemctl got of D-Bus's variables and XDG_RUNTIME_DIR, a line a call: never a D-Bus address, nor the
        caller's XDG_RUNTIME_DIR (unless env passes it on).  by_path: run by its path, as its first line says (bash
        -p), not as bash SCRIPT."""
        given = dict(env or {})
        setpriv = shutil.which("setpriv")
        if setpriv is None:
            self.skipTest("machine: no setpriv (util-linux) here")
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for name, text in (("systemctl", APPLY_STUB), ("sudo", SUDO)):
                (t / name).write_text(text)
                (t / name).chmod(0o755)
            (t / "host-units.conf").write_text(conf)
            if mode_of_list is not None:
                (t / "host-units.conf").chmod(mode_of_list)
            (t / "states").write_text(states)
            (t / "default").write_text(default + "\n")
            (t / "log").write_text("")
            tools = t / "tools"
            tools.mkdir()
            for name in ("bash", "awk", "cat", "dirname", "id"):
                os.symlink(shutil.which(name), tools / name)
            path = f"{t}:{tools}"
            for name in ("sudo", "systemctl"):              # the stand-ins, and nothing else of those names
                self.assertEqual(shutil.which(name, path=path), str(t / name))
            (t / "xdg").mkdir()                             # an XDG_RUNTIME_DIR with no bus in it
            env = {**{k: v for k, v in os.environ.items() if k not in SHELL_ENV and not k.startswith("BASH_FUNC_")},
                   "PATH": path, "HOST_UNITS_CONF": None if relative else str(t / "host-units.conf"),
                   "STUB_STATES": str(t / "states"), "STUB_DEFAULT": str(t / "default"), "XDG_RUNTIME_DIR": str(t / "xdg"),
                   "STUB_LOG": str(t / "log"), "STUB_SEEN": str(t / "seen"), **(env or {})}
            env = {k: v for k, v in env.items() if v is not None}
            script = ["scripts/host-units.sh"] if relative else [str(SCRIPT)]
            shell = [] if by_path else [shutil.which("bash")]
            r = subprocess.run([setpriv, "--no-new-privs", "--", *shell, *script, mode], env=env,
                               cwd=SCRIPT.parents[1] if relative else None, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=30)
            calls = (t / "log").read_text().splitlines()
            got = (t / "seen").read_text().splitlines() if (t / "seen").exists() else []
            self.assertEqual([l for l in got if l.startswith("DBUS_")], [], r.stdout + r.stderr)
            caller = os.environ.get("XDG_RUNTIME_DIR")
            if caller and given.get("XDG_RUNTIME_DIR") != caller:
                self.assertNotIn(f"XDG_RUNTIME_DIR={caller}", got)
            if seen is not None:
                seen.extend(got)
            if mode == "check":                             # a check changes no unit, with sudo or without
                self.assertEqual(calls, [], r.stdout + r.stderr)
            if log is not None:
                log.extend(calls)
            return r

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
            "is-active unreadable": self.GOOD.replace("cups.service disabled inactive", "cups.service disabled"),
            "reloading": self.GOOD.replace("cups.service disabled inactive", "cups.service disabled reloading"),
            "deactivating": self.GOOD.replace("cups.service disabled inactive", "cups.service disabled deactivating"),
            "activating": self.GOOD.replace("cups.service disabled inactive", "cups.service disabled activating"),
        }
        for name, states in cases.items():
            with self.subTest(name):
                r = self.run_check(self.CONF, states)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("DRIFT", r.stdout)
        r = self.run_check(self.CONF, self.GOOD, default="graphical.target")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DRIFT  default target is graphical.target", r.stdout)

    def test_the_user_instance_without_xdg_runtime_dir(self):
        # a cron job's environment: the user units are read through the account's runtime directory, when it has one
        with tempfile.TemporaryDirectory() as run_user:
            mine = Path(run_user) / str(os.getuid())
            mine.mkdir()
            r = self.run_check(self.CONF, self.GOOD, env={"XDG_RUNTIME_DIR": None, "HOST_UNITS_RUN_USER": run_user,
                                                          "STUB_XDG": str(mine)})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # one that is set is kept
            r = self.run_check(self.CONF, self.GOOD, env={"XDG_RUNTIME_DIR": "/run/user/kept", "STUB_XDG": "/run/user/kept",
                                                          "HOST_UNITS_RUN_USER": run_user})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = self.run_check(self.CONF, self.GOOD, env={"XDG_RUNTIME_DIR": None,
                                                          "HOST_UNITS_RUN_USER": str(Path(run_user) / "none")})
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("DRIFT  user pipewire.socket is unreadable", r.stdout)

    def test_a_failed_unit_is_not_running(self):
        r = self.run_check(self.CONF, self.GOOD.replace("cups.service disabled inactive", "cups.service disabled failed"))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_bad_or_empty_lists_fail_closed(self):
        for conf in ("enabled system cups.service\n", "masked elsewhere cups.service\n", "# nothing\n", ""):
            with self.subTest(conf=conf):
                self.assertEqual(self.run_check(conf, self.GOOD).returncode, 1)
        # a state or a scope it does not take is a BAD line, not a unit it checks (a user's default target is none)
        for conf in ("enabled system cups.service\n", "default user graphical.target\n"):
            with self.subTest(conf=conf):
                r = self.run_check(conf, "system cups.service enabled inactive\n", default="graphical.target")
                self.assertEqual((r.returncode, r.stdout), (1, f"BAD    line: {conf}"), r.stdout + r.stderr)

    def test_a_last_line_without_a_newline(self):
        # an editor may write none at the end: the last line is read as the others are, in check and in apply
        conf = "masked system fwupd.service\nmasked system cups.service"
        states = "system fwupd.service masked inactive\nsystem cups.service enabled active\n"      # cups: drift
        r = self.run_check(conf, states)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("DRIFT  system cups.service is enabled / active, want masked and not running", r.stdout)
        r = self.run_check(conf, states.replace("cups.service enabled active", "cups.service masked inactive"))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.count("ok "), 2, r.stdout)
        r = self.run_check(conf + "\n# the end", states.replace("cups.service enabled active", "cups.service masked "
                                                                "inactive"))
        self.assertEqual((r.returncode, r.stdout.count("ok ")), (0, 2), r.stdout)    # a comment there is a comment
        log = []
        r = self.run_check(conf, states, mode="apply", log=log)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sudo systemctl mask --now cups.service", log)
        self.assertIn("ok     system cups.service masked, inactive", r.stdout)
        log = []
        r = self.run_check(conf + "\nmasked user pipewire.socket", states, mode="apply", log=log)    # the user's: no sudo
        self.assertIn("user mask --now pipewire.socket", log)
        self.assertNotIn("sudo systemctl --user mask --now pipewire.socket", log)
        self.assertIn("ok     user pipewire.socket masked, inactive", r.stdout)

    def test_a_list_that_cannot_be_read(self):
        # missing, or not a regular file (a FIFO would hang the loop): a DRIFT line, as for a unit, and at once
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "fifo.conf"
            os.mkfifo(fifo)
            for conf in (fifo, Path(tmp) / "missing.conf", Path(tmp)):
                with self.subTest(conf=conf.name):
                    r = self.run_check("", self.GOOD, env={"HOST_UNITS_CONF": str(conf)})
                    self.assertEqual(r.returncode, 1, r.stdout)
                    self.assertEqual(r.stdout, f"DRIFT  {conf} cannot be read, or is not a regular file: no unit checked\n")

    def test_a_last_line_of_one_word_without_a_newline(self):
        # read too, also when it has no unit (a line it cannot use: BAD)
        r = self.run_check("masked system fwupd.service\nbogus", "system fwupd.service masked inactive\n")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("BAD    line: bogus", r.stdout)

    def test_a_list_it_may_not_read(self):
        # a regular file that this user cannot read: said at once, as a missing one is
        if os.geteuid() == 0:
            self.skipTest("machine: root reads a file of mode 0")
        r = self.run_check(self.CONF, self.GOOD, mode_of_list=0)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertRegex(r.stdout, r"\ADRIFT  \S+/host-units\.conf cannot be read, or is not a regular file: no unit "
                                   r"checked\n\Z")

    def test_apply_sets_each_state_with_sudo(self):
        # the default target and the system units with sudo, a user unit without it; disable and mask with --now
        conf = "default system multi-user.target\ndisabled system cups.service\nmasked system fwupd.service\n" \
               "masked user pipewire.socket"
        states = ("system cups.service enabled active\nsystem fwupd.service enabled active\n"
                  "user pipewire.socket enabled active\n")
        log = []
        r = self.run_check(conf, states, default="graphical.target", mode="apply", log=log)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(log, ["sudo systemctl set-default multi-user.target", "system set-default multi-user.target",
                               "sudo systemctl disable --now cups.service", "system disable --now cups.service",
                               "sudo systemctl mask --now fwupd.service", "system mask --now fwupd.service",
                               "user mask --now pipewire.socket"])
        self.assertEqual(r.stdout.count("ok "), 4, r.stdout)
        # what systemctl says of a change (Removed ..., Created symlink ...) is not the script's: it prints only its
        # own lines
        self.assertEqual(([l for l in r.stdout.splitlines() if not l.startswith("ok ")], r.stderr), ([], ""))

    def test_what_it_runs_does_not_read_the_list(self):
        # the list is not the standard input of what the loop runs: a systemctl (or a sudo that logs its input) that
        # reads its standard input to the end leaves the other lines to the loop
        r = self.run_check(self.CONF, self.GOOD, env={"STUB_EAT_STDIN": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("ok "), 4, r.stdout)

    def test_the_stand_in_sudo_runs_only_the_stand_in_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "sudo").write_text(SUDO)
            (t / "sudo").chmod(0o755)
            (t / "log").write_text("")
            r = subprocess.run([str(t / "sudo"), "id"], env={"PATH": os.environ["PATH"], "STUB_LOG": str(t / "log")},
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(r.stdout, "")                  # id did not run
            self.assertEqual((t / "log").read_text(), "sudo id\n")

    def test_it_sets_no_path(self):
        # the tests keep the system's sudo and systemctl away through PATH (and setpriv): a PATH line of the script
        # would undo it, and so would a sudo or a systemctl named by its path, or a file it sources (. /etc/environment)
        code = [l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#")]
        self.assertEqual([l for l in code if re.search(r"\bPATH\+?=|\bexport\b.*\bPATH\b|\bdeclare\b.*\bPATH\b", l)], [])
        self.assertEqual([l for l in code if re.search(r"/(sudo|systemctl)\b", l)], [])
        self.assertEqual([l for l in code if re.search(r"(^|[;&|({]|\bthen|\bdo|\belse)\s*(\.|source)\s", l)], [])

    def test_the_stand_ins_get_no_bus_and_not_the_callers_runtime_dir(self):
        # a caller's D-Bus addresses and XDG_RUNTIME_DIR, which would lead a systemctl --user to its manager: neither
        # reaches what the script runs, in check or in apply
        caller = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/session-bus",
                  "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/system-bus",
                  "XDG_RUNTIME_DIR": "/nonexistent/callers-runtime-dir"}
        with mock.patch.dict(os.environ, caller):
            for mode in ("check", "apply"):
                with self.subTest(mode):
                    seen = []
                    r = self.run_check(self.CONF, self.GOOD, mode=mode, seen=seen)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertGreater(len(seen), 0)
                    self.assertEqual([l for l in seen if "DBUS" in l or "callers-runtime-dir" in l], [], seen)
                    self.assertEqual(len(set(seen)), 1, seen)       # one XDG_RUNTIME_DIR: the test's own
                    self.assertTrue(seen[0].startswith("XDG_RUNTIME_DIR=/") and seen[0].endswith("/xdg"), seen)

    def test_what_it_runs_gains_no_privileges(self):
        # under setpriv --no-new-privs: a sudo (setuid) would not gain root, even one of the system's
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status"
            r = self.run_check(self.CONF, self.GOOD, env={"STUB_STATUS": str(status)})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("NoNewPrivs:\t1\n", status.read_text())

    def test_functions_and_options_from_the_environment(self):
        # a function named systemctl or sudo would answer for them, and errexit would end the script at the first
        # unit that is not enabled: run as bash SCRIPT, it removes the functions (exported, or a BASH_ENV file's) and
        # turns the options off, also when a function stands in for set; one it cannot remove stops it.  Run by its
        # path, bash -p takes none of them
        with tempfile.TemporaryDirectory() as tmp:
            bash_env = Path(tmp) / "bash_env.sh"
            bash_env.write_text("systemctl() { echo enabled; }\nsudo() { :; }\n")
            readonly = Path(tmp) / "readonly.sh"
            readonly.write_text("g() { :; }\nreadonly -f g\n")
            liar = {"BASH_FUNC_systemctl%%": "() { echo enabled; }", "BASH_FUNC_sudo%%": "() { :; }"}
            for name, env, by_path in (("exported systemctl and sudo", liar, False),
                                       ("a BASH_ENV file's", {"BASH_ENV": str(bash_env)}, False),
                                       ("errexit", {"SHELLOPTS": "errexit"}, False),
                                       ("errexit, and an exported set", {"SHELLOPTS": "errexit",
                                                                         "BASH_FUNC_set%%": "() { :; }"}, False),
                                       ("by its path", {**liar, "BASH_ENV": str(bash_env), "SHELLOPTS": "errexit"},
                                        True),
                                       ("by its path, a readonly function of a BASH_ENV file",
                                        {"BASH_ENV": str(readonly)}, True),
                                       ("an exported read, and f", {"BASH_FUNC_read%%": "() { return 0; }", "f": "x"},
                                        False)):
                with self.subTest(name):
                    r = self.run_check(self.CONF, self.GOOD, env=env, by_path=by_path)
                    self.assertEqual((r.returncode, r.stdout.count("ok ")), (0, 4), r.stdout + r.stderr)
                    log = []
                    states = self.GOOD.replace("cups.service disabled inactive", "cups.service enabled active")
                    r = self.run_check(self.CONF, states, mode="apply", env=env, log=log, by_path=by_path)
                    self.assertIn("sudo systemctl disable --now cups.service", log, r.stdout + r.stderr)
                    self.assertEqual((r.returncode, r.stdout.count("ok ")), (0, 4), r.stdout + r.stderr)
            left = {"a readonly function of a BASH_ENV file": {"BASH_ENV": str(readonly)},
                    "exported declare and unset": {"BASH_FUNC_declare%%": "() { :; }", "BASH_FUNC_unset%%": "() { :; }"},
                    "an exported compgen": {"BASH_FUNC_compgen%%": "() { :; }"}}
            for (name, env), mode in itertools.product(left.items(), ("check", "apply")):
                with self.subTest(f"{name}, {mode}"):
                    log = []
                    r = self.run_check(self.CONF, self.GOOD, env=env, mode=mode, log=log)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertEqual(r.stdout, "DRIFT  functions from the environment or a BASH_ENV file are left after "
                                               "their removal (one is readonly, or has the name of a builtin that "
                                               "removes them): no unit checked\n")
                    self.assertEqual(log, [])

    def test_a_unit_name_with_an_escape(self):
        # systemd's \x2d in a name is the list's text as it is (read -r), not an escape for read
        name = "systemd-fsck@dev-disk-by\\x2duuid-abcd.service"
        r = self.run_check(f"disabled system {name}\n", f"system {name} disabled inactive\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, f"ok     system {name} disabled, inactive\n")

    def test_run_from_the_repository_with_cdpath(self):
        # scripts/host-units.sh, run from the repository with CDPATH in the environment, under which cd prints the
        # directory it goes to: it finds its own list all the same
        conf = (SCRIPT.parents[1] / "config" / "host-units.conf").read_text()
        r = self.run_check("", "", relative=True, env={"CDPATH": ".:/tmp"})
        self.assertNotIn("cannot be read, or is not a regular file", r.stdout)
        lines = [l for l in conf.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(r.stdout.count("DRIFT") + r.stdout.count("ok "), len(lines), r.stdout)

    def test_the_stand_in_systemctl_logs_every_call_that_may_change_a_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "systemctl").write_text(APPLY_STUB)
            (t / "systemctl").chmod(0o755)
            for name, text in (("log", ""), ("states", "system x.service enabled active\n"), ("default", "x.target\n")):
                (t / name).write_text(text)
            env = {"PATH": os.environ["PATH"], "STUB_LOG": str(t / "log"), "STUB_STATES": str(t / "states"),
                   "STUB_DEFAULT": str(t / "default"), "XDG_RUNTIME_DIR": str(t)}
            for args in (["is-enabled", "x.service"], ["is-active", "x.service"], ["get-default"], ["stop", "x.service"],
                         ["--user", "restart", "y.service"], ["mask", "x.service"], ["enable", "--now", "x.service"]):
                subprocess.run([str(t / "systemctl"), *args], env=env, capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=30)
            self.assertEqual((t / "log").read_text().splitlines(),
                             ["system stop x.service", "user restart y.service", "system mask x.service",
                              "system enable --now x.service"])

    def test_the_repository_list_parses(self):
        """Every line of config/host-units.conf is a known state and scope (no stub answers: all are drift)."""
        conf = (SCRIPT.parents[1] / "config" / "host-units.conf").read_text()
        r = self.run_check(conf, "")
        self.assertNotIn("BAD", r.stdout)
        lines = [l for l in conf.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(r.stdout.count("DRIFT") + r.stdout.count("ok "), len(lines), r.stdout)


if __name__ == "__main__":
    unittest.main()
