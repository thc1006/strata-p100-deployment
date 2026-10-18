"""scripts/deploy.sh in throwaway repositories: a bare origin, a clone that plays this checkout, and stubs for GitHub's
API (curl), systemctl, timeout, tar, scripts/test.sh and scripts/check-drift.sh.  Needs git; no network, no service, no
sudo."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

DEPLOY = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
URL = "https://github.com/example/ops.git"   # never contacted: url.<origin>.insteadOf sends git to the bare repo
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}   # not a caller's GIT_DIR or GIT_INDEX_FILE
GIT_OPTS = "-c gc.auto=0 -c maintenance.auto=false -c core.quotePath=true"

STUBS = {
    # GET .../actions/workflows/test.yml/runs?head_sha=<sha>&...: runs/<sha>.json; without it, or with runs/<sha>.http
    # and -f (an HTTP error), curl exits 22
    "bin/curl": """#!/bin/sh
echo "$*" >> "$STUB_DIR/curl.log"
fail=0
for a; do url=$a; case $a in --fail) fail=1 ;; --*) ;; -*f*) fail=1 ;; esac; done
sha=${url#*head_sha=}; sha=${sha%%&*}
[ -e "$STUB_DIR/runs/$sha.json" ] || exit 22
[ ! -e "$STUB_DIR/runs/$sha.http" ] || [ $fail = 0 ] || exit 22
cat "$STUB_DIR/runs/$sha.json"
""",
    # ActiveState: the next word of $STUB_STATES at each call (then the last one again); Job: $STUB_JOB
    "bin/systemctl": """#!/bin/sh
case "$*" in
  "show -p ActiveState --value strata-q3.service")
    n=$(( $(cat "$STUB_DIR/states.n" 2>/dev/null || echo 0) + 1 )); echo $n > "$STUB_DIR/states.n"
    set -- ${STUB_STATES-active}
    if [ $# = 0 ]; then echo; else [ $n -le $# ] || n=$#; eval "echo \\"\\${$n}\\""; fi ;;
  "show -p Job --value strata-q3.service") [ -z "${STUB_JOB_FAIL-}" ] || exit 1; echo "${STUB_JOB-}" ;;
  *) exit 1 ;;
esac
""",
    # the real timeout, except for a command named $STUB_TIMEOUT_FOR: that one "runs out of time"
    "bin/timeout": """#!/bin/sh
echo "$*" >> "$STUB_DIR/timeout.log"
if [ -n "${STUB_TIMEOUT_FOR-}" ]; then
  for a; do case $a in */"$STUB_TIMEOUT_FOR") exit 124 ;; esac; done
