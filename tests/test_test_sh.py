"""scripts/test.sh on small trees of its own: each copy of the script checks a tree with a fault, and must fail on it
(a tree with no fault must pass).  The faults are those the script once let through: no test at all (also when every
test is skipped with its class or module), a test file discovery does not load, a test that cannot run (also one that
never starts, or whose body never runs), a skip for another reason, a script that no step reads (also one whose #!
line has an option, or outside the directories the steps read), a file that is not regular (a FIFO hung the run),
Python that only compiling rejects, Python inside a shell script in the forms a shell runs it (through the script's
functions, su, runuser, sudo -s, sg, source, programs it does not follow, code that runs its own arguments or an
expansion of the word that gave it, variables set once, files the script writes, also in parts, and then runs, and the
forms its lexer once misread), unit programs and drop-ins in forms it did not read (also drop-ins of units that
systemd/ does not have, and of the distribution's units), and a unit's script that only the tested tree has; and what
runs no wrong Python, and tests that run (behind a decorator without functools.wraps, too), must pass.  The forms that
mutation testing of scripts/test.sh found untested are cases here too.  Each tree is <tmp>/repo, with the programs a
test stands in for in <tmp>/bin, outside it."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
LIVE = "/home/thc1006/dev/p100-q3-deployment"
GOOD_TEST = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"


@unittest.skipUnless(shutil.which("systemd-analyze"), "needs systemd-analyze")
class TestSh(unittest.TestCase):
    def tree(self) -> Path:
        """A repository at <tmp>/repo, with <tmp>/bin for programs the tests stand in for (outside the repository, whose
        every script the run reads)."""
        d = Path(tempfile.mkdtemp()) / "repo"
        self.addCleanup(shutil.rmtree, d.parent, True)
        for sub in ("scripts", "tests", "bench", "systemd", "tools", "../bin"):
            (d / sub).mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "test.sh", d / "scripts" / "test.sh")
        (d / "scripts" / "ok.sh").write_text("#!/bin/bash\nexit 0\n")
        (d / "scripts" / "ok.sh").chmod(0o755)
        (d / "bench" / "b.sh").write_text("#!/bin/bash\necho b\n")
        (d / "tests" / "t.sh").write_text("#!/bin/bash\necho t\n")
        (d / "tests" / "test_good.py").write_text(GOOD_TEST)
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\n")
        self.unit(d, "x.timer", "[Timer]\nOnCalendar=daily\n\n[Install]\nWantedBy=timers.target\n")
        self.program(d, "shellcheck", "#!/bin/sh\nexit 0\n")      # CI has the real one
        return d

    @staticmethod
    def program(d: Path, name: str, text: str) -> None:
        """A program on the PATH of the run on the tree d."""
        (d.parent / "bin" / name).write_text(text)
        (d.parent / "bin" / name).chmod(0o755)

    @staticmethod
    def unit(d: Path, name: str, text: str) -> None:
        (d / "systemd" / name).write_text("[Unit]\nDescription=test\n\n" + text)

    JOBS: tuple[str, ...] = ()      # what TestShJobs adds to the arguments of each run, and to its environment
    DEAL: dict[str, str] = {}

    def environment(self, d: Path) -> dict:
        e = {k: v for k, v in os.environ.items() if k not in ("STRATA_TEST_HARNESSES", "TEST_REPO_ONLY",
                                                              "TEST_JOBS_DEAL")}
        e["PATH"] = f"{d.parent / 'bin'}:{e['PATH']}"
        return {**e, **self.DEAL}

    def run_sh(self, d: Path, *args: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        """The copy of scripts/test.sh in d, run in a session of its own: one that does not end in time is killed with
        what it started (the runner's Python too)."""
        e = self.environment(d)
        with subprocess.Popen(["bash", str(d / "scripts" / "test.sh"), *args, *self.JOBS], cwd=d,
                              env={**e, **(env or {})},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="backslashreplace",
                              start_new_session=True) as p:
            try:
                out, err = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.communicate()
                raise
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def start_sh(self, d: Path, *args: str) -> tuple[subprocess.Popen, Path]:
        """As run_sh, but in the background, with its output (stdout and stderr) in a file, which a process that it
        leaves behind does not keep open as it would a pipe; killed with its process group after the test."""
        out = d.parent / "out"
        with open(out, "w") as f:
            p = subprocess.Popen(["bash", str(d / "scripts" / "test.sh"), *args, *self.JOBS], cwd=d,
                                 env=self.environment(d), stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and os.killpg(p.pid, signal.SIGKILL), p.wait()))
        return p, out

    @staticmethod
    def with_text(text: str) -> list[int]:
        """The processes whose command line holds text."""
        found = []
        for q in filter(str.isdigit, os.listdir("/proc")):
            try:
                if text.encode() in Path(f"/proc/{q}/cmdline").read_bytes().replace(b"\0", b" "):
                    found.append(int(q))
            except OSError:
                pass
        return found

    def wait_for(self, what, seconds: float = 30):
        deadline = time.monotonic() + seconds
        while not what():
            self.assertLess(time.monotonic(), deadline, "it did not come")
            time.sleep(0.05)

    def passes(self, d: Path, *args: str, env: dict | None = None) -> None:
        r = self.run_sh(d, *args, env=env)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (0, ["all checks passed"]), r.stdout + r.stderr)

    def fails(self, d: Path, says: str, *args: str, env: dict | None = None) -> None:
        r = self.run_sh(d, *args, env=env)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertIn(says, r.stdout)

    def scripts_fail(self, cases: dict[str, str], path: str = "scripts/s.sh") -> None:
        """Each body (a script after its #! line), alone in a tree, fails and says what is given."""
        for body, says in cases.items():
            with self.subTest(body=body):
                d = self.tree()
                (d / path).write_text("#!/bin/bash\n" + body)
                self.fails(d, says)

    def units_fail(self, cases: dict[str, str]) -> None:
        """Each line, in a unit x.service (a socket y.socket for a [Socket] text), fails and says what is given.  The
        tree has stand-ins for programs that CI's container lacks, as libexec/sudo, libexec/time ... of the checkout."""
        for line, says in cases.items():
            with self.subTest(line=line):
                d = self.tree()
                (d / "libexec").mkdir()
                for name in ("sudo", "time", "runuser", "systemd-run", "su", "zsh"):
                    (d / "libexec" / name).write_text("")
                    (d / "libexec" / name).chmod(0o755)
                if line.startswith("[Socket]"):
                    self.unit(d, "y.socket", line)
                else:
                    self.unit(d, "x.service", f"[Service]\nType=oneshot\nExecStart=/bin/true\n{line}\n")
                self.fails(d, says)

    def test_a_tree_without_a_fault_passes(self):
        d = self.tree()
        self.passes(d)
        self.passes(d, "--repo-only")

    def test_no_test_ran(self):
        d = self.tree()
        (d / "tests" / "test_good.py").unlink()
        self.fails(d, "no test ran")

    def test_a_test_file_discovery_does_not_load(self):
        for name, says in (("deploy_test.py", "tests/deploy_test.py is not named test_*.py"),
                           ("testdeploy.py", "tests/testdeploy.py is not named test_*.py"),
                           ("sub/test_deploy.py", "tests/sub/test_deploy.py is in a directory under tests/")):
            with self.subTest(name=name):
                d = self.tree()
                (d / "tests" / name).parent.mkdir(exist_ok=True)
                (d / "tests" / name).write_text(GOOD_TEST.replace("assertTrue(True)", "assertTrue(False)"))
                self.fails(d, says)

    def test_tests_that_cannot_run(self):
        cases = {
            GOOD_TEST + "    def test_ok(self):\n        self.assertTrue(False)\n":
                "tests/test_twice.py:5: T.test_ok is defined again (first at line 3): the first one never runs",
            GOOD_TEST + "def test_free():\n    assert False\n":
                "tests/test_twice.py:5: test_free is not in a TestCase, so it never runs",
            GOOD_TEST + GOOD_TEST.replace("import unittest\n", "").replace("assertTrue(True)", "assertTrue(False)"):
                "tests/test_twice.py:5: class T is defined again: the first one never runs",
            GOOD_TEST + "class Mixin:\n    def test_m(self):\n        assert False\n":
                "tests/test_twice.py:6: Mixin.test_m never runs",
            GOOD_TEST + "class Outer:\n    class Inner(unittest.TestCase):\n        def test_i(self):\n            self.fail()\n":
                "tests/test_twice.py:7: Inner.test_i never runs",
            GOOD_TEST + "class G(unittest.TestCase):\n    def test_g(self):\n        yield\n        self.fail()\n":
                "test_twice.G.test_g is a generator: its body never runs",
            GOOD_TEST + "class A(unittest.TestCase):\n    async def test_a(self):\n        self.fail()\n":
                "test_twice.A.test_a is async in a TestCase that does not await it",
            GOOD_TEST + "def gone(f):\n    return None\nclass D(unittest.TestCase):\n    @gone\n    def test_d(self):\n        self.fail()\n":
                "tests/test_twice.py:9: D.test_d never runs",
            GOOD_TEST + "if True\n": "tests/test_twice.py does not compile",
            # round 4: the test that never runs is found by its def, not by its name; duplicates in any class body
            GOOD_TEST + "class U:\n    def test_ok(self):\n        self.fail()\n":
                "tests/test_twice.py:6: U.test_ok never runs",
            "import unittest\nif True:\n    class T(unittest.TestCase):\n        def test_ok(self):\n            self.fail()\n"
            "        def test_ok(self):\n            pass\n":
                "tests/test_twice.py:6: T.test_ok is defined again (first at line 4)",
            "import unittest\ntry:\n    class T(unittest.TestCase):\n        def test_ok(self):\n            self.fail()\n"
            "    class T(unittest.TestCase):\n        def test_ok(self):\n            pass\nexcept ImportError:\n    pass\n":
                "tests/test_twice.py:6: class T is defined again",
            "import unittest\ntry:\n    import json\nexcept ImportError:\n    json = None\nelse:\n"
            "    class T(unittest.TestCase):\n        def test_a(self):\n            self.fail()\n"
            "        def test_a(self):\n            pass\n":
                "tests/test_twice.py:10: T.test_a is defined again (first at line 8)",
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.fail()\n"
            "    test_ok = lambda self: None\n":
                "tests/test_twice.py:3: T.test_ok never runs",
            GOOD_TEST + "def gone(f):\n    return None\nclass D(unittest.TestCase):\n    @gone\n    def test_ok(self):\n"
            "        self.fail()\n":
                "tests/test_twice.py:9: D.test_ok never runs",
            GOOD_TEST + "class NotATest:\n    def test_ok(self):\n        assert False\n":
                "tests/test_twice.py:6: NotATest.test_ok never runs",
            "import unittest\nclass Mixin:\n    def test_a(self):\n        assert False\n"
            "class T(Mixin, unittest.TestCase):\n    def test_a(self):\n        pass\n":
                "tests/test_twice.py:3: Mixin.test_a never runs",
            GOOD_TEST + "class A(unittest.TestCase):\n    async def test_a(self):\n        yield\n        self.fail()\n":
                "test_twice.A.test_a is an async generator: its body never runs",
            GOOD_TEST + "class A(unittest.IsolatedAsyncioTestCase):\n    async def test_a(self):\n        yield\n"
            "        self.fail()\n":
                "test_twice.A.test_a is an async generator: its body never runs",
            GOOD_TEST + "import functools\ndef deco(f):\n    @functools.wraps(f)\n    def w(*a):\n        return f(*a)\n"
            "    return w\nclass G(unittest.TestCase):\n    @deco\n    def test_g(self):\n        yield\n        self.fail()\n":
                "test_twice.G.test_g is a generator: its body never runs",
            GOOD_TEST + "import functools\ndef deco(f):\n    @functools.wraps(f)\n    def w(*a):\n        return f(*a)\n"
            "    return w\nclass G(unittest.TestCase):\n    @deco\n    async def test_g(self):\n        self.fail()\n":
                "test_twice.G.test_g is async in a TestCase that does not await it",
            "import unittest\nif True:\n    def test_free():\n        assert False\n" + GOOD_TEST.replace("import unittest\n", ""):
                "tests/test_twice.py:3: test_free is not in a TestCase, so it never runs",
        }
        for text, says in cases.items():
            with self.subTest(says=says):
                d = self.tree()
                (d / "tests" / "test_twice.py").write_text(text)
                self.fails(d, says)
        d = self.tree()                     # what does run passes: a base class's test, a decorated one, a helper
        (d / "tests" / "test_runs.py").write_text(
            "import functools, unittest\ndef deco(f):\n    @functools.wraps(f)\n    def w(*a):\n        return f(*a)\n"
            "    return w\nclass Base(unittest.TestCase):\n    def test_base(self):\n        pass\nclass Sub(Base):\n"
            "    @deco\n    def test_sub(self):\n        def test_inner():\n            pass\n        test_inner()\n"
            "    @unittest.skipIf(False, 'x')\n    def test_kept(self):\n        pass\n"
            "def make():\n    class F(unittest.TestCase):\n        def test_f(self):\n            pass\n    return F\nFT = make()\n")
        self.passes(d)

    def test_no_test_ran_when_classes_or_modules_are_skipped(self):
        skip_class = ("import unittest\nclass S(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
                      "        raise unittest.SkipTest('harness: not asked for')\n    def test_s(self):\n        self.fail()\n")
        skip_module = ("import unittest\ndef setUpModule():\n    raise unittest.SkipTest('harness: not asked for')\n"
                       "class S(unittest.TestCase):\n    def test_s(self):\n        self.fail()\n")
        skip_all = skip_class + "class U(unittest.TestCase):\n    @unittest.skip('harness: y')\n    def test_u(self):\n" \
                                "        self.fail()\n"
        for name, text in (("setUpClass", skip_class), ("setUpModule", skip_module), ("both kinds", skip_all)):
            with self.subTest(skipped=name):
                d = self.tree()
                (d / "tests" / "test_good.py").write_text(text)
                self.fails(d, "no test ran (every one was skipped)")
        d = self.tree()                     # a test that runs beside a class skipped in setUpClass
        (d / "tests" / "test_skip.py").write_text(skip_class)
        self.passes(d)
        d = self.tree()                     # a test that runs and skips a subtest ran
        (d / "tests" / "test_good.py").write_text(
            "import unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n        self.assertTrue(True)\n"
            "        with self.subTest(x=1):\n            self.skipTest('harness: not asked for')\n")
        self.passes(d)

    def test_a_failing_test(self):
        d = self.tree()
        (d / "tests" / "test_bad.py").write_text(GOOD_TEST.replace("assertTrue(True)", "assertTrue(False)"))
        self.fails(d, "FAILED (failures=1)")

    def test_a_test_that_expects_to_fail(self):
        d = self.tree()
        (d / "tests" / "test_xf.py").write_text(
            "import unittest\nclass X(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_x(self):\n"
            "        self.assertTrue(False)\n")
        self.fails(d, "test_xf.X.test_x expects to fail")

    def test_a_test_that_ends_the_run(self):
        d = self.tree()
        (d / "tests" / "test_exit.py").write_text(
            "import os, unittest\nclass E(unittest.TestCase):\n    def test_a(self):\n        os._exit(0)\n"
            "    def test_b(self):\n        self.fail()\n")
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"FAIL: the unit tests did not finish \(no verdict\): worker \d of \d ended with 0, "
                                   r"without its record")

    def test_a_test_module_without_a_test(self):
        d = self.tree()
        (d / "tests" / "test_none.py").write_text(GOOD_TEST.replace("def test_ok", "def check_ok"))
        self.fails(d, "tests/test_none.py has no test")

    def test_every_test_skipped(self):
        d = self.tree()
        (d / "tests" / "test_good.py").write_text(
            "import unittest\nclass S(unittest.TestCase):\n    @unittest.skip('harness: not asked for')\n    def test_s(self):\n"
            "        pass\n")
        self.fails(d, "no test ran (every one was skipped)")

    def test_skips(self):
        d = self.tree()
        skip = "import unittest\nclass S(unittest.TestCase):\n    @unittest.skip({!r})\n    def test_s(self):\n        pass\n"
        (d / "tests" / "test_skip.py").write_text(skip.format("harness: not asked for"))
        self.passes(d)
        self.fails(d, "was skipped: harness: not asked for", "--harnesses")
        (d / "tests" / "test_skip.py").write_text(skip.format("machine: needs this machine"))
        self.passes(d, "--repo-only")
        self.fails(d, "was skipped: machine: needs this machine")
        (d / "tests" / "test_skip.py").write_text(skip.format("needs a tool"))
        self.fails(d, "was skipped: needs a tool", "--repo-only")

    def test_a_shell_script_bash_cannot_parse(self):
        d = self.tree()
        (d / "scripts" / "bad.sh").write_text("#!/bin/bash\nif true; then\n  echo x\n")
        self.fails(d, "syntax error: scripts/bad.sh")

    def test_scripts_the_steps_do_not_read(self):
        for path, text, says in (("scripts/lib/helper.sh", "#!/bin/bash\nif\n", "scripts/lib/helper.sh: a shell script that bash -n"),
                                 ("bench/run-set", "#!/bin/bash\necho x\n", "bench/run-set: a shell script that bash -n"),
                                 ("tools/gen", "#!/usr/bin/env python3\nif True\n", "tools/gen: Python without .py"),
                                 # the interpreter, not the last word of the #! line
                                 ("bench/run-set", "#!/bin/bash -e\nif\n", "bench/run-set: a shell script that bash -n"),
                                 ("scripts/lib/helper", "#!/bin/sh -eu\nif\n", "scripts/lib/helper: a shell script that"),
                                 ("tools/gen", "#!/usr/bin/env -S bash -e\nif\n", "tools/gen: a shell script that bash -n"),
                                 ("tools/gen", "#!/usr/bin/env -u X A=1 dash\nif\n", "tools/gen: a shell script that"),
                                 ("tools/gen", "#!/usr/bin/env -Sbash -e\nif\n", "tools/gen: a shell script that bash -n"),
                                 ("tools/gen", "#!/usr/bin/env -S python3 -u\nif True\n", "tools/gen: Python without .py")):
            with self.subTest(path=path):
                d = self.tree()
                (d / path).parent.mkdir(parents=True, exist_ok=True)
                (d / path).write_text(text)
                self.fails(d, says)

    def test_python_that_only_compiling_rejects(self):
        d = self.tree()
        (d / "bench" / "x.py").write_text("import os\nfrom __future__ import annotations\n")
        self.fails(d, "bench/x.py")

    def test_python_inside_a_shell_script(self):
        d = self.tree()
        (d / "scripts" / "h.sh").write_text("#!/bin/bash\nv=$(python3 -I - \"$1\" <<'EOF'\nfor c in x\n    pass\nEOF\n)\n")
        self.fails(d, "scripts/h.sh:3 (heredoc)")
        d = self.tree()
        (d / "bench" / "c.sh").write_text("#!/bin/bash\npython3 -c 'import sys\nif True\n    pass\n' \"$1\"\n")
        self.fails(d, "bench/c.sh:2 (python3 -c)")
        d = self.tree()
        (d / "scripts" / "u.sh").write_text("#!/bin/bash\npython3 - <<'EOF'\nprint(1)\n")   # bash -n only warns
        self.fails(d, "scripts/u.sh:2: the heredoc EOF has no end")
        cases = {
            "python3 -W ignore - <<'EOF'\nif True\n    pass\nEOF\n": "scripts/s.sh:3 (heredoc)",      # an option's value
            "python3 - <<'END.PY'\nif True\n    pass\nEND.PY\n": "scripts/s.sh:3 (heredoc)",
            "python3 - <<\\EOF\nif True\n    pass\nEOF\n": "scripts/s.sh:3 (heredoc)",
            "python3 - <<-'EOF'\n\tif True:\n\t\tprint(1)\n\tEOF\n": "scripts/s.sh:3 (heredoc)",   # bash strips the tabs
            "python3 -I \\\n  - <<'EOF'\nif True\n    pass\nEOF\n": "scripts/s.sh:4 (heredoc)",    # a continued line
            "x=$(python3 -c \"if True\n  pass\" \"$1\")\n": "scripts/s.sh:2 (python3 -c)",
            "python3 -Ic 'if True\n  pass'\n": "scripts/s.sh:2 (python3 -c)",
            "python3 -c $'if True\\n  pass'\n": "scripts/s.sh:2 (python3 -c)",
            "python3 -c \"$(cat x)\"\n": "cannot read: scripts/s.sh:2: python -c runs a command substitution",
            "python3 <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",                  # no "-": stdin too
            "y=$(( 1 << n ))\npython3 -c 'if True'\nn\n": "scripts/s.sh:3 (python3 -c)",       # a shift, not a heredoc
            # a program from a pipe, a process substitution or a variable; options with values; a function, a variable
            # or a wrapper that runs python; command substitutions in an expanded heredoc and in ${ }; a heredoc
            # operator in a comment; sh -c, flock -c and a shell's heredoc
            "printf 'x' | python3 -\n": "cannot read: scripts/s.sh:2: python reads its program from its standard input",
            "python3 <(cat x)\n": "cannot read: scripts/s.sh:2: python runs a process substitution",
            "python3 -c \"$var\"\n": "cannot read: scripts/s.sh:2: python -c runs what a variable holds",
            "python3 -X dev -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "py() { python3 \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "\"$PY\" -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "nice -n 19 taskset -c 0 timeout -k 5 60 python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "\"${as_user[@]}\" python3 -I - <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",
            "SIGDFL=(python3 -c 'if True')\n": "scripts/s.sh:2 (python3 -c)",
            "cat <<EOF\n$(python3 -c 'if True')\nEOF\n": "scripts/s.sh:3 (python3 -c)",
            "x=${1:-$(python3 -c 'if True')}\n": "scripts/s.sh:2 (python3 -c)",
            "echo x # python3 - <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (python3 -c)",
            "python3 - x.py <<EOF\nprint(1)\nEOF\n": "a heredoc whose delimiter is not quoted",
            "bash -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (bash -c):1 (python3 -c)",
            "sh -e -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (sh -c):1 (python3 -c)",
            "flock /tmp/l -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (flock -c):1 (python3 -c)",
            "bash <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (bash heredoc):1 (python3 -c)",
            "eval \"python3 -c 'x'\"\n": "cannot read: scripts/s.sh:2: eval",
            "if python3 -c 'if True'; then :; fi\n": "scripts/s.sh:2 (python3 -c)",            # after a keyword
            "env -u X A=1 python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "python3 -c'if True'\n": "scripts/s.sh:2 (python3 -c)",                             # the code in the word
            "python3 <<<'if True'\n": "scripts/s.sh:2 (python3 <<<)",
            # round 3: a file a heredoc writes; $( ) in arithmetic and [[ ]]; python by a path through a variable or
            # a default; a case pattern, an extglob and a regex in $( ); stdin forms; functions; programs that run
            # code: su, find, trap; two heredocs; backticks
            "cat > x.py <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",
            "cat > stub <<'EOF'\n#!/bin/bash\npython3 -c 'if True'\nEOF\n": "(a file this heredoc writes):2 (python3 -c)",
            "n=$(( $(python3 -c 'if True') + 1 ))\n": "scripts/s.sh:2 (python3 -c)",
            "[[ $(python3 -c 'if True') == x ]] && echo y\n": "scripts/s.sh:2 (python3 -c)",
            "if [[ $(python3 -c 'if True') == x ]]; then :; fi\n": "scripts/s.sh:2 (python3 -c)",
            "\"$VENV/bin/python3\" -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "${PYTHON:-python3} -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "$interp -c 'x'\n": "the program is in a variable",
            "v=\"$(case $x in a) python3 -c 'if True' ;; esac)\"\n": "scripts/s.sh:2 (python3 -c)",
            "x=\"$(ls !(*.txt) && python3 -c 'if True')\"\n": "scripts/s.sh:2 (python3 -c)",
            "python3 -- - <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",
            "bash -s x <<'EOF'\npython3 -c 'if True'\nEOF\n": "(bash heredoc):1 (python3 -c)",
            "function py { python3 \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "su -c 'python3 -c \"if True\"' nobody\n": "(su -c):1 (python3 -c)",
            "find . -exec sh -c 'python3 -c \"if True\"' \\;\n": "scripts/s.sh:2 (sh -c):1 (python3 -c)",   # its -exec
            "trap 'python3 -c \"if True\"' EXIT\n": "(trap):1 (python3 -c)",
            "python3 - <<'A' 3<<'B'\nif True\nA\nprint(1)\nB\n": "scripts/s.sh:3 (heredoc)",
            "python3 <<< \"$code\"\n": "a here-string with an expansion in it",
            "bash -c \"$cmd\"\n": "bash -c runs what a variable or a command gives",
            "x=`python3 -c 'if True'`\n": "scripts/s.sh:2 (python3 -c)",
            "x=\"`python3 -c 'if True'`\"\n": "scripts/s.sh:2 (python3 -c)",
            "cat <<EOF\n`python3 -c 'if True'`\nEOF\n": "scripts/s.sh:3 (python3 -c)",
            "sudo -n python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "xargs python3 -c 'if True' < list\n": "scripts/s.sh:2 (python3 -c)",
            "nohup python3 -c 'if True' &\n": "scripts/s.sh:2 (python3 -c)",
            "xyz python3 -c 'x'\n": "cannot read: scripts/s.sh:2: xyz is given a word that names python (python3)",
        }
        for body, says in cases.items():
            with self.subTest(body=body):
                d = self.tree()
                (d / "scripts" / "s.sh").write_text("#!/bin/bash\n" + body)
                self.fails(d, says)
        d = self.tree()                     # the forms read here pass when their Python is right, and text passes
        (d / "x.py").write_text("print(1)\n")
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\npython3 -W ignore - <<'EOF'\nprint(1)\nEOF\npython3 - <<-'EOF'\n\tif True:\n\t    print(1)\n"
            "\tEOF\nx=$(python3 -c \"print('$HOME', ${#x})\")\npython3 tool.py <<'EOF'\nnot python\nEOF\n"
            "cat <<'EOF'\npython3 -c not read here\nEOF\ny=$(( 1 << 30 ))\n"
            "echo \"use python3 -c to test\"\ngrep -q 'python3 -c' x\n# python3 -c 'if True'\nsay() { echo \"$*\"; }\n"
            "say \"python3 -c failed\"\n"
            "command -v python3 > /dev/null\npython3 -c \"print('\\$(x)')\"\npython3 -m json.tool <<'EOF'\nnot python\nEOF\n"
            "python3 - < x.py\nfor ((i = 0; i << 2; i++)); do :; done\n[[ $a < $b ]] && echo y\n"
            "[[ $x =~ ^([0-9]+)$ ]] && echo y\narr=(a b \"c d\")\necho a#b ${#x} $#\n"
            "spec() { python3 - \"$1\" \"$@\" <<'EOF'\nprint(1)\nEOF\n}\nspec x\n"                  # a function's name is
            "summary_of() { python3 -B tool.py \"$@\" > out; }\nsummary_of -c x\n"                  # no command; nor its arguments python's
            "python3 -c $'x = 1\\nif x:\\n  pass'\npython3 --version\necho python3 -c x\nps -C python3 -o pid=\n"
            "cat > f <<EOF\ngrep -q 'ready$' log\nEOF\necho \"price in $'s\"\nx=${a:-'$(python3 -c \"if True\")'}\n"
            "cat > notes.txt <<'EOF'\nif True\nEOF\nPY=(python3 -I)\n[[ $x == \"a\\\"b\" ]] && echo y\n"
            "arr=(\n  a  # don't change\n)\nfor ((i = 0; i << 2; i++)); do :; done\n")
        self.passes(d)
        d = self.tree()                     # a file named python written by cat is not Python
        (d / "tests" / "w.sh").write_text("#!/bin/bash\ncat > \"$1/python\" <<'EOF'\n#!/bin/bash\nexec true\nEOF\n")
        self.passes(d)

    def test_functions_that_run_their_arguments(self):
        # a function's body is read at each call with the call's words in place of "$@" (as the act() of
        # scripts/strata-health.sh and the run() of scripts/install-system-files.sh do)
        self.scripts_fail({
            "act() { if [ $DRY = 1 ]; then echo \"would run: $*\"; else \"$@\"; fi; }\nact python3 -c 'if True'\n":
                "scripts/s.sh:3 (python3 -c)",
            "run() { if [ $DRY = 1 ]; then echo \"  would run: $*\"; else \"$@\" || exit 1; fi; }\n"
            "run sudo python3 - <<'EOF'\nif True\nEOF\n": "scripts/s.sh:4 (heredoc)",
            "retry() { local i; for i in 1 2 3; do \"$@\" && return 0; sleep 1; done; return 1; }\n"
            "retry python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "PY=python3\npy() { \"$PY\" \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:4 (python3 -c)",
            "py() { \"$VENV/bin/python3\" \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "py() { \"${PY:-python3}\" \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "py() { python3 \"${@}\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "py() ( python3 \"$@\" )\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "true; py() { python3 \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "py() {\n  # usage: py ARGS }\n  python3 \"$@\"\n}\npy -c 'if True'\n": "scripts/s.sh:6 (python3 -c)",
            "py() { python3 -c \"$@\"; }\npy 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "later() { \"${@:2}\"; }\nlater x python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "act() { \"$@\"; }\nquiet() { act \"$@\" > /dev/null; }\nquiet python3 -c 'if True'\n":
                "scripts/s.sh:4 (python3 -c)",
            "py() { python3 \"$@\"; }\ncat <<'EOF' | py -\nif True\nEOF\n": "scripts/s.sh:4 (heredoc)",
            "act() { \"$@\"; }\nact bash -c 'python3 -c \"if True\"'\n": "scripts/s.sh:3 (bash -c):1 (python3 -c)",
            "f() if true; then python3 \"$@\"; fi\n": "is not { } or ( ), which this check reads",
            "".join(f"f{i}() {{ f{i + 1} \"$@\"; f{i + 1} \"$@\"; }}\n" for i in range(16)) + "f16() { :; }\nf0 x\n":
                "more calls of the script's functions (with their commands and words) than this check follows",
        })
        d = self.tree()                     # calls whose words run no Python, and a function that calls a program
        (d / "scripts" / "s.sh").write_text(  # of its own name
            "#!/bin/bash\nact() { if [ \"$DRY\" = 1 ]; then echo \"would run: $*\"; else \"$@\"; fi; }\n"
            "act systemctl restart x.service\nact python3 -c 'print(1)'\ngit() { command git -C /tmp \"$@\"; }\n"
            "git status\nsay() { echo \"x: $*\"; }\nsay python3 -c failed\nspec() { python3 - \"$@\" <<'EOF'\nprint(1)\n"
            "EOF\n}\nspec -c x\nspec - y\nf() { g \"$@\"; }\ng() { f \"$@\"; }\nf python3\n"
            "py() { python3 -c \"$@\"; }\npy 'print(1)'\nnever() { python3 -c \"$@\"; }\n")
        self.passes(d)

    def test_programs_that_run_shell_code(self):
        self.scripts_fail({
            # su and runuser: -c anywhere among their words; runuser -u runs a command
            "su - nobody -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",
            "su nobody -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",
            "su -l nobody -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",
            "su --command='python3 -c \"if True\"' nobody\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",
            "su -lc 'python3 -c \"if True\"' nobody\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",
            "su nobody <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (su heredoc):1 (python3 -c)",
            "runuser -u nobody -- python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "runuser -l nobody -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (runuser -c):1 (python3 -c)",
            # python or a shell after a program this check does not follow (round 8: refused, not read)
            "strace -f python3 -u -c 'if True'\n": "cannot read: scripts/s.sh:2: strace is given a word that names python",
            "setarch x86_64 bash -e -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2: setarch is given a word that names",
            "strace -f bash -ec 'python3 -c \"if True\"'\n": "scripts/s.sh:2: strace is given a word that names python",
            "strace -f \"$PY\" -c 'x'\n": "cannot read: scripts/s.sh:2: strace is given a word that names python ($PY)",
            "strace -f python3 tool.py -c x\n": "cannot read: scripts/s.sh:2: strace is given a word that names python",
            "$interp -I -c 'if True'\n": "$interp -c: the program is in a variable",
            # an expansion of the word that gives a shell its code is not known there
            "bash -c \"$PY -c 'if True'\"\n": "(bash -c):1: a command that is only an expansion of the word that gave",
            "bash -c \"python3 -c '$prog'\"\n": "(bash -c):1: python -c runs what a variable holds",
            "bash -c \"python3 - <<'EOF'\n$code\nEOF\"\n": "a heredoc that is an expansion of the word that gave this code",
            "flock -c \"$cmd\" /tmp/l\n": "flock -c runs what a variable or a command gives",
            "watch -x python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "zsh -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (zsh -c):1 (python3 -c)",
            "env -S'python3 -c \"if True\"'\n": "scripts/s.sh:2 (env -S):1 (python3 -c)",
            "trap \"$handler\" EXIT\n": "trap runs what a variable or a command gives",
            "watch -n 1 \"$cmd\"\n": "watch runs what a variable or a command gives",
            "$interp -Ic 'if True'\n": "$interp -Ic: the program is in a variable",
            "$SHELL -o errexit -c 'x'\n": "$SHELL -c: the program is in a variable",
            "act() { \"$@\"; }\ntrap 'act python3 -c \"if True\"' EXIT\n": "scripts/s.sh:3 (trap):1 (python3 -c)",
        })
        d = self.tree()                     # what they run is read when it is right
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nsu - nobody -c 'python3 -c \"print(1)\"'\nrunuser -u nobody -- python3 -c 'print(1)'\n"
            "bash -c \"echo $HOME; python3 -c 'print(\\\"$x\\\")'\"\nsay() { echo \"$*\"; }\n"
            "say install python3 -m venv first\nwatch -n 1 -x python3 -c 'print(1)'\n"
            "exec \"$ps2pdfwr\" -dCompatibilityLevel=1.4 \"$@\"\n$GS -sDEVICE=txtwrite -q x\n$xsession -gdm\n")
        self.passes(d)

    def test_files_a_script_writes_and_runs(self):
        self.scripts_fail({
            "cat <<'EOF' | tee x.py > /dev/null\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",
            "{ cat <<'EOF'; } > x.py\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",
            "> x.py cat <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",
            "cat > \"$tmp/prog\" <<'EOF'\nif True\nEOF\npython3 \"$tmp/prog\"\n": "scripts/s.sh:3 (a file this heredoc writes)",
            "f=$(mktemp)\ncat > \"$f\" <<'EOF'\nif True\n    pass\nEOF\npython3 \"$f\"\n":
                "scripts/s.sh:4 (a file this heredoc writes)",
            "cat > x.sh <<'EOF'\npython3 -c 'if True'\nEOF\nbash x.sh\n": "scripts/s.sh:3 (a file this heredoc writes):1 (python3 -c)",
            "cat > lib <<'EOF'\npython3 -c 'if True'\nEOF\n. ./lib\nsource lib\n": "(a file this heredoc writes):1 (python3 -c)",
            "tee prog <<< 'if True' > /dev/null\npython3 - < prog\n": "scripts/s.sh:2 (a file this here-string writes)",
            "printf 'import time\\nif True\\n' > \"$SB/driver2.py\"\n": "scripts/s.sh:2 (a file printf writes)",
            "printf 'if True\\n' > prog\npython3 prog &\n": "scripts/s.sh:2 (a file printf writes)",
            "echo 'if True' > x.py\n": "scripts/s.sh:2 (a file echo writes)",
            "printf '%s\\n' \"$x\" > x.py\n": "printf writes a .py file from words this check cannot read",
            "echo \"$code\" >> x.py\n": "echo writes a .py file from words this check cannot read",
        })
        d = self.tree()                     # what they write passes when it is right, and other files are no program
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nprintf 'import time\\ntime.sleep(300)\\n' > \"$SB/driver2.py\"\npython3 \"$SB/driver2.py\" &\n"
            "echo \"# server\" > \"$SB/strata/serve/server.py\"\necho \"# a change\" >> \"$SB/tools/x.py\"\n"
            "printf '%s\\n' \"$x\" > notes.txt\ncat <<'EOF' | tee log.txt\nif True\nEOF\ncat x.txt > y.py\n"
            "printf 'x = 100%%\\n' > p.txt\n")
        self.passes(d)

    def test_forms_the_lexer_reads(self):
        self.scripts_fail({
            # arithmetic: a "(" in it, and "$( (" / "$((" that bash reads as a subshell in a command substitution
            "x=\"$(echo $(( (1+2))); python3 -c 'if True')\"\n": "scripts/s.sh:2 (python3 -c)",
            "cat <<EOF\n$(echo $((x*(y+1))); python3 -c 'if True')\nEOF\n": "scripts/s.sh:3 (python3 -c)",
            "x=$((python3 -c 'if True') | (wc -l))\n": "scripts/s.sh:2 (python3 -c)",
            "((python3 -c 'if True') )\n": "scripts/s.sh:2 (python3 -c)",
            # a function in a command substitution; ! and a subshell; [[ ]] with ]] and | inside a regex
            "x=\"$(f() { python3 -c 'if True'; }; f)\"\n": "scripts/s.sh:2 (python3 -c)",
            "if !(python3 -c 'if True'); then :; fi\n": "scripts/s.sh:2 (python3 -c)",
            "[[ $l =~ ^([0-9a-f]{64})\\ \\ ([^[:space:]]+)$ ]] && python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "case $x in python3|python) python3 -c 'if True' ;; esac\n": "scripts/s.sh:2 (python3 -c)",
            "{ if true; then echo a; fi }\npython3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            # the file descriptor python reads, the last redirection of it, an array, aliases, backquotes in "..."
            "python3 /dev/fd/3 3<<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",
            "echo 'if True' | python3 - 3<<<'x'\n": "python reads its program from its standard input",
            "python3 - <<'EOF' <<<'if True'\nprint(1)\nEOF\n": "scripts/s.sh:2 (python3 <<<)",
            "python3 - 0<&3\n": "python reads its program from <&3",
            "cmd=(python3 -); \"${cmd[@]}\" <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",
            "shopt -s expand_aliases\nalias py=python3\npy -c 'if True'\n": "aliases (shopt -s expand_aliases)",
            "x=\"`python3 -c \\\"1+\\\"`\"\n": "scripts/s.sh:2 (python3 -c)",
            "echo }\n)\n": "cannot read: scripts/s.sh:3: a \")\" with no \"(\"",
        })
        for raw, says in ((b"#!/bin/bash\necho \xe9\n", "cannot read: scripts/s.sh: it is not UTF-8"),
                          (b"\xef\xbb\xbf#!/bin/bash\necho x\n", "cannot read: scripts/s.sh: it starts with a byte order mark")):
            with self.subTest(raw=raw):
                d = self.tree()
                (d / "scripts" / "s.sh").write_bytes(raw)
                self.fails(d, says)
        d = self.tree()                     # forms that once failed although bash runs no wrong Python in them
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nfunction f { python3 -c 'print(1)'; }\npython3 -c $\"print(1)\"\n"
            "find . -name '*.py' -exec python3 -m py_compile {} +\nx=$((cd /tmp; ls) )\nsay() { echo \"$*\"; }\n"
            "say install python3 -m venv first\necho $(( (1+2) )) $((x*(y+1)))\n"
            "case \"$(basename \"$1\")\" in\n  python3|python) echo python ;;\n  (*) echo other ;;\nesac\n"
            "while IFS= read -r l; do\n  case \"$l\" in ''|'#'*) continue ;; esac\n"
            "  [[ \"$l\" =~ ^([0-9a-f]{64})\\ \\ ([^[:space:]]+)$ ]] || continue\ndone < list\n"
            "{ echo a; } > log 2>&1\nfunction g() {\n  echo \"$@\"\n}\ng x\nif { true; } then :; fi\n"
            "case \\\n  \"$(tty)\"\nin\n  ^h|^H) echo bs ;;\nesac\n")
        self.passes(d)

    def test_forms_mutation_testing_found_untested(self):
        # round 4: each of these tells scripts/test.sh from a mutant of it that mutation testing found surviving
        self.scripts_fail({
            "cat <(python3 -c 'if True')\n": "scripts/s.sh:2 (python3 -c)",                                # P13
            "while read -r l; do :; done < <(python3 -c 'if True')\n": "scripts/s.sh:2 (python3 -c)",       # P16
            "v=\"$(if [[ $x =~ ^(a|b)$ ]]; then python3 -c 'if True'; fi)\"\n": "scripts/s.sh:2 (python3 -c)",  # P17
            "args=(-c 'if True'); python3 \"${args[@]}\"\n":                                                 # P26, P59
                "python's arguments come from an array (${args[@]})",
            "$HOME/.venv/bin/python -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                         # P33
            "${INTERP:-python3} -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                             # P35
            "python3 /dev/stdin <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",                       # P36
            "! python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                                      # P37
            "flock -n -c 'python3 -c \"if True\"' /tmp/l\n": "scripts/s.sh:2 (flock -c):1 (python3 -c)",   # P44
            "env -S 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (env -S):1 (python3 -c)",                 # P46
            "python3 - < <(printf 'if True\\n')\n": "python reads its program from a process substitution",  # P55
            "watch -n 1 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (watch):1 (python3 -c)",              # P67
            "cat > stub <<'EOF'\n#!/usr/bin/env python3\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",  # P69
            "tee x.py > /dev/null <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",  # P73
            "py() { python3 -W ignore \"$@\"; }\npy -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",          # P78
            "setsid -f python3 -u -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                           # P85
            "exec -a x python3 -u -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                           # P86
            "command -p python3 -u -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                          # P88
            "time -p python3 -u -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                             # P89
            "systemd-run --user --scope -q python3 -u -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",       # P90
            "if true; then python3 -u -c 'if True'; fi\n": "scripts/s.sh:2 (python3 -c)",                   # P91
            "su -s /bin/bash -c 'python3 -c \"if True\"' nobody\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",  # P92
            "trap -- 'python3 -c \"if True\"' EXIT\n": "scripts/s.sh:2 (trap):1 (python3 -c)",            # P93
        })
        d = self.tree()                     # P20: a ")" in quotes in an array; P72: an unquoted heredoc to a .py file;
        (d / "scripts" / "s.sh").write_text(  # P28: a quoted $( ) in ${ } is no command substitution
            "#!/bin/bash\narr=(')' \"$y\")\necho ok\ncat > x.py <<EOF\nx = $N + 1\nEOF\nx=${a:-'$(python3 -c \"if True\")'}\n")
        self.passes(d)
        self.units_fail({
            "ExecStartPost=@/bin/sh sh -c 'if then'": "the shell code of /bin/sh -c does not parse",      # U20
            "ExecCondition=/bin/sh -c 'if then'": "the shell code of /bin/sh -c does not parse",          # U02
        })

    def test_unit_programs_in_every_form(self):
        cases = {
            "ExecStartPost=!!/opt/nonexistent/a": "/opt/nonexistent/a does not exist",
            "ExecStartPost=-+/opt/nonexistent/d": "/opt/nonexistent/d does not exist",
            "  ExecStartPost=/opt/nonexistent/b": "/opt/nonexistent/b does not exist",
            'ExecStartPost="/opt/nonexistent/c" x': "/opt/nonexistent/c does not exist",
            "ExecStartPost=no-such-program-xyz": "no-such-program-xyz is not an absolute path",
            "ExecStartPost=%h/prog": "a specifier",
            "ExecStartPost=": "with no command",
            "ExecStartPost=/home/nobody/p100-q3-deployment/scripts/ok.sh": "names a checkout other than",
            f"ExecStartPost={LIVE}/scripts/missing.sh": "is missing or not executable",
        }
        for line, says in cases.items():
            with self.subTest(line=line):
                d = self.tree()
                self.unit(d, "x.service", f"[Service]\nType=oneshot\nExecStart=/bin/true\n{line}\n")
                self.fails(d, says)
        d = self.tree()                     # a socket unit's program is checked too
        self.unit(d, "y.socket", "[Socket]\nListenStream=127.0.0.1:1\nExecStartPre=/opt/nonexistent/s\n")
        self.fails(d, "/opt/nonexistent/s does not exist")

    def test_unit_lines_it_cannot_read_and_more_forms(self):
        cases = {
            "ExecStartPost=/bin/true ; /opt/nonexistent/e": "/opt/nonexistent/e does not exist",
            "ExecStartPost=/bin/true ; ;": "an empty one between",
            "ExecStartPost=@/opt/nonexistent/f name": "/opt/nonexistent/f does not exist",
            "ExecStartPost=/bin/echo \\\n  more": "it ends with a backslash, so systemd joins it with the next line",
            "ExecFoo=/bin/true": "Unknown key name 'ExecFoo'",
            'ExecStartPost=/bin/echo "unbalanced': "cannot read the command",
            "ExecStartPost=/bin/sh -c 'if then'": "the shell code of /bin/sh -c does not parse",
            "ExecStartPost=/bin/sh -e -c 'if then'": "the shell code of /bin/sh -c does not parse",
            "ExecStartPost=/bin/sh -c 'date +%s'": "has a % that systemd expands",
            "ExecStartPost=/bin/sh -c 'echo $HOME'": "has a $ that systemd may expand",
            "ExecStartPost=/bin/sh -c 'python3 -c x'": "runs python, which this check does not read in a unit",
            "ExecStartPost=/usr/bin/env A=1 /opt/nonexistent/w": "/opt/nonexistent/w does not exist",
            "ExecStartPost=/usr/bin/timeout -k 5 60 /opt/nonexistent/t": "/opt/nonexistent/t does not exist",
            "ExecStartPost=/usr/bin/flock /tmp/l -c 'if then'": "the shell code of /usr/bin/flock -c does not parse",
            "ExecStartPost=/usr/bin/nice": "/usr/bin/nice runs no command",
            f"ExecStartPost=/bin/true {LIVE}/config/missing.conf": f"{LIVE}/config/missing.conf -> ",
            "ExecStartPost=bin/x": "bin/x is not an absolute path",
            "ExecStartPost=/bin/sh -ec 'if then'": "the shell code of /bin/sh -c does not parse",
            "ExecStartPost=/bin/sh -c 'exec /opt/nonexistent/prog'": "/opt/nonexistent/prog does not exist",
            "ExecStartPost=/usr/bin/python3 /opt/nonexistent/server.py": "/opt/nonexistent/server.py, which /usr/bin/python3 runs, does not exist",
            "ExecStartPost=:/bin/sh -c 'echo $$HOME'": "which systemd passes on as $$ with the",
        }
        for line, says in cases.items():
            with self.subTest(line=line):
                d = self.tree()
                self.unit(d, "x.service", f"[Service]\nType=oneshot\nExecStart=/bin/true\n{line}\n")
                self.fails(d, says)
        d = self.tree()                     # what systemd passes on as % and $; a path of the tested tree
        (d / "config").mkdir()
        (d / "config" / "x.conf").write_text("")
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/sh -c 'echo $$HOME 100%%'\n"
                                  f"ExecStartPost=/usr/bin/env A=1 /bin/true {LIVE}/config/x.conf\n"
                                  "ExecStartPost=:/bin/sh -c 'echo $HOME'\nExecPaths=/usr\n")
        self.passes(d)
        d = self.tree()
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\nExecStartPost=bin/x\n")
        self.fails(d, "bin/x is not an absolute path", "--repo-only")

    def test_unit_python_and_scripts(self):
        # python in a unit runs a script, found after its options and wrappers; a shell's script too
        self.units_fail({
            "ExecStartPost=/usr/bin/python3 -c \"if True\"": "/usr/bin/python3 -c: python in a unit runs a script here",
            "ExecStartPost=/usr/bin/python3 -m json.tool": "/usr/bin/python3 -m: python in a unit runs a script here",
            "ExecStartPost=/usr/bin/python3 -I": "/usr/bin/python3 reads its program from its standard input",
            "ExecStartPost=/usr/bin/python3 -": "/usr/bin/python3 reads its program from its standard input",
            "ExecStartPost=/usr/bin/env python3 /opt/nonexistent/server.py":
                "/opt/nonexistent/server.py, which python3 runs, does not exist",
            "ExecStartPost=/usr/bin/python3 -X dev /opt/nonexistent/x.py":
                "/opt/nonexistent/x.py, which /usr/bin/python3 runs, does not exist",
            "ExecStartPost=/usr/bin/python3 -W ignore /opt/nonexistent/x.py":
                "/opt/nonexistent/x.py, which /usr/bin/python3 runs, does not exist",
            "ExecStartPost=/usr/bin/nice -n 5 /usr/bin/python3 /opt/nonexistent/server.py":
                "/opt/nonexistent/server.py, which /usr/bin/python3 runs, does not exist",
            "ExecStartPost=/usr/bin/python3 relative.py": "relative.py, which /usr/bin/python3 runs, is not an absolute path",
            f"ExecStartPost=/usr/bin/python3 -u {LIVE}/missing.py": f"{LIVE}/missing.py -> ",
            # round 4: ExecStopPre, sudo and time, a shell's script, C escapes in single quotes, a # inside a word
            "[Socket]\nListenStream=127.0.0.1:1\nExecStopPre=/opt/nonexistent/sp\n": "/opt/nonexistent/sp does not exist",
            f"ExecStartPost={LIVE}/libexec/sudo -u nobody /opt/nonexistent/su": "/opt/nonexistent/su does not exist",
            f"ExecStartPost={LIVE}/libexec/time -p /opt/nonexistent/ti": "/opt/nonexistent/ti does not exist",
            "ExecStartPost=/bin/bash /opt/nonexistent/x.sh": "/opt/nonexistent/x.sh, which /bin/bash runs, does not exist",
            "ExecStartPost=/bin/sh -s": "/bin/sh reads its commands from its standard input",
            "ExecStartPost=/bin/sh -c 'echo \\x27'": "the shell code of /bin/sh -c does not parse",
            "ExecStartPost=/bin/sh -c 'echo a#b; exec /opt/nonexistent/h'": "/opt/nonexistent/h does not exist",
            "ExecStartPost=/bin/sh -c 'echo x; 2>/dev/null /opt/nonexistent/r'": "/opt/nonexistent/r does not exist",
        })
        d = self.tree()                     # what they run is there
        (d / "scripts" / "run.py").write_text("print(1)\n")
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\n"
                                  f"ExecStartPost=/usr/bin/env python3 -X dev -W ignore {LIVE}/scripts/run.py --flag x\n"
                                  f"ExecStartPost=/bin/bash -e {LIVE}/scripts/ok.sh\n"
                                  "ExecStartPost=/bin/sh -c 'echo \\x41 a#b; exec /bin/true'\n")
        self.passes(d)
        self.passes(d, "--repo-only")

    def test_drop_ins(self):
        memory = ("# The maintainer's account (uid 1003) may use at most 6 GiB of RAM and no swap, so that no job of its can "
                  "push\n# strata-q3 out of memory (docs/decisions/0007-memory-limit-of-the-maintainers-account.md)\n"
                  "[Slice]\nMemoryMax=6G\nMemorySwapMax=0\n")
        d = self.tree()                     # decision 0007's drop-in, for a unit that systemd/ does not have
        (d / "systemd" / "user-1003.slice.d").mkdir()
        (d / "systemd" / "user-1003.slice.d" / "50-memory.conf").write_text(memory)
        self.passes(d)
        self.passes(d, "--repo-only")
        d = self.tree()                     # one for a service that systemd/ does not have (its stand-in runs /bin/true)
        (d / "systemd" / "y.service.d").mkdir()
        (d / "systemd" / "y.service.d" / "a.conf").write_text("[Service]\nEnvironment=A=1\n")
        self.passes(d)
        d = self.tree()                     # one for a unit of systemd/: its commands can be reset and are checked
        (d / "systemd" / "x.service.d").mkdir()
        (d / "systemd" / "x.service.d" / "a.conf").write_text(
            f"[Service]\nExecStart=\nExecStart={LIVE}/scripts/ok.sh\nEnvironment=A=1\n")
        self.passes(d)
        cases = {
            ("user-1003.slice.d/50-memory.conf", "[Slice]\nMemoryMaxx=6G\n"): "Unknown key name 'MemoryMaxx'",
            ("user-1003.slice.d/50-memory.conf", "[Slice]\nMemoryMax=lots\n"): "Invalid memory limit 'lots'",
            ("user-1003.slice.d/50-memory.conf", "[Service]\nMemoryMax=6G\n"): "Unknown section 'Service'",
            ("user-1003.slice.d/notes.txt", "x\n"): "systemd/user-1003.slice.d/notes.txt: not a drop-in this check reads",
            ("user-1003.slice.d/sub/a.conf", "[Slice]\n"): "systemd/user-1003.slice.d/sub: not a drop-in this check reads",
            ("stuff/a.conf", "[Slice]\n"): "systemd/stuff: not a unit, or a drop-in directory",
            ("x.service.d/a.conf", "[Service]\nExecStartPost=/opt/nonexistent/dd\n"):
                "systemd/x.service.d/a.conf:2: /opt/nonexistent/dd does not exist",
            ("x.service.d/a.conf", "[Service]\nBogusKey=1\n"): "systemd/x.service.d/a.conf:2: Unknown key name 'BogusKey'",
            ("x.service.d/a.conf", f"[Service]\nExecStartPost={LIVE}/scripts/missing.sh\n"): "is missing or not executable",
            ("x.slice", "[Slice]\n"): "systemd/x.slice: not a unit, or a drop-in directory",
        }
        for (path, text), says in cases.items():
            with self.subTest(path=path, text=text):
                d = self.tree()
                (d / "systemd" / path).parent.mkdir(parents=True, exist_ok=True)
                (d / "systemd" / path).write_text(text)
                self.fails(d, says)
        d = self.tree()                     # an empty reset, in a unit and not in a drop-in, is no command
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\nExecStartPost=\n")
        self.fails(d, "with no command (a reset, only for a drop-in)")

    def test_what_systemd_analyze_finds(self):
        d = self.tree()
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\nBogusKey=1\n")
        self.fails(d, "Unknown key name 'BogusKey'")
        self.fails(d, "Unknown key name 'BogusKey'", "--repo-only")

    def test_a_unit_script_only_the_tested_tree_has(self):
        # the units name the live checkout; verify and the program check read the tree under test instead
        d = self.tree()
        (d / "scripts" / "new-in-this-tree.sh").write_text("#!/bin/bash\nexit 0\n")
        (d / "scripts" / "new-in-this-tree.sh").chmod(0o755)
        self.unit(d, "x.service", f"[Service]\nType=oneshot\nExecStart={LIVE}/scripts/new-in-this-tree.sh\n")
        self.passes(d)
        self.passes(d, "--repo-only")

    def test_repo_only_skips_the_programs_outside_the_repo(self):
        d = self.tree()
        self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\nExecStartPost=/opt/nonexistent/z\n"
                                  "ExecStartPost=no-such-program-xyz\n")
        self.passes(d, "--repo-only")
        self.fails(d, "/opt/nonexistent/z does not exist")

    @unittest.skipIf(os.geteuid() == 0, "machine: root reads every directory")
    def test_a_directory_it_cannot_read(self):
        d = self.tree()
        (d / "bench" / "locked").mkdir()
        (d / "bench" / "locked").chmod(0)
        self.addCleanup((d / "bench" / "locked").chmod, 0o755)
        self.fails(d, "cannot read ./bench/locked")

    def test_repo_only_needs_shellcheck(self):
        d = self.tree()
        with unittest.mock.patch.dict(os.environ, {"TEST_SHELLCHECK": "no-such-shellcheck"}):
            self.fails(d, "shellcheck is not installed (CI installs it)", "--repo-only")
            self.passes(d)
        self.program(d, "shellcheck", "#!/bin/sh\necho 'h.sh: error' >&2\nexit 1\n")   # shellcheck's errors
        self.fails(d, "")

    # round 5: the forms the fifth review found read wrongly; each test but the last fails with the scripts/test.sh
    # before round 5 (the last one holds the cases that kill the mutants the fifth review found alive)
    def test_python_and_shells_behind_programs_it_does_not_follow(self):
        # python or a shell behind a program this check does not follow (round 8: not read; what names python there,
        # as a word, a file the script wrote or an input, or an expansion after the shell, is refused, also as data)
        says = "is given a word that names python"
        self.scripts_fail({
            "strace -f -o /dev/null python3 <<'EOF'\nif True\nEOF\n": f"cannot read: scripts/s.sh:2: strace {says}",
            "setarch x86_64 python3 -u <<'EOF'\nif True\nEOF\n": f"cannot read: scripts/s.sh:2: setarch {says}",
            "setarch x86_64 python3 < <(printf 'if True\\n')\n": f"scripts/s.sh:2: setarch {says}",
            "printf 'if True\\n' | setarch x86_64 python3\n": f"scripts/s.sh:2: setarch {says}",
            "setarch x86_64 bash <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:2: setarch is given an input that names",
            "strace -f -o /dev/null sh -s <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:2: strace is given an input",
            "strace -f python3 <(printf 'if True\\n')\n": f"cannot read: scripts/s.sh:2: strace {says}",
            "strace -f bash <(printf 'x\\n')\n": "scripts/s.sh:2: bash (after strace) runs what it is given as a command line",
            "cat > x.sh <<'EOF'\npython3 -c 'if True'\nEOF\nstrace -f bash x.sh\n":
                "scripts/s.sh:5: strace is given a file the script wrote that names python (x.sh)",
            "readlink -f /usr/bin/python3\n": f"scripts/s.sh:2: readlink {says} (/usr/bin/python3)",       # data too
            "sha256sum \"$VENV/bin/python3\" > sums\n": f"scripts/s.sh:2: sha256sum {says} ($VENV/bin/python3)",
            "killall -q python3 || true\n": f"scripts/s.sh:2: killall {says} (python3)",
        })
        self.scripts_fail({                 # round 9: a function's $1 is read at its calls (when its shifts are counted)
            "resolve() { readlink -f \"$1\"; }\nresolve /usr/bin/python3\n": f"scripts/s.sh:2: readlink {says}",
        })
        d = self.tree()                     # what the rules let through: python named to a program that only prints or
        (d / "scripts" / "s.sh").write_text(  # matches text, and a shell's code that names no python
            "#!/bin/bash\nls -l /usr/bin/python3\npgrep -x python3 || true\nchsh -s /bin/bash nobody\nfile /bin/bash\n"
            "setarch x86_64 bash <<'EOF'\necho ok\nEOF\n")
        self.passes(d)

    def test_shells_that_read_their_standard_input(self):
        self.scripts_fail({
            # sudo -s or -i and runuser -u with no command run a shell
            "sudo -s <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sh heredoc):1 (python3 -c)",
            "sudo -i -u nobody <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sh heredoc):1 (python3 -c)",
            "sudo -iu nobody <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sh heredoc):1 (python3 -c)",
            "runuser -u nobody <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sh heredoc):1 (python3 -c)",
            "sudo -s\n": "cannot read: scripts/s.sh:2: sh reads its code from its standard input",
            # source and . of a stdin path or a process substitution; a shell whose script is a process substitution
            "source /dev/stdin <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (source heredoc):1 (python3 -c)",
            ". /dev/fd/0 <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (. heredoc):1 (python3 -c)",
            "source <(cat <<'EOF'\npython3 -c 'print(1)'\nEOF\n)\n": "source runs a process substitution",
            "bash <(printf '%s\\n' \"python3 -c 'print(1)'\")\n": "bash runs a process substitution",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nsudo -s <<'EOF'\npython3 -c 'print(1)'\nEOF\nsudo -v\nsudo -k\n"
            "source /dev/stdin <<'EOF'\npython3 -c 'print(2)'\nEOF\n")
        self.passes(d)

    def test_shell_code_that_runs_outer_expansions_or_its_arguments(self):
        self.scripts_fail({
            # an expansion of the word that gave the code, as a whole command: the shell reads what it holds as code
            "cmd=\"python3 -c 'if True'\"\nbash -c \"set -e; $cmd\"\n": "(bash -c):1: a command that is only an expansion",
            "cmd=\"python3 -c 'if True'\"\nbash -c \"if true; then $cmd; fi\"\n": "(bash -c):1: a command that is only an expansion",
            # code that runs its own arguments ($0 and "$@"), also through a wrapper and a function
            "bash -c '\"$@\"' _ python3 -c 'if True'\n": "scripts/s.sh:2 (bash -c):2 (python3 -c)",
            "sh -c 'exec \"$0\" \"$@\"' python3 -c 'if True'\n": "scripts/s.sh:2 (sh -c):2 (python3 -c)",
            "nice bash -c 'cd /tmp && \"$@\"' _ python3 -c 'if True'\n": "scripts/s.sh:2 (bash -c):2 (python3 -c)",
            "bash -c '\"${@:2}\"' _ x python3 -c 'if True'\n": "scripts/s.sh:2 (bash -c):2 (python3 -c)",
            "f() { bash -c '\"$@\"' _ \"$@\"; }\nf python3 -c 'print(1)'\nf python3 -c 'if True'\n": "(bash -c):4 (python3 -c)",
        })
        d = self.tree()                     # (round 6: SUDO set once to a word, see test_outer_expansions_before_words)
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nSUDO=sudo\nbash -c \"$SUDO make\"\nbash -c \"cd /tmp && $SUDO make install\"\n"
            "bash -c '\"$@\"' _ python3 -c 'print(1)'\nsh -c 'exec \"$0\" \"$@\"' python3 -c 'print(2)'\n")
        self.passes(d)

    def test_functions_that_shift_or_read_the_calls_input(self):
        retry = "retry() { local n=$1; shift; for i in $(seq \"$n\"); do \"$@\" && return 0; done; return 1; }\n"
        self.scripts_fail({
            retry + "retry 3 python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            # a shift that may not run in this shell, or by a count not known: the call is read with every word
            "f() { [ \"$1\" = -v ] && shift; \"$@\"; }\nf python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "f() { if [ \"$1\" = -v ]; then shift; fi; \"$@\"; }\nf python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "f() { shift \"$n\"; \"$@\"; }\nf python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "f() { x=$(shift); shift | cat; \"$@\"; }\nf python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "f() { \"$@\"; shift; }\nf python3 -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "f() {\n  if [ \"$1\" = -v ]; then\n    shift\n  fi\n  \"$@\"\n}\nf python3 -c 'if True'\n": "scripts/s.sh:8 (python3 -c)",
            "f() {\n  case $1 in\n    -v) shift ;;\n  esac\n  \"$@\"\n}\nf python3 -c 'if True'\n": "scripts/s.sh:8 (python3 -c)",
            # a function command that reads its program from the call's standard input
            "py() { python3 -; }\npy <<'EOF'\nif True\nEOF\n": "scripts/s.sh:4 (heredoc)",
            "py() { python3 -; }\npy\n": "cannot read: scripts/s.sh:2: python reads its program from its standard input",
            "sh_() { bash; }\nsh_ <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:4 (bash heredoc):1 (python3 -c)",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\n" + retry + "retry 3 python3 -c 'print(1)'\nstep() { echo \"== $1\"; shift; \"$@\"; }\n"
            "step compile python3 -c 'print(1)'\nrun2() { shift 2; \"$@\"; }\nrun2 a b python3 -c 'print(1)'\n"
            "timed() { local t=$1; shift; timeout \"$t\" \"$@\"; }\ntimed 5 python3 -c 'print(1)'\n"
            "as() { local u=$1; shift; sudo -u \"$u\" \"$@\"; }\nas nobody python3 -c 'print(1)'\n"
            "py() { python3 -; }\npy <<'EOF'\nprint(1)\nEOF\nnever() { python3 -; }\n"
            "bq() { x=`date`; shift; \"$@\"; }\nbq label python3 -c 'print(1)'\n")
        self.passes(d)

    def test_variables_set_once_to_a_program(self):
        self.scripts_fail({
            "PYBIN=python3\n$PYBIN -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "SH=bash\n$SH -c 'python3 -c \"if True\"'\n": "scripts/s.sh:3 (bash -c):1 (python3 -c)",
            # set twice, also by a read, or by a default: the program is in a variable
            "JQ=jq\nJQ=python3\n\"$JQ\" -c 'x'\n": "$JQ -c: the program is in a variable",
            "JQ=jq\nread -r JQ\n\"$JQ\" -c 'x'\n": "$JQ -c: the program is in a variable",
            "JQ=jq\n\"${JQ:-jq}\" -c 'x'\n": "${JQ:-jq} -c: the program is in a variable",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nJQ=jq\necho '{}' | \"$JQ\" -c .\nCURL=curl\n$CURL -m 5 -sS http://127.0.0.1:1/ || true\n"
            "TAR=tar\n$TAR -cf out.tar dir\nSSH=ssh\n$SSH -c aes128-ctr host true\nSORT=sort\nprintf 'b\\n' | $SORT -\n")
        self.passes(d)

    def test_more_forms_the_lexer_reads(self):
        self.scripts_fail({
            # time and coproc before ( and { }
            "time ( python3 -c 'if True' )\n": "scripts/s.sh:2 (python3 -c)",
            "coproc P ( python3 -c 'if True' )\n": "scripts/s.sh:2 (python3 -c)",
            "coproc Q { python3 -c 'if True'; }\n": "scripts/s.sh:2 (python3 -c)",
            # a heredoc in $( ) or <( ) whose ")" comes first: bash gives it no body and runs its lines
            "x=$(cat <<'EOF')\npython3 -c 'if True'\nEOF\n": "the heredoc EOF in $( ) or <( ), whose \")\" comes before its body",
            "cat <(cat <<'EOF')\npython3 -c 'if True'\nEOF\n": "the heredoc EOF in $( ) or <( ), whose \")\" comes before its body",
            # programs that run shell code: script -c, env -vS, bash with --rcfile or --init-file
            "script -qc \"python3 -c 'if True'\" /dev/null\n": "scripts/s.sh:2 (script -c):1 (python3 -c)",
            "env -vS 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (env -S):1 (python3 -c)",
            "bash --rcfile /dev/null -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (bash -c):1 (python3 -c)",
            "bash --init-file /dev/null -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (bash -c):1 (python3 -c)",
            # a lone surrogate in what the step prints
            "cat <<$'\\ud800'\nx\n": "cannot read: scripts/s.sh:2: the heredoc \\ud800 has no end",
            # the work that the calls of a function take is counted: words built too
            "f() { echo " + " ".join(['"$@"'] * 200) + "; }\nf " + " ".join(f"w{i}" for i in range(200)) + "\n":
                "more calls of the script's functions (with their commands and words) than this check follows",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\ntime ( python3 -c 'print(1)' )\ntime -p ( python3 -c 'print(2)' )\n"
            "coproc ( python3 -c 'print(3)' )\nwait\ncoproc P { python3 -c 'print(4)'; }\ncoproc python3 -c 'print(5)'\n"
            "(cat <<'EOF')\nx\nEOF\nx=$(cat <<'EOF'\ny\nEOF\n)\nscript -qc \"python3 -c 'print(6)'\" /dev/null\n")
        self.passes(d)

    def test_files_written_in_parts(self):
        self.scripts_fail({
            "cat > x.py <<'EOF'\ndef f():\nEOF\ncat >> x.py <<'EOF'\nreturn 1\nEOF\n":
                "scripts/s.sh:3 (a file this heredoc writes): expected an indented block",
            "cat > x.sh <<'EOF'\n#!/bin/bash\nEOF\ncat >> x.sh <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:3 (a file this heredoc writes):2 (python3 -c)",
            "printf 'python3 -c \"if True\"\\n' > lib.sh\n. ./lib.sh\n": "scripts/s.sh:2 (a file printf writes):1 (python3 -c)",
            "printf '%s\\n' \"python3 -c 'print(1)'\" > lib.sh\n. ./lib.sh\n":
                "cannot read: scripts/s.sh:3: . runs ./lib.sh, which the script wrote at scripts/s.sh:2 from words",
            # a file that grows by >> and runs after each part is read whole each time: those lines are counted
            "echo 'x = 1' >> f.py\npython3 f.py\n" * 250: "more lines of files written in parts and read again than",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\ncat > x.py <<'EOF'\ndef f():\nEOF\ncat >> x.py <<'EOF'\n    return 1\nEOF\npython3 x.py\n"
            "printf 'if True:\\n' > y.py\necho '    print(1)' >> y.py\npython3 y.py\necho 'if True:' > z.py\n"
            "echo '    pass' | tee -a z.py > /dev/null\n")
        self.passes(d)

    def test_tests_whose_body_never_runs(self):
        h = "import functools, os, unittest\n" + GOOD_TEST.replace("import unittest\n", "")
        cases = {
            h + "def gate(f):\n    @functools.wraps(f)\n    def w(self):\n        if not os.environ.get('NO_SUCH_VAR'):\n"
            "            return None\n        return f(self)\n    return w\nclass U(unittest.TestCase):\n    @gate\n"
            "    def test_h(self):\n        self.fail()\n":
                "test_r.U.test_h started, but its body never ran",
            h + "class U(unittest.TestCase):\n    def run(self, result=None):\n        return result\n"
            "    def test_u(self):\n        self.fail()\n":
                "test_r.U.test_u never started",
            h + "class U(unittest.TestCase):\n    async def _a(self):\n        self.fail()\n    def test_c(self):\n"
            "        return self._a()\n":
                "test_r.U.test_c returns a value (coroutine), which the runner drops",
            h + "match os.name:\n    case 'nt':\n        class W(unittest.TestCase):\n            def test_w(self):\n"
            "                self.fail()\n":
                "tests/test_r.py:8: W.test_w never runs",
            # the profiler is on again for the test after one that turned it off
            h + "import sys\ndef gate(f):\n    @functools.wraps(f)\n    def w(self):\n        return None\n    return w\n"
            "class U(unittest.TestCase):\n    def test_a(self):\n        sys.setprofile(None)\n    @gate\n"
            "    def test_b(self):\n        self.fail()\n":
                "test_r.U.test_b started, but its body never ran",
        }
        for text, says in cases.items():
            with self.subTest(says=says):
                d = self.tree()
                (d / "tests" / "test_r.py").write_text(text)
                self.fails(d, says)
        with self.subTest(says="a lone surrogate"):    # in a failure's message: the runner prints it, and its verdict
            d = self.tree()
            (d / "tests" / "test_r.py").write_text(GOOD_TEST.replace("self.assertTrue(True)", "self.fail('\\ud800')"))
            self.fails(d, "AssertionError: \\ud800")
        d = self.tree()                     # what runs passes: a classmethod, a test that turns the profiler off, and
        (d / "tests" / "test_r.py").write_text(       # the tests of a class or module skipped as a whole
            "import sys, unittest\nclass T(unittest.TestCase):\n    @classmethod\n    def test_c(cls):\n        pass\n"
            "    def test_p(self):\n        sys.setprofile(None)\n    def test_q(self):\n        pass\n"
            "class S(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
            "        raise unittest.SkipTest('harness: not asked for')\n    def test_s(self):\n        self.fail()\n"
            "class A(unittest.IsolatedAsyncioTestCase):\n    async def test_a(self):\n        pass\n")
        (d / "tests" / "test_m.py").write_text(
            "import unittest\ndef setUpModule():\n    raise unittest.SkipTest('harness: not asked for')\n"
            "class S(unittest.TestCase):\n    def test_s(self):\n        self.fail()\n")
        self.passes(d)

    def test_drop_ins_of_units_that_systemd_has_not(self):
        # each stand-in has what its type needs; a prefix drop-in is read beside a unit it is for; verify does not read
        # the drop-ins of this machine (/etc, /run): nvidia-persistenced's here resets ExecStart=
        for path, text in (("apt-daily.timer.d/50-x.conf", "[Timer]\nRandomizedDelaySec=1h\n"),
                           ("ssh.socket.d/50-x.conf", "[Socket]\nBacklog=64\n"),
                           ("zz-stand-in.socket.d/50-x.conf", "[Socket]\nBacklog=64\n"),
                           ("home.mount.d/50-x.conf", "[Mount]\nOptions=noatime\n"),
                           ("x.path.d/50-x.conf", "[Path]\nMakeDirectory=yes\n"),
                           ("user@1003.service.d/50-x.conf", "[Service]\nMemoryMax=4G\n"),
                           ("user-.slice.d/50-x.conf", "[Slice]\nMemoryMax=6G\n"),
                           ("foo@.service.d/50-x.conf", "[Service]\nEnvironment=A=1\n")):
            with self.subTest(path=path):
                d = self.tree()
                (d / "systemd" / path).parent.mkdir()
                (d / "systemd" / path).write_text(text)
                self.passes(d)
        cases = {
            ("user-.slice.d/50-x.conf", "[Slice]\nMemoryMaxx=6G\n"): "systemd/user-.slice.d/50-x.conf:2: Unknown key name 'MemoryMaxx'",
            ("ssh.socket.d/50-x.conf", "[Socket]\nBogusKey=1\n"): "systemd/ssh.socket.d/50-x.conf:2: Unknown key name 'BogusKey'",
            ("nvidia-persistenced.service.d/50-x.conf", "[Service]\nExecStart=/bin/true\n"):
                "nvidia-persistenced.service: Service has more than one ExecStart= setting",
        }
        for (path, text), says in cases.items():
            with self.subTest(path=path, text=text):
                d = self.tree()
                (d / "systemd" / path).parent.mkdir()
                (d / "systemd" / path).write_text(text)
                self.fails(d, says)

    def test_unit_programs_through_runuser_systemd_run_su_and_other_shells(self):
        lx = f"{LIVE}/libexec"
        self.units_fail({
            f"ExecStartPost={lx}/runuser -u nobody -- /usr/bin/python3 -c 'if True'": "/usr/bin/python3 -c: python in a unit",
            f"ExecStartPost={lx}/runuser -u nobody -- /opt/nonexistent/ru": "/opt/nonexistent/ru does not exist",
            f"ExecStartPost={lx}/systemd-run --scope -p MemoryMax=1G /usr/bin/python3 -m json.tool": "/usr/bin/python3 -m: python",
            f"ExecStartPost={lx}/su -s /bin/sh nobody -c 'python3 -c x'": f"{lx}/su runs python, which this check does not follow",
            f"ExecStartPost={lx}/runuser nobody -c 'python3 -c x'": f"{lx}/runuser runs python, which this check does not follow",
            f"ExecStartPost={lx}/zsh -c 'python3 -c x'": f"{lx}/zsh runs python, which this check does not follow",
        })
        with self.subTest(verify="a template's instance"):     # verify names it foo@i.service: a finding, as such
            d = self.tree()
            (d / "systemd" / "foo@.service").write_text("[Unit]\nDescription=t\n\n[Service]\nExecStart=/bin/true\n"
                                                        "ExecStart=/bin/true\n")
            r = self.run_sh(d)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("foo@i.service: Service has more than one ExecStart= setting", r.stdout)
            self.assertNotIn("systemd-analyze verify exited with", r.stdout)
        with self.subTest(verify="a byte that is no UTF-8"):    # grep reads its output as text all the same
            d = self.tree()
            self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/true\nEnvironment=A=\\xff\n")
            self.fails(d, "Invalid environment assignment, ignoring: A=")
        with self.subTest(verify="a unit that is not UTF-8"):
            d = self.tree()
            (d / "systemd" / "x.service").write_bytes(b"[Unit]\nDescription=\xff\n[Service]\nExecStart=/bin/true\n")
            self.fails(d, "systemd/x.service: it is not UTF-8 (at byte 19)")
        with self.subTest(verify="a lone surrogate in sh -c code"):
            d = self.tree()
            self.unit(d, "x.service", "[Service]\nType=oneshot\nExecStart=/bin/sh -c 'echo \\ud800'\n")
            self.fails(d, "the shell code of /bin/sh -c cannot be given to /bin/sh: UnicodeEncodeError")
        with self.subTest(verify="its exit status"):           # alone, it fails the run
            d = self.tree()
            self.program(d, "systemd-analyze", "#!/bin/sh\necho 'something about another unit'\nexit 1\n")
            self.fails(d, "systemd-analyze verify exited with 1:")
        with self.subTest(verify='a TMPDIR with ".."'):        # verify names the files by another path then
            d = self.tree()
            (d / "systemd" / "x.service.d").mkdir()
            (d / "systemd" / "x.service.d" / "a.conf").write_text("[Service]\nBogusKey=1\n")
            (d.parent / "t" / "sub").mkdir(parents=True)
            self.fails(d, "systemd/x.service.d/a.conf:2: Unknown key name 'BogusKey'",
                       env={"TMPDIR": str(d.parent / "t" / "sub" / "..")})

    def test_shell_scripts_the_steps_do_not_read_anywhere_in_the_repo(self):
        for path, text in (("docs/x.sh", "#!/bin/bash\necho x\n"), ("scripts/x.bash", "echo x\n"),
                           ("examples/run", "#!/bin/sh\necho x\n")):
            with self.subTest(path=path):
                d = self.tree()
                (d / path).parent.mkdir(exist_ok=True)
                (d / path).write_text(text)
                self.fails(d, f"{path}: a shell script that bash -n, shellcheck and the Python step do not read")
        d = self.tree()                     # results/ holds raw measurements
        (d / "results").mkdir()
        (d / "results" / "run.sh").write_text("#!/bin/bash\necho x\n")
        self.passes(d)

    def test_forms_round_4_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the fifth review's mutation testing left alive
        self.scripts_fail({
            "bash -o pipefail -c 'python3 -c \"if True\"'\n": "scripts/s.sh:2 (bash -c):1 (python3 -c)",            # AN02
            "python3 - \"$@\" <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",                                 # AN03
            "python3 /proc/self/fd/0 <<'EOF'\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",                          # AN04
            "bash -c \"$(cat x) && true\"\n": "bash -c runs what a variable or a command gives",                      # AN13
            "strace -f python3 - <<'EOF'\nif True\nEOF\n": "strace is given a word that names python",              # AN15
            "find . -exec echo {} + -exec python3 -c 'if True' {} \;\n": "scripts/s.sh:2 (python3 -c)",              # AN18
            "python3 - <<'EOF'\nprint(1)\n  EOF\nif True\nEOF\n": "scripts/s.sh:3 (heredoc)",                       # LX02
            "(( $(python3 -c 'if True') ))\n": "scripts/s.sh:2 (python3 -c)",                                       # LX05
            "echo a&python3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",                                         # LX08
            "cat <<'EOF' &> x.py\nif True\nEOF\n": "scripts/s.sh:3 (a file this heredoc writes)",                   # LX16
            "{ python3 - <<'A'; } <<'B'\nif True\nA\nprint(1)\nB\n": "scripts/s.sh:3 (heredoc)",                    # LX17
        })
        d = self.tree()                     # SB01: a .sh file is a shell script also without a #! line
        (d / "scripts" / "lib").mkdir()
        (d / "scripts" / "lib" / "helper.sh").write_text("if\n")
        self.fails(d, "scripts/lib/helper.sh: a shell script that bash -n")
        self.units_fail({
            "ExecStartPost=/bin/sh -c 'exec\\s/opt/nonexistent/q'": "/opt/nonexistent/q does not exist",            # UN03
            "ExecStartPost=/bin/sh -c 'echo \\047'": "the shell code of /bin/sh -c does not parse",                 # UN04
        })
        d = self.tree()                     # AN01, AN05, AN10, LX04, LX06, LX14, LX15: forms that must pass
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\npython3 -c \"import $m\"\nuntil python3 -c 'print(1)'; do :; done\nprintf 'x = 100 %% 7\\n' > x.py\n"
            "case x in x) : ;& y) python3 -c 'print(1)' ;; esac\npython3 -c \"x = 1$((2))\"\n"
            "x=$([[ a == a ]]); python3 -c 'print(1)'\nf()\n{\n  python3 -c 'print(1)'\n}\nf\n")
        (d / "tests" / "test_cm.py").write_text(                                                                 # RN13
            "import unittest\nclass T(unittest.TestCase):\n    @classmethod\n    def test_c(cls):\n        pass\n")
        self.passes(d)

    # round 6: the forms the sixth review found read wrongly; each test but the last fails with the scripts/test.sh
    # before round 6 (the last one holds the cases that kill the mutants the sixth review found alive)
    def test_outer_expansions_before_words(self):
        # an expansion of the word that gives a shell its code, unquoted before more words, is code that the shell
        # reads; it is read as a word only when the script sets it once, at its top level, to one (SUDO=sudo)
        cmd = "cmd=\"python3 -c 'if True'\"\n"
        says = "a command that is only an expansion of the word that gave this shell its code, or starts with one"
        self.scripts_fail({
            cmd + "bash -c \"$cmd -v\"\n": f"scripts/s.sh:3 (bash -c):1: {says}",
            cmd + "bash -c \"$cmd config/profile.conf\"\n": f"scripts/s.sh:3 (bash -c):1: {says}",
            cmd + "sudo bash -c \"$cmd --check\"\n": f"scripts/s.sh:3 (bash -c):1: {says}",
            cmd + "bash -c \"$cmd \\\"\\$@\\\"\" _ x\n": f"scripts/s.sh:3 (bash -c):1: {says}",
            cmd + "trap \"$cmd x\" EXIT\n": f"scripts/s.sh:3 (trap):1: {says}",
            cmd + "su -c \"$cmd x\" nobody\n": f"scripts/s.sh:3 (su -c):1: {says}",
            cmd + "flock /tmp/l -c \"$cmd x\"\n": f"scripts/s.sh:3 (flock -c):1: {says}",
            cmd + "script -qc \"$cmd x\" /dev/null\n": f"scripts/s.sh:3 (script -c):1: {says}",
            cmd + "watch -n 1 \"$cmd x\"\n": f"scripts/s.sh:3 (watch):1: {says}",
            "bash -c \"$SUDO make\"\n": f"scripts/s.sh:2 (bash -c):1: {says}",                 # not set in the script
            "SUDO=\n[ -n \"$x\" ] && SUDO=sudo\nbash -c \"$SUDO make\"\n": f"scripts/s.sh:4 (bash -c):1: {says}",
            "P=python3\nbash -c \"$P -c 'if True'\"\n": "scripts/s.sh:3 (bash -c):1 (python3 -c)",   # its value is read
            "SUDO=sudo\nstrace -f bash -c \"$SUDO -n true\"\n":        # round 8: a shell behind strace is not read
                "scripts/s.sh:3: bash (after strace) runs what it is given as a command line, and is given $SUDO -n true",
        })
        d = self.tree()                     # a quoted expansion is one word: a program, as a variable is
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nSUDO=sudo\nbash -c \"$SUDO make\"\nC=cleanup\n"
                                            "trap \"$C x\" EXIT\nsu -c \"$SUDO -n true\" nobody\n"
                                            "watch -n 1 \"$SUDO -n true\"\n"
                                            "bash -c \"\\\"$TOOL\\\" --version\"\n")
        self.passes(d)

    def test_tests_behind_decorators_without_wraps_and_helpers_named_test(self):
        # the profiler decides: a test behind a decorator without functools.wraps runs when its function runs (or when
        # the decorator skipped its test), a helper whose name starts with test when it runs (also at import), and a
        # test that saves the profile function and sets it back keeps the profiler
        h = "import functools, os, sys, unittest\n"
        d = self.tree()
        (d / "tests" / "test_r.py").write_text(
            h + "def deco(f):\n    def w(self):\n        return f(self)\n    return w\n"
            "def needs(f):\n    def w(self):\n        if True:\n            self.skipTest('harness: not here')\n"
            "        return f(self)\n    return w\ndef testdata(name):\n    return name\ndef testcases():\n    return 2\n"
            "N = testcases()\nclass T(unittest.TestCase):\n    @deco\n    def test_a(self):\n"
            "        self.assertEqual(testdata('a'), 'a')\n    @needs\n    def test_b(self):\n        self.fail()\n"
            "    @needs\n    @deco\n    def test_e(self):\n        self.fail()\n"
            "    def test_c(self):\n        old = sys.getprofile()\n        sys.setprofile(None)\n"
            "        sys.setprofile(old)\nclass S(unittest.TestCase):\n    pass\nfor n in range(N):\n"
            "    def test(self, n=n):\n        self.assertLess(n, 2)\n    setattr(S, f'test_{n}', test)\n")
        (d / "tests" / "test_f.py").write_text(
            "import unittest\ndef check_sum():\n    assert sum([1, 2]) == 3\ndef load_tests(loader, tests, pattern):\n"
            "    tests.addTest(unittest.FunctionTestCase(check_sum))\n    return tests\n")
        self.passes(d)
        cases = {   # what does not run fails still: a decorator that holds the test but returns without calling it
            h + "def gate(f):\n    def w(self):\n        if not os.environ.get('NO_SUCH_VAR'):\n            return None\n"
            "        return f(self)\n    return w\nclass U(unittest.TestCase):\n    @gate\n    def test_g(self):\n"
            "        self.fail()\n": "tests/test_r.py:10: U.test_g never runs",
            h + "def gate(f):\n    @functools.wraps(f)\n    def w(self):\n        return None\n    return w\n"
            "class U(unittest.TestCase):\n    def test_a(self):\n        old = sys.getprofile()\n        sys.setprofile(None)\n"
            "        sys.setprofile(old)\n    @gate\n    def test_b(self):\n        self.fail()\n":
                "test_r.U.test_b started, but its body never ran",
            h + "def testdata():\n    return 1\nclass U(unittest.TestCase):\n    def test_u(self):\n        pass\n":
                "tests/test_r.py:2: testdata is not in a TestCase, so it never runs",
        }
        for text, says in cases.items():
            with self.subTest(says=says):
                d = self.tree()
                (d / "tests" / "test_r.py").write_text(text)
                self.fails(d, says)

    def test_variables_set_once_only_at_the_top_level(self):
        # a variable named like PY is python whatever it is set to; a value counts only from an assignment at the
        # script's top level that always runs: not in a function, an if, a case or a loop, nor after && or ||
        self.scripts_fail({
            "PY=pypy3\n$PY -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "PY=python3-dbg\n$PY -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "[ -n \"$PYBIN\" ] || PYBIN=jq\n$PYBIN -c 'if True'\n": "scripts/s.sh:3 (python3 -c)",
            "f() { PY=jq; }\n$PY -c 'if True'\nf\n": "scripts/s.sh:3 (python3 -c)",
            "PY=jq\ncat > env.sh <<'EOF'\nPY=python3\nEOF\n. ./env.sh\n$PY -c 'if True'\n": "scripts/s.sh:7 (python3 -c)",
            "f() { local J=jq; \"$J\" -c .; }\nf\n": "$J -c: the program is in a variable",
            "f() { J=jq; }\n\"$J\" -c 'x'\nf\n": "$J -c: the program is in a variable",
            "if true; then\n  J=jq\nfi\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
            "case $x in a) J=jq ;; esac\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
            "for i in 1; do J=jq; done\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
            "true && J=jq\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
            "J=jq\ncat > env.sh <<'EOF'\nJ=python3\nEOF\n. ./env.sh\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
        })
        d = self.tree()                     # set once at the top level, also after an if and a loop that set nothing
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nif true; then\n  echo x\nfi\nfor i in 1; do :; done\nJQ=jq\necho '{}' | \"$JQ\" -c .\n"
            "export CURL=curl\n$CURL -m 5 -sS http://127.0.0.1:1/ || true\n")
        self.passes(d)

    def test_python_behind_a_program_it_does_not_follow_from_a_file(self):
        # python's program on the standard input from a file of the repository is read (a .py file is compiled by the
        # walk of the .py files), and /dev/null is no program; another file cannot be read (round 8: python behind a
        # program this check does not follow is refused for the word python3)
        d = self.tree()
        (d / "tools" / "x.py").write_text("print(1)\n")
        (d / "tools" / "gen").write_text("print(2)\n")
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\npython3 < tools/x.py\npython3 - < ./tools/gen\n"
                                            "python3 < /dev/null\n")
        self.passes(d)
        for body, says in (("python3 < tools/gen\n", "syntax error: tools/gen"),
                           ("python3 < .cache/x.py\n", "syntax error: .cache/x.py"),                    # the walk skips it
                           ("python3 < /etc/x.py\n", "python reads its program from /etc/x.py, which this check does"),
                           ("python3 < tools/none.py\n", "python reads its program from tools/none.py"),
                           ("python3 < tools\n", "python reads its program from tools, which"),           # a directory
                           ("python3 < ../outside.py\n", "python reads its program from ../outside.py"),
                           ("python3 < ../repo2/x.py\n", "python reads its program from ../repo2/x.py"),
                           ("docker run -i img python3 < ./tools/x.py\n", "docker is given a word that names python"),
                           ("docker run -i img python3 <<'EOF'\nprint(1)\nEOF\n", "docker is given a word that names")):
            with self.subTest(body=body):
                d = self.tree()
                (d / "tools" / "gen").write_text("if True\n")
                (d / ".cache").mkdir()
                (d / ".cache" / "x.py").write_text("if True\n")
                (d.parent / "outside.py").write_text("print(1)\n")
                (d.parent / "repo2").mkdir()                # a directory beside the repository, named as it begins
                (d.parent / "repo2" / "x.py").write_text("print(1)\n")
                (d / "scripts" / "s.sh").write_text("#!/bin/bash\n" + body)
                self.fails(d, says)

    def test_files_that_are_not_regular(self):
        # a FIFO is named and not opened: opening it would wait for a writer (a step that did hung the run)
        for path in ("docs/fifo", "docs/x.py", "scripts/x.sh", "tests/test_fifo.py", "results/x.py"):
            with self.subTest(path=path):
                d = self.tree()
                (d / path).parent.mkdir(exist_ok=True)
                os.mkfifo(d / path)
                try:
                    r = self.run_sh(d, timeout=60)
                    out = r.stdout
                except subprocess.TimeoutExpired:
                    out = "(the run did not end)"
                try:                        # a step that waits on the FIFO: let it go, and fail
                    os.close(os.open(d / path, os.O_WRONLY | os.O_NONBLOCK))
                    out += "\n(a step waited on the FIFO)"
                except OSError:
                    pass
                self.assertIn("not a regular file", out)
                self.assertNotIn("(a step waited on the FIFO)", out)
                self.assertEqual(out.splitlines()[-1:], ["FAILED"], out)

    def test_shell_code_given_its_arguments_counts_its_length(self):
        # code with "$@" is read again at each call: its length counts toward the 20000, so a long one called often
        # gives up instead of taking minutes
        self.scripts_fail({
            "f() { bash -c '\"$@\"; echo \"" + "x" * 20000 + "\"' _ \"$@\"; }\n" + "f true\n" * 1000:
                "more calls of the script's functions (with their commands and words) than this check follows",
        })

    def test_positional_parameters_sg_and_tmux(self):
        # "$1" to "$9" in -c code are the words after it; sg runs its command with sh -c; tmux's python is refused
        self.scripts_fail({
            "bash -c '\"$1\" \"$2\" \"$3\"' _ python3 -c 'if True'\n": "scripts/s.sh:2 (bash -c):2 (python3 -c)",
            "bash -c 'python3 -c \"$1\"' _ 'if True'\n": "scripts/s.sh:2 (bash -c):2 (python3 -c)",
            "sh -c 'exec \"${1}\" -c \"$2\"' sh python3 'if True'\n": "scripts/s.sh:2 (sh -c):2 (python3 -c)",
            "sg docker -c \"python3 -c 'if True'\"\n": "scripts/s.sh:2 (sg -c):1 (python3 -c)",
            "sg - docker \"python3 -c 'if True'\"\n": "scripts/s.sh:2 (sg -c):1 (python3 -c)",
            "sg docker <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sg heredoc):1 (python3 -c)",
            "tmux new-session -d \"python3 -c 'if True'\"\n": "scripts/s.sh:2: tmux is given a word that names python",
            "tmux new-window -t s \"python3 tool.py\"\n": "scripts/s.sh:2: tmux is given a word that names python",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nbash -c '\"$1\" \"$2\" \"$3\"' _ python3 -c 'print(1)'\nsg docker -c 'make test'\n"
            "tmux new-session -d -s work 'make test'\n")
        self.passes(d)

    def test_a_drop_in_for_a_distribution_unit(self):
        # verified beside a copy of the distribution's unit: a oneshot service may have a second ExecStart=
        if not Path("/lib/systemd/system/systemd-tmpfiles-clean.service").is_file():
            self.skipTest("machine: no systemd-tmpfiles-clean.service in /lib/systemd/system")
        d = self.tree()
        (d / "systemd" / "systemd-tmpfiles-clean.service.d").mkdir()
        (d / "systemd" / "systemd-tmpfiles-clean.service.d" / "50-x.conf").write_text("[Service]\nExecStart=/bin/true\n")
        self.passes(d)
        (d / "systemd" / "systemd-tmpfiles-clean.service.d" / "50-x.conf").write_text("[Service]\nBogusKey=1\n")
        self.fails(d, "systemd/systemd-tmpfiles-clean.service.d/50-x.conf:2: Unknown key name 'BogusKey'")

    def test_forms_round_5_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the sixth review's mutation testing left alive
        self.scripts_fail({
            "never() {\n  python3 - <<'EOF'\nif True\nEOF\n}\n": "scripts/s.sh:4 (heredoc)",                    # FN8
            "sudo --login <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sh heredoc):1 (python3 -c)",      # SU2
            "echo \"$code\" > lib.sh\necho 'echo x' >> lib.sh\n. ./lib.sh\n":                                       # WR4
                "cannot read: scripts/s.sh:4: . runs ./lib.sh, which the script wrote at scripts/s.sh:2",
        })
        d = self.tree()                     # SU3, VA4: forms that must pass; SB2: a dot directory is not read
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nsudo -usam\nbash -c 'J=jq; \"$J\" -c .'\n")
        (d / ".git" / "hooks").mkdir(parents=True)
        (d / ".git" / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\nexit 0\n")
        self.passes(d)
        self.units_fail({"ExecStartPost=/opt/\\ud800x": "/opt/\\ud800x does not exist on this machine"})        # UN13

    # round 7: the forms the seventh review found read wrongly; each test but the last fails with the scripts/test.sh
    # before round 7 (the last one holds the cases that kill the mutants the seventh review found alive)
    def test_ssh_at_and_command_lines_given_to_programs(self):
        # ssh's command runs in the other host's shell, at's and batch's standard input in a shell later; a program
        # the step does not follow, given a word that holds a command line of python, may run it (round 8: none of them
        # is read; what names python there, and an expansion given to one that runs a command line, is refused)
        says = "is given a word that names python"
        line = "runs what it is given as a command line, and is given"
        self.scripts_fail({
            "ssh host \"python3 -c 'if True'\"\n": f"scripts/s.sh:2: ssh {says}",
            "ssh host python3 -c 'if True'\n": f"scripts/s.sh:2: ssh {says}",
            "ssh -p 22 -o BatchMode=yes host -t \"python3 -c 'if True'\"\n": f"scripts/s.sh:2: ssh {says}",
            "ssh host 'python3 -' <<'EOF'\nif True\nEOF\n": f"scripts/s.sh:2: ssh {says}",
            "ssh host <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:2: ssh is given an input that names python",
            "cmd=\"python3 -c 'if True'\"\nssh host \"$cmd\"\n": f"scripts/s.sh:3: ssh {says} ($cmd)",   # (round 10: set to
            "cmd=\"make -k\"\nssh host \"$cmd\"\n": f"scripts/s.sh:3: ssh {line} $cmd, which this check",    # python: python's)
            "ssh \"$host\" \"$@\"\n": f"scripts/s.sh:2: ssh {line} $host, which this check cannot read",
            "cmd=\"python3 -c 'if True'\"\nssh host \"$cmd\" -v\n": f"scripts/s.sh:3: ssh is given a word that names python",
            "at now <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:2: at is given an input that names python",
            "echo \"python3 -c 'if True'\" | at now + 1 minute\n": "scripts/s.sh:2: at is given an input that names python",
            "batch <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:2: batch is given an input that names python",
            "tmux send-keys -t s \"python3 -c 'if True'\" Enter\n": f"scripts/s.sh:2: tmux {says}",
            "tmux new-session -d \"cd /tmp && python3 x.py\"\n": f"scripts/s.sh:2: tmux {says}",
            "screen -S s -X stuff \"python3 -c 'if True'\\n\"\n": f"scripts/s.sh:2: screen {says}",
            "parallel \"python3 -c 'if True' {}\" ::: a\n": f"scripts/s.sh:2: parallel {says}",
            "xterm -e \"exec /usr/bin/python3 x.py\"\n": f"scripts/s.sh:2: xterm {says}",
            "flock /tmp/l --command=\"python3 -c 'if True'\"\n": "scripts/s.sh:2 (flock -c):1 (python3 -c)",
            "flock --command=\"python3 -c 'if True'\" /tmp/l\n": "scripts/s.sh:2 (flock -c):1 (python3 -c)",
            "cat <<'EOF' | ssh host python3 -\nif True\nEOF\n": f"scripts/s.sh:2: ssh {says}",
            "cat > job.sh <<'EOF'\npython3 -c 'if True'\nEOF\nat -f job.sh now\n":
                "scripts/s.sh:5: at is given a file the script wrote that names python (job.sh)",
            "printf 'x = \\ue000\\n' > x.py\n": "printf writes a .py file from words this check cannot read",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nssh host uptime\nssh -N -L 9000:localhost:80 host\nssh -O exit host\n"
            "ssh host 'bash -s' <<'EOF'\nmake test\nEOF\nssh host < /dev/null\nssh -N host <<'EOF'\n( not shell code\nEOF\n"
            "echo 'make test' | at now\nat -l\ntmux new -s dev\nsay() { echo \"$@\"; }\nsay \"python3 -c failed\"\n")
        self.passes(d)

    def test_modules_and_tests_the_runner_once_refused(self):
        # a module skipped at its import, doctests of a tools module, a FunctionTestCase of functools.partial, a helper
        # named test... that only a skipped test calls (or a module that turned the profiler off), tests held as the
        # default of a decorator's argument or behind four decorators, and a profile hook that calls the old one
        h = "import functools, sys, unittest\n"
        skip = "        self.skipTest('harness: not asked for')\n"
        doc = "def f():\n    " + '"' * 3 + "\n    >>> 1 + 1\n    2\n    " + '"' * 3 + "\n"
        d = self.tree()
        files = {
            "tests/test_skipped.py": h + "raise unittest.SkipTest('harness: not asked for')\n",
            "tools/mod.py": doc,
            "tests/test_doc.py": "import doctest, os, sys, unittest\n"
                "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))\nimport mod\n"
                "def load_tests(loader, tests, pattern):\n    tests.addTests(doctest.DocTestSuite(mod))\n    return tests\n",
            "tests/test_partial.py": "import functools, unittest\ndef check(n):\n    assert n < 3\n"
                "def load_tests(loader, tests, pattern):\n    for n in range(3):\n"
                "        tests.addTest(unittest.FunctionTestCase(functools.partial(check, n)))\n    return tests\n",
            "tests/test_off.py": h + "sys.setprofile(None)\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n",
            "tests/test_r.py": h + "def test_env():\n    return {'A': '1'}\ndef testcases():\n    return 1\nN = testcases()\n"
                "def held(f):\n    def w(self, f=f):\n" + skip + "        return f(self)\n    return w\n"
                "def kw(f):\n    def w(self, *, f=f):\n" + skip + "        return f(self)\n    return w\n"
                "def d(f):\n    def w(self):\n        return f(self)\n    return w\n"
                "def s(f):\n    def w(self):\n" + skip + "        return f(self)\n    return w\n"
                "class T(unittest.TestCase):\n    @unittest.skip('harness: not asked for')\n    def test_h(self):\n"
                "        self.assertEqual(test_env()['A'], '1')\n    @held\n    def test_d(self):\n        self.fail()\n"
                "    @kw\n    def test_k(self):\n        self.fail()\n"
                "    @s\n    @d\n    @d\n    @d\n    def test_f(self):\n        self.fail()\n    def test_hook(self):\n"
                "        old, calls = sys.getprofile(), []\n        def hook(frame, event, arg):\n"
                "            calls.append(event)\n            if old is not None:\n                old(frame, event, arg)\n"
                "        sys.setprofile(hook)\n        try:\n            len([1])\n            abs(-1)\n        finally:\n"
                "            sys.setprofile(old)\n        self.assertGreaterEqual(len(calls), 2, calls)\n"
                "    def test_n(self):\n        self.assertEqual(N, 1)\n",
            "tests/test_free.py": "import unittest\n@unittest.skip('harness: not asked for')\ndef test_f():\n    assert False\n"
                "def load_tests(loader, tests, pattern):\n    tests.addTest(unittest.FunctionTestCase(test_f))\n"
                "    return tests\n",
            "tests/test_base.py": "from test_good import T\n",
        }
        for path, text in files.items():
            (d / path).write_text(text)
        self.passes(d)
        d = self.tree()                     # a whole module skipped for the machine: only with --repo-only
        (d / "tests" / "test_m.py").write_text(h + "raise unittest.SkipTest('machine: needs this machine')\n")
        self.passes(d, "--repo-only")
        self.fails(d, "was skipped: machine: needs this machine")
        d = self.tree()                     # a FunctionTestCase of a partial that turns the profiler off: no traceback
        (d / "tests" / "test_p.py").write_text(
            "import functools, sys, unittest\ndef load_tests(loader, tests, pattern):\n"
            "    tests.addTest(unittest.FunctionTestCase(functools.partial(sys.setprofile, None)))\n    return tests\n")
        self.passes(d)

    def test_units_with_their_documentation_and_the_distributions_own_findings(self):
        # verify runs man for each Documentation=man: unless --man=no (CI's container has no man); and what it finds in
        # a copy of the distribution's unit alone is no finding of the drop-in beside it
        d = self.tree()
        self.program(d, "man", "#!/bin/sh\nexit 1\n")
        for path, text in (("user-1003.slice.d/50-memory.conf", "[Slice]\nMemoryMax=6G\n"),
                           ("user-.slice.d/50-x.conf", "[Slice]\nMemoryMax=6G\n"),
                           ("systemd-tmpfiles-clean.service.d/50-x.conf",
                            "[Service]\nExecStart=/bin/true\nExecStart=/bin/true\n"),
                           ("rc-local.service.d/50-x.conf", "[Service]\nEnvironment=A=1\n"),
                           # where snapd is installed: its unit has a key that this systemd does not know, by its path
                           ("snapd.service.d/50-x.conf", "[Service]\nEnvironment=A=1\n")):
            (d / "systemd" / path).parent.mkdir(exist_ok=True)
            (d / "systemd" / path).write_text(text)
        self.unit(d, "x.service", "[Unit]\nDocumentation=man:nosuchpage(8)\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
        self.passes(d)
        self.passes(d, "--repo-only")
        (d / "systemd" / "rc-local.service.d" / "50-x.conf").write_text("[Service]\nBogusKey=1\n")
        self.fails(d, "systemd/rc-local.service.d/50-x.conf:2: Unknown key name 'BogusKey'")

    def test_arrays_set_once_rules_and_the_placeholder(self):
        # an array's words in -c code run nothing; time before a loop or an if, and declare -n, give no value set once;
        # $'...' that gives U+E000, the step's own placeholder, is refused; time ! ( ) is a subshell
        self.scripts_fail({
            "time while [ -z \"$J\" ]; do\n  J=jq\ndone\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
            "time -p if [ -z \"$J\" ]; then\n  J=jq\nfi\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",
            "K=python3\ndeclare -n J=K\n\"$J\" -c 'if True'\n": "$J -c: the program is in a variable",
            "python3 -c $'print(1)\\n\\ue000 = 1'\n": "a $'...' that gives the character U+E000",
            "time ! ( python3 -c 'if True' )\n": "scripts/s.sh:2 (python3 -c)",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nOPTS=\"$1\"\nbash -c \"a=($OPTS --verbose); printf '%s\\n' \\\"\\${a[@]}\\\"\"\n"
            "bash -c \"a=($OPTS)\"\ntime ! ( python3 -c 'print(1)' )\n")
        self.passes(d)

    def test_a_bench_without_shell_scripts(self):
        # a glob that matches nothing is no file for bash -n
        d = self.tree()
        (d / "bench" / "b.sh").unlink()
        self.passes(d)

    def test_forms_round_6_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the seventh review's mutation testing left alive
        h = "import functools, sys, unittest\n"
        skip = "        self.skipTest('harness: not asked for')\n"
        d = self.tree()                     # N02, N03, N05, N35, N36: tests and helpers that run, or were skipped
        (d / "tests" / "test_r.py").write_text(
            h + "REG = []\ndef register(f):\n    REG.append(f)\n    return f\n@register\ndef testcase_one():\n    return 1\n"
            "def deco(f):\n    def w(self):\n        return f(self)\n    return w\n"
            "def gap(f):\n    def w(self):\n        if False:\n            print(gone)\n" + skip + "        return f(self)\n"
            "    gone = 1\n    del gone\n    return w\n"
            "class T(unittest.TestCase):\n    def test_all(self):\n        def test_never():\n            pass\n"
            "        for f in REG:\n            self.assertEqual(f(), 1)\n    @gap\n    def test_g(self):\n        self.fail()\n"
            "class S(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
            "        raise unittest.SkipTest('harness: not asked for')\n    @deco\n    def test_s(self):\n        self.fail()\n")
        (d / "tools" / "linked_test.py").write_text(GOOD_TEST)        # N06, N09, N12: links to regular files
        (d / "tests" / "test_link.py").symlink_to("../tools/linked_test.py")
        (d / "docs").mkdir()
        (d / "docs" / "notes.txt").write_text("x\n")
        (d / "docs" / "link.txt").symlink_to("notes.txt")
        (d / "tools" / "x.py").write_text("print(1)\n")
        (d / "tools" / "alias.py").symlink_to("x.py")
        (d / "scripts" / "s.sh").write_text(                          # N27, N28, N29, N30, N32, N43: values set once
            "#!/bin/bash\nSUDO=sudo\nsu --command=\"$SUDO -n true\" nobody\nsu -lc \"$SUDO -n true\" nobody\n"
            "sg docker \"$SUDO -n true\"\nflock /tmp/l -c \"$SUDO -n true\"\nbash -c \"${SUDO} make\"\n"
            "bash -c ': " + "x" * 1_700_000 + "'\n")                   # N37: code read once is not charged
        self.passes(d)
        self.scripts_fail({
            "until true; do\n  J=jq\ndone\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",            # N39
            "select i in a; do\n  J=jq\ndone\n\"$J\" -c 'x'\n": "$J -c: the program is in a variable",         # N39
            "bash -c '\"$9\" -c \"if True\"' _ 1 2 3 4 5 6 7 8 python3\n": "scripts/s.sh:2 (bash -c):1 (python3 -c)",  # N20
            "f() { bash -c '\"$@\"; echo \"" + "x" * 8000 + "\"' _ \"$@\"; }\n" + "f true\n" * 200:              # N19
                "more calls of the script's functions (with their commands and words) than this check follows",
        })
        for path, text, says in (("docs/null", {"link": "/dev/null"}, "docs/null: not a regular file"),     # N41
                                 ("scripts/x.sh", None, "scripts/x.sh"),                                    # N11
                                 ("systemd/hwclock.service.d/50-x.conf", "[Service]\nExecStart=/bin/true\n",  # N31
                                  "hwclock.service: Service has more than one ExecStart= setting")):
            with self.subTest(path=path):
                d = self.tree()
                (d / path).parent.mkdir(exist_ok=True)
                if isinstance(text, dict):
                    (d / path).symlink_to(text["link"])
                elif text is None:          # a directory named like a shell script
                    (d / path).mkdir()
                else:
                    (d / path).write_text(text)
                self.fails(d, says)

    # round 8: the forms the eighth review found read wrongly, under the rules the maintainer chose for the programs
    # the step does not follow (what names python there, or may run what the step cannot read, fails); each test but
    # the last fails with the scripts/test.sh before round 8 (the last one holds the cases that kill the mutants the
    # eighth review found alive)
    def test_programs_it_does_not_follow_are_given_no_python(self):
        # a word, a variable set once, a file the script wrote or an input that names python, given to a program the
        # step does not follow, fails: the program may run it; echo, printf, logger, the script's functions (also from
        # a file that it sources) and the words of for, select, case, [[ ]] and arrays are data
        says = "is given a word that names python"
        self.scripts_fail({
            "tmux new-window -d \"PYTHONPATH=tools python3 -c 'if True'\"\n": f"scripts/s.sh:2: tmux {says}",
            "tmux new-window -d \"timeout 60 python3 -c 'if True'\"\n": f"scripts/s.sh:2: tmux {says}",
            "tmux new-window -d \"cd /x && X=1 python3 -c 'if True'\"\n": f"scripts/s.sh:2: tmux {says}",
            "tmux new-window -d \"uv run python -c 'if True'\"\n": f"scripts/s.sh:2: tmux {says}",
            "tmux new-window -d \"/usr/bin/env python3 -c 'if True'\"\n": f"scripts/s.sh:2: tmux {says}",
            "mail -s \"python3 job failed\" root < /dev/null\n": f"scripts/s.sh:2: mail {says}",
            "perl -pi -e 's/python3 -m old/python3 -m new/' notes.txt\n": f"scripts/s.sh:2: perl {says}",
            "journalctl --user -g \"python3 server.py\" -n 5\n": f"scripts/s.sh:2: journalctl {says}",
            "notify-send \"python3 finished\"\n": f"scripts/s.sh:2: notify-send {says}",
            "whiptail --msgbox \"python3 is not installed\" 8 40\n": f"scripts/s.sh:2: whiptail {says}",
            "jq --arg cmd \"python3 -m http.server\" '.cmd = $cmd' f.json\n": f"scripts/s.sh:2: jq {says}",
            "getopt -o h -n \"python3 wrapper\" -- \"$@\" > /dev/null\n": f"scripts/s.sh:2: getopt {says}",
            "docker run --rm img python3 -c 'print(1)'\n": f"scripts/s.sh:2: docker {says}",
            "command -v python3 > /dev/null || die \"python3 is required\"\n": f"scripts/s.sh:2: die {says}",
            "INTERP=python3\ndocker run --rm img \"$INTERP\" tool.py\n": f"scripts/s.sh:3: docker {says} ($INTERP)",
            "docker run --rm img \"$VENV_PY\" tool.py\n": f"scripts/s.sh:2: docker {says} ($VENV_PY)",
            "cat > job.txt <<'EOF'\npython3 x.py\nEOF\nscp job.txt host:\n":
                "scripts/s.sh:5: scp is given a file the script wrote that names python (job.txt)",
            "mail -s report root <<'EOF'\npython3 failed\nEOF\n": "scripts/s.sh:2: mail is given an input that names python",
            "printf 'python3 x.py\\n' | mail -s report root\n": "scripts/s.sh:2: mail is given an input that names python",
            "\"X=python3 -c 'if True'\"\n": f"scripts/s.sh:2: X=python3 -c 'if True' {says}",   # a command, no assignment
            "X\\=python3 x\n": f"scripts/s.sh:2: X=python3 {says}",
            "pip() { command pip \"$@\"; }\npip install python-dateutil\n": f"scripts/s.sh:2: pip {says} (python-dateutil)",
        })
        d = self.tree()
        (d / "scripts" / "lib.sh").write_text("#!/bin/bash\ndie() { echo \"$*\" >&2; exit 1; }\n")
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\n. ./lib.sh\ncommand -v python3 > /dev/null || die \"python3 is required\"\n"
            "say() { echo \"$*\"; }\nsay \"python3 -c failed\"\nlogger -t x \"python3 -c failed\"\n"
            "echo \"python3 -c failed\" >&2\nprintf 'python3 %s\\n' x\ngrep -q python3 notes.txt\n"
            "for cmd in \"python3 -m pytest\" \"python3 -m mypy\"; do\n  echo \"would run: $cmd\"\ndone\n"
            "select tool in \"python3 -m pip list\" quit; do\n  echo \"$tool\"; break\ndone\n"
            "CHECKS=(\"python3 -m json.tool config.json\" \"python3 tools/x.py\")\nprintf '%s\\n' \"${CHECKS[@]}\"\n"
            "[[ -x /usr/bin/python3 ]] && echo yes\n(( n = 1 ))\ncase $x in python3) echo y ;; esac\n"
            "want='grep -c python[1] out'\nINC=\"-I$dir/python${V}\"\ntypeset T=\"python3 -m x\"\n"
            "chmod +x .venv/bin/python\nxrandr --display \"$DPY\"\ncp x \"$TUTORCOPY\"\n")
        (d / "scripts" / "u.sh").write_text("#!/bin/bash\nsource scripts/lib.sh\ndie \"python3 is required\"\n")
        (d / "scripts" / "lib2.sh").write_text("#!/bin/bash\n[ -n \"${LIB2:-}\" ] && return 0\nLIB2=1\n. ./lib2.sh\n")
        self.passes(d)
        (d / "scripts" / "lib3").write_text("x=1  # \ue000\n")       # sourced, so read: U+E000 is the step's own
        (d / "scripts" / "v.sh").write_text("#!/bin/bash\n. ./lib3\n")
        self.fails(d, "cannot read: scripts/v.sh:2: scripts/lib3, which . reads, holds the character U+E000")

    def test_programs_that_run_a_command_line_are_given_no_expansion(self):
        # ssh, tmux, at ... run a word or their standard input as a command line: an expansion among their words or in
        # the input the script gives them, or after a shell's name among another program's words, is not known (also a
        # variable that the script sets once); a function's program that may run its input reads the call's
        says = "runs what it is given as a command line, and is given"
        stdin = "runs its standard input as code, and is given"
        self.scripts_fail({
            "in_tmux() { tmux new-window -d -n job \"$1\"; }\nin_tmux \"python3 -c 'if True'\"\n":    # (round 9: $1 is
                "scripts/s.sh:2: tmux is given a word that names python (python3 -c 'if True')",       # read at the call)
            "in_tmux() { tmux new-window -d -n job \"$1\"; }\nin_tmux \"$CMD\"\n": f"scripts/s.sh:3: tmux {says} $CMD",
            "CMD=\"python3 -c 'if True'\"\ntmux new-window -d \"$CMD\"\n":           # (round 10: set to python: python's)
                "scripts/s.sh:3: tmux is given a word that names python ($CMD)",
            "CMD=\"make -k\"\ntmux new-window -d \"$CMD\"\n": f"scripts/s.sh:3: tmux {says} $CMD",
            "ssh \"$HOST\" uptime\n": f"scripts/s.sh:2: ssh {says} $HOST",
            "HOST=gpu1\nssh \"$HOST\" uptime\n": f"scripts/s.sh:3: ssh {says} $HOST",
            "ssh gpu1 \"$(cat cmd.txt)\"\n": f"scripts/s.sh:2: ssh {says} $(...)",
            "docker run --rm img bash -c \"$CMD\"\n": f"scripts/s.sh:2: bash (after docker) {says} $CMD",
            "ssh gpu1 <<EOF\n$PY -c 'print(1)'\nEOF\n": "scripts/s.sh:2: ssh is given an input that names python",
            "ssh gpu1 <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {stdin} an input with an expansion in it",
            "ssh gpu1 <<< \"$CMD\"\n": f"scripts/s.sh:2: ssh {stdin} an input with an expansion in it",
            "echo \"$CMD\" | ssh gpu1\n": f"scripts/s.sh:2: ssh {stdin} an input with an expansion in it",
            "echo \"$CMD\" | cat | ssh gpu1\n": f"scripts/s.sh:2: ssh {stdin} an input with an expansion in it",
            "SSH_OPTS=\"-o BatchMode=yes\"\nssh $SSH_OPTS \"$HOST\" python3 - < tools/x.py\n":
                "scripts/s.sh:3: ssh is given a word that names python (python3)",
            "opts=(-o BatchMode=yes -p 2222)\nssh \"${opts[@]}\" \"$HOST\" python3 - < tools/x.py\n":
                "scripts/s.sh:3: ssh is given a word that names python (python3)",
            "HOST=gpu1\nremote_py() { ssh \"$HOST\" python3 -; }\nremote_py <<'EOF'\nprint(1)\nEOF\n":
                "scripts/s.sh:3: ssh is given a word that names python (python3)",
            "on_host() { ssh gpu1; }\non_host <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: ssh is given an input that names python",
            "on_host() { ssh -T gpu1 < /dev/stdin; }\non_host <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: ssh is given an input that names python",
            "py() { python3 - < /dev/stdin; }\npy <<'EOF'\nif True\nEOF\n": "scripts/s.sh:4 (heredoc)",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nssh gpu1 uptime\nssh -N -L 9000:localhost:80 gpu1\nssh -O exit gpu1\nssh gpu1 < /dev/null\n"
            "ssh -N gpu1 <<'EOF'\n( not shell code\nEOF\nssh -t gpu1 'tmux new -A -s main'\non_host() { ssh gpu1; }\n"
            "on_host\non_host <<'EOF'\nuptime\nEOF\ntmux new-window -d 'make test'\necho \"$X\" | mail -s report root\n"
            "docker exec ctr sh -c 'make test'\n")
        self.passes(d)

    def test_at_and_batch_need_a_job_this_check_sees(self):
        # at and batch run their job (their standard input, or -f FILE) in a shell later: one this check does not see,
        # or that names python, fails; in a function, the job is the call's input, read at the call
        self.scripts_fail({
            "later() { at now + 1 minute; }\nlater <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: at is given an input that names python",
            "later() { at now; }\nouter() { later; }\nouter <<'EOF'\npython3 -c 'if True'\nEOF\n":  # (round 9: the
                "scripts/s.sh:2: at is given an input that names python",      # outer call's input, read at its call)
            "later() { at now; }\nouter() { later; }\nouter\n":
                "scripts/s.sh:2: at reads its job from its standard input, which this check does not see here",
            "at now\n": "scripts/s.sh:2: at reads its job from its standard input, which this check does not see here",
            "batch < job.txt\n": "scripts/s.sh:2: batch reads its job from its standard input, which this check does not",
            "echo -e \"python3 -c 'print(1)'\" | at now\n": "scripts/s.sh:2: at reads its job from its standard input",
            "printf '%s\\n' \"python3 -c 'if True'\" > job.txt\nat -f job.txt now\n":
                "scripts/s.sh:3: at reads its job from job.txt, which this check does not see",
            "cat > job.sh <<'EOF'\npython3 -c 'if True'\nEOF\nat -fjob.sh now\n":
                "scripts/s.sh:5: at is given a job that names python (job.sh)",
            "at now <<EOF\n$CMD\nEOF\n": "scripts/s.sh:2: at runs what it is given as a command line, and is given an input",
        })
        d = self.tree()
        (d / "tools" / "job.txt").write_text("make test\n")
        (d / "tools" / "py_job").write_text("python3 -c 'print(1)'\n")
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nin_batch() { batch; }\nin_batch <<'EOF'\necho hi\nEOF\nat now + 5 minutes <<EOF\necho hi\nEOF\n"
            "at -l -q a\nat -lq a\nat -d 3\nat -r 4\nat -c 5\nat -V\natrm 3\necho 'make test' | at now\n"
            "at -f tools/job.txt now\nat now -f tools/job.txt\n"
            "as_docker() { sg docker; }\nas_docker <<'EOF'\necho hi\nEOF\n"
            "later() { at now; }\nouter() { later; }\nouter <<'EOF'\necho hi\nEOF\n")
        self.passes(d)
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nat -f tools/py_job now\n")
        self.fails(d, "scripts/s.sh:2: at is given a job that names python (tools/py_job)")

    def test_helpers_skipped_modules_and_tests_without_a_name(self):
        # a function named test... outside a class is a helper only when a function's body reads it (not the module's
        # top level, as under if __name__ == "__main__", a comprehension there, a local variable of that name, an
        # assignment, nor its own body); a module skipped at its import has none of its code after the skip checked,
        # and one that turns the profiler off at its import none of its functions outside a class; a FunctionTestCase
        # of functools.partial that fails prints its failure
        h = "import unittest\n"
        cases = {
            GOOD_TEST + "def test_b():\n    assert False\nif __name__ == '__main__':\n    test_b()\n    unittest.main()\n":
                "tests/test_f.py:5: test_b is not in a TestCase, so it never runs",
            h + "def test_paths():\n    assert False\nclass T(unittest.TestCase):\n    def test_a(self):\n"
            "        for test_paths in (['a'],):\n            self.assertTrue(test_paths)\n":
                "tests/test_f.py:2: test_paths is not in a TestCase, so it never runs",
            GOOD_TEST + "def test_r(n=1):\n    if n:\n        test_r(n - 1)\n":
                "tests/test_f.py:5: test_r is not in a TestCase, so it never runs",
            GOOD_TEST + "def test_s():\n    again = lambda: test_s()\n    assert False\n":
                "tests/test_f.py:5: test_s is not in a TestCase, so it never runs",
            "def test_one():\n    assert False\n" + GOOD_TEST:       # on line 1, where the module's own code is
                "tests/test_f.py:1: test_one is not in a TestCase, so it never runs",
            GOOD_TEST + "def test_c():\n    assert False\nCASES = [test_c for _ in ()]\n":
                "tests/test_f.py:5: test_c is not in a TestCase, so it never runs",
            GOOD_TEST + "def test_g():\n    assert False\ndef reset():\n    global test_g\n    test_g = None\n":
                "tests/test_f.py:5: test_g is not in a TestCase, so it never runs",
        }
        for text, says in cases.items():
            with self.subTest(says=says):
                d = self.tree()
                (d / "tests" / "test_f.py").write_text(text)
                self.fails(d, says)
        d = self.tree()
        files = {
            # helpers that a function reads: also under try at the top level, or in a lambda
            "tests/test_f.py": h + "try:\n    def test_helper():\n        return 1\nexcept ImportError:\n    pass\n"
                "def test_data():\n    return 2\ncheck = lambda: test_data()\n"
                "class T(unittest.TestCase):\n    @unittest.skip('harness: not asked for')\n    def test_a(self):\n"
                "        self.assertEqual(test_helper(), 1)\n        self.assertEqual(check(), 2)\n",
            # modules skipped at their import, with tests and code after the skip
            "tests/test_m.py": h + "raise unittest.SkipTest('machine: needs this machine')\n"
                "class T(unittest.TestCase):\n    def test_x(self):\n        self.fail()\n",
            "tests/test_n.py": h + "raise unittest.SkipTest('harness: not asked for')\ndef testcases():\n    return 1\n"
                "N = testcases()\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail()\n",
            # a module that turns the profiler off at its import, and the next one, whose import is seen again
            "tests/test_o.py": "import sys, unittest\nsys.setprofile(None)\ndef testcases():\n    return 1\n"
                "N = testcases()\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(N, 1)\n",
            "tests/test_p.py": h + "def testdata():\n    return 1\nN = testdata()\n"
                "class T(unittest.TestCase):\n    def test_y(self):\n        self.assertEqual(N, 1)\n",
        }
        for path, text in files.items():
            (d / path).write_text(text)
        self.passes(d, "--repo-only")
        self.fails(d, "was skipped: machine: needs this machine")
        d = self.tree()                     # the module after one that turned the profiler off is checked again
        (d / "tests" / "test_o.py").write_text(files["tests/test_o.py"])
        (d / "tests" / "test_p.py").write_text(GOOD_TEST + "def test_never():\n    assert False\n")
        self.fails(d, "tests/test_p.py:5: test_never is not in a TestCase, so it never runs")
        d = self.tree()
        (d / "tests" / "test_p.py").write_text(
            "import functools, unittest\ndef check(n):\n    assert n < 1, 'n is %d' % n\ndef boom(n):\n"
            "    raise RuntimeError(f'boom {n}')\ndef load_tests(loader, tests, pattern):\n    for n in range(2):\n"
            "        tests.addTest(unittest.FunctionTestCase(functools.partial(check, n)))\n"
            "    tests.addTest(unittest.FunctionTestCase(functools.partial(boom, 7)))\n    return tests\n")
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        for says in ("FAIL: <unittest.case.FunctionTestCase tec=functools.partial(", "AssertionError: n is 1",
                     "ERROR: <unittest.case.FunctionTestCase tec=functools.partial(", "RuntimeError: boom 7"):
            self.assertIn(says, r.stdout)
        self.assertNotIn("has no attribute '__name__'", r.stdout + r.stderr)

    def test_verify_failing_beside_a_distribution_units_copy(self):
        # when the copy of a distribution's unit alone fails verify, verify's failure passes only if it printed nothing
        # else (systemd-analyze stands in here: a finding of the copy alone, and another line)
        if not Path("/lib/systemd/system/systemd-tmpfiles-clean.service").is_file():
            self.skipTest("machine: no systemd-tmpfiles-clean.service in /lib/systemd/system")
        d = self.tree()
        (d / "systemd" / "systemd-tmpfiles-clean.service.d").mkdir()
        (d / "systemd" / "systemd-tmpfiles-clean.service.d" / "50-x.conf").write_text("[Service]\nNice=5\n")
        stub = ("#!/bin/sh\nfor a in \"$@\"; do\n  case $a in */systemd-tmpfiles-clean.service) echo \"$a:2: a finding\" ;; esac\n"
                "done\ncase \"$*\" in\n  */dist/*) ;;\n  *) %s ;;\nesac\nexit 1\n")
        self.program(d, "systemd-analyze", stub % ":")
        self.passes(d)
        self.program(d, "systemd-analyze", stub % "echo 'something else'")
        self.fails(d, "systemd-analyze verify exited with 1:")

    def test_unit_lines_that_systemd_joins_with_the_next(self):
        # systemd joins a line that ends with a backslash that no backslash escapes with the next line (not a comment,
        # whose first character but blanks is # or ;, nor one with blanks after its backslash; a form feed before # is
        # no blank); the units step reads each line alone, so such a line fails, in a unit and in a drop-in
        says = "it ends with a backslash, so systemd joins it with the next line"
        service = "\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
        for path, text, line in (
                ("x.service", "[Unit]\nDescription=a \\\n  b\n" + service, 2),
                ("x.service", "[Unit]\nDescription=x\n" + service + "Environment=A=1 \\\n  B=2\n", 7),
                ("x.service", "[Unit]\nDescription=x\nDocumentation=man:true(1) \\\n  man:false(1)\n" + service, 3),
                ("x.service", "[Unit] \\\nDescription=x\n" + service, 1),
                ("x.service", "[Unit]\nDescription=x\n\x0c# a form feed first \\\nAfter=network.target\n" + service, 3),
                ("x.service", ("[Unit]\nDescription=a \\\nAfter=network.target\n" + service).replace("\n", "\r\n"), 2),
                ("x.service", "[Unit]\nDescription=a \\\\\\\n  b\n" + service, 2),                    # three: one is left
                ("x.service.d/50-x.conf", "[Service]\nEnvironment=A=1 \\\nB=2\n", 2)):
            with self.subTest(path=path, text=text):
                d = self.tree()
                (d / "systemd" / path).parent.mkdir(exist_ok=True)
                (d / "systemd" / path).write_text(text)
                self.fails(d, f"systemd/{path}:{line}: {says}")
        d = self.tree()                     # what systemd does not join
        self.unit(d, "x.service", "Description=foo \\\\\nDescription=bar \\ \n# a comment \\\n; another \\\n"
                                  "  # an indented one \\\n" + service)
        (d / "systemd" / "x.service.d").mkdir()
        (d / "systemd" / "x.service.d" / "50-x.conf").write_text("# a comment \\\n[Service]\nNice=5\n")
        self.passes(d)

    def test_forms_round_7_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the eighth review's mutation testing left alive
        # (or from one of round 8's changes: L1, watch's code read again at each call is counted)
        self.scripts_fail({
            "K=python3\ntypeset -n J=K\n\"$J\" -c 'if True'\n": "$J -c: the program is in a variable",            # B18
            "K=python3\ndeclare -rn J=K\n\"$J\" -c 'if True'\n": "$J -c: the program is in a variable",           # B17
            "time -p ! ( python3 -c 'if True' )\n": "scripts/s.sh:2 (python3 -c)",                                 # B15
            "f() { watch \"$@\" \": " + "x" * 8000 + "\"; }\n" + "f true\n" * 300:                                   # L1
                "more calls of the script's functions (with their commands and words) than this check follows",
        })
        d = self.tree()                     # watch's code read once is not counted (as sh -c code read once)
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nwatch -n 1 ': " + "x" * 1_700_000 + "'\n")
        self.passes(d)
        d = self.tree()                     # B04, B12: two FunctionTestCases of functools.partial, one skipped
        (d / "tests" / "test_good.py").unlink()
        part = ("import functools, unittest\ndef check(n):\n    assert n < 3\ndef skipped(n):\n"
                "    raise unittest.SkipTest(%r)\ndef load_tests(loader, tests, pattern):\n"
                "    tests.addTest(unittest.FunctionTestCase(functools.partial(check, 1)))\n"
                "    tests.addTest(unittest.FunctionTestCase(functools.partial(skipped, 2)))\n    return tests\n")
        (d / "tests" / "test_p.py").write_text(part % "harness: not asked for")
        self.passes(d)
        (d / "tests" / "test_p.py").write_text(part % "flaky")                                                     # B11
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"FAIL: <unittest.case.FunctionTestCase tec=functools.partial\(<function skipped at "
                                   r"0x[0-9a-f]+>, 2\)> was skipped: flaky")
        d = self.tree()                     # B05: a test that two modules load counts for the first one
        (d / "tests" / "test_a.py").write_text(
            GOOD_TEST + "TESTS = [T('test_ok')]\ndef load_tests(loader, tests, pattern):\n    return unittest.TestSuite(TESTS)\n")
        (d / "tests" / "test_b.py").write_text(
            "import unittest\nfrom test_a import TESTS\ndef load_tests(loader, tests, pattern):\n"
            "    return unittest.TestSuite(TESTS)\n")
        self.fails(d, "tests/test_b.py has no test")
        d = self.tree()                     # B07: a decorator whose function holds itself (a cycle), and the test
        (d / "tests" / "test_r.py").write_text(
            "import unittest\ndef deco(f):\n    def w(self, f=f, me=None):\n        self.skipTest('harness: not asked for')\n"
            "        return f(self)\n    w.__defaults__ = (f, w)\n    return w\n"
            "class T(unittest.TestCase):\n    @deco\n    def test_a(self):\n        self.fail()\n")
        self.passes(d)

    # round 9: the forms the ninth review found read wrongly; each test but the last fails with the scripts/test.sh
    # before round 9 (the last one holds the cases that kill the mutants the ninth review found alive)
    def test_variables_and_texts_that_are_python_behind_programs_it_does_not_follow(self):
        # a variable named for python (PYBIN, PYEXE), set once to python, or a literal array that holds python, given to
        # a program the step does not follow as a word, in its input or in a file the script wrote that it may run; and
        # python in a command substitution in such a word
        says = "is given a word that names python"
        self.scripts_fail({
            "PYBIN=/usr/bin/python3\nstrace -f \"$PYBIN\" -c 'if True'\n": f"scripts/s.sh:3: strace {says} ($PYBIN)",
            "PYTHONBIN=python3\nnumactl --cpunodebind=0 \"$PYTHONBIN\" -c 'if True'\n": f"scripts/s.sh:3: numactl {says}",
            "PYEXE=/usr/bin/python3.10\nperf stat -- \"$PYEXE\" - <<'EOF'\nif True\nEOF\n": f"scripts/s.sh:3: perf {says}",
            "VENVPY=.venv/bin/python3\ngdb --args \"$VENVPY\" -c 'if True'\n": f"scripts/s.sh:3: gdb {says} ($VENVPY)",
            "chroot /srv/root \"$PYBIN\" -c 'if True'\n": f"scripts/s.sh:2: chroot {says} ($PYBIN)",     # set nowhere
            "PY=(python3 -I)\nperf stat \"${PY[@]}\" -c 'if True'\n": f"scripts/s.sh:3: perf {says} (${{PY[@]}})",
            "a=(python3 -I)\nperf stat \"${a[@]}\" -c 'if True'\n": f"scripts/s.sh:3: perf {says} (${{a[@]}})",
            "INTERP='python3'\nperf stat \"$INTERP\" -c 'if True'\n": f"scripts/s.sh:3: perf {says} ($INTERP)",
            "strace -f \"$(command -v python3)\" -c 'if True'\n": f"scripts/s.sh:2: strace {says} ($(...))",
            "docker run --rm img \"$(command -v python3)\"\n": f"scripts/s.sh:2: docker {says} ($(...))",
            "docker run --rm img `which python3` -c 'if True'\n": f"scripts/s.sh:2: docker {says} (`...`)",
            "tmux new-window -d 'exec \"$PY\" -c \"if True\"'\n": f"scripts/s.sh:2: tmux {says}",
            "ssh gpu1 'cd /srv && \"$PY\" -c \"if True\"'\n": f"scripts/s.sh:2: ssh {says}",
            "ssh gpu1 <<'EOF'\n\"$PY\" -c 'if True'\nEOF\n": "scripts/s.sh:2: ssh is given an input that names python",
            "docker exec -i ctr bash <<'EOF'\n\"$PYBIN\" -c 'if True'\nEOF\n":
                "scripts/s.sh:2: docker is given an input that names python",
            "at now + 1 minute <<'EOF'\n\"$PY\" -c 'if True'\nEOF\n": "scripts/s.sh:2: at is given an input that names python",
            "cat > job.sh <<'EOF'\n\"$PY\" -c 'if True'\nEOF\nchroot /srv/root bash job.sh\n":
                "scripts/s.sh:5: chroot is given a file the script wrote that names python (job.sh)",
            "cat > job.sh <<EOF\n$PY -c 'if True'\nEOF\nssh gpu1 bash -s < job.sh\n":
                "scripts/s.sh:5: ssh is given an input that names python",
            "cat > job.sh <<EOF\n$PY -c x\nEOF\nscp job.sh host:\n":
                "scripts/s.sh:5: scp is given a file the script wrote that names python (job.sh)",
            "cat > j.sh <<'EOF'\nmake\nEOF\ncat >> j.sh <<EOF\n$PY x\nEOF\nscp j.sh host:\n":
                "scripts/s.sh:8: scp is given a file the script wrote that names python (j.sh)",
            "cat > job.sh <<EOF\n$PY -c x\nEOF\nat -fjob.sh now\n": "scripts/s.sh:5: at is given a job that names python",
            "docker run --rm img pypy3 -c 'x'\n": f"scripts/s.sh:2: docker {says} (pypy3)",
        })
        d = self.tree()                     # Python's own variables, and names that hold py but are not python's
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\ndocker run --rm -e PYTHONPATH=\"$PYTHONPATH\" img x\ndocker run --rm img \"$PYTHONPATH\" x\n"
            "cp x \"$COPY\"\nxrandr --display \"$DPY\"\n")
        self.passes(d)

    def test_inputs_given_to_programs_that_run_them_as_code(self):
        # ssh with no command or with a shell's, and a shell among another program's words that reads its standard
        # input, run what the script gives them there: an input that the step does not see, or that holds an expansion,
        # fails; a call in another function's body passes the outer call's input on
        stdin = "runs its standard input as code, and is given"
        unseen = "an input that this check does not see"
        self.scripts_fail({
            "./make_job.sh | ssh gpu1\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "./make_job.sh | ssh gpu1 bash -s\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "./make_job.sh | docker exec -i ctr bash\n": f"scripts/s.sh:2: bash (after docker) {stdin} {unseen}",
            "ssh gpu1 < \"$JOB\"\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "ssh gpu1 < <(./make_job.sh)\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "docker exec -i ctr bash < <(./make_job.sh)\n": f"scripts/s.sh:2: bash (after docker) {stdin} {unseen}",
            "docker exec -i ctr bash <<EOF\n$X\nEOF\n": f"scripts/s.sh:2: bash (after docker) {stdin} an input with an",
            "echo \"$CMD\" | docker exec -i ctr bash\n": f"scripts/s.sh:2: bash (after docker) {stdin} an input with an",
            "docker exec -i ctr sh <<< \"$CMD\"\n": f"scripts/s.sh:2: sh (after docker) {stdin} an input with an",
            "cat <<EOF | chroot /srv/root /bin/sh\n$CMD\nEOF\n": f"scripts/s.sh:2: /bin/sh (after chroot) {stdin} an input",
            "nsenter -t 1 -m -- bash <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: bash (after nsenter) {stdin} an input with",
            "remote() { ssh gpu1; }\nouter() { remote; }\nouter <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: ssh is given an input that names python",
            "remote() { pdsh -w gpu1; }\nouter() { remote; }\nouter <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: pdsh is given an input that names python",
            "remote() { ssh gpu1 bash -s; }\nouter() { remote; }\nouter <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: ssh is given an input that names python",
            "py() { python3 -; }\nouter() { py; }\nouter <<'EOF'\nif True\nEOF\n": "scripts/s.sh:5 (heredoc)",
            "./make_job.sh | pdsh -w gpu1\n": f"scripts/s.sh:2: pdsh {stdin} {unseen}",
            "ssh -o \"$OPTS\" gpu1 uptime\n": "scripts/s.sh:2: ssh runs what it is given as a command line, and is given $OPTS",
        })
        d = self.tree()                     # a file of the repository as the input: read
        (d / "tools" / "job").write_text("python3 -c 'print(1)'\n")
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nssh gpu1 < tools/job\n")
        self.fails(d, f"scripts/s.sh:2: ssh {stdin} an input that names python")
        d = self.tree()                     # what passes: an input that the program on the other host does not run
        (d / "tools" / "job").write_text("make test\n")     # (tar), that this check sees, or none
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\ntar -cf - . | ssh gpu1 'tar -xf -'\n./make_job.sh | ssh -n gpu1 uptime\nssh gpu1 < /dev/null\n"
            "ssh gpu1 bash -s < tools/job\nssh gpu1 bash -s <<'EOF'\nmake test\nEOF\nssh -N -L 9000:localhost:80 gpu1\n"
            "ssh -i \"$KEY\" -p \"$PORT\" gpu1 uptime\nssh gpu1\ndocker exec -i ctr bash < tools/job\n"
            "remote() { ssh gpu1; }\nouter() { remote; }\nouter <<'EOF'\nuptime\nEOF\nouter\n"
            "./make_job.sh | ssh gpu1 cat > out\n./make_job.sh | ssh -N -L 9000:localhost:80 gpu1\n"
            "./make_list.sh | chsh -s /bin/bash\n")
        self.passes(d)

    def test_data_words_that_the_strict_rules_once_refused(self):
        # array elements with an expansion in their subscript are assignments; a shell or a program of CMDLINE named as
        # data (an option's value, a file, a shell that runs a file) is no command line; a sourced file's functions are
        # known before its source line, and to the script after sh -c code sourced it; a file the script wrote, given
        # to rm, cp or mv
        d = self.tree()
        (d / "scripts" / "lib.sh").write_text("#!/bin/bash\ndie() { echo \"$*\" >&2; exit 1; }\n")
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\ni=0\ncmds[$i]=\"python3 tools/x.py\"\ndeclare -A m\nm[\"$k\"]=python3\n"
            "cmds[${#cmds[@]}]=\"python3 -m pytest\"\nx[$((i+1))]=python3\n"
            "rsync -a -e ssh \"$src/\" \"$dst\"\nuseradd -m -s /bin/bash \"$NAME\"\njournalctl -u ssh --since \"$T\" -n 20\n"
            "cp -r results/batch \"$out\"\ncp results/batch \"$out\"\nusermod -s /bin/sh \"$u\"\n"
            "ln -sf /bin/bash \"$ROOT/bin/sh\"\n"
            "strace -f -o trace.txt bash \"$script\"\nchsh -s /bin/zsh \"$USER\"\n"
            "OPTS=$(getopt -s bash -o \"$SHORTOPTS\" -l \"$LONGOPTS\" -n \"$0\" -- \"$@\") || exit 2\n"
            "main() { command -v python3 > /dev/null || die \"python3 is required\"; }\n. ./lib.sh\nmain\n"
            "cat > x.py <<'EOF'\n#!/usr/bin/env python3\nprint(1)\nEOF\npython3 x.py\nrm -f x.py\n"
            "cat > job.txt <<'EOF'\npython3 x.py\nEOF\nmv job.txt queue/\ncp job.txt /tmp/\n")
        (d / "scripts" / "u.sh").write_text(
            "#!/bin/bash\nbash -c '. ./lib.sh; die \"python3 is required\"'\n. ./lib.sh\ndie \"python3 is required\"\n")
        self.passes(d)
        self.scripts_fail({                 # what is left, as the header says: a command-line program's name as data
            "systemctl restart ssh \"$UNIT\"\n": "scripts/s.sh:2: ssh (after systemctl) runs what it is given as a command",
            "mv parallel \"$d\"\n": "scripts/s.sh:2: parallel (after mv) runs what it is given as a command line",
            "cat > job.txt <<'EOF'\npython3 x.py\nEOF\nscp ./job.txt host:\n":
                "scripts/s.sh:5: scp is given a file the script wrote that names python (./job.txt)",
            "strace -f bash <(printf 'x\\n')\n": "scripts/s.sh:2: bash (after strace) runs what it is given as a command",
        })

    def test_helpers_are_read_by_code_that_may_run(self):
        # a free test function is a helper only when code that ran, a loaded test's function (also a skipped one) or a
        # function that a loaded test holds reads it, or a function that those read; a decorator's generator expression
        # or lambda is no def that ran; a module that is dark by an import still reports a function named nowhere
        cases = {
            GOOD_TEST + "def test_b():\n    assert False\ndef main():\n    test_b()\n    unittest.main()\n"
            "if __name__ == '__main__':\n    main()\n": "tests/test_f.py:5: test_b is not in a TestCase, so it never runs",
            GOOD_TEST + "def test_x():\n    assert False\n    test_y()\ndef test_y():\n    assert False\n    test_x()\n":
                "tests/test_f.py:5: test_x is not in a TestCase, so it never runs",
            GOOD_TEST + "class Mixin:\n    @unittest.skipUnless(all(x for x in (1,)), 'machine: needs it')\n"
            "    def test_m(self):\n        self.fail()\n": "tests/test_f.py:7: Mixin.test_m never runs",
            "import functools\n" + GOOD_TEST + "@functools.lru_cache(maxsize=(lambda: 4)())\ndef test_c():\n"
            "    assert False\n": "tests/test_f.py:7: test_c is not in a TestCase, so it never runs",
        }
        for text, says in cases.items():
            with self.subTest(says=says):
                d = self.tree()
                (d / "tests" / "test_f.py").write_text(text)
                self.fails(d, says)
        d = self.tree()                     # dark by importing a module that turns the profiler off
        (d / "tests" / "test_o.py").write_text("import sys\nsys.setprofile(None)\nimport unittest\n"
                                               "class U(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        (d / "tests" / "test_a.py").write_text("import test_o\n" + GOOD_TEST + "def test_never():\n    assert False\n")
        self.fails(d, "tests/test_a.py:6: test_never is not in a TestCase, so it never runs")
        d = self.tree()                     # held as a skipped test's default (and what it reads), a helper that a
        (d / "tests" / "test_f.py").write_text(   # helper in a block reads, one that a function that ran reads, and one
            "import functools, sys, unittest\ndef test_helper():\n    return test_deep()\ndef test_deep():\n    return 1\n"
            "def test_inner():\n    return 2\nif True:\n    def helper2():\n        return test_inner()\n"
            "def test_listed():\n    return 3\ndef cases():\n    return [test_listed]\nCASES = cases()\n"
            "def test_fn_helper():\n    return 4\ndef test_fn():\n    assert test_fn_helper() == 4\n"
            "def skip_it():\n    raise unittest.SkipTest('harness: not asked for')\n"
            "def load_tests(loader, tests, pattern):\n    fn = getattr(sys.modules[__name__], 'test_' + 'fn')\n"
            "    tests.addTest(unittest.FunctionTestCase(functools.partial(fn), setUp=skip_it))\n    return tests\n"
            "class T(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(len(CASES), 1)\n"
            "    @unittest.skip('harness: not asked for')\n    def test_a(self, f=test_helper):\n"
            "        self.assertEqual(f() + helper2(), 3)\n")                       # that a FunctionTestCase's reads
        self.passes(d)

    def test_more_ways_python_runs_that_once_passed(self):
        # pypy and ipython are python; "$@" -c at a script's top runs the script's arguments; xargs -I puts its input in
        # python's code; watch at a function's second call; su -c in a function that is never called; a function's $1
        # given to a program the step does not follow, or as the program
        self.scripts_fail({
            "pypy3 -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "ipython -c 'if True'\n": "scripts/s.sh:2 (python3 -c)",
            "\"$@\" -c 'if True'\n": "scripts/s.sh:2: $@ -c: the program is the script's arguments",
            "xargs -I{} python3 -c '{}' < snippets.txt\n": "scripts/s.sh:2: xargs puts what it reads ({}) into the code",
            "xargs -I @ sh -c 'echo @' < snippets.txt\n": "scripts/s.sh:2: xargs puts what it reads (@) into the code",
            "f() { sg docker -c \"python3 -c 'if True'\"; }\n": "scripts/s.sh:2 (sg -c):1 (python3 -c)",
            "f() { watch -n 1 echo \"$@\"; }\nf hello\nf \"; python3 -c 'if True'\"\n": "scripts/s.sh:2 (watch):1 (python3 -c)",
            "f() { su -c \"python3 -c 'if True'\" nobody; }\n": "scripts/s.sh:2 (su -c):1 (python3 -c)",
            "py_in() { docker exec -i ctr \"$1\" -c \"$2\"; }\npy_in python3 'if True'\n":
                "scripts/s.sh:2: docker is given a word that names python (python3)",
            "f() { docker exec -i ctr \"$1\" \"${@:2}\"; }\nf python3 -c 'if True'\n": "scripts/s.sh:2: docker is given a",
            "run() { \"$1\" -c \"$2\"; }\nrun python3 'if True'\n": "scripts/s.sh:3 (python3 -c)",
        })
        d = self.tree()                     # what passes: xargs -I into another program's words, su without -c in a
        (d / "scripts" / "s.sh").write_text(  # function, and $1 after a shift this check does not count (a limit)
            "#!/bin/bash\nxargs -I{} cp {} /tmp/ < list\nf() { su nobody; }\nf <<'EOF'\necho hi\nEOF\n"
            "g() { while [ $# -gt 0 ]; do notify-send \"$1\"; shift; done; }\ng python3\n")
        self.passes(d)

    def test_a_sourced_file_that_cannot_be_read(self):
        # it is named, not a traceback
        if os.geteuid() == 0:
            self.skipTest("machine: root reads a file of mode 0")
        d = self.tree()
        (d / "scripts" / "lib.sh").write_text("#!/bin/bash\ndie() { exit 1; }\n")
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\n. ./lib.sh\ndie x\n")
        (d / "scripts" / "lib.sh").chmod(0)
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertIn("cannot read: scripts/s.sh:2: scripts/lib.sh cannot be read: Permission denied", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_forms_round_8_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the ninth review's mutation testing left alive
        h = "import unittest\n"
        d = self.tree()                     # N02: a local of a test that its nested function reads is no helper
        (d / "tests" / "test_m.py").write_text(
            h + "def test_p():\n    assert False\nclass T(unittest.TestCase):\n    def test_a(self):\n        test_p = 1\n"
            "        def inner():\n            return test_p\n        self.assertEqual(inner(), 1)\n")
        self.fails(d, "tests/test_m.py:2: test_p is not in a TestCase, so it never runs")
        d = self.tree()                     # N04: a comprehension in a skipped test reads a helper
        (d / "tests" / "test_m.py").write_text(
            h + "def test_helper():\n    return 1\nclass T(unittest.TestCase):\n    @unittest.skip('harness: not asked for')\n"
            "    def test_a(self):\n        self.assertEqual([test_helper() for _ in (1,)], [1])\n")
        self.passes(d)
        d = self.tree()                     # N07, N62: a module that sets its own profile function at its import is dark;
        (d / "tests" / "test_m.py").write_text(   # a test method of a dark module that never runs fails all the same
            "import sys\nsys.setprofile(lambda *a: None)\nimport unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\ndef testcases():\n    return 1\nN = testcases()\n")
        self.passes(d)
        (d / "tests" / "test_n.py").write_text(
            "import sys\nsys.setprofile(None)\nimport unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
            "        pass\nclass Mixin:\n    def test_m(self):\n        self.fail()\n")
        self.fails(d, "tests/test_n.py:8: Mixin.test_m never runs")
        stdin = "runs its standard input as code, and is given an input with an expansion in it"
        self.scripts_fail({
            "f() { pdsh -w gpu1; }\nf <<'EOF'\npython3 -c 'if True'\nEOF\n":                                     # N30
                "scripts/s.sh:2: pdsh is given an input that names python",
            "printf '%s\\n' \"$CMD\" | ssh gpu1\n": f"scripts/s.sh:2: ssh {stdin}",                                   # N43
            "echo \"run $CMD\" | ssh gpu1\n": f"scripts/s.sh:2: ssh {stdin}",                                        # N44
            "mosh gpu1 -- \"$CMD\"\n": "scripts/s.sh:2: mosh runs what it is given as a command line, and is given $CMD",  # N45
            "docker run --rm img /bin/bash -c \"$CMD\"\n": "scripts/s.sh:2: /bin/bash (after docker) runs what it is",    # N46
            "'X=python3' x\n": "scripts/s.sh:2: X=python3 is given a word that names python",                         # N15
            "$'X=python3' x\n": "scripts/s.sh:2: X=python3 is given a word that names python",                        # N14
            "\"SUDO=sudo\"\nbash -c \"$SUDO make\"\n": "scripts/s.sh:3 (bash -c):1: a command that is only an expansion",  # N16
            "on_host() { /usr/bin/ssh gpu1; }\non_host <<'EOF'\npython3 -c 'if True'\nEOF\n":                     # N60
                "scripts/s.sh:2: ssh is given an input that names python",
            "py() { python3 - < /dev/fd/0; }\npy <<'EOF'\nif True\nEOF\n": "scripts/s.sh:4 (heredoc)",                # N32
            "cat > job.txt <<'EOF'\npython3 x.py\nEOF\nscp ./job.txt host:\n":                                     # N41
                "scripts/s.sh:5: scp is given a file the script wrote that names python (./job.txt)",
            "at -f /etc/hostname now\n": "scripts/s.sh:2: at reads its job from /etc/hostname, which this check does not",  # N49
        })
        d = self.tree()                     # N31, N20, N21, N67, N74: forms that pass
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nf() { su nobody; }\nf <<'EOF'\necho hi\nEOF\n"
            "g() { [ $# -gt 0 ] || return 0; shift; X=1 g \"$@\"; }\ng a \"python3 -c x\"\n"
            "h() { if [ $# -gt 1 ]; then h \"${@:2}\"; fi; }\nh a \"python3 -c x\"\n"
            "cat > job.sh <<'EOF'\nmake test\nEOF\nat -f ./job.sh now\ndocker run --rm img \"$PYTHONPATH\" x\n")
        self.passes(d)
        d = self.tree()                     # N22: a directory beside the repository, named as it begins, is not its own
        (d.parent / "repo2").mkdir()
        (d.parent / "repo2" / "lib.sh").write_text("die() { exit 1; }\n")
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\n. ../repo2/lib.sh\ndie \"python3 is required\"\n")
        self.fails(d, "scripts/s.sh:3: die is given a word that names python")
        d = self.tree()                     # N55: a comment line indented with a tab goes on on no next line
        self.unit(d, "x.service", "\t# a comment \\\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
        self.passes(d)

    def test_ssh_given_an_input_with_an_expansion_or_one_it_runs(self):
        # ssh with a command, unless it reads no input (-n), refuses an input with an expansion again, also when the
        # command only stores it (a limit); ssh's command reads its input as code also through sudo -s or -i, doas -s,
        # a pipe from cat or tee, xargs into a shell, and csh, tcsh and fish (whose code this check does not read)
        stdin = "runs its standard input as code, and is given"
        exp = "an input with an expansion in it"
        unseen = "an input that this check does not see"
        cmdline = "runs what it is given as a command line, and is given an input with an expansion in it"
        self.scripts_fail({
            "ssh gpu1 sudo -s <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "ssh gpu1 \"sudo -i\" <<< \"$CMD\"\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "echo \"$CMD\" | ssh gpu1 sudo -i\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "ssh gpu1 \"sudo -iu app\" <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "ssh gpu1 doas -s <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "run_remote() {\n  ssh gpu1 \"$@\"\n}\nrun_remote sudo -i <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:3: ssh {stdin} {exp}",
            "ssh gpu1 tcsh <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "ssh gpu1 'tee /tmp/j.sh | bash' <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {stdin} {exp}",
            "ssh gpu1 'cat > /tmp/run.sh && bash /tmp/run.sh' <<EOF\ncd /srv\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh {cmdline}",
            "ssh gpu1 \"sudo tee /etc/cron.d/job\" <<EOF\n* * * * * root $CMD\nEOF\n": f"scripts/s.sh:2: ssh {cmdline}",
            "ssh gpu1 'cat > /tmp/run.sh' <<EOF\n$CMD\nEOF\nssh gpu1 bash /tmp/run.sh\n": f"scripts/s.sh:2: ssh {cmdline}",
            "./gen.sh | ssh gpu1 sudo -s\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "./gen.sh | ssh gpu1 \"sudo -u app -s\"\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "ssh gpu1 'cat | bash' < <(./gen.sh)\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "./gen.sh | ssh gpu1 xargs -0 bash -c\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "f() { ssh gpu1 \"$@\"; }\nf \"cat | bash\" < <(./gen.sh)\n": f"scripts/s.sh:2: ssh {stdin} {unseen}",
            "tcsh -c \"$CMD\"\n": "scripts/s.sh:2: tcsh runs what it is given as a command line, and is given $CMD",
            "fish -c \"$CMD\"\n": "scripts/s.sh:2: fish runs what it is given as a command line, and is given $CMD",
            "./gen.sh | csh\n": f"scripts/s.sh:2: csh {stdin} {unseen}",
            "fish <<'EOF'\necho python3\nEOF\n": "scripts/s.sh:2: fish is given an input that names python",
            "doas -s <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:3 (sh heredoc):1 (python3 -c)",
            "remote() { ssh gpu1; }\nmid() { remote; }\nouter() { mid; }\nouter <<'EOF'\npython3 -c 'if True'\nEOF\n":
                "scripts/s.sh:2: ssh is given an input that names python",
            "f() { sudo -s; }\nf <<'EOF'\npython3 -c 'if True'\nEOF\n": "scripts/s.sh:4 (sh heredoc):1 (python3 -c)",
        })
        d = self.tree()                     # what passes: no input read (-n), or one that ssh's command does not run
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nssh -n gpu1 uptime <<EOF\n$CMD\nEOF\nssh gpu1 sudo bash -s <<'EOF'\nmake\nEOF\n"
            "echo uptime | ssh gpu1 'cat | bash'\n./gen.sh | ssh gpu1 'cat > out'\nssh gpu1 sudo -s < /dev/null\n"
            "./gen.sh | ssh gpu1 sudo -i make install\n./gen.sh | ssh gpu1 'echo ls | bash'\n"
            "./gen.sh | ssh gpu1 'cat < /etc/motd | bash'\n")
        self.passes(d)

    def test_programs_after_an_option_of_another_program(self):
        # a program of CMDLINE or a shell among another program's words counts after any option, but an option that
        # names a program as data (rsync -e, chsh -s, journalctl -u ...); ssh there has its own rules; a shell as
        # docker's --entrypoint runs code when a later word is an option with c, and else may read its input
        stdin = "runs its standard input as code, and is given"
        unseen = "an input that this check does not see"
        self.scripts_fail({
            "sshpass -e ssh gpu1 \"$CMD\"\n": "scripts/s.sh:2: ssh (after sshpass) runs what it is given as a command",
            "strace -f ssh gpu1 \"$CMD\"\n": "scripts/s.sh:2: ssh (after strace) runs what it is given as a command",
            "torsocks -i ssh gpu1 \"$CMD\"\n": "scripts/s.sh:2: ssh (after torsocks) runs what it is given as a command",
            "xvfb-run -a xterm -e \"$CMD\"\n": "scripts/s.sh:2: xterm (after xvfb-run) runs what it is given as a command",
            "docker run --rm --entrypoint bash img -c \"$CMD\"\n":
                "scripts/s.sh:2: bash (after docker) runs what it is given as a command line, and is given $CMD",
            "docker run --entrypoint=bash img -c \"$CMD\"\n":
                "scripts/s.sh:2: --entrypoint=bash (after docker) runs what it is given as a command line",
            "nsenter -t 1 -m bash <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: bash (after nsenter) {stdin} an input with an",
            "unshare -Urm bash <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: bash (after unshare) {stdin} an input with an",
            "docker run --rm -i --entrypoint /bin/sh img <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: /bin/sh (after docker) {stdin}",
            "./gen.sh | docker run -i --entrypoint bash img\n": f"scripts/s.sh:2: bash (after docker) {stdin} {unseen}",
            "./gen.sh | sshpass -e ssh gpu1\n": f"scripts/s.sh:2: ssh (after sshpass) {stdin} {unseen}",
            "sshpass -e ssh gpu1 <<EOF\n$CMD\nEOF\n": f"scripts/s.sh:2: ssh (after sshpass) {stdin} an input with an",
            "./gen.sh | docker exec -i ctr bash -\n": f"scripts/s.sh:2: bash (after docker) {stdin} {unseen}",
            "gzip -c at \"$x\"\n": "scripts/s.sh:2: at (after gzip) runs what it is given as a command line, and is given $x",
            "docker run img ssh gpu1 'cat > f' <<EOF\n$X\nEOF\n":
                "scripts/s.sh:2: ssh (after docker) runs what it is given as a command line, and is given an input with",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nrsync -a -e ssh \"$src/\" \"$dst\"\nrsync --rsh ssh \"$a\" \"$b\"\nuseradd -s /bin/bash \"$N\"\n"
            "usermod --shell /bin/sh \"$u\"\n./make_list.sh | chsh -s /bin/bash\ngetopt -s bash -o \"$O\" -- \"$@\"\n"
            "journalctl -u ssh --since \"$T\"\ndocker run --rm --entrypoint bash img script.sh\n"
            "sshpass -e ssh -i \"$KEY\" gpu1 uptime\n./gen.sh | docker run -i --entrypoint bash img -c 'make'\n")
        self.passes(d)

    def test_variables_set_to_python_in_common_forms(self):
        # a variable that any assignment sets to a word that names python, also through a command substitution that
        # runs another program, a path or several words, and an array built with += or local -a, is python's; a
        # variable set to python's output is not
        says = "is given a word that names python"
        self.scripts_fail({
            "INTERP=$(command -v python3)\nnumactl --cpunodebind=0 \"$INTERP\" -c 'if True'\n":
                f"scripts/s.sh:3: numactl {says} ($INTERP)",
            "VENV=.venv\nRUNNER=\"$VENV/bin/python3\"\nperf stat \"$RUNNER\" -c 'if True'\n": f"scripts/s.sh:4: perf {says}",
            "RUNNER=${VENV:-.venv}/bin/python3\nperf stat \"$RUNNER\" -c 'if True'\n": f"scripts/s.sh:3: perf {says}",
            "INTERP=\"python3 -u\"\nperf stat $INTERP -c 'if True'\n": f"scripts/s.sh:3: perf {says} ($INTERP)",
            "INTERP=`which python3`\nstrace -f \"$INTERP\" -c 'if True'\n": f"scripts/s.sh:3: strace {says} ($INTERP)",
            "main() {\n  local interp=python3\n  strace -f \"$interp\" -c 'if True'\n}\nmain\n": f"scripts/s.sh:4: strace {says}",
            "RUN=()\nRUN+=(python3 -u)\nperf stat \"${RUN[@]}\" -c 'if True'\n": f"scripts/s.sh:4: perf {says} (${{RUN[@]}})",
            "main() {\n  local -a run=(python3 -u)\n  perf stat \"${run[@]}\" -c 'if True'\n}\nmain\n":
                f"scripts/s.sh:4: perf {says} (${{run[@]}})",
            "RUN=(\"$VENV/bin/python3\" -u)\nperf stat \"${RUN[@]}\" -c 'if True'\n": f"scripts/s.sh:3: perf {says}",
            "RUN=(make)\nRUN+=(\"$X\")\n\"${RUN[@]}\" -c x\n": "scripts/s.sh:4: ${RUN[@]} -c: the program is in a variable",
        })
        d = self.tree()                     # python's output, and += of literal words, which run as python reads them
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nPORT=$(python3 -c 'print(8080)')\ncurl \"http://127.0.0.1:$PORT/health\"\n"
            "LOG=$(python3 -c 'print(\"x.log\")')\nstat -c %s \"$LOG\"\nRUN=(python3)\nRUN+=(-u)\n\"${RUN[@]}\" -c 'print(1)'\n")
        self.passes(d)
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nRUN=(python3)\nRUN+=(-u)\n\"${RUN[@]}\" -c 'if True'\n")
        self.fails(d, "scripts/s.sh:4 (python3 -c)")
        d = self.tree()
        (d / "scripts" / "lib.sh").write_text("#!/bin/bash\nINTERP=$(command -v python3)\n")
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\n. ./lib.sh\nstrace -f \"$INTERP\" -c 'if True'\n")
        self.fails(d, "scripts/s.sh:3: strace is given a word that names python ($INTERP)")
        self.scripts_fail({"PY=python3\nbash -c \"$PY -c 'print(1)'\"\n": "scripts/s.sh:3 (bash -c):1: a command that is only"})

    def test_a_written_file_whose_first_line_names_pypy(self):
        d = self.tree()
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\ncat > job <<'EOF'\n#!/usr/bin/env pypy3\nif True\nEOF\n"
                                            "chmod +x job\n./job\n")
        self.fails(d, "syntax error: scripts/s.sh:3 (a file this heredoc writes)")

    def test_lookups_defaults_of_parameters_and_a_file_overwritten_by_another(self):
        # hash only looks a name up (killall python3 fails: it is no lookup); ${1:-} is $1 at a call (its default when the call has no $1); a file the
        # script wrote and that the code of another file it wrote overwrites from words it cannot read
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nneed() { hash \"$1\" 2>/dev/null || { echo \"need $1\" >&2; exit 1; }; }\nneed python3\n"
            "cat > a.sh <<'EOF'\n#!/bin/bash\necho \"$1\" > b.txt\nEOF\necho hi > b.txt\n")
        self.passes(d)
        self.scripts_fail({
            "run() { docker exec -i ctr \"${1:-}\" -c 'if True'; }\nrun python3\n":
                "scripts/s.sh:2: docker is given a word that names python (python3)",
            "run() { strace -f \"${1:-python3}\" -c 'if True'; }\nrun\n":
                "scripts/s.sh:2: strace is given a word that names python (${1:-python3})",
            "cat > j.sh <<'EOF'\nmake\nEOF\nscp j.sh host:\ncat >> j.sh <<'EOF'\npython3 x\nEOF\nscp j.sh host:\n":
                "scripts/s.sh:9: scp is given a file the script wrote that names python (j.sh)",
            "cat > j.sh <<'EOF'\nmake\nEOF\nscp j.sh host:\ncat > j.sh <<'EOF'\npython3 x\nEOF\nscp j.sh host:\n":
                "scripts/s.sh:9: scp is given a file the script wrote that names python (j.sh)",
        })

    def test_a_helper_that_a_skipped_tests_method_reads_through_self(self):
        d = self.tree()
        (d / "tests" / "test_m.py").write_text(
            "import unittest\ndef test_helper():\n    return 1\nclass Base(unittest.TestCase):\n    def check(self):\n"
            "        return test_helper()\nclass T(Base):\n    def test_ok(self):\n        self.assertTrue(True)\n"
            "    @unittest.skip('harness: not asked for')\n    def test_a(self):\n        self.assertEqual(self.check(), 1)\n")
        self.passes(d)
        (d / "tests" / "test_m.py").write_text(       # a method that nothing reads does not make one
            "import unittest\ndef test_b():\n    assert False\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
            "        self.assertTrue(True)\n    def helper(self):\n        return test_b()\n")
        self.fails(d, "tests/test_m.py:2: test_b is not in a TestCase, so it never runs")

    def test_reading_again_at_calls_stays_linear(self):
        # a body's ssh command with a long word read at 1000 calls, 20000 appends to a file that such a body gives ssh,
        # and 2000 functions each calling the next, defined in reverse: each finishes in a few seconds
        cases = {
            "f() { ssh gpu1 '" + "/a" * 50000 + "' \"$@\"; }\n" + "f x\n" * 1000,
            "".join(f"echo 'line {i}' >> job.txt\n" for i in range(20000)) +
            "f() { ssh gpu1 \"$@\" < job.txt; }\n" + "f\n" * 1000,
            "".join(f"f{i}() {{ f{i - 1}; }}\n" for i in range(2000, 0, -1)) + "f0() { ssh gpu1; }\n",
        }
        for body in cases:
            with self.subTest(body=body[:40]):
                d = self.tree()
                (d / "scripts" / "s.sh").write_text("#!/bin/bash\n" + body)
                r = self.run_sh(d, timeout=60)
                self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (0, ["all checks passed"]), r.stdout[-2000:])
        d = self.tree()                     # each new text of ssh's commands counts
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nf() { ssh gpu1 '" + "/a" * 50000 + "' \"$@\"; }\n" + "".join(f"f x{i}\n" for i in range(2000)))
        self.fails(d, "scripts/s.sh:2: more characters of ssh's commands (each 80) read than this check follows")

    def test_forms_round_9_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the tenth review's mutation testing left alive
        h = "import unittest\n"
        d = self.tree()                     # A01: a lambda that ran reads a helper
        (d / "tests" / "test_m.py").write_text(
            h + "def test_helper():\n    return 1\nHELPERS = (lambda: [test_helper])()\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertEqual(len(HELPERS), 1)\n")
        self.passes(d)
        d = self.tree()                     # A02: a lambda that a test holds as a default reads a helper
        (d / "tests" / "test_m.py").write_text(
            h + "def test_b():\n    assert False\nclass T(unittest.TestCase):\n    def test_ok(self, f=lambda: test_b()):\n"
            "        self.assertTrue(True)\n")
        self.passes(d)
        d = self.tree()                     # A05: a name that a test only declares global and assigns is not read
        (d / "tests" / "test_m.py").write_text(
            h + "def test_b():\n    assert False\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
            "        global test_b\n        test_b = None\n        self.assertTrue(True)\n")
        self.fails(d, "tests/test_m.py:2: test_b is not in a TestCase, so it never runs")
        d = self.tree()                     # A06: a dark module that only stores a name does not name it
        (d / "tests" / "test_o.py").write_text("import sys\nsys.setprofile(None)\nimport unittest\n"
                                               "class U(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        (d / "tests" / "test_a.py").write_text(
            "import test_o\n" + GOOD_TEST + "def test_never():\n    assert False\nfor test_never in ():\n    pass\n")
        self.fails(d, "tests/test_a.py:6: test_never is not in a TestCase, so it never runs")
        stdin = "runs its standard input as code, and is given an input that this check does not see"
        self.scripts_fail({
            "strace -f \"$py_cmd\" -c 'if True'\n": "scripts/s.sh:2: strace is given a word that names python ($py_cmd)",  # B03
            "./gen.sh | ssh gpu1 cd /srv '&&' bash -s\n": f"scripts/s.sh:2: ssh {stdin}",                                # B21
            "./gen.sh | clush -w gpu1\n": f"scripts/s.sh:2: clush {stdin}",                                               # B26
            "./gen.sh | mosh gpu1\n": f"scripts/s.sh:2: mosh {stdin}",                                                    # B27
            "cat > job.txt <<'EOF'\npython3 x.py\nEOF\nrsync job.txt host:\n":                                         # B28
                "scripts/s.sh:5: rsync is given a file the script wrote that names python (job.txt)",
            "xargs -iZ python3 -c 'Z' < snippets.txt\n": "scripts/s.sh:2: xargs puts what it reads (Z) into the code",     # B29
            "\"${@:2}\" -c 'if True'\n": "scripts/s.sh:2: ${@:2} -c: the program is the script's arguments",              # B31
            "perf stat true <<< \"$PYBIN -c x\"\n": "scripts/s.sh:2: perf is given an input that names python",             # B46
            "docker run img /usr/bin/ssh gpu1 \"$X\"\n": "scripts/s.sh:2: /usr/bin/ssh (after docker) runs what it is",    # B56
            "docker run img ipython -c x\n": "scripts/s.sh:2: docker is given a word that names python (ipython)",           # B62
            "./gen.sh | ssh -- gpu1\n": f"scripts/s.sh:2: ssh {stdin}",                                                   # B65
            "ssh -F \"$CFG\" gpu1 uptime\n": "scripts/s.sh:2: ssh runs what it is given as a command line, and is given $CFG",  # B68
        })
        d = self.tree()                     # B41: a guarded source is read where it stands (a limit)
        (d / "scripts" / "lib.sh").write_text("#!/bin/bash\ndie() { echo \"$*\" >&2; exit 1; }\n")
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nmain() { die \"python3 is required\"; }\n[ -f scripts/lib.sh ] && . ./lib.sh\nmain\n")
        self.fails(d, "scripts/s.sh:2: die is given a word that names python")
        d = self.tree()                     # B17, B19, B20, B30, B33, B50: forms that pass
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\n./gen.sh | ssh -p22 gpu1 uptime\n./gen.sh | ssh -f gpu1 bash -s\nssh -J \"$JUMP\" gpu1 uptime\n"
            "xargs -I{} python3 -c 'print(1)' < list.txt\n./gen.sh | ssh gpu1 sg docker make\npython2 -c 'x = 1'\n")
        self.passes(d)

    def test_a_shell_as_dockers_entrypoint_or_with_code_and_the_words_after_it(self):
        # an expansion counts after a shell that runs code from that code on, and after docker's --entrypoint shell only
        # after its later option with c; a shell that reads its input takes the words after it as its arguments
        line = "runs what it is given as a command line, and is given"
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\ndocker run --rm --entrypoint /bin/bash \"$IMG\" /w/build.sh\n"
            "docker run --rm --entrypoint sh \"$IMG\" -c \"make all\"\ndocker run --rm --entrypoint bash img /w/build.sh \"$ARG\"\n"
            "docker run --rm -i --entrypoint=bash \"$IMG\" < /dev/null\n"
            "docker run --rm --entrypoint /bin/bash -v \"$PWD:/w\" img /w/build.sh\n"
            "docker run --rm --entrypoint bash -e \"A=$B\" img -c \"make\"\n"
            "docker exec -i ctr bash -s -- \"$arg\" < /dev/null\ndocker exec ctr bash --rcfile \"$RC\" -c make\n")
        self.passes(d)
        self.scripts_fail({
            "docker run --rm --entrypoint=/bin/sh img -c \"$CMD\"\n": f"scripts/s.sh:2: --entrypoint=/bin/sh (after docker) {line} $CMD",
            "docker run --rm --entrypoint bash \"$IMG\" -c \"$CMD\"\n": f"scripts/s.sh:2: bash (after docker) {line} $CMD",
            "docker exec ctr bash -c make \"$X\"\n": f"scripts/s.sh:2: bash (after docker) {line} $X",
            "strace -f ssh \"$HOST\" uptime\n": f"scripts/s.sh:2: ssh (after strace) {line} $HOST",
            "./gen.sh | docker run -i --entrypoint bash img --rcfile /etc/rc\n":     # (a long option has no -c)
                "scripts/s.sh:2: bash (after docker) runs its standard input as code, and is given an input that",
        })

    def test_variables_set_to_pythons_output(self):
        # a command substitution that names python only as the program of its commands (after wrappers, in a pipeline
        # or a list) gives python's output, no program; one that names it otherwise does not
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nout=$(cat x.json)\nverdict=$(printf \"%s\" \"$out\" | python3 -I -c \"print(1)\")\n"
            "mail -s report root <<< \"$verdict\"\n"
            "total0=$(curl -fsS \"$URL/metrics\" | python3 -c \"import sys; print(1)\" 2>/dev/null)\n"
            "curl -fsS \"$URL/x?n=$total0\"\nVER=$(timeout 5 python3 -c \"print(1)\")\ncurl -d \"v=$VER\" \"$URL\"\n"
            "N=$(cd /tmp && python3 -c \"print(1)\")\ncurl \"$URL/$N\"\nP=$(PYTHONHASHSEED=0 python3 -c \"print(1)\")\n"
            "curl \"$URL:$P\"\nQ=$(\"python3\" -c \"print(1)\")\ncurl \"$URL:$Q\"\n"
            "V=$(env PYTHONHASHSEED=0 python3 -c \"print(1)\")\ncurl \"$URL/$V\"\nas_user=(runuser -u strata --)\n"
            "key=$(\"${as_user[@]}\" python3 -I - \"$CFG\" <<'EOF' 2>/dev/null\nprint(1)\nEOF\n)\n"
            "curl -H \"Authorization: Bearer $key\" \"$URL\"\n")
        self.passes(d)
        self.scripts_fail({
            "W=$(echo python3)\ncurl \"$URL/$W\"\n": "scripts/s.sh:3: curl is given a word that names python ($URL/$W)",
            "X=$(command -v python3)\ncurl \"$URL/$X\"\n": "scripts/s.sh:3: curl is given a word that names python ($URL/$X)",
        })

    def test_a_program_given_as_data_after_a_group_of_short_options(self):
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nrsync -avze ssh \"$SRC\" \"$HOST:$DST\"\njournalctl -xeu ssh --since \"$SINCE\"\n"
            "useradd -ms /usr/bin/fish \"$U\"\nuseradd -ms /bin/bash \"$U\"\n")
        self.passes(d)
        self.scripts_fail({          # only the group's last letter takes the next word, and a long option is no group (as the
            "rsync -avz ssh \"$SRC\"\n": "scripts/s.sh:2: ssh (after rsync) runs what it is given as a command line, and is given $SRC",
            "rsync -ev ssh \"$SRC\"\n": "scripts/s.sh:2: ssh (after rsync) runs what it is given as a command line",  # header
            "rsync --delete ssh \"$SRC\"\n": "scripts/s.sh:2: ssh (after rsync) runs what it is given as a command line",  # says)
        })

    def test_arrays_of_wrappers_and_shells_run_nothing(self):
        d = self.tree()
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\nu=strata\nas_user=(runuser -u \"$u\" --)\n"
            "key=$(\"${as_user[@]}\" python3 -I - \"$CFG\" <<'EOF' 2>/dev/null\nprint(1)\nEOF\n)\n"
            "printf \"%s\\n\" \"$key\" > hdr\nsh_cmd=(bash)\n[ -n \"${DEBUG:-}\" ] && sh_cmd=(bash -x)\n"
            "\"${sh_cmd[@]}\" scripts/ok.sh\nas_root=(sudo -i)\nas_app=(su app)\n")
        self.passes(d)
        self.scripts_fail({"RUN=(python3 -c 'if True')\n": "scripts/s.sh:2 (python3 -c)"})    # python's code still is

    def test_a_python_variable_exported_to_a_new_shell(self):
        says = "is given a word that names python ($INTERP)"
        self.scripts_fail({
            "INTERP=$(command -v python3)\nexport INTERP\nbash -c 'perf stat \"$INTERP\" -c \"if True\"'\n":
                f"scripts/s.sh:4 (bash -c):1: perf {says}",
            "INTERP=$(command -v python3)\nexport INTERP\nsudo -E bash -c 'numactl --cpunodebind=0 \"$INTERP\" -c \"if True\"'\n":
                f"scripts/s.sh:4 (bash -c):1: numactl {says}",
            "INTERP=$(command -v python3)\nexport INTERP\nbash <<'EOF'\nperf stat \"$INTERP\" -c 'if True'\nEOF\n":
                f"scripts/s.sh:5 (bash heredoc):1: perf {says}",
        })

    def test_helpers_through_methods_of_related_classes_and_more_forms(self):
        # a method read as self.NAME is one of the reader's class or a related one; Base.check(self) is Base's; ${X:=}
        # and ${1:?} set and give python; a variable's own name is no mention of python; arrays expanded again and again
        # are counted
        d = self.tree()
        (d / "tests" / "test_m.py").write_text(
            "import unittest\ndef test_forgot():\n    assert False\nclass Unused:\n    def check(self):\n"
            "        return test_forgot()\nclass T(unittest.TestCase):\n    def check(self):\n        return 1\n"
            "    def test_ok(self):\n        self.assertEqual(self.check(), 1)\n")
        self.fails(d, "tests/test_m.py:2: test_forgot is not in a TestCase, so it never runs")
        (d / "tests" / "test_m.py").write_text(
            "import unittest\ndef test_helper():\n    return 1\nclass Base(unittest.TestCase):\n    def check(self):\n"
            "        return test_helper()\nclass T(Base):\n    def test_ok(self):\n        self.assertTrue(True)\n"
            "    @unittest.skip('harness: not asked for')\n    def test_a(self):\n        self.assertEqual(Base.check(self), 1)\n")
        self.passes(d)
        (d / "tests" / "test_m.py").write_text(       # a base's self.hook() may be a derived class's method
            "import unittest\ndef test_helper():\n    return 1\nclass Base(unittest.TestCase):\n    def check(self):\n"
            "        return self.hook()\nclass T(Base):\n    def hook(self):\n        return test_helper()\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n    @unittest.skip('harness: not asked for')\n"
            "    def test_a(self):\n        self.assertEqual(self.check(), 1)\n")
        self.passes(d)
        self.scripts_fail({
            ": \"${INTERP:=python3}\"\nperf stat \"$INTERP\" -c \"if True\"\n":
                "scripts/s.sh:3: perf is given a word that names python ($INTERP)",
            "run() { docker exec -i ctr \"${1:?}\" -c \"if True\"; }\nrun python3\n":
                "scripts/s.sh:2: docker is given a word that names python (python3)",
            "a=()\n" + "a+=(w)\n" * 200 + "\"${a[@]}\" y\n" * 200:
                "more words of arrays put in place of their expansions than this check follows",
        })
        d = self.tree()
        (d / "scripts" / "s.sh").write_text("#!/bin/bash\nipython_dir=/tmp/x\ncurl -d \"$ipython_dir\" http://localhost:9/x\n")
        self.passes(d)

    def test_forms_round_10_mutation_testing_found_untested(self):
        # each of these tells scripts/test.sh from a mutant of it that the eleventh review's mutation testing left alive
        d = self.tree()                     # R01: cls.NAME; R02: super().NAME
        for read in ("cls = type(self)\n        self.assertEqual(cls.check(self), 1)", "self.assertEqual(super().check(), 1)"):
            (d / "tests" / "test_m.py").write_text(
                "import unittest\ndef test_helper():\n    return 1\nclass Base(unittest.TestCase):\n    def check(self):\n"
                "        return test_helper()\nclass T(Base):\n    def test_ok(self):\n        self.assertTrue(True)\n"
                "    @unittest.skip('harness: not asked for')\n    def test_a(self):\n        " + read + "\n")
            with self.subTest(read=read):
                self.passes(d)
        d = self.tree()                     # R03: a function outside a class is no method
        (d / "tests" / "test_m.py").write_text(
            "import unittest\ndef test_b():\n    assert False\ndef helper():\n    return test_b()\n"
            "class T(unittest.TestCase):\n    def test_ok(self):\n        self.helper = 1\n        self.assertTrue(self.helper)\n")
        self.fails(d, "tests/test_m.py:2: test_b is not in a TestCase, so it never runs")
        stdin = "runs its standard input as code, and is given an input that this check does not see"
        self.scripts_fail({
            "docker run --entrypoint=ssh img gpu1 \"$CMD\"\n": "scripts/s.sh:2: --entrypoint=ssh (after docker) runs what",  # M11
            "cat > job.txt <<'EOF'\npython3 x.py\nEOF\ndocker run img bin/fish job.txt\n":                                 # M15
                "scripts/s.sh:5: docker is given a file the script wrote that names python (job.txt)",
            "./gen.sh | ssh gpu1 \"bash -c '\"\n": f"scripts/s.sh:2: ssh {stdin}",                                         # M22
            "./gen.sh | ssh gpu1 \"cat <<X\"\n": f"scripts/s.sh:2: ssh {stdin}",
            "./gen.sh | ssh gpu1 doas -u app -s\n": f"scripts/s.sh:2: ssh {stdin}",                                        # M36
            "A=1 INTERP=python3\nperf stat \"$INTERP\" -c \"if True\"\n": "scripts/s.sh:3: perf is given a word that names python",  # M38
        })
        d = self.tree()                     # M33, M34, M41, M42: forms that pass
        (d / "scripts" / "s.sh").write_text(
            "#!/bin/bash\n./gen.sh | ssh gpu1 doas -i\n./gen.sh | ssh gpu1 doas --shell\n"
            "PORT=$(PYTHONHASHSEED=0 python3 -c \"print(1)\")\ncurl \"$URL:$PORT\"\nP2=$(\"python3\" -c \"print(1)\")\n"
            "curl \"$URL:$P2\"\n")
        self.passes(d)

    def test_usage(self):
        r = self.run_sh(self.tree(), "--repo")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_jobs(self):
        # --jobs N, N from 1 to 16 as the next argument; the run then uses N workers
        d = self.tree()
        for args in (["--jobs"], ["--jobs", "0"], ["--jobs", "17"], ["--jobs", "02"], ["--jobs", "x"], ["--jobs=2"],
                     ["--jobs", "-1"], ["--jobs", "2", "3"], ["--jobs", "--repo-only"]):
            with self.subTest(args=args):
                r = self.run_sh(d, *args)
                self.assertEqual((r.returncode, r.stdout), (2, ""), r.stderr)
                self.assertIn("usage: scripts/test.sh", r.stderr)
        for jobs in ("1", "16"):
            with self.subTest(jobs=jobs):
                self.passes(d, "--jobs", jobs, "--repo-only")

    def test_workers_run_each_test_once(self):
        # each loaded test runs once, in one of the workers, after its module's and its class's fixtures, which a
        # worker sets up for the tests it takes; with --jobs 1 one worker runs them all
        d = self.tree()
        log = d.parent / "log"
        text = ("import os, time, unittest\n"
                f"def note(what):\n    with open({str(log)!r}, 'a') as f:\n        f.write(f'{{what}} {{os.getpid()}}\\n')\n"
                "def setUpModule():\n    note('module.' + __name__)\n"
                "class A(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        note('class.' + cls.__module__)\n")
        text += "".join(f"    def test_{i}(self):\n        time.sleep(0.2)\n        note(self.id())\n" for i in range(6))
        for m in "pqr":
            (d / "tests" / f"test_{m}.py").write_text(text)
        expected = sorted(f"test_{m}.A.test_{i}" for m in "pqr" for i in range(6))
        for jobs in ("1", "3"):
            with self.subTest(jobs=jobs):
                log.unlink(missing_ok=True)
                r = self.run_sh(d, "--jobs", jobs)
                self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (0, ["all checks passed"]), r.stdout + r.stderr)
                self.assertIn("\nRan 19 tests in ", r.stdout)
                lines = [line.split() for line in log.read_text().splitlines()]
                self.assertEqual(sorted(what for what, _ in lines if what.startswith("test_")), expected)
                workers = {pid for what, pid in lines}
                if jobs == "1":
                    self.assertEqual(len(workers), 1)
                    self.assertEqual(sorted(what for what, _ in lines if not what.startswith("test_")),
                                     ["class.test_p", "class.test_q", "class.test_r",
                                      "module.test_p", "module.test_q", "module.test_r"])
                else:
                    self.assertGreater(len(workers), 1, lines)
                    self.assertLessEqual(len(workers), 3, lines)
                seen = set()                # a worker's tests of a module come after its setUpModule and setUpClass
                for what, pid in lines:
                    if what.startswith("test_"):
                        self.assertLessEqual({("module", what.split(".")[0], pid), ("class", what.split(".")[0], pid)},
                                             seen, what)
                    else:
                        seen.add((*what.split("."), pid))

    def test_what_the_tests_leave_in_their_tmpdir(self):
        # the tests get a TMPDIR of their own, which fails the run when something is left in it, also by a test that
        # passed; what a test removes again passes
        d = self.tree()
        text = ("import os, shutil, tempfile, unittest\nclass T(unittest.TestCase):\n    def test_t(self):\n"
                "        d = tempfile.mkdtemp(prefix='left-')\n        self.assertNotEqual(tempfile.gettempdir(), '/tmp')\n")
        (d / "tests" / "test_tmp.py").write_text(text + "        shutil.rmtree(d)\n")
        self.passes(d)
        (d / "tests" / "test_tmp.py").write_text(text)
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\nOK\n(.*\n)*FAIL: the tests left this in their TMPDIR: left-\w+ \n")
        # a NamedTemporaryFile that a test keeps in an attribute of its own is removed as the worker lets go of the
        # test, as unittest's suite does once the test has run, and collects it (the test refers to itself).  Before,
        # the worker never finalized it
        (d / "tests" / "test_tmp.py").write_text(
            "import tempfile, unittest\nclass T(unittest.TestCase):\n    def setUp(self):\n"
            "        self.f = tempfile.NamedTemporaryFile(prefix='ntf-')\n        self.me = self\n    def test_t(self):\n"
            "        self.f.write(b'x')\n    def test_u(self):\n        self.f.write(b'y')\n")
        self.passes(d)
        # a test that removes the TMPDIR itself: the check cannot read it, and the run fails
        (d / "tests" / "test_tmp.py").write_text(
            "import shutil, tempfile, unittest\nclass T(unittest.TestCase):\n    def test_t(self):\n"
            "        shutil.rmtree(tempfile.gettempdir())\n")
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\nOK\n(.*\n)*FAIL: cannot read the tests' TMPDIR\n")

    def test_what_a_test_holds_once_the_tests_have_run(self):
        # a test that makes another one hold a function as it runs (as the default of an argument) makes that function
        # a helper: what a test holds is read after the tests have run, in the worker that ran it
        d = self.tree()
        (d / "tests" / "test_v.py").write_text(
            "import unittest\ndef test_free():\n    pass\nclass T(unittest.TestCase):\n    def test_a(self):\n"
            "        T.test_b.__defaults__ = (globals()['test_' + 'free'],)\n    def test_b(self, h=None):\n"
            "        pass\n")
        self.passes(d)

    def test_a_unit_that_no_worker_runs(self):
        # a setUpClass that takes the next unit from the queue and then skips its class: that unit never starts, and
        # the skip of its class excuses it, so only the count of the units that the workers took fails the run
        d = self.tree()
        (d / "tests" / "test_a.py").write_text(
            "import os, unittest\nclass A(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
            "        for fd in os.listdir('/proc/self/fd'):\n            try:\n"
            "                if os.readlink(f'/proc/self/fd/{fd}').endswith('verdict.units'):\n"
            "                    os.read(int(fd), 4)\n            except OSError:\n                pass\n"
            "        raise unittest.SkipTest('machine: not here')\n"
            "    def test_0(self):\n        pass\n    def test_1(self):\n        self.fail('never runs')\n")
        r = self.run_sh(d, "--repo-only")
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertEqual([line for line in r.stdout.splitlines() if line.startswith("FAIL:")],
                         ["FAIL: the workers did not run each unit once: 2 runs of 2 of 3 units"])

    def test_workers_take_the_units_in_turn_with_the_test_hook(self):
        # TEST_JOBS_DEAL=1: worker k takes the units k, k + N, k + 2 N ... (TestShJobs uses it)
        d = self.tree()
        log = d.parent / "log"
        (d / "tests" / "test_good.py").write_text("import os, unittest\nclass T(unittest.TestCase):\n" + "".join(
            f"    def test_{i}(self):\n        with open({str(log)!r}, 'a') as f:\n"
            f"            f.write(f'{i} {{os.getpid()}}\\n')\n" for i in range(7)))
        self.passes(d, "--jobs", "3", env={"TEST_JOBS_DEAL": "1"})
        pid = dict(line.split() for line in log.read_text().splitlines())
        self.assertEqual(len(pid), 7)
        self.assertEqual(len(set(pid.values())), 3)
        for i in range(7):
            self.assertEqual(pid[str(i)], pid[str(i % 3)], (i, pid))

    def test_tests_that_turn_the_profiler_off_in_every_worker(self):
        # what ran of such a test is not known, in whichever worker it ran: each of three workers runs some of these
        d = self.tree()
        (d / "tests" / "test_off.py").write_text("import sys, time, unittest\nclass T(unittest.TestCase):\n" + "".join(
            f"    def test_{i}(self):\n        time.sleep(0.2)\n        sys.setprofile(None)\n" for i in range(9)))
        for jobs in ("1", "3"):
            with self.subTest(jobs=jobs):
                self.passes(d, "--jobs", jobs)

    def test_a_signal_to_the_runner_ends_its_workers(self):
        # the workers run in scripts/test.sh's process group, the runner meanwhile in one of its own: a signal to the
        # runner alone goes on to them, once, and the end of the runner (SIGKILL) gives each a SIGKILL; either way the
        # run leaves no verdict.  The runner then ends as the workers did (of the signal), and scripts/test.sh, which
        # did not get the signal, goes on, as it did when the tests ran in the runner
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGKILL):
            with self.subTest(signal=sig.name):
                d = self.tree()
                pids = d.parent / "pids"
                (d / "tests" / "test_long.py").write_text(
                    "import os, time, unittest\nclass L(unittest.TestCase):\n" + "".join(
                        f"    def test_{i}(self):\n        with open({str(pids)!r}, 'a') as f:\n"
                        "            f.write(f'{os.getpid()} {os.getppid()}\\n')\n        time.sleep(60)\n"
                        for i in range(2)))
                e = {k: v for k, v in os.environ.items() if k not in ("STRATA_TEST_HARNESSES", "TEST_REPO_ONLY")}
                e["PATH"] = f"{d.parent / 'bin'}:{e['PATH']}"
                p = subprocess.Popen(["bash", str(d / "scripts" / "test.sh"), "--jobs", "2"], cwd=d, env=e,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                self.addCleanup(lambda p=p: (p.poll() is None and os.killpg(p.pid, signal.SIGKILL), p.communicate()))
                for _ in range(300):
                    lines = pids.read_text().splitlines() if pids.exists() else []
                    if len(lines) == 2:
                        break
                    time.sleep(0.1)
                workers = [int(line.split()[0]) for line in lines]
                runners = {int(line.split()[1]) for line in lines}
                self.assertEqual((len(workers), len(runners)), (2, 1), lines)
                runner = runners.pop()
                self.assertEqual([os.getpgid(w) for w in workers], [p.pid, p.pid])
                self.assertEqual(os.getpgid(runner), runner)
                os.kill(runner, sig)
                out, _ = p.communicate(timeout=60)
                self.assertEqual((p.returncode, out.splitlines()[-1:]), (1, ["FAILED"]), out)
                if sig != signal.SIGKILL:
                    self.assertIn(f"FAIL: the unit tests did not finish (no verdict): stopped by {sig.name}", out)
                    self.assertIn(f"FAIL: the unit tests did not finish (no verdict): worker 1 of 2 ended with "
                                  f"-{sig.value}, without its record", out)
                self.assertIn("\n== shell syntax", out)
                for w in workers:
                    for _ in range(100):
                        if not os.path.exists(f"/proc/{w}"):
                            break
                        time.sleep(0.1)
                    self.assertFalse(os.path.exists(f"/proc/{w}"), f"worker {w} outlived the run")

    def test_a_worker_that_dies_of_sigkill(self):
        # as when the OOM killer takes a worker: the run has no verdict, the runner then dies of SIGKILL too, and bash
        # goes on with the other steps, as it did when the OOM killer took the process of the tests.  Before, the
        # runner failed with a traceback, as Python cannot set the action of SIGKILL
        d = self.tree()
        (d / "tests" / "test_k.py").write_text(
            "import os, signal, unittest\nclass K(unittest.TestCase):\n    def test_k(self):\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n")
        r = self.run_sh(d, "--jobs", "2")
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"FAIL: the unit tests did not finish \(no verdict\): worker \d of \d ended with -9, "
                                   r"without its record\n")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("\n== shell syntax", r.stdout)
        self.assertRegex(r.stderr, r"Killed +TMPDIR=")

    def test_a_signal_to_the_group_reaches_what_the_tests_started(self):
        # a Ctrl-C, a hang-up or a kill -- -PGID reaches scripts/test.sh's whole process group: the runner, or its
        # workers, and what the tests started in it, which the signal ends.  Before, the workers ran in groups of their
        # own, and a test's child outlived the run
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                d = self.tree()
                tag = f"sleep 296.{os.getpid()}{sig.value}{time.monotonic_ns() % 10**6}"
                (d / "tests" / "test_sl.py").write_text(
                    "import subprocess, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n"
                    f"        subprocess.run(['bash', '-c', {tag + '; true'!r}])\n")
                self.addCleanup(lambda tag=tag: [os.kill(q, signal.SIGKILL) for q in self.with_text(tag)])
                p, out = self.start_sh(d)
                self.wait_for(lambda: self.with_text(tag))
                time.sleep(0.3)
                os.killpg(p.pid, sig)
                p.wait(timeout=60)
                time.sleep(0.5)
                self.assertEqual(self.with_text(tag), [], "a process of the test outlived the run")

    def test_a_signal_to_the_group_ends_the_run_as_it_ended_the_tests(self):
        # scripts/test.sh ends of the signal and runs none of its other steps, as when the tests ran in the runner,
        # which then died of it: bash stops after a Ctrl-C only when its command died of SIGINT.  Before, the runner
        # ended with exit 1 after a Ctrl-C, and bash went on with the other steps
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                d = self.tree()
                started = d.parent / "started"
                (d / "tests" / "test_sl.py").write_text(
                    "import time, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n"
                    f"        open({str(started)!r}, 'w').close()\n        time.sleep(60)\n")
                p, out = self.start_sh(d)
                self.wait_for(started.exists)
                time.sleep(0.3)
                os.killpg(p.pid, sig)
                self.assertEqual(p.wait(timeout=60), -sig, out.read_text())
                self.assertNotIn("\n== shell syntax", out.read_text())

    def test_a_signal_to_the_group_reaches_each_test_once(self):
        # a test that handles SIGINT itself gets a Ctrl-C to the group once, and the run goes on to its verdict, as
        # when the tests ran in the runner: the runner, which passes on what is sent to it alone, is not in the group.
        # (The signal reaches every worker, and one whose test does not handle it ends: here it comes once the other
        # workers have ended)
        d = self.tree()
        got, started = d.parent / "got", d.parent / "started"
        (d / "tests" / "test_c.py").write_text(
            "import os, signal, time, unittest\nclass C(unittest.TestCase):\n    def test_c(self):\n"
            f"        signal.signal(signal.SIGINT, lambda *a: open({str(got)!r}, 'a').write('x'))\n"
            f"        open({str(started)!r}, 'w').write(str(os.getppid()))\n        time.sleep(5)\n")
        p, out = self.start_sh(d)
        self.wait_for(lambda: started.exists() and started.read_text())
        runner = started.read_text()

        def children():
            found = []
            for q in filter(str.isdigit, os.listdir("/proc")):
                try:
                    if Path(f"/proc/{q}/stat").read_text().rpartition(")")[2].split()[1] == runner:
                        found.append(q)
                except OSError:
                    pass
            return found
        self.wait_for(lambda: len(children()) == 1)
        os.killpg(p.pid, signal.SIGINT)
        self.assertEqual(p.wait(timeout=60), 0, out.read_text())
        self.assertEqual(got.read_text(), "x")

    def test_a_stop_of_the_group_stops_the_tests(self):
        # SIGSTOP to scripts/test.sh's process group (as a Ctrl-Z stops the job at a terminal) stops the tests, and
        # SIGCONT lets them go on.  Before, the workers ran on in groups of their own
        d = self.tree()
        tick = d.parent / "tick"
        (d / "tests" / "test_t.py").write_text(
            "import time, unittest\nclass T(unittest.TestCase):\n    def test_t(self):\n        for _ in range(40):\n"
            f"            with open({str(tick)!r}, 'a') as f:\n                f.write('x')\n            time.sleep(0.1)\n")
        p, out = self.start_sh(d)
        self.wait_for(tick.exists)
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGSTOP)
        try:
            time.sleep(0.3)
            before = tick.stat().st_size
            time.sleep(1.5)
            self.assertEqual(tick.stat().st_size, before, "the test went on while its group was stopped")
        finally:
            os.killpg(p.pid, signal.SIGCONT)
        self.assertEqual(p.wait(timeout=60), 0, out.read_text())
        self.assertEqual(tick.stat().st_size, 40)

    def test_what_python_does_as_the_tests_end(self):
        # as a process ends: it waits for its threads, then runs the atexit handlers, the finalizers of weakref (as
        # tempfile.TemporaryDirectory's cleanup) among them.  Those registered at the loading of the tests run once,
        # at the end of the run, and not while another worker still runs tests.  Before, a worker ended with os._exit
        # and none of those of its tests ran
        d = self.tree()
        o = d.parent / "o"
        o.mkdir()
        (d / "tests" / "test_end.py").write_text(f"""import atexit, os, shutil, tempfile, threading, time, unittest
OUT = {str(o)!r}
def note(what):
    with open(os.path.join(OUT, 'log'), 'a') as f:
        f.write(what + '\\n')
atexit.register(note, 'loading')
D = tempfile.TemporaryDirectory(dir=OUT)
keep = []
class A(unittest.TestCase):
    def test_0(self):
        d = tempfile.mkdtemp(dir=OUT)
        atexit.register(shutil.rmtree, d)
        keep.append(tempfile.TemporaryDirectory(dir=OUT))
        threading.Thread(target=lambda: (time.sleep(1), note('thread'))).start()
    def test_1(self):
        time.sleep(2)
        self.assertTrue(os.path.isdir(D.name))
""")
        self.passes(d)
        self.assertEqual(sorted(os.listdir(o)), ["log"])
        self.assertEqual(sorted((o / "log").read_text().splitlines()), ["loading", "thread"])

    def test_what_libraries_register_as_the_tests_load(self):
        # an atexit handler that a library registers as a test module imports it runs in each worker too:
        # multiprocessing.util's, which ends the daemonic children of the process.  One that a test module registers as it
        # loads runs once, at the end of the run, also when its function is a library's (shutil.rmtree), and not while
        # another worker still runs tests.  Before, a worker ran none of the loading's handlers, and a daemonic child
        # outlived the run
        d = self.tree()
        o, pids = d.parent / "o", d.parent / "pids"
        o.mkdir()
        self.addCleanup(self.end_children, pids)
        (d / "tests" / "test_mp.py").write_text(
            "import atexit, multiprocessing, multiprocessing.util, os, shutil, tempfile, time, unittest\n"
            f"E = tempfile.mkdtemp(dir={str(o)!r})\natexit.register(shutil.rmtree, E)\n"
            "class M(unittest.TestCase):\n    def test_0(self):\n"
            "        c = multiprocessing.Process(target=time.sleep, args=(60,), daemon=True)\n        c.start()\n"
            f"        with open({str(pids)!r}, 'a') as f:\n            f.write(f'{{c.pid}}\\n')\n"
            "    def test_1(self):\n        time.sleep(2)\n        self.assertTrue(os.path.isdir(E))\n")
        p, out = self.start_sh(d)
        self.assertEqual(p.wait(timeout=60), 0, out.read_text())
        self.assertEqual(os.listdir(o), [])
        children = pids.read_text().split()
        self.assertEqual(len(children), 1)
        for _ in range(50):
            if not os.path.exists(f"/proc/{children[0]}"):
                break
            time.sleep(0.1)
        self.assertFalse(os.path.exists(f"/proc/{children[0]}"), "the daemonic child outlived the run")
        # a handler registered before the tests load (here by a sitecustomize module) is one that the runner did not
        # see: a worker would drop it, so the run fails
        site = d.parent / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text("import atexit\natexit.register(print, end='')\n")
        (d / "tests" / "test_mp.py").unlink()
        self.fails(d, "FAIL: atexit has handlers that the runner did not see registered as the tests loaded: a worker "
                      "would not run them", env={"PYTHONPATH": str(site)})

    @staticmethod
    def end_children(pids: Path) -> None:
        """SIGKILL to each process named in pids that runs a copy of the runner (its command line names the verdict)."""
        for q in pids.read_text().split() if pids.exists() else ():
            try:
                if b"verdict" in Path(f"/proc/{q}/cmdline").read_bytes():
                    os.kill(int(q), signal.SIGKILL)
            except OSError:
                pass

    def test_a_signal_that_the_loading_holds(self):
        # a test module that blocks a signal at its import and sends it to itself: it stays pending as it did in the
        # runner, also in the workers, which start without it; a test that unblocks it then gets it, and the run ends
        # (SIGTERM) as it ended in the runner.  Before, the workers unblocked SIGINT, SIGTERM and SIGHUP
        head = ("import os, signal, time, unittest\nsignal.pthread_sigmask(signal.SIG_BLOCK, {{signal.{0}}})\n"
                "os.kill(os.getpid(), signal.{0})\nclass H(unittest.TestCase):\n    def test_h(self):\n")
        d = self.tree()
        (d / "tests" / "test_held.py").write_text(head.format("SIGINT") + "        time.sleep(0.5)\n")
        self.passes(d)
        (d / "tests" / "test_held.py").write_text(
            head.format("SIGTERM") + "        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
                                     "        time.sleep(5)\n")
        r = self.run_sh(d)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (1, ["FAILED"]), r.stdout + r.stderr)
        self.assertIn("\n== shell syntax", r.stdout)
        self.assertNotIn("\nOK", r.stdout)


