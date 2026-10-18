#!/usr/bin/env python3
"""The IQ3_XXS / IQ3_S A/B (docs/decisions/0004-iq3-s.md) from the raw result files: whether the runs follow the
protocol, whether each run was clean, and the speed criterion.  Every number is read from results/.

  python3 -B bench/ab_model_summary.py iq3xxs-1 iq3s-1 iq3s-2 iq3xxs-2 > results/AB-iq3s.md

The runs come from bench/ab_model_run.sh (<tag>-arm.txt before, <tag>-after.txt after) around bench/run_profile.sh.
Evaluated in this order:
  1. The protocol: no tag twice; an arm record for every run, whose model name is the model in its --native shard and
     its --pack; two models, the baseline's name ending in -iq3_xxs; RUNS_PER_ARM runs of each, in the order ORDER
     (A the baseline); a separate engine start for every run; the runs the same in everything but the model (the
     engine's other arguments and its STRATA_/CUDA_ environment, the engine, server.py and serve_load.py, the shared
     settings, the deployment commit, the ZFS ARC cap); no other arm record measured between the first and the last.
  2. Each run: an after record, not interrupted, the same engine at the end, no error line in the engine log, the
     service journal (systemd's lines included) or the kernel log, exactly EXPECTED_DONE requests completed by the
     server ("[strata] done:" lines; more means other traffic), none cancelled or ending other than stop / length,
     no client Traceback, each scenario's record exactly once, and no failed request (an HTTP status other than 200,
     an error field, a finish other than stop / length; serve_load by its error count).
  3. Speed: the two baseline runs agree within AA_LIMIT on every criterion metric; then on 1, 2 and 4 clients and the
     4.4K and 32K prompts the candidate may be at most 10% lower, and on the time to first token of a 16K prompt
     joining three decoders at most 15% higher, in its mean and in each of its runs (faster never fails).
Metrics: "1 / 2 / 4 clients" is serve_load's total tok/s, the median over its 3 rounds of the tokens a round
received divided by the round's wall time (time to first token included); the prompt rates are the engine's, the
median of the 3 replicate runs.  A value that is not a finite number counts as missing.
Exit status: 0 the protocol holds, every run is clean and the speed criterion holds; 1 only the speed criterion fails
(the decision record leaves that to the maintainer); 3 a candidate run is not clean (the stability criterion fails);
2 it cannot be evaluated (the protocol is not followed, a baseline run is not clean, the baseline runs disagree, or a
record or a number is missing).  Not evaluated here: the robustness run's floors and the quality subset.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"
RUNS_PER_ARM = 2
ORDER = "ABBA"                      # the decision record's order, A the baseline: a linear drift cancels out
BASELINE_SUFFIX = "-iq3_xxs"
EXPECTED_DONE = 98                  # the requests run_profile.sh sends (98 "[strata] done:" lines in its journal window)
AA_LIMIT = 0.05                     # the two baseline runs: their difference over their mean, on every criterion metric
# (key, label, higher is better, largest allowed change against the baseline)
CRITERIA = [("decode_1", "1 client total tok/s", True, 0.10), ("decode_2", "2 clients total tok/s", True, 0.10),
            ("decode_4", "4 clients total tok/s", True, 0.10), ("prompt_4k", "4.4K prompt tok/s", True, 0.10),
            ("prompt_32k", "32K prompt tok/s", True, 0.10),
            ("join16_ttft", "16K prompt joining 3 decoders: TTFT s", False, 0.15)]
MODEL_ARGS = {"--pack", "--native", "--ple-gguf", "--layer-split"}     # the engine arguments the model sets
REPLICATE = ("reasoning-v1", "code-v1", "docs-4.4k", "docs-32k")
STRESS = ("replicate", "think4", "worst")
LATENCY = ("coldjoin", "preempt", "poisson")
FOUR_USER = (("burst", 8192), ("burst", 16384), ("admit", 16384))
CLIENTS = (1, 2, 4)
GSQ_NATIVE = re.compile(r"/Qwen3\.8-Flash-Next-GSQ-RCO-(IQ3_XXS|IQ3_S)-00001-of-00002\.gguf$")


class Damaged(Exception):
    """A result file that is not what its program writes (a run stopped in the middle of a write)."""


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Damaged(f"{path.name} is not valid JSON ({e})") from None


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise Damaged(f"{path.name} line {n} is not valid JSON ({e})") from None
    return out


def finite(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def median(xs):
    xs = [x for x in (finite(x) for x in xs) if x is not None]
    return statistics.median(xs) if xs else None


def grep(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.M)
    return m.group(1).strip() if m else None


def arm_info(results: Path, tag: str) -> dict:
    """What bench/ab_model_run.sh recorded before the run; {} when there is no record."""
    p = results / f"{tag}-arm.txt"
    if not p.exists():
        return {}
    t = p.read_text()
    args = (grep(t, r"^engine args: (.*)$") or "").split()
    flags = {a: args[i + 1] for i, a in enumerate(args[:-1]) if a.startswith("--") and not args[i + 1].startswith("--")}
    native = GSQ_NATIVE.search(flags.get("--native", ""))
    rest, skip = [], False
    for a in args:                                   # the arguments without the ones the model sets
        if skip:
            skip = False
        elif a in MODEL_ARGS:
            skip = True
        else:
            rest.append(a)
    return {"at": grep(t, r"^tag \S+\s+arm \S+\s+at (.+)$"), "model": grep(t, r'"model": "([^"]+)"'),
            "loaded": native.group(1).lower() if native else None, "pack": Path(flags.get("--pack", "")).name or None,
            "other_args": " ".join(rest), "env": grep(t, r"^engine env: (.*)$"),
            "exe_sha": grep(t, r"^engine pid \S+ exe \S+ sha256 (\w+)"), "engine": grep(t, r"session is up \(engine ([0-9.]+)\)"),
            "server_sha": grep(t, r"^server\.py sha256 (\w+)"), "serve_load_sha": grep(t, r"serve_load\.py sha256 (\w+)"),
            "shared_sha": grep(t, r"^shared settings \S+ sha256 (\w+)"), "commit": grep(t, r"^deployment commit (.+)$"),
            "arc": grep(t, r"^zfs_arc_max (\d+)"), "ticks": grep(t, r"^engine start ticks (\d+)"),
            "started": grep(t, r"^engine started: (.+)$"), "config_sha": grep(t, r"^config \S+ sha256 (\w+)")}


def after_info(results: Path, tag: str) -> dict:
    """What bench/ab_model_run.sh recorded after the run; {} when there is no record.  Counts are ints, or None when
    the record says they are unknown."""
    p = results / f"{tag}-after.txt"
    if not p.exists():
        return {}
    t = p.read_text()
    num = lambda v: int(v) if v is not None and v.isdigit() else None   # noqa: E731
    # "(length,cancel=False 44, stop,cancel=False 54)": the names hold a comma themselves
    finishes = {name: int(n) for name, n in re.findall(r"([a-z_]+,cancel=\w+) (\d+)",
                                                       grep(t, r"^journal done lines during the run: \S+ \((.*)\)$") or "")}
    return {"interrupted": grep(t, r"\binterrupted (\w+)\s*$"), "same_engine": grep(t, r"^same engine as before: (\w+)"),
            "engine_errors": num(grep(t, r"^engine log error lines during the run: (\S+)")),
            "journal": grep(t, r"^journal: (\w+);"), "journal_errors": num(grep(t, r"^journal: \w+; error lines during the run: (\S+)")),
            "done": num(grep(t, r"^journal done lines during the run: (\S+)")), "finishes": finishes,
            "kernel_errors": num(grep(t, r"^kernel error lines during the run: (\S+)")),
            "tracebacks": num(grep(t, r"^perf log Traceback lines: (\S+)"))}


def failed(x) -> bool:
    """A request record that did not complete: a status other than 200, an error field (even empty), or a finish
    other than stop / length (a record without the field is not judged on it)."""
    if not isinstance(x, dict):
        return True
    return (("status" in x and x["status"] != 200) or ("error" in x and x["error"] is not None)
            or ("finish" in x and x["finish"] not in ("stop", "length")))


def run_records(results: Path, tag: str, stress: list, lat: list) -> dict:
    """The run's records (each expected once), its request records, failures and criterion metrics."""
    st = {s: [r.get("result") or {} for r in stress if r.get("tag") == tag and r.get("scenario") == s] for s in STRESS}
    la = {s: [r.get("result") or {} for r in lat if r.get("tag") == tag and r.get("scenario") == s
              and (s != "poisson" or (r.get("result") or {}).get("rate_req_s") == 0.1)] for s in LATENCY}
    fu_all = read_jsonl(results / f"{tag}-four_user.jsonl")
    fu = {k: [r for r in fu_all if (r.get("scenario"), r.get("target_tokens")) == k] for k in FOUR_USER}
    loads = read_json(results / f"{tag}-serve_load.json", [])
    sl = {c: [r for r in loads if r.get("clients") == c] for c in CLIENTS}
    counts = {**{f"stress {s}": len(v) for s, v in st.items()}, **{f"latency {s}": len(v) for s, v in la.items()},
              **{f"four_user {k[0]} {k[1]}": len(v) for k, v in fu.items()}, **{f"serve_load {c} clients": len(v) for c, v in sl.items()}}
    one = lambda v: v[0] if len(v) == 1 else {}                          # noqa: E731
    rep, th, wo = one(st["replicate"]), one(st["think4"]), one(st["worst"])
    cj, pe, pr = one(la["coldjoin"]), one(la["preempt"]), one(la["poisson"])
    runs = {k: (rep.get(k) or {}).get("runs") or [] for k in REPLICATE}
    th_reqs = [x for rd in th.get("rounds") or [] for x in rd]
    reqs = [x for k in REPLICATE for x in runs[k]] + th_reqs + list(wo.get("requests") or [])
    reqs += [pe[k] for k in ("short", "long") if pe.get(k)] + list(cj.get("decoders") or []) + list(cj.get("joiners") or [])
    reqs += list(pr.get("rows") or [])
    for k in FOUR_USER:
        for rec in fu[k]:
            reqs += list(rec.get("requests") or []) + list(rec.get("decoders_result") or [])
            reqs += [rec["newcomer"]] if rec.get("newcomer") else []
    n_fail = sum(failed(x) for x in reqs) + sum(finite(r.get("errors")) or 0 for r in loads)
    mon = read_json(results / f"{tag}-monitor-all.json", {})
    frees = [finite((mon.get(g) or {}).get("min_free_mib")) for g in ("gpu0", "gpu1", "gpu2", "gpu3")]
    b16, ad = one(fu[("burst", 16384)]), one(fu[("admit", 16384)])
    return {"counts": counts, "failed": n_fail, "checked": len(reqs),
            "decode_1": finite(one(sl[1]).get("total_tps")), "decode_2": finite(one(sl[2]).get("total_tps")),
            "decode_4": finite(one(sl[4]).get("total_tps")),
            "prompt_4k": median(x.get("engine_prompt_tok_s") for x in runs["docs-4.4k"]),
            "prompt_32k": median(x.get("engine_prompt_tok_s") for x in runs["docs-32k"]),
            "think4_user": median(x.get("client_decode_tok_s") for x in th_reqs),
            "burst16_last_ttft": finite(b16.get("ttft_last_s")), "join16_ttft": finite((ad.get("newcomer") or {}).get("ttft_s")),
            "oneliner_ttft": finite((pe.get("short") or {}).get("ttft_s")),
            "min_free_vram": min(frees) if None not in frees else None,
            "min_avail": finite((mon.get("host") or {}).get("min_mem_avail_gib"))}


