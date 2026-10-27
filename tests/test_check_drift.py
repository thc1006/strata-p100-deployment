"""scripts/check-drift.sh on a sandbox of its own: the repo's files that the check reads (its units point at the
sandbox: the checkout they run and the Strata tree, the user, the limits; and they give no mount namespace, which the
fake server.py runs without), a ~/dev tree with a Strata checkout (a git repository at the pinned commit) and its venv,
the system's files under DRIFT_SYSROOT (the cgroups' memory limits and the service user's site-packages too),
systemctl as a stub that shows what the repo's units and drop-ins say, and, for the running service, the Strata
tree's serve/server.py: a fake one, started as its unit's ExecStart says, that starts fake engines as server.py does
(a compiled program that only waits, at .../engine-cuda12/strata-fake: neither strata-preflight.sh nor an experiment
driver takes it for an engine), and holds the socket that the test's HTTP server listens on; the engine maps a shared
library outside ~/dev that the list names (an ELF file, as the check reads its SONAME).
Each case changes one thing and must give DRIFT (or the note it names); the sandbox unchanged must give "no drift".  The
sandboxes of a test's cases are made and checked at the same time, in threads that only start subprocesses; the
assertions are made in the test's own thread, in the order of the cases.
ModelOfServerPy compares the check's model of server.py with server.py's own functions, on the machine."""
from __future__ import annotations

import ast
import atexit
import base64
import concurrent.futures
import contextlib
import copy
import datetime
import grp
import hashlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import locale
import marshal
import os
from pathlib import Path
import platform
import pwd
import py_compile
import re
import resource
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ("strata-q3.service", "strata-q3-health.service", "strata-q3-health.timer", "zfs-arc-max.service")
LIVE = "/home/thc1006/dev/p100-q3-deployment"       # what the repo's units name
STRATA = "/home/thc1006/dev/strata"
MANAGER_PATH = "/usr/local/bin:/usr/bin:/bin"       # root's directories, on any Ubuntu
TAG = sys.implementation.cache_tag
# the engine: waits (at most 10 minutes, so that a run stopped before its clean-up leaves nothing for long), and maps
# the files maps.txt beside it names
WAIT_C = r"""#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
int main(void) {
    char p[PATH_MAX], line[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", p, sizeof p - 16);
    if (n > 0) {
        p[n] = 0;
        char *s = strrchr(p, '/');
        if (s) {
            strcpy(s + 1, "maps.txt");
            FILE *f = fopen(p, "r");
            while (f && fgets(line, sizeof line, f)) {
                line[strcspn(line, "\n")] = 0;
                int fd = open(line, O_RDONLY);
                if (fd >= 0) mmap(0, 4096, PROT_READ, MAP_PRIVATE, fd, 0);
            }
        }
    }
    alarm(600);
    for (;;) pause();
}
"""
_built: dict[str, Path] = {}
_building = threading.Lock()
# the sandboxes of independent cases are made and checked at the same time, in threads that only start subprocesses
# and wait for them; the assertions stay in the test's own thread
WORKERS = max(1, min(6, (os.cpu_count() or 2) - 2))


# what the check says when a function from the environment or a BASH_ENV file is left after it removed them
LEFT = ("DRIFT  functions from the environment or a BASH_ENV file are left after their removal (one is readonly, or "
        "has the name of a builtin that removes them): nothing checked")


def together(fn, items: list) -> list:
    """fn(item) for each item, at the same time; the results in the order of the items (the first exception raised)."""
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        return list(ex.map(fn, items))


def engine_program() -> Path:
    """WAIT_C, compiled once for all the sandboxes."""
    with _building:
        if "exe" not in _built:
            d = Path(tempfile.mkdtemp())
            atexit.register(shutil.rmtree, d, True)
            (d / "wait.c").write_text(WAIT_C)
            subprocess.run(["gcc", "-O2", "-o", str(d / "wait"), str(d / "wait.c")], check=True)
            _built["exe"] = d / "wait"
        return _built["exe"]


def shared_library(soname: str | None, flags: tuple = ()) -> Path:
    """A shared library (ELF) with that SONAME (None: without one) and the linker's flags, compiled once for all the
    sandboxes."""
    with _building:
        if f"lib {soname} {flags}" not in _built:
            d = Path(tempfile.mkdtemp())
            atexit.register(shutil.rmtree, d, True)
            (d / "lib.c").write_text("int strata_drift_test(void) { return 1; }\n")
            subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(d / "lib.so"), str(d / "lib.c"), *flags]
                           + ([f"-Wl,-soname,{soname}"] if soname else []), check=True)
            _built[f"lib {soname} {flags}"] = d / "lib.so"
        return _built[f"lib {soname} {flags}"]


def patched_library(src: Path, dst: Path, how: str):
    """A copy at dst of the ELF64 little-endian library src, changed where a loader reads: "paddr" moves each PT_LOAD's
    p_paddr away from its p_vaddr (the loader reads p_vaddr); "null" moves its DT_SONAME after the DT_NULL that ends
    the dynamic section, into a spare slot, and puts a DT_DEBUG in its place (the loader stops at that DT_NULL: the
    library has no SONAME then)."""
    data = bytearray(src.read_bytes())
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise AssertionError(f"{src} is no little-endian ELF64 file")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    dynamic = None
    for i in range(phnum):
        at = phoff + i * phentsize
        p_type, _, p_offset, p_vaddr, _, p_filesz = struct.unpack_from("<IIQQQQ", data, at)
        if p_type == 1 and how == "paddr":
            struct.pack_into("<Q", data, at + 24, p_vaddr + 0x10000000)
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    if how == "null":
        tags = [struct.unpack_from("<qQ", data, dynamic[0] + j) for j in range(0, dynamic[1], 16)]
        i, end = [t for t, _ in tags].index(14), [t for t, _ in tags].index(0)
        if not i < end < len(tags) - 1:
            raise AssertionError("no spare slot after the DT_NULL")
        struct.pack_into("<qQ", data, dynamic[0] + 16 * i, 21, 0)
        struct.pack_into("<qQ", data, dynamic[0] + 16 * (end + 1), 14, tags[i][1])
    dst.write_bytes(bytes(data))


def loader_says() -> dict:
    """What the system's loader says of the subdirectories it searches in each directory of a library path (ld.so
    --help): {"glibc-hwcaps/x86-64-v3": True, "glibc-hwcaps/x86-64-v4": False, "x86_64": True, ...}, True for those it
    searches on this CPU.  Read otherwise than the check reads them (LD_DEBUG)."""
    interp = next((l.split(None, 5)[5] for l in Path("/proc/self/maps").read_text().splitlines()
                   if re.search(r"/ld-linux[^/]*\.so[.\d]*$", l)), None)
    out, where = {}, None
    if interp is None:
        return out
    for line in subprocess.run([interp, "--help"], capture_output=True, text=True).stdout.splitlines():
        if line.startswith("Subdirectories of glibc-hwcaps"):
            where = "glibc-hwcaps/"
        elif line.startswith("Legacy HWCAP subdirectories"):
            where = ""
        elif line.startswith("  ") and where is not None:
            out[where + line.split()[0]] = "searched" in line
        else:
            where = None
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sha_of_zeros(n: int) -> str:
    """The sha256 of n zero bytes (a sparse file's), computed once for each n."""
    with _building:
        if f"zeros {n}" not in _built:
            h, block = hashlib.sha256(), bytes(1 << 20)
            for _ in range(n >> 20):
                h.update(block)
            h.update(block[:n & ((1 << 20) - 1)])
            _built[f"zeros {n}"] = h.hexdigest()
        return _built[f"zeros {n}"]


SIZE = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def size(v: str) -> str:
    """A memory limit as systemctl show prints it: bytes, or infinity."""
    m = re.fullmatch(r"(\d+)([KMGT]?)", v)
    return str(int(m.group(1)) * SIZE[m.group(2)]) if m else v


BOOLEAN = {**dict.fromkeys(("1", "yes", "y", "true", "t", "on"), "yes"), **dict.fromkeys(("0", "no", "n", "false", "f", "off"), "no")}


def loaded_environment(props: dict) -> list:
    """The loaded Environment= of a unit, item by item, as busctl gives it."""
    return props["EnvironmentList"] if "EnvironmentList" in props else props.get("Environment", "").split()


def cgroup_of(unit: str) -> str:
    return f"/user.slice/{unit}" if unit.startswith("user-") else f"/system.slice/{unit}"


def show_of(unit_file: Path | None, unit: str, dropins: list | None = None) -> dict:
    """What systemctl show prints for a unit loaded from unit_file and the drop-ins (the properties check-drift.sh
    reads).  A unit with drop-ins only is a user's slice: systemd's own drop-in gives it its defaults, and it is
    active."""
    dropins = sorted(dropins or [], key=lambda f: f.name)
    props = {"FragmentPath": f"/etc/systemd/system/{unit}" if unit_file else "", "LoadState": "loaded",
             "DropInPaths": " ".join(([] if unit_file else ["/usr/lib/systemd/system/user-.slice.d/10-defaults.conf"])
                                     + [f"/etc/systemd/system/{unit}.d/{f.name}" for f in dropins]),
             "NeedDaemonReload": "no", "ActiveState": "inactive" if unit_file else "active",
             "ControlGroup": cgroup_of(unit)}
    env, timers = [], []
    for line in [l for f in [unit_file] if f for l in f.read_text().splitlines()] + \
            [l for f in dropins for l in f.read_text().splitlines()]:
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        k, _, v = line.partition("=")
        if k in ("Environment", "ExecStart", "ExecStartPre", "ExecStartPost") and not v.strip():
            if k == "Environment":                      # an empty assignment empties the list
                env = []
            else:
                props.pop(k, None)
        elif k in ("Wants", "After", "Before", "WantedBy") and not v.strip():
            pass                                        # a dependency's: adds nothing, empties nothing
        elif k == "Environment":
            try:
                env += shlex.split(v)
            except ValueError:                          # an unbalanced quote: systemd ignores the line
                pass
        elif k in ("ExecStart", "ExecStartPre", "ExecStartPost"):         # one line per command
            v = v.lstrip(" \t")                        # blanks before a prefix: systemd strips them
            prefix = re.match(r"[-@:+!]*", v).group(0)
            words = shlex.split(v[len(prefix):])        # with @, the second word is argv[0]
            props.setdefault(k, []).append(f"{{ path={words[0]} ; argv[]={' '.join(words[1:] if '@' in prefix else words)}"
                                           f" ; ignore_errors={'yes' if '-' in prefix else 'no'} ; start_time=[n/a] ; "
                                           "pid=0 }")
        elif k in ("NoNewPrivileges", "PrivateTmp", "RemainAfterExit", "ProtectHome", "ProtectSystem",
                   "RuntimeDirectoryPreserve"):         # a boolean in any spelling: yes or no
            props[k] = BOOLEAN.get(v.strip().lower(), v)
        elif k == "RuntimeDirectoryMode":
            props[k] = f"{int(v, 8):04o}"
        elif k in ("LimitNOFILE", "LimitMEMLOCK"):      # the hard limit, and the soft one apart
            parts = [x if x == "infinity" else size(x) for x in v.split(":")]
            props[k], props[f"{k}Soft"] = parts[-1], parts[0]
        elif k in ("OnBootSec", "OnUnitActiveSec"):
            timers.append(f"{{ {k.replace('Sec', 'USec')}={v} ; next_elapse=1min }}")
        elif k.endswith("Sec"):
            props[k.replace("Sec", "USec")] = v
        elif k.startswith("Memory"):
            props[k] = size(v)
        else:
            props[k] = v
    # systemd 249 prints an item with a blank or a line break as [unprintable]; busctl gives it as it is
    props["Environment"] = " ".join("[unprintable]" if re.search(r"[ \n]", x) else x for x in env)
    if props["Environment"].split() != env:
        props["EnvironmentList"] = env
    if timers:
        props["TimersMonotonic"] = timers
    return props


# busctl --json=short get-property org.freedesktop.systemd1 PATH INTERFACE Environment, from the spec
BUSCTL = r'''#!/usr/bin/env python3
import json, os, re, sys
a = sys.argv[1:]
spec = json.load(open(os.environ["DRIFT_TEST_SPEC"]))
if a[:3] != ["--json=short", "get-property", "org.freedesktop.systemd1"] or a[5:] != ["Environment"]:
    sys.exit(1)
label = a[3].rpartition("/")[2]
if not a[3].startswith("/org/freedesktop/systemd1/unit/") or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
    sys.exit(1)                                  # no object path of a unit
unit = re.sub(r"_([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), label)
props = spec.get(unit)
if props is None or a[4] != "org.freedesktop.systemd1." + unit.rpartition(".")[2].capitalize():
    sys.exit(1)
env = props["EnvironmentList"] if "EnvironmentList" in props else props.get("Environment", "").split()
print(json.dumps({"type": "as", "data": env}))
'''

SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
spec = json.load(open(os.environ["DRIFT_TEST_SPEC"]))
if a[:1] == ["show"]:
    value_only = "--value" in a
    a = [x for x in a if x != "--value"]
    unit = next(x for x in a[1:] if not x.startswith("-") and x != a[a.index("-p") + 1])
    props = a[a.index("-p") + 1].split(",")
    s = spec.get(unit, {})
    for p in props:
        v = s.get(p, "")
        for one in (v if isinstance(v, list) else [v]):
            print(one if value_only else f"{p}={one}")
    sys.exit(0)
if a[:1] == ["show-environment"]:
    if spec["__manager__"] is None:
        sys.exit(1)
    for k, v in spec["__manager__"].items():
        print(f"{k}={v}")
    sys.exit(0)
if a[:1] == ["is-active"]:
    print(spec.get(a[1], {}).get("ActiveState", "inactive")); sys.exit(0)
sys.exit(1)
'''

# serve/server.py of the sandbox's Strata tree: it starts its engines as server.py does, as far as the sandbox's
# configs go (the args, the layer split, split_skip_if_fits, vram_elastic, --batch; the environment, the directory),
# writes the file server-params.json names, and waits
SERVER = r'''import json, os, subprocess, sys, time
from pathlib import Path
params = json.loads((Path(__file__).resolve().parents[3] / "server-params.json").read_text())
cfg = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
args = list(cfg["args"])
gpus = cfg.get("gpu") or []
if len(gpus) > 1 and "--layer-split" not in args:
    v = cfg.get("layer_split")
    args += ["--layer-split", "auto" if v in (None, "auto") else ",".join(str(x) for x in v)]
if len(gpus) > 1 and cfg.get("split_skip_if_fits"):
    args.append("--split-skip-if-fits")
if cfg.get("vram_elastic") is True:
    args.append("--vram-elastic")
if (cfg.get("parallel") or 0) > 1 and "--batch" not in args:
    args += ["--batch", str(cfg["parallel"])]
