#!/usr/bin/env python3
"""Resource sampler for the stress runs (standard library only).

  monitor.py record OUT.csv [--every 2]        sample until killed (SIGTERM / Ctrl+C)
  monitor.py summary OUT.csv [--since T --until T]   peaks over the whole file or a window (unix seconds)

Per sample: each GPU's memory used, utilisation, SM clock, power, temperature and active throttle reasons; the host's
MemAvailable, swap used, mlocked memory, the ZFS ARC size and the engine's RSS.  The summary is what the acceptance
rules read: the least VRAM left on any card, the least MemAvailable, swap growth, throttling.
"""
from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

GPU_FIELDS = "index,memory.used,memory.total,utilization.gpu,clocks.sm,power.draw,temperature.gpu,clocks_throttle_reasons.active"


def meminfo() -> dict:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        out[k] = int(v.split()[0]) * 1024
    return out


def arc_size() -> int | None:
    try:
        for line in Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines():
            parts = line.split()
            if parts and parts[0] == "size":
                return int(parts[2])
    except OSError:
        pass
    return None


def disk_read_bytes(part: str = "sda/sda4") -> int | None:
    """Physical reads of rpool's partition (sda4; sda2 is the swap partition, sda3 bpool).  On ZFS a process's
    read_bytes (/proc/<pid>/io, the engine's "the OS read") also counts ARC hits, so it is not storage I/O."""
    try:
        return int(Path(f"/sys/block/{part}/stat").read_text().split()[2]) * 512
    except (OSError, IndexError, ValueError):
        return None


def arc_demand_misses() -> int | None:
    try:
        for line in Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines():
            parts = line.split()
            if parts and parts[0] == "demand_data_misses":
                return int(parts[2])
    except OSError:
        pass
    return None


