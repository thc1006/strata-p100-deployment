"""Running a stub harness (tests/*_stubs.sh) from a unit test.

A harness starts fake servers and fake engines in sessions of their own, and a fake engine has the command line that
strata-preflight.sh looks for: while one lives, a start of strata-q3 is refused.  So a harness runs here in a session and
a temporary directory of its own (its sandbox is made under TMPDIR); a run past its time gets SIGTERM, so that its EXIT
trap stops what it started, then SIGKILL; and every process whose command line still names the directory is killed
and reported.  A harness does not start while an engine experiment, a scheduled restore or a start of strata-q3 is under
way, since a start in that time would be refused."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile


def _cmdlines():
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            yield int(pid), Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:                         # gone, or not ours to read
            continue


def sweep(path: str) -> list[str]:
    """Kill every process whose command line names path; the command lines killed."""
    killed = []
    for pid, cmd in _cmdlines():
        if pid != os.getpid() and any(path.encode() in part for part in cmd):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(b" ".join(cmd).decode(errors="replace")[:200])
            except OSError:
                pass
    return killed


def _active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):  # no systemd here (CI's container): nothing can start the service
        return False
    return r.stdout.strip() in ("active", "activating", "reloading")


def not_now() -> str | None:
    """Why a harness must not run now, or None."""
    drivers = (b"bench/ab_engine.sh", b"bench/ab_model_run.sh", b"ab-ops/switch_model.sh")
    running = [pid for pid, cmd in _cmdlines() if any(part.endswith(d) for part in cmd[:3] for d in drivers)]
    if running:
        return (f"an experiment driver runs (pids {', '.join(map(str, running))}); it starts strata-q3, which a fake "
                "engine would block")
    if _active("strata-ab-rescue.timer"):
        return "strata-ab-rescue.timer is armed: it would start strata-q3, which a fake engine would block"
    try:
        r = subprocess.run(["systemctl", "is-active", "strata-q3.service"], capture_output=True, text=True, timeout=10)
        if r.stdout.strip() in ("activating", "reloading"):
            return "strata-q3 is starting: its preflight would see the fake engines"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def run(script: Path, root: Path, timeout: float) -> dict:
    """The harness's exit status (None past its time), output and error output, the processes it left behind (killed),
    and the directory it ran in (removed).  The output goes to files, not pipes: a process left behind that holds a
    pipe would keep a reader waiting."""
    with tempfile.TemporaryDirectory(prefix="stubs.") as tmp:
        out_path, err_path = Path(tmp) / "harness.out", Path(tmp) / "harness.err"
        with open(out_path, "w") as out, open(err_path, "w") as err:
            p = subprocess.Popen(["bash", str(script), str(root)], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                 env={**os.environ, "TMPDIR": tmp}, start_new_session=True)
            try:
                status = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                status = None
                try:
                    os.killpg(p.pid, signal.SIGTERM)   # bash runs its EXIT trap on SIGTERM: it stops what it started
                    p.wait(timeout=60)
                except (OSError, subprocess.TimeoutExpired):
                    for kill in (lambda: os.killpg(p.pid, signal.SIGKILL), p.kill):
                        try:
                            kill()
                        except OSError:
                            pass
                    p.wait(timeout=60)
        left = sweep(tmp)
        result = {"status": status, "out": out_path.read_text(errors="replace"),
                  "err": err_path.read_text(errors="replace"), "left": left, "dir": tmp}
    return result


def check(test, result: dict, expected: list[str], timeout: float) -> None:
    """The harness ran to its end, left nothing behind, and printed exactly the expected checks, each PASS, in order."""
    lines = result["out"].splitlines()
    tail = "\n" + result["err"][-2000:]
    test.assertIsNotNone(result["status"], f"the harness ran past {timeout} s" + tail)
    test.assertEqual(result["left"], [], "processes outlived the harness")
    test.assertEqual(result["status"], 0, "\n".join(l for l in lines if not l.startswith("PASS ")) + tail)
    test.assertEqual(lines[-1:], ["0 failed"], "the harness did not end with its count of failures" + tail)
    test.assertEqual([l for l in lines[:-1] if not l.startswith("PASS ")], [], "lines other than PASS")
    test.assertEqual([l[len("PASS "):] for l in lines[:-1]], expected,
                     "the checks that passed are not the expected ones (one stopped running, or one is new: list it)")