def protocol(tags: list, info: dict, results: Path) -> list:
    """The ways the runs do not follow the protocol (empty when they do)."""
    bad = []
    if len(set(tags)) != len(tags):
        return [f"a tag is given more than once ({' '.join(tags)})"]
    missing = [t for t in tags if not info[t].get("model") or not info[t].get("at")]
    if missing:
        return [f"no arm record (or no model or measured-from time in it) for {', '.join(missing)}"]
    for t in tags:
        i = info[t]
        if not i["loaded"] or not i["model"].endswith("-" + i["loaded"]):
            bad.append(f"{t} answered as {i['model']} but loaded {i['loaded'] or 'an unknown file'}")
        elif i["pack"] != i["loaded"]:
            bad.append(f"{t} loaded the {i['loaded']} shard with the pack {i['pack']}")
    models = sorted({info[t]["model"] for t in tags})
    base = [m for m in models if m.endswith(BASELINE_SUFFIX)]
    if len(models) != 2 or len(base) != 1:
        return bad + [f"the runs are of {', '.join(models)}; it needs exactly two models, one ending in {BASELINE_SUFFIX}"]
    for m in models:
        n = sum(info[t]["model"] == m for t in tags)
        if n != RUNS_PER_ARM:
            bad.append(f"{m} has {n} runs, the protocol {RUNS_PER_ARM}")
    order = "".join("A" if info[t]["model"] == base[0] else "B" for t in tags)
    if order != ORDER:
        bad.append(f"the runs were measured in the order {order}, the protocol is {ORDER}")
    starts = [info[t]["ticks"] for t in tags]
    if None in starts or len(set(starts)) != len(starts):
        bad.append("the runs do not each have an engine start of their own (start ticks " + ", ".join(map(str, starts)) + ")")
    for key, label in (("other_args", "the engine's other arguments"), ("env", "the engine's environment"),
                       ("exe_sha", "the engine"), ("engine", "the engine version"), ("server_sha", "server.py"),
                       ("serve_load_sha", "serve_load.py"), ("shared_sha", "the shared settings"),
                       ("commit", "the deployment commit"), ("arc", "the ZFS ARC cap")):
        values = {info[t].get(key) for t in tags}
        if None in values or len(values) != 1:
            bad.append(f"the runs differ in {label}: " + "; ".join(f"{t} {info[t].get(key)}" for t in tags))
    first, last = info[tags[0]]["at"], info[tags[-1]]["at"]
    others = sorted(p.name[:-len("-arm.txt")] for p in results.glob("*-arm.txt") if p.name[:-len("-arm.txt")] not in tags
                    and first <= (grep(p.read_text(), r"^tag \S+\s+arm \S+\s+at (.+)$") or "") <= last)
    if others:
        bad.append(f"other runs were measured between the first and the last and are not included: {', '.join(others)}")
    return bad


