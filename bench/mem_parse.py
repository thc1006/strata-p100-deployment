#!/usr/bin/env python3
"""Pair the kprobe entry / return events of mem_hint.sh per thread (perf script -F tid,time,event,trace on stdin).

Per probed function: calls, total and mean time, and for arc_wait_for_eviction the part spent inside zpl_fadvise (the
hint's own ARC allocations).  Per sched_switch of a probed thread: how it left the CPU (asleep: prev_state S / D;
preempted: R) and which probed function it was inside at the time.  Unmatched returns (an entry before the recording
started, or a kretprobe miss) are counted, not guessed."""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict

LINE = re.compile(r"^\s*(\d+)\s+(\d+\.\d+):\s+(\S+?):?\s+(.*)$")
NAMES = {"madv": "do_madvise", "zplf": "zpl_fadvise", "awfe": "arc_wait_for_eviction", "genf": "generic_fadvise"}
ORDER = ("awfe", "zplf", "genf", "madv")          # innermost first, for "inside which function"


def main() -> int:
    stacks: dict[int, list] = defaultdict(list)
    tot = defaultdict(float); n = Counter(); unmatched = Counter()
    nested = defaultdict(float); nested_n = Counter()
    sw = Counter(); by_tid = Counter(); events = 0
    for line in sys.stdin:
        m = LINE.match(line)
        if not m:
            continue
        events += 1
        tid, t, ev, rest = int(m[1]), float(m[2]), m[3].rstrip(":"), m[4]
        if ev.startswith("strata:"):
            name = ev.split(":", 1)[1]
            fn, kind = name[:-2], name[-1]
            st = stacks[tid]
            if kind == "e":
                st.append((fn, t))
                if fn == "zplf" or fn == "genf":
                    by_tid[tid] += 1
            else:
                idx = next((i for i in range(len(st) - 1, -1, -1) if st[i][0] == fn), None)
                if idx is None:
                    unmatched[fn] += 1
                    continue
                f, t0 = st[idx]
                del st[idx:]                                  # anything above it lost its return
                d = t - t0
                tot[fn] += d; n[fn] += 1
                outer = {x[0] for x in st}
                if fn == "awfe" and "zplf" in outer:
                    nested["awfe_in_zplf"] += d; nested_n["awfe_in_zplf"] += 1
        elif ev == "sched:sched_switch":
            mm = re.search(r"prev_pid=(\d+) .*prev_state=(\S+)", rest)
            if not mm or int(mm[1]) != tid:
                continue
            inside = {x[0] for x in stacks[tid]}
            where = next((f for f in ORDER if f in inside), "none")
            how = "preempted" if mm[2].startswith("R") else "asleep"
            sw[(where, how)] += 1
    print(f"events {events:,}")
    print(f"{'function':<24} {'calls':>10} {'total s':>10} {'mean us':>9} {'unmatched returns':>18}")
    for fn in ("madv", "zplf", "genf", "awfe"):
        if n[fn] or unmatched[fn]:
            print(f"{NAMES[fn]:<24} {n[fn]:>10,} {tot[fn]:>10.3f} {(tot[fn] / n[fn] * 1e6 if n[fn] else 0):>9.2f} {unmatched[fn]:>18,}")
    if nested_n["awfe_in_zplf"] or n["zplf"]:
        share = nested["awfe_in_zplf"] / tot["zplf"] * 100 if tot["zplf"] else 0
        print(f"arc_wait_for_eviction inside zpl_fadvise: {nested_n['awfe_in_zplf']:,} calls, "
              f"{nested['awfe_in_zplf']:.3f} s = {share:.1f}% of the time in zpl_fadvise")
    print("switch-outs of the probed threads, by the innermost probed function they were in:")
    for where in ORDER + ("none",):
        a, p = sw[(where, "asleep")], sw[(where, "preempted")]
        if a or p:
            print(f"  {NAMES.get(where, 'outside the hint'):<24} asleep {a:>9,}   preempted {p:>9,}")
    print("threads issuing the hint (fadvise entries): " +
          ", ".join(f"{tid}: {c:,}" for tid, c in by_tid.most_common(6)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
