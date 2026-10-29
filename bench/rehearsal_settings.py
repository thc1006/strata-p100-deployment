#!/usr/bin/python3 -I
# bench/rehearsal_settings.py: R0's look at setup.py's settings inside the rebuild rehearsal's sandbox (the rehearsal's
# r0-settings.py), run inside bench/rehearsal_rr.sh by bench/rehearsal_selftest.sh from a copy in
# ~/rebuild-rehearsal/verify.  It writes nothing.  In bench/: run it only when the maintainer asks (AGENTS.md).
"""Inside rr: setup.py's own settings_path() and load_settings(), taken from production's setup.py source with ast
(no other code of setup.py runs), and where the paths the real settings name resolve inside the namespace."""
import ast, json, os, sys
from pathlib import Path
src = Path("/home/thc1006/rebuild-rehearsal/prod/dev/strata/setup.py").read_text(encoding="utf-8")
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
for q in [s.get("data_dir")] + list(s.get("installs", [])):
    print(f"  {q} inside: exists {os.path.exists(q)}; it is under the bind of /home/thc1006/dev:",
          os.stat("/home/thc1006/dev").st_ino == os.stat("/home/thc1006/rebuild-rehearsal/root/home/thc1006/dev").st_ino)