def stability(tag: str, after: dict, rec: dict) -> tuple[list, list]:
    """(not clean, cannot tell): what makes the run not clean, and what makes it impossible to judge."""
    a, dirty, unknown = after, [], []
    if not a:
        return [], ["no after record"]
    if a.get("interrupted") != "no":
        unknown.append("the run was interrupted" if a.get("interrupted") == "yes" else "no interrupted line")
    if a.get("same_engine") != "yes":
        dirty.append("the engine restarted" if a.get("same_engine") == "NO" else "no same-engine line")
    for key, label in (("engine_errors", "engine log"), ("journal_errors", "journal"), ("kernel_errors", "kernel log")):
        if a.get(key) is None:
            unknown.append(f"the {label} error count is unknown")
        elif a[key]:
            dirty.append(f"{a[key]} {label} error lines")
    if a.get("journal") != "system":
        unknown.append("the system journal was not read")
    if a.get("done") is None:
        unknown.append("the completed-request count is unknown")
    elif a["done"] < EXPECTED_DONE:
        dirty.append(f"{a['done']} requests completed, {EXPECTED_DONE} sent")
    elif a["done"] > EXPECTED_DONE:
        unknown.append(f"{a['done']} requests completed, {EXPECTED_DONE} sent: other traffic during the run")
    odd = {k: v for k, v in a.get("finishes", {}).items() if k not in ("stop,cancel=False", "length,cancel=False")}
    if odd:
        dirty.append("completed requests that were cancelled or ended otherwise: " + ", ".join(f"{k} {v}" for k, v in odd.items()))
    if a.get("tracebacks") is None:
        unknown.append("the perf log is missing")
    elif a["tracebacks"]:
        unknown.append(f"{a['tracebacks']} client Tracebacks in the perf log")
    if rec["failed"]:
        dirty.append(f"{rec['failed']} failed requests")
    wrong = [f"{k} {n}" for k, n in rec["counts"].items() if n != 1]
    if wrong:
        (dirty if dirty else unknown).append("records not exactly once: " + ", ".join(wrong))
    return dirty, unknown


