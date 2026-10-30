#!/usr/bin/python3 -I
# bench/rehearsal_settings.py: R0's look at setup.py's settings inside the rebuild rehearsal's sandbox, run inside
# bench/rehearsal_rr.sh by bench/rehearsal_selftest.sh from a copy in ~/rebuild-rehearsal/verify.  It writes nothing.
# In bench/: run it only when the maintainer asks (AGENTS.md).
# It is not the copy that the records ran (~/rebuild-rehearsal/bin/r0-settings.py, sha256 9dd4f79f... in
# results/rebuild-rehearsal-r0-20261030.txt): that copy did not check where it ran, and for each path it printed the
# same inode comparison of /home/thc1006/dev, which did not test the path.
"""Inside rr: setup.py's own settings_path() and load_settings(), taken from production's setup.py source with ast
(no other code of setup.py runs), and where the paths the real settings name resolve inside the namespace.  It refuses
to run (exit 2) unless the sandbox marker is in /home/thc1006/dev and production's tree is on a read-only mount."""
import ast, json, os, sys
from pathlib import Path
TREE = "/home/thc1006/rebuild-rehearsal/prod/dev/strata"
SBDEV = "/home/thc1006/rebuild-rehearsal/root/home/thc1006/dev"
try:
    inside = os.path.exists("/home/thc1006/dev/.rehearsal-sandbox") and bool(os.statvfs(TREE).f_flag & os.ST_RDONLY)
except OSError:
    inside = False
if not inside:
    print("rehearsal_settings: not inside the rehearsal's sandbox: nothing run", file=sys.stderr)
    sys.exit(2)
src = Path(TREE + "/setup.py").read_text(encoding="utf-8")
mod = ast.parse(src)
fns = [n for n in mod.body if isinstance(n, ast.FunctionDef) and n.name in ("settings_path", "load_settings")]
ns = {"WIN": False, "Path": Path, "os": os, "json": json}
exec(compile(ast.Module(body=fns, type_ignores=[]), "setup.py", "exec"), ns)
p = ns["settings_path"]()
print("setup.py settings_path() inside:", p, "| exists:", p.exists())
print("setup.py load_settings() inside:", ns["load_settings"]())
real = Path("/home/thc1006/.config/strata/settings.json")
s = json.loads(real.read_text(encoding="utf-8"))
print("the real ~/.config/strata/settings.json, read-only inside, names:", {k: s[k] for k in ("data_dir", "installs") if k in s})
dev_is_sandbox = os.path.samefile("/home/thc1006/dev", SBDEV)  # device and inode
for q in [s.get("data_dir")] + list(s.get("installs", [])):
    if not q:
        continue
    under = os.path.realpath(q).startswith("/home/thc1006/dev/")
    print(f"  {q} inside: exists {os.path.exists(q)}; resolves under /home/thc1006/dev {under}, "
          f"which is the sandbox's {dev_is_sandbox}")
