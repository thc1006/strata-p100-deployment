#!/usr/bin/env python3
"""Configs for the 0.1.45 PLE-hint A/B: the deployed config with only exe / cwd / log (the tree), the PLE flags and,
for the ext4 arms, the --ple-gguf path changed.  Prints each config's difference from the deployed one."""
import json
from pathlib import Path

H = Path.home()
DEPLOYED = H / "dev/strata/strata-iq3_xxs.json"
EXT4_PLE = "/mnt/ple-ext4/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf"
# The engine takes the model's shards from --native's name (<stem>-0000N-of-0000M.gguf in the same directory) and adds
# --ple-gguf unless it is one of them, so a --ple-gguf in another directory is a second shard 2 ("duplicate split shard
# number").  The ext4 directory therefore holds the copied shard 2 and a symlink to shard 1 on ZFS, and --native
# points there too: shard 1 is still read from ZFS, the PLE table from ext4.
EXT4_NATIVE = "/mnt/ple-ext4/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
base = json.loads(DEPLOYED.read_text())
assert "--no-ple-prefetch" in base["args"] and base["args"][base["args"].index("--ple-io") + 1] == "ram"


def make(tree: str, name: str, *, hint: bool, ple_io: str = "ram", ext4: bool = False) -> None:
    t = H / "dev" / tree
    c = json.loads(json.dumps(base))
    c["exe"], c["cwd"], c["log"] = str(t / "engine-cuda12/strata"), str(t), str(t / f"{name}.log")
    a = c["args"]
    if hint:
        a.remove("--no-ple-prefetch")
    a[a.index("--ple-io") + 1] = ple_io
    if ext4:
        a[a.index("--ple-gguf") + 1] = EXT4_PLE
        a[a.index("--native") + 1] = EXT4_NATIVE
    (t / f"{name}.json").write_text(json.dumps(c, indent=2) + "\n")
    diff = {k: c[k] for k in c if k != "args" and c[k] != base[k]}
    print(f"{t / name}.json  args -{sorted(set(base['args']) - set(a))} +{sorted(set(a) - set(base['args']))}  {diff}")


for tree in ("strata-0.1.45", "strata-0.1.45-locked"):
    make(tree, "cfg-zfs-off", hint=False)
    make(tree, "cfg-zfs-hint", hint=True)
    make(tree, "cfg-zfs-mmap-hint", hint=True, ple_io="mmap")
    make(tree, "cfg-ext4-off", hint=False, ext4=True)
    make(tree, "cfg-ext4-hint", hint=True, ext4=True)