def fmt(v, nd=1) -> str:
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def evaluate(tags: list, results: Path) -> tuple[int, list]:
    info = {t: arm_info(results, t) for t in tags}
    try:
        stress, lat = read_jsonl(results / "stress.jsonl"), read_jsonl(results / "latency.jsonl")
        recs = {t: run_records(results, t, stress, lat) for t in tags}
    except Damaged as e:
        return 2, [f"Cannot be evaluated: {e}."]
    after = {t: after_info(results, t) for t in tags}
    chrono = sorted(tags, key=lambda t: (info[t].get("at") is None, info[t].get("at") or ""))
    o = ["# GSQ-RCO IQ3_XXS vs IQ3_S on this box (generated by bench/ab_model_summary.py)", "",
         "Runs in the order they were measured: " + " -> ".join(chrono) + ".  The perf set is bench/run_profile.sh.", ""]
    if chrono != tags:
        o += [f"Note: the arguments were in another order ({' '.join(tags)}); the tables keep the measured order.", ""]
    o += ["| run | model | 1 / 2 / 4 clients total tok/s | 4.4K prompt tok/s | 32K prompt tok/s | 4 users thinking: per-user "
          "tok/s | 4x16K burst: last TTFT s | 16K joins 3 decoders: TTFT s | one-liner during a 28K read: TTFT s | "
          "failed requests (records checked) | min free VRAM MiB | min MemAvailable GiB |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for t in chrono:
        x = recs[t]
        o.append(f"| {t} | {info[t].get('model') or '?'} | {fmt(x['decode_1'])} / {fmt(x['decode_2'])} / {fmt(x['decode_4'])} | "
                 f"{fmt(x['prompt_4k'])} | {fmt(x['prompt_32k'])} | {fmt(x['think4_user'])} | {fmt(x['burst16_last_ttft'])} | "
                 f"{fmt(x['join16_ttft'], 2)} | {fmt(x['oneliner_ttft'])} | {x['failed']} ({x['checked']}) | "
                 f"{fmt(x['min_free_vram'])} | {fmt(x['min_avail'])} |")
    o += ["", "## Where each run came from", "",
          "| run | measured from | model | loaded / pack | engine started (ticks) | engine | exe sha256 | config sha256 | ARC cap |",
          "|---|---|---|---|---|---|---|---|---:|"]
    for t in chrono:
        i = info[t]
        o.append(f"| {t} | {i.get('at') or '?'} | {i.get('model') or '?'} | {i.get('loaded') or '?'} / {i.get('pack') or '?'} | "
                 f"{i.get('started') or '?'} ({i.get('ticks') or '?'}) | {i.get('engine') or '?'} | {i.get('exe_sha') or '?'} | "
                 f"{i.get('config_sha') or '?'} | {i.get('arc') or '?'} |")
    bad = protocol(chrono, info, results)
    if bad:
        return 2, o + ["", "Cannot be evaluated: the runs do not follow the protocol: " + "; ".join(bad) + "."]
    base = next(info[t]["model"] for t in chrono if info[t]["model"].endswith(BASELINE_SUFFIX))
    cand = next(info[t]["model"] for t in chrono if info[t]["model"] != base)
    o += ["", "## Each run", "", "| run | model | clean | not clean | cannot tell |", "|---|---|---|---|---|"]
    status, unknown_any, cand_dirty = 0, [], []
    for t in chrono:
        dirty, unknown = stability(t, after[t], recs[t])
        o.append(f"| {t} | {info[t]['model']} | {'yes' if not dirty and not unknown else 'no'} | {'; '.join(dirty) or '-'} | "
                 f"{'; '.join(unknown) or '-'} |")
        if dirty and info[t]["model"] == cand:
            cand_dirty.append(f"{t}: {'; '.join(dirty)}")
        elif dirty:
            unknown_any.append(f"the baseline run {t} is not clean ({'; '.join(dirty)})")
        unknown_any += [f"{t}: {u}" for u in unknown]
    per = {m: [t for t in chrono if info[t]["model"] == m] for m in (base, cand)}
    o += ["", f"## Speed: {cand} against {base}", "",
          f"| metric | {base} runs | agree within {AA_LIMIT:.0%} | {cand} runs | change of the mean (of each run) | allowed | holds |",
          "|---|---:|---|---:|---:|---:|---|"]
    speed_ok, speed_unknown = True, []
    for key, label, higher, limit in CRITERIA:
        va, vb = [recs[t][key] for t in per[base]], [recs[t][key] for t in per[cand]]
        if None in va or None in vb or min(va) <= 0:
            speed_unknown.append(label)
            o.append(f"| {label} | {', '.join(fmt(v, 2) for v in va)} | - | {', '.join(fmt(v, 2) for v in vb)} | - | - | no data |")
            continue
        ma = statistics.fmean(va)
        aa = (max(va) - min(va)) / ma <= AA_LIMIT
        changes = [(statistics.fmean(vb) - ma) / ma] + [(v - ma) / ma for v in vb]
        holds = all((c >= -limit) if higher else (c <= limit) for c in changes)
        if not aa:
            speed_unknown.append(f"{label} (the baseline runs differ by {(max(va) - min(va)) / ma:.2%})")
        speed_ok &= holds
        o.append(f"| {label} | {', '.join(fmt(v, 2) for v in va)} | {'yes' if aa else 'NO'} | {', '.join(fmt(v, 2) for v in vb)} | "
                 f"{changes[0]:+.2%} ({', '.join(f'{c:+.2%}' for c in changes[1:])}) | {'-' if higher else '+'}{limit:.0%} | "
                 f"{'yes' if holds else 'NO'} |")
    o.append("")
    if cand_dirty:
        o.append("Stability criterion (perf runs): does NOT hold: " + "; ".join(cand_dirty) + ".")
        status = 3
    if unknown_any or speed_unknown:
        o.append("Cannot be evaluated: " + "; ".join(unknown_any + [f"no usable value for {u}" for u in speed_unknown]) + ".")
        status = status or 2
    if not cand_dirty and not unknown_any:
        o.append("Stability criterion (perf runs): holds: every run clean.")
    if not speed_unknown:
        o.append(f"Speed criterion: {'holds' if speed_ok else 'does NOT hold'} (the mean and each {cand} run).")
        if not speed_ok and status == 0:
            status = 1
    o.append("Not evaluated here (decision record 0004): the robustness run's floors and the quality subset.")
    return status, o


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tags", nargs="+")
    ap.add_argument("--results", type=Path, default=RESULTS, help="the raw results (default: the repository's results/)")
    a = ap.parse_args(argv)
    try:
        status, lines = evaluate(a.tags, a.results)
    except Exception as e:                           # a crash is "cannot be evaluated", never a verdict
        status, lines = 2, [f"Cannot be evaluated: {type(e).__name__}: {e}"]
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
