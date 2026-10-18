#!/usr/bin/env python3
"""Configs for an engine experiment (bench/ab_engine.sh): the deployed config with only exe / cwd / log moved to the
tree under test, the arm's environment added and, when given, engine arguments appended (the arms file names them
as the arm's args_add).  Writes <tree>/<name>.json for each arm and prints each one's difference from the deployed
config, so a reader sees that nothing else changed.
Usage: bench/make_engine_configs.py TREE NAME:KEY=VALUE[,KEY=VALUE...][:ARGS TO ADD[:ARGS TO REMOVE]] ...
  (the arguments to remove are one run of the deployed config's arguments, removed from every arm of an experiment
  alike, e.g. "--batch-groups auto" for #2985, which an explicit --batch-groups turns off)
  e.g. bench/make_engine_configs.py ~/dev/strata-0.1.45 \\
           cfg-gs0:STRATA_MMQ_GROUP_SORT=0,STRATA_MMQ_RESIDENT_SORT_NE=0 \\
           "cfg-pp768:STRATA_MMQ_GROUP_SORT=0,STRATA_MMQ_RESIDENT_SORT_NE=0:--prefill-pipe 768"
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
        name, _, rest = spec.partition(":")
        kvs, _, extra = rest.partition(":")
        extra, _, drop = extra.partition(":")
        env = dict(kv.split("=", 1) for kv in kvs.split(",") if kv)
        c = json.loads(json.dumps(base))
        c["exe"], c["cwd"], c["log"] = str(tree / "engine-cuda12/strata"), str(tree), str(tree / f"{name}.log")
        c.setdefault("env", {}).update(env)
        args, gone = list(c.get("args") or []), drop.split()
        if gone:
            at = [i for i in range(len(args) - len(gone) + 1) if args[i:i + len(gone)] == gone]
            if len(at) != 1:
                sys.exit(f"{name}: the deployed args hold {gone} {len(at)} times, not once")
            args = args[:at[0]] + args[at[0] + len(gone):]
        c["args"] = args + extra.split()          # never --flag=value: the engine refuses it
        out = tree / f"{name}.json"
        out.write_text(json.dumps(c, indent=2) + "\n")
        diff = {k: c[k] for k in c if k not in ("env",) and c[k] != base.get(k)}
        envd = {k: v for k, v in c["env"].items() if (base.get("env") or {}).get(k) != v}
        print(f"{out}: {diff}; env {envd}; args - {drop.split() or 'nothing'} + {extra.split() or 'nothing'}")


if __name__ == "__main__":
    main(sys.argv[1:])