class TestShJobs(TestSh):
    """The cases of TestSh about the unit-test step, again with --jobs 3: what one worker sees (a test that fails,
    never runs, returns a value, is skipped for another reason ...) fails the run as it does with one, also when the
    code that makes something a helper ran in another worker.  The workers take the units in turn (TEST_JOBS_DEAL), so
    that two tests next to each other in the order run in different workers on every run."""
    JOBS = ("--jobs", "3")
    DEAL = {"TEST_JOBS_DEAL": "1"}


for _name in [n for n in vars(TestSh) if n.startswith("test_") and n not in {
        "test_a_tree_without_a_fault_passes", "test_no_test_ran", "test_a_test_file_discovery_does_not_load",
        "test_tests_that_cannot_run", "test_no_test_ran_when_classes_or_modules_are_skipped", "test_a_failing_test",
        "test_a_test_that_expects_to_fail", "test_a_test_that_ends_the_run", "test_a_test_module_without_a_test",
        "test_every_test_skipped", "test_skips", "test_tests_whose_body_never_runs",
        "test_forms_round_4_mutation_testing_found_untested",
        "test_tests_behind_decorators_without_wraps_and_helpers_named_test", "test_files_that_are_not_regular",
        "test_modules_and_tests_the_runner_once_refused", "test_forms_round_6_mutation_testing_found_untested",
        "test_helpers_skipped_modules_and_tests_without_a_name", "test_forms_round_7_mutation_testing_found_untested",
        "test_helpers_are_read_by_code_that_may_run", "test_forms_round_8_mutation_testing_found_untested",
        "test_a_helper_that_a_skipped_tests_method_reads_through_self",
        "test_forms_round_9_mutation_testing_found_untested",
        "test_helpers_through_methods_of_related_classes_and_more_forms",
        "test_forms_round_10_mutation_testing_found_untested", "test_a_signal_to_the_group_reaches_what_the_tests_started",
        "test_a_signal_to_the_group_ends_the_run_as_it_ended_the_tests", "test_a_stop_of_the_group_stops_the_tests",
        "test_a_signal_to_the_group_reaches_each_test_once",
        "test_what_python_does_as_the_tests_end", "test_a_signal_that_the_loading_holds",
        "test_what_libraries_register_as_the_tests_load", "test_what_the_tests_leave_in_their_tmpdir",
        "test_a_worker_that_dies_of_sigkill"}]:
    setattr(TestShJobs, _name, None)          # not loaded: the other steps do not depend on --jobs


if __name__ == "__main__":
    unittest.main()