env = dict(os.environ)
if gpus:
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpus)
env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
dirs = [d for d in cfg.get("lib_dirs") or [] if os.path.isdir(d)]
if dirs:
    env["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
args += params["engine_args"]
env.update(params["engine_env"])
fd = params.get("child_socket")                  # the listening socket: its engines' only, not its own
engines = [subprocess.Popen([cfg["exe"], "--serve", *args], env=env, cwd=cfg.get("cwd"), pass_fds=() if fd is None else (fd,))
           for _ in range(params["engines"])]
if fd is not None:
    os.close(fd)
Path(params["up"]).write_text("up")
later = params.get("listen6")                    # a second socket of its own, on [::1], when the test asks for it
if later:
    import socket
    for _ in range(2400):
        if os.path.exists(later):
            break
        time.sleep(0.05)
    s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s6.bind(("::1", int(Path(later).read_text())))
    s6.listen()
    Path(later + ".done").write_text("")
time.sleep(600)
'''


def limit(r: int) -> str:
    """This process's limit r as a unit's Limit*= gives it."""
    soft, hard = resource.getrlimit(r)
    f = lambda v: "infinity" if v == resource.RLIM_INFINITY else str(v)            # noqa: E731
    return f(soft) if soft == hard else f"{f(soft)}:{f(hard)}"


def to_sandbox(text: str, repo: Path, strata: Path) -> str:
    """A unit of the repo with the paths of the live checkout and of the Strata tree in the sandbox, and this user and
    this process's limits, which the fake server.py runs with."""
    text = re.sub(rf"{re.escape(LIVE)}(?=/|\s|$)", str(repo), text, flags=re.M)
    text = re.sub(rf"{re.escape(STRATA)}(?=/|\s|$)", str(strata), text, flags=re.M)
    for key, value in (("User", pwd.getpwuid(os.getuid()).pw_name), ("Group", grp.getgrgid(os.getgid()).gr_name),
                       ("LimitMEMLOCK", limit(resource.RLIMIT_MEMLOCK)), ("LimitNOFILE", limit(resource.RLIMIT_NOFILE)),
                       ("OOMScoreAdjust", Path("/proc/self/oom_score_adj").read_text().strip()),
                       ("ProtectSystem", "no"), ("PrivateTmp", "no"), ("ProtectHome", "no")):
        text = re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", text)
    return text


def pyc_of(src: Path, code: str, flags: int = 0, mtime: int | None = None, wrong_hash: bool = False) -> bytes:
    """Bytecode for src, as Python caches it, that holds the code of another source."""
    st = src.stat()
    head = importlib.util.MAGIC_NUMBER + flags.to_bytes(4, "little")
    if flags & 1:
        head += b"\0" * 8 if wrong_hash else importlib.util.source_hash(src.read_bytes())
    else:
        head += (int(st.st_mtime) if mtime is None else mtime).to_bytes(4, "little") + st.st_size.to_bytes(4, "little")
    return head + marshal.dumps(compile(code, str(src), "exec", dont_inherit=True))


class Sandbox:
    def __init__(self, test: unittest.TestCase):
        self.t = test
        self.d = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.d, True)
        # the check's TMPDIR: its temporary directory (mktemp -d) is made in the sandbox and goes with it, also when
        # run() kills the check, which leaves its EXIT trap no time to remove that directory
        self.tmp = self.d / "tmp"
        self.tmp.mkdir()
        self.repo, self.sys, self.dev = self.d / "repo", self.d / "sys", self.d / "dev"
        self.strata = self.dev / "strata"
        # this sandbox's engines only: those of another one that runs meanwhile are not "another engine" here
        self.engine_re = f"^{re.escape(str(self.strata))}/[e]ngine-cuda12/strata-fake --serve"
        for sub in ("scripts", "systemd", "config", "tools"):
            (self.repo / sub).mkdir(parents=True)
        for f in ("scripts/check-drift.sh", "scripts/strata-health.sh", "tools/configure_strata_q3.py",
                  "config/profile.conf", "config/strata-iq3_xxs.setup.json"):
            shutil.copy2(ROOT / f, self.repo / f)
        for f in (ROOT / "systemd").iterdir():
            if f.is_dir():                                # a unit's drop-ins
                (self.repo / "systemd" / f.name).mkdir()
                for g in f.iterdir():
                    (self.repo / "systemd" / f.name / g.name).write_text(to_sandbox(g.read_text(), self.repo, self.strata))
            else:
                (self.repo / "systemd" / f.name).write_text(to_sandbox(f.read_text(), self.repo, self.strata))
        (self.repo / "scripts" / "host-units.sh").write_text("#!/bin/sh\necho 'ok     host units (stub)'\n")
        (self.repo / "scripts" / "host-units.sh").chmod(0o755)
        # the engine: a program that only waits, at the path the configs name; server.py
        (self.strata / "engine-cuda12").mkdir(parents=True)
        self.exe = self.strata / "engine-cuda12" / "strata-fake"
        shutil.copy2(engine_program(), self.exe)
        (self.strata / "serve").mkdir()
        (self.strata / "serve" / "server.py").write_text(SERVER)
        # the setup config with the sandbox's engine and directory; the deployed snapshot is what the tool makes of it
        setup = json.loads((self.repo / "config" / "strata-iq3_xxs.setup.json").read_text())
        setup.update(exe=str(self.exe), cwd=str(self.strata))
        (self.repo / "config" / "strata-iq3_xxs.setup.json").write_text(json.dumps(setup, indent=4) + "\n")
        out = self.d / "made.json"
        shutil.copy2(self.repo / "config" / "strata-iq3_xxs.setup.json", out)
        args = shlex.split(re.sub(r"#.*", "", (self.repo / "config" / "profile.conf").read_text()))
        subprocess.run([sys.executable, str(self.repo / "tools" / "configure_strata_q3.py"), str(out), *args],
                       check=True, capture_output=True)
        shutil.copy2(out, self.repo / "config" / "strata-iq3_xxs.deployed.json")
        shutil.copy2(out, self.strata / "strata-iq3_xxs.json")
        # the Strata checkout at its pinned commit
        self.git = ["git", "-C", str(self.strata), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", str(self.strata)], check=True)
        (self.strata / ".gitignore").write_text("engine-cuda12/\n*.json\n.venv/\n")
        self.commit_strata()
        self.make_venv()
        # the files the list names; the engine maps the library outside ~/dev
        (self.dev / "Strata-data" / "packs" / "p").mkdir(parents=True)
        (self.dev / "Strata-data" / "packs" / "p" / "index.txt").write_text("index\n")
        self.abs_file = self.d / "elsewhere" / "libx.so"
        self.abs_file.parent.mkdir()
        shutil.copyfile(shared_library("libx.so"), self.abs_file)
        self.big = self.dev / "Strata-data" / "packs" / "p" / "experts.bin"
        with open(self.big, "wb") as f:                          # sparse: over 1 GiB, no disk
            f.truncate((1 << 30) + 4096)
        self.write_list()
        self.maps(self.abs_file)
        # the system's files
        for u in UNITS:
            (self.sys / "etc/systemd/system").mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repo / "systemd" / u, self.sys / "etc/systemd/system" / u)
        (self.sys / "etc/logrotate.d").mkdir(parents=True)
        shutil.copy2(self.repo / "systemd" / "strata-q3.logrotate", self.sys / "etc/logrotate.d/strata-q3")
        (self.sys / "usr/local/sbin").mkdir(parents=True)
        shutil.copy2(self.repo / "scripts" / "strata-health.sh", self.sys / "usr/local/sbin/strata-q3-health")
        params = self.sys / "sys/module/zfs/parameters"
        params.mkdir(parents=True)
        for p in ("zfs_arc_max", "zfs_arc_shrinker_limit"):
            m = re.search(rf"echo (\d+) > /sys/module/zfs/parameters/{p}\b", (self.repo / "systemd/zfs-arc-max.service").read_text())
            (params / p).write_text(m.group(1) + "\n")
        os.chmod(self.sys / "usr/local/sbin/strata-q3-health", 0o755)
        for f in list((self.sys / "etc").rglob("*")):
            if f.is_file():
                os.chmod(f, 0o644)
        # systemctl, and the memory limits of the units' cgroups
        self.spec = {u: show_of(self.repo / "systemd" / u, u) for u in UNITS}
        self.spec["__manager__"] = {"LANG": "C.UTF-8", "PATH": MANAGER_PATH}
        for d in sorted((self.repo / "systemd").glob("*.d")):
            self.install_dropins(d.name[:-2])
        self.cgroups()
        self.bin = self.d / "bin"
        self.bin.mkdir()
        for name, text in (("systemctl", SYSTEMCTL), ("busctl", BUSCTL)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.save_spec()
        # this checkout, the one the units name: a git repository with no local change
        self.commit()
        self.url = ""
        self.seen: list[str | None] = []          # the Authorization header of each request the server got

    def dropin(self, unit: str = "user-1003.slice", name: str = "50-memory.conf",
               text: str = "[Slice]\nMemoryMax=6G\nMemorySwapMax=0\n"):
        """A drop-in of the repo for unit (ADR 0007's memory limit by default), installed and loaded."""
        d = self.repo / "systemd" / f"{unit}.d"
        d.mkdir(exist_ok=True)
        (d / name).write_text(text)
        self.install_dropins(unit)
        self.cgroups()
        self.save_spec()
        self.commit()

    def install_dropins(self, unit: str):
        """The repo's drop-ins of unit installed as install-system-files.sh installs them, and what systemd loads."""
        files = sorted((self.repo / "systemd" / f"{unit}.d").glob("*.conf"))
        there = self.sys / "etc/systemd/system" / f"{unit}.d"
        there.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, there / f.name)
            os.chmod(there / f.name, 0o644)
        file = self.repo / "systemd" / unit
        state = {k: v for k, v in self.spec.get(unit, {}).items() if k in ("ActiveState", "MainPID")}
        self.spec[unit] = {**show_of(file if file.exists() else None, unit, files), **state}

    def cgroups(self):
        """The memory limits of each unit's cgroup, as systemd writes them from what it loaded."""
        for unit, props in self.spec.items():
            for key, name in (("MemoryMax", "memory.max"), ("MemorySwapMax", "memory.swap.max")):
                if key in props:
                    f = self.sys / f"sys/fs/cgroup{cgroup_of(unit)}" / name
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(("max" if props[key] == "infinity" else props[key]) + "\n")

    def commit_strata(self, *paths: str):
        subprocess.run(self.git + ["add", ".gitignore", "serve/server.py", *paths], check=True)
        subprocess.run(self.git + ["commit", "-q", "-m", "pin"], check=True)
        self.pin = subprocess.run(self.git[:3] + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def make_venv(self):
        """A venv as python -m venv and pip make one: this Python and its links, an activate script, and one package
        with its RECORD, its bytecode and a program in bin/."""
        venv = self.strata / ".venv"
        (venv / "bin").mkdir(parents=True)
        version = f"python{sys.version_info[0]}.{sys.version_info[1]}"
        os.symlink(sys.executable, venv / "bin" / "python3")
        os.symlink("python3", venv / "bin" / "python")
        os.symlink("python3", venv / "bin" / version)
        (venv / "bin" / "activate").write_text("# activate\n")
        os.symlink("lib", venv / "lib64")
        (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\ninclude-system-site-packages = false\n"
                                         f"version = {platform.python_version()}\n")
        self.site = venv / "lib" / version / "site-packages"
        (self.site / "pkg").mkdir(parents=True)
        (self.site / "pkg" / "__init__.py").write_text("from .mod import x\n")
        (self.site / "pkg" / "mod.py").write_text("x = 1\n")
        for f in ("__init__.py", "mod.py"):
            py_compile.compile(str(self.site / "pkg" / f), doraise=True)
        info = self.site / "pkg-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n\nA test package.\n")
        (info / "INSTALLER").write_text("pip\n")
        rows = []
        for f in sorted(p for p in self.site.rglob("*") if p.is_file()):
            rel = f.relative_to(self.site).as_posix()
            if "__pycache__" in rel:
                rows.append(f"{rel},,")
            else:
                digest = base64.urlsafe_b64encode(hashlib.sha256(f.read_bytes()).digest()).rstrip(b"=").decode()
                rows.append(f"{rel},sha256={digest},{f.stat().st_size}")
        (venv / "bin" / "pkg").write_text("#!/usr/bin/python3\nimport pkg\n")
        digest = base64.urlsafe_b64encode(hashlib.sha256((venv / "bin" / "pkg").read_bytes()).digest()).rstrip(b"=").decode()
        rows += [f"../../../bin/pkg,sha256={digest},{(venv / 'bin' / 'pkg').stat().st_size}", "pkg-1.0.dist-info/RECORD,,"]
        (info / "RECORD").write_text("\n".join(rows) + "\n")
        self.pin_venv()

    def pin_venv(self):
        """config/strata-venv.txt as one writes it after an intended change of the venv: each dist-info's name==version
        and the sha256 of its RECORD."""
        lines = ["# test"]
        for info in sorted(self.site.glob("*.dist-info")):
            meta = dict(l.split(": ", 1) for l in (info / "METADATA").read_text().split("\n\n")[0].splitlines())
            lines.append(f"{meta['Name']}=={meta['Version']}  {sha(info / 'RECORD')}")
        (self.repo / "config" / "strata-venv.txt").write_text("\n".join(lines) + "\n")

    def record(self, *rows: str, info: str = "pkg-1.0.dist-info"):
        """Rows added to a RECORD of the venv (a file's own row: its sha256 and size), and the list pinned again."""
        rec = self.site / info / "RECORD"
        new = []
        for r in rows:
            if "," not in r:
                f = self.site / r
                d = base64.urlsafe_b64encode(hashlib.sha256(f.read_bytes()).digest()).rstrip(b"=").decode()
                r = f"{r},sha256={d},{f.stat().st_size}"
            new.append(r)
        rec.write_text(rec.read_text() + "".join(f"{r}\n" for r in new))
        self.pin_venv()

    def write_list(self, pin: str | None = None, extra: str = "", big: str = "0" * 64):
        lines = ["# test list", f"# strata source pin: {pin or self.pin} (test)"]
        for p in (self.exe, self.strata / "strata-iq3_xxs.json",
                  self.dev / "Strata-data" / "packs" / "p" / "index.txt"):
            lines.append(f"{sha(p)}  {p.relative_to(self.dev)}")
        lines.append(f"{sha(self.abs_file)}  {self.abs_file}")
        lines.append(f"{big}  {self.big.relative_to(self.dev)}")          # over 1 GiB: only with --full
        (self.repo / "config" / "deployed-artifacts.sha256").write_text("\n".join(lines) + "\n" + extra)

    def listed(self, p: Path) -> str:
        """A line of the list for a file of the sandbox."""
        return f"{sha(p)}  {p.relative_to(self.dev) if p.is_relative_to(self.dev) else p}\n"

    def maps(self, *files: Path):
        """The files the engine maps (from its start on)."""
        (self.exe.parent / "maps.txt").write_text("".join(f"{f}\n" for f in files))

    def save_spec(self):
        (self.d / "spec.json").write_text(json.dumps(self.spec))

    def commit(self):
        g = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        if not (self.repo / ".git").exists():
            subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-q", "-m", "change", "--allow-empty"], check=True)

    def edit_unit(self, change):
        """The service's unit changed in the repo, installed and loaded alike."""
        for d in (self.repo / "systemd", self.sys / "etc/systemd/system"):
            f = d / "strata-q3.service"
            f.write_text(change(f.read_text()))
        state = {k: v for k, v in self.spec["strata-q3.service"].items()
                 if k in ("ActiveState", "MainPID", "ExecMainStartTimestampMonotonic", "ExecMainStartTimestamp")}
        self.spec["strata-q3.service"] = {**show_of(self.repo / "systemd" / "strata-q3.service", "strata-q3.service"), **state}
        self.save_spec()
        self.commit()

    def unit(self) -> dict:
        out = {}
        for line in (self.repo / "systemd" / "strata-q3.service").read_text().splitlines():
            k, _, v = line.partition("=")
            if v and not k.startswith(("#", ";", "[")):
                out[k.strip()] = v.strip()
        return out

    def run(self, *args: str, env: dict | None = None, script: Path | None = None, cwd: str | None = None,
            timeout: float = 300, by_path: bool = False) -> subprocess.CompletedProcess:
        """The check, in a session of its own: when it does not end within timeout s (a read that blocks), its
        processes are killed and the result says so (returncode None).  It runs as bash SCRIPT, or by_path, as
        scripts/deploy.sh runs it: its first line says how."""
        e = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "DRIFT_SYSROOT": str(self.sys),
             "DRIFT_OWNER": f"{os.getuid()}:{os.getgid()}", "DRIFT_ENGINE_RE": self.engine_re,
             "DRIFT_URL": self.url or "http://127.0.0.1:9", "DRIFT_TEST_SPEC": str(self.d / "spec.json"),
             "TMPDIR": str(self.tmp), **(env or {})}
        e = {k: v for k, v in e.items() if v is not None}
        argv = [*([] if by_path else ["bash"]), str(script or self.repo / "scripts" / "check-drift.sh"), *args]
        p = subprocess.Popen(argv, env=e,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            return subprocess.CompletedProcess(p.args, None, out, err + f"\n(the check did not end within {timeout} s)")
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def configure(self, change):
        """Setup's config changed, and the deployed snapshot, the live config and the list made of it again."""
        f = self.repo / "config" / "strata-iq3_xxs.setup.json"
        cfg = json.loads(f.read_text())
        change(cfg)
        f.write_text(json.dumps(cfg, indent=4) + "\n")
        out = self.d / "made.json"
        shutil.copy2(f, out)
        args = shlex.split(re.sub(r"#.*", "", (self.repo / "config" / "profile.conf").read_text()))
        subprocess.run([sys.executable, str(self.repo / "tools" / "configure_strata_q3.py"), str(out), *args],
                       check=True, capture_output=True)
        shutil.copy2(out, self.repo / "config" / "strata-iq3_xxs.deployed.json")
        shutil.copy2(out, self.strata / "strata-iq3_xxs.json")
        self.write_list()
        self.commit()

    def start_service(self, engines: int = 1, server_env: dict | None = None, shared: dict | None = None,
                      unloaded: bool = False, key: str | None = None, argv: list | None = None, cwd: Path | None = None,
                      no_new_privs: bool = True, oom: int | None = None, nofile: int | None = None,
                      memlock: int | None = None, bind: str = "127.0.0.1", program: str | None = None,
                      redirect: str | None = None, reply=None, foreign: bool = False, engine_args: list | None = None,
                      engine_env: dict | None = None, child_socket: bool = False, listen6: Path | None = None,
                      wrap: list | None = None):
        """serve/server.py as its unit starts it (its ExecStart, WorkingDirectory, NoNewPrivileges, and this user and
        process's limits, which the sandbox's unit gives), with systemd's environment for it; its children are fake
        engines (with engine_args and engine_env added, as server.py would not).  An HTTP server of this test answers
        /settings and /v1/models (with the key, when one is given; or reply(Authorization), raw), on a socket that
        server.py holds too, as if it listened on it itself (foreign: it does not; child_socket: only its engines
        do; listen6: it listens on [::1] too, at the port that file holds, once the test writes it; wrap: a command
        that runs it, which execs it)."""
        u = self.unit()
        unit_env = dict(kv.split("=", 1) for kv in loaded_environment(self.spec["strata-q3.service"]) if "=" in kv)
        pw = pwd.getpwuid(os.getuid())
        env = {**self.spec["__manager__"], **unit_env, "HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
               "SHELL": pw.pw_shell, "INVOCATION_ID": "0" * 32, **(server_env or {})}
        env = {k: v for k, v in env.items() if v is not None}
        up = self.d / "up"
        up.unlink(missing_ok=True)
        params = {"engines": engines, "up": str(up), "engine_args": engine_args or [], "engine_env": engine_env or {},
                  "listen6": str(listen6) if listen6 else None}
        bodies = {"/settings": json.dumps(shared or {"shared": False, "defaults": {}}).encode(),
                  "/v1/models": json.dumps({"data": [{"id": "m", "status": {"value": "unloaded" if unloaded else "loaded"}}]}).encode()}
        seen = self.seen

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                if reply:
                    self.wfile.write(reply(self.headers.get("Authorization")))
                    return
                if redirect:
                    self.send_response(302); self.send_header("Location", redirect + self.path); self.end_headers()
                    return
                if key and self.headers.get("Authorization") != f"Bearer {key}":
                    self.send_response(401); self.end_headers()
                    return
                body = bodies.get(self.path)
                if body is None:
                    self.send_response(404); self.end_headers()
                    return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer((bind, 0), H)
        self.t.addCleanup(srv.server_close)
        if child_socket:
            params["child_socket"] = srv.fileno()
        (self.d / "server-params.json").write_text(json.dumps(params))
        launch = list(wrap or [])                          # util-linux programs that exec the next one
        if nofile is not None:
            launch += ["prlimit", f"--nofile={nofile}:{resource.getrlimit(resource.RLIMIT_NOFILE)[1]}", "--"]
        if memlock is not None:
            hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)[1]
            launch += ["prlimit", f"--memlock={memlock}:{'unlimited' if hard == resource.RLIM_INFINITY else hard}", "--"]
        if oom is not None:
            launch += ["choom", "-n", str(oom), "--"]
        if no_new_privs:
            launch += ["setpriv", "--no-new-privs", "--"]
        if program:                                        # another program, with the unit's command line
            launch += ["bash", "-c", 'p=$PROGRAM; unset PROGRAM; exec -a "$0" "$p" "$@"']
            env["PROGRAM"] = program
        # the check counts the start in the 10 ms ticks of /proc/<pid>/stat: what was written before is before it
        time.sleep(0.02)
        started = int(time.monotonic() * 1e6)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S.%f UTC")
        p = subprocess.Popen(launch + (argv or shlex.split(u["ExecStart"])), env=env, cwd=cwd or u["WorkingDirectory"],
                             start_new_session=True, pass_fds=() if foreign else (srv.fileno(),))

        def end():
            with contextlib.suppress(ProcessLookupError):        # a test may have ended it
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        self.t.addCleanup(end)
        self.srv, self.proc = srv, p
        for _ in range(200):
            if up.exists():
                break
            time.sleep(0.05)
        self.t.assertTrue(up.exists(), "the fake server.py did not start its engines")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.t.addCleanup(srv.shutdown)                  # runs first
        self.url = f"http://{bind}:{srv.server_address[1]}"
        s = self.spec["strata-q3.service"]
        s.update(ActiveState="active", MainPID=str(p.pid), ExecMainStartTimestampMonotonic=str(started),
                 ExecMainStartTimestamp=stamp)
        self.save_spec()
        time.sleep(0.02)              # a file's times come from the kernel's coarse clock: later changes are later
        return p

    def edit_live(self, change):
        live = self.strata / "strata-iq3_xxs.json"
        cfg = json.loads(live.read_text())
        change(cfg)
        live.write_text(json.dumps(cfg, indent=4) + "\n")


@unittest.skipUnless(shutil.which("gcc") and shutil.which("git"), "needs gcc and git")
class CheckDrift(unittest.TestCase):
    def drift(self, sb: Sandbox | None, says: str, *args: str, env: dict | None = None,
              r: subprocess.CompletedProcess | None = None):
        """The check on sb (or its result r) finds drift, and says that."""
        r = r if r is not None else sb.run(*args, env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(says, r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "drift found: see DRIFT lines above")
        self.assertNotIn("Traceback", r.stderr)
        return r

    def clean(self, sb: Sandbox | None, *args: str, env: dict | None = None,
              r: subprocess.CompletedProcess | None = None):
        r = r if r is not None else sb.run(*args, env=env)
        self.assertEqual((r.returncode, r.stdout.splitlines()[-1:]), (0, ["no drift"]), r.stdout + r.stderr)
        return r

    def started(self, **kw) -> Sandbox:
        sb = Sandbox(self)
        sb.start_service(**kw)
        return sb

    def each(self, cases: list, timeout: float = 300):
        """cases: (name, prepare).  prepare(sb) changes a sandbox of its own (and may start its service) and returns
        what the check must say of it: {"drift": a text of a DRIFT line, or None for "no drift"; "notes": texts of
        note lines; "absent": texts it must not print; "then": a function of the result, for more assertions; "args",
        "env", "script" and "cwd": the check's arguments, environment, copy and directory; "by_path": run by its path,
        not as bash SCRIPT}.  The sandboxes are made and
        checked at the same time; the assertions are made here, in the order of the cases."""
        def go(case):
            sb = Sandbox(self)
            want = case[1](sb) or {}
            return want, sb.run(*want.get("args", ()), env=want.get("env"), script=want.get("script"),
                                cwd=want.get("cwd"), timeout=timeout, by_path=want.get("by_path", False))
        for (name, _), (want, r) in zip(cases, together(go, cases)):
            with self.subTest(name):
                if want.get("drift") is None:
                    self.clean(None, r=r)
                else:
                    self.drift(None, want["drift"], r=r)
                for n in want.get("notes", ()):
                    self.assertIn(f"note   {n}", r.stdout)
                for t in want.get("absent", ()):
                    self.assertNotIn(t, r.stdout + r.stderr)
                if want.get("then"):
                    want["then"](r)

    def test_an_unchanged_sandbox(self):
        sb = Sandbox(self)
        r = self.clean(sb)
        self.assertIn("skip   Strata-data/packs/p/experts.bin (--full: 1 GiB)", r.stdout)
        self.assertIn(f"ok     {sb.abs_file}", r.stdout)
        self.assertIn(f"ok     {sb.strata}: its 2 files are the pinned commit's", r.stdout)
        self.assertIn(f"ok     {sb.strata}/.venv: the packages config/strata-venv.txt lists, with the RECORDs it pins, "
                      "file by file", r.stdout)
        self.assertIn(f"ok     {sb.repo}: its commit", r.stdout)
        self.assertIn("note   strata-q3.service is inactive: what runs is not compared", r.stdout)
        self.assertIn("note   test hooks set: DRIFT_ENGINE_RE DRIFT_OWNER DRIFT_SYSROOT DRIFT_TEST_SPEC DRIFT_URL", r.stdout)
        self.assertIn("== host units (config/host-units.conf)\nok     host units (stub)\n", r.stdout)   # its own lines
        self.assertNotIn("runs what the files above say", r.stdout)
        sb.start_service()
        r = self.clean(sb)
        self.assertIn("ok     strata-q3.service runs what the files above say", r.stdout)

    def test_usage_and_no_temporary_directory(self):
        sb = Sandbox(self)
        for a in ("--ful", "-full", "--help", "x", "--candidates", "--fullx"):
            self.assertEqual(sb.run(a).returncode, 2, a)
        tmp = sb.d / "tmpdir"
        tmp.mkdir()
        self.clean(sb, "--candidate", env={"TMPDIR": str(tmp)})
        self.assertEqual(list(tmp.iterdir()), [])          # its temporary directory removed
        self.drift(sb, "cannot create a temporary directory", env={"TMPDIR": str(sb.d / "nonexistent")})

    def test_installed_files(self):
        own = f"{os.getuid()}:{os.getgid()}"

        def edited(sb):
            f = sb.sys / "etc/systemd/system/strata-q3.service"
            f.write_text(f.read_text() + "\n# edited\n")
            return {"drift": f"{f} differs from the repo copy"}

        def missing(sb):
            (sb.sys / "usr/local/sbin/strata-q3-health").unlink()
            return {"drift": "strata-q3-health is missing"}

        def zfs(sb):
            (sb.sys / "sys/module/zfs/parameters/zfs_arc_max").write_text("8589934592\n")
            return {"drift": "zfs_arc_max is 8589934592"}

        def mode(sb):
            f = sb.sys / "etc/logrotate.d/strata-q3"
            os.chmod(f, 0o600)
            return {"drift": f"{f} is owner:group mode {own} 600, not {own} 644"}

        def roots(sb):                                  # root's, as install-system-files.sh installs them
            return {"drift": f"is owner:group mode {own} 644, not 0:0 644", "env": {"DRIFT_OWNER": "0:0"}}
        self.each([(f.__name__, f) for f in (edited, missing, zfs, mode, roots)])

    def test_what_systemd_loaded(self):
        cases = [
            ("strata-q3.service", "FragmentPath", "/run/systemd/system/strata-q3.service", "systemd loads it from /run"),
            ("strata-q3.service", "DropInPaths", "/etc/systemd/system/strata-q3.service.d/x.conf", "drop-ins override it"),
            # the system's own, for a unit the repo has a file of: that file is all of it
            ("strata-q3.service", "DropInPaths", "/usr/lib/systemd/system/service.d/10-timeout-abort.conf",
             "drop-ins override it: /usr/lib/systemd/system/service.d/10-timeout-abort.conf"),
            ("strata-q3.service", "NeedDaemonReload", "yes", "changed on disk since the last daemon-reload"),
            ("strata-q3.service", "LoadState", "masked", "LoadState is masked"),
            ("strata-q3.service", "LoadState", "bad-setting", "LoadState is bad-setting"),
            ("strata-q3.service", "NeedDaemonReload", "", "changed on disk since the last daemon-reload"),
            ("strata-q3.service", "Environment", "PYTHONUNBUFFERED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 "
             "MKL_NUM_THREADS=1 STRATA_ENGINE_READY_S=1800", "Environment differs from the repo's in ['PYTHONUNBUFFERED']"),
            ("strata-q3.service", "TimeoutStartUSec", "1h 30min", "TimeoutStartSec is '1h 30min'"),
            ("strata-q3.service", "TimeoutStartUSec", "2fortnights", "a time unit this check does not know"),
            ("strata-q3.service", "Environment", "PYTHONUNBUFFERED=1", "Environment differs from the repo's in"),
            ("strata-q3.service", "ExecStartPre", "{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no }", "ExecStartPre: the loaded commands are not the repo's"),
            ("strata-q3.service", "LimitMEMLOCK", "12345", ":12345' (soft:hard), the repo says"),
            ("strata-q3.service", "WantedBy", "", "WantedBy lacks multi-user.target"),
            ("strata-q3-health.timer", "TimersMonotonic", ["{ OnBootUSec=10min ; next_elapse=1min }"], "OnUnitActiveSec: the loaded timer has none"),
            ("zfs-arc-max.service", "ExecStart", "{ path=/bin/sh ; argv[]=/bin/sh -ec echo 1 ; ignore_errors=no }", "ExecStart: the loaded commands are not the repo's"),
        ]

        def case(unit, prop, value, says):
            def prepare(sb):
                sb.spec[unit][prop] = value
                sb.save_spec()
                return {"drift": says}
            return prepare

        def last_value(sb):                         # a setting of the unit and of a drop-in: the drop-in's counts
            sb.dropin(unit="strata-q3.service", name="90-restart.conf", text="[Service]\nRestart=always\n")
            return {}

        def one_more_command(sb):                   # a command loaded beside the repo's: drift too
            s = sb.spec["strata-q3.service"]
            s["ExecStartPre"] = s["ExecStartPre"] + ["{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; "
                                                     "start_time=[n/a] ; pid=0 }"]
            sb.save_spec()
            return {"drift": "ExecStartPre: the loaded commands are not the repo's (2 loaded, 1 in the repo; command 2 "
                             "differs)"}

        def two_drop_ins(sb):                       # read in the order of their names: the last one's value counts
            sb.dropin(unit="strata-q3.service", name="20-b.conf", text="[Service]\nRestart=no\n")
            sb.dropin(unit="strata-q3.service", name="10-a.conf", text="[Service]\nRestart=always\n")
            return {}

        def no_memory_limit(sb):                    # MemoryMax=infinity: max in the cgroup
            sb.dropin(text="[Slice]\nMemoryMax=infinity\nMemorySwapMax=0\n")
            return {"then": lambda r: self.assertIn("ok     user-1003.slice (loaded: as the repo's drop-ins, 2 settings; "
                                                    "its cgroup holds its MemoryMax, MemorySwapMax)", r.stdout)}

        def every_line(sb):                         # a list setting on two lines: the items of both
            sb.dropin(unit="strata-q3.service", name="90-after.conf", text="[Unit]\nAfter=foo.service\n")
            sb.spec["strata-q3.service"]["After"] = "foo.service"
            sb.save_spec()
            return {"drift": "After lacks nvidia-persistenced.service zfs-arc-max.service"}
        self.each([(f"{unit} {prop}={value}", case(unit, prop, value, says)) for unit, prop, value, says in cases]
                  + [("the last value", last_value), ("every line's items", every_line),
                     ("one command more", one_more_command), ("two drop-ins", two_drop_ins),
                     ("MemoryMax=infinity", no_memory_limit)])

    def test_what_it_prints_of_a_unit_holds_no_value(self):
        sb = Sandbox(self)
        s = sb.spec["strata-q3.service"]
        s["Environment"] += " STRATA_API_KEY=k-not-printed"            # a drop-in's key, say
        sb.save_spec()
        r = self.drift(sb, "Environment differs from the repo's in ['STRATA_API_KEY']")
        self.assertNotIn("k-not-printed", r.stdout + r.stderr)
        sb = Sandbox(self)                               # a command line with a key
        s = sb.spec["strata-q3.service"]
        s["ExecStart"] = [s["ExecStart"][0].replace("--port 8080", "--port 8080 --api-key k-not-printed")]
        sb.save_spec()
        r = self.drift(sb, "ExecStart: the loaded commands are not the repo's (1 loaded, 1 in the repo; command 1 differs)")
        self.assertNotIn("k-not-printed", r.stdout + r.stderr)

    def test_several_commands_and_their_prefixes(self):
        def commands(change=None, says=None):
            def prepare(sb):
                for d in (sb.repo / "systemd", sb.sys / "etc/systemd/system"):
                    f = d / "strata-q3.service"
                    f.write_text(f.read_text().replace("\nExecStartPre=", "\nExecStartPre=-/bin/true x\nExecStartPre=", 1))
                sb.spec["strata-q3.service"] = show_of(sb.repo / "systemd" / "strata-q3.service", "strata-q3.service")
                sb.commit()
                if change:
                    lines = sb.spec["strata-q3.service"]["ExecStartPre"]
                    sb.spec["strata-q3.service"]["ExecStartPre"] = change(lines)
                sb.save_spec()
                return {"drift": says}
            return prepare
        self.each([
            ("as the repo's", commands()),
            ("its prefix", commands(lambda lines: [lines[0].replace("ignore_errors=yes", "ignore_errors=no"), lines[1]],
                                    "ExecStartPre: the loaded commands are not the repo's (2 loaded, 2 in the repo; "
                                    "command 1 differs)")),
            ("the first not loaded", commands(lambda lines: lines[1:], "ExecStartPre: the loaded commands are not the "
                                                                       "repo's (1 loaded, 2 in the repo"))])

    def test_a_drop_in_that_empties_a_list(self):
        # an empty assignment (ExecStart=, Environment=) empties the list before it, as systemd does, also in a drop-in
        # of the repo; a dependency's (After=) adds nothing and empties nothing.  For strata-q3.service and
        # zfs-arc-max.service the rest of the check reads some settings from the unit file alone (the live config, the
        # checkout, what runs, the ZFS values): a drop-in of the repo that sets one is drift, and says where to set it
        def health(text, says=None, loaded=None):
            def prepare(sb):
                start = re.search(r"(?m)^ExecStart=(.*)$", (sb.repo / "systemd" / "strata-q3-health.service").read_text())
                sb.dropin(unit="strata-q3-health.service", name="90-x.conf", text=text.format(start=start.group(1)))
                if loaded:                           # what systemd loaded, if it were otherwise
                    sb.spec["strata-q3-health.service"].update(loaded)
                    sb.save_spec()
                return {"drift": says}
            return prepare

        def refused(unit, key, text):
            def prepare(sb):
                sb.dropin(unit=unit, name="90-x.conf", text=text)
                f = sb.repo / "systemd" / f"{unit}.d" / "90-x.conf"
                said = (f"DRIFT  {unit}: {f} sets {key}=, which the rest of this check reads from systemd/{unit} alone: "
                        "set it there")
                return {"drift": said, "absent": [f"ok     {unit} (loaded"]}
            return prepare

        def strata_start(sb):                        # the same command again: still refused
            start = re.search(r"(?m)^ExecStart=(.*)$", (sb.repo / "systemd" / "strata-q3.service").read_text()).group(1)
            return refused("strata-q3.service", "ExecStart", f"[Service]\nExecStart=\nExecStart={start}\n")(sb)

        def runtime_of_emptied(sb):                  # systemctl set-property of a setting the repo's drop-in empties
            sb.dropin(unit="strata-q3-health.service", name="90-x.conf", text="[Service]\nEnvironment=\n")
            d = "/run/systemd/system.control/strata-q3-health.service.d"
            (sb.sys / d.lstrip("/")).mkdir(parents=True)
            (sb.sys / d.lstrip("/") / "50-Environment.conf").write_text("[Service]\nEnvironment=A=1\n")
            sb.spec["strata-q3-health.service"]["DropInPaths"] += f" {d}/50-Environment.conf"
            sb.save_spec()
            return {"drift": f"{d}/50-Environment.conf (systemctl set-property --runtime) sets Environment otherwise than "
                             "the repo's files", "absent": ["Traceback"]}

        def emptied(sb):                             # loaded empty, said empty: no command, and no drift of one
            sb.dropin(unit="strata-q3-health.service", name="90-x.conf", text="[Service]\nExecStart=\n")
            return {"then": lambda r: self.assertNotIn("ExecStart: the loaded commands", r.stdout)}

        def emptied_but_loaded(sb):                  # said empty, loaded with its command: compared, and drift
            loaded = sb.spec["strata-q3-health.service"]["ExecStart"]
            sb.dropin(unit="strata-q3-health.service", name="90-x.conf", text="[Service]\nExecStart=\n")
            sb.spec["strata-q3-health.service"]["ExecStart"] = loaded
            sb.save_spec()
            return {"drift": "ExecStart: the loaded commands are not the repo's (1 loaded, 0 in the repo; command 1 "
                             "differs)"}
        self.each([
            ("the health check's command, emptied and set again", health("[Service]\nExecStart=\nExecStart={start}\n")),
            ("emptied and set to another", health("[Service]\nExecStart=\nExecStart=/bin/true\n")),
            ("emptied", emptied), ("emptied, but loaded with a command", emptied_but_loaded),
            ("its environment, emptied and set", health("[Service]\nEnvironment=A=1\nEnvironment=\nEnvironment=B=2\n")),
            ("an empty After= empties nothing", health("[Unit]\nAfter=\n")),
            ("an empty After=, and one loaded without the unit's", health(
                "[Unit]\nAfter=\n", "After lacks strata-q3.service", {"After": "basic.target"})),
            ("strata-q3.service's ExecStart, emptied and set again", strata_start),
            ("strata-q3.service's ExecStartPre", refused("strata-q3.service", "ExecStartPre",
                                                         "[Service]\nExecStartPre=/bin/true\n")),
            ("strata-q3.service's WorkingDirectory", refused("strata-q3.service", "WorkingDirectory",
                                                             "[Service]\nWorkingDirectory=/\n")),
            ("strata-q3.service's User", refused("strata-q3.service", "User", "[Service]\nUser=nobody\n")),
            ("strata-q3.service's LimitNOFILE", refused("strata-q3.service", "LimitNOFILE",
                                                        "[Service]\nLimitNOFILE=1024\n")),
            ("strata-q3.service's ProtectHome", refused("strata-q3.service", "ProtectHome",
                                                        "[Service]\nProtectHome=yes\n")),
            *[(f"strata-q3.service's {k}", refused("strata-q3.service", k, f"[Service]\n{k}={v}\n"))
              for k, v in (("Group", "nogroup"), ("LimitMEMLOCK", "64K"), ("OOMScoreAdjust", "0"),
                           ("NoNewPrivileges", "no"), ("ProtectSystem", "strict"), ("PrivateTmp", "yes"))],
            ("strata-q3.service's ExecStartPre, emptied", refused("strata-q3.service", "ExecStartPre",
                                                                  "[Service]\nExecStartPre=\n")),
            ("a runtime copy of a setting the repo emptied", runtime_of_emptied),
            ("zfs-arc-max.service's ExecStart", refused(
                "zfs-arc-max.service", "ExecStart", "[Service]\nExecStart=\nExecStart=/bin/sh -ec 'echo 8589934592 > "
                "/sys/module/zfs/parameters/zfs_arc_max; echo 0 > /sys/module/zfs/parameters/zfs_arc_shrinker_limit'\n")),
        ])
        # and the other way: one the rest of the check does not read from the file, in strata-q3.service's drop-in,
        # is compared as any setting (test_what_systemd_loaded: Restart=, After=)

    def test_values_as_systemd_shows_them(self):
        # systemctl show prints a boolean in any of systemd's spellings as yes or no, a mode as four octal digits, a
        # limit as its hard and soft values, a span in its own units, and a command written with @ as its program and
        # the argv[0] after it; Environment= comes from systemd's bus, item by item, so an item with a blank or a quote
        # (which systemctl show prints as [unprintable], or as it is) compares too.  A real difference is drift
        def unit(old, new, running=False, loaded=None, says=None):
            def prepare(sb):
                sb.edit_unit(lambda t: re.sub(old, new, t, count=1, flags=re.M))
                if loaded:                           # what systemd loaded, if it were otherwise
                    sb.spec["strata-q3.service"].update(loaded)
                    sb.save_spec()
                if running:
                    sb.start_service()
                return {"drift": says, "absent": ["Traceback", "[unprintable]"],
                        "then": (lambda r: self.assertIn("ok     strata-q3.service runs what the files above say",
                                                         r.stdout)) if running and not says else None}
            return prepare

        def health(old, new, loaded=None, says=None):
            def prepare(sb):
                for d in (sb.repo / "systemd", sb.sys / "etc/systemd/system"):
                    f = d / "strata-q3-health.service"
                    f.write_text(re.sub(old, new, f.read_text(), count=1, flags=re.M))
                sb.spec["strata-q3-health.service"] = show_of(sb.repo / "systemd" / "strata-q3-health.service",
                                                              "strata-q3-health.service")
                sb.spec["strata-q3-health.service"].update(loaded or {})
                sb.save_spec()
                sb.commit()
                return {"drift": says}
            return prepare

        def zfs(old, new):                           # zfs-arc-max.service's unit, changed in the repo and installed
            def prepare(sb):
                for d in (sb.repo / "systemd", sb.sys / "etc/systemd/system"):
                    f = d / "zfs-arc-max.service"
                    f.write_text(re.sub(old, new, f.read_text(), count=1, flags=re.M))
                sb.spec["zfs-arc-max.service"] = show_of(sb.repo / "systemd" / "zfs-arc-max.service", "zfs-arc-max.service")
                sb.save_spec()
                sb.commit()
                return {}
            return prepare

        def no_busctl(running):                      # busctl fails: said, not compared, and no traceback
            def prepare(sb):
                (sb.bin / "busctl").write_text("#!/bin/sh\nexit 1\n")
                if running:
                    sb.start_service()
                return {"drift": "DRIFT  strata-q3.service (loaded): Environment: the loaded one cannot be read "
                                 "(OSError: busctl): not compared",
                        "absent": ["Traceback", "which neither its loaded unit nor systemd gives it"],
                        "then": (lambda r: self.assertIn("strata-q3.service's loaded Environment cannot be read "
                                                         "(OSError: busctl): server.py's environment is not compared",
                                                         r.stdout)) if running else None}
            return prepare
        self.each([
            ("NoNewPrivileges=True", unit(r"^NoNewPrivileges=.*$", "NoNewPrivileges=True")),
            ("NoNewPrivileges=y, running", unit(r"^NoNewPrivileges=.*$", "NoNewPrivileges=y", running=True)),
            ("ProtectSystem=0, running", unit(r"^ProtectSystem=no$", "ProtectSystem=0", running=True)),
            ("PrivateTmp=off", unit(r"^PrivateTmp=no$", "PrivateTmp=off")),
            ("ProtectHome=false, running", unit(r"^PrivateTmp=no$", "PrivateTmp=no\nProtectHome=false", running=True)),
            ("NoNewPrivileges=true, loaded no", unit(r"^NoNewPrivileges=.*$", "NoNewPrivileges=true",
                                                     loaded={"NoNewPrivileges": "no"},
                                                     says="NoNewPrivileges is 'no', the repo says 'true'")),
            ("a mode in three digits", health(r"^RuntimeDirectoryMode=0700$", "RuntimeDirectoryMode=700")),
            ("RuntimeDirectoryPreserve=true", health(r"^RuntimeDirectoryPreserve=yes$", "RuntimeDirectoryPreserve=true")),
            ("a mode loaded otherwise", health(r"^RuntimeDirectoryMode=0700$", "RuntimeDirectoryMode=700",
                                               {"RuntimeDirectoryMode": "0755"}, "RuntimeDirectoryMode is '0755'")),
            ("LimitMEMLOCK=64K:infinity", unit(r"^LimitMEMLOCK=.*$", "LimitMEMLOCK=64K:infinity")),
            ("LimitMEMLOCK=64K:infinity, loaded otherwise", unit(
                r"^LimitMEMLOCK=.*$", "LimitMEMLOCK=64K:infinity", loaded={"LimitMEMLOCKSoft": "infinity"},
                says="LimitMEMLOCK is 'infinity:infinity' (soft:hard), the repo says '64K:infinity'")),
            ("LimitMEMLOCK=64K:infinity, loaded with another hard limit", unit(
                r"^LimitMEMLOCK=.*$", "LimitMEMLOCK=64K:infinity", loaded={"LimitMEMLOCK": "65536"},
                says="LimitMEMLOCK is '65536:65536' (soft:hard), the repo says '64K:infinity'")),
            ("TimeoutStopSec=120, shown as 2min", unit(r"^TimeoutStopSec=.*$", "TimeoutStopSec=120",
                                                       loaded={"TimeoutStopUSec": "2min"})),
            ("TimeoutStartSec=1M, shown as 1month", unit(r"^TimeoutStartSec=.*$", "TimeoutStartSec=1M",
                                                         loaded={"TimeoutStartUSec": "1month"})),
            ("TimeoutStopSec=infinity", unit(r"^TimeoutStopSec=.*$", "TimeoutStopSec=infinity")),
            ("ExecStartPre with @", unit(r"^ExecStartPre=(\S+)$", r"ExecStartPre=@\1 strata-preflight")),
            ("ExecStartPre with @, another program loaded", unit(
                r"^ExecStartPre=(\S+)$", r"ExecStartPre=@\1 strata-preflight",
                loaded={"ExecStartPre": ["{ path=/bin/true ; argv[]=strata-preflight ; ignore_errors=no ; "
                                         "start_time=[n/a] ; pid=0 }"]},
                says="ExecStartPre: the loaded commands are not the repo's (1 loaded, 1 in the repo; command 1 differs)")),
            ("Environment with a blank", unit(r"^(Environment=STRATA\S*)$", r'\1' + '\nEnvironment="A=x y"')),
            ("Environment with a blank, running", unit(r"^(Environment=STRATA\S*)$", r'\1' + '\nEnvironment="A=x y"',
                                                       running=True)),
            ("Environment with a quote, running", unit(r"^(Environment=STRATA\S*)$", r'\1' + '\nEnvironment="G=don\'t"',
                                                       running=True)),
            ("Environment with a blank, loaded otherwise", unit(
                r"^(Environment=STRATA\S*)$", r'\1' + '\nEnvironment="A=x y"',
                loaded={"EnvironmentList": ["PYTHONUNBUFFERED=1", "OMP_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1",
                                            "MKL_NUM_THREADS=1", "STRATA_ENGINE_READY_S=1800", "A=x z"]},
                says="Environment differs from the repo's in ['A']")),
            ("Environment with an unbalanced quote", unit(r"^(Environment=STRATA\S*)$", r'\1' + "\nEnvironment=G=don't",
                                                          says="Environment: a line of the repo's that this check "
                                                               "cannot split into words (No closing quotation): not "
                                                               "compared")),
            ("RemainAfterExit=true", zfs(r"^RemainAfterExit=yes$", "RemainAfterExit=true")),
            ("no busctl", no_busctl(False)), ("no busctl, running", no_busctl(True)),
        ])

    def test_a_setting_the_check_cannot_compare_yet(self):
        sb = Sandbox(self)
        sb.edit_unit(lambda t: t.replace("[Service]\n", "[Service]\nCPUQuota=50%\n"))
        self.drift(sb, "CPUQuota= is in the repo's unit, and this check does not compare it yet")

    def test_the_host_units_and_the_config(self):
        def host_units(sb):
            (sb.repo / "scripts" / "host-units.sh").write_text("#!/bin/sh\necho 'DRIFT  a host unit (stub)'\nexit 1\n")
            sb.commit()
            return {"drift": "DRIFT  a host unit (stub)"}

        def live(sb):
            f = sb.strata / "strata-iq3_xxs.json"
            f.write_text(f.read_text() + "\n")
            return {"drift": f"{f} differs from config/strata-iq3_xxs.deployed.json"}

        def no_arguments(sb):
            (sb.repo / "config" / "profile.conf").write_text("# nothing\n")
            sb.commit()
            return {"drift": "config/profile.conf has no arguments"}

        def another_model(sb):
            f = sb.repo / "config" / "profile.conf"
            if "--model iq3_s" not in f.read_text():
                raise AssertionError("config/profile.conf has no --model iq3_s")
            f.write_text(f.read_text().replace("--model iq3_s", "--model iq3_xxs"))
            sb.commit()
            return {"drift": "configure_strata_q3.py with config/profile.conf no longer makes the deployed snapshot"}

        def no_config(sb):
            f = sb.repo / "systemd" / "strata-q3.service"
            f.write_text(re.sub(r" --config \S+", "", f.read_text()))
            sb.commit()
            return {"drift": "systemd/strata-q3.service names no --config"}

        def no_preflight(sb):
            f = sb.repo / "systemd" / "strata-q3.service"
            f.write_text(f.read_text().replace("/scripts/strata-preflight.sh", "/scripts/other.sh"))
            sb.commit()
            return {"drift": "systemd/strata-q3.service runs no .../scripts/strata-preflight.sh"}
        def lookalikes(sb):                          # the live config and the checkout are read from ExecStart= and
            sb.edit_unit(lambda t: t.replace(        # ExecStartPre= lines: not from a comment, nor from another key
                "\nExecStartPre=", "\n# ExecStart=/x --config /nonexistent/old.json\n"
                "; ExecStartPre=/nonexistent/old/scripts/strata-preflight.sh\n"
                "#ExecStartPre=/nonexistent/old/scripts/strata-preflight.sh\n"
                "ExecStartPre=/bin/true --config /nonexistent/other.json\nExecStartPre=", 1))
            return {"then": lambda r: self.assertIn(f"ok     {sb.strata / 'strata-iq3_xxs.json'}\n", r.stdout)}

        def blank_before_prefix(sb):                 # blanks before a prefix: systemd strips them
            sb.edit_unit(lambda t: t.replace("\nExecStartPre=", "\nExecStartPre=  -", 1))
            return {}

        def not_json(sb):                            # a live config that is no JSON: it differs, and has no key
            (sb.strata / "strata-iq3_xxs.json").write_text("not JSON\n")
            return {"drift": f"DRIFT  {sb.strata / 'strata-iq3_xxs.json'} differs from config/strata-iq3_xxs.deployed.json\n",
                    "absent": ["api_key set", "Traceback"]}
        def crlf_profile(sb):                        # CRLF line ends: read as strata-preflight.sh reads them
            f = sb.repo / "config" / "profile.conf"
            f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
            sb.commit()
            return {"then": lambda r: self.assertIn("ok     setup's config + configure_strata_q3.py", r.stdout)}

        def empty_key(sb):                           # an empty api_key is no key: compared byte for byte, and hashed
            live = sb.strata / "strata-iq3_xxs.json"
            live.write_text(json.dumps({**json.loads(live.read_text()), "api_key": ""}, indent=4) + "\n")
            return {"drift": f"DRIFT  {live} differs from config/strata-iq3_xxs.deployed.json\n",
                    "absent": ["api_key set", "api_key ignored"]}
        self.each([(f.__name__, f) for f in (host_units, live, no_arguments, another_model, no_config, no_preflight,
                                             lookalikes, blank_before_prefix, not_json, crlf_profile, empty_key)])

    def test_an_api_key_in_the_live_config(self):
        # compared without the key, which is never printed; equal JSON text, so 8080.0 is not 8080
        sb = Sandbox(self)
        live = sb.strata / "strata-iq3_xxs.json"
        cfg = json.loads(live.read_text())
        live.write_text(json.dumps({**cfg, "api_key": "k-not-printed"}, indent=4) + "\n")
        r = self.clean(sb)
        self.assertIn(f"ok     {live} (api_key set: compared without it)", r.stdout)
        self.assertIn("skip   strata/strata-iq3_xxs.json (api_key set: checked above)", r.stdout)
        self.assertNotIn("k-not-printed", r.stdout + r.stderr)
        live.write_text(json.dumps({**cfg, "api_key": "k-not-printed", "port": float(cfg["port"])}, indent=4) + "\n")
        self.drift(sb, f"{live} differs from config/strata-iq3_xxs.deployed.json (api_key ignored)")
        live.write_text(json.dumps({"api_key": "k-not-printed", **dict(reversed(list(cfg.items())))}, indent=4) + "\n")
        self.clean(sb)                                  # its keys in another order: the same settings

    def test_the_list(self):
        def changed(sb):
            (sb.dev / "Strata-data/packs/p/index.txt").write_text("changed\n")
            return {"drift": "Strata-data/packs/p/index.txt (sha256 changed"}

        def outside(sb):
            sb.abs_file.write_text("another library\n")
            return {"drift": f"{sb.abs_file} (sha256 changed"}

        def full(sb):
            return {"drift": "Strata-data/packs/p/experts.bin (sha256 changed", "args": ("--full",)}

        def missing(sb):
            sb.write_list(extra=f"{'1' * 64}  Strata-data/missing.bin\n")
            sb.commit()
            return {"drift": "Strata-data/missing.bin is missing"}

        def live_config(sb):                         # without an api_key, the live config is hashed as listed too
            f = sb.repo / "config" / "deployed-artifacts.sha256"
            f.write_text(f.read_text().replace(sha(sb.strata / "strata-iq3_xxs.json"), "0" * 64))
            sb.commit()
            return {"drift": "strata/strata-iq3_xxs.json (sha256 changed"}

        def one_space(sb):                           # the list's lines: two spaces between the sum and the path
            index = sb.dev / "Strata-data/packs/p/index.txt"
            sb.write_list(extra=f"{sha(index)} {index.relative_to(sb.dev)}\n")
            sb.commit()
            return {"drift": f"not a '<sha256>  <path>' line: {sha(index)} {index.relative_to(sb.dev)}"}
        self.each([(f.__name__, f) for f in (changed, outside, full, missing, live_config, one_space)])

    def test_a_list_it_cannot_read(self):
        def short(sb):
            sb.write_list(extra="0123  Strata-data/short.bin\n")
            sb.commit()
            return {"drift": "not a '<sha256>  <path>' line: 0123  Strata-data/short.bin"}

        def empty(sb):
            (sb.repo / "config" / "deployed-artifacts.sha256").write_text(f"# strata source pin: {sb.pin}\n")
            sb.commit()
            return {"drift": "config/deployed-artifacts.sha256 lists no files"}

        def no_pin(sb):
            f = sb.repo / "config" / "deployed-artifacts.sha256"
            f.write_text(re.sub(r"(?m)^# strata source pin: .*\n", "", f.read_text()))
            sb.commit()
            return {"drift": "has no '# strata source pin: <commit>' line"}
        self.each([(f.__name__, f) for f in (short, empty, no_pin)])

    def test_the_strata_tree_file_by_file(self):
        server = lambda sb: sb.strata / "serve" / "server.py"                      # noqa: E731

        def other_pin(sb):
            sb.write_list(pin="0" * 40)
            sb.commit()
            return {"drift": f"is at {sb.pin}, the list pins {'0' * 40}"}

        def one(change, says):
            def prepare(sb):
                change(sb)
                return {"drift": f"{sb.strata} differs from the pinned commit in 1 files: {says}"}
            return prepare

        def stored(sb, attributes, name):               # a file that git stores with LF, here with CRLF
            (sb.strata / ".gitattributes").write_text(attributes)
            (sb.strata / name).write_text("@echo off\r\nexit\r\n")
            sb.commit_strata(".gitattributes", name)
            sb.write_list()
            sb.commit()
            if subprocess.run(sb.git[:3] + ["cat-file", "-p", f"HEAD:{name}"], capture_output=True).stdout != \
                    b"@echo off\nexit\n":
                raise AssertionError(f"git does not store {name} with LF")

        def eol(sb):                                    # eol= alone makes a file text too
            stored(sb, "*.cmd eol=crlf\n", "run.cmd")
            return {}

        def text(sb):                                   # a text file with CRLF line ends that git stores with LF
            stored(sb, "*.bat text eol=crlf\n*.bin filter=strata-test-undefined\n", "run.bat")
            return {}

        def filtered(sb):                               # a filter git would run is not followed
            stored(sb, "*.bat text eol=crlf\n*.bin filter=strata-test-undefined\n", "run.bat")
            (sb.strata / "x.bin").write_bytes(b"x")
            sb.commit_strata("x.bin")
            sb.write_list()
            sb.commit()
            return {"drift": "x.bin has git attributes this check does not follow (filter)"}
        self.each([
            ("another pin", other_pin),
            ("changed", one(lambda sb: server(sb).write_text("# changed\n"), "serve/server.py differs")),
            ("changed and hidden from git status",
             one(lambda sb: (subprocess.run(["git", "-C", str(sb.strata), "update-index", "--skip-worktree",
                                             "serve/server.py"], check=True),
                             server(sb).write_text("# changed and hidden\n")), "serve/server.py differs")),
            ("missing", one(lambda sb: server(sb).unlink(), "serve/server.py is missing")),
            ("executable", one(lambda sb: server(sb).chmod(0o755), "serve/server.py: its executable bit differs")),
            ("a link to the same text", one(lambda sb: (shutil.copy2(server(sb), sb.d / "s.py"), server(sb).unlink(),
                                                        os.symlink(sb.d / "s.py", server(sb))),
                                            "serve/server.py is a link here")),
            ("CRLF line ends without a text attribute",
             one(lambda sb: server(sb).write_bytes(server(sb).read_bytes().replace(b"\n", b"\r\n")),
                 "serve/server.py differs")),
            ("eol= alone", eol), ("text eol=crlf", text), ("a filter", filtered)])

    def test_modules_where_server_py_imports_from(self):
        ext = importlib.machinery.EXTENSION_SUFFIXES[0]

        def module(where):
            def prepare(sb):
                (sb.strata / where).parent.mkdir(parents=True, exist_ok=True)
                (sb.strata / where).write_text("raise SystemExit\n")
                return {"drift": f"has modules where server.py imports from that the pinned commit does not have and the "
                                 f"list does not name: {where}"}
            return prepare

        def ignored(sb):                                  # also when .gitignore hides it
            (sb.strata / "tools").mkdir()
            (sb.strata / "tools" / "re.py").write_text("raise SystemExit\n")
            with open(sb.strata / ".gitignore", "a") as f:
                f.write("tools/\n")
            return {"drift": "tools/re.py"}

        def package(sb, helper=False):                    # a package the commit has: a module beside its __init__.py
            (sb.strata / "serve" / "lib").mkdir()
            (sb.strata / "serve" / "lib" / "__init__.py").write_text("")
            sb.commit_strata("serve/lib/__init__.py")
            sb.write_list()
            sb.commit()
            py_compile.compile(str(sb.strata / "serve" / "lib" / "__init__.py"), doraise=True)  # its __init__'s bytecode
            if not helper:
                return {}
            (sb.strata / "serve" / "lib" / f"helper{ext}").write_text("")
            return {"drift": f"serve/lib/helper{ext}"}

        def never_imported(sb):                           # what Python never imports from there
            (sb.strata / "notes.txt").write_text("x\n")
            (sb.strata / "data").mkdir()                  # a directory of the commit's, which is no package
            (sb.strata / "data" / "README").write_text("data\n")
            sb.commit_strata("data/README")
            sb.write_list()
            sb.commit()
            (sb.strata / "data" / "x.py").write_text("raise SystemExit\n")
            (sb.strata / "serve" / "__pycache__").mkdir()
            (sb.strata / "serve" / "__pycache__" / "json.cpython-310.pyc").write_bytes(b"x")    # no source: never run
            return {}

        def listed(sb, changed=False):                    # one the list names (and so pins) is allowed
            old = sb.strata / "configure_old.py"
            old.write_text("# an old copy\n")
            sb.write_list(extra=sb.listed(old))
            sb.commit()
            if not changed:
                return {}
            old.write_text("# changed\n")
            return {"drift": "strata/configure_old.py (sha256 changed"}
        self.each([(w, module(w)) for w in ("serve/json.py", "tools/re.py", "jsonx.py", "serve/http/__init__.py",
                                            f"serve/json{ext}", "tools/re.pyc", f"tools/pkg/__init__{ext}")]
                  + [("ignored", ignored), ("a package of the commit", package),
                     ("a module beside its __init__.py", lambda sb: package(sb, helper=True)),
                     ("never imported", never_imported), ("listed", listed),
                     ("listed, changed", lambda sb: listed(sb, changed=True))])

    def test_the_bytecode_python_would_run(self):
        def cached(sb, src: Path) -> Path:
            d = src.parent / "__pycache__"
            d.mkdir(exist_ok=True)
            return d / f"{src.stem}.{TAG}.pyc"
        src = lambda sb: sb.strata / "serve" / "server.py"                          # noqa: E731

        def pythons(sb):                                  # what Python itself writes
            py_compile.compile(str(src(sb)), cfile=str(cached(sb, src(sb))), doraise=True)
            return {}

        def header(data, says):
            def prepare(sb):
                cached(sb, src(sb)).write_bytes(data(sb))
                return {"drift": f"serve/__pycache__/server.{TAG}.pyc "} if says else {}
            return prepare

        def elsewhere(where):                             # tools/ and the root, which are on its import path too
            def prepare(sb):
                f = sb.strata / where
                f.parent.mkdir(exist_ok=True)
                f.write_text("x = 1\n")
                sb.commit_strata(where)
                sb.write_list()
                sb.commit()
                cached(sb, f).write_bytes(pyc_of(f, "raise SystemExit"))
                return {"drift": f"{os.path.dirname(where) + '/' if '/' in where else ''}__pycache__/{f.stem}.{TAG}.pyc "
                                 "is not what its source compiles to"}
            return prepare

        def venv(sb):                                     # the venv's
            cached(sb, sb.site / "pkg" / "mod.py").write_bytes(pyc_of(sb.site / "pkg" / "mod.py", "x = 2"))
            return {"drift": f".venv: lib/python{sys.version_info[0]}.{sys.version_info[1]}/site-packages/pkg/__pycache__/"
                             f"mod.{TAG}.pyc is not what its source compiles to"}
        self.each([("what Python writes", pythons)] + [(name, header(data, says)) for name, data, says in [
            ("by time and size", lambda sb: pyc_of(src(sb), "raise SystemExit"), True),
            ("by hash, checked", lambda sb: pyc_of(src(sb), "raise SystemExit", flags=0b11), True),
            ("by hash, unchecked", lambda sb: pyc_of(src(sb), "raise SystemExit", flags=0b01), True),
            ("by hash, unchecked, another hash", lambda sb: pyc_of(src(sb), "raise SystemExit", flags=0b01, wrong_hash=True), True),
            ("by hash, checked, another hash", lambda sb: pyc_of(src(sb), "raise SystemExit", flags=0b11, wrong_hash=True), False),
            ("not marshal data", lambda sb: pyc_of(src(sb), "")[:16] + b"\xff\xff", True),
            ("another time", lambda sb: pyc_of(src(sb), "raise SystemExit", mtime=1), False),
            ("another magic number", lambda sb: b"\0\0\0\0" + pyc_of(src(sb), "raise SystemExit")[4:], False),
            ("flags Python does not know", lambda sb: pyc_of(src(sb), "raise SystemExit", flags=0b100), False),
            ("another size", lambda sb: (lambda d: d[:12] + (src(sb).stat().st_size + 1).to_bytes(4, "little") + d[16:])(
                pyc_of(src(sb), "raise SystemExit")), False),
        ]] + [(w, elsewhere(w)) for w in ("tools/helper.py", "rootmod.py")] + [("the venv's", venv)])

    def test_the_venv(self):
        cases = [
            (lambda sb: (sb.site / "pkg" / "mod.py").write_text("x = 2\n"), "pkg/mod.py differs from its RECORD"),
            (lambda sb: (sb.site / "pkg" / "mod.py").unlink(), "pkg/mod.py is missing"),
            (lambda sb: (sb.site / "sitecustomize.py").write_text("import os\n"), "sitecustomize.py was installed by no package"),
            (lambda sb: (sb.site / "x.pth").write_text("import os\n"), "x.pth was installed by no package"),
            (lambda sb: (sb.site / "pkg" / "__pycache__" / "extra.py").write_text(""), "pkg/__pycache__/extra.py was installed by no package"),
            (lambda sb: (sb.repo / "config" / "strata-venv.txt").write_text(f"pkg==1.1  {sha(sb.site / 'pkg-1.0.dist-info' / 'RECORD')}\n"),
             ".venv does not hold the packages config/strata-venv.txt lists, as it pins their RECORDs: the venv has "
             "['pkg==1.0  "),
            (lambda sb: (sb.repo / "config" / "strata-venv.txt").write_text("pkg==1.0\n"),
             "config/strata-venv.txt: not a '<name>==<version>  <sha256 of its RECORD>' line: pkg==1.0"),
            (lambda sb: (sb.site / "pkg-1.0.dist-info" / "RECORD").unlink(), "pkg-1.0.dist-info cannot be read (FileNotFoundError)"),
            (lambda sb: shutil.copytree(sb.site, sb.site.parent.parent / "python3.99" / "site-packages"),
             ".venv has 2 site-packages directories, not one"),
            # what Debian's python also reads in a venv, and what wins over its pyvenv.cfg
            (lambda sb: (sb.site.parent / "dist-packages").mkdir() or (sb.site.parent / "dist-packages" / "x.pth").write_text("import os\n"),
             "dist-packages/x.pth was installed by no package"),
            (lambda sb: (sb.strata / ".venv" / "local" / "lib").mkdir(parents=True) or
             (sb.strata / ".venv" / "local" / "lib" / "x.pth").write_text("import os\n"), "local/lib/x.pth was installed by no package"),
            (lambda sb: (sb.strata / ".venv" / "bin" / "pyvenv.cfg").write_text("include-system-site-packages = true\n"),
             "bin/pyvenv.cfg was installed by no package"),
            (lambda sb: (sb.strata / ".venv" / "bin" / "pkg").write_text("#!/usr/bin/python3\nimport os\n"),
             "bin/pkg differs from its RECORD"),
            (lambda sb: (sb.d / "outside").mkdir() or os.symlink(sb.d / "outside", sb.site / "addon"), "addon (a link)"),
            (lambda sb: (sb.strata / ".venv" / "bin" / "python").unlink() or os.symlink(sys.executable, sb.strata / ".venv" / "bin" / "python"),
             "bin/python (a link)"),
        ]

        def case(change, says):
            def prepare(sb):
                change(sb)
                sb.commit()
                return {"drift": says}
            return prepare

        def copied_python(sb):                          # its python, a copy that others than root can change
            py = sb.d / "py" / "python3"
            py.parent.mkdir()
            shutil.copy2(os.path.realpath(sys.executable), py)
            (sb.strata / ".venv" / "bin" / "python").unlink()
            os.symlink(py, sb.strata / ".venv" / "bin" / "python")
            return {"drift": f"{sb.strata}/.venv/bin/python is not the system's python: it resolves to {py}",
                    "then": lambda r: self.assertIn(f"this check's python ({os.path.realpath(sys.executable)}) is not the "
                                                    f"service's ({py})", r.stdout)}

        def activated(sb):                              # this check run by a venv's python (an activated venv)
            act = sb.d / "act"
            (act / "bin").mkdir(parents=True)
            (act / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\ninclude-system-site-packages = false\n")
            os.symlink(sys.executable, act / "bin" / "python3")
            return {"drift": f"python3 here is a venv's ({act}/bin/python3): run this check with the system's python3",
                    "env": {"PATH": f"{act / 'bin'}:{sb.bin}:{os.environ['PATH']}"}}

        def shared(sb):
            (sb.strata / "strata-iq3_xxs.shared-settings.json").write_text('{"temperature": 0.1}')
            return {"drift": "strata-iq3_xxs.shared-settings.json exists"}

        def shared_link(sb):                            # a link there too, also one that leads nowhere
            os.symlink("nowhere.json", sb.strata / "strata-iq3_xxs.shared-settings.json")
            return {"drift": "strata-iq3_xxs.shared-settings.json exists"}
        self.each([(says, case(change, says)) for change, says in cases]
                  + [(f.__name__, f) for f in (copied_python, activated, shared, shared_link)])

    def test_the_services_checkout(self):
        def preflight(sb):
            (sb.repo / "scripts" / "strata-preflight.sh").write_text("exit 0\n")
            return {"drift": f"{sb.repo} has local changes: scripts/strata-preflight.sh is not in its commit"}

        def assumed(sb):
            f = sb.repo / "config" / "profile.conf"
            f.write_text(f.read_text() + "\n")
            subprocess.run(["git", "-C", str(sb.repo), "update-index", "--assume-unchanged", "config/profile.conf"],
                           check=True)
            return {"drift": f"{sb.repo} has local changes: config/profile.conf differs"}

        def ignored(where, content="raise SystemExit\n"):  # an ignored file where the service runs code from
            def prepare(sb):
                (sb.repo / ".gitignore").write_text(f"{where}\n__pycache__/\nresults/*.data\n")
                sb.commit()
                (sb.repo / where).write_text(content)
                return {"drift": f"{where} is ignored, but where the service runs code from"}
            return prepare

        def ignored_elsewhere(sb):
            (sb.repo / ".gitignore").write_text("results/*.data\n__pycache__/\n")
            sb.commit()
            (sb.repo / "results").mkdir()
            (sb.repo / "results" / "x.data").write_text("")
            (sb.repo / "tools" / "__pycache__").mkdir()
            (sb.repo / "tools" / "__pycache__" / "x.pyc").write_text("")
            return {}

        def linked(sb):                                 # a linked directory
            (sb.d / "elsewhere-tools").mkdir()
            os.symlink(sb.d / "elsewhere-tools", sb.repo / "tools2")
            return {"drift": f"{sb.repo} has local changes: tools2 is not in its commit"}

        def exported(sb):                               # an exported tree: no commit to tie it to
            shutil.rmtree(sb.repo / ".git")
            return {"drift": f"{sb.repo} is not the top of a git checkout of its own"}

        # run from another clone: the checkout the units name is the one compared, and the two at one commit
        def clone(sb):
            other = sb.d / "other"
            subprocess.run(["git", "clone", "-q", str(sb.repo), str(other)], check=True)
            return other

        def live_changed(sb):
            other = clone(sb)
            (sb.repo / "scripts" / "strata-preflight.sh").write_text("exit 0\n")
            return {"drift": f"DRIFT  {sb.repo} has local changes: scripts/strata-preflight.sh is not in its commit",
                    "notes": [f"this check runs from {other}, which the service does not run (at the same commit)"],
                    "script": other / "scripts" / "check-drift.sh"}

        def other_changed(sb):
            other = clone(sb)
            (other / "x").write_text("")
            return {"drift": f"DRIFT  {other} has local changes: x is not in its commit",
                    "script": other / "scripts" / "check-drift.sh"}

        def other_commit(sb, candidate=False):
            other = clone(sb)
            subprocess.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                            "--allow-empty", "-m", "next"], check=True)
            if candidate:
                return {"args": ("--candidate",), "script": other / "scripts" / "check-drift.sh",
                        "notes": [f"this check runs from {other}, which the service does not run: --candidate"]}
            return {"drift": f"DRIFT  this check runs from {other}, at another commit than the service's checkout "
                             f"{sb.repo}", "script": other / "scripts" / "check-drift.sh"}

        def worktree(sb):                               # a worktree of the service's checkout, whose .git is a file
            wt = sb.d / "wt"
            subprocess.run(["git", "-C", str(sb.repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
            return {"script": wt / "scripts" / "check-drift.sh",
                    "notes": [f"this check runs from {wt}, which the service does not run (at the same commit)"]}
        self.each([("preflight", preflight), ("assumed unchanged", assumed), ("tools/json.py", ignored("tools/json.py")),
                   ("scripts/x.sh", ignored("scripts/x.sh", "exit 0\n")),
                   ("sitecustomize.py", ignored("sitecustomize.py", "exit 0\n")),
                   ("a drop-in under systemd/", ignored("systemd/user-1003.slice.d/90-local.conf",      # installed by
                                                        "[Slice]\nMemoryMax=8G\n")),           # install-system-files.sh
                   ("ignored elsewhere", ignored_elsewhere), ("linked", linked), ("exported", exported),
                   ("another clone, the live one changed", live_changed), ("another clone, changed", other_changed),
                   ("another clone, another commit", other_commit),
                   ("another clone, another commit, --candidate", lambda sb: other_commit(sb, candidate=True)),
                   ("a worktree", worktree)])

    def test_it_writes_no_index(self):
        # git status refreshes a checkout's index when a file's times changed; this check is read-only
        sb = Sandbox(self)
        indexes = [sb.strata / ".git" / "index", sb.repo / ".git" / "index"]
        before = [i.stat().st_mtime_ns for i in indexes]
        time.sleep(0.05)
        for f in (sb.strata / "serve" / "server.py", sb.repo / "config" / "profile.conf"):
            os.utime(f)                               # the same content, a new time
        self.clean(sb)
        self.assertEqual([i.stat().st_mtime_ns for i in indexes], before)

    def test_the_service_managers_environment(self):
        def manager(change, says, cwd=None):
            def prepare(sb):
                change(sb, sb.spec)
                sb.save_spec()
                return {"drift": says(sb) if callable(says) else says, "cwd": cwd} if says else {}
            return prepare

        def path(entry):
            return lambda sb, spec: spec["__manager__"].__setitem__("PATH", f"{MANAGER_PATH}:{entry(sb)}")

        def not_root(entry):
            return f"the service manager's PATH has {entry}, which is not absolute or which others than root can change"
        self.each([
            ("set-environment", manager(lambda sb, spec: spec["__manager__"].__setitem__("CUDA_LAUNCH_BLOCKING", "1"),
                                        "the service manager gives every service ['CUDA_LAUNCH_BLOCKING']")),
            ("unreadable", manager(lambda sb, spec: spec.__setitem__("__manager__", None),
                                   "systemctl show-environment cannot be read")),
            ("this user's", manager(path(lambda sb: sb.d), lambda sb: not_root(sb.d))),
            ("bin", manager(path(lambda sb: "bin"), not_root("bin"))),
            ("an empty entry", manager(path(lambda sb: ""), not_root("(an empty entry)"))),
            ("not there, and only root can make it", manager(path(lambda sb: "/usr/no-such-dir"), None)),
            ("a name that only begins as one it allows",
             manager(lambda sb, spec: spec["__manager__"].__setitem__("PATHX", "1"),
                     "the service manager gives every service ['PATHX']")),
            ("a name that only begins as LANG",
             manager(lambda sb, spec: spec["__manager__"].__setitem__("LANGX", "1"),
                     "the service manager gives every service ['LANGX']")),
            ("root's, but anyone may write to it", manager(path(lambda sb: "/tmp"), not_root("/tmp"))),
            ("relative, run from /", manager(path(lambda sb: "bin"), "the service manager's PATH has bin, which is not "
                                                                    "absolute", cwd="/")),
        ])

    def test_the_server_process(self):
        u = Sandbox(self).unit()
        memlock = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]

        def started(kw, says):
            def prepare(sb):
                sb.start_service(**kw(sb))
                return {"drift": says} if says else {}
            return prepare
        cases = [
            ("as its unit says", started(lambda sb: {}, None)),
            ("another command line", started(lambda sb: dict(argv=shlex.split(sb.unit()["ExecStart"]) + ["--host", "127.0.0.1"]),
                                             "server.py's command line is not its unit's ExecStart (from word 9): restart pending")),
            ("another directory", started(lambda sb: dict(cwd=Path(tempfile.gettempdir())),
                                          "server.py runs in another directory than its unit's WorkingDirectory")),
            ("LimitNOFILE", started(lambda sb: dict(nofile=resource.getrlimit(resource.RLIMIT_NOFILE)[0] - 1),
                                    "server.py's LimitNOFILE is not its unit's")),
            ("LimitMEMLOCK", started(lambda sb: dict(memlock=memlock - 1 if memlock != resource.RLIM_INFINITY else 1 << 20),
                                     "server.py's LimitMEMLOCK is not its unit's")),
        ]
        if int(u["OOMScoreAdjust"]) < 1000:              # raised, as anyone may
            cases.append(("OOMScoreAdjust", started(lambda sb: dict(oom=int(u["OOMScoreAdjust"]) + 1),
                                                    "server.py's OOMScoreAdjust is not its unit's")))
        if "NoNewPrivs:\t0" in Path("/proc/self/status").read_text():
            cases.append(("NoNewPrivileges", started(lambda sb: dict(no_new_privs=False),
                                                     "server.py's NoNewPrivileges is not its unit's")))

        def as_root(sb):                                 # the unit runs it as root
            sb.edit_unit(lambda t: re.sub(r"(?m)^User=.*$", "User=root", re.sub(r"(?m)^Group=.*$", "Group=root", t)))
            sb.start_service()
            return {"drift": "server.py runs as another user than its unit's User: restart pending",
                    "then": lambda r: self.assertIn("server.py runs as another group than its unit's Group: restart pending",
                                                    r.stdout)}

        def option(change):                              # an option the check does not follow
            def prepare(sb):
                sb.edit_unit(lambda t: t.replace("--port 8080", change))
                sb.start_service()
                return {"drift": "server.py's command line has options this check does not follow, or no --engine strata"}
            return prepare

        def own_python(sb):
            py = sb.d / "py" / "python3"
            py.parent.mkdir()
            shutil.copy2(os.path.realpath(sys.executable), py)
            return py

        def replaced(sb):                                # its program replaced after the start
            py = own_python(sb)
            (sb.strata / ".venv" / "bin" / "python").unlink()
            os.symlink(py, sb.strata / ".venv" / "bin" / "python")
            sb.start_service()
            shutil.copy2(py, py.with_suffix(".new"))
            os.replace(py.with_suffix(".new"), py)
            return {"drift": f"the program server.py runs ({py}) was replaced on disk after it started: restart pending"}

        def another_program(sb):                         # another program than the one its unit names
            sb.start_service(program=str(own_python(sb)))
            return {"drift": "server.py runs another program than its unit's ExecStart names: restart pending"}

        def specifier(sb):                               # a specifier in its ExecStart, which this check does not expand
            sb.edit_unit(lambda t: t.replace("--port 8080", "--port 8080 --host %H"))
            sb.start_service()
            return {"drift": "strata-q3.service's ExecStart has a specifier, a variable or an escape"}

        def escape(sb):                                  # an escape, which systemd reads otherwise than shlex
            sb.edit_unit(lambda t: t.replace("--port 8080", "--port 8080 --host 127.0.0.\\061"))
            sb.start_service()
            return {"drift": "strata-q3.service's ExecStart has a specifier, a variable or an escape"}
        def host(sb):                                    # --host, which the check follows
            sb.edit_unit(lambda t: t.replace("--port 8080", "--port 8080 --host 127.0.0.1"))
            sb.start_service()
            return {}
        self.each(cases + [("--host", host), ("as root", as_root), ("--lazy", option("--port 8080 --lazy")),
                           ("--engine mock", option("--port 8080 --engine mock")), ("replaced", replaced),
                           ("another program", another_program), ("a specifier", specifier), ("an escape", escape)])

    def test_the_running_service(self):
        def started(says, **kw):
            def prepare(sb):
                sb.start_service(**kw)
                return {"drift": says}
            return prepare

        def ready(sb):                               # its value is not printed (one no hash or time can hold)
            sb.start_service(server_env={"STRATA_ENGINE_READY_S": "900.5-not-printed"})
            return {"drift": "server.py's ['STRATA_ENGINE_READY_S'] are not what its loaded unit and the service manager "
                             "give it: restart pending", "absent": ["900.5-not-printed"]}

        def manager(sb):
            sb.spec["__manager__"]["CUDA_LAUNCH_BLOCKING"] = "1"
            sb.save_spec()
            sb.start_service()
            return {"drift": "the service manager gives every service ['CUDA_LAUNCH_BLOCKING']"}

        def manager_later(sb):                           # what the manager gives every service changed after the start
            sb.start_service()
            sb.spec["__manager__"]["LANG"] = "C"
            sb.save_spec()
            return {"drift": "server.py's ['LANG'] are not what its loaded unit and the service manager give it: "
                             "restart pending"}

        def stray(sb):                                   # another engine, not server.py's
            sb.start_service()
            p = subprocess.Popen([str(sb.exe), "--serve"], start_new_session=True)
            self.addCleanup(lambda: (p.kill(), p.wait()))
            return {"drift": f"another engine runs (pids {p.pid}): strata-preflight.sh refuses a start while it does"}

        def binary_replaced(sb):                         # the binary replaced after the start (and the list with it)
            sb.start_service()
            tmp = sb.exe.with_suffix(".new")
            shutil.copy2(sb.exe, tmp)
            with open(tmp, "ab") as f:
                f.write(b"\0")
            os.replace(tmp, sb.exe)
            sb.write_list()
            sb.commit()
            return {"drift": "the running engine's binary has sha256",
                    "notes": ["the engine runs a binary that was replaced on disk after it started"]}

        def config_edited(sb):                           # the live config edited after the start (and the snapshot)
            sb.start_service()
            for f in (sb.strata / "strata-iq3_xxs.json", sb.repo / "config" / "strata-iq3_xxs.deployed.json"):
                cfg = json.loads(f.read_text())
                cfg["args"][cfg["args"].index("--prefill") + 1] = "1024"
                f.write_text(json.dumps(cfg, indent=4) + "\n")
            sb.commit()
            return {"drift": "the engine's command line is not what server.py makes of the live config (from word",
                    "then": lambda r: self.assertIn(f"1 files the service read at its start changed after it: restart "
                                                    f"pending ({sb.strata}/strata-iq3_xxs.json)", r.stdout)}
        self.each([
            ("2 engines", started("server.py has 2 engine processes, not 1", engines=2)),
            ("a variable of its unit's", ready),
            ("variables of no one's", started("server.py has ['PYTHONPATH', 'STRATA_X'], which neither its loaded unit nor "
                                              "systemd gives it: restart pending",
                                              server_env={"PYTHONPATH": "/nonexistent", "STRATA_X": "1"})),
            ("the manager's", manager),
            ("shared settings", started("the server has shared settings (GET /settings)",
                                        shared={"shared": True, "defaults": {"temperature": 0.1}})),
            ("defaults alone", started("the server has shared settings (GET /settings)",
                                       shared={"shared": False, "defaults": {"temperature": 0.1}})),
            ("a word more for the engine", started("the engine's command line is not what server.py makes of the live "
                                                   "config (from word", engine_args=["--extra"])),
            ("a variable of the engine's only", started("the engine's environment is not what server.py makes of the live "
                                                        "config, in ['STRATA_ENGINE_ONLY']",
                                                        engine_env={"STRATA_ENGINE_ONLY": "1"})),
            ("the manager's, later", manager_later), ("a stray engine", stray), ("the binary replaced", binary_replaced),
            ("the config edited", config_edited)])

    def test_files_changed_after_the_start(self):
        stamp = "%a %Y-%m-%d %H:%M:%S.%f UTC"
        server = lambda sb: sb.strata / "serve" / "server.py"                    # noqa: E731

        def restart(*paths):
            return (f"{len(paths)} files the service read at its start changed after it: restart pending "
                    f"({', '.join(map(str, paths))})")

        def touched(where):                                  # the same content, a new time
            def prepare(sb):
                sb.start_service()
                os.utime(where(sb))
                return {"drift": restart(where(sb))}
            return prepare

        def record(sb):                                      # a record that the service does not read
            rec = sb.dev / "Strata-data" / "packs" / "p" / "made.json"
            rec.write_text("{}\n")
            sb.write_list(extra="# how the pack was made (not read at start)\n" + sb.listed(rec))
            sb.commit()
            sb.start_service()
            os.utime(rec)
            return {}

        def after_the_records(sb):                           # after the records, a heading of files read at start
            rec = sb.dev / "Strata-data" / "packs" / "p" / "made.json"
            rec.write_text("{}\n")
            shard = sb.dev / "Strata-data" / "packs" / "p" / "shard.bin"
            shard.write_text("weights\n")
            sb.write_list(extra="# records (not read at start)\n" + sb.listed(rec) + "# shards\n" + sb.listed(shard))
            sb.commit()
            sb.start_service()
            os.utime(shard)
            return {"drift": restart(shard)}

        def module(where):                                   # tools/ and the root's modules, which it imports
            def prepare(sb):
                (sb.strata / where).parent.mkdir(exist_ok=True)
                (sb.strata / where).write_text("x = 1\n")
                sb.commit_strata(where)
                sb.write_list()
                sb.commit()
                sb.start_service()
                os.utime(sb.strata / where)
                return {"drift": restart(sb.strata / where)}
            return prepare

        def start_script(sb):                                # a start script of the checkout: the next start's
            sb.start_service()
            os.utime(sb.repo / "config" / "profile.conf")
            return {"notes": [f"{sb.repo}/config/profile.conf changed after strata-q3.service started: it takes effect "
                              "at the next start"]}

        def wait_ready(sb):                                  # strata-wait-ready.sh, the unit's ExecStartPost
            f = sb.repo / "scripts" / "strata-wait-ready.sh"
            f.write_text("#!/bin/sh\nexit 0\n")
            sb.commit()
            sb.start_service()
            os.utime(f)
            return {"notes": [f"{f} changed after strata-q3.service started: it takes effect at the next start"]}

        # its start: the earlier of its main process's start by the boot clock, which counts a suspend too, and of the
        # stamp that systemd wrote then by the wall clock.  A clock set back after the start makes the stamp later; one
        # stepped forward after it makes the boot clock's start later by the wall clock: a change after the start,
        # though before what one of them says, is one after it
        def set_back(sb):
            sb.start_service()
            os.utime(server(sb))
            s = sb.spec["strata-q3.service"]
            s["ExecMainStartTimestamp"] = (datetime.datetime.strptime(s["ExecMainStartTimestamp"], stamp)
                                           + datetime.timedelta(seconds=60)).strftime(stamp)
            sb.save_spec()
            return {"drift": restart(server(sb))}

        def stepped_forward(sb):                             # by the boot clock the change came before the start
            time.sleep(1.1)                                  # the sandbox's other files: before the stamp, below
            os.utime(server(sb))
            sb.start_service()
            s = sb.spec["strata-q3.service"]
            s["ExecMainStartTimestamp"] = datetime.datetime.fromtimestamp(server(sb).stat().st_ctime - 0.5,
                                                                          datetime.timezone.utc).strftime(stamp)
            sb.save_spec()
            return {"drift": restart(server(sb))}

        def no_stamp(sb):
            sb.start_service()
            sb.spec["strata-q3.service"]["ExecMainStartTimestamp"] = ""
            sb.save_spec()
            return {"drift": "strata-q3.service: its start time (ExecMainStartTimestamp) cannot be read"}
        self.each([("server.py touched", touched(server)),
                   ("a listed file touched", touched(lambda sb: sb.dev / "Strata-data/packs/p/index.txt")),
                   ("its installed unit touched", touched(lambda sb: sb.sys / "etc/systemd/system/strata-q3.service")),
                   ("a record", record), ("after the records", after_the_records),
                   ("tools/helper.py", module("tools/helper.py")), ("rootmod.py", module("rootmod.py")),
                   ("a start script", start_script), ("strata-wait-ready.sh", wait_ready), ("the clock set back", set_back),
                   ("the clock stepped forward", stepped_forward), ("no stamp", no_stamp)])

    def test_the_locale_python_sets_for_the_engine(self):
        # PEP 538: in the C locale, or one that is not there, Python sets LC_CTYPE, and the engine gets it
        def locale_(env):
            def prepare(sb):
                sb.spec["__manager__"].update(env)
                sb.save_spec()
                sb.start_service()
                return {}
            return prepare
        self.each([(repr(env), locale_(env)) for env in ({"LANG": "C"}, {"LANG": "POSIX"}, {"LANG": "xx_XX.UTF-8"},
                                                          {"LANG": "C", "LC_ALL": "C"})])

    def test_no_engine(self):
        def none(sb):
            sb.start_service(engines=0)
            return {"drift": "server.py runs no engine, and /v1/models does not say that the model was unloaded"}

        def unloaded(sb):
            sb.start_service(engines=0, unloaded=True)
            return {"notes": ["server.py runs no engine: the model is unloaded on purpose"]}
        self.each([("no engine", none), ("unloaded", unloaded)])

    def test_what_runs_against_the_live_config_and_the_list(self):
        def no_process(sb):
            sb.start_service()
            sb.spec["strata-q3.service"]["MainPID"] = "4194305"            # above any pid_max: no such process
            sb.save_spec()
            return {"drift": "its main process (4194305) cannot be read"}

        def no_state(sb):
            sb.start_service()
            sb.spec["strata-q3.service"]["ActiveState"] = ""
            sb.save_spec()
            return {"drift": "strata-q3.service: systemctl show gives no state"}

        def another_exe(sb):
            sb.start_service()
            other = sb.exe.with_name("strata2")
            shutil.copy2(sb.exe, other)
            sb.edit_live(lambda c: c.__setitem__("exe", str(other)))
            return {"drift": f"the engine runs {sb.exe}, the live config's exe is {other}",
                    "then": lambda r: self.assertIn(f"config/deployed-artifacts.sha256 does not list the config's exe "
                                                    f"({other})", r.stdout)}

        def another_hash(sb):
            sb.start_service()
            f = sb.repo / "config" / "deployed-artifacts.sha256"
            f.write_text(f.read_text().replace(sha(sb.exe), "f" * 64))
            sb.commit()
            return {"drift": f"the running engine's binary has sha256 {sha(sb.exe)[:16]}…, the list says {'f' * 16}…"}

        def edited(change, says):
            def prepare(sb):
                sb.start_service()
                sb.edit_live(change)
                return {"drift": says}
            return prepare
        cases = [
            (lambda c: c.__setitem__("layer_split", [13, 26, 38]), "the engine's command line is not what server.py makes"),
            (lambda c: c.__setitem__("parallel", 2), "the engine's command line is not what server.py makes"),
            (lambda c: c.__setitem__("split_skip_if_fits", True), "the engine's command line is not what server.py makes"),
            (lambda c: c.__setitem__("vram_elastic", True), "the engine's command line is not what server.py makes"),
            (lambda c: c["env"].__setitem__("STRATA_MMQ_GROUP_SORT", "0"),
             "the engine's environment is not what server.py makes of the live config, in ['STRATA_MMQ_GROUP_SORT']"),
            (lambda c: c.update(gpu=[0, 1, 2], layer_split=[13, 25]),
             "the engine's environment is not what server.py makes of the live config, in ['CUDA_VISIBLE_DEVICES']"),
            (lambda c: c.__setitem__("cwd", str(Path(tempfile.gettempdir()))),
             "the engine runs in another directory than the live config's cwd"),
            (lambda c: c.__setitem__("layer_split", [13, 13, 38]), "the live config has ['a value server.py would refuse (ValueError)']"),
            (lambda c: c.__setitem__("vision", {}), "the live config has ['vision'], which this check does not follow"),
            (lambda c: c.__setitem__("gpu_order", "auto") or c.__setitem__("layer_split", "auto"),
             "the live config has ['gpu_order'], which this check does not follow"),
            (lambda c: c.__setitem__("effort_position", "end"), "the live config has ['effort_position']"),
            (lambda c: c.__setitem__("replicas", 2), "the live config has ['replicas']"),
            (lambda c: c.__setitem__("expert_profile_save", "learned.bin"), "the live config has ['expert_profile_save']"),
        ]
        self.each([(f.__name__, f) for f in (no_process, no_state, another_exe, another_hash)]
                  + [(says, edited(change, says)) for change, says in cases])

    def test_the_files_the_engine_maps(self):
        def mapped(sb, listed: bool) -> Path:
            m = sb.exe.with_name("map.bin")
            m.write_bytes(b"x" * 4096)
            sb.maps(sb.abs_file, m)
            if listed:
                sb.write_list(extra=sb.listed(m))
                sb.commit()
            sb.start_service()
            return m

        def listed(sb):
            mapped(sb, listed=True)
            return {}

        def device(sb):                                 # a device, as /dev/nvidia0 is: the system's
            sb.maps(sb.abs_file, Path("/dev/zero"))
            sb.start_service()
            return {}

        def unlisted(sb):
            m = mapped(sb, listed=False)
            return {"drift": f"the engine maps {m}, which config/deployed-artifacts.sha256 does not list"}

        def replaced(sb):                               # replaced after the start, and the list with it
            m = mapped(sb, listed=True)
            new = m.with_suffix(".new")
            new.write_bytes(b"y" * 4096)
            os.replace(new, m)
            sb.write_list(extra=sb.listed(m))
            sb.commit()
            return {"drift": f"the engine maps a file that was replaced or deleted after it started: {m}"}

        def not_mapped(sb):                             # the listed library outside ~/dev not loaded
            sb.maps()
            sb.start_service()
            return {"drift": f"the engine does not map {sb.abs_file}, which the list names: it loads another copy, or none"}

        def another_copy(sb):                           # another copy of it loaded too
            copy_ = sb.d / "other" / "libx.so"
            copy_.parent.mkdir()
            shutil.copy2(sb.abs_file, copy_)
            sb.maps(sb.abs_file, copy_)
            sb.start_service()
            return {"drift": f"the engine maps {copy_}, another copy of the listed {sb.abs_file}"}

        def not_listed(sb):                             # a library the list does not name, from any directory but the system's
            lib = sb.d / "cuda-bin" / "libcuda.so.1"
            lib.parent.mkdir()
            lib.write_bytes(b"x" * 4096)
            sb.maps(sb.abs_file, lib)
            sb.start_service()
            return {"drift": f"the engine maps {lib}, which config/deployed-artifacts.sha256 does not list"}
        self.each([(f.__name__, f) for f in (listed, device, unlisted, replaced, not_mapped, another_copy, not_listed)])

    def test_settings_and_the_key(self):
        def nothing_answers(sb):
            sb.start_service()
            sb.url = "http://127.0.0.1:9"
            return {"drift": "nothing listens on port 9: server.py does not answer there"}

        def keyed(api_key, says=None, sent=None, hidden=("k-first",), **start):
            def prepare(sb):
                sb.edit_live(lambda c: c.__setitem__("api_key", api_key))
                sb.start_service(key="k-first", **start)

                def then(r):
                    for k in hidden:
                        self.assertNotIn(k, r.stdout + r.stderr)
                    if isinstance(sent, set):
                        self.assertEqual(set(sb.seen), sent)
                    elif sent:
                        self.assertIn(sent, sb.seen)
                return {"drift": says, "then": then}
            return prepare

        def env_first(sb):                              # STRATA_API_KEY in the unit's environment comes first
            sb.edit_unit(lambda t: t.replace("\nEnvironment=", "\nEnvironment=STRATA_API_KEY=k-env\nEnvironment=", 1))
            sb.edit_live(lambda c: c.__setitem__("api_key", "k-wrong"))
            sb.start_service(key="k-env")
            return {"absent": ["k-env"], "then": lambda r: self.assertIn("Bearer k-env", sb.seen)}

        def redirect(sb):                               # a redirect elsewhere: not followed, so the key stays here
            elsewhere = Sandbox(self)
            elsewhere.start_service(bind="127.0.0.2")
            sb.edit_live(lambda c: c.__setitem__("api_key", "k-first"))
            sb.start_service(key="k-first", redirect=elsewhere.url)
            return {"drift": "GET /settings cannot be read (HTTP 302)",
                    "then": lambda r: self.assertEqual(elsewhere.seen, [])}

        def no_proxy(sb):                               # never through a proxy
            sb.start_service()
            return {"env": {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": None,
                            "NO_PROXY": None}}
        self.each([("nothing answers", nothing_answers)]
                  + [(repr(k), keyed(k, sent="Bearer k-first"))           # the first key of the live config is sent
                     for k in ("k-first", " k-first ,k-second", ["k-first", "k-second"])]
                  + [("a wrong key", keyed("k-wrong", "GET /settings cannot be read (HTTP 401)", hidden=("k-wrong",))),
                     ("a key with a line end", keyed("k-first\nnot-printed", "GET /settings cannot be read (HTTP 401)",
                                                     sent={None}, hidden=("not-printed",))),   # in no header, nor printed
                     ("a key with a DEL", keyed("k-first\x7fnot-printed", "GET /settings cannot be read (HTTP 401)",
                                                sent={None}, hidden=("not-printed",))),
                     ("not this machine's address", keyed("k-first", "GET /settings cannot be read (HTTP 401)",
                                                          sent={None}, bind="127.0.0.2")),
                     ("the unit's STRATA_API_KEY", env_first), ("a redirect", redirect), ("a proxy", no_proxy)])

    def test_the_venvs_records(self):
        mod = lambda sb: sb.site / "pkg" / "mod.py"          # noqa: E731
        rec = lambda sb: sb.site / "pkg-1.0.dist-info" / "RECORD"      # noqa: E731

        def forged(sb):                                 # a file changed together with its RECORD row
            mod(sb).write_text("x = 1\nimport os\n")
            (mod(sb).parent / "__pycache__" / f"mod.{TAG}.pyc").unlink()
            d = base64.urlsafe_b64encode(hashlib.sha256(mod(sb).read_bytes()).digest()).rstrip(b"=").decode()
            rec(sb).write_text(re.sub(r"(?m)^pkg/mod\.py,[^\n]*", f"pkg/mod.py,sha256={d},{mod(sb).stat().st_size}",
                                      rec(sb).read_text()))

        def second(sb):                                 # another dist-info of the same package, pinned too
            info = sb.site / "pkg-1.0.dist-info2.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n\n")
            (sb.site / "sitecustomize.py").write_text("import os\n")
            (info / "RECORD").write_text("sitecustomize.py,sha256=" + base64.urlsafe_b64encode(hashlib.sha256(
                b"import os\n").digest()).rstrip(b"=").decode() + ",10\n" + f"{info.name}/RECORD,,\n")
            sb.pin_venv()

        def extra(sb):                                  # a package the list does not name
            info = sb.site / "extra-1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: extra\nVersion: 1.0\n\n")
            (info / "RECORD").write_text(f"{info.name}/RECORD,,\n")
        cases = [
            (forged, ".venv does not hold the packages config/strata-venv.txt lists, as it pins their RECORDs: the venv has "
                     "['pkg==1.0  "),
            (lambda sb: ((sb.site / "zz.pth").write_text("import os\n"), sb.record("zz.pth,,")),
             "lib/python3.10/site-packages/zz.pth has no hash in its RECORD"),
            (lambda sb: ((sb.site / "zz.pth").write_text("import os\n"), sb.record(
                "zz.pth,md5=" + base64.urlsafe_b64encode(hashlib.md5(b"import os\n").digest()).rstrip(b"=").decode() + ",10")),
             "zz.pth has a hash in its RECORD that is not sha256 or stronger"),
            (second, ".venv has more than one dist-info directory of pkg"),
            (extra, "the venv has ['extra==1.0  "),
            (lambda sb: (sb.site / "sitecustomize.pyc").write_bytes(b"x"), "sitecustomize.pyc was installed by no package"),
        ]

        def case(change, says):
            def prepare(sb):
                change(sb)
                sb.commit()
                return {"drift": says.replace("python3.10", f"python{sys.version_info[0]}.{sys.version_info[1]}")}
            return prepare

        def spelled(sb):                                # its name spelled otherwise: one package (PEP 503)
            info = sb.site / "PKG-1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: PKG\nVersion: 1.0\n\n")
            (info / "RECORD").write_text(f"{info.name}/RECORD,,\n")
            sb.pin_venv()
            return {"drift": ".venv has more than one dist-info directory of pkg"}

        def stale(sb):                                  # bytecode's rows: pip compiles it again, and a hash goes stale
            sb.record(f"pkg/__pycache__/mod.{TAG}.pyc,sha256=" + "A" * 43 + ",1")
            sb.commit()
            return {}
        self.each([(says, case(change, says)) for change, says in cases] + [("a stale hash of bytecode", stale),
                                                                             ("a name spelled otherwise", spelled)])

    def test_the_service_users_site_packages(self):
        # what a python3 started without -I runs at its start from the user's own site-packages (the preflight's)
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)

        def site(sb):
            d = sb.sys / home.relative_to("/") / ".local/lib/python3.10/site-packages"
            d.mkdir(parents=True)
            return d

        def hook(name):
            def prepare(sb):
                d = site(sb)
                (d / name).mkdir() if "." not in name else (d / name).write_text("import os\n")
                return {"drift": f"the service user's site-packages hold code that a python3 started without -I runs at "
                                 f"its start: {d / name}"}
            return prepare

        def module(sb):                                 # a module there only runs when something imports it
            (site(sb) / "foo.py").write_text("import os\n")
            return {"then": lambda r: self.assertIn("ok     no .pth, usercustomize or sitecustomize in the service user's "
                                                    "site-packages", r.stdout)}
        self.each([(n, hook(n)) for n in ("zz.pth", "usercustomize.py", "sitecustomize.cpython-310-x86_64-linux-gnu.so",
                                          "usercustomize")] + [("a module", module)])

    def test_namespace_packages_where_server_py_imports_from(self):
        # a directory without __init__.py is a package that Python imports from: serve/structured.py's
        # "from jsonschema import validators" ran jsonschema/validators.py from there
        def namespace(where):
            def prepare(sb):
                (sb.strata / where).mkdir(parents=True)
                (sb.strata / where / "validators.py").write_text("raise SystemExit\n")
                return {"drift": f"has modules where server.py imports from that the pinned commit does not have and "
                                 f"the list does not name: {where}/ (a namespace package)"}
            return prepare

        def listed(sb):                                 # one that holds a listed file, as engine-cuda12/ does
            (sb.strata / "packdir").mkdir()
            (sb.strata / "packdir" / "weights.bin").write_text("w\n")
            sb.write_list(extra=sb.listed(sb.strata / "packdir" / "weights.bin"))
            sb.commit()
            return {}

        def no_identifier(sb):                          # names that no import statement reaches
            for d in (".pytest_cache", "serve/my-pkg", "tools/.hidden"):
                (sb.strata / d).mkdir(parents=True)
                (sb.strata / d / "__init__.py").write_text("raise SystemExit\n")
            (sb.strata / ".pytest_cache" / "README.md").write_text("x\n")
            for f in ("foo.bar.py", "serve/x-y.py", f"serve/a b{importlib.machinery.EXTENSION_SUFFIXES[0]}"):
                (sb.strata / f).write_text("raise SystemExit\n")
            return {}
        self.each([(w, namespace(w)) for w in ("jsonschema", "tools/jsonschema", "serve/jsonschema")]
                  + [("listed", listed), ("no identifier", no_identifier)])

    def test_who_gets_the_key(self):
        # every probe's error is caught and named by its class only (a status line, a header or a body can hold the
        # key that the listener got); and only a listener of server.py's gets the key
        secret = "k-SECRET-not-to-print"

        def keyed(says, sent=True, **kw):
            def prepare(sb):
                sb.edit_live(lambda c: c.__setitem__("api_key", secret))
                sb.start_service(key=secret, **kw)
                return {"drift": says, "absent": [secret, "Traceback"],
                        "then": lambda r: (self.assertIn(f"Bearer {secret}", sb.seen) if sent      # server.py's socket
                                           else self.assertEqual(sb.seen, []))}
            return prepare

        def foreign(sb):                                # a listener that is not server.py's
            want = keyed("", sent=False, foreign=True)(sb)
            return {**want, "drift": f"another process than server.py (its main process or a child) listens on port "
                                     f"{sb.url.rsplit(':', 1)[1]}: it gets no key, and what it answers is not taken"}
        self.each([
            ("BadStatusLine", keyed("GET /settings cannot be read (BadStatusLine)",
                                    reply=lambda auth: (auth or "").encode() + b"\r\n\r\n")),
            ("IncompleteRead", keyed("GET /settings cannot be read (IncompleteRead)",
                                     reply=lambda auth: b"HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n" + (auth or "").encode())),
            ("LineTooLong", keyed("GET /settings cannot be read (LineTooLong)",
                                  reply=lambda auth: b"HTTP/1.0 200 OK\r\n" + b"X: " + (auth or "").encode() * 4000 + b"\r\n\r\n")),
            ("no engine", keyed("server.py runs no engine, and /v1/models does not say that the model was unloaded",
                                reply=lambda auth: (auth or "").encode() + b"\r\n\r\n", engines=0)),
            ("a listener of another's", foreign)])

    def test_a_library_put_on_the_engines_path_after_its_start(self):
        # the config's lib_dirs come first on the engine's library path: a library put there after the start, under
        # the name of one the engine maps, is what its next start loads
        libc = os.path.realpath(next(l.split(None, 5)[5] for l in Path(f"/proc/{os.getpid()}/maps").read_text().splitlines()
                                     if re.search(r"/libc\.so\.6$", l)))
        caps = [k for k, on in loader_says().items() if k.startswith("glibc-hwcaps/") and on]

        def put(where=None):
            def prepare(sb):
                bin_ = sb.d / "cuda-bin"
                bin_.mkdir()
                sb.configure(lambda c: c.__setitem__("lib_dirs", [str(bin_)]))
                sb.start_service()
                if where is None:
                    return {}
                (bin_ / where).parent.mkdir(parents=True, exist_ok=True)
                (bin_ / where).write_bytes(b"\x7fELF not really\n")
                return {"drift": f"the engine's next start would load {bin_}/{where}, which comes earlier on its library "
                                 f"path, instead of {libc}, which it maps now",
                        "notes": [f"{bin_}, on the engine's library path, changed after it started"]}
            return prepare

        def mode(sb):                                    # its mode alone: its ctime
            bin_ = sb.d / "cuda-bin"
            bin_.mkdir()
            sb.configure(lambda c: c.__setitem__("lib_dirs", [str(bin_)]))
            sb.start_service()
            os.chmod(bin_, 0o700)
            return {"notes": [f"{bin_}, on the engine's library path, changed after it started"]}
        self.each([("nothing there", put()), ("libc.so.6", put("libc.so.6")), ("its mode", mode)]
                  + [(f"{caps[0]}/libc.so.6", put(f"{caps[0]}/libc.so.6"))] * bool(caps))  # where it looks first here

    def test_the_file_system_its_unit_gives_it(self):
        # what systemd sets up at the start: a unit loaded after it with other settings does not change it
        def setting(line, says):
            def prepare(sb):
                key = line.split("=")[0]
                sb.edit_unit(lambda t: re.sub(rf"(?m)^{key}=.*\n", "", t).replace("[Service]\n", f"[Service]\n{line}\n"))
                sb.start_service()
                return {"drift": "DRIFT  ", "then": lambda r: self.assertRegex(r.stdout, says)}
            return prepare
        def own_tmp(private):                        # in a mount namespace of its own (unshare, as this user), with
            def prepare(sb):                         # /var/tmp mounted there: what systemd mounts for PrivateTmp=yes (a
                sb.edit_unit(lambda t: t.replace("PrivateTmp=no", "PrivateTmp=yes"))     # directory systemd-private-*),
                (sb.d / "systemd-private-x" / "tmp").mkdir(parents=True)    # or a tmpfs of another's.  (Not /tmp: the
                how = ('mount --bind "$1/tmp" /var/tmp' if private              # sandbox is there)
                       else "mount -t tmpfs none /var/tmp")
                sb.start_service(wrap=["unshare", "-rm", "sh", "-c", how + ' && shift && exec "$@"', "sh",
                                       str(sb.d / "systemd-private-x")])
                line = "server.py's /var/tmp is not the one of its own that its unit's PrivateTmp=yes gives"
                return {"drift": "server.py's /tmp is not the one of its own that its unit's PrivateTmp=yes gives",
                        "then": lambda r: (self.assertNotIn if private else self.assertIn)(line, r.stdout)}
            return prepare
        probe = subprocess.run(["unshare", "-rm", "sh", "-c", "mount -t tmpfs none /var/tmp"], capture_output=True)
        spaces = [("PrivateTmp=yes, its own /var/tmp", own_tmp(True)), ("PrivateTmp=yes, a tmpfs at /var/tmp", own_tmp(False))]
        self.each(([] if probe.returncode else spaces) + [(line, setting(line, says)) for line, says in (
            ("ProtectSystem=strict", r"server\.py has /(, /\w+)* mounted otherwise \(read-only or "
                                     r"writable\) than its unit's ProtectSystem=strict gives: restart pending"),
            ("ProtectSystem=full", r"server\.py has (/\w+, )*/etc(, /\w+)* mounted otherwise \(read-only or "
                                   r"writable\) than its unit's ProtectSystem=full gives: restart pending"),
            ("PrivateTmp=yes", r"server\.py's /tmp is not the one of its own that its unit's "
                               r"PrivateTmp=yes gives: restart pending"),
            ("ProtectHome=yes", r"strata-q3\.service's ProtectHome=yes: this check does not compare it"))])

    def test_git_attributes_from_outside_the_commit(self):
        # a preflight with CRLF line ends does not run (/usr/bin/env: 'bash\r'); attributes that only this machine
        # has must not make it count as the commit's
        def crlf(sb):
            f = sb.repo / "scripts" / "strata-preflight.sh"
            f.write_text("#!/usr/bin/env bash\nexit 0\n")
            f.chmod(0o755)
            sb.commit()
            f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))

        def info(sb):
            crlf(sb)
            (sb.repo / ".git" / "info").mkdir(exist_ok=True)
            (sb.repo / ".git" / "info" / "attributes").write_text("* text=auto\n")
            return {"drift": f"{sb.repo}/.git/info/attributes sets git attributes from outside the commit, which this "
                             "check does not follow"}

        def users(sb):                                  # the user's own attributes file
            crlf(sb)
            home = sb.d / "home"
            (home / ".config" / "git").mkdir(parents=True)
            (home / ".config" / "git" / "attributes").write_text("*.sh text\n")
            return {"drift": "scripts/strata-preflight.sh differs", "env": {"HOME": str(home), "XDG_CONFIG_HOME": None}}

        def worktrees(sb):                              # a .gitattributes in the Strata tree that its commit has not
            server = sb.strata / "serve" / "server.py"
            server.write_bytes(server.read_bytes().replace(b"\n", b"\r\n"))
            (sb.strata / "serve" / ".gitattributes").write_text("* text\n")
            return {"drift": "differs from the pinned commit in 1 files: serve/server.py differs"}
        self.each([(f.__name__, f) for f in (info, users, worktrees)])

    def test_what_systemd_gives_a_service_of_its_user(self):
        # HOME, USER, LOGNAME and SHELL come from the passwd entry of the unit's User
        def given(name, value):
            def prepare(sb):
                sb.start_service(server_env={name: value})
                return {"drift": f"server.py's ['{name}'] are not what systemd gives a service of its User",
                        "absent": [value]}
            return prepare
        self.each([(name, given(name, value)) for name, value in (
            ("HOME", "/nonexistent/elsewhere"), ("USER", "nobody-else"), ("LOGNAME", "nobody-else"),
            ("SHELL", "/bin/false-not"))])

    def test_a_drop_in_of_the_repo(self):
        # decision 0007: the maintainer's account is limited by a drop-in for user-1003.slice
        inst = lambda sb: sb.sys / "etc/systemd/system/user-1003.slice.d"            # noqa: E731
        cg = lambda sb: sb.sys / "sys/fs/cgroup/user.slice/user-1003.slice"           # noqa: E731

        def installed(sb):
            sb.dropin()

            def then(r):
                self.assertIn(f"ok     {inst(sb)}/50-memory.conf", r.stdout)
                self.assertIn("ok     user-1003.slice (loaded: as the repo's drop-ins, 2 settings; its cgroup holds its "
                              "MemoryMax, MemorySwapMax)", r.stdout)
                self.assertEqual((cg(sb) / "memory.max").read_text(), "6442450944\n")
            return {"then": then}
        runtime = "/run/systemd/system.control/user-1003.slice.d"

        def runtime_copy(sb, text):
            d = sb.sys / runtime.lstrip("/")
            d.mkdir(parents=True, exist_ok=True)
            (d / "50-MemoryMax.conf").write_text("# set-property\n[Slice]\n" + text)
            sb.spec["user-1003.slice"]["DropInPaths"] += f" {runtime}/50-MemoryMax.conf"

        def runtime_null(sb):                           # masks a drop-in of its name, as an empty one does
            d = sb.sys / runtime.lstrip("/")
            d.mkdir(parents=True, exist_ok=True)
            os.symlink("/dev/null", d / "50-MemoryMax.conf")
            sb.spec["user-1003.slice"]["DropInPaths"] += f" {runtime}/50-MemoryMax.conf"
        cases = [
            (lambda sb: (inst(sb) / "50-memory.conf").unlink(), "DRIFT  {inst}/50-memory.conf is missing"),
            (lambda sb: (inst(sb) / "50-memory.conf").write_text("[Slice]\nMemoryMax=8G\nMemorySwapMax=0\n"),
             "{inst}/50-memory.conf differs from the repo copy"),
            (lambda sb: os.chmod(inst(sb) / "50-memory.conf", 0o664), "{inst}/50-memory.conf is owner:group mode"),
            (lambda sb: (inst(sb) / "60-more.conf").write_text("[Slice]\nMemoryMax=infinity\n"),
             "{inst} has 60-more.conf, which the repo's systemd/user-1003.slice.d does not have"),
            (lambda sb: os.symlink("/dev/null", inst(sb) / "10-defaults.conf"), "{inst} has 10-defaults.conf"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__("DropInPaths", "/usr/lib/systemd/system/user-.slice.d/10-defaults.conf"),
             "/etc/systemd/system/user-1003.slice.d/50-memory.conf is installed, but systemd has not loaded it"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__("MemoryMax", "8589934592"),
             "user-1003.slice (loaded): MemoryMax is '8589934592', the repo says '6G'"),
            (lambda sb: (cg(sb) / "memory.max").write_text("max\n"), "MemoryMax: its cgroup's memory.max is max, the repo says 6G"),
            (lambda sb: (cg(sb) / "memory.swap.max").unlink(), "MemorySwapMax: {cg}/memory.swap.max cannot be read"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__("ControlGroup", ""), "its control group is not known"),
            (lambda sb: runtime_copy(sb, "MemoryMax=8589934592\n"),
             f"{runtime}/50-MemoryMax.conf (systemctl set-property --runtime) sets MemoryMax otherwise than the repo's files"),
            (lambda sb: runtime_copy(sb, "CPUQuota=50%\n"),
             f"{runtime}/50-MemoryMax.conf (systemctl set-property --runtime) sets CPUQuota otherwise than the repo's files"),
            (lambda sb: runtime_copy(sb, "MemoryMax=6G\0\n"),
             f"{runtime}/50-MemoryMax.conf (systemctl set-property --runtime) has a line (3) with a NUL byte, where "
             "systemd ends a line and this check does not"),
            (lambda sb: runtime_copy(sb, "MemoryMax=6G \\\n"),
             f"{runtime}/50-MemoryMax.conf (systemctl set-property --runtime) has a line (3) that ends with a backslash, "
             "which this check does not join with the next one"),
            (lambda sb: runtime_copy(sb, ""),
             f"{runtime}/50-MemoryMax.conf (systemctl set-property --runtime) sets nothing: it masks a drop-in of its name"),
            (runtime_null, f"{runtime}/50-MemoryMax.conf (systemctl set-property --runtime) is a link to /dev/null: it "
                           "masks a drop-in of its name"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__(
                "DropInPaths", sb.spec["user-1003.slice"]["DropInPaths"] + " /etc/systemd/system/user-.slice.d/x.conf"),
             "drop-ins override it: /etc/systemd/system/user-.slice.d/x.conf"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__(
                "DropInPaths", sb.spec["user-1003.slice"]["DropInPaths"] + f" /etc/systemd/system.control/user-1003.slice.d/50-MemoryMax.conf"),
             "drop-ins override it: /etc/systemd/system.control/user-1003.slice.d/50-MemoryMax.conf"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__("FragmentPath", "/etc/systemd/system/user-1003.slice"),
             "systemd loads it from /etc/systemd/system/user-1003.slice, a unit file the repo does not have"),
            (lambda sb: sb.spec["user-1003.slice"].__setitem__("NeedDaemonReload", "yes"),
             "its file or a drop-in changed on disk since the last daemon-reload"),
        ]
        def case(change, says):
            def prepare(sb):
                sb.dropin()
                change(sb)
                sb.save_spec()
                return {"drift": says.format(inst=inst(sb), cg=cg(sb))}
            return prepare
        def holds_the_repos_values(sb):                 # a runtime copy of the repo's values: allowed, and said
            sb.dropin()
            runtime_copy(sb, "MemoryMax=6442450944\n")
            sb.save_spec()
            return {"notes": [f"user-1003.slice: {runtime}/50-MemoryMax.conf (systemctl set-property --runtime) holds "
                              "the repo's values"]}

        def no_session(sb):                             # no session of the user: no cgroup, nothing in force to compare
            sb.dropin()
            sb.spec["user-1003.slice"].update(ActiveState="inactive", ControlGroup="")
            sb.save_spec()
            return {"then": lambda r: self.assertIn("ok     user-1003.slice (loaded: as the repo's drop-ins, 2 settings)",
                                                    r.stdout)}

        def fraction(sb):                               # 1.5G: a size with a fraction (systemd: 1610612736 bytes)
            sb.dropin(text="[Slice]\nMemoryMax=1.5G\nMemorySwapMax=0\n")
            sb.spec["user-1003.slice"]["MemoryMax"] = "1610612736"
            sb.cgroups()
            sb.save_spec()
            return {}

        def whole_pages(sb):                            # a limit that is no whole number of pages: the kernel rounds down
            sb.dropin(text="[Slice]\nMemoryMax=1000000\nMemorySwapMax=0\n")
            page = os.sysconf("SC_PAGE_SIZE")
            (cg(sb) / "memory.max").write_text(f"{1000000 // page * page}\n")
            return {}

        def reloading(sb):                              # its cgroup is in force while it reloads too
            sb.dropin()
            sb.spec["user-1003.slice"]["ActiveState"] = "reloading"
            (cg(sb) / "memory.max").write_text("max\n")
            sb.save_spec()
            return {"drift": "MemoryMax: its cgroup's memory.max is max, the repo says 6G"}

        def not_installed(sb):                          # missing, and so not loaded: said once
            sb.dropin()
            (inst(sb) / "50-memory.conf").unlink()
            sb.spec["user-1003.slice"]["DropInPaths"] = "/usr/lib/systemd/system/user-.slice.d/10-defaults.conf"
            sb.save_spec()
            return {"drift": f"{inst(sb)}/50-memory.conf is missing",
                    "absent": ["is installed, but systemd has not loaded it"]}

        def services_own(sb):                           # the service's own MemorySwapMax=0, in force while it runs
            sb.start_service()
            (sb.sys / "sys/fs/cgroup/system.slice/strata-q3.service/memory.swap.max").write_text("max\n")
            return {"drift": "strata-q3.service (loaded): MemorySwapMax: its cgroup's memory.swap.max is max, the repo "
                             "says 0"}
        self.each([("installed", installed)] + [(says, case(change, says)) for change, says in cases]
                  + [(f.__name__, f) for f in (holds_the_repos_values, no_session, services_own, fraction, whole_pages,
                                               reloading, not_installed)])

    def test_which_library_the_next_start_loads(self):
        # glibc's loader looks a library up by its SONAME, in each directory of the library path first in the
        # subdirectories that this CPU supports (ld.so --help says which), then in the directory; the directories it
        # found the libraries in (their RUNPATH) come after LD_LIBRARY_PATH
        says = loader_says()
        caps = [k for k, on in says.items() if k.startswith("glibc-hwcaps/") and on]
        libc = os.path.realpath(next(l.split(None, 5)[5] for l in Path("/proc/self/maps").read_text().splitlines()
                                     if re.search(r"/libc\.so\.6$", l)))
        fake = b"\x7fELF not really\n"

        def first_on_path(sb, d=None):               # the config's lib_dirs: first on the engine's library path
            d = d or sb.d / "cuda-bin"
            d.mkdir(exist_ok=True)
            sb.configure(lambda c: c.__setitem__("lib_dirs", [str(d)]))
            return d

        def put(path, data=fake):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        def versioned(sb, name, flags=(), how=None):  # libv.so.1.0, whose SONAME is libv.so.1, mapped, as CUDA's are
            v = sb.d / "vlib" / "libv.so.1.0"
            v.parent.mkdir()
            if how:
                patched_library(shared_library("libv.so.1", flags), v, how)
            else:
                shutil.copyfile(shared_library("libv.so.1", flags), v)
            os.symlink("libv.so.1.0", v.parent / "libv.so.1")
            bin_ = first_on_path(sb)
            put(bin_ / name)
            sb.write_list(extra=sb.listed(v))
            sb.commit()
            sb.maps(sb.abs_file, v)
            sb.start_service()
            return bin_, v

        def legacy(sb):
            bin_ = first_on_path(sb)
            sb.start_service()
            put(bin_ / "x86_64" / "libc.so.6")
            return {"drift": f"the engine's next start would load {bin_}/x86_64/libc.so.6, which comes earlier on its "
                             f"library path, instead of {libc}, which it maps now"}

        def own_caps(sb):                            # its own directory, first on the path: its subdirectories first
            first_on_path(sb, sb.abs_file.parent)
            sb.start_service()
            put(sb.abs_file.parent / caps[0] / "libx.so")
            return {"drift": f"the engine's next start would load {sb.abs_file.parent}/{caps[0]}/libx.so, which comes "
                             f"earlier on its library path, instead of {sb.abs_file}, which it maps now"}

        def other_names(sb):                         # what the loader does not look for here: no drift
            bin_ = first_on_path(sb)
            sb.start_service()
            for rel in ["libc.so.5", "libc.so", "libc.so.6.bak"] + [f"{k}/libc.so.6" for k, on in says.items() if not on]:
                put(bin_ / rel)
            return {"notes": [f"{bin_}, on the engine's library path, changed after it started"],
                    "absent": ["would load"]}

        def by_soname(sb):
            bin_, v = versioned(sb, "libv.so.1")
            return {"drift": f"the engine's next start would load {bin_}/libv.so.1, which comes earlier on its library "
                             f"path, instead of {v}, which it maps now"}

        def not_by_file_name(sb):
            versioned(sb, "libv.so.1.0")
            return {"absent": ["would load"]}

        def physical_address(sb):                    # its p_paddr not its p_vaddr: the loader reads p_vaddr
            bin_, v = versioned(sb, "libv.so.1", how="paddr")
            return {"drift": f"the engine's next start would load {bin_}/libv.so.1, which comes earlier on its library "
                             f"path, instead of {v}, which it maps now"}

        def soname_after_the_end(sb):                # its DT_SONAME after the DT_NULL that ends the section: none
            versioned(sb, "libv.so.1", how="null")   # so it is looked up by its file name, libv.so.1.0
            return {"absent": ["would load"]}

        def at_another_address(sb):                  # its string table at an address that is not its file offset
            bin_, v = versioned(sb, "libv.so.1", ("-Wl,-Ttext-segment=0x200000",))
            return {"drift": f"the engine's next start would load {bin_}/libv.so.1, which comes earlier on its library "
                             f"path, instead of {v}, which it maps now"}

        def runpath(sb):                             # a directory it found a library in, not on LD_LIBRARY_PATH
            sb.start_service()
            put(sb.abs_file.parent / "libc.so.6")
            return {"drift": f"the engine's next start would load {sb.abs_file.parent}/libc.so.6, which comes earlier on "
                             f"its library path, instead of {libc}, which it maps now"}

        def later(sb):                               # a second file of its SONAME later on the path: never loaded
            later_dir = sb.d / "cuda-bin"
            later_dir.mkdir()
            sb.configure(lambda c: c.__setitem__("lib_dirs", [str(sb.abs_file.parent), str(later_dir)]))
            shutil.copyfile(sb.abs_file, later_dir / "libx.so")
            sb.start_service()
            return {"absent": ["would load"]}

        def semicolon(sb):                           # glibc's loader takes ";" as well as ":" between directories
            bin_ = sb.d / "cuda-bin"
            bin_.mkdir()
            sb.configure(lambda c: c.setdefault("env", {}).__setitem__("LD_LIBRARY_PATH", f"{sb.d / 'nothing'};{bin_}"))
            sb.start_service()
            put(bin_ / "libc.so.6")
            return {"drift": f"the engine's next start would load {bin_}/libc.so.6, which comes earlier on its library "
                             f"path, instead of {libc}, which it maps now"}

        def no_elf(sb):                              # a library it maps that is no ELF file: drift, not a traceback
            bad = sb.abs_file.with_name("libbad.so")
            bad.write_bytes(b"not a library\n" * 400)
            sb.write_list(extra=sb.listed(bad))
            sb.commit()
            sb.maps(sb.abs_file, bad)
            sb.start_service()
            return {"drift": f"{bad}, which the engine maps, cannot be read as a library (ValueError)"}
        cases = [("another SONAME, a development link, a level this CPU does not search", other_names),
                 ("by its SONAME", by_soname), ("not by its file name", not_by_file_name),
                 ("a directory it found a library in", runpath), ("no ELF", no_elf),
                 ("a second file of its SONAME later", later), ("';' between directories", semicolon),
                 ("by its SONAME, its segments at another address", at_another_address),
                 ("p_paddr is not p_vaddr", physical_address), ("DT_SONAME after the DT_NULL", soname_after_the_end)]
        if says.get("x86_64"):
            cases.append(("x86_64/ of an earlier directory", legacy))
        if caps:
            cases.append((f"{caps[0]}/ of its own directory", own_caps))
        self.assertTrue(says, "ld.so --help says nothing of the subdirectories it searches")
        self.each(cases)

    def test_a_fifo_where_the_check_reads(self):
        # a named pipe (or a device) where the check reads a file: it says so at once, and never blocks on it
        ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"

        def fifo(path):
            if path.exists() or path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(path)
            return path

        def listed(sb):
            f = fifo(sb.dev / "Strata-data/packs/p/index.txt")
            return {"drift": f"{f} is not a regular file"}

        def live(sb):                                # said twice: by the comparison with the snapshot, and the list's
            f = fifo(sb.strata / "strata-iq3_xxs.json")
            return {"drift": f"{f} is not a regular file", "then": lambda r: self.assertEqual(
                r.stdout.count(f"DRIFT  {f} is not a regular file\n"), 2, r.stdout)}

        def live_running(sb):
            sb.start_service()
            f = fifo(sb.strata / "strata-iq3_xxs.json")
            return {"drift": f"{f} is not a regular file: what the service should run is not known"}

        def venv_file(sb):
            fifo(sb.site / "pkg" / "mod.py")
            return {"drift": f"lib/{ver}/site-packages/pkg/mod.py is not a regular file"}

        def metadata(sb):
            fifo(sb.site / "pkg-1.0.dist-info" / "METADATA")
            return {"drift": f"lib/{ver}/site-packages/pkg-1.0.dist-info/METADATA is not a regular file"}

        def record(sb):
            fifo(sb.site / "pkg-1.0.dist-info" / "RECORD")
            return {"drift": f"lib/{ver}/site-packages/pkg-1.0.dist-info/RECORD is not a regular file"}

        def bytecode(sb):
            fifo(sb.strata / "serve" / "__pycache__" / f"server.{TAG}.pyc")
            return {"drift": f"serve/__pycache__/server.{TAG}.pyc is not a regular file"}

        def source(sb):                              # with its bytecode, which the check compares with it
            src = sb.strata / "serve" / "server.py"
            py_compile.compile(str(src), doraise=True)
            fifo(src)
            return {"drift": "serve/server.py is not a regular file"}

        def attributes(sb):
            f = fifo(sb.strata / ".git" / "info" / "attributes")
            return {"drift": f"{f} is not a regular file"}

        def runtime_dropin(sb):
            sb.dropin()
            d = "/run/systemd/system.control/user-1003.slice.d"
            fifo(sb.sys / d.lstrip("/") / "50-MemoryMax.conf")
            sb.spec["user-1003.slice"]["DropInPaths"] += f" {d}/50-MemoryMax.conf"
            sb.save_spec()
            return {"drift": f"{d}/50-MemoryMax.conf (systemctl set-property --runtime) is not a regular file"}

        def cgroup(sb):
            sb.dropin()
            f = fifo(sb.sys / "sys/fs/cgroup/user.slice/user-1003.slice/memory.max")
            return {"drift": f"MemoryMax: {f} is not a regular file"}

        def zfs(sb):
            f = fifo(sb.sys / "sys/module/zfs/parameters/zfs_arc_max")
            return {"drift": f"{f} is not a regular file"}

        def installed(sb):
            f = fifo(sb.sys / "etc/systemd/system/strata-q3.service")
            return {"drift": f"{f} is not a regular file"}

        def repo_file(sb):
            f = fifo(sb.repo / "config" / "profile.conf")
            return {"drift": f"{f} is not a regular file: nothing else checked"}

        def checkout_file(sb):
            (sb.repo / "docs").mkdir()
            (sb.repo / "docs" / "x.md").write_text("x\n")
            sb.commit()
            fifo(sb.repo / "docs" / "x.md")
            return {"drift": f"{sb.repo} has local changes: docs/x.md is not a regular file"}

        def mapped(sb):                              # a library the engine maps, a FIFO after its start
            sb.start_service()
            fifo(sb.abs_file)
            return {"drift": f"{sb.abs_file}, which the engine maps, cannot be read as a library (not a regular file)"}

        def candidate_dropin(link=False):            # a drop-in of a tree checked with --candidate, as deploy.sh does
            def prepare(sb):
                cand = sb.d / "candidate"
                shutil.copytree(sb.repo, cand, ignore=shutil.ignore_patterns(".git"))
                f = cand / "systemd" / "user-1003.slice.d" / "60-extra.conf"
                if link:                             # a link that leads nowhere
                    os.symlink("nowhere.conf", f)
                else:
                    fifo(f)
                return {"drift": f"{f} is not a regular file", "args": ("--candidate",),
                        "script": cand / "scripts" / "check-drift.sh"}
            return prepare

        def tty_listed(sb):                          # a device: stat says so before an open would (ENXIO here)
            link = sb.dev / "Strata-data" / "packs" / "p" / "tty.bin"
            os.symlink("/dev/tty", link)
            sb.write_list(extra=f"{'0' * 64}  {link.relative_to(sb.dev)}\n")
            sb.commit()
            return {"drift": f"{link} is not a regular file"}

        def tty_attributes(sb):
            f = sb.strata / ".git" / "info" / "attributes"
            f.parent.mkdir(exist_ok=True)
            os.symlink("/dev/tty", f)
            return {"drift": f"{f} is not a regular file"}
        self.each([(f.__name__, f) for f in (listed, live, live_running, venv_file, metadata, record, bytecode, source,
                                             attributes, runtime_dropin, cgroup, zfs, installed, repo_file,
                                             checkout_file, mapped, tty_listed, tty_attributes)]
                  + [("candidate_dropin", candidate_dropin()), ("candidate_dropin_link", candidate_dropin(link=True))],
                  timeout=90)

    def test_a_file_replaced_between_two_looks(self):
        # compare() looks at a file of the commit (lstat) and then opens it: one replaced by a FIFO in between is not
        # read (open_regular looks again, and opens without blocking).  A copy of the check waits between the two
        # until this test has replaced the file: one of the Strata tree, and one of the service's checkout
        def race(tree, path, says):
            sb = Sandbox(self)
            at = '                if bool(st.st_mode & 0o100) != (mode == "100755"):\n'  # after the lstat, before the open
            text = (sb.repo / "scripts" / "check-drift.sh").read_text()
            self.assertEqual(text.count(at), 1)
            pause = sb.d / "pause"
            script = sb.repo / "scripts" / "check-drift-paused.sh"
            script.write_text(text.replace(at, ("                if path == %r and os.path.realpath(repo) == %r:\n"
                                                "                    open(%r, 'w').close()\n"
                                                "                    for _ in range(2400):\n"
                                                "                        if os.path.exists(%r):\n"
                                                "                            break\n"
                                                "                        __import__('time').sleep(0.05)\n")
                                           % (path, os.path.realpath(tree(sb)), f"{pause}.reached", f"{pause}.go") + at))
            out = {}
            check = threading.Thread(target=lambda: out.update(r=sb.run(script=script, timeout=90)))
            check.start()
            for _ in range(1200):
                if Path(f"{pause}.reached").exists() or not check.is_alive():
                    break
                time.sleep(0.05)
            self.assertTrue(Path(f"{pause}.reached").exists(), "the check did not reach the point between its looks")
            f = tree(sb) / path
            f.unlink()
            os.mkfifo(f)
            Path(f"{pause}.go").write_text("")
            check.join(120)
            self.drift(None, says(sb), r=out["r"])
            self.assertNotIn("Traceback", out["r"].stderr)
        for name, args in (("the Strata tree", (lambda sb: sb.strata, "serve/server.py", lambda sb:
                                                f"{sb.strata} differs from the pinned commit in 1 files: serve/server.py "
                                                "is not a regular file")),
                           ("the service's checkout", (lambda sb: sb.repo, "config/profile.conf", lambda sb:
                                                       f"DRIFT  {sb.repo} has local changes: config/profile.conf is not "
                                                       "a regular file"))):
            with self.subTest(name):
                race(*args)

    def test_the_test_hooks(self):
        # a value of a test hook that the check cannot use stops it with drift that names the hook; host-units.sh's
        # hooks (HOST_UNITS_*) are said too
        def git_limit(value, usable=False):
            def prepare(sb):
                if usable:
                    return {"env": {"DRIFT_GIT_TIMEOUT": value},
                            "notes": ["test hooks set: DRIFT_ENGINE_RE DRIFT_GIT_TIMEOUT DRIFT_OWNER"]}
                return {"env": {"DRIFT_GIT_TIMEOUT": value},
                        "drift": "DRIFT  DRIFT_GIT_TIMEOUT is not a number of seconds above 0 and at most 86400: nothing "
                                 "checked", "then": lambda r: self.assertEqual(
                                     [l for l in r.stdout.splitlines() if l.startswith("DRIFT")],
                                     ["DRIFT  DRIFT_GIT_TIMEOUT is not a number of seconds above 0 and at most 86400: "
                                      "nothing checked"])}
            return prepare

        def pattern(sb):
            return {"env": {"DRIFT_ENGINE_RE": "("},
                    "drift": "DRIFT  DRIFT_ENGINE_RE is not a pattern that Python's re compiles: nothing checked"}

        def its_own_text(pattern):                   # the engine's command line as it is, without the default's [e],
            def prepare(sb):                         # matches the check's own command lines, which hold the pattern
                return {"env": {"DRIFT_ENGINE_RE": pattern},
                        "drift": "DRIFT  DRIFT_ENGINE_RE matches its own text, so it would match this check's own "
                                 "processes: write a letter of it in brackets, as the default does ([e]ngine): nothing "
                                 "checked"}
            return prepare

        def host_units(sb):
            conf = sb.d / "host-units.conf"
            conf.write_text("default system multi-user.target\n")
            return {"env": {"HOST_UNITS_CONF": str(conf), "HOST_UNITS_RUN_USER": str(sb.d)},
                    "notes": ["test hooks set: DRIFT_ENGINE_RE DRIFT_OWNER DRIFT_SYSROOT DRIFT_TEST_SPEC DRIFT_URL "
                              "HOST_UNITS_CONF HOST_UNITS_RUN_USER"]}
        self.each([(f"DRIFT_GIT_TIMEOUT={v!r}", git_limit(v)) for v in ("inf", "infinity", "nan", "abc", "0", "-1",
                                                                         "1e400", "1e9")]
                  + [("DRIFT_GIT_TIMEOUT='' (the default)", git_limit("", usable=True)),
                     ("DRIFT_GIT_TIMEOUT='7'", git_limit("7", usable=True)),
                     ("DRIFT_GIT_TIMEOUT='86400' (a day)", git_limit("86400", usable=True)),
                     ("DRIFT_GIT_TIMEOUT='86401'", git_limit("86401")),
                     ("DRIFT_ENGINE_RE='('", pattern),
                     ("DRIFT_ENGINE_RE matching its own text", its_own_text("engine-cuda12/strata-fake --serve")),
                     ("DRIFT_ENGINE_RE matching its own text after its start",                  # [e]? lets the text's
                      its_own_text("[e]?ngine-cuda12/strata-fake --serve")),                     # own "ngine..." match
                     ("HOST_UNITS_*", host_units)])
        # the bounds, as the check's own Python reads them: a day is allowed and a moment more is not, a fraction of a
        # second is (each git command of a run with one would have to end within it)
        text = (ROOT / "scripts" / "check-drift.sh").read_text()
        head = 'unusable=$(python3 -I -S - "$GIT_TIMEOUT" "$ENGINE_RE" <<\'EOF\'\n'
        body = text[text.index(head) + len(head):]
        body = body[:body.index("\nEOF\n")]
        for value, usable in (("86400", True), ("86400.001", False), ("86401", False), ("0.5", True), ("1.5", True),
                              ("1e-3", True), ("0", False), ("-0.5", False)):
            with self.subTest(value=value):
                r = subprocess.run([sys.executable, "-I", "-S", "-", value, "[e]ngine"], input=body,
                                   capture_output=True, text=True)
                self.assertEqual(r.stdout == "", usable, r.stdout + r.stderr)
        # the default, 300 s, which a test cannot wait for: the line that sets it, run by bash
        line = next(l for l in (ROOT / "scripts" / "check-drift.sh").read_text().splitlines()
                    if l.startswith("GIT_TIMEOUT="))
        for env, want in (({}, "300"), ({"DRIFT_GIT_TIMEOUT": ""}, "300"), ({"DRIFT_GIT_TIMEOUT": "7"}, "7")):
            with self.subTest(env=env):
                e = {k: v for k, v in os.environ.items() if k != "DRIFT_GIT_TIMEOUT"}
                r = subprocess.run(["bash", "-c", f'{line}\necho "$GIT_TIMEOUT"'], env={**e, **env},
                                   capture_output=True, text=True)
                self.assertEqual(r.stdout.strip(), want, r.stderr)

    def test_a_unit_file_that_is_missing_or_not_regular(self):
        # in the tree checked (one checked with --candidate, here), a unit file that is missing or not a regular file is
        # drift; it is neither read nor compared (a FIFO would hang cmp).  A change may give a list: every DRIFT line
        def candidate(change, absent=None):
            def prepare(sb):
                cand = sb.d / "candidate"
                shutil.copytree(sb.repo, cand, ignore=shutil.ignore_patterns(".git"))
                says, only = change(sb, cand), None
                if isinstance(says, list):
                    says, only = says[0], says

                def then(r):
                    if absent:
                        self.assertEqual([l for l in r.stdout.splitlines()
                                          if l.endswith("deployed-artifacts.sha256 is missing")],
                                         ["DRIFT  config/deployed-artifacts.sha256 is missing"])     # said once,
                        self.assertNotIn("No such file or directory", r.stderr)                   # and not read
                    if only is not None:
                        self.assertEqual([l for l in r.stdout.splitlines() if l.startswith("DRIFT")], only)
                    self.assertNotIn("Traceback", r.stderr)
                return {"drift": says, "args": ("--candidate",), "script": cand / "scripts" / "check-drift.sh",
                        "then": then}
            return prepare

        def missing(rel, says=None, alone=False):    # says: the line, with {cand} for the tree
            def change(sb, cand):
                (cand / rel).unlink()
                said = (says or f"DRIFT  {{cand}}/{rel} is missing").replace("{cand}", str(cand))
                return [said] if alone else said
            return change

        def a_directory(sb, cand):                   # user-1003.slice, a unit the repo has only drop-ins for
            (cand / "systemd" / "user-1003.slice").mkdir()
            return [f"DRIFT  {cand}/systemd/user-1003.slice is not a regular file"]

        def a_link_to_nowhere(sb, cand):
            os.symlink("nowhere.slice", cand / "systemd" / "user-1003.slice")
            return [f"DRIFT  {cand}/systemd/user-1003.slice is not a regular file"]

        def beside_its_dropins(sb, cand):            # one of the repo's own units: its file, also beside a drop-in
            (cand / "systemd" / "strata-q3-health.timer").unlink()      # directory, which a unit the repo has only
            (cand / "systemd" / "strata-q3-health.timer.d").mkdir()     # drop-ins for has
            return [f"DRIFT  {cand}/systemd/strata-q3-health.timer is missing"]

        def unreadable(sb, cand):                    # a regular file that cannot be read: drift, not a traceback
            os.chmod(cand / "systemd" / "strata-q3-health.timer", 0)
            return (f"DRIFT  strata-q3-health.timer: {cand}/systemd/strata-q3-health.timer cannot be read "
                    "(PermissionError): what systemd loaded of it is not compared")

        def not_utf8(sb, cand):                      # nor one that is not UTF-8
            f = cand / "systemd" / "strata-q3-health.timer"
            f.write_bytes(f.read_bytes() + b"# \xff\n")
            return f"DRIFT  strata-q3-health.timer: {f} cannot be read (UnicodeDecodeError): what systemd loaded of it is not compared"

        def running(change):                         # the same, while the service runs
            def started(sb, cand):
                sb.start_service()
                return change(sb, cand)
            return started

        def a_fifo(unit, then):
            def change(sb, cand):
                installed = sb.sys / "etc/systemd/system" / unit
                if not installed.exists():
                    installed.write_text("[Unit]\n")
                (cand / "systemd" / unit).unlink(missing_ok=True)
                os.mkfifo(cand / "systemd" / unit)
                said = f"DRIFT  {cand}/systemd/{unit} is not a regular file{then}"
                return said if then else [said]
            return change
        self.each([("systemd/zfs-arc-max.service missing", candidate(missing("systemd/zfs-arc-max.service"))),
                   ("systemd/strata-q3-health.timer missing", candidate(missing("systemd/strata-q3-health.timer",
                                                                                alone=True))),       # no other drift
                   ("systemd/strata-q3-health.timer missing beside its drop-in directory", candidate(beside_its_dropins)),
                   ("systemd/strata-q3.logrotate missing", candidate(missing("systemd/strata-q3.logrotate",
                                                                             "DRIFT  {cand}/systemd/strata-q3.logrotate "
                                                                             "is missing, or not a regular file"))),
                   ("a link that leads nowhere at systemd/user-1003.slice", candidate(a_link_to_nowhere)),
                   ("a unit file that is not UTF-8", candidate(not_utf8)),
                   ("systemd/strata-q3.service missing, the service running",
                    candidate(running(missing("systemd/strata-q3.service")))),
                   ("the list missing, the service running",
                    candidate(running(missing("config/deployed-artifacts.sha256",
                                              "DRIFT  config/deployed-artifacts.sha256 is missing")))),
                   ("systemd/strata-q3.service missing", candidate(missing("systemd/strata-q3.service"))),
                   ("a directory at systemd/user-1003.slice", candidate(a_directory)),
                   ("a FIFO at systemd/user-1003.slice", candidate(a_fifo("user-1003.slice", ""))),
                   ("a FIFO at systemd/strata-q3-health.timer",
                    candidate(a_fifo("strata-q3-health.timer", ": nothing else checked"))),
                   ("the list missing", candidate(missing("config/deployed-artifacts.sha256",
                                                          "DRIFT  config/deployed-artifacts.sha256 is missing"),
                                                  absent="deployed-artifacts.sha256 is missing\nDRIFT")),
                   ("the venv's list missing", candidate(missing("config/strata-venv.txt")))]
                  + ([("a unit file that cannot be read", candidate(unreadable))] if os.geteuid() else []),  # root reads it
                  timeout=60)

    def test_a_file_of_the_repo_that_is_not_utf8(self):
        # a file of the repo that the check reads as text and that is not UTF-8 is drift where it is read, and the check
        # goes on; installed alike, it gives no other drift
        def drift_lines(r):
            return [l for l in r.stdout.splitlines() if l.startswith("DRIFT")]

        def appended(sb, *files):
            for f in files:
                f.write_bytes(f.read_bytes() + b"# \xff\n")
            sb.commit()

        def dropin(sb):                              # the only drift: not compared with what systemd loaded
            sb.dropin()
            f = sb.repo / "systemd" / "user-1003.slice.d" / "50-memory.conf"
            appended(sb, f, sb.sys / "etc/systemd/system/user-1003.slice.d/50-memory.conf")
            said = (f"DRIFT  user-1003.slice: {f} cannot be read (UnicodeDecodeError): what systemd loaded of it is not "
                    "compared")
            return {"drift": said, "absent": ["ok     user-1003.slice (loaded", "Traceback"],
                    "then": lambda r: self.assertEqual(drift_lines(r), [said])}

        def unit_running(sb):                        # the service's unit: what runs is not compared with it either
            sb.start_service()
            f = sb.repo / "systemd" / "strata-q3.service"
            appended(sb, f, sb.sys / "etc/systemd/system/strata-q3.service")
            return {"drift": f"DRIFT  {f} cannot be read (above): what runs is not compared with it",
                    "absent": ["server.py's command line is not its unit's", "Traceback"]}

        def list_running(sb):                        # the list: in section 5, and the check of what runs goes on
            sb.start_service()
            f = sb.repo / "config" / "deployed-artifacts.sha256"
            appended(sb, f)
            return {"drift": f"DRIFT  {f} cannot be read (UnicodeDecodeError)", "absent": ["Traceback"],
                    "then": lambda r: self.assertIn("== the running service\n", r.stdout)}

        def venv_list(sb):
            f = sb.repo / "config" / "strata-venv.txt"
            appended(sb, f)
            return {"drift": f"DRIFT  {f} cannot be read (UnicodeDecodeError)", "absent": ["Traceback"]}
        self.each([(f.__name__, f) for f in (dropin, unit_running, list_running, venv_list)])

    def test_what_it_says_of_a_file_it_cannot_use(self):
        # a file of the repo, or one installed, that the check cannot read or that is missing is named as such, once,
        # and not as a difference of another file
        def drift_lines(r):
            return [l for l in r.stdout.splitlines() if l.startswith("DRIFT")]

        def candidate(change):                       # a tree checked with --candidate: change gives its DRIFT lines
            def prepare(sb):
                cand = sb.d / "candidate"
                shutil.copytree(sb.repo, cand, ignore=shutil.ignore_patterns(".git"))
                lines = change(sb, cand)
                return {"drift": lines[0], "args": ("--candidate",), "script": cand / "scripts" / "check-drift.sh",
                        "then": lambda r: (self.assertEqual(drift_lines(r), lines),
                                           self.assertNotIn("Traceback", r.stderr))}
            return prepare

        def repo_copy(sb, cand):                     # the logrotate file of the repo, unreadable
            f = cand / "systemd" / "strata-q3.logrotate"
            os.chmod(f, 0)
            return [f"DRIFT  {f} cannot be read: {sb.sys}/etc/logrotate.d/strata-q3 is not compared with it"]

        def snapshot(sb, cand):
            (cand / "config" / "strata-iq3_xxs.deployed.json").unlink()
            return ["DRIFT  config/strata-iq3_xxs.deployed.json is missing: the live config is not compared with it"]

        def setup(sb, cand):
            (cand / "config" / "strata-iq3_xxs.setup.json").unlink()
            return ["DRIFT  config/strata-iq3_xxs.setup.json cannot be read: what configure_strata_q3.py makes of it is "
                    "not compared"]

        def host_units_script(sb, cand):
            (cand / "scripts" / "host-units.sh").unlink()
            return [f"DRIFT  {cand}/scripts/host-units.sh is missing, or not a regular file: the host units are not "
                    "checked"]

        def host_units_directory(sb, cand):          # not run: it is no regular file
            (cand / "scripts" / "host-units.sh").unlink()
            (cand / "scripts" / "host-units.sh").mkdir()
            return [f"DRIFT  {cand}/scripts/host-units.sh is missing, or not a regular file: the host units are not "
                    "checked"]

        def snapshot_unreadable(sb, cand):           # said once: configure's output is not compared with it either
            os.chmod(cand / "config" / "strata-iq3_xxs.deployed.json", 0)
            return ["DRIFT  config/strata-iq3_xxs.deployed.json cannot be read: the live config is not compared with it"]

        def both_missing(sb, cand):                  # each said, the live config first; the list names it too
            live = sb.strata / "strata-iq3_xxs.json"
            live.unlink()
            (cand / "config" / "strata-iq3_xxs.deployed.json").unlink()
            return [f"DRIFT  {live} is missing",
                    "DRIFT  config/strata-iq3_xxs.deployed.json is missing: the live config is not compared with it",
                    f"DRIFT  {live} is missing"]

        def host_units_list(sb, cand):               # the repository's host-units.sh (the sandbox's is a stub)
            shutil.copy2(ROOT / "scripts" / "host-units.sh", cand / "scripts" / "host-units.sh")
            (cand / "config" / "host-units.conf").unlink(missing_ok=True)
            return [f"DRIFT  {cand}/config/host-units.conf cannot be read, or is not a regular file: no unit checked"]

        def installed(sb):                           # an installed file, unreadable
            f = sb.sys / "etc/logrotate.d/strata-q3"
            os.chmod(f, 0)
            said = f"DRIFT  {f} cannot be read: it is not compared with the repo copy"
            return {"drift": said, "then": lambda r: self.assertEqual(drift_lines(r), [said])}

        def silent(sb):                              # host-units.sh fails and says nothing
            f = sb.repo / "scripts" / "host-units.sh"
            f.write_text("#!/bin/sh\nexit 3\n")
            sb.commit()
            said = "DRIFT  scripts/host-units.sh check failed (exit 3) and said no DRIFT line"
            return {"drift": said, "then": lambda r: self.assertEqual(drift_lines(r), [said])}

        def silent_comparison(sb):                   # the Python that compares the two ends without a word: not taken
            stub = sb.bin / "python3"                # for the same (a python3 that fails for it alone)
            stub.write_text(f'#!/bin/sh\ncase "$*" in *strata-iq3_xxs.deployed.json*) exit 1 ;; esac\n'
                            f'exec {shutil.which("python3")} "$@"\n')
            stub.chmod(0o755)
            said = f"DRIFT  {sb.strata}/strata-iq3_xxs.json differs from config/strata-iq3_xxs.deployed.json"
            return {"drift": said, "absent": [f"ok     {sb.strata}/strata-iq3_xxs.json\n"]}

        def key_and_no_snapshot(sb, cand):           # the key is seen all the same: no line of the list's sha256 of it
            live = sb.strata / "strata-iq3_xxs.json"
            live.write_text(json.dumps({**json.loads(live.read_text()), "api_key": "k-not-printed"}, indent=4) + "\n")
            (cand / "config" / "strata-iq3_xxs.deployed.json").unlink()
            return ["DRIFT  config/strata-iq3_xxs.deployed.json is missing: the live config is not compared with it"]

        def live(sb):                                # the live config, missing: not said to differ
            f = sb.strata / "strata-iq3_xxs.json"
            f.unlink()
            return {"drift": f"DRIFT  {f} is missing", "absent": ["differs from config/strata-iq3_xxs.deployed.json"]}

        def live_unreadable(sb):                     # nor when it cannot be read
            f = sb.strata / "strata-iq3_xxs.json"
            os.chmod(f, 0)
            return {"drift": f"DRIFT  {f} cannot be read\n", "absent": ["differs from config/strata-iq3_xxs.deployed.json"]}

        def bad_only(sb):                            # host-units.sh's BAD line (a line of its list it cannot read)
            f = sb.repo / "scripts" / "host-units.sh"     # says why it failed: no DRIFT line is added
            f.write_text("#!/bin/sh\necho 'BAD    line: masked elsewhere x'\nexit 1\n")
            sb.commit()
            return {"drift": "== host units (config/host-units.conf)\nBAD    line: masked elsewhere x\n== Strata config\n",
                    "then": lambda r: self.assertEqual(drift_lines(r), [])}

        def cmp_fails(on, said):                     # cmp itself cannot read one of the two (exit 2): not a difference
            def prepare(sb):
                stub = sb.bin / "cmp"
                stub.write_text(f'#!/bin/sh\ncase "$*" in *{on}*) exit 2 ;; esac\nexec {shutil.which("cmp")} "$@"\n')
                stub.chmod(0o755)
                lines = [said(sb)]
                return {"drift": lines[0], "then": lambda r: self.assertEqual(drift_lines(r), lines)}
            return prepare
        self.each([("the repo's snapshot missing", candidate(snapshot)), ("setup's config missing", candidate(setup)),
                   ("the live config and the snapshot missing", candidate(both_missing)),
                   ("scripts/host-units.sh missing", candidate(host_units_script)),
                   ("a directory at scripts/host-units.sh", candidate(host_units_directory)),
                   ("host-units.sh says a BAD line only", bad_only),
                   ("config/host-units.conf missing", candidate(host_units_list)),
                   ("host-units.sh fails without a DRIFT line", silent), ("the live config missing", live),
                   ("cmp cannot read one", cmp_fails("logrotate", lambda sb: (
                       f"DRIFT  {sb.repo}/systemd/strata-q3.logrotate or {sb.sys}/etc/logrotate.d/strata-q3 cannot be "
                       "read: they are not compared"))),
                   ("cmp cannot read the snapshot", cmp_fails("deployed.json", lambda sb: (
                       "DRIFT  config/strata-iq3_xxs.deployed.json cannot be read: what configure_strata_q3.py makes is "
                       "not compared with it"))),
                   ("the comparison says nothing", silent_comparison),
                   ("a key in the live config, the snapshot missing", candidate(key_and_no_snapshot))]
                  + ([("the repo's logrotate file unreadable", candidate(repo_copy)),
                      ("an installed file unreadable", installed),
                      ("the snapshot unreadable", candidate(snapshot_unreadable)),
                      ("the live config unreadable", live_unreadable)] if os.geteuid() else []), timeout=60)

    def test_a_file_of_the_services_checkout_that_cannot_be_read(self):
        # what the check cannot read in the service's checkout is drift, and the check goes on, as in the Strata tree:
        # the repository's info/attributes when it is no regular file, a file of the commit it cannot open or reach,
        # a git attribute whose value is not UTF-8
        def info_attributes(make, running=False):
            def prepare(sb):
                if running:
                    sb.start_service()
                f = sb.repo / ".git" / "info" / "attributes"
                f.parent.mkdir(exist_ok=True)
                make(f)
                return {"drift": f"DRIFT  {sb.repo}: {f} is not a regular file",
                        "absent": ["Traceback", "cannot be read (above)"]}
            return prepare

        def mode_0(sb):                              # made with mode 0, and committed from its content
            g = ["git", "-C", str(sb.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
            (sb.repo / "notes").mkdir()
            fd = os.open(sb.repo / "notes" / "x.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0)
            os.write(fd, b"x\n")
            os.close(fd)
            blob = subprocess.run(g + ["hash-object", "-w", "--stdin"], input=b"x\n", capture_output=True,
                                  check=True).stdout.decode().strip()
            subprocess.run(g + ["update-index", "--add", "--cacheinfo", f"100644,{blob},notes/x.md"], check=True)
            subprocess.run(g + ["commit", "-q", "-m", "x"], check=True)
            return {"drift": f"DRIFT  {sb.repo} has local changes: notes/x.md cannot be read (Permission denied)",
                    "absent": ["Traceback"]}

        def under_a_file(sb):                        # notes/y.md, where notes is a file now
            (sb.repo / "notes").mkdir()
            (sb.repo / "notes" / "y.md").write_text("y\n")
            sb.commit()
            (sb.repo / "notes" / "y.md").unlink()
            (sb.repo / "notes").rmdir()
            (sb.repo / "notes").write_text("a file where the directory was\n")
            return {"drift": f"DRIFT  {sb.repo} has local changes: notes/y.md cannot be read (Not a directory); notes is "
                             "not in its commit", "absent": ["Traceback"]}

        def attribute(sb):                           # git check-attr gives its value as it is
            (sb.repo / ".gitattributes").write_bytes(b"* eol=\xff\n")
            sb.commit()
            return {"drift": f"DRIFT  {sb.repo} has local changes: .gitattributes has a git attribute whose value is not "
                             "UTF-8 (eol), which this check does not follow", "absent": ["Traceback"]}
        self.each([("info/attributes a link to /dev/tty", info_attributes(lambda f: os.symlink("/dev/tty", f))),
                   ("info/attributes a FIFO", info_attributes(os.mkfifo)),
                   ("info/attributes a FIFO, the service running", info_attributes(os.mkfifo, running=True)),
                   ("a file of the commit under a file", under_a_file), ("an attribute that is not UTF-8", attribute)]
                  + ([("a file of the commit that cannot be read", mode_0)] if os.geteuid() else []), timeout=60)

    def test_the_arc_values_its_unit_writes(self):
        # what zfs-arc-max.service writes is read from its ExecStart lines, as systemd reads them: a comment that holds
        # an old command, or one in prose, writes nothing; blanks may stand around the key; two values of one parameter
        # are drift.  The unit changed in the repo and installed alike
        def unit(sb, old, new):
            for f in (sb.repo / "systemd" / "zfs-arc-max.service", sb.sys / "etc/systemd/system/zfs-arc-max.service"):
                t = f.read_text()
                self.assertEqual(t.count(old), 1, f)
                f.write_text(t.replace(old, new, 1))
            sb.commit()

        def comments(sb):
            unit(sb, "\nExecStart=", "\n#ExecStart=/bin/sh -ec 'echo 8589934592 > /sys/module/zfs/parameters/zfs_arc_max; "
                 "echo 1 > /sys/module/zfs/parameters/zfs_arc_shrinker_limit'\n"
                 "  ; echo 4294967296 > /sys/module/zfs/parameters/zfs_arc_max\n"
                 "# before: echo 4294967296 > /sys/module/zfs/parameters/zfs_arc_max\n# a comment goes on nowhere \\\n"
                 "ExecStart=")
            return {"then": lambda r: [self.assertIn(l, r.stdout) for l in
                                       ("ok     zfs_arc_max = 2147483648\n", "ok     zfs_arc_shrinker_limit = 0\n")]}

        def blanks(sb):
            unit(sb, "\nExecStart=", "\n  ExecStart\t= ")
            return {"then": lambda r: self.assertIn("ok     zfs_arc_max = 2147483648\n", r.stdout)}

        def twice(sb):
            unit(sb, "\n# No ExecStop", "\nExecStart=/bin/sh -ec 'echo 4294967296 > /sys/module/zfs/parameters/zfs_arc_max'"
                 "\n# No ExecStop")
            return {"drift": "DRIFT  zfs-arc-max.service writes zfs_arc_max more than once (2147483648,4294967296): which "
                             "value counts is not known\n",
                    "then": lambda r: self.assertIn("ok     zfs_arc_shrinker_limit = 0\n", r.stdout)}
        def twice_alike(sb):                         # one value twice: drift all the same
            unit(sb, "\n# No ExecStop", "\nExecStart=/bin/sh -ec 'echo 2147483648 > /sys/module/zfs/parameters/zfs_arc_max'"
                 "\n# No ExecStop")
            return {"drift": "DRIFT  zfs-arc-max.service writes zfs_arc_max more than once (2147483648,2147483648): which "
                             "value counts is not known\n"}

        def ok_lines(r):
            for l in ("ok     zfs_arc_max = 2147483648\n", "ok     zfs_arc_shrinker_limit = 0\n"):
                self.assertIn(l, r.stdout)

        def other_keys(sb):                          # ExecStartPre= and another line that is no ExecStart= line: not
            unit(sb, "\nExecStart=", "\nExecStartPre=/bin/sh -ec 'echo 4294967296 > /sys/module/zfs/parameters/"   # read
                 "zfs_arc_max'\nDescription=echo 1 > /sys/module/zfs/parameters/zfs_arc_shrinker_limit\nExecStart=")
            sb.spec["zfs-arc-max.service"] = show_of(sb.repo / "systemd" / "zfs-arc-max.service", "zfs-arc-max.service")
            sb.save_spec()
            return {"then": ok_lines}

        def not_systemds_key(name, key):             # a key in another case, or after a form feed: systemd does not know
            def prepare(sb):                         # it, and runs no command of it (drift above)
                unit(sb, "\n# No ExecStop", f"\n{key}=/bin/sh -ec 'echo 4294967296 > /sys/module/zfs/parameters/"
                     "zfs_arc_max'\n# No ExecStop")
                return {"drift": f"DRIFT  zfs-arc-max.service (loaded): {key}= is in the repo's unit, and this check "
                                 "does not compare it yet", "then": ok_lines}
            return prepare

        def continued(sb):                           # over two lines: drift above, and no value read here
            unit(sb, "zfs_arc_max; echo 0 >", "zfs_arc_max; \\\n echo 0 >")
            return {"drift": "DRIFT  the ZFS ARC values are not compared: systemd/zfs-arc-max.service has a line that "
                             "goes on on the next one (above)\n", "absent": ["writes ?", "zfs_arc_max ="]}

        def one_not_written(sb):                     # a parameter that no ExecStart line writes
            unit(sb, "; echo 0 > /sys/module/zfs/parameters/zfs_arc_shrinker_limit'", "'")
            return {"drift": "DRIFT  systemd/zfs-arc-max.service has no ExecStart line that writes zfs_arc_shrinker_limit "
                             "(echo N > ...): it is not compared\n", "absent": ["writes ?"]}
        self.each([(f.__name__, f) for f in (comments, blanks, twice, twice_alike, other_keys, continued, one_not_written)]
                  + [("a key in lower case", not_systemds_key("lower", "execstart")),
                     ("a form feed before the key", not_systemds_key("form feed", "\fExecStart"))])

    def test_a_unit_line_that_goes_on_on_the_next_one(self):
        # systemd joins a line that ends with a backslash with the next one; this check does not (nor does
        # scripts/test.sh): such a line is drift where the check reads the unit, also while the service runs, and an
        # ExecStart that cannot be split into words is too.  A comment that ends with a backslash goes on nowhere.  The
        # unit is written after the start, as it is, in the repo and installed alike
        def changed(sb, old, new, running=True):
            if running:
                sb.start_service()
            for f in (sb.repo / "systemd" / "strata-q3.service", sb.sys / "etc/systemd/system/strata-q3.service"):
                text = f.read_text()
                self.assertEqual(text.count(old), 1, f)
                f.write_text(text.replace(old, new, 1))
            sb.commit()
            return sb.repo / "systemd" / "strata-q3.service"

        def continued(running):
            def prepare(sb):
                f = changed(sb, " --port 8080", " \\\n    --port 8080", running)
                n = next(i for i, l in enumerate(f.read_text().splitlines(), 1) if l.endswith("\\"))
                lines = [f"DRIFT  strata-q3.service: {f}: its line {n} ends with a backslash, which goes on on the next "
                         "line; this check does not join lines, so what systemd loaded of it is not compared"]
                if running:
                    lines.append(f"DRIFT  {f}: its line {n} ends with a backslash, which goes on on the next line (above): "
                                 "what runs is not compared with it")
                return {"drift": lines[0], "absent": ["is in the repo's unit, and this check does not compare it yet",
                                                      "No escaped character", "Traceback"],
                        "then": lambda r: [self.assertIn(l + "\n", r.stdout) for l in lines]}
            return prepare

        def unbalanced(sb):
            changed(sb, " --port 8080", ' --port 8080 --host "127.0.0.1')
            return {"drift": "DRIFT  strata-q3.service's ExecStart cannot be split into words (No closing quotation): "
                             "what runs is not compared with it",
                    "then": lambda r: self.assertIn("DRIFT  strata-q3.service (loaded): ExecStart: No closing quotation",
                                                    r.stdout), "absent": ["Traceback"]}

        def comment(running):                        # with the service running, the unit written before its start:
            def prepare(sb):                         # the check of what runs reads it too
                changed(sb, "\nExecStart=", "\n# a comment that ends with a backslash \\\nExecStart=", running=False)
                if running:
                    sb.start_service()
                return {}
            return prepare

        def ends_with(tail, running=False):          # two at its end: the first escapes the second; or a space after
            def prepare(sb):                         # one: the line ends there, for systemd too
                sb.edit_unit(lambda t: re.sub(r"(?m)^(Description=.*)$", lambda m: m.group(1) + tail, t, count=1))
                if running:
                    sb.start_service()
                return {}
            return prepare
        def blanks(old, new, running=False):         # systemd's blanks are " \t\n\r" only: a form feed before # makes
            def prepare(sb):                         # no comment, and a section's line goes on as any other does
                changed(sb, old, new, running=False)
                if running:
                    sb.start_service()
                f = sb.repo / "systemd" / "strata-q3.service"
                n = next(i for i, l in enumerate(f.read_text().split("\n"), 1) if l.endswith("\\"))
                lines = [f"DRIFT  strata-q3.service: {f}: its line {n} ends with a backslash, which goes on on the next "
                         "line; this check does not join lines, so what systemd loaded of it is not compared"]
                if running:
                    lines.append(f"DRIFT  {f}: its line {n} ends with a backslash, which goes on on the next line (above): "
                                 "what runs is not compared with it")
                return {"drift": lines[0], "absent": ["Traceback"],
                        "then": lambda r: [self.assertIn(l + "\n", r.stdout) for l in lines]}
            return prepare

        def blanks_of_systemd(running):              # a tab, ; and blanks before # make comments, whatever their end,
            def prepare(sb):                         # and blanks around a section's line are stripped (the unit written
                changed(sb, "\nExecStart=", "\n\t# a tab first \\\n; a ; comment \\\n  # an indented one \\\n"
                        "ExecStart=", running=False)              # before the start)
                changed(sb, "\n[Service]\n", "\n  [Service] \t\n", running=False)
                if running:
                    sb.start_service()
                return {}
            return prepare

        def blank_before_equals(sb):                 # "ExecStart =": systemd strips the key, as both readers do, and
            changed(sb, "\nExecStart=", "\nExecStart = ", running=False)          # the lines that name the live
            changed(sb, "\nExecStartPre=", "\nExecStartPre =  ", running=False)    # config and the checkout
            sb.start_service()
            return {}

        def nbsp_value(sb):                          # a no-break space is no blank to systemd: kept in the value, as
            sb.edit_unit(lambda t: re.sub(r"(?m)^(Description=.*)$", lambda m: m.group(1) + "\u00a0", t, count=1))
            desc = next(l for l in (sb.repo / "systemd" / "strata-q3.service").read_text().split("\n")    # systemd
                        if l.startswith("Description="))                                                # shows it
            sb.spec["strata-q3.service"]["Description"] = desc.partition("=")[2]
            sb.save_spec()
            return {}

        def nul(running):                            # systemd ends a line at a NUL byte and reads on after it; the check
            def prepare(sb):                         # does not: drift, in both readers
                changed(sb, "\nExecStart=", "\n# a comment\0ExecStartPost=/nonexistent/after-nul\nExecStart=",
                        running=False)
                if running:
                    sb.start_service()
                f = sb.repo / "systemd" / "strata-q3.service"
                n = next(i for i, l in enumerate(f.read_text().split("\n"), 1) if "\0" in l)
                lines = [f"DRIFT  strata-q3.service: {f}: its line {n} holds a NUL byte, where systemd ends a line; this "
                         "check does not, so what systemd loaded of it is not compared"]
                if running:
                    lines.append(f"DRIFT  {f} holds a NUL byte (above): what runs is not compared with it")
                return {"drift": lines[0], "absent": ["Traceback"],
                        "then": lambda r: [self.assertIn(l + "\n", r.stdout) for l in lines]}
            return prepare

        def bom(running):                            # a byte order mark before [Unit]: systemd skips it
            def prepare(sb):
                for f in (sb.repo / "systemd" / "strata-q3.service", sb.sys / "etc/systemd/system/strata-q3.service"):
                    f.write_text("\ufeff" + f.read_text())
                sb.commit()
                if running:
                    sb.start_service()
                return {}
            return prepare

        def bom_later(running, first=False):         # systemd skips the first byte order mark at the start of whichever
            def prepare(sb):                         # line it is: the line is a comment then; after one at the start of
                for f in (sb.repo / "systemd" / "strata-q3.service", sb.sys / "etc/systemd/system/strata-q3.service"):
                    text = f.read_text()             # the file, it is part of its line, a key systemd does not know
                    f.write_text(("\ufeff" if first else "") + text.replace("\n[Service]\n", "\n\ufeff# a comment\n[Service]\n", 1))
                sb.commit()
                if running:
                    sb.start_service()
                return {"drift": "DRIFT  strata-q3.service (loaded): \ufeff# a comment= is in the repo's unit, and this "
                                 "check does not compare it yet"} if first else {}
            return prepare

        def bom_before_a_key(sb):                    # one that what runs is compared with: read, as systemd reads it
            sb.edit_unit(lambda t: t.replace("\nWorkingDirectory=", "\n\ufeffWorkingDirectory=", 1))
            wd = re.search(r"(?m)^\ufeffWorkingDirectory=(.*)$", (sb.repo / "systemd" / "strata-q3.service").read_text())
            sb.spec["strata-q3.service"]["WorkingDirectory"] = wd.group(1)
            sb.save_spec()
            sb.start_service(cwd=Path(wd.group(1)))
            return {"then": lambda r: self.assertIn("ok     strata-q3.service runs what the files above say", r.stdout)}

        def key_with_a_form_feed(sb):                # nor a blank after a key: Description\f is a key systemd does not
            sb.edit_unit(lambda t: t.replace("\nDescription=", "\nDescription\f=", 1))     # know, a line it ignores
            return {"drift": "DRIFT  strata-q3.service (loaded): Description\f= is in the repo's unit, and this check does "
                             "not compare it yet"}
        self.each([("continued, the service running", continued(True)), ("continued, the service stopped",
                                                                         continued(False)),
                   ("a form feed before #", blanks("\nExecStart=", "\n\f# a comment \\\nExecStart=")),
                   ("a form feed before #, the service running",
                    blanks("\nExecStart=", "\n\f# a comment \\\nExecStart=", running=True)),
                   ("a section's line", blanks("\n[Service]\n", "\n[Service] \\\n")),
                   ("a section's line, the service running", blanks("\n[Service]\n", "\n[Service] \\\n", running=True)),
                   ("a form feed after a key", key_with_a_form_feed),
                   ("an unbalanced quote, the service running", unbalanced), ("a comment", comment(False)),
                   ("a comment, the service running", comment(True)),
                   ("an escaped backslash", ends_with(" \\\\")), ("a backslash and a space", ends_with(" \\ ")),
                   ("an escaped backslash, the service running", ends_with(" \\\\", running=True)),
                   ("a backslash and a space, the service running", ends_with(" \\ ", running=True)),
                   ("systemd's blanks", blanks_of_systemd(False)), ("systemd's blanks, the service running",
                                                                    blanks_of_systemd(True)),
                   ("blanks before =, the service running", blank_before_equals),
                   ("a value that ends with a no-break space", nbsp_value),
                   ("a NUL byte", nul(False)), ("a NUL byte, the service running", nul(True)),
                   ("a byte order mark", bom(False)), ("a byte order mark, the service running", bom(True)),
                   ("a byte order mark on a later line", bom_later(False)),
                   ("a byte order mark on a later line, the service running", bom_later(True)),
                   ("a second byte order mark", bom_later(False, first=True)),
                   ("a byte order mark before a key, the service running", bom_before_a_key)])

    def test_what_the_callers_environment_cannot_change(self):
        # no git variable of the caller's reaches the check's git: GIT_DIR, nor GIT_CONFIG_PARAMETERS (what git -c
        # passes on), which would win over the check's own settings, nor GIT_INDEX_FILE; and a test hook that a BASH_ENV
        # file sets without export is named, as BASH_ENV is.  The git variables are this test's own, beside one that
        # sorts before them; none of the caller's (an editor's GIT_ASKPASS) is left
        mine = {k: None for k in os.environ if k.startswith("GIT_")} | {"GIT_AAA_OF_THIS_TEST": "1"}

        def git_dir(sb):
            return {"env": mine | {"GIT_DIR": str(sb.d / "nowhere")}}

        def git_c(sb):
            attrs = sb.d / "attributes"
            attrs.write_text("* filter=x\n")
            return {"env": mine | {"GIT_CONFIG_PARAMETERS": f"'core.attributesfile'='{attrs}'"}}

        def git_index(sb):                           # a FIFO as the index would block git check-ignore, which reads it
            gitignore = sb.repo / ".gitignore"       # for an ignored file, docs/x.log, outside where code runs from
            gitignore.write_text((gitignore.read_text() if gitignore.exists() else "") + "*.log\n")
            (sb.repo / "docs").mkdir(exist_ok=True)
            (sb.repo / "docs" / "x.log").write_text("x\n")
            sb.commit()
            os.mkfifo(sb.d / "index")
            return {"env": mine | {"GIT_INDEX_FILE": str(sb.d / "index"), "DRIFT_GIT_TIMEOUT": "2"}}

        def bash_env(sb):
            f = sb.d / "bash_env.sh"
            f.write_text("DRIFT_GIT_TIMEOUT=7\n")
            return {"env": {"BASH_ENV": str(f)},
                    "notes": ["test hooks set: DRIFT_ENGINE_RE DRIFT_GIT_TIMEOUT DRIFT_OWNER",
                              "BASH_ENV is set: bash ran that file before this check, and what it set there can change "
                              "what the check says"]}
        # bash's options and functions from the environment: turned off and removed before the check reads a file
        drop_in = lambda sb: sb.sys / "etc/systemd/system/user-1003.slice.d/50-memory.conf"     # noqa: E731

        def options(shellopts=None, bashopts=None, functions=None, changed=False, named="", hidden=False, history=False):
            # with an environment of a few variables only: under keyword (-k), a check that kept it would run export
            # with no word, which prints every exported variable with its value.  hidden: an editor's lock in the repo's
            # drop-in directory, committed, which systemd does not read (dotglob would make it one of the drop-ins)
            def prepare(sb):
                if changed:                          # an installed drop-in that differs from the repo's
                    drop_in(sb).write_text("[Slice]\nMemoryMax=8G\nMemorySwapMax=0\n")
                if hidden:
                    (sb.repo / "systemd" / "user-1003.slice.d" / ".#50-memory.conf").write_text("[Slice]\nMemoryMax=1G\n")
                    sb.commit()
                keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME"}
                env = {k: None for k in os.environ if k not in keep} | {"SHELLOPTS": shellopts, "BASHOPTS": bashopts} \
                    | {f"BASH_FUNC_{f}%%": body for f, body in (functions or {}).items()} \
                    | ({"HISTFILE": str(sb.d / "history")} if history else {})
                said = {"env": env, "absent": ["\nexport ", "declare -x", "no match"],
                        "notes": [f"bash had options or functions from its environment or a BASH_ENV file ({named}): "
                                  "this check turned off the options that change what it says, and removed the "
                                  "functions, before it read anything"],
                        "then": lambda r: self.assertFalse((sb.d / "history").exists(), "history written")}
                return said | ({"drift": f"DRIFT  {drop_in(sb)} differs from the repo copy (diff it)"} if changed else {})
            return prepare

        def left(function):                          # a function named after a builtin that removes the functions:
            def prepare(sb):                         # found, and the check stops
                drop_in(sb).write_text("[Slice]\nMemoryMax=8G\nMemorySwapMax=0\n")
                keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME"}
                said = LEFT
                return {"env": {k: None for k in os.environ if k not in keep}
                        | {f"BASH_FUNC_{function}%%": "() { :; }", "BASH_FUNC_cmp%%": "() { return 0; }"},
                        "drift": said, "then": lambda r: self.assertEqual(
                            [l for l in r.stdout.splitlines() if l.startswith("DRIFT")], [said])}
            return prepare

        def cdpath(sb):                              # run as scripts/check-drift.sh from its checkout, under CDPATH, where
            return {"env": {"CDPATH": ".:/tmp"}, "script": Path("scripts/check-drift.sh"),    # cd prints where it
                    "cwd": str(sb.repo)}                                                      # goes
        self.each([(f.__name__, f) for f in (git_dir, git_c, git_index, bash_env)]
                  + [("SHELLOPTS=noglob", options("noglob", changed=True, named="SHELLOPTS")),   # *.d would match
                     ("SHELLOPTS=errexit", options("errexit", changed=True, named="SHELLOPTS")),  # cmp's 1 would end it
                     ("SHELLOPTS and BASHOPTS", options("errexit:noglob:allexport:keyword:noclobber:posix",  # nothing
                                                        "nullglob:failglob:dotglob:nocaseglob:nocasematch:extglob:"
                                                        "globstar:lastpipe:xpg_echo:expand_aliases",
                                                        named="BASHOPTS SHELLOPTS", hidden=True)),
                     ("an exported cmp", options(functions={"cmp": "() { return 0; }"}, changed=True,  # would say equal
                                                 named="cmp()")),
                     ("SHELLOPTS=history and a HISTFILE", options("history", named="SHELLOPTS", history=True)),
                     ("an exported unset", left("unset")), ("an exported compgen", left("compgen")),
                     ("CDPATH and a relative call", cdpath)])

    def test_functions_named_after_the_builtins_that_remove_them(self):
        # run as bash SCRIPT, the check removes the functions from the environment before it reads anything: one named
        # read does not hold it in a loop (it reads no line to remove them), one named set or shopt is removed and the
        # options are turned off again; one that is left (named unset, compgen, declare and unset, or one that a
        # BASH_ENV file made readonly) stops it, also alone.  Run by its path, as scripts/deploy.sh runs it, it is under
        # bash -p, which takes no function, no option from the environment and runs no BASH_ENV file: even one named
        # compgen beside one named declare, which the lines in the script cannot find, changes nothing
        drop_in = lambda sb: sb.sys / "etc/systemd/system/user-1003.slice.d/50-memory.conf"     # noqa: E731
        keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME"}

        def functions(names, extra=None, says=None, notes=(), by_path=False, cmp=True):
            def prepare(sb):
                drop_in(sb).write_text("[Slice]\nMemoryMax=8G\nMemorySwapMax=0\n")
                env = {k: None for k in os.environ if k not in keep} \
                    | {f"BASH_FUNC_{n}%%": body for n, body in names.items()} \
                    | ({"BASH_FUNC_cmp%%": "() { return 0; }"} if cmp else {}) | (extra(sb) if extra else {})
                said = says or f"DRIFT  {drop_in(sb)} differs from the repo copy (diff it)"
                return {"env": env, "drift": said, "notes": list(notes), "by_path": by_path,
                        "then": lambda r: self.assertEqual(
                            [l for l in r.stdout.splitlines() if l.startswith("DRIFT")], [said])}
            return prepare

        def readonly(sb):
            f = sb.d / "bash_env.sh"
            f.write_text("g() { :; }\nreadonly -f g\n")
            return {"BASH_ENV": str(f)}

        def dotglob(sb):                             # an editor's lock in the repo's drop-in directory: no drop-in
            (sb.repo / "systemd" / "user-1003.slice.d" / ".#50-memory.conf").write_text("[Slice]\nMemoryMax=1G\n")
            sb.commit()
            return {"BASHOPTS": "dotglob"}

        def by_path_env(sb):
            f = sb.d / "bash_env.sh"
            f.write_text("DRIFT_GIT_TIMEOUT=7\ncmp() { return 0; }\n")
            return {"BASH_ENV": str(f), "SHELLOPTS": "noglob"}
        quiet = {"set": "() { :; }", "compgen": "() { :; }", "declare": "() { :; }", "shopt": "() { :; }"}

        def history(sb):                             # history turned off again, after set is no function: no HISTFILE
            said = functions({"set": "() { :; }"}, lambda sb: {"SHELLOPTS": "history", "HISTFILE": str(sb.d / "history")})(sb)
            return said | {"then": lambda r: self.assertFalse((sb.d / "history").exists(), "history written")}
        self.each([
            ("an exported read, and f", functions({"read": "() { return 0; }"}, lambda sb: {"f": "x"},
                                                  notes=["bash had options or functions from its environment or a "
                                                         "BASH_ENV file (cmp() read()): this check turned off"])),
            ("an exported set, under noglob", functions({"set": "() { :; }"}, lambda sb: {"SHELLOPTS": "noglob"})),
            ("an exported shopt, under dotglob", functions({"shopt": "() { :; }"}, dotglob)),
            ("exported declare and unset", functions({"declare": "() { :; }", "unset": "() { :; }"}, says=LEFT)),
            ("an exported unset alone", functions({"unset": "() { :; }"}, says=LEFT, cmp=False)),
            ("an exported compgen alone", functions({"compgen": "() { :; }"}, says=LEFT, cmp=False)),
            ("a readonly function of a BASH_ENV file", functions({}, readonly, says=LEFT, cmp=False)),
            ("by its path: set, compgen, declare, shopt and BASH_ENV", functions(
                quiet, by_path_env, by_path=True,
                notes=["BASH_ENV is set: bash ran this check, and runs scripts/host-units.sh, under -p, which does "
                       "not run that file",
                       "bash had options or functions in its environment (SHELLOPTS set() compgen() declare() shopt() "
                       "cmp()): it ran this check, and runs scripts/host-units.sh, under -p, which does not take them",
                       "test hooks set: DRIFT_ENGINE_RE DRIFT_OWNER DRIFT_SYSROOT DRIFT_TEST_SPEC DRIFT_URL"])),
            ("by its path, as it is", lambda sb: {"by_path": True}),
            ("an exported set, under history", history),
        ], timeout=90)

    def test_the_host_units_script_takes_no_function(self):
        # the repository's host-units.sh, with a list of one unit that the stand-in systemctl cannot answer for (drift):
        # a function named systemctl, exported or a BASH_ENV file's, does not answer for it, whether the check runs by
        # its path (bash -p, which leaves the functions in the environment of what it runs) or as bash SCRIPT; under -p
        # a note names them, and says that neither runs the BASH_ENV file
        said = "DRIFT  system foo.service is unreadable / inactive, want masked and not running"
        fake = '() { [ "$1" = --user ] && shift; case $1 in is-enabled) echo masked ;; is-active) echo inactive ;; esac; }'

        def case(env, by_path, notes=()):
            def prepare(sb):
                shutil.copy2(ROOT / "scripts" / "host-units.sh", sb.repo / "scripts" / "host-units.sh")
                (sb.repo / "config" / "host-units.conf").write_text("masked system foo.service\n")
                sb.commit()
                f = sb.d / "bash_env.sh"
                f.write_text("systemctl " + fake + "\n")
                return {"drift": said, "by_path": by_path, "notes": list(notes),
                        "env": {k: (str(f) if v == "FILE" else v) for k, v in env.items()},
                        "then": lambda r: self.assertEqual([l for l in r.stdout.splitlines() if l.startswith("DRIFT")],
                                                           [said])}
            return prepare
        exported = {"BASH_FUNC_systemctl%%": fake}
        self.each([
            ("by its path, an exported systemctl", case(exported, True, [
                "bash had options or functions in its environment (systemctl()): it ran this check, and runs "
                "scripts/host-units.sh, under -p, which does not take them"])),
            ("by its path, a BASH_ENV file's systemctl", case({"BASH_ENV": "FILE"}, True, [
                "BASH_ENV is set: bash ran this check, and runs scripts/host-units.sh, under -p, which does not run "
                "that file"])),
            ("as bash SCRIPT, an exported systemctl", case(exported, False)),
            ("as bash SCRIPT, a BASH_ENV file's systemctl", case({"BASH_ENV": "FILE"}, False)),
            ("as it is, by its path", case({}, True)),
        ], timeout=120)

    def test_a_git_command_that_does_not_end(self):
        # git reads a repository's own files as they are (git read-tree and git check-ignore read .git/info/exclude): a
        # FIFO there blocks it.  The check gives a git command DRIFT_GIT_TIMEOUT s (300 by default) and says which step
        # failed, and which command did not end within that time
        def fifo(sb):
            exclude = sb.repo / ".git" / "info" / "exclude"
            exclude.unlink(missing_ok=True)
            os.mkfifo(exclude)

        def blocked(sb):
            fifo(sb)
            return {"drift": f"{sb.repo}: git read-tree of its commit failed", "env": {"DRIFT_GIT_TIMEOUT": "2"},
                    "then": lambda r: self.assertIn("DRIFT  git did not end within 2 s, and was taken to have failed: "
                                                    f"git read-tree in {sb.repo}\n", r.stdout)}
        def three(sb):                               # one in each tree: the Strata tree, the service's checkout, and
            clone = sb.d / "clone"                   # this check's own, another clone at its commit: all named
            subprocess.run(["git", "clone", "-q", str(sb.repo), str(clone)], check=True, capture_output=True)
            for repo in (sb.strata, sb.repo, clone):
                exclude = repo / ".git" / "info" / "exclude"
                exclude.parent.mkdir(exist_ok=True)
                exclude.unlink(missing_ok=True)
                os.mkfifo(exclude)
            return {"drift": f"{sb.repo}: git read-tree of its commit failed", "env": {"DRIFT_GIT_TIMEOUT": "2"},
                    "script": clone / "scripts" / "check-drift.sh",
                    "then": lambda r: self.assertIn("DRIFT  git did not end within 2 s, and was taken to have failed: "
                                                    f"git read-tree in {sb.strata}, git read-tree in {sb.repo}, "
                                                    f"git read-tree in {clone}\n", r.stdout)}
        # and without the hook it waits 300 s: after 45 s, it still waits (a run of 45 s, beside the others)
        sb = Sandbox(self)
        fifo(sb)
        waited = {}
        default = threading.Thread(target=lambda: waited.update(r=sb.run(env={"DRIFT_GIT_TIMEOUT": None}, timeout=45)))
        default.start()
        try:
            self.each([("a FIFO at .git/info/exclude", blocked), ("three of them", three)], timeout=60)
        finally:
            default.join(120)
        self.assertIsNone(waited["r"].returncode, waited["r"].stdout[-2000:])
        self.assertIn("(the check did not end within 45 s)", waited["r"].stderr)

    def test_what_changes_where_it_imports_from_after_the_start(self):
        # an entry added, removed or renamed after the start where the service imports from is noted, with the
        # entries newer than the start: it changes what runs only when a module it had imported is gone, which the
        # check cannot tell.  A module added there, or one of the commit's removed, is drift as it is before a start
        def note(d, newer=None):
            return (f"{d}, where the service imports from, changed after its start ("
                    + (f"newer than it: {newer}" if newer else "no entry there is newer than it") + ")")

        def shared(sb):                              # server.py writes and removes it (POST /settings)
            sb.start_service()
            f = sb.strata / "strata-iq3_xxs.shared-settings.json"
            f.write_text('{"temperature": 0.1}')
            f.unlink()
            return {"notes": [note(sb.strata)]}

        def log(sb):                                 # server.py opens its log to append: made at the first start
            sb.start_service()
            open(sb.strata / "strata-iq3_xxs.log", "a").close()
            return {"notes": [note(sb.strata, "strata-iq3_xxs.log")]}

        def swap(sb):                                # an editor's swap file, made and removed
            sb.start_service()
            f = sb.strata / "serve" / ".server.py.swp"
            f.write_bytes(b"b0VIM 8.2")
            f.unlink()
            return {"notes": [note(sb.strata / "serve")]}

        def bytecode_and_rotation(sb):               # what Python writes and logrotate makes while it runs
            log = sb.strata / "strata-iq3_xxs.log"
            log.write_text("line\n" * 100)
            sb.start_service()
            py_compile.compile(str(sb.strata / "serve" / "server.py"), doraise=True)
            shutil.copy2(log, log.with_name(log.name + ".1"))
            log.write_text("")
            return {"notes": [note(sb.strata, "serve, strata-iq3_xxs.log, strata-iq3_xxs.log.1"),
                              note(sb.strata / "serve", "__pycache__"),
                              note(sb.strata / "serve" / "__pycache__", f"server.{TAG}.pyc")]}

        def gone_of_the_commit(sb):                  # a module of the commit removed: drift, as before a start
            (sb.strata / "serve" / "other.py").write_text("x = 1\n")
            sb.commit_strata("serve/other.py")
            sb.write_list()
            sb.commit()
            sb.start_service()
            (sb.strata / "serve" / "other.py").unlink()
            return {"drift": f"{sb.strata} differs from the pinned commit in 1 files: serve/other.py is missing",
                    "notes": [note(sb.strata / "serve")]}

        def added(sb):                               # a module added after the start: drift, as before a start
            sb.start_service()
            (sb.strata / "serve" / "json.py").write_text("raise SystemExit\n")
            return {"drift": "has modules where server.py imports from that the pinned commit does not have and the "
                             "list does not name: serve/json.py", "notes": [note(sb.strata / "serve", "json.py")]}

        def gone_of_no_commit(sb):                   # one that was drift at the start, gone: only the note now
            (sb.strata / "serve" / "zz_mod.py").write_text("x = 1\n")
            sb.start_service()
            (sb.strata / "serve" / "zz_mod.py").unlink()
            return {"notes": [note(sb.strata / "serve")]}

        def pth_gone(sb):                            # a .pth in the venv that ran at the start, removed
            (sb.site / "zz.pth").write_text("import os\n")
            sb.start_service()
            (sb.site / "zz.pth").unlink()
            return {"notes": [note(sb.site)]}

        def renamed(sb):                             # to a name Python does not import
            (sb.strata / "serve" / "zz_mod.py").write_text("x = 1\n")
            sb.start_service()
            os.rename(sb.strata / "serve" / "zz_mod.py", sb.strata / "serve" / "zz_mod.py.off")
            return {"notes": [note(sb.strata / "serve", "zz_mod.py.off")]}

        def bytecode_gone(sb):                       # wrong bytecode that Python ran instead of its source, removed
            src = sb.strata / "serve" / "server.py"
            (src.parent / "__pycache__").mkdir()
            (src.parent / "__pycache__" / f"server.{TAG}.pyc").write_bytes(pyc_of(src, "raise SystemExit"))
            sb.start_service()
            (src.parent / "__pycache__" / f"server.{TAG}.pyc").unlink()
            return {"notes": [note(src.parent / "__pycache__")]}

        def mode(sb):                                # a directory's own mode: its ctime only
            sb.start_service()
            os.chmod(sb.strata / ".venv" / "bin", 0o700)
            return {"notes": [note(sb.strata / ".venv" / "bin")]}

        def many(sb):                                # 11 directories changed: a note for each of the first 8, which
            dirs = [sb.strata / ".venv" / f"zz{i}" for i in range(10)]          # names up to 4 of its newer
            for d in dirs:                                                      # entries, and a count of the others
                d.mkdir()
            sb.start_service()
            for d in dirs:
                os.utime(d)
            for i in range(6):
                (sb.strata / f"n{i}.txt").write_text("")

            def then(r):
                said = [l for l in r.stdout.splitlines()
                        if l.startswith("note   ") and ", where the service imports from, changed after its start (" in l]
                self.assertEqual(len(said), 8, said)
                self.assertIn(f"note   {note(sb.strata, 'n0.txt, n1.txt, n2.txt, n3.txt')}", r.stdout)
                self.assertIn("note   and 3 more directories where the service imports from changed after its start\n",
                              r.stdout)
            return {"then": then}
        self.each([(f.__name__, f) for f in (shared, log, swap, bytecode_and_rotation, gone_of_the_commit, added,
                                             gone_of_no_commit, pth_gone, renamed, bytecode_gone, mode, many)])

    def test_an_inode_that_changed_but_not_its_content(self):
        # a file the service read at its start whose inode alone changed after it (a link, its mode, or a content put
        # back with its old time) is drift; but one that the engine maps, the very file, holds what the engine maps:
        # noted, when its content is the list's (with --full for one over 1 GiB).  A new modification time is drift,
        # also with the same content
        index = lambda sb: sb.dev / "Strata-data" / "packs" / "p" / "index.txt"           # noqa: E731
        mod = lambda sb: sb.site / "pkg" / "mod.py"                                        # noqa: E731

        def restart(*paths):                         # a new modification time
            return f"1 files the service read at its start changed after it: restart pending ({paths[0]})"

        def relinked(*paths):                        # its inode alone, of a file the engine does not map
            return (f"{len(paths)} files the service read at its start changed after it, though not their modification "
                    f"time: a link, their mode or their owner changed, or a content was put back with its old "
                    f"modification time (cp -p), and then the service runs what it read before: restart pending "
                    f"({', '.join(map(str, paths))})")

        def kept(*paths):                            # its inode alone, of a file the engine maps
            return (f"{len(paths)} files that the service read at its start and that the engine maps, these very files: "
                    f"their inode changed after the start (a link, their mode or their owner), not their content, which "
                    f"is what the list says ({', '.join(map(str, paths))})")

        def big_unknown(path):
            return (f"1 files over 1 GiB that the service read at its start and that the engine maps, these very files: "
                    f"their inode changed after the start (a link, their mode or their owner), and their content is hashed "
                    f"only with --full: run it to know whether a restart is pending ({path})")

        def started(sb, *mapped):                    # the engine maps the library outside ~/dev and these
            sb.maps(sb.abs_file, *mapped)
            sb.start_service()

        def link_removed(mapped):                    # IQ3_S's shard 2 is a hard link of IQ3_XXS's, and the engine maps it
            def prepare(sb):
                other = sb.dev / "Strata-data" / "other-name.txt"
                os.link(index(sb), other)
                started(sb, *[index(sb)] * mapped)
                other.unlink()
                return {"notes": [kept(index(sb))]} if mapped else {"drift": relinked(index(sb))}
            return prepare

        def chmod(mapped):
            def prepare(sb):
                started(sb, *[index(sb)] * mapped)
                os.chmod(index(sb), 0o600)
                return {"notes": [kept(index(sb))]} if mapped else {"drift": relinked(index(sb))}
            return prepare

        def put_back(sb):                            # changed before the start, put back with its old time after it
            subprocess.run(["cp", "-p", str(index(sb)), str(sb.d / "index.orig")], check=True)
            index(sb).write_text("changed\n")
            started(sb)
            subprocess.run(["cp", "-p", str(sb.d / "index.orig"), str(index(sb))], check=True)
            return {"drift": relinked(index(sb))}

        def replaced(sb):                            # the engine maps the file it opened; another one is there now
            started(sb, index(sb))
            shutil.copy2(index(sb), sb.d / "index.new")      # the same content and time, another inode
            os.replace(sb.d / "index.new", index(sb))
            return {"drift": relinked(index(sb))}

        def touched(mapped):                         # the same content, a new time
            def prepare(sb):
                started(sb, *[index(sb)] * mapped)
                os.utime(index(sb))
                return {"drift": restart(index(sb))}
            return prepare

        def not_as_listed(sb):                       # mapped, but its content is not what the list says
            os.utime(index(sb), (1, 1))
            index(sb).write_bytes(b"other\n")
            os.utime(index(sb), (1, 1))
            started(sb, index(sb))
            os.chmod(index(sb), 0o600)
            return {"drift": restart(index(sb))}

        def of_the_commit(sb):                       # a file of the Strata tree, as its commit has it
            started(sb)
            os.chmod(sb.strata / "serve" / "server.py", 0o600)
            return {"drift": relinked(sb.strata / "serve" / "server.py")}

        def installed_unit(sb):                      # its installed unit, as the repo has it: a link of it removed
            unit = sb.sys / "etc/systemd/system/strata-q3.service"
            os.link(unit, sb.d / "unit-link")
            started(sb)
            (sb.d / "unit-link").unlink()
            return {"drift": relinked(unit)}

        def live_config(key):                        # the live config, as the snapshot is (without its key)
            def prepare(sb):
                live = sb.strata / "strata-iq3_xxs.json"
                if key:
                    sb.edit_live(lambda c: c.__setitem__("api_key", "k-not-printed"))
                started(sb)
                os.chmod(live, 0o600)
                return {"drift": relinked(live), "absent": ["k-not-printed"]}
            return prepare

        def big(mapped, full=False):                 # over 1 GiB: hashed only with --full
            def prepare(sb):
                if full:
                    sb.write_list(big=sha_of_zeros(sb.big.stat().st_size))
                    sb.commit()
                started(sb, *[sb.big] * mapped)
                os.chmod(sb.big, 0o600)
                want = {"args": ("--full",)} if full else {}
                if not mapped:
                    return {**want, "drift": relinked(sb.big)}
                return {**want, "notes": [kept(sb.big) if full else big_unknown(sb.big)]}
            return prepare

        def big_touched(sb):
            sb.write_list(big=sha_of_zeros(sb.big.stat().st_size))
            sb.commit()
            started(sb, sb.big)
            os.utime(sb.big)
            return {"args": ("--full",), "drift": restart(sb.big)}

        def venv_rewritten(sb):                      # changed before the start, written back after it: drift
            mod(sb).write_text("x = 2\n")
            started(sb)
            mod(sb).write_text("x = 1\n")
            return {"drift": restart(mod(sb))}

        def venv_put_back(sb):                       # put back with its old time (cp -p)
            shutil.copy2(mod(sb), sb.d / "mod.py.orig")
            mod(sb).write_text("x = 2\n")
            started(sb)
            shutil.copy2(sb.d / "mod.py.orig", mod(sb))
            return {"drift": relinked(mod(sb))}
        self.each([("a link removed, mapped", link_removed(True)), ("a link removed, not mapped", link_removed(False)),
                   ("chmod, mapped", chmod(True)), ("chmod, not mapped", chmod(False)),
                   ("cp -p of a changed file, and back", put_back), ("a mapped file replaced", replaced),
                   ("touched, mapped", touched(True)), ("touched, not mapped", touched(False)),
                   ("not as listed, mapped", not_as_listed), ("a file of the commit", of_the_commit),
                   ("the installed unit", installed_unit), ("the live config", live_config(False)),
                   ("the live config with a key", live_config(True)),
                   ("over 1 GiB, mapped", big(True)), ("over 1 GiB, not mapped", big(False)),
                   ("over 1 GiB, mapped, --full", big(True, full=True)),
                   ("over 1 GiB, not mapped, --full", big(False, full=True)), ("over 1 GiB, touched", big_touched),
                   ("the venv, written back", venv_rewritten), ("the venv, put back with its old time", venv_put_back)])

    def test_a_config_value_of_the_wrong_shape(self):
        # a value that server.py refuses (a traceback) or misreads: drift, never a traceback of the check
        def edited(change, says):
            def prepare(sb):
                sb.start_service()
                sb.edit_live(change)
                return {"drift": f"the live config has [{says!r}], which this check does not follow"}
            return prepare
        self.each([
            ("env a list", edited(lambda c: c.__setitem__("env", ["STRATA_X=1"]),
                                  "a value server.py would refuse or misread (env is not a mapping)")),
            ("env a string", edited(lambda c: c.__setitem__("env", "STRATA_X=1"),
                                    "a value server.py would refuse or misread (env is not a mapping)")),
            ("lib_dirs a string", edited(lambda c: c.__setitem__("lib_dirs", "/usr/lib"),
                                         "a value server.py would refuse or misread (lib_dirs is not a list of strings)")),
            ("lib_dirs with a number", edited(lambda c: c.__setitem__("lib_dirs", [1]),
                                              "a value server.py would refuse or misread (lib_dirs is not a list of "
                                              "strings)")),
            ("args a string", edited(lambda c: c.__setitem__("args", "--ctx 4096"),
                                     "a value server.py would refuse or misread (args is not a list of strings)")),
            ("args with a number", edited(lambda c: c["args"].append(4096),
                                          "a value server.py would refuse or misread (args is not a list of strings)")),
            ("gpu a mapping", edited(lambda c: c.__setitem__("gpu", {"0": 1}), "a value server.py would refuse (ValueError)")),
        ])

    def test_drop_ins_that_systemd_has_not_loaded(self):
        # every drop-in directory systemd reads for a unit, in each directory of its search path: <unit>.d, each dash
        # prefix's, <type>.d.  A .conf there that it has not loaded (a new directory: it reads only those that were
        # there at its last daemon-reload) is drift; so is one that a file of its name earlier in its order masks
        def new(rel, unit):
            def prepare(sb):
                f = sb.sys / rel.lstrip("/")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("[Slice]\nMemoryMax=infinity\n" if unit.endswith(".slice") else "[Service]\nNice=5\n")
                return {"drift": f"{unit} (loaded): {rel} is not loaded: run daemon-reload"}
            return prepare

        def vendor(sb):                              # the system's own drop-in, there and loaded; /lib is /usr/lib
            f = sb.sys / "usr/lib/systemd/system/user-.slice.d/10-defaults.conf"
            f.parent.mkdir(parents=True)
            f.write_text("[Slice]\nTasksMax=33%\n")
            os.symlink("usr/lib", sb.sys / "lib")
            return {}

        def masked(sb):                              # an empty one of its name in system.control: not the repo's
            d = "/run/systemd/system.control/user-1003.slice.d"
            (sb.sys / d.lstrip("/")).mkdir(parents=True)
            (sb.sys / d.lstrip("/") / "50-memory.conf").write_text("")
            s = sb.spec["user-1003.slice"]
            s["DropInPaths"] = s["DropInPaths"].replace("/etc/systemd/system/user-1003.slice.d/50-memory.conf",
                                                        f"{d}/50-memory.conf")
            sb.save_spec()
            return {"drift": f"/etc/systemd/system/user-1003.slice.d/50-memory.conf is installed, but "
                             f"{d}/50-memory.conf masks it"}

        def hidden(where):                           # an editor's lock (emacs: a link to user@host.pid:time)
            def prepare(sb):
                d = sb.sys / where.lstrip("/")
                d.mkdir(parents=True, exist_ok=True)
                os.symlink("user@host.1234:1700000000", d / ".#90-x.conf")
                (d / ".90-y.conf").write_text("[Slice]\nMemoryMax=infinity\n")
                return {"absent": [".#90-x.conf", ".90-y.conf"]}
            return prepare

        def lib_apart(sb):                           # /lib and /usr/lib apart (no merged /usr): /lib's comes first
            for top in ("lib", "usr/lib"):
                f = sb.sys / top / "systemd/system/user-.slice.d/10-defaults.conf"
                f.parent.mkdir(parents=True)
                f.write_text(f"[Slice]\nTasksMax=33%\n# {top}\n")
            s = sb.spec["user-1003.slice"]
            s["DropInPaths"] = s["DropInPaths"].replace("/usr/lib/systemd/system/user-.slice.d/",
                                                        "/lib/systemd/system/user-.slice.d/")
            sb.save_spec()
            return {}

        def linked(not_loaded):                      # a drop-in directory that is a link: systemd shows where it leads
            def prepare(sb):
                d = sb.sys / "usr/lib/systemd/system"
                (d / "user-defaults.d").mkdir(parents=True)
                (d / "user-defaults.d" / "10-defaults.conf").write_text("[Slice]\nTasksMax=33%\n")
                os.symlink("user-defaults.d", d / "user-.slice.d")
                s = sb.spec["user-1003.slice"]
                s["DropInPaths"] = s["DropInPaths"].replace("/usr/lib/systemd/system/user-.slice.d/10-defaults.conf",
                                                            "/usr/lib/systemd/system/user-defaults.d/10-defaults.conf")
                sb.save_spec()
                if not not_loaded:
                    return {}
                (d / "user-defaults.d" / "90-x.conf").write_text("[Slice]\nTasksMax=50%\n")
                return {"drift": "user-1003.slice (loaded): /usr/lib/systemd/system/user-defaults.d/90-x.conf is not "
                                 "loaded: run daemon-reload"}
            return prepare

        def nearer_prefix(sb):                       # of one name, the nearer prefix's (strata-q3-) is the one read
            for d in ("strata-q3-.service.d", "strata-.service.d"):
                f = sb.sys / "etc/systemd/system" / d / "90-x.conf"
                f.parent.mkdir(parents=True)
                f.write_text("[Service]\nNice=5\n")
            sb.spec["strata-q3-health.service"]["DropInPaths"] = "/etc/systemd/system/strata-q3-.service.d/90-x.conf"
            sb.save_spec()
            return {"drift": "strata-q3-health.service (loaded): drop-ins override it: "
                             "/etc/systemd/system/strata-q3-.service.d/90-x.conf",
                    "absent": ["strata-q3-health.service (loaded): /etc/systemd/system/strata-.service.d/90-x.conf is "
                               "not loaded"]}

        def no_conf(d):                              # what systemd does not read: a name without .conf (one in
            def prepare(sb):                         # each directory: one of a name would mask the other)
                f = sb.sys / "etc/systemd/system" / d / "README"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("notes\n")
                return {"absent": ["README"]}
            return prepare
        self.each([
            ("a new prefix directory", new("/etc/systemd/system/user-.slice.d/90-x.conf", "user-1003.slice")),
            ("a new slice.d", new("/run/systemd/system/slice.d/90-x.conf", "user-1003.slice")),
            ("a prefix of a service", new("/etc/systemd/system/strata-.service.d/90-x.conf", "strata-q3.service")),
            ("a longer prefix", new("/run/systemd/system/strata-q3-.service.d/90-x.conf", "strata-q3-health.service")),
            ("the prefix of a prefix", new("/etc/systemd/system/strata-.service.d/90-x.conf", "strata-q3-health.service")),
            ("the system's service.d", new("/usr/lib/systemd/system/service.d/90-x.conf", "zfs-arc-max.service")),
            ("a timer's", new("/usr/local/lib/systemd/system/timer.d/90-x.conf", "strata-q3-health.timer")),
            ("the system's own, loaded", vendor), ("masked", masked),
            ("hidden names in a prefix directory", hidden("/etc/systemd/system/user-.slice.d")),
            ("hidden names beside the repo's drop-in", hidden("/etc/systemd/system/user-1003.slice.d")),
            ("/lib and /usr/lib apart", lib_apart), ("a linked directory", linked(False)),
            ("a linked directory, a .conf not loaded", linked(True)), ("the nearer prefix", nearer_prefix),
            ("a name without .conf, a prefix's directory", no_conf("user-.slice.d")),
            ("a name without .conf, the unit's directory", no_conf("user-1003.slice.d"))])

    def test_who_gets_the_key_among_several_listeners(self):
        # only when every socket that listens on the port is server.py's (its own, or a child's) does it get the key;
        # a connection on the port that is no listener (TIME_WAIT, inode 0) is no listener
        secret = "k-SECRET-not-to-print"

        def keyed(sb, **kw):
            sb.edit_live(lambda c: c.__setitem__("api_key", secret))
            sb.start_service(key=secret, **kw)
            return int(sb.url.rsplit(":", 1)[1])

        def listen(sb, family, host, port):
            s_ = socket.socket(family, socket.SOCK_STREAM)
            self.addCleanup(s_.close)
            s_.bind((host, port))
            s_.listen()
            return {"drift": f"another process than server.py (its main process or a child) listens on port {port}: it "
                             "gets no key", "absent": [secret, "GET /settings"]}    # nothing asked of it at all

        def another_v4(sb):                          # beside server.py's socket, another's on another address
            return listen(sb, socket.AF_INET, "127.0.0.2", keyed(sb))

        def another_v6(sb):
            return listen(sb, socket.AF_INET6, "::1", keyed(sb))

        def time_wait(sb):                           # a closed connection that the server ended: TIME_WAIT, inode 0
            port = keyed(sb)
            with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
                c.sendall(b"GET /nothing HTTP/1.0\r\n\r\n")
                while c.recv(4096):                  # to its end: the server closes first
                    pass
            for _ in range(200):
                if any(l.split()[3] == "06" and int(l.split()[1].rsplit(":", 1)[1], 16) == port
                       for l in Path("/proc/net/tcp").read_text().splitlines()[1:]):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("no connection in TIME_WAIT on the server's port")
            return {"absent": [secret]}

        def child_holds_it(sb):                      # only a child of server.py holds the listening socket
            keyed(sb, child_socket=True)
            return {"absent": [secret]}
        cases = [("another listener (IPv4)", another_v4), ("a connection in TIME_WAIT", time_wait),
                 ("a child's socket", child_holds_it)]
        if socket.has_ipv6:
            with contextlib.suppress(OSError), socket.socket(socket.AF_INET6) as s_:
                s_.bind(("::1", 0))
                cases.append(("another listener (IPv6)", another_v6))
        def go(case):
            sb = Sandbox(self)
            want = case[1](sb)
            return sb, want, sb.run()
        for (name, _), (sb, want, r) in zip(cases, together(go, cases)):
            with self.subTest(name):
                if want.get("drift"):
                    self.drift(None, want["drift"], r=r)
                    self.assertNotIn(f"Bearer {secret}", sb.seen)
                else:
                    self.clean(None, r=r)
                    self.assertIn(f"Bearer {secret}", sb.seen)
                for t in want.get("absent", ()):
                    self.assertNotIn(t, r.stdout + r.stderr)

    def test_the_key_and_a_listener_that_took_the_port_meanwhile(self):
        # the check looks at the listeners, compares the engine (seconds), and only then asks GET /settings with the
        # key: when server.py is gone meanwhile, nothing goes to the port, neither to another listener there nor to
        # the socket that server.py held and that still listens.  A copy of the check waits at that point until this
        # test has made the change
        secret = "k-SECRET-not-to-print"

        def paused(change, kill=True, listen6=False):
            sb = Sandbox(self)
            sb.edit_live(lambda c: c.__setitem__("api_key", secret))
            sb.start_service(key=secret, listen6=sb.d / "listen6" if listen6 else None)
            port = int(sb.url.rsplit(":", 1)[1])
            at = "answers = bool(socks) and socks <= ours\n"
            text = (sb.repo / "scripts" / "check-drift.sh").read_text()
            self.assertEqual(text.count(at), 1)
            pause = sb.d / "pause"
            script = sb.repo / "scripts" / "check-drift-paused.sh"
            script.write_text(text.replace(at, at + "open(%r, 'w').close()\nfor _ in range(2400):\n"
                                           "    if os.path.exists(%r):\n        break\n    time.sleep(0.05)\n"
                                           % (f"{pause}.reached", f"{pause}.go")))
            out = {}
            check = threading.Thread(target=lambda: out.update(r=sb.run(script=script, timeout=150)))
            check.start()
            for _ in range(1200):
                if Path(f"{pause}.reached").exists() or not check.is_alive():
                    break
                time.sleep(0.05)
            self.assertTrue(Path(f"{pause}.reached").exists(), "the check did not reach the point after its look")
            if kill:                                     # server.py and its engine gone
                os.killpg(sb.proc.pid, signal.SIGKILL)
                sb.proc.wait()
            seen = change(sb, port)
            Path(f"{pause}.go").write_text("")
            check.join(200)
            r = out["r"]
            self.drift(None, f"GET /settings cannot be read (what listens on port {port} changed while this check ran: "
                             "nothing was sent)", r=r)
            self.assertEqual(seen, [])
            self.assertNotIn(secret, r.stdout + r.stderr)

        def another_listener(sb, port):              # its socket closed too, and another listener on the port
            sb.srv.shutdown()
            sb.srv.server_close()
            seen = []

            class Foreign(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append(self.headers.get("Authorization"))
                    self.send_response(404)
                    self.end_headers()

                def log_message(self, *a):
                    pass

            class Reuse(http.server.HTTPServer):
                allow_reuse_address = True
            foreign = Reuse(("127.0.0.1", port), Foreign)
            threading.Thread(target=foreign.serve_forever, daemon=True).start()
            self.addCleanup(foreign.server_close)
            self.addCleanup(foreign.shutdown)
            return seen

        def its_socket_still_listening(sb, port):    # the second look cannot read server.py's sockets: no request
            return sb.seen

        def a_second_socket_of_its_own(sb, port):    # server.py itself listens on [::1] too: not what the check found
            trigger = sb.d / "listen6"
            trigger.write_text(str(port))
            for _ in range(400):
                if Path(f"{trigger}.done").exists():
                    break
                time.sleep(0.05)
            self.assertTrue(Path(f"{trigger}.done").exists(), "server.py did not listen on [::1]")
            return sb.seen
        for change in (another_listener, its_socket_still_listening):
            with self.subTest(change.__name__):
                paused(change)
        ipv6 = False
        if socket.has_ipv6:
            with contextlib.suppress(OSError), socket.socket(socket.AF_INET6) as s_:
                s_.bind(("::1", 0))
                ipv6 = True
        if ipv6:
            with self.subTest("a second socket of its own"):
                paused(a_second_socket_of_its_own, kill=False, listen6=True)

def heredoc_functions(script: Path, after: str, names: set) -> dict:
    """The functions of the python heredoc after the line `after` in a shell script, defined in a namespace."""
    text = script.read_text()
    body = text[text.index(after):]
    body = body[body.index("\n", body.index("<<'EOF'")) + 1:]
    body = body[:body.index("\nEOF\n")]
    nodes = [n for n in ast.parse(body).body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names, names - {n.name for n in nodes}
    ns = {"os": os, "re": re, "locale": locale}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(script), "exec"), ns)
    return ns


@unittest.skipUnless(Path(STRATA, "serve", "server.py").is_file(), "machine: needs the Strata tree at ~/dev/strata")
class ModelOfServerPy(unittest.TestCase):
    """What check-drift.sh's model of server.py makes of a config (the engine's command line, directory and
    environment), against what server.py's own functions make of it: taken from the pinned source with ast, not
    imported, so nothing of server.py runs but these functions.  A config the model does not follow must be said;
    one server.py refuses must not pass."""
    SERVER_FUNCTIONS = {"gpu_list", "layer_split_value", "layer_split_of", "engine_args", "parallel_args",
                        "profile_shape", "learned_profile_args", "hip_visible", "ordered_gpus", "child_env",
                        "split_joined_args", "lift_reasoning_effort_arg", "effort_end_args", "engine_binary"}
    ENV = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "HOME": "/home/u", "PYTHONUNBUFFERED": "1"}

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(STRATA, "serve", "server.py").read_text())
        nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in cls.SERVER_FUNCTIONS) or
                 (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "PARALLEL_MAX" for t in n.targets))]
        cls.srv = {"os": os, "re": re, "struct": struct, "sys": sys, "Path": Path, "json": json,
                   "gpu_speed_scores": lambda gl: None, "hip_speed_scores": lambda gl: None,
                   "gpu_vram_mib": lambda gl, hip=False: None, "hip_env_guard": lambda env, cfg: []}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "server.py", "exec"), cls.srv)
        missing = cls.SERVER_FUNCTIONS - set(cls.srv)
        if missing:
            raise AssertionError(f"server.py no longer has {sorted(missing)}: the check's model needs a review")
        cls.chk = heredoc_functions(ROOT / "scripts" / "check-drift.sh", 'echo "== the running service"',
                                    {"split_joined", "lift_effort", "gpu_list", "layer_split_value", "locale_there",
                                     "expected"})
        cls.work = tempfile.mkdtemp()
        os.mkdir(os.path.join(cls.work, "lib"))
        cls.exe = os.path.join(cls.work, "strata")
        Path(cls.exe).write_bytes(b"... --tail-role-token ...")
        cls.base = json.loads((ROOT / "config" / "strata-iq3_xxs.deployed.json").read_text())
        cls.base["exe"] = cls.exe

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, True)

    def server(self, cfg):
        """What server.py's main() makes of cfg before it starts the engine (StrataEngine: [exe, "--serve", *args])."""
        class Tok:
            def encode(self, s, parse_special=False):
                return [151644] if s == "system" else [1, 2]
        cfg = copy.deepcopy(cfg)
        old_env, old_cwd = dict(os.environ), os.getcwd()
        os.environ.clear()
        os.environ.update(self.ENV)
        os.chdir(self.work)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                s = self.srv
                s["split_joined_args"](cfg)
                s["lift_reasoning_effort_arg"](cfg)
                env = s["child_env"](cfg)
                if s["layer_split_of"](cfg):
                    s["layer_split_value"](cfg)
                exe = cfg["exe"] if os.path.isabs(cfg["exe"]) else os.path.abspath(os.path.join(cfg.get("cwd") or ".", cfg["exe"]))
                effort = s["effort_end_args"](cfg, exe, Tok())
                return [exe, "--serve", *s["engine_args"](cfg), *(effort or [])], os.path.realpath(cfg.get("cwd") or "."), env
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            os.chdir(old_cwd)

    def model(self, cfg):
        self.chk["server_env"], self.chk["server_cwd"] = dict(self.ENV), self.work
        return self.chk["expected"](copy.deepcopy(cfg))

    def test_the_model_is_server_pys(self):
        def rep(old, new):
            def f(c):
                i = c["args"].index(old)
                c["args"][i:i + 2] = new
            return f
        same = [
            ("the deployed config", lambda c: None),
            ('"--prefill 2048" as one element', rep("--prefill", ["--prefill 2048"])),
            ('"--prefill=2048" as one element', rep("--prefill", ["--prefill=2048"])),
            ("--reasoning-effort high in args", lambda c: c["args"].extend(["--reasoning-effort", "high"])),
            ("--reasoning-effort=high in args", lambda c: c["args"].append("--reasoning-effort=high")),
            ('"--reasoning-effort high" as one element', lambda c: c["args"].append("--reasoning-effort high")),
            ('effort_position "start"', lambda c: c.update(effort_position="start")),
            ('gpu "0,1,2,3"', lambda c: c.update(gpu="0,1,2,3")),
            ("gpu 2", lambda c: c.update(gpu=2)),
            ("gpu []", lambda c: c.update(gpu=[])),
            ('gpu ""', lambda c: c.update(gpu="")),
            ('layer_split "13,25,38"', lambda c: c.update(layer_split="13,25,38")),
            ('layer_split " 13, 25 ,38 "', lambda c: c.update(layer_split=" 13, 25 ,38 ")),
            ('layer_split ["13", "25", "38"]', lambda c: c.update(layer_split=["13", "25", "38"])),
            ('layer_split "auto"', lambda c: c.update(layer_split="auto")),
            ('layer_split " AUTO "', lambda c: c.update(layer_split=" AUTO ")),
            ("layer_split null", lambda c: c.update(layer_split=None)),
            ("layer_split 13 with 2 cards", lambda c: c.update(layer_split=13, gpu=[0, 1])),
            ("parallel true", lambda c: c.update(parallel=True)),
            ('parallel "4"', lambda c: c.update(parallel="4")),
            ("parallel 4.0", lambda c: c.update(parallel=4.0)),
            ("parallel 1", lambda c: c.update(parallel=1)),
            ("parallel 9", lambda c: c.update(parallel=9)),
            ("parallel 0", lambda c: c.update(parallel=0)),
            ("parallel false", lambda c: c.update(parallel=False)),
            ("env values 1, true, null, 1.5", lambda c: c["env"].update(A=1, B=True, C=None, D=1.5)),
            ("env null", lambda c: c.update(env=None)),
            ("lib_dirs with a missing one", lambda c: c["lib_dirs"].append("/nonexistent/lib")),
            ("lib_dirs relative", lambda c: c.update(lib_dirs=["lib"])),
            ("no lib_dirs", lambda c: c.pop("lib_dirs")),
            ('backend "cuda"', lambda c: c.update(backend="cuda")),
            ("vram_elastic true", lambda c: c.update(vram_elastic=True)),
            ("vram_elastic true, vram_segment_mib 256", lambda c: c.update(vram_elastic=True, vram_segment_mib=256)),
            ("vram_elastic true, vram_segment_mib true", lambda c: c.update(vram_elastic=True, vram_segment_mib=True)),
            ('vram_elastic "true"', lambda c: c.update(vram_elastic="true")),
            ('expert_profile_save ""', lambda c: c.update(expert_profile_save="")),
            ("split_skip_if_fits true", lambda c: c.update(split_skip_if_fits=True)),
            ("split_skip_if_fits true, one card", lambda c: c.update(split_skip_if_fits=True, gpu=1)),
            ("replicas 1", lambda c: c.update(replicas=1)),
            ("replicas false", lambda c: c.update(replicas=False)),
            ("no gpu_order, a layer split given", lambda c: c.pop("gpu_order")),
            ('gpu_order "as_given", layer_split "auto"', lambda c: c.update(layer_split="auto")),
            ("--slots in args", lambda c: c["args"].extend(["--slots", "2"])),
            ("--batch in args", lambda c: c["args"].extend(["--batch", "2"])),
            ("--peer-device in args, 2 cards", lambda c: (c["args"].extend(["--peer-device", "1"]), c.update(gpu=[0, 1]))),
            ("--layer-split in args", lambda c: c["args"].extend(["--layer-split", "10,20,30"])),
            ('"--layer-split 10,20,30" as one element', lambda c: c["args"].append("--layer-split 10,20,30")),
            ("exe relative, cwd absolute", lambda c: c.update(exe="strata", cwd=self.work)),
            ("exe relative, cwd relative", lambda c: c.update(exe="strata", cwd=".")),
            ("exe relative, no cwd", lambda c: (c.update(exe="strata"), c.pop("cwd"))),
            ("cwd relative", lambda c: c.update(cwd="lib")),
            ('a value after a flag that looks like "--a b"', lambda c: c["args"].extend(["--model-name", "--a b"])),
        ]
        not_followed = [
            ('effort_position "end"', lambda c: c.update(effort_position="end"), "effort_position"),
            ('backend "hip"', lambda c: c.update(backend="hip"), "backend"),
            ('expert_profile_save "x.bin"', lambda c: c.update(expert_profile_save="x.bin"), "expert_profile_save"),
            ("replicas 2", lambda c: c.update(replicas=2), "replicas"),
            ("no gpu_order, layer_split auto", lambda c: (c.pop("gpu_order"), c.update(layer_split="auto")), "gpu_order"),
            ("vision {}", lambda c: c.update(vision={}), "vision"),
        ]
        refused = [
            ("layer_split [13.0, 25, 38]", lambda c: c.update(layer_split=[13.0, 25, 38])),
            ("layer_split [25, 13, 38]", lambda c: c.update(layer_split=[25, 13, 38])),
            ("layer_split [1, 2, 3]", lambda c: c.update(layer_split=[1, 2, 3])),
            ("layer_split [13, 25] with 4 cards", lambda c: c.update(layer_split=[13, 25])),
            ("layer_split [true, 25, 38]", lambda c: c.update(layer_split=[True, 25, 38])),
            ("layer_split {}", lambda c: c.update(layer_split={})),
            ("layer_split 13.5", lambda c: c.update(layer_split=13.5)),
            ('effort_position "middle"', lambda c: c.update(effort_position="middle")),
        ]
        for name, change in same:
            with self.subTest(name):
                cfg = copy.deepcopy(self.base)
                change(cfg)
                argv, cwd, env = self.server(cfg)
                m_argv, m_cwd, m_env, odd = self.model(cfg)
                self.assertEqual(odd, [])
                self.assertEqual((m_argv, m_cwd), (argv, cwd))
                self.assertEqual(m_env, env)
        for name, change, key in not_followed:
            with self.subTest(name):
                cfg = copy.deepcopy(self.base)
                change(cfg)
                self.assertIn(key, self.model(cfg)[3])
        for name, change in refused:
            with self.subTest(name):
                cfg = copy.deepcopy(self.base)
                change(cfg)
                with self.assertRaises(ValueError):
                    self.server(cfg)
                try:
                    odd = self.model(cfg)[3]
                except ValueError:
                    continue
                self.assertTrue(odd, "the model passes a config that server.py refuses")
        misshapen = [                                   # server.py refuses (a traceback) or misreads these
            ("env a list", lambda c: c.update(env=["A=1"]), AttributeError),
            ("env a string", lambda c: c.update(env="A=1"), AttributeError),
            ("lib_dirs [1]", lambda c: c.update(lib_dirs=[1]), TypeError),
            ("lib_dirs a string", lambda c: c.update(lib_dirs="/usr/lib"), None),     # each letter a directory
            ("args a string", lambda c: c.update(args="--ctx 4096"), None),          # each letter an argument
            ("args with a number", lambda c: c["args"].append(4096), None),          # Popen refuses it
        ]
        for name, change, refuses in misshapen:
            with self.subTest(name):
                cfg = copy.deepcopy(self.base)
                change(cfg)
                if refuses:
                    with self.assertRaises(refuses):
                        self.server(cfg)
                with self.assertRaises(TypeError) as cm:
                    self.model(cfg)
                self.assertEqual(cm.exception.args[0], "shape")


if __name__ == "__main__":
    unittest.main()