def engine_rss() -> int | None:
    r = subprocess.run(["pgrep", "-f", "[e]ngine-cuda12/strata --serve"], capture_output=True, text=True)
    pids = r.stdout.split()
    if not pids:
        return None
    try:
        for line in Path(f"/proc/{pids[0]}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


_cpu_prev: dict = {}


def cpu_busy() -> dict:
    """Per-CPU busy % since the previous sample (the engine's host thread is pinned to CPU 0)."""
    out = {}
    for line in Path("/proc/stat").read_text().splitlines():
        f = line.split()
        if f and f[0].startswith("cpu") and f[0][3:].isdigit():
            v = list(map(int, f[1:]))
            tot, idle = sum(v), v[3] + v[4]
            if f[0] in _cpu_prev:
                pt, pi = _cpu_prev[f[0]]
                out[f[0] + "_busy"] = round(100 * (1 - (idle - pi) / max(1, tot - pt)), 1)
            _cpu_prev[f[0]] = (tot, idle)
    return out


def sample() -> dict:
    row = {"t": round(time.time(), 1)}
    row.update(cpu_busy() or {f"cpu{i}_busy": "" for i in range(os.cpu_count() or 8)})
    r = subprocess.run(["nvidia-smi", f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=20)
    for line in r.stdout.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        i = f[0]
        row.update({f"g{i}_mem_mib": f[1], f"g{i}_util": f[3], f"g{i}_sm_mhz": f[4], f"g{i}_watt": f[5],
                    f"g{i}_temp": f[6], f"g{i}_throttle": f[7]})
        row["vram_total_mib"] = f[2]
    m = meminfo()
    row.update({"mem_avail_gib": round(m["MemAvailable"] / 2**30, 3),
                "swap_used_gib": round((m["SwapTotal"] - m["SwapFree"]) / 2**30, 3),
                "mlocked_gib": round(m.get("Mlocked", 0) / 2**30, 3)})
    a, e, d, m = arc_size(), engine_rss(), disk_read_bytes(), arc_demand_misses()
    row["arc_gib"] = round(a / 2**30, 3) if a is not None else ""
    row["disk_read_gb_total"] = round(d / 1e9, 3) if d is not None else ""
    sw = disk_read_bytes("sda/sda2")
    row["swap_in_gb_total"] = round(sw / 1e9, 3) if sw is not None else ""
    row["arc_demand_misses_total"] = m if m is not None else ""
    row["engine_rss_gib"] = round(e / 2**30, 3) if e is not None else ""
    return row


def record(path: str, every: float) -> int:
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    writer = None
    with open(path, "a", newline="") as f:
        while not stop:
            try:
                row = sample()
            except Exception as e:                     # one failed sample must not end the recording
                print(f"monitor: sample failed: {e}", file=sys.stderr)
                time.sleep(every)
                continue
            try:
                if writer is None:
                    if not any(k.endswith("_mem_mib") for k in row):   # nvidia-smi failed: wait for a full sample
                        raise RuntimeError("no GPU columns in this sample")
                    writer = csv.DictWriter(f, fieldnames=list(row), extrasaction="ignore", restval="")
                    if f.tell() == 0:
                        writer.writeheader()
                writer.writerow(row)
                f.flush()
            except Exception as e:                     # never die silently mid-run: say so and keep sampling
                print(f"monitor: write failed: {e}", file=sys.stderr)
            time.sleep(every)
    return 0


def summary(path: str, since: float | None, until: float | None) -> dict:
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f)
                if (since is None or float(r["t"]) >= since) and (until is None or float(r["t"]) <= until)]
    if not rows:
        return {"samples": 0}
    gpus = sorted({k.split("_")[0][1:] for k in rows[0] if k.startswith("g") and k.endswith("_mem_mib")})
    total = float(rows[0]["vram_total_mib"])
    out: dict = {"samples": len(rows), "seconds": round(float(rows[-1]["t"]) - float(rows[0]["t"]), 1)}
    for g in gpus:
        mem = [float(r[f"g{g}_mem_mib"]) for r in rows]
        util = [float(r[f"g{g}_util"]) for r in rows]
        clk = [float(r[f"g{g}_sm_mhz"]) for r in rows]
        thr = {r[f"g{g}_throttle"] for r in rows} - {"0x0000000000000000"}
        busy = [c for c, u in zip(clk, util) if u >= 50]
        out[f"gpu{g}"] = {"peak_mem_mib": max(mem), "min_free_mib": round(total - max(mem)),
                          "mean_util_pct": round(sum(util) / len(util), 1),
                          "sm_mhz_when_busy_min": min(busy) if busy else None,
                          "max_temp_c": max(float(r[f"g{g}_temp"]) for r in rows),
                          "max_watt": max(float(r[f"g{g}_watt"]) for r in rows),
                          "throttle_reasons_seen": sorted(thr)}
    avail = [float(r["mem_avail_gib"]) for r in rows]
    swap = [float(r["swap_used_gib"]) for r in rows]
    rss = [float(r["engine_rss_gib"]) for r in rows if r["engine_rss_gib"]]
    arc = [float(r["arc_gib"]) for r in rows if r["arc_gib"]]
    disk = [float(r["disk_read_gb_total"]) for r in rows if r.get("disk_read_gb_total")]
    miss = [int(r["arc_demand_misses_total"]) for r in rows if r.get("arc_demand_misses_total")]
    swin = [float(r["swap_in_gb_total"]) for r in rows if r.get("swap_in_gb_total")]
    out["storage"] = {"disk_read_gb": round(disk[-1] - disk[0], 3) if disk else None,
                      "swap_in_gb": round(swin[-1] - swin[0], 3) if swin else None,
                      "arc_demand_misses": miss[-1] - miss[0] if miss else None}
    cpus = sorted({k for k in rows[0] if k.startswith("cpu") and k.endswith("_busy")}, key=lambda k: int(k[3:-5]))
    out["cpu_mean_busy_pct"] = {k[:-5]: round(sum(float(r[k]) for r in rows if r.get(k)) /
                                               max(1, sum(1 for r in rows if r.get(k))), 1) for k in cpus}
    out["host"] = {"min_mem_avail_gib": min(avail), "swap_used_start_gib": swap[0], "swap_used_max_gib": max(swap),
                   "engine_rss_start_gib": rss[0] if rss else None, "engine_rss_max_gib": max(rss) if rss else None,
                   "arc_max_gib": max(arc) if arc else None}
    return out


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "record":
        every = float(argv[argv.index("--every") + 1]) if "--every" in argv else 2.0
        return record(argv[1], every)
    if len(argv) >= 2 and argv[0] == "summary":
        since = float(argv[argv.index("--since") + 1]) if "--since" in argv else None
        until = float(argv[argv.index("--until") + 1]) if "--until" in argv else None
        print(json.dumps(summary(argv[1], since, until), indent=1))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
