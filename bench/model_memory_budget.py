#!/usr/bin/env python3
"""Where a GGUF quantization of Qwen3.8-Flash-Next would live on this box (standard library only).

For each model: the bytes of its routed experts, PLE table and dense weights, read from the GGUF tensor headers
(local files, or Hugging Face URLs through HTTP range requests); per layer split, the experts each card would hold
and the share of the expert profile's routing that lands on them; the experts left for RAM.

  python3 bench/model_memory_budget.py --base iq3_xxs \\
      --model iq3_xxs=~/dev/Strata-data/models/IQ3_XXS/<shard 1>,~/dev/Strata-data/models/IQ3_XXS/<shard 2> \\
      --model iq3_s=<shard 1>,<shard 2> --capacity 10.495,10.658,11.840,9.473 --split 13,26,38 \\
      --profile ~/dev/strata/data/expert-profile.bin --reserve 1536,1280,1024

--capacity: what each card can give the experts with the base model at the deployed 1536 MiB reserve (the experts it
holds now plus its free VRAM above the reserve).  A smaller reserve adds the difference to every card.  A model's
dense weights that are larger than the base model's take their room from the card that holds those layers.
The fill approximates the engine's PROFILE policy: every card takes its own layers' experts in the order of their
routing counts; an expert that does not fit in what is left is skipped and a smaller one may still fit.  The engine's
own slot sizing differs in detail (slots per layer), so the shares are estimates; the engine log of a start is the
measurement.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import itertools
import json
import os
import re
import struct
import sys
import urllib.request
from pathlib import Path

GI = 2 ** 30
SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


class Reader:
    """A GGUF file's first bytes, from disk or by HTTP range requests (grown as the header needs)."""

    def __init__(self, where: str):
        self.where, self.buf, self.pos, self.size = where, b"", 0, None
        if not where.startswith("https://"):
            with open(where, "rb") as f:
                f.seek(0, 2)
                self.size = f.tell()

    def need(self, n: int) -> None:
        while self.pos + n > len(self.buf):
            want = max(2 * len(self.buf), 1 << 22, self.pos + n)
            if self.where.startswith("https://"):
                req = urllib.request.Request(self.where, headers={"Range": f"bytes={len(self.buf)}-{want - 1}",
                                                                  "User-Agent": "model-memory-budget/1"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    m = re.search(r"/(\d+)$", r.headers.get("Content-Range") or "")
                    if r.status != 206 or not m:
                        raise OSError(f"{self.where}: no range support (HTTP {r.status})")
                    self.size = int(m.group(1))
                    chunk = r.read()
            else:
                with open(self.where, "rb") as f:
                    f.seek(len(self.buf))
                    chunk = f.read(want - len(self.buf))
            if not chunk:
                raise EOFError(self.where)
            self.buf += chunk

    def take(self, n: int) -> bytes:
        self.need(n)
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def text(self) -> str:
        return self.take(self.u64()).decode("utf-8", "replace")

    def skip_value(self, t: int):
        if t in SCALAR:
            return struct.unpack(SCALAR[t], self.take(struct.calcsize(SCALAR[t])))[0]
        if t == 8:
            return self.text()
        if t == 9:
            et, n = self.u32(), self.u64()
            if et in SCALAR:
                self.take(struct.calcsize(SCALAR[et]) * n)
            else:
                for _ in range(n):
                    self.skip_value(et)
            return None
        raise ValueError(f"{self.where}: GGUF value type {t}")


def tensors(where: str) -> list[dict]:
    """name, type id, offset and bytes of every tensor in one shard; a tensor's bytes run to the next offset (the
    last one's to the end of the file), so no table of block sizes is needed."""
    r = Reader(where)
    if r.take(4) != b"GGUF":
        raise ValueError(f"{where}: not a GGUF file")
    r.u32()
    n_tensors, n_kv = r.u64(), r.u64()
    align = 32
    for _ in range(n_kv):
        key, t = r.text(), r.u32()
        v = r.skip_value(t)
        if key == "general.alignment":
            align = int(v)
    out = []
    for _ in range(n_tensors):
        name = r.text()
        dims = [r.u64() for _ in range(r.u32())]
        out.append({"name": name, "type": r.u32(), "dims": dims, "offset": r.u64()})
    start = (r.pos + align - 1) // align * align
    if r.size is None:
        r.need(r.pos + 1)
    order = sorted(out, key=lambda t: t["offset"])
    for a, b in zip(order, order[1:] + [None]):
        a["bytes"] = (b["offset"] if b else r.size - start) - a["offset"]
    return out


# ggml's type ids (ggml.h) for the names the report gives the PLE table's quantization
GGML_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 10: "Q2_K", 11: "Q3_K",
              12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S", 20: "IQ4_NL",
              21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 29: "IQ1_M", 30: "BF16"}


def parts(shards: list[str]) -> dict:
    """GiB per layer of routed experts and of dense weights, the output head, the PLE table (and its types) and the
    rest."""
    exp, dense, head, ple, other, ple_types = [0.0] * 48, [0.0] * 48, 0.0, 0.0, 0.0, set()
    for s in shards:
        for t in tensors(os.path.expanduser(s)):
            m = re.match(r"blk\.(\d+)\.", t["name"])
            g = t["bytes"] / GI
            if "_exps" in t["name"]:
                exp[int(m.group(1))] += g
            elif t["name"].startswith("per_layer_token_embd"):
                ple += g
                ple_types.add(GGML_TYPES.get(t["type"], f"type {t['type']}"))
            elif m:
                dense[int(m.group(1))] += g
            elif t["name"].startswith("output"):
                head += g
            else:
                other += g
    return {"experts": exp, "dense": dense, "head": head, "ple": ple, "other": other, "ple_types": sorted(ple_types)}


def read_profile(path: str) -> list[list[int]]:
    """The routing counts of Strata's expert profile: version 1, the ranked pairs, then a layers x experts int32 table."""
    b = Path(os.path.expanduser(path)).read_bytes()
    if b[:4] != b"STRP":
        raise ValueError(f"{path}: not a Strata expert profile")
    version, nl, ne, _, n = struct.unpack("<5I", b[4:24])
    if version != 1 or len(b) != 24 + 4 * n + 4 * nl * ne:
        raise ValueError(f"{path}: version {version} or a size this reader does not know")
    base = 24 + 4 * n
    return [list(struct.unpack(f"<{ne}i", b[base + 4 * l * ne: base + 4 * (l + 1) * ne])) for l in range(nl)]


def held(m: dict, base: dict, counts: list[list[int]], cuts: tuple, cap: list[float], base_cuts: tuple) -> tuple:
    """(share of routing on the cards, GiB of experts left for RAM) for one split and per-card capacity."""
    spans = list(zip((0,) + cuts, cuts + (48,)))
    base_spans = list(zip((0,) + base_cuts, base_cuts + (48,)))
    hit = total = spill = 0.0
    for g, (x, y) in enumerate(spans):
        bx, by = base_spans[g]
        room = cap[g] - (sum(m["dense"][x:y]) - sum(base["dense"][bx:by])) - \
            ((m["head"] - base["head"]) if g == 3 else 0.0)
        pairs = sorted(((counts[l][e], l) for l in range(x, y) for e in range(len(counts[l]))), reverse=True)
        for c, l in pairs:
            size = m["experts"][l] / len(counts[l])
            total += c
            if room >= size:
                room -= size
                hit += c
            else:
                spill += size
    return hit / total, spill


def evaluate(job: tuple) -> tuple:
    """One (model, reserve): the share at the base cuts and the best cuts nearby.  Runs in a worker process."""
    name, m, base, counts, base_cuts, cap0, reserve, search = job
    cap = [c + (1536 - reserve) / 1024 for c in cap0]
    here = held(m, base, counts, base_cuts, cap, base_cuts)
    best = max(((held(m, base, counts, cuts, cap, base_cuts), cuts) for cuts in itertools.product(
        *(range(c - search, c + search + 1) for c in base_cuts)) if cuts[0] < cuts[1] < cuts[2]),
        key=lambda x: x[0][0])
    return name, reserve, here, best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", required=True, help="NAME=SHARD[,SHARD...] (paths or https URLs)")
    ap.add_argument("--base", required=True, help="the model --capacity was measured with")
    ap.add_argument("--capacity", required=True, help="GiB per card for experts at the 1536 MiB reserve, 4 values")
    ap.add_argument("--split", default="13,26,38", help="the base model's cuts")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--reserve", default="1536", help="reserves (MiB) to evaluate, comma-separated")
    ap.add_argument("--search", type=int, default=3, help="also try cuts within this many layers of --split")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help="worker processes (default: all CPUs but one; run under taskset to keep a core free)")
    a = ap.parse_args()
    cap0 = [float(x) for x in a.capacity.split(",")]
    base_cuts = tuple(int(x) for x in a.split.split(","))
    if len(cap0) != 4 or len(base_cuts) != 3:
        ap.error("--capacity needs 4 values and --split 3")
    models = {}
    for spec in a.model:
        name, _, files = spec.partition("=")
        models[name] = parts(files.split(","))
    if a.base not in models:
        ap.error("--base must be one of the --model names")
    counts = read_profile(a.profile)
    base = models[a.base]
    print(f"capacity at 1536 MiB: {' / '.join(f'{c:.3f}' for c in cap0)} GiB ({sum(cap0):.2f}); base {a.base}, "
          f"cuts {a.split}; profile {sum(map(sum, counts)):,} routing events")
    reserves = [int(x) for x in a.reserve.split(",")]
    jobs = [(n, m, base, counts, base_cuts, cap0, r, a.search) for n, m in models.items() for r in reserves]
    with cf.ProcessPoolExecutor(min(a.jobs, len(jobs))) as ex:
        results = {(n, r): (here, best) for n, r, here, best in ex.map(evaluate, jobs)}
    for name, m in models.items():
        per_card = [sum(m["experts"][x:y]) for x, y in zip((0,) + base_cuts, base_cuts + (48,))]
        print(f"\n{name}: routed experts {sum(m['experts']):.2f} GiB ({' / '.join(f'{v:.2f}' for v in per_card)} at "
              f"{a.split}), PLE {m['ple']:.2f} GiB ({', '.join(m['ple_types'])}), dense {sum(m['dense']):.2f} + head {m['head']:.2f} + other "
              f"{m['other']:.2f} GiB")
        for reserve in reserves:
            here, best = results[(name, reserve)]
            print(f"  reserve {reserve} MiB: at {a.split} the cards hold {here[0]:.3%} of the routing "
                  f"({480 * (1 - here[0]):.1f} of 480 expert products per token from RAM; {here[1]:.2f} GiB of experts "
                  f"in RAM); best cuts {','.join(map(str, best[1]))}: {best[0][0]:.3%} ({480 * (1 - best[0][0]):.1f}; "
                  f"{best[0][1]:.2f} GiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
