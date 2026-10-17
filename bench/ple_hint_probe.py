#!/usr/bin/env python3
"""The PLE hint A/B probe for 0.1.45 and the skip-when-locked patch (standard library only).

Like ple_prefetch_probe.py, but every prompt carries a fixed nonce (seed + slice offset), so the same request can be
sent to every arm and the answers compared byte for byte (the hint cannot change a value, so they must match), and
each request also records what the rest of the machine spent on it:
  - disk: the rpool partition's reads (/sys/block/sda/sda4/stat); ARC: prefetch / demand misses, memory throttles
  - pressure: /proc/pressure/{cpu,memory,io} "some"/"full" totals (microseconds of stall, all tasks)
  - CPU seconds of the whole machine by mode (/proc/stat: user, system, iowait, softirq, steal), of the engine
    (user / system), and of ZFS's kernel threads (arc_evict, z_rd_int_*, z_rd_iss_*).  z_rd_int / z_rd_iss are taskq
    threads that come and go: a thread that ends between the two snapshots takes its CPU time with it, so their
    figures count threads alive at both snapshots plus new ones (a lower bound; "zfs_threads_gone" says how many
    ended).  The machine-wide system time is the figure that cannot lose anything.
  - (added 2026-10-17 20:2x, after z-lk-1; fields only added, none changed) per engine thread from
    /proc/<pid>/task/<tid>/schedstat: on-CPU and run-queue time, so for the busiest thread "blocked" = wall - on-CPU -
    run-queue; its voluntary / involuntary switches; the ARC's size and target (c) at both ends and its memory counters
    (memory_direct/indirect_count, evict_not_enough, *_iohits); /proc/vmstat reclaim counters; kswapd0 CPU.
Modes: prompt = fresh ~16K-token prompts, 32 tokens out; decode = a short prompt (a ~1.5K-token slice to translate),
512 tokens out, to see the hint on the decode windows.

  python3 bench/ple_hint_probe.py --tag z-off-1 --mode prompt --json results/ple_hint_probe.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")
ARC_KEYS = ("prefetch_data_misses", "prefetch_data_hits", "demand_data_misses", "demand_data_hits",
            "memory_throttle_count", "evict_skip")


def engine_pid() -> int | None:
    out = subprocess.run(["pgrep", "-f", "[e]ngine-cuda12/strata --serve"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def cpu_s(pid: int | str) -> tuple[float, float]:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(f[11]) / TICK, int(f[12]) / TICK          # utime, stime (fields 14, 15)
    except (OSError, IndexError, ValueError):
        return 0.0, 0.0


ZFS_GROUPS = ("arc_evict", "z_rd_int", "z_rd_iss")


def zfs_threads() -> dict:
    """pid -> (group, CPU seconds) for ZFS's kernel threads of interest."""
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            comm = (d / "comm").read_text().strip()
        except OSError:
            continue
        for g in ZFS_GROUPS:
            if comm.startswith(g):
                u, s = cpu_s(d.name)
                out[int(d.name)] = (g, u + s)
    return out


STAT_MODES = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def machine_cpu() -> dict:
    f = Path("/proc/stat").read_text().splitlines()[0].split()
    assert f[0] == "cpu"
    return {m: int(v) / TICK for m, v in zip(STAT_MODES, f[1:9])}


ARC_MORE = ("memory_direct_count", "memory_indirect_count", "evict_not_enough", "prefetch_data_iohits",
            "demand_data_iohits")
ARC_GAUGES = ("size", "c", "arc_no_grow", "memory_available_bytes")
VMSTAT = ("pgscan_kswapd", "pgscan_direct", "pgsteal_kswapd", "pgsteal_direct", "allocstall_normal",
          "allocstall_movable", "workingset_refault_file")


def threads(pid: int | None) -> dict:
    """tid -> (comm, on-CPU s, run-queue s, voluntary, involuntary) for the engine's threads."""
    out = {}
    if not pid:
        return out
    for t in Path(f"/proc/{pid}/task").iterdir():
        try:
            on, rq, _ = (t / "schedstat").read_text().split()
            st = dict(l.split(":\t", 1) for l in (t / "status").read_text().splitlines() if ":\t" in l)
            out[int(t.name)] = ((t / "comm").read_text().strip(), int(on) / 1e9, int(rq) / 1e9,
                                int(st["voluntary_ctxt_switches"]), int(st["nonvoluntary_ctxt_switches"]))
        except (OSError, ValueError, KeyError):
            continue
    return out


def kv_file(path: str, keys) -> dict:
    out = {}
    for line in Path(path).read_text().splitlines():
        p = line.split()
        if p and p[0] in keys:
            out[p[0]] = int(p[-1])
    return out


def psi() -> dict:
    d = {}
    for res in ("cpu", "memory", "io"):
        for line in Path(f"/proc/pressure/{res}").read_text().splitlines():
            kind, *kv = line.split()
            d[f"{res}_{kind}_us"] = int(dict(x.split("=") for x in kv)["total"])
    return d


