"""The scripts a start of strata-q3 runs, run as they are (copies of the files, unchanged) against stubs, so that a
change that breaks them fails here and not at the next start of the service: strata-preflight.sh (ExecStartPre, with
the repo's real configure_strata_q3.py, whose JSON it reads), strata-wait-ready.sh (ExecStartPost) and the watchdog
strata-health.sh (--dry-run, in the states it can be checked in without root).  The preflight checks /dev/nvidia3
itself, so its cases need this machine; the others run anywhere.  The cases that need a path the scripts name without
a setting (the watchdog's counters in /run/strata-q3-health, wait-ready's clock in /proc/uptime) run where a user can
make a private user and mount namespace, in which a file or directory of the test's is bind-mounted over the path or
over /run (bound())."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def stub(d: Path, name: str, body: str) -> None:
    (d / name).write_text("#!/bin/bash\n" + body + "\n")
    (d / name).chmod(0o755)


UNSHARE, MOUNT, STAT = shutil.which("unshare"), shutil.which("mount"), shutil.which("stat")
# In a user and mount namespace of its own (unshare -Urm: root there, with no right over the machine's files), each
# source is bind-mounted over its target, with what is mounted under it (--rbind), and checked to be what the target
# now is; then the command runs as the caller's uid and gid again (a nested user namespace), as it does outside.  The
# mounts are private to the namespace: the real targets are neither read nor written
BIND_SH = r'''mount=$1 stat=$2 unshare=$3 u=$4 g=$5; shift 5
while [ $# -gt 1 ] && [ "$1" != -- ]; do
  "$mount" --rbind "$1" "$2" || exit 97
  [ "$("$stat" -c %d:%i "$1")" = "$("$stat" -c %d:%i "$2")" ] || exit 96
  shift 2
done
[ "${1:-}" = -- ] || exit 95
shift
exec "$unshare" --map-user="$u" --map-group="$g" -- "$@"'''


def bound(binds: list[tuple[Path, str]], argv: list[str], keep: Path | None = None) -> list[str]:
    """argv, to run with each (source, target) of binds: source bind-mounted over target, in a namespace of its own.
    keep, the test's own directory, stays where it is: when it lies under a target (a TMPDIR under /run), it is first
    bound to the same place under that target's source, which the target's mount carries along"""
    pairs = []
    for source, target in binds:
        inside = os.path.relpath(os.path.realpath(keep), os.path.realpath(target)) if keep else os.pardir
        if inside.split(os.sep)[0] not in (os.curdir, os.pardir):
            (Path(source) / inside).mkdir(parents=True, exist_ok=True)
            pairs.append((keep, Path(source) / inside))
        pairs.append((source, target))
    return [UNSHARE, "-Urm", "/bin/sh", "-c", BIND_SH, "sh", MOUNT, STAT, UNSHARE, str(os.getuid()), str(os.getgid()),
            *(str(x) for pair in pairs for x in pair), "--", *argv]


_why_not: dict[str, str] = {}


def namespace_problem(target: str) -> str:
    """Why a file or directory of the test's cannot be bind-mounted over target here ("" when it can): no unshare,
    no target, or a kernel or container that lets no user make a user and mount namespace"""
    if target not in _why_not:
        if not (UNSHARE and MOUNT and STAT):
            _why_not[target] = "machine: no unshare, mount or stat here"
        elif not os.path.exists(target):
            _why_not[target] = f"machine: no {target} here to bind a file over"
        else:
            d = Path(tempfile.mkdtemp())
            try:
                src = d / "src"
                if os.path.isdir(target):
                    src.mkdir()
                else:
                    src.write_text("")
                r = subprocess.run(bound([(src, target)], ["true"], d), capture_output=True, text=True, timeout=30)
            finally:
                shutil.rmtree(d, True)
            _why_not[target] = "" if r.returncode == 0 else \
                f"machine: no private user and mount namespace here (unshare -Urm: {r.stderr.strip()[:200]})"
    return _why_not[target]


GIB_KB, GIB = 1 << 20, 1 << 30            # /proc/meminfo counts kB (KiB), the ARC's kstats count bytes


def meminfo(available_kb: int | None, free_kb: int = GIB_KB) -> str:
    """The first lines of /proc/meminfo, with this MemAvailable (None: none)"""
    lines = ["MemTotal:       65846904 kB", f"MemFree:        {free_kb} kB"]
    if available_kb is not None:
        lines.append(f"MemAvailable:   {available_kb} kB")
    return "\n".join(lines + ["Buffers:            2048 kB", "Cached:         40000000 kB"]) + "\n"


# the ARC's minimum and maximum here (results/zfs-arc-20261025.txt): c_min is MemTotal/32, c_max the 2 GiB cap
C_MIN, C_MAX = 2107100928, 2147483648
# what follows size in arcstats here, in its order: more lines whose names hold "size"
ARC_SIZES = [("compressed_size", 1630339584), ("uncompressed_size", 3313508352), ("overhead_size", 299909632),
             ("hdr_size", 12735360), ("data_size", 1446215680), ("metadata_size", 484033536), ("dbuf_size", 30474240),
             ("dnode_size", 72867328), ("bonus_size", 21099520), ("anon_size", 4198400), ("mru_size", 1199902720),
             ("l2_size", 0), ("l2_asize", 0), ("arc_raw_size", 0), ("abd_chunk_waste_size", 1572864)]


def arcstats(size: int, c_min: int = C_MIN) -> str:
    """/proc/spl/kstat/zfs/arcstats as ZFS writes it: a kstat header, then name, type and data"""
    return ("9 1 0x01 147 39984 32938171391 1053370270553224\nname                            type data\n"
            "hits                            4    3231135125\n"
            f"c_min                           4    {c_min}\nc_max                           4    {C_MAX}\n"
            f"size                            4    {size}\n"
            + "".join(f"{name:<32}4    {value}\n" for name, value in ARC_SIZES))


class MemoryGate(unittest.TestCase):
    def test_a_start_at_the_gate_leaves_the_floor_with_iq3_s(self):
        # what each IQ3_S start took of the memory the preflight saw usable (its "GiB usable" line against MemAvailable
        # after the load, in the switch records), plus decision 0004's 6 GiB floor, must fit under the gate's default.
        # The gate was 36 GiB, set for IQ3_XXS: IQ3_S's starts took up to 35.6 GiB of it.  It must stay below what
        # those starts saw usable, or every start would wait for memory and fail
        took, saw = [], []
        for f in sorted((ROOT / "results").glob("ab3-iq3s-*-switch.txt")):
            text = f.read_text()
            usable = int(re.search(r"ok: 4 GPUs, (\d+) GiB usable", text).group(1))
            after = int(re.search(r"MemAvailable before \d+ MiB, after (\d+) MiB", text).group(1))
            took.append(usable - after / 1024)
            saw.append(usable)
        self.assertEqual(len(took), 3, "the three IQ3_S starts of decision 0004's ab3 runs")
        gate = re.search(r"^MIN_AVAIL_GIB=\$\{STRATA_MIN_AVAIL_GIB:-(\d+)\}$",
                         (ROOT / "scripts/strata-preflight.sh").read_text(), re.M)
        self.assertGreaterEqual(int(gate.group(1)), max(took) + 6, took)
        self.assertLessEqual(int(gate.group(1)), min(saw), saw)


@unittest.skipUnless(os.path.exists("/dev/nvidia3"), "machine: the preflight needs /dev/nvidia3, which it checks itself")
class Preflight(unittest.TestCase):
    def sandbox(self, tune=None, profile: str | None = None) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for f in ("scripts/strata-preflight.sh", "tools/configure_strata_q3.py", "config/profile.conf"):
            (d / "repo" / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, d / "repo" / f)
        if profile is not None:
            (d / "repo/config/profile.conf").write_text(profile)
        cfg = json.loads((ROOT / "config/strata-iq3_xxs.deployed.json").read_text())
        cfg["exe"] = str(d / "strata/engine-cuda12/strata")         # the engine the preflight wants under STRATA_DIR
        if tune:
            tune(cfg)
        (d / "strata").mkdir()
        (d / "strata/strata-iq3_xxs.json").write_text(json.dumps(cfg, indent=4) + "\n")
        (d / "bin").mkdir()
        stub(d / "bin", "ss", 'cat "$STUB/ss.out" 2>/dev/null; exit "$(cat "$STUB/ss.rc" 2>/dev/null || echo 0)"')
        stub(d / "bin", "pgrep", 'exit "$(cat "$STUB/pgrep.rc" 2>/dev/null || echo 1)"')
        # the GPUs that nvidia-smi lists: gpus, if there is one, else 4
        stub(d / "bin", "nvidia-smi", '[ -f "$STUB/gpus" ] && cat "$STUB/gpus" || printf \'0\\n1\\n2\\n3\\n\'')
        # the memory gate reads /proc/meminfo and the ARC's kstats with awk: this awk reads the sandbox's meminfo and
        # arcstats instead, and notes which of the two it was asked for.  sleep returns at once; after its n-th call it
        # puts meminfo.<n> and gpus.<n>, if there are, in place of meminfo and gpus (memory that another job gives back,
        # GPUs that the driver binds), and moves the clock in uptime, if there is one, on by the seconds it was given
        stub(d / "bin", "awk", f'''a=()
for x in "$@"; do
  case $x in
    /proc/meminfo) echo meminfo >> "$STUB/awk.read"; a+=("$STUB/meminfo") ;;
    /proc/spl/kstat/zfs/arcstats) echo arcstats >> "$STUB/awk.read"; a+=("$STUB/arcstats") ;;
    *) a+=("$x") ;;
  esac
done
exec {shutil.which("awk")} "${{a[@]}}"''')
        stub(d / "bin", "sleep", '''echo "$*" >> "$STUB/sleep.calls"
n=$(wc -l < "$STUB/sleep.calls")
[ ! -f "$STUB/meminfo.$n" ] || cat "$STUB/meminfo.$n" > "$STUB/meminfo"
[ ! -f "$STUB/gpus.$n" ] || cat "$STUB/gpus.$n" > "$STUB/gpus"
if [ -f "$STUB/uptime" ]; then
  read -r u _ < "$STUB/uptime"; printf "%s.50 4000.00\\n" "$(( ${u%.*} + $1 ))" > "$STUB/uptime"
fi''')
        (d / "meminfo").write_text(meminfo(50 * GIB_KB))
        (d / "arcstats").write_text(arcstats(3 * GIB))
        return d

    def run_pf(self, d: Path, timeout: int = 120, binds: list[tuple[Path, str]] | None = None,
               **extra: str | None) -> subprocess.CompletedProcess:
        """The preflight of sandbox d; a setting of extra that is None is not set (the script's default).  With
        binds, in a namespace of its own with those files of the test's over the machine's (bound())"""
        env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "STUB": str(d), "STRATA_DIR": str(d / "strata"),
               "STRATA_MIN_AVAIL_GIB": "0", **extra}
        env = {k: v for k, v in env.items() if v is not None}
        argv = ["bash", str(d / "repo/scripts/strata-preflight.sh")]
        return subprocess.run(bound(binds, argv, d) if binds else argv, env=env, capture_output=True, text=True,
                              timeout=timeout)

    def gate(self) -> int:
        """The gate's default, in GiB"""
        return int(re.search(r"^MIN_AVAIL_GIB=\$\{STRATA_MIN_AVAIL_GIB:-(\d+)\}$",
                             (ROOT / "scripts/strata-preflight.sh").read_text(), re.M).group(1))

    def memory(self, d: Path, info: str | None, arc: str | None) -> None:
        """What the sandbox's /proc/meminfo and arcstats hold (None: they cannot be read)"""
        for name, text in (("meminfo", info), ("arcstats", arc)):
            (d / name).unlink()
            if text is not None:
                (d / name).write_text(text)

    def test_the_deployed_config_starts(self):
        # the real tool's JSON through the real preflight: a key the preflight reads, renamed in the tool, fails here
        r = self.run_pf(self.sandbox())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("strata-preflight: ok: 4 GPUs", r.stdout)
        args = " ".join(re.sub(r"#.*", "", (ROOT / "config/profile.conf").read_text()).split())   # as the preflight reads it
        self.assertIn(f"config matches ({args})", r.stdout)

    def test_its_python_reads_nothing_of_the_users_environment(self):
        # the service runs it with HOME set: a .pth in the user's site-packages, or a sitecustomize on PYTHONPATH,
        # would run in the start path of the service
        d = self.sandbox()
        home = d / "home"
        site = home / ".local/lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
        site.mkdir(parents=True)
        (site / "zz.pth").write_text('import os; open(os.path.join(os.environ["STUB"], "user-site-ran"), "w").close()\n')
        (d / "pypath").mkdir()
        (d / "pypath/sitecustomize.py").write_text(
            'import os\nopen(os.path.join(os.environ["STUB"], "pythonpath-ran"), "w").close()\n')
        r = self.run_pf(d, HOME=str(home), PYTHONPATH=str(d / "pypath"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("strata-preflight: ok: 4 GPUs", r.stdout)
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name.endswith("-ran")), [])

    def test_a_critical_difference_is_refused(self):
        def mmap(c):
            c["args"][c["args"].index("--ple-io") + 1] = "mmap"
        r = self.run_pf(self.sandbox(mmap))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("the live config cannot start as config/profile.conf", r.stdout)

    def test_a_tuning_step_only_warns(self):
        def prefill(c):
            c["args"][c["args"].index("--prefill") + 1] = "1024"
        r = self.run_pf(self.sandbox(prefill))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("note: differs from the profile (allowed)", r.stdout)

    def test_another_engine_the_wrong_exe_or_an_empty_profile_is_refused(self):
        d = self.sandbox()
        (d / "pgrep.rc").write_text("0")
        self.assertIn("another Strata engine", self.run_pf(d).stdout)
        d = self.sandbox()
        (d / "ss.out").write_text("LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*\n")
        self.assertIn("a listener on :8080", self.run_pf(d).stdout)
        d = self.sandbox()
        (d / "ss.rc").write_text("1")
        self.assertIn("ss failed", self.run_pf(d).stdout)
        d = self.sandbox(lambda c: c.__setitem__("exe", "/opt/other/engine-cuda12/strata"))
        r = self.run_pf(d)
        self.assertEqual(r.returncode, 1)
        self.assertIn("exe is not the deployed engine", r.stdout)
        r = self.run_pf(self.sandbox(profile="# nothing\n"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("config/profile.conf has no arguments", r.stdout)

    def test_a_tool_whose_json_lacks_a_key_is_refused(self):
        d = self.sandbox()
        tool = d / "repo/tools/configure_strata_q3.py"
        tool.write_text(tool.read_text().replace('"semantic_change"', '"semantic"'))
        r = self.run_pf(d)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("config verdict failed", r.stdout)

    def test_the_gate_counts_memavailable_and_the_arc_above_its_minimum(self):
        # usable: MemAvailable (MemAvailable does not count the ARC on this ZFS) plus what the ARC holds above its
        # minimum, each in whole GiB (rounded down: in GB, either would be 1 more in the first case); a start needs
        # the gate's default.  MemFree (1 GiB here) is not what is available, and the ARC gives back nothing below
        # c_min (not c_max: 2 GiB above c_min is 1 GiB and some above c_max); its size is the line named size
        gate = self.gate()
        for available_kb, size, c_min, usable in (
                ((gate - 7) * GIB_KB + GIB_KB * 9 // 10, 9 * GIB + GIB // 2, C_MIN, gate),     # 35.9 GiB + 7.5 GiB
                ((gate - 2) * GIB_KB, 4 * GIB, C_MIN, gate),
                ((gate - 2) * GIB_KB - 1, 4 * GIB, C_MIN, gate - 1),
                ((gate - 2) * GIB_KB, C_MIN + 2 * GIB, C_MIN, gate),
                ((gate + 1) * GIB_KB, 1 * GIB, C_MIN, gate + 1)):                              # below its minimum: 0
            with self.subTest(available_kb=available_kb, size=size, c_min=c_min):
                d = self.sandbox()
                self.memory(d, meminfo(available_kb), arcstats(size, c_min))
                r = self.run_pf(d, STRATA_MIN_AVAIL_GIB=None, STRATA_MEM_WAIT_S="0")
                if usable >= gate:
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn(f"strata-preflight: ok: 4 GPUs, {usable} GiB usable, config matches", r.stdout)
                else:
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertEqual(r.stdout, f"strata-preflight: only {usable} GiB usable after 0 s (need {gate}); "
                                               "not starting\n")
                self.assertEqual(sorted(set((d / "awk.read").read_text().split())), ["arcstats", "meminfo"],
                                 "the gate did not read both through awk")

    def test_what_the_gate_cannot_read_counts_as_no_memory(self):
        # fail closed: no MemAvailable line, no meminfo or no ARC stats count as nothing usable, not as plenty
        gate = self.gate()
        for what, info, arc, usable in (
                ("no MemAvailable", meminfo(None, free_kb=60 * GIB_KB), arcstats(8 * GIB), 6),
                ("no meminfo", None, arcstats(8 * GIB), 6),
                ("no ARC stats", meminfo((gate - 1) * GIB_KB), None, gate - 1),
                ("neither", None, None, 0)):
            with self.subTest(what):
                d = self.sandbox()
                self.memory(d, info, arc)
                r = self.run_pf(d, STRATA_MIN_AVAIL_GIB=None, STRATA_MEM_WAIT_S="0")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(r.stdout, f"strata-preflight: only {usable} GiB usable after 0 s (need {gate}); "
                                           "not starting\n")

    def test_without_the_memory_it_waits_25_minutes_then_refuses(self):
        # it waits in steps of 15 s, says so every 5 minutes, and gives up after 25 minutes (failing at once would use
        # up the unit's start limit); it does not start then
        gate = self.gate()
        d = self.sandbox()
        self.memory(d, meminfo(10 * GIB_KB), arcstats(2 * GIB))
        r = self.run_pf(d, STRATA_MIN_AVAIL_GIB=None, STRATA_MEM_WAIT_S=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((d / "sleep.calls").read_text().split(), ["15"] * 100)
        self.assertEqual(r.stdout.splitlines(),
                         [f"strata-preflight: waiting for memory: 10 GiB usable, need {gate} (the PLE table is locked)"] * 5
                         + [f"strata-preflight: only 10 GiB usable after 1500 s (need {gate}); not starting"])

    def test_it_starts_once_the_memory_is_there(self):
        # another job gives its memory back during the wait: the preflight reads the memory again each time
        gate = self.gate()
        d = self.sandbox()
        self.memory(d, meminfo(10 * GIB_KB), arcstats(2 * GIB))
        (d / "meminfo.3").write_text(meminfo((gate + 5) * GIB_KB))        # after the third wait of 15 s
        r = self.run_pf(d, STRATA_MIN_AVAIL_GIB=None, STRATA_MEM_WAIT_S=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((d / "sleep.calls").read_text().split(), ["15"] * 3)
        self.assertEqual(r.stdout.count("strata-preflight: waiting for memory: "), 1, r.stdout)
        self.assertIn(f"strata-preflight: ok: 4 GPUs, {gate + 5} GiB usable, config matches", r.stdout)

    def test_a_gate_or_wait_that_is_no_whole_number_is_refused(self):
        # with a gate of "4x", the loop's test [ n -lt 4x ] would be an error, which ends the wait as if there were
        # memory enough; with a wait of "x", the test [ waited -ge x ] would be an error each time, and the wait would
        # never end (30 s here)
        for var, value in (("STRATA_MIN_AVAIL_GIB", "4x"), ("STRATA_MIN_AVAIL_GIB", "-1"), ("STRATA_MEM_WAIT_S", "1.5"),
                           ("STRATA_MEM_WAIT_S", "x")):
            with self.subTest(var=var, value=value):
                d = self.sandbox()
                self.memory(d, meminfo(10 * GIB_KB), arcstats(2 * GIB))
                r = self.run_pf(d, 30, **{"STRATA_MIN_AVAIL_GIB": None, "STRATA_MEM_WAIT_S": None, var: value})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(r.stdout,
                                 "strata-preflight: STRATA_MIN_AVAIL_GIB and STRATA_MEM_WAIT_S must be whole numbers\n")

    def test_it_waits_120_s_for_the_four_gpus(self):
        # the unit can be dispatched before the driver has bound the GPUs at boot: it waits up to 120 s, in steps of
        # 5 s, for nvidia-smi to list four.  Its clock is /proc/uptime, read with the shell's read: a file of the
        # test's stands in for it (bound()), and sleep moves it on
        why = namespace_problem("/proc/uptime")
        if why:
            self.skipTest(why)
        for what, more, says in (("three, then still three", None, None),
                                 ("three, then four after the 2nd wait", 2, "strata-preflight: ok: 4 GPUs")):
            with self.subTest(what):
                d = self.sandbox()
                (d / "uptime").write_text("1000.50 4000.00\n")
                (d / "gpus").write_text("0\n1\n2\n")
                if more:
                    (d / f"gpus.{more}").write_text("0\n1\n2\n3\n")
                r = self.run_pf(d, binds=[(d / "uptime", "/proc/uptime")])
                if says:
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn(says, r.stdout)
                    self.assertEqual((d / "sleep.calls").read_text().split(), ["5"] * more)
                else:
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertEqual(r.stdout, "strata-preflight: only 3 GPUs (or no /dev/nvidia3) after 120 s; "
                                               "not starting\n")
                    self.assertEqual((d / "sleep.calls").read_text().split(), ["5"] * 24)

    def test_the_arc_note_reads_the_value_from_the_unit_s_execstart_lines(self):
        # the cap only warns.  The value is what an ExecStart line of the installed zfs-arc-max.service writes, as
        # systemd reads the unit: not a comment (# or ;), nor a commented ExecStart, nor an ExecStartPre.  The unit
        # and zfs_arc_max are the test's files over the machine's, in a namespace (bound())
        unit, param = "/etc/systemd/system/zfs-arc-max.service", "/sys/module/zfs/parameters/zfs_arc_max"
        for path in (unit, param):
            why = namespace_problem(path)
            if why:
                self.skipTest(why)
        echo = "/bin/sh -ec 'echo {} > /sys/module/zfs/parameters/zfs_arc_max'"
        note = "strata-preflight: note: zfs_arc_max is {}, zfs-arc-max.service writes {} (did it run?)"
        for what, lines, have, says in (
                ("comments", ["# was: echo 8589934592 > /sys/module/zfs/parameters/zfs_arc_max",
                              "#ExecStart=" + echo.format(1), "; ExecStart=" + echo.format(2),
                              "ExecStartPre=" + echo.format(3), "ExecStart=" + echo.format(C_MAX)], C_MAX, None),
                ("blanks around the key", ["  ExecStart = " + echo.format(C_MAX)], C_MAX, None),
                ("another value", ["ExecStart=" + echo.format(C_MAX)], 8589934592, note.format(8589934592, C_MAX)),
                ("only in a comment", ["# ExecStart=" + echo.format(C_MAX)], C_MAX, note.format(C_MAX, "?"))):
            with self.subTest(what):
                d = self.sandbox()
                (d / "unit").write_text("[Service]\nType=oneshot\n" + "\n".join(lines) + "\n")
                (d / "param").write_text(f"{have}\n")
                r = self.run_pf(d, binds=[(d / "unit", unit), (d / "param", param)])
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("strata-preflight: ok: 4 GPUs", r.stdout)
                self.assertEqual([x for x in r.stdout.splitlines() if "zfs_arc_max" in x], [says] if says else [])


def answering(t: unittest.TestCase, answers: list[bytes]) -> str:
    """An HTTP server on a free port of 127.0.0.1 that answers every GET with 200 and these bodies, one per request,
    the last one again and again; its URL."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = answers.pop(0) if len(answers) > 1 else answers[0]
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t.addCleanup(srv.server_close)
    t.addCleanup(srv.shutdown)                          # runs first
    return f"http://127.0.0.1:{srv.server_address[1]}"


def recording(t: unittest.TestCase, body: bytes, status: int = 200) -> tuple[str, list]:
    """An HTTP server on a free port of 127.0.0.1 that answers every GET with status and body, and records the path
    and the Authorization header of each; its URL, and the list."""
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t.addCleanup(srv.server_close)
    t.addCleanup(srv.shutdown)                          # runs first
    return f"http://127.0.0.1:{srv.server_address[1]}", seen


# curl takes a proxy for http:// from these, also for 127.0.0.1, unless --noproxy says otherwise (not from HTTP_PROXY)
PROXY_VARS = ("http_proxy", "all_proxy", "ALL_PROXY")


def curlrc(t: unittest.TestCase) -> str:
    """A HOME whose .curlrc sends every answer to /dev/null: curl reads it unless -q is its first option."""
    home = Path(tempfile.mkdtemp())
    t.addCleanup(shutil.rmtree, home, True)
    (home / ".curlrc").write_text("output = /dev/null\n")
    return str(home)


class WaitReady(unittest.TestCase):
    def server(self, answers: list[bytes]) -> str:
        return answering(self, answers)

    def run_wr(self, url: str, mainpid: str | None = None, timeout: int = 60, **extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "STRATA_URL": url, **extra}
        if mainpid:
            env["MAINPID"] = mainpid
        return subprocess.run(["bash", str(ROOT / "scripts/strata-wait-ready.sh")], env=env, capture_output=True,
                              text=True, timeout=timeout)

    def test_ready_once_the_model_is_loaded(self):
        # server.py's /health as json.dumps writes it
        loading = json.dumps({"status": "ok", "loaded": False}).encode()
        loaded = json.dumps({"status": "ok", "loaded": True, "model": "x"}).encode()
        r = self.run_wr(self.server([loading, loading, loaded]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"strata-wait-ready: loaded after [0-9]+ s")

    def test_its_curl_reads_no_curlrc(self):
        # the service runs it with the user's HOME: an "output" in ~/.curlrc would hide every answer until it gives up
        loaded = json.dumps({"status": "ok", "loaded": True}).encode()
        r = self.run_wr(self.server([loaded]), timeout=20, HOME=curlrc(self))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_its_curl_takes_no_proxy(self):
        # a proxy that the environment names would answer for the server: here it says "loaded" for a model that is not
        loading = json.dumps({"status": "ok", "loaded": False}).encode()
        loaded = json.dumps({"status": "ok", "loaded": True}).encode()
        for var in PROXY_VARS:
            with self.subTest(var=var):
                proxy, proxied = recording(self, loaded)
                url, seen = recording(self, loaded)
                r = self.run_wr(url, timeout=20, **{var: proxy})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(proxied, [], f"the proxy of {var} answered for the server")
                self.assertEqual([p for p, _ in seen], ["/health"])
        # and with a server that is still loading, a proxy that says "loaded" does not end the wait
        proxy, proxied = recording(self, loaded)
        url, seen = recording(self, loading)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_wr(url, timeout=5, http_proxy=proxy)
        self.assertEqual(proxied, [])

    def test_fails_when_server_py_exits(self):
        p = subprocess.Popen(["true"])
        p.wait()                                    # a pid that no longer runs
        r = self.run_wr(self.server([json.dumps({"loaded": False}).encode()]), mainpid=str(p.pid))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("strata-wait-ready: server.py exited", r.stdout)

    def test_a_poll_gives_up_on_a_server_that_never_answers(self):
        # each poll waits at most 5 s (-m 5): a listener that accepts and never answers would hold the wait, which then
        # could not see that server.py has exited
        s = socket.socket()
        self.addCleanup(s.close)                    # also ends a curl still connected
        s.bind(("127.0.0.1", 0))
        s.listen(8)                                 # the kernel accepts; nothing reads or answers
        p = subprocess.Popen(["true"])
        p.wait()                                    # a pid that no longer runs
        t0 = time.monotonic()
        r = self.run_wr(f"http://127.0.0.1:{s.getsockname()[1]}", mainpid=str(p.pid), timeout=20)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "strata-wait-ready: server.py exited\n")
        self.assertGreaterEqual(time.monotonic() - t0, 5)

    def test_fails_after_30_minutes_without_a_loaded_model(self):
        # exit 0 would make the unit active without a model.  Its clock is /proc/uptime, which it reads with the
        # shell's read: a file of the test's stands in for it (bound()), and a sleep that moves that clock on by step
        # seconds stands in for the 30 minutes.  A poll at 1799 s is the last
        why = namespace_problem("/proc/uptime")
        if why:
            self.skipTest(why)
        loading = json.dumps({"status": "ok", "loaded": False}).encode()
        for step, polls in ((600, 3), (1799, 2)):
            with self.subTest(step=step):
                d = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, d, True)
                (d / "uptime").write_text("1000.25 4000.00\n")
                stub(d, "sleep", f'''echo "$*" >> "{d}/sleep.calls"
read -r u _ < "{d}/uptime"
printf "%s.25 4000.00\\n" "$(( ${{u%.*}} + {step} ))" > "{d}/uptime"''')
                url, seen = recording(self, loading)
                env = {k: v for k, v in os.environ.items() if k != "MAINPID"}
                env.update(PATH=f"{d}:{os.environ['PATH']}", STRATA_URL=url)
                r = subprocess.run(bound([(d / "uptime", "/proc/uptime")],
                                         ["bash", str(ROOT / "scripts/strata-wait-ready.sh")], d),
                                   env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(r.stdout, "strata-wait-ready: not loaded after 30 min\n")
                self.assertEqual(seen, [("/health", None)] * polls)
                self.assertEqual((d / "sleep.calls").read_text().split(), ["2"] * polls)


# the watchdog's counters are in /run/strata-q3-health, root's, 0700, which the script names without a setting and
# makes at its first run (10 min after a boot): a test binds a directory of its own over /run, which holds them
LOADED = json.dumps({"object": "list", "data": [{"id": "x", "status": "loaded"}]}).encode()
UNLOADED = json.dumps({"object": "list", "data": [{"id": "x", "status": "unloaded"}]}).encode()
# what a dry run of a healthy check says: an answer clears the count of checks without one; then a running engine, or a
# model unloaded on purpose, clears the count of checks without an engine and the attempts to start a failed service
HEALTHY = ("strata-health: (dry run) would clear http_failures\n"
           "strata-health: (dry run) would clear engine_failures failed_retries\n")


def timestamp(ago: float) -> str:
    """A time ago seconds before now, as systemctl show writes it"""
    return time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(time.time() - ago))


class WatchdogDryRun(unittest.TestCase):
    def run_wd(self, state: str, inactive_enter: str = "", url: str = "http://127.0.0.1:9", mainpid: str = "4242",
               engine: str = "", counters: dict[str, str] | None = None, **extra: str) -> subprocess.CompletedProcess:
        """A dry run against stand-ins: systemctl says state, inactive_enter and mainpid of test-strata.service, and
        pgrep finds engine (a pid, or nothing) under mainpid.  The account that runs the tests cannot read root's
        counters in /run/strata-q3-health, or there are none: each counts as 0.  With counters, the dry run runs in a
        namespace of its own (bound()) whose /run is a directory of the test's, and /run/strata-q3-health in it holds
        them; self.counters_after is what that directory holds after the dry run"""
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        stub(d, "systemctl", f'''case "$*" in
  "show -p ActiveState --value test-strata.service") echo {state} ;;
  "show -p InactiveEnterTimestamp --value test-strata.service") echo "{inactive_enter}" ;;
  "show -p MainPID --value test-strata.service") echo {mainpid} ;;
  *) echo "systemctl stub: $*" >&2; exit 1 ;;
esac''')
        # the engine: only a child of the unit's MainPID whose command line has the engine's pattern
        stub(d, "pgrep", f'''printf "%s\\n" "$*" >> "{d}/pgrep.calls"
[ -n "{engine}" ] && [ "$*" = "-P {mainpid} -f [e]ngine-cuda12/strata --serve" ] && {{ echo {engine}; exit 0; }}
exit 1''')
        for tool in ("mkdir", "chmod", "mktemp"):       # what it does to files: recorded, then done
            stub(d, tool, f'echo "{tool} $*" >> "{d}/calls"; exec /usr/bin/{tool} "$@"')
        # curl's arguments, one per line and "--end--" after each call; for each header file (-H @file) its mode, its
        # directory's mode and its size; then the real curl
        stub(d, "curl", f'''printf "%s\\n" "$@" --end-- >> "{d}/curl.args"
p=
for a in "$@"; do
  case "$p:$a" in
    -H:@*) f=${{a#@}}; echo "$(stat -c %a "$f") $(stat -c %a "${{f%/*}}") $(stat -c %s "$f")" >> "{d}/curl.hdr" ;;
  esac
  p=$a
done
exec {shutil.which("curl")} "$@"''')
        env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "STRATA_UNIT": "test-strata.service",
               "STRATA_URL": url, "STRATA_CFG": str(d / "none.json"), "TMPDIR": str(d), **extra}
        argv = ["bash", str(ROOT / "scripts/strata-health.sh"), "--dry-run"]
        if counters is not None:
            run = d / "run" / "strata-q3-health"
            run.mkdir(parents=True, mode=0o700)
            for name, text in counters.items():
                (run / name).write_text(text)
            argv = bound([(d / "run", "/run")], argv, d)
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        if counters is not None:
            self.counters_after = {p.name: p.read_text() for p in run.iterdir()}
        # its TMPDIR is d: what it made there (mktemp -d, with the header file that holds the key) is gone
        mine = {"systemctl", "pgrep", "mkdir", "chmod", "mktemp", "curl", "calls", "curl.args", "curl.hdr",
                "pgrep.calls"} | ({"run"} if counters is not None else set())
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name not in mine), [],
                         "the dry run left its directory")

        def lines(name: str) -> list[str]:
            return (d / name).read_text().splitlines() if (d / name).exists() else []
        self.calls = "\n".join(lines("calls"))
        self.curl_args = lines("curl.args")
        self.curl_hdr = lines("curl.hdr")
        self.pgrep_calls = lines("pgrep.calls")
        return r

    def need_namespace(self) -> None:
        why = namespace_problem("/run")
        if why:
            self.skipTest(why)

    def test_its_cases_in_a_namespace_run_with_a_temporary_directory_under_run(self):
        # a caller's TMPDIR under /run (/run/user/1003, say), over which these cases bind a directory: they run, they
        # do not skip (scripts/test.sh fails a run with a machine: skip, and deploy.sh refuses then).  Here in a
        # namespace whose /run is the test's, so that nothing is written in the real one; also through a symlink
        # (/var/run is one to /run)
        self.need_namespace()
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "run/tmp").mkdir(parents=True)
        cases = ["tests.test_start_path.WatchdogDryRun.test_without_an_answer_the_3rd_check_restarts_it",
                 "tests.test_start_path.WatchdogDryRun.test_a_dry_run_reads_the_counters_and_writes_none",
                 "tests.test_start_path.WaitReady.test_fails_after_30_minutes_without_a_loaded_model"]
        for tmp in ["/run/tmp"] + (["/var/run/tmp"] if os.path.realpath("/var/run") == "/run" else []):
            with self.subTest(TMPDIR=tmp):
                r = subprocess.run(bound([(d / "run", "/run")], [sys.executable, "-B", "-m", "unittest", *cases], d),
                                   cwd=ROOT, env={**os.environ, "TMPDIR": tmp}, capture_output=True, text=True,
                                   timeout=300)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertRegex(r.stderr, r"\nRan 3 tests in [0-9.]+s\n\nOK\n\Z")      # not "OK (skipped=...)"
                self.assertEqual(list((d / "run/tmp").iterdir()), [])

    def test_a_dry_run_changes_nothing_in_its_directory(self):
        # before, it made /run/strata-q3-health and changed its mode before it read --dry-run, and wrote its
        # temporary files (the key's header) there
        for state in ("active", "failed", "inactive"):
            with self.subTest(state=state):
                r = self.run_wd(state)
                self.assertNotIn("/run/strata-q3-health", self.calls, r.stdout + r.stderr)

    def test_its_curl_reads_no_curlrc(self):
        # a model unloaded on purpose is healthy; an "output" in a .curlrc would hide the answer that says so, and the
        # watchdog would ask server.py to load the model
        url = answering(self, [UNLOADED])
        r = self.run_wd("active", url=url, HOME=curlrc(self))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, HEALTHY)

    def test_no_proxy_gets_the_key(self):
        # a dry run that a person starts reads the live config's key and sends it: a proxy that the person's
        # environment names must not get it
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "cfg.json"
        cfg.write_text(json.dumps({"api_key": "k-not-for-a-proxy"}))
        for var in PROXY_VARS:
            with self.subTest(var=var):
                proxy, proxied = recording(self, UNLOADED)
                url, seen = recording(self, UNLOADED)
                r = self.run_wd("active", url=url, STRATA_CFG=str(cfg), **{var: proxy})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(proxied, [], f"the proxy of {var} got the request")
                self.assertEqual(seen, [("/v1/models", "Bearer k-not-for-a-proxy")])
                self.assertEqual(r.stdout, HEALTHY)

    def test_the_key_stays_off_curl_s_command_line(self):
        # it runs as root every 2 minutes: a key on curl's command line would be readable by every account in /proc.
        # The key goes in a header file instead (-H @file): 0600, in the dry run's own 0700 directory
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "cfg.json"
        cfg.write_text(json.dumps({"api_key": "k-off-the-command-line"}))
        url, seen = recording(self, UNLOADED)
        r = self.run_wd("active", url=url, STRATA_CFG=str(cfg))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(seen, [("/v1/models", "Bearer k-off-the-command-line")])
        self.assertIn("--end--", self.curl_args, "curl did not run through the stand-in")
        self.assertFalse([a for a in self.curl_args if "k-off-the-command-line" in a], self.curl_args)
        self.assertTrue([a for a in self.curl_args if a.startswith("@")], "no -H @file")
        self.assertEqual(self.curl_hdr, [f"600 700 {len('Authorization: Bearer k-off-the-command-line') + 1}"])

    def test_a_key_with_a_control_character_is_not_sent(self):
        # nor written to the header file, which curl reads line by line: what follows a newline in the key would go
        # as a header of its own
        for ch in ("\n", "\r", "\t", "\x1b", "\x7f"):
            for key in (f"k-one{ch}k-two", [f"k-one{ch}k-two", "k-three"]):
                with self.subTest(key=key):
                    d = Path(tempfile.mkdtemp())
                    self.addCleanup(shutil.rmtree, d, True)
                    cfg = d / "cfg.json"
                    cfg.write_text(json.dumps({"api_key": key}))
                    url, seen = recording(self, UNLOADED)
                    r = self.run_wd("active", url=url, STRATA_CFG=str(cfg))
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertEqual(seen, [("/v1/models", None)])
                    self.assertEqual(self.curl_hdr, ["600 700 0"], "the header file is not empty")

    def test_the_reload_it_would_ask_for_takes_no_proxy_and_its_key_from_the_header_file(self):
        # with the server up, no engine and a model that is not unloaded on purpose, it asks server.py to load the
        # model (POST /v1/load); a dry run prints that request.  It takes no proxy either, and the key goes in the
        # header file that /v1/models was sent with (-H @file), not on the command line.  One check without an engine
        # does not restart the service; the dry run says what count it would set
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "cfg.json"
        cfg.write_text(json.dumps({"api_key": "k-not-on-the-reload-s-command-line"}))
        url, seen = recording(self, LOADED)
        r = self.run_wd("active", url=url, STRATA_CFG=str(cfg))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(seen, [("/v1/models", "Bearer k-not-on-the-reload-s-command-line")])
        reload = re.findall(r"^strata-health: \(dry run\) would run: curl -q --noproxy \* -s -o /dev/null -m 3 -X POST "
                            r"-H Content-Type: application/json -H @(\S+) -d \{\} " + re.escape(url) + "/v1/load$",
                            r.stdout, re.M)
        self.assertEqual(len(reload), 1, r.stdout)
        self.assertIn("@" + reload[0], self.curl_args, "not the header file that /v1/models was sent with")
        self.assertNotIn("k-not-on-the-reload-s-command-line", r.stdout + r.stderr)
        self.assertEqual(re.sub(r" -H @\S+ ", " -H @<file> ", r.stdout).splitlines(), [
            "strata-health: (dry run) would clear http_failures",
            "strata-health [warning]: no engine process under server.py (check 1/8): asking server.py to load it "
            "(POST /v1/load)",
            "strata-health: (dry run) would run: curl -q --noproxy * -s -o /dev/null -m 3 -X POST "
            f"-H Content-Type: application/json -H @<file> -d {{}} {url}/v1/load",
            "strata-health: (dry run) would set engine_failures to 1"])

    def test_a_running_engine_is_healthy(self):
        # an engine under server.py serves (or reloads): healthy, whatever /v1/models says of the model.  Not seeing it
        # would ask server.py to load the model, and restart the service after 8 checks
        url, seen = recording(self, LOADED)
        r = self.run_wd("active", url=url, mainpid="4242", engine="4243")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, HEALTHY)
        self.assertEqual(self.pgrep_calls, ["-P 4242 -f [e]ngine-cuda12/strata --serve"])
        self.assertEqual(seen, [("/v1/models", None)])

    def test_a_401_or_403_is_healthy_and_the_engine_is_not_checked(self):
        # the server is up, but the check cannot read its key: healthy, with a warning.  Counted as no answer, it would
        # restart a healthy service after 3 checks
        for code in (401, 403):
            with self.subTest(code=code):
                url, seen = recording(self, b'{"error": "unauthorized"}', status=code)
                r = self.run_wd("active", url=url)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(r.stdout, f"strata-health [warning]: /v1/models answered {code}: the server is up but "
                                           "the check cannot read its key; counted healthy, engine not checked\n"
                                           "strata-health: (dry run) would clear http_failures engine_failures "
                                           "failed_retries\n")
                self.assertEqual(seen, [("/v1/models", None)])

    def test_one_check_without_an_answer_does_not_restart_it(self):
        # nothing listens on port 9: no answer (code 000).  The 3rd check in a row restarts the service; the first,
        # with no counter that the account can read, does not
        r = self.run_wd("active")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "strata-health [warning]: the HTTP server does not answer (code 000, check 1/3)\n"
                                   "strata-health: (dry run) would set http_failures to 1\n")

    def test_states_it_leaves_alone(self):
        # it runs nothing; the counts of the checks start again from 0
        for state in ("activating", "deactivating", "inactive"):
            with self.subTest(state=state):
                r = self.run_wd(state)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(r.stdout, "strata-health: (dry run) would clear http_failures engine_failures\n")

    def test_a_failed_service_is_started_after_its_cool_down(self):
        # with no counter that the account can read, the first attempt: it counts the attempt, which doubles the next
        # cool-down
        r = self.run_wd("failed", timestamp(3600))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertRegex(lines[0], r": reset-failed and start again \(automatic attempt 1\)$")
        self.assertEqual(lines[1:], ["strata-health: (dry run) would set failed_retries to 1",
                                     "strata-health: (dry run) would run: systemctl reset-failed test-strata.service",
                                     "strata-health: (dry run) would run: systemctl start --no-block test-strata.service"])
        r = self.run_wd("failed", timestamp(60))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("would", r.stdout)
        self.assertIn("next automatic attempt after 1800 s", r.stdout)

    def test_without_an_engine_the_8th_check_restarts_it(self):
        # each check without an engine asks server.py to load the model; the 8th in a row (16 min) restarts the service
        # with try-restart, which never starts a unit that an admin is stopping.  A counter that is no number counts 0
        self.need_namespace()
        url, seen = recording(self, LOADED)
        for before, n in (("", 1), ("x7\n", 1), ("-3\n", 1), ("6\n", 7), ("7\n", 8), ("12\n", 13)):
            with self.subTest(engine_failures=before):
                counters = {"engine_failures": before, "http_failures": "2\n"}
                r = self.run_wd("active", url=url, counters=counters)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(re.sub(r" -H @\S+ ", " -H @<file> ", r.stdout).splitlines(), [
                    "strata-health: (dry run) would clear http_failures",
                    f"strata-health [warning]: no engine process under server.py (check {n}/8): asking server.py to "
                    "load it (POST /v1/load)",
                    "strata-health: (dry run) would run: curl -q --noproxy * -s -o /dev/null -m 3 -X POST "
                    f"-H Content-Type: application/json -H @<file> -d {{}} {url}/v1/load",
                    f"strata-health: (dry run) would set engine_failures to {n}"] + ([
                        "strata-health: (dry run) would clear http_failures engine_failures",
                        f"strata-health [err]: restarting test-strata.service after {n} failed checks",
                        "strata-health: (dry run) would run: systemctl try-restart --no-block test-strata.service"]
                        if n >= 8 else []))
                self.assertEqual(self.counters_after, counters)

    def test_without_an_answer_the_3rd_check_restarts_it(self):
        # nothing listens on port 9: no answer (code 000).  The 3rd check in a row (6 min) restarts the service
        self.need_namespace()
        for before, n in (("", 1), ("1\n", 2), ("2\n", 3), ("5\n", 6)):
            with self.subTest(http_failures=before):
                counters = {"http_failures": before, "engine_failures": "7\n"}
                r = self.run_wd("active", counters=counters)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(r.stdout.splitlines(), [
                    f"strata-health [warning]: the HTTP server does not answer (code 000, check {n}/3)",
                    f"strata-health: (dry run) would set http_failures to {n}"] + ([
                        "strata-health: (dry run) would clear http_failures engine_failures",
                        f"strata-health [err]: restarting test-strata.service after {n} failed checks",
                        "strata-health: (dry run) would run: systemctl try-restart --no-block test-strata.service"]
                        if n >= 3 else []))
                self.assertEqual(self.counters_after, counters)

    def test_a_dry_run_reads_the_counters_and_writes_none(self):
        # each path that would set or clear a counter, with counters one check short of a restart: a dry run says
        # what it would set and clear, and sets and clears nothing
        self.need_namespace()
        counters = {"http_failures": "2\n", "engine_failures": "7\n", "failed_retries": "1\n"}
        for what, kw, says in (
                ("failed, cooled down", {"state": "failed", "inactive_enter": timestamp(3 * 3600)},
                 r"\(automatic attempt 2\)\n.*would set failed_retries to 2\n.*would run: systemctl reset-failed .*\n"
                 r".*would run: systemctl start "),
                ("failed, cooling down", {"state": "failed", "inactive_enter": timestamp(60)},
                 r"\A[^\n]*next automatic attempt after 3600 s\n\Z"),
                ("inactive", {"state": "inactive"}, r"\A.*would clear http_failures engine_failures\n\Z"),
                ("401", {"state": "active", "url": recording(self, b"{}", status=401)[0]},
                 r"\A[^\n]*answered 401: [^\n]*\n.*would clear http_failures engine_failures failed_retries\n\Z"),
                ("unloaded", {"state": "active", "url": recording(self, UNLOADED)[0]}, rf"\A{re.escape(HEALTHY)}\Z"),
                ("engine", {"state": "active", "url": recording(self, LOADED)[0], "engine": "4243"},
                 rf"\A{re.escape(HEALTHY)}\Z"),
                ("no engine", {"state": "active", "url": recording(self, LOADED)[0]},
                 r"\A.*would clear http_failures\n.*\(check 8/8\).*\n.*/v1/load\n.*would set engine_failures to 8\n"
                 r".*would clear http_failures engine_failures\n.*after 8 failed checks\n"
                 r".*would run: systemctl try-restart "),
                ("no answer", {"state": "active"},
                 r"\(code 000, check 3/3\)\n.*would set http_failures to 3\n"
                 r".*would clear http_failures engine_failures\n.*after 3 failed checks\n"
                 r".*would run: systemctl try-restart ")):
            with self.subTest(what):
                r = self.run_wd(counters=counters, **kw)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertRegex(r.stdout, says)
                self.assertEqual(self.counters_after, counters)

    def test_the_cool_down_doubles_per_attempt_up_to_8_h(self):
        # a failed service: started again 30 min after it failed, then 1 h, 2 h, 4 h and 8 h after each attempt, which
        # it counts (failed_retries, which the dry run says it would set).  The shift is capped: a large count would
        # overflow it, and a cool-down of 0 or less starts at once
        self.need_namespace()
        for tries, wait in (("", 1800), ("1\n", 3600), ("2\n", 7200), ("3\n", 14400), ("4\n", 28800), ("5\n", 28800),
                            ("60\n", 28800), ("63\n", 28800)):
            for ago, starts in ((wait - 300, False), (wait + 300, True)):
                with self.subTest(failed_retries=tries, ago=ago):
                    r = self.run_wd("failed", timestamp(ago), counters={"failed_retries": tries})
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    if starts:
                        attempt = int(tries or 0) + 1
                        lines = r.stdout.splitlines()
                        self.assertRegex(lines[0], r"^strata-health \[err\]: test-strata\.service has been failed for "
                                                   r"\d+ s: reset-failed and start again "
                                                   rf"\(automatic attempt {attempt}\)$")
                        self.assertEqual(lines[1:], [
                            f"strata-health: (dry run) would set failed_retries to {attempt}",
                            "strata-health: (dry run) would run: systemctl reset-failed test-strata.service",
                            "strata-health: (dry run) would run: systemctl start --no-block test-strata.service"])
                    else:
                        self.assertRegex(r.stdout, r"\Astrata-health \[warning\]: test-strata\.service is failed "
                                                   rf"\(\d+ s\); next automatic attempt after {wait} s\n\Z")


if __name__ == "__main__":
    unittest.main()
