#!/usr/bin/env python3
"""An engine experiment's screen (bench/ab_engine.sh, bench/engine_screen.py) from the raw result files: whether each
run measured what it says, the numbers per arm, and the answers request by request.  Standard library only.

  python3 -B bench/ab_engine_summary.py --arms bench/arms-group-sort.json --baseline C0 c0-1 c1-1 c0-2 \
      > results/SCREEN-group-sort.md

Per run (<tag>-arm.txt, <tag>-after.txt, the tag's lines in engine-screen.jsonl): the arm check passed, the same
engine answered at the end, the service counted, once it served nothing, exactly the requests the warm-up and the
screen sent (more: another client's requests were measured), the screen exited 0, no record failed, no error line in
the engine's log after the warm-up, in the test unit's journal or in the kernel log, and both logs were read.  A run
that fails any of these is listed and left out of the numbers.  With --arms, a run must also show its arm's switch at
work: every "engaged" pattern matches one of its engine lines (<tag>-engine-lines.txt) and no "never" pattern does (a
switch that acts only on some requests can only be seen there).  All runs must have cut their prompts from the same
corpus.  An admission round counts only if it ran as designed (every decoder decoding before and while the joiner
was read); the report gives each run's invalid rounds.
Numbers per arm: each run's median of a metric's records, then the arms' means of those; the change of each other arm
against the --baseline arm, of its mean and of each run; the baseline's own runs' spread (the first and the last run
of the baseline bracket the others: their difference is the drift and the noise a change has to clear), and a change
no larger than that spread is marked "within the baseline's spread".
Answers: every request's answer sha256 across the runs: within an arm (does the engine repeat itself; "n/a" for an
arm with one run), and against the baseline, counted only on requests where all the baseline's runs agree (a request
the baseline itself answers in more than one way is counted apart).
Exit status: 0 the runs are usable, 2 a run is not (the report says which).  The screen decides nothing on its own:
a candidate goes on to the ABBA confirmation, whose criteria are fixed before it runs.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"
# (key, label, higher is better, how a record yields it)
METRICS = [
    ("prefill1200", "1.2K prompt: engine read tok/s", True, lambda r: r.get("engine_prompt_tok_s") if r["phase"] == "prefill" and r["size"] == 1200 else None),
    ("prefill4000", "4K prompt: engine read tok/s", True, lambda r: r.get("engine_prompt_tok_s") if r["phase"] == "prefill" and r["size"] == 4000 else None),
    ("prefill16000", "16K prompt: engine read tok/s", True, lambda r: r.get("engine_prompt_tok_s") if r["phase"] == "prefill" and r["size"] == 16000 else None),
    ("ttft16000", "16K prompt alone: TTFT s", False, lambda r: r.get("ttft_s") if r["phase"] == "prefill" and r["size"] == 16000 else None),
    ("decode1", "1 user: engine decode tok/s", True, lambda r: r.get("engine_decode_tok_s") if r["phase"] == "decode" else None),
    ("four", "4 users: total tok/s", True, lambda r: r.get("total_tok_s") if r["phase"] == "four" else None),
    ("join_ttft", "16K joining 3 decoders: TTFT s", False, lambda r: r.get("joiner_ttft_s") if admit_ok(r) else None),
    ("join_gap", "... the decoders' longest gap s", False, lambda r: r.get("longest_gap_s") if admit_ok(r) else None),
    ("join_makespan", "... the four requests' makespan s", False, lambda r: r.get("makespan_s") if admit_ok(r) else None),
    ("decode4", "4 users decoding, short prompts: summed engine decode tok/s", True,
     lambda r: r.get("sum_engine_decode_tok_s") if r["phase"] == "decode4" else None),
    ("eval4", "4 users, thinking, sampled: summed engine decode tok/s", True,
     lambda r: r.get("sum_engine_decode_tok_s") if r["phase"] == "eval4" else None),
    ("join28_ttft", "28K joining 3 decoders: TTFT s", False, lambda r: r.get("joiner_ttft_s") if admit_ok(r, "admit28") else None),
    ("join28_gap", "... the decoders' longest gap s", False, lambda r: r.get("longest_gap_s") if admit_ok(r, "admit28") else None),
    ("join28_makespan", "... the four requests' makespan s", False, lambda r: r.get("makespan_s") if admit_ok(r, "admit28") else None),
]


def admit_ok(r: dict, phase: str = "admit") -> bool:
    """An admission round that ran as designed."""
    return r["phase"] == phase and r.get("valid") is True


def grep(text: str, pattern: str):
    m = re.search(pattern, text, re.M)
    return m.group(1).strip() if m else None


def run_info(results: Path, tag: str) -> dict:
    arm = results / f"{tag}-arm.txt"
    after = results / f"{tag}-after.txt"
    a = arm.read_text() if arm.exists() else ""
    b = after.read_text() if after.exists() else ""
    journal = b.split("--- the test unit's journal lines", 1)[-1].split("--- kernel lines", 1)[0] if b else ""
    kernel = b.split("--- kernel lines", 1)[-1] if "--- kernel lines" in b else ""
    j_lines = [l for l in journal.splitlines() if l.strip()]
    k_lines = [l for l in kernel.splitlines() if l.strip()]
    return {"arm": grep(a, r"^tag \S+\s+arm (\S+)"), "at": grep(a, r"^tag \S+\s+arm \S+\s+at (\S+ \S+)"),
            "check": grep(a, r"^arm check: (.*)$"), "exe": grep(a, r"^engine pid \S+ exe \S+ sha256 (\w+)"),
            "screen_exit": grep(b, r"engine_screen exit (\d+)"), "same": grep(b, r"^same engine as before: (\w+)"),
            "counted": grep(b, r"^requests counted by /metrics: (\S+)"), "sent": grep(b, r"\bsent: (\d+)"),
            "idle_end": grep(b, r"^idle before the count: (\w+)"), "engine_errors": grep(b, r"^engine log error lines: (\S+)"),
            "journal_unreadable": j_lines == ["unreadable"], "kernel_unreadable": k_lines == ["unreadable"],
            "journal_lines": [] if j_lines == ["unreadable"] else j_lines,
            "kernel_lines": [] if k_lines == ["unreadable"] else k_lines, "has_arm": bool(a), "has_after": bool(b)}


def engine_lines(results: Path, tag: str) -> list | None:
    f = results / f"{tag}-engine-lines.txt"
    return f.read_text(errors="replace").splitlines() if f.exists() else None


def problems(i: dict, recs: list, spec: dict | None = None, lines: list | None = None) -> list:
    out = []
    if not i["has_arm"] or not i["has_after"]:
        return ["no arm or after record"]
    if i["check"] != "ok":
        out.append(f"the arm check: {i['check']}")
    if i["same"] != "yes":
        out.append("the engine restarted")
    if i["screen_exit"] != "0":
        out.append(f"engine_screen exited {i['screen_exit']}")
    if i["idle_end"] != "yes":
        out.append("the unit was still serving when the requests were counted")
    if i["counted"] != i["sent"]:
        out.append(f"/metrics counted {i['counted']} requests, the run sent {i['sent']}")
    if i["engine_errors"] != "0":
        out.append(f"{i['engine_errors']} engine error lines after the warm-up")
    if i["journal_unreadable"]:
        out.append("journal unreadable")
    if i["kernel_unreadable"]:
        out.append("kernel log unreadable")
    if i["journal_lines"]:
        out.append(f"{len(i['journal_lines'])} journal lines")
    if i["kernel_lines"]:
        out.append(f"{len(i['kernel_lines'])} kernel lines")
    bad = [r for r in recs if r.get("error")]
    if bad:
        out.append(f"{len(bad)} failed records")
    if not recs:
        out.append("no screen records")
    if spec is not None:
        if lines is None:
            out.append("no engine lines")
        else:
            for rx in spec.get("engaged") or []:
                if not any(re.search(rx, l) for l in lines):
                    out.append(f"its switch never showed in the engine lines ({rx})")
            for rx in spec.get("never") or []:
                if any(re.search(rx, l) for l in lines):
                    out.append(f"an engine line matches {rx}")
    return out


def label(r: dict) -> list:
    """(request label, answer sha256) pairs of one record, for the comparison of answers."""
    p = r["phase"]
    if p == "prefill":
        return [(f"prefill {r['size']} #{r['i']}", r.get("text_sha"))]
    if p == "decode":
        return [(f"decode #{r['i']}", r.get("text_sha"))]
    if p in ("four", "decode4", "eval4"):
        return [(f"{p} r{r['round']} #{j}", x.get("text_sha")) for j, x in enumerate(r.get("requests") or [])]
    if p in ("admit", "admit28"):
        return ([(f"{p} r{r['round']} joiner", (r.get("joiner") or {}).get("text_sha"))]
                + [(f"{p} r{r['round']} decoder {j}", x.get("text_sha")) for j, x in enumerate(r.get("decoders") or [])])
    if p == "turns":
        return [(f"turns #{r['i']} first", r.get("first_sha")), (f"turns #{r['i']} second", r.get("second_sha"))]
    return []


def evaluate(tags: list, baseline: str, results: Path, arms: dict | None = None) -> tuple[int, list]:
    recs_all = []
    path = results / "engine-screen.jsonl"
    if path.exists():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip():
                try:
                    recs_all.append(json.loads(line))
                except json.JSONDecodeError as e:
                    return 2, [f"Cannot be evaluated: engine-screen.jsonl line {n} is not valid JSON ({e})."]
    info = {t: run_info(results, t) for t in tags}
    recs = {t: [r for r in recs_all if r.get("tag") == t] for t in tags}
    o = ["# Engine screen (generated by bench/ab_engine_summary.py)", "",
         "| run | arm | measured from | arm check | same engine | requests counted / sent | usable |",
         "|---|---|---|---|---|---|---|"]
    status, usable = 0, []
    for t in tags:
        i = info[t]
        spec = (arms or {}).get(i["arm"]) if arms is not None else None
        if arms is not None and spec is None:
            spec = {"engaged": ["(?!)"]}                     # an arm the arms file does not have: not usable
        bad = problems(i, recs[t], spec, engine_lines(results, t) if arms is not None else None)
        o.append(f"| {t} | {i['arm'] or '?'} | {i['at'] or '?'} | {i['check'] or '?'} | {i['same'] or '?'} | "
                 f"{i['counted'] or '?'} / {i['sent'] or '?'} | {'yes' if not bad else 'NO: ' + '; '.join(bad)} |")
        if bad:
            status = 2
        else:
            usable.append(t)
    arms = sorted({info[t]["arm"] for t in usable if info[t]["arm"]}, key=lambda a: (a != baseline, a))
    if baseline not in arms:
        return 2, o + ["", f"Cannot be evaluated: no usable run of the baseline arm {baseline}."]
    exes = {info[t]["exe"] for t in usable}
    corpora = {r.get("corpus_sha") for t in usable for r in recs[t]}
    if len(corpora) != 1 or None in corpora:
        return 2, o + ["", f"Cannot be evaluated: the usable runs cut their prompts from different corpora ({sorted(map(str, corpora))})."]
    screens = {(r.get("phases"), r.get("screen")) for t in usable for r in recs[t]}
    if len(screens) > 1:
        return 2, o + ["", f"Cannot be evaluated: the usable runs ran different screens ({sorted(map(str, screens))})."]
    invalid = {t: sum(1 for r in recs[t] if r["phase"] in ("admit", "admit28") and r.get("valid") is not True)
               for t in usable}
    cached = {t: [r.get("second_cached") for r in recs[t] if r["phase"] == "turns"] for t in usable}
    o += ["", f"Engines: {', '.join(sorted(x or '?' for x in exes))}.  Corpus {corpora.pop()}.",
          "Admission rounds not as designed (left out of the admission numbers): "
          + ", ".join(f"{t} {n}" for t, n in invalid.items()) + ".",
          "Second turns' cached prompt tokens: " + "; ".join(f"{t} {c}" for t, c in cached.items()) + ".", "",
          "## Numbers", "", "Each cell: the arm's mean of its runs' medians (each run's median in brackets); the change "
          "against the baseline's mean, with each run's change in brackets.", "",
          "| metric | " + " | ".join(arms) + " |", "|---|" + "---:|" * len(arms)]
    for key, lab, higher, f in METRICS:
        per = {}
        for a in arms:
            meds = []
            for t in usable:
                if info[t]["arm"] != a:
                    continue
                xs = [v for v in (f(r) for r in recs[t]) if isinstance(v, (int, float))]
                meds.append(statistics.median(xs) if xs else None)
            per[a] = meds
        base = [m for m in per[baseline] if m is not None]
        bmean = statistics.fmean(base) if base else None
        bspread = (max(base) - min(base)) / bmean if len(base) > 1 and bmean else None
        cells = []
        for a in arms:
            ms = [m for m in per[a] if m is not None]
            if not ms:
                cells.append("-")
                continue
            mean = statistics.fmean(ms)
            each = ", ".join(f"{m:.2f}" for m in ms)
            if a == baseline or not bmean:
                spread = f"; spread {(max(ms) - min(ms)) / mean:+.1%}" if a == baseline and len(ms) > 1 and mean else ""
                cells.append(f"{mean:.2f} ({each}){spread}")
            else:
                ch = ", ".join(f"{(m - bmean) / bmean:+.1%}" for m in ms)
                within = (" within the baseline's spread" if bspread is not None and abs(mean - bmean) / bmean <= bspread
                          else "")
                cells.append(f"{mean:.2f} ({each}): {(mean - bmean) / bmean:+.1%} ({ch}){within}")
        o.append(f"| {lab} ({'higher' if higher else 'lower'} is better) | " + " | ".join(cells) + " |")
    # answers, request by request
    o += ["", "## Answers", ""]
    by_label: dict = {}
    for t in usable:
        for r in recs[t]:
            for lb, h in label(r):
                by_label.setdefault(lb, {})[t] = h
    base_runs = [t for t in usable if info[t]["arm"] == baseline]
    # requests every baseline run answered, and answered the same way
    agreed = {lb: hs[base_runs[0]] for lb, hs in by_label.items()
              if base_runs and all(t in hs for t in base_runs) and len({hs[t] for t in base_runs}) == 1}
    for a in arms:
        runs = [t for t in usable if info[t]["arm"] == a]
        if len(runs) > 1:
            within = sum(1 for lb, hs in by_label.items() if len({hs.get(t) for t in runs if t in hs}) > 1)
            line = f"- {a}: {len(by_label)} requests; differ between its own {len(runs)} runs: {within}"
        else:
            line = f"- {a}: {len(by_label)} requests; differ between its own runs: n/a (one run)"
        if a != baseline and base_runs:
            vs = sum(1 for lb, ref in agreed.items() if any(t in by_label[lb] and by_label[lb][t] != ref for t in runs))
            line += (f"; differ from the baseline on {vs} of the {len(agreed)} requests all its runs answer alike"
                     f" ({len(by_label) - len(agreed)} it answers in more than one way)")
        o.append(line)
    if status:
        o += ["", "Not every run is usable: the numbers above use only the usable ones."]
    return status, o


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tags", nargs="+")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--arms", type=Path, help="the arms file: each run must show its arm's engaged patterns, not its never ones")
    a = ap.parse_args(argv)
    try:
        arms = json.loads(a.arms.read_text())["arms"] if a.arms else None
        status, lines = evaluate(a.tags, a.baseline, a.results, arms)
    except Exception as e:                       # a crash is "cannot be evaluated", never a verdict
        status, lines = 2, [f"Cannot be evaluated: {type(e).__name__}: {e}"]
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
