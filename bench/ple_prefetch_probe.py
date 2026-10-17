#!/usr/bin/env python3
"""Physical disk reads and prefill speed of fresh 16K-token prompts (standard library only).

For each prompt: the engine's prompt rate, TTFT, the rpool partition's physical reads (/sys/block/sda/sda4/stat) and
the ZFS ARC's prefetch misses.  On this ZFS box the engine's own "the OS read" line counts ARC hits, so the partition
counter is the storage figure.  With --ple-io ram and the prompt path's per-row MADV_WILLNEED (ngram.cpp), ZFS turns
the hints into whole-record prefetch reads; --no-ple-prefetch removes them.

  python3 bench/ple_prefetch_probe.py --tag deployed --json results/ple_prefetch_probe.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402


def snap() -> dict:
    d = {"sda4_bytes": int(Path("/sys/block/sda/sda4/stat").read_text().split()[2]) * 512}
    for line in Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "prefetch_data_misses":
            d["prefetch_misses"] = int(p[2])
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--json")
    ap.add_argument("--offsets", type=int, nargs="+", default=[777777, 333333, 222222],
                    help="text slices (default: the same three slices as the earlier probes)")
    a = ap.parse_args()
    text = SM.docs_corpus()
    rows = []
    for off in a.offsets:
        p = SM.sized(text, 16000, off % len(text))
        s0 = snap()
        r = SM.brief(SM.chat(p, max_tokens=32))
        s1 = snap()
        rows.append({"offset": off, "prompt_tokens": r.get("prompt_tokens"), "engine_prompt_tok_s": r.get("engine_prompt_tok_s"),
                     "ttft_s": r.get("ttft_s"), "sda4_read_gb": round((s1["sda4_bytes"] - s0["sda4_bytes"]) / 1e9, 3),
                     "prefetch_misses": s1["prefetch_misses"] - s0["prefetch_misses"], "error": r.get("error")})
        print(json.dumps(rows[-1]), flush=True)
    if a.json:
        with open(a.json, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tag": a.tag, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "offsets": a.offsets, "rows": rows}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