fi
PATH=${PATH#"$STUB_DIR/bin:"} exec timeout "$@"
""",
    # the real tar, unless $STUB_TAR_FAIL is set
    "bin/tar": """#!/bin/sh
[ -z "${STUB_TAR_FAIL-}" ] || exit 2
PATH=${PATH#"$STUB_DIR/bin:"} exec tar "$@"
""",
}
# the repository's own checks; each run is logged with the directory it ran from, must not hold the deploy lock (fd 9),
# and runs $STUB_DURING first: what someone else does while the checks run
REPO_FILES = {
    "scripts/test.sh": """#!/bin/sh
d=$(cd "$(dirname "$0")/.." && pwd)
echo "test.sh $d" >> "$STUB_DIR/ran.log"
[ $# = 0 ] || { echo "unexpected arguments: $*"; exit 3; }
[ ! -e /proc/$$/fd/9 ] || { echo "the deploy lock is open here"; exit 4; }
[ -z "${STUB_DURING-}" ] || sh -c "$STUB_DURING"
[ ! -e "$d/SLOW_TEST" ] || { echo $$ > "$STUB_DIR/slow.pid"; sleep 30; }
[ ! -e "$d/FAIL_TEST" ] || { echo "FAILED"; exit 1; }
[ ! -e "$d/CTRL_TEST" ] || { printf 'red \\033[31m, reversed \\342\\200\\256, bell \\007\\n'; exit 1; }
[ ! -e "$d/QUIET_TEST" ] || exit 0
echo "all checks passed"
""",
    "scripts/check-drift.sh": """#!/bin/sh
d=$(cd "$(dirname "$0")/.." && pwd)
echo "check-drift.sh $d" >> "$STUB_DIR/ran.log"
[ ! -e /proc/$$/fd/9 ] || { echo "the deploy lock is open here"; exit 4; }
if [ -e "$d/DRIFT" ] || [ "$d" = "${STUB_DRIFT_IN-}" ]; then echo "DRIFT  /etc/systemd/system/x.service"; exit 1; fi
[ ! -e "$d/DRIFT_EXIT0" ] || echo "DRIFT  /etc/systemd/system/x.service"    # a check that lost its exit status
echo "no drift"
""",
    "README.md": "base\n",
}


def run(*args: str, env: dict, cwd: Path | None = None) -> str:
    return subprocess.run(args, env=env, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def isolated(root: Path) -> dict:
    """An environment in which git reads no configuration of this machine or of its user."""
    return {**CLEAN_ENV, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
            "HOME": str(root), "XDG_CONFIG_HOME": str(root / ".config")}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class DeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """One origin, work clone and live clone, copied for each test."""
        cls.template = Path(tempfile.mkdtemp(prefix="deploy-template-"))
        cls.addClassCleanup(shutil.rmtree, cls.template)
        env = isolated(cls.template)
        t = cls.template
        for name, text in STUBS.items():
            cls.write(t / "stub" / name, text, 0o755)
        run("git", "init", "-q", "--bare", "-b", "main", str(t / "origin.git"), env=env)
        run("git", "init", "-q", "-b", "main", str(t / "work"), env=env)
        for name, text in {"scripts/deploy.sh": DEPLOY.read_text(), **REPO_FILES}.items():
            cls.write(t / "work" / name, text, 0o755 if name.endswith(".sh") else 0o644)
        run("git", "-C", str(t / "work"), "add", "-A", env=env)
        run("git", "-C", str(t / "work"), "-c", "user.name=test", "-c", "user.email=", "commit", "-q", "-m", "base",
            env=env)
        run("git", "-C", str(t / "work"), "push", "-q", str(t / "origin.git"), "HEAD:main", env=env)
        run("git", "clone", "-q", str(t / "origin.git"), str(t / "live"), env=env)
        cls.base = run("git", "-C", str(t / "work"), "rev-parse", "HEAD", env=env)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="deploy-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copytree(self.template, self.root, symlinks=True, dirs_exist_ok=True)
        self.stub, self.origin, self.work, self.live = (self.root / d for d in ("stub", "origin.git", "work", "live"))
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.env = {**isolated(self.root), "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": "test",
                    "GIT_COMMITTER_EMAIL": "", "STUB_DIR": str(self.stub), "TMPDIR": str(self.tmpdir)}
        self.use_origin_url(URL)

    @staticmethod
    def write(path: Path, text: str, mode: int = 0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def git(self, repo: Path, *args: str) -> str:
        return run("git", "-C", str(repo), *args, env=self.env)

    def use_origin_url(self, url: str):
        """Give the live clone this origin URL; git still fetches from the bare repository."""
        self.git(self.live, "config", "remote.origin.url", url)
        self.git(self.live, "config", "--replace-all", f"url.{self.origin}.insteadOf", url)

    def commit(self, files: dict[str, str], message: str = "change", parent: str | None = None) -> str:
        """Commit the files in the work clone, on parent if given, and make that commit origin's main."""
        if parent:
            self.git(self.work, "reset", "-q", "--hard", parent)
        for name, text in files.items():
            self.write(self.work / name, text, 0o755 if name.endswith(".sh") else 0o644)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)
        self.git(self.work, "push", "-q", str(self.origin), "+HEAD:main")
        return self.git(self.work, "rev-parse", "HEAD")

    def runs(self, sha: str, *runs: dict, total: int | None = None, http_error: bool = False):
        """GitHub's answer for the workflow runs of sha: by default one passed push run on main; each dict overrides."""
        rows = [{"path": ".github/workflows/test.yml", "head_sha": sha, "head_branch": "main", "event": "push",
                 "status": "completed", "conclusion": "success", "run_number": n, **r}
                for n, r in enumerate(runs or ({},), 1)]
        data = {"total_count": len(rows) if total is None else total, "workflow_runs": rows}
        self.write(self.stub / "runs" / f"{sha}.json", json.dumps(data))
        if http_error:
            self.write(self.stub / "runs" / f"{sha}.http", "")

    def start(self, *args: str, **env: str) -> subprocess.Popen:
        for log in ("curl.log", "ran.log", "timeout.log", "states.n", "slow.pid"):
            (self.stub / log).unlink(missing_ok=True)
        path = f"{self.stub / 'bin'}{os.pathsep}{os.environ['PATH']}"
        return subprocess.Popen([str(self.live / "scripts" / "deploy.sh"), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**self.env, "PATH": path, **env},
                                start_new_session=True)       # its own process group, like a command on a terminal

    def deploy(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        p = self.start(*args, **env)
        stdout, stderr = p.communicate(timeout=120)
        self.assertEqual(list(self.tmpdir.iterdir()), [], "deploy.sh left files in TMPDIR")
        return subprocess.CompletedProcess(p.args, p.returncode, stdout, stderr)

    def log(self, name: str) -> str:
        p = self.stub / name
        return p.read_text() if p.exists() else ""

    def head(self, ref: str = "HEAD") -> str:
        return self.git(self.live, "rev-parse", ref)

    def assertRefused(self, r: subprocess.CompletedProcess, reason: str, head: str):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"not deployed: {reason}", r.stderr)
        self.assertEqual(self.head("main"), head, "a refused deploy moved main")

    def test_up_to_date_asks_nobody(self):
        r = self.deploy("--apply", STUB_STATES="activating")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("up to date: "), r.stdout)
        self.assertEqual(self.log("curl.log") + self.log("ran.log") + self.log("states.n"), "")

    def test_dry_run_reports_and_apply_fast_forwards(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"dry run: scripts/deploy.sh --apply deploys {new[:7]}", r.stdout)
        self.assertEqual(self.head(), self.base)
        curl = self.log("curl.log")
        self.assertIn("--max-time 30 ", curl)
        self.assertIn(f"/repos/example/ops/actions/workflows/test.yml/runs?head_sha={new}&event=push&branch=main"
                      "&per_page=100", curl)
        ran = self.log("ran.log").splitlines()
        self.assertEqual([line.split()[0] for line in ran], ["test.sh", "check-drift.sh"])
        self.assertNotIn(str(self.live), self.log("ran.log"), "the checks ran from this checkout, not the new commit")
        timeouts = self.log("timeout.log").splitlines()
        self.assertTrue(timeouts[0].startswith(f"-k 10 120 git {GIT_OPTS} fetch "), timeouts)
        self.assertTrue(timeouts[1].startswith("-k 10 600 ") and timeouts[1].endswith("/scripts/test.sh"), timeouts)
        r = self.deploy("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"deployed {new[:7]}: change", r.stdout)
        self.assertNotIn("to run them now", r.stdout)
        self.assertEqual(self.head(), new)
        self.assertEqual(self.git(self.live, "status", "--porcelain"), "")
        self.assertIn(f"check-drift.sh {self.live.resolve()}", self.log("ran.log"))
        self.assertEqual(self.deploy("--apply").stdout.split(":")[0], "up to date")

    def test_the_newest_run_decides(self):
        new = self.commit({"docs.md": "a\n"})
        for runs in ([{"conclusion": "failure"}, {}], [{}, {}],
                     [{"run_number": 7}, {"run_number": 3, "conclusion": "failure"}]):
            with self.subTest(runs=runs):
                self.runs(new, *runs)
                r = self.deploy()
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_refuses_without_a_passing_run(self):
        new = self.commit({"docs.md": "a\n"})
        cases = [([{"status": "queued", "conclusion": None}], "queued"),
                 ([{"status": "in_progress", "conclusion": None}], "in_progress"),
                 ([{}, {"status": "in_progress", "conclusion": None}], "in_progress")]
        cases += [([{"conclusion": c}], f"completed {c}") for c in ("failure", "cancelled", "skipped", "neutral")]
        cases += [([{key: value}], "missing") for key, value in (("path", ".github/workflows/other.yml"),
                  ("event", "pull_request"), ("head_branch", "other"), ("head_sha", "0" * 40))]
        for runs, state in cases:
            with self.subTest(state=state, runs=runs):
                self.runs(new, *runs)
                r = self.deploy("--apply")
                self.assertRefused(r, f"the newest run of test.yml for {new[:7]} is {state}", self.base)
                self.assertEqual(self.log("ran.log"), "", "the checks ran before GitHub said yes")
        self.runs(new, {}, total=101)
        self.assertRefused(self.deploy("--apply"), "the newest run of test.yml for "
                           f"{new[:7]} is unreadable: more runs than one page", self.base)
        self.write(self.stub / "runs" / f"{new}.json", "<html>")
        self.assertRefused(self.deploy("--apply"), f"the newest run of test.yml for {new[:7]} is unreadable", self.base)
        self.runs(new, http_error=True)
        self.assertRefused(self.deploy("--apply"), f"cannot read the runs of test.yml for {new[:7]}", self.base)
        (self.stub / "runs" / f"{new}.json").unlink()
        self.assertRefused(self.deploy("--apply"), f"cannot read the runs of test.yml for {new[:7]}", self.base)

    def test_refuses_commits_that_apply_outside_this_checkout(self):
        for path in ("systemd/strata-q3-deploy.timer", "config/profile.conf", "scripts/strata-health.sh",
                     "scripts/install-system-files.sh", "scripts/check-drift.sh"):
            with self.subTest(path=path):
                self.runs(self.commit({path: "# new\n", "docs.md": path}, parent=self.base))
                r = self.deploy("--apply")
                self.assertRefused(r, f"the commits change {path}, which apply outside this checkout", self.base)
                self.assertEqual(self.log("curl.log") + self.log("ran.log"), "")

    def test_refuses_local_changes(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        (self.live / "README.md").write_text("edited here\n")
        self.assertRefused(self.deploy("--apply"), "this checkout has local changes", self.base)
        self.git(self.live, "checkout", "--", "README.md")
        self.git(self.live, "config", "status.showUntrackedFiles", "no")
        (self.live / "notes.txt").write_text("untracked\n")
        self.assertRefused(self.deploy("--apply"), "this checkout has local changes", self.base)
        (self.live / "notes.txt").unlink()
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag=flag):
                self.git(self.live, "update-index", flag, "README.md")
                (self.live / "README.md").write_text("edited here, hidden\n")
                self.assertRefused(self.deploy("--apply"), "this checkout hides files from git status", self.base)
                self.git(self.live, "update-index", flag.replace("--", "--no-"), "README.md")
                self.git(self.live, "checkout", "--", "README.md")

    def test_refuses_when_the_checkout_changes_during_the_checks(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        live = self.live
        cases = [(f"git -C {live} checkout -q -b other", "this checkout is not on branch main", "other"),
                 (f"git -C {live} commit -q --allow-empty -m moved", "this checkout moved while the checks ran", ""),
                 (f"echo changed > {live}/README.md", "this checkout has local changes", "")]
        for during, reason, branch in cases:
            with self.subTest(during=during):
                r = self.deploy("--apply", STUB_DURING=during)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"not deployed: {reason}", r.stderr)
                self.assertNotEqual(self.head(), new)
                self.git(live, "checkout", "-q", "-f", "main")
                self.git(live, "reset", "-q", "--hard", self.base)
                if branch:
                    self.git(live, "branch", "-q", "-D", branch)

    def test_refuses_another_branch_or_a_detached_head(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        self.git(self.live, "checkout", "-q", "-b", "other")
        self.assertRefused(self.deploy("--apply"), "this checkout is not on branch main", self.base)
        self.git(self.live, "checkout", "-q", "--detach", "main")
        self.assertRefused(self.deploy("--apply"), "this checkout is not on branch main", self.base)

    def test_refuses_commits_that_origin_does_not_have(self):
        self.write(self.live / "local.md", "a commit made here\n")
        self.git(self.live, "add", "local.md")
        self.git(self.live, "commit", "-q", "-m", "local")
        local = self.head()
        self.assertRefused(self.deploy("--apply"), "origin/main", local)          # ahead of origin/main
        self.runs(self.commit({"docs.md": "a\n"}))
        r = self.deploy("--apply")                                                 # and origin/main moved on
        self.assertRefused(r, "origin/main", local)
        self.assertIn("is not a fast-forward", r.stderr)

    def test_service_state(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        cases = [({"STUB_STATES": "activating"}, "activating"), ({"STUB_STATES": "reloading"}, "reloading"),
                 ({"STUB_STATES": "maintenance"}, "maintenance"), ({"STUB_STATES": ""}, "in an unknown state"),
                 ({"STUB_JOB": "42"}, "active, with job 42 queued"), ({"STUB_JOB_FAIL": "1"}, "in an unknown state")]
        for env, shown in cases:
            with self.subTest(env=env):
                r = self.deploy("--apply", **env)
                self.assertRefused(r, f"strata-q3.service is {shown};", self.base)
                self.assertEqual(self.log("curl.log") + self.log("ran.log"), "")
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                r = self.deploy(STUB_STATES=state)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"ok     strata-q3.service is {state}", r.stdout)
        r = self.deploy("--apply", STUB_STATES="active activating")     # it starts while the checks run
        self.assertRefused(r, "strata-q3.service is activating;", self.base)
        self.assertIn("check-drift.sh", self.log("ran.log"))

    def test_refuses_a_commit_whose_checks_fail_here(self):
        cases = [({"FAIL_TEST": ""}, "scripts/test.sh", "exit 1", "  FAILED"),
                 ({"QUIET_TEST": ""}, "scripts/test.sh", 'exit 0, but no "all checks passed" at the end', ""),
                 ({"DRIFT": ""}, "scripts/check-drift.sh", "exit 1", "  DRIFT  /etc/systemd/system/x.service"),
                 ({"DRIFT_EXIT0": ""}, "scripts/check-drift.sh", "exit 0, but with DRIFT lines", "  DRIFT")]
        for files, script, why, shown in cases:
            with self.subTest(files=files):
                new = self.commit({**files, "docs.md": str(files)}, parent=self.base)
                self.runs(new)
                r = self.deploy("--apply")
                self.assertRefused(r, f"{script} of {new[:7]} failed: {why}", self.base)
                self.assertIn(shown, r.stdout)
                if script == "scripts/test.sh":
                    self.assertNotIn("check-drift.sh", self.log("ran.log"))
        self.runs(self.commit({"docs.md": "a\n"}, parent=self.base))
        for script in ("test.sh", "check-drift.sh"):
            with self.subTest(timeout=script):
                r = self.deploy("--apply", STUB_TIMEOUT_FOR=script)
                self.assertRefused(r, f"scripts/{script} of", self.base)
                self.assertIn("failed: it did not finish in 600 s", r.stderr)

    def test_ctrl_c_stops_it_before_the_merge(self):
        new = self.commit({"SLOW_TEST": "", "docs.md": "a\n"})
        self.runs(new)
        p = self.start("--apply")
        deadline = time.monotonic() + 20
        while not (self.stub / "slow.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        slow = int((self.stub / "slow.pid").read_text())
        os.killpg(p.pid, signal.SIGINT)                 # what a Ctrl-C on the terminal sends
        stdout, stderr = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 1, stdout + stderr)
        self.assertIn("not deployed: interrupted", stderr)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(list(self.tmpdir.iterdir()), [])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(slow, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the check of the new commit is still running")

    def test_names_what_the_service_runs_at_its_next_start(self):
        new = self.commit({"scripts/strata-preflight.sh": "# changed\n", "scripts/strata-wait-ready.sh": "# changed\n",
                           "tools/configure_strata_q3.py": "# changed\n", "docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertIn("note   strata-q3.service runs these from this checkout at its next start: "
                      "scripts/strata-preflight.sh scripts/strata-wait-ready.sh tools/configure_strata_q3.py\n",
                      r.stdout)
        self.assertIn("to run them now: sudo systemctl restart strata-q3", r.stdout)
        self.runs(self.commit({"docs.md": "b\n"}))                             # only a document changes now
        r = self.deploy("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("next start", r.stdout)
        self.assertNotIn("to run them now", r.stdout)

    def test_reports_drift_that_appears_after_the_fast_forward(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        r = self.deploy("--apply", STUB_DRIFT_IN=str(self.live.resolve()))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("deployed, but scripts/check-drift.sh now finds drift: exit 1", r.stderr)
        self.assertEqual(self.head(), new)

    def test_a_deploy_that_replaces_this_script(self):
        new = self.commit({"scripts/deploy.sh": DEPLOY.read_text() + "# a newer version\n"})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertTrue((self.live / "scripts" / "deploy.sh").read_text().endswith("# a newer version\n"))

    def test_keeps_ignored_files(self):
        self.runs(self.commit({"notes.md": "from the commit\n"}))
        with open(self.live / ".git" / "info" / "exclude", "a") as f:
            f.write("notes.md\n")
        (self.live / "notes.md").write_text("kept here\n")
        self.assertRefused(self.deploy("--apply"), "git merge --ff-only", self.base)
        self.assertEqual((self.live / "notes.md").read_text(), "kept here\n")

    def test_ignores_the_callers_git_variables(self):
        new = self.commit({"docs.md": "a\n"})
        self.runs(new)
        work_head = self.git(self.work, "rev-parse", "HEAD")
        r = self.deploy("--apply", GIT_DIR=str(self.work / ".git"), GIT_WORK_TREE=str(self.work),
                        GIT_INDEX_FILE=str(self.root / "index-of-a-hook"))
        self.assertEqual((r.returncode, self.head()), (0, new), r.stderr)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), work_head)
        self.assertFalse((self.root / "index-of-a-hook").exists())

    def test_prints_no_control_characters(self):
        new = self.commit({"docs.md": "a\n"}, message="subject \x1b[8mhidden\x1b[0m \x9b31m ‮reversed \x07end")
        self.runs(new)
        r = self.deploy("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hidden", r.stdout)
        new = self.commit({"CTRL_TEST": ""})
        self.runs(new)
        r = self.deploy("--apply")
        self.assertRefused(r, f"scripts/test.sh of {new[:7]} failed", self.head())
        self.assertIn("reversed", r.stdout)
        for c in ("\x1b", "\x9b", "\x07", "‮"):
            self.assertNotIn(c, r.stdout)

    def test_origin_must_be_github_and_its_password_stays_hidden(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        self.use_origin_url("https://user:fixture-password@gitlab.com/example/ops.git")
        r = self.deploy("--apply")
        self.assertRefused(r, "origin is not a GitHub repository: https://gitlab.com/example/ops", self.base)
        self.assertNotIn("fixture-password", r.stdout + r.stderr)
        for url in ("https://user:fixture-password@github.com/example/ops.git", "https://github.com/example/ops/",
                    "ssh://git@github.com/example/ops.git", "git@github.com:example/ops", "https://GitHub.com/example/ops"):
            with self.subTest(url=url):
                self.use_origin_url(url)
                r = self.deploy()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("/repos/example/ops/actions/", self.log("curl.log"))
                self.assertNotIn("fixture-password", r.stdout + r.stderr)

    def test_refuses_when_git_or_the_lock_fails(self):
        self.runs(self.commit({"docs.md": "a\n"}))
        with open(self.live / ".git" / "deploy.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            r = self.deploy("--apply")
            self.assertRefused(r, "another scripts/deploy.sh is running", self.base)
            self.assertEqual((r.stdout, self.log("curl.log")), ("", ""))
        self.assertRefused(self.deploy("--apply", STUB_TAR_FAIL="1"), "cannot export", self.base)
        (self.live / ".git" / "index.lock").write_text("")
        self.assertRefused(self.deploy("--apply"), "git merge --ff-only", self.base)
        (self.live / ".git" / "index.lock").unlink()
        index = (self.live / ".git" / "index").read_bytes()
        (self.live / ".git" / "index").write_bytes(b"not an index")
        self.assertRefused(self.deploy("--apply"), "git status failed", self.base)
        (self.live / ".git" / "index").write_bytes(index)
        shutil.rmtree(self.origin)
        self.assertRefused(self.deploy("--apply"), "git fetch origin main failed", self.base)

    def test_usage(self):
        for args in (["--force"], ["--apply", "--apply"], ["apply"]):
            with self.subTest(args=args):
                r = self.deploy(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("usage: scripts/deploy.sh [--apply]", r.stderr)
        self.assertEqual(self.log("curl.log"), "")


if __name__ == "__main__":
    unittest.main()