def snap(pid: int | None) -> dict:
    d = {"sda4_bytes": int(Path("/sys/block/sda/sda4/stat").read_text().split()[2]) * 512, "t": time.monotonic()}
    for line in Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines():
        p = line.split()
        if len(p) == 3 and p[0] in ARC_KEYS:
            d[p[0]] = int(p[2])
    d.update(psi())
    d["engine_user_s"], d["engine_sys_s"] = cpu_s(pid) if pid else (0.0, 0.0)
    d["zfs"] = zfs_threads()
    d["cpu"] = machine_cpu()
    d["thr"] = threads(pid)
    d["arc2"] = kv_file("/proc/spl/kstat/zfs/arcstats", ARC_MORE + ARC_GAUGES)
    d["vm"] = kv_file("/proc/vmstat", VMSTAT)
    k = subprocess.run(["pgrep", "-x", "kswapd0"], capture_output=True, text=True).stdout.split()
    d["kswapd_s"] = sum(cpu_s(k[0])) if k else 0.0
    return d


def delta(a: dict, b: dict) -> dict:
    out = {"disk_gb": round((b["sda4_bytes"] - a["sda4_bytes"]) / 1e9, 3)}
    for k in ARC_KEYS:
        out[k] = b[k] - a[k]
    for k in a:
        if k.endswith("_us"):
            out[k.replace("_us", "_s")] = round((b[k] - a[k]) / 1e6, 3)
        elif k in ("engine_user_s", "engine_sys_s"):
            out[k] = round(b[k] - a[k], 2)
    for m in STAT_MODES:
        out[f"machine_{m}_s"] = round(b["cpu"][m] - a["cpu"][m], 2)
    za, zb = a["zfs"], b["zfs"]
    for g in ZFS_GROUPS:
        out[f"{g}_s"] = round(sum(c - (za[p][1] if p in za else 0.0) for p, (gg, c) in zb.items() if gg == g), 2)
    out["zfs_threads_gone"] = sum(1 for p in za if p not in zb)
    wall = b["t"] - a["t"]
    ta, tb = a["thr"], b["thr"]
    per = {t: (tb[t][0], tb[t][1] - ta[t][1], tb[t][2] - ta[t][2], tb[t][3] - ta[t][3], tb[t][4] - ta[t][4])
           for t in tb if t in ta}
    if per:
        top = max(per, key=lambda t: per[t][1])
        comm, on, rq, vol, inv = per[top]
        out.update({"top_thread": f"{comm}/{top}", "top_on_cpu_s": round(on, 2), "top_runq_s": round(rq, 2),
                    "top_blocked_s": round(wall - on - rq, 2), "top_vol_switches": vol, "top_invol_switches": inv,
                    "engine_on_cpu_s": round(sum(v[1] for v in per.values()), 2),
                    "engine_runq_s": round(sum(v[2] for v in per.values()), 2)})
    # threads that started during the request count whole; those that ended took their time with them (the process's
    # user + sys is the complete figure)
    out["engine_new_threads"] = sum(1 for t in tb if t not in ta)
    out["engine_new_threads_on_cpu_s"] = round(sum(tb[t][1] for t in tb if t not in ta), 2)
    out["engine_threads_gone"] = sum(1 for t in ta if t not in tb)
    for k in ARC_MORE:
        out[k] = b["arc2"].get(k, 0) - a["arc2"].get(k, 0)
    for k in ARC_GAUGES:
        out[f"arc_{k}_start"], out[f"arc_{k}_end"] = a["arc2"].get(k), b["arc2"].get(k)
    for k in VMSTAT:
        out[k] = b["vm"].get(k, 0) - a["vm"].get(k, 0)
    out["kswapd_s"] = round(b["kswapd_s"] - a["kswapd_s"], 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--mode", choices=("prompt", "decode"), default="prompt")
    ap.add_argument("--json")
    ap.add_argument("--seed", default="pr2281")
    ap.add_argument("--offsets", type=int, nargs="+")
    a = ap.parse_args()
    text = SM.docs_corpus()
    offs = a.offsets or ([777777, 333333, 222222] if a.mode == "prompt" else [123456, 654321, 246810])
    pid = engine_pid()
    rows = []
    for off in offs:
        if a.mode == "prompt":
            body, max_tokens = SM.sized(text, 16000, off % len(text)), 32
        else:
            body = ("Translate the following text into Traditional Chinese. Output only the translation.\n\n"
                    + SM.sized(text, 1500, off % len(text)))
            max_tokens = 512
        s0 = snap(pid)
        r = SM.chat(f"[nonce {a.seed}-{off}]\n" + body, max_tokens=max_tokens, nonce=False)
        s1 = snap(pid)
        b = SM.brief(r)
        rows.append({"offset": off, "mode": a.mode, **{k: b.get(k) for k in (
            "prompt_tokens", "completion_tokens", "finish", "engine_prompt_tok_s", "ttft_s", "engine_decode_tok_s",
            "client_decode_tok_s", "drafts", "error")},
            "wall_s": round(s1["t"] - s0["t"], 2), **delta(s0, s1),
            "text_sha": hashlib.sha256(r.get("text", "").encode()).hexdigest()[:16], "text_head": r.get("text", "")[:48]})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    if a.json:
        with open(a.json, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tag": a.tag, "mode": a.mode, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "seed": a.seed,
                                "engine_pid": pid, "rows": rows}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
