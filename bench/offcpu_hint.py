#!/usr/bin/env python3
"""Where the engine's threads wait while the PLE hint runs: off-CPU time split by cause (standard library only).

Reads `perf script` output of `perf record -a -g -e sched:sched_switch` (kernel call chains) taken while
ple_hint_probe.py ran.  For every switch-out of one of the engine's threads, the time until that thread is switched in
again is off-CPU time; the switch-out's kernel stack says why it left the CPU:
  arc_eviction   arc_wait_for_eviction on the stack (an ARC buffer allocation waiting for eviction: "memory")
  zfs_other      any other ZFS frame (locks, dbuf / dnode mutexes, zio waits)
  mmap_lock      rwsem / mmap_lock frames (madvise drops and retakes mmap_lock around vfs_fadvise)
  preempted      prev_state R (runnable: it lost the CPU to another task)
  other          everything else (futex waits of the engine's own threads, GPU syncs, ...)
Each is reported for switch-outs whose stack contains zpl_fadvise / generic_fadvise (inside the hint) and in total.

  sudo perf script -i sched.data -F comm,tid,time,event,trace,sym | python3 offcpu_hint.py --pid <engine pid>
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

HEAD = re.compile(r"^\s*(?P<comm>.+?)\s+(?P<tid>\d+)\s+(?P<time>\d+\.\d+):\s+(?P<event>\S+):\s+(?P<trace>.*)$")
SW = re.compile(r"prev_pid=(?P<prev>\d+) .*prev_state=(?P<state>\S+) ==> .*next_pid=(?P<next>\d+)")


def classify(state: str, stack: list[str]) -> str:
    if state.startswith("R"):
        return "preempted"
    s = " ".join(stack)
    if "arc_wait_for_eviction" in s:
        return "arc_eviction"
    if "rwsem" in s or "mmap_read_lock" in s or "mmap_lock" in s:
        return "mmap_lock"
    if re.search(r"\b(zfs|zpl|dmu|dbuf|arc|zio|spa|dnode|cv_wait|__cv_)", s):
        return "zfs_other"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid", type=int, required=True, help="the engine's process id (its threads are /proc/<pid>/task)")
    ap.add_argument("--tids", help="file with the engine's thread ids (default: read /proc/<pid>/task now)")
    a = ap.parse_args()
    tids = set(int(x) for x in (Path(a.tids).read_text().split() if a.tids else
                                [p.name for p in Path(f"/proc/{a.pid}/task").iterdir()]))
    out_at: dict[int, tuple[float, str, bool]] = {}      # tid -> (switch-out time, cause, inside the hint)
    tot = defaultdict(float); n = defaultdict(int)
    events = 0
    cur = None

    def finish(ev):
        nonlocal events
        if ev is None:
            return
        events += 1
        m = SW.search(ev["trace"])
        if not m:
            return
        t = float(ev["time"])
        prev, nxt = int(m["prev"]), int(m["next"])
        if nxt in out_at:
            t0, cause, hint = out_at.pop(nxt)
            for key in ((cause, "all"), (cause, "hint") if hint else None):
                if key:
                    tot[key] += t - t0; n[key] += 1
        if prev in tids:
            hint = any(f in fr for fr in ev["stack"] for f in ("zpl_fadvise", "generic_fadvise", "vfs_fadvise"))
            out_at[prev] = (t, classify(m["state"], ev["stack"]), hint)

    for line in sys.stdin:
        h = HEAD.match(line)
        if h and "sched:sched_switch" in h["event"]:
            finish(cur)
            cur = {"time": h["time"], "trace": h["trace"], "stack": []}
        elif cur is not None and line.strip():
            cur["stack"].append(line.strip().split()[-1] if line.strip() else "")
    finish(cur)
    print(f"sched_switch events {events:,}; engine threads {len(tids)}")
    print(f"{'cause':<14} {'inside the hint (s, n)':>26} {'all engine threads (s, n)':>30}")
    for c in ("arc_eviction", "zfs_other", "mmap_lock", "preempted", "other"):
        print(f"{c:<14} {tot[(c, 'hint')]:>16.3f} {n[(c, 'hint')]:>9,} {tot[(c, 'all')]:>20.3f} {n[(c, 'all')]:>9,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
