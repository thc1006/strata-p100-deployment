#!/usr/bin/env python3
"""Configs for an engine experiment (bench/ab_engine.sh): the deployed config with only exe / cwd / log moved to the
tree under test and the arm's environment added.  Writes <tree>/<name>.json for each arm and prints each one's
difference from the deployed config, so a reader sees that nothing else changed.
Usage: bench/make_engine_configs.py TREE NAME:KEY=VALUE[,KEY=VALUE...] ...
  e.g. bench/make_engine_configs.py ~/dev/strata-0.1.45 \\
           cfg-gs0:STRATA_MMQ_GROUP_SORT=0,STRATA_MMQ_RESIDENT_SORT_NE=0 \\
           cfg-gs1:STRATA_MMQ_GROUP_SORT=1,STRATA_MMQ_RESIDENT_SORT_NE=0
"""
import json
import sys
from pathlib import Path

DEPLOYED = Path.home() / "dev/strata/strata-iq3_xxs.json"


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    tree = Path(argv[0]).expanduser().resolve()
    if not (tree / "engine-cuda12" / "strata").is_file():
        sys.exit(f"no engine in {tree}")
    base = json.loads(DEPLOYED.read_text())
    for spec in argv[1:]:
        name, _, kvs = spec.partition(":")
        env = dict(kv.split("=", 1) for kv in kvs.split(",") if kv)
        c = json.loads(json.dumps(base))
        c["exe"], c["cwd"], c["log"] = str(tree / "engine-cuda12/strata"), str(tree), str(tree / f"{name}.log")
        c.setdefault("env", {}).update(env)
        out = tree / f"{name}.json"
        out.write_text(json.dumps(c, indent=2) + "\n")
        diff = {k: c[k] for k in c if k not in ("env",) and c[k] != base.get(k)}
        envd = {k: v for k, v in c["env"].items() if (base.get("env") or {}).get(k) != v}
        print(f"{out}: {diff}; env {envd}; args {'same' if c['args'] == base['args'] else 'DIFFER'}")


if __name__ == "__main__":
    main(sys.argv[1:])
