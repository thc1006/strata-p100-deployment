#!/usr/bin/env python3
"""The IQ3_XXS / IQ3_S A/B (docs/decisions/0004-iq3-s.md) from the raw result files: whether the runs follow the
protocol, whether each run was clean, and the speed criterion.  Every number is read from results/.

  python3 -B bench/ab_model_summary.py ab-iq3xxs-1 ab-iq3s-1 ab-iq3s-2 ab-iq3xxs-2 > results/AB-iq3s.md

The runs come from bench/ab_model_run.sh (<tag>-arm.txt before, <tag>-after.txt after, and the captured lines of the
engine log, the service journal and the kernel log) around bench/run_profile.sh.  Evaluated in this order:
  1. The protocol: no tag twice; an arm record for every run, whose model name is the model in its --native shard, its
     --ple-gguf shard, its --pack and its profile.conf; two models, the baseline's name ending in -iq3_xxs;
     RUNS_PER_ARM runs of each, in the order ORDER (A the baseline); a separate engine start for every run, measured
     SETTLE_S or more after the service became ready; what step 1 deploys, read from this repository (the ZFS ARC
     cap, both RAM floors, each model's layer cuts), and the host units as configured; the runs the same in everything
     but the model (the engine's other arguments and its STRATA_/CUDA_ environment, the engine, server.py and
     serve_load.py, the config without the model's fields, the shared settings, the service's code, the perf set,
     serve_load's fixed prompts); no other arm record measured between the first and the last.
  2. Each run.  Not clean (a fault): the engine restarted; a line of the engine log; a service journal line that tells
     of a fault (systemd's own lines included); an NVIDIA Xid or an OOM kill of the service or of the whole host;
     fewer requests completed than the perf set sent; a completed request cancelled or ending other than stop /
     length; a failed request in the perf set's own records (an HTTP status other than 200, an error field, a finish
     other than stop / length; serve_load by its error count); an uncorrected ECC error.
     Cannot tell (the run must be measured again): the perf set did not run to its end (interrupted, a non-zero exit,
     a client Traceback, a scenario record missing or twice); other traffic (more requests completed than sent, a
     request refused with a 4xx, requests in flight at the end); a kernel problem that names no process of the
     service; a record that is missing or unreadable.  Lost requests and odd finishes are a fault only when the perf
     set ran to its end and there was no other traffic.
  3. Speed: the two baseline runs agree within AA_LIMIT on every criterion metric; then on 1, 2 and 4 clients and the
     4.4K and 32K prompts the candidate may be at most 10% lower, and on the time to first token of a 16K prompt
     joining three decoders at most 15% higher, in its mean and in each of its runs (faster never fails).
Metrics: "1 / 2 / 4 clients" is serve_load's total tok/s, the median over its 3 rounds of the tokens a round
received divided by the round's wall time (time to first token included); the prompt rates are the engine's, the
median of the 3 replicate runs.  A value that is not a finite number counts as missing.
Exit status: 0 the protocol holds, every run is clean and the speed criterion holds; 1 only the speed criterion fails
(the decision record leaves that to the maintainer); 3 a candidate run is not clean while both baseline runs are (the
stability criterion fails); 2 it cannot be evaluated (the protocol is not followed, a baseline run is not clean, a run
cannot be told, the baseline runs disagree, or a number is missing).  Not evaluated here: the robustness run's floors
and the quality subset.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
RUNS_PER_ARM = 2
ORDER = "ABBA"                      # the decision record's order, A the baseline: a linear drift cancels out
BASELINE_SUFFIX = "-iq3_xxs"
SETTLE_S = 300                      # seconds between the service becoming ready (the model loaded) and a run
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

# The captured lines (bench/ab_model_run.sh greps them wide) by kind.  Journal: a "[strata] done:" line is judged by
# its finish (the after record); a slot wait is queueing, not a fault (the leak it may come before is "marked busy
# with no request", a fault); a 4xx is a request the server refused, another client's (one of the perf set's would
# be a failed record too); the traceback of a malformed request cannot be told from a fault of the server.
J_DONE = re.compile(r"\[strata\] done: ")
J_WAITED = re.compile(r"\[strata\] parallel: a request has waited \d+ s for a free slot")
J_REFUSED = re.compile(r"\[strata\] 4\d\d\b")
J_MALFORMED = re.compile(r"\[strata\] 400 malformed request")
J_TRACE = re.compile(r"Traceback \(most recent call last\)|\b\w*(Error|Exception): ")
# Kernel: an Xid (only the engine uses the GPUs) or an OOM kill of the service or of the whole host is a fault; an OOM
# inside another cgroup's own limit and a crash of some process are other processes' (the engine's or the server's
# crash shows as a restart); a kernel problem that names no process of the service cannot be told.
K_FAULT = re.compile(r"NVRM: Xid|Out of memory: Killed process|constraint=CONSTRAINT_NONE|global_oom")
K_SERVICE = re.compile(r"strata-q3\.service")
K_OTHER = re.compile(r"Memory cgroup out of memory|constraint=CONSTRAINT_MEMCG|invoked oom-killer|oom_reaper|segfault|"
                     r"general protection", re.I)


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


def read_lines(path: Path) -> list | None:
    return [l for l in path.read_text().splitlines() if l.strip()] if path.exists() else None


def finite(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def median(xs):
    xs = [x for x in (finite(x) for x in xs) if x is not None]
    return statistics.median(xs) if xs else None


def grep(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.M)
    return m.group(1).strip() if m else None


def num(v: str | None) -> int | None:
    return int(v) if v is not None and v.isdigit() else None


def deployed() -> dict:
    """What protocol step 1 deploys, read from this repository: the ARC cap (systemd/zfs-arc-max.service), the two RAM
    floors and each model's layer cuts (tools/configure_strata_q3.py)."""
    spec = importlib.util.spec_from_file_location("configure_strata_q3", ROOT / "tools" / "configure_strata_q3.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    arc = re.search(r"echo (\d+) > /sys/module/zfs/parameters/zfs_arc_max", (ROOT / "systemd" / "zfs-arc-max.service").read_text())
    return {"arc": arc.group(1) if arc else None, "headroom": tool.ENV_DEFAULTS["STRATA_RESIDENT_HEADROOM_GIB"],
            "park_mib": str(tool.PARK_MIN_FREE_MIB),
            "cuts": {m: ",".join(map(str, tool.CUTS[tool.MODEL_CUTS[m]])) for m in tool.MODELS}}


def ecc(text: str | None) -> dict:
    """{gpu: uncorrected count} from nvidia-smi's "index, corrected, uncorrected;..." (cards that say [N/A] left out)."""
    out = {}
    for part in (text or "").split(";"):
        f = [x.strip() for x in part.split(",")]
        if len(f) == 3 and f[0].isdigit() and f[2].isdigit():
            out[f[0]] = int(f[2])
    return out


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
    env_line = grep(t, r"^engine env: (.*)$")
    load = re.findall(r"^load: (.*)$", t, re.M)
    profile = (grep(t, r"^profile\.conf: (.*)$") or "").split()
    return {"at": grep(t, r"^tag \S+\s+arm \S+\s+at (.+)$"), "model": grep(t, r'"model": "([^"]+)"'),
            "loaded": native.group(1).lower() if native else None, "pack": Path(flags.get("--pack", "")).name or None,
            "native": flags.get("--native"), "ple": flags.get("--ple-gguf"), "split": flags.get("--layer-split"),
            "park_mib": flags.get("--conversation-cache-min-free-mib"),
            "other_args": " ".join(rest), "env": env_line,
            "env_vars": dict(v.split("=", 1) for v in (env_line or "").split() if "=" in v),
            "exe_sha": grep(t, r"^engine pid \S+ exe \S+ sha256 (\w+)"), "engine": grep(t, r"session is up \(engine ([0-9.]+)\)"),
            "server_sha": grep(t, r"^server\.py sha256 (\w+)"), "serve_load_sha": grep(t, r"serve_load\.py sha256 (\w+)"),
            "shared_sha": grep(t, r"^shared settings \S+ sha256 (\w+)"),
            "commit": grep(t, r"^service checkout \S+\s+commit (\S+)"),
            "code_sha": grep(t, r"^deployment code sha256 (\w+)"), "perf_sha": grep(t, r"^perf set sha256 (\w+)"),
            "profile": " ".join(profile) or None,
            "profile_models": [profile[k + 1] for k, x in enumerate(profile[:-1]) if x == "--model"],
            "nonce": grep(t, r"^serve_load nonce (\S+)"),
            "config_sha": grep(t, r"^config \S+ sha256 (\w+)"),
            "config_model_free": grep(t, r"^config \S+ sha256 \w+\s+without the model's fields (\w+)"),
            "arc": grep(t, r"^zfs_arc_max (\d+)"), "ticks": grep(t, r"^engine start ticks (\d+)"),
            "engine_age": num(grep(t, r"^engine start ticks \d+\s+engine age (\d+) s")),
            "ready": num(grep(t, r"^service ready for (\d+) s")), "host_units": grep(t, r"^host units: (\w+)"),
            "started": grep(t, r"^engine started: (.+)$"), "load": load,
            "resident": grep("\n".join(load), r"([\d.]+)% of the experts resident"),
            "slots": "/".join(re.findall(r"^strata generate: (?:expert cache|layer split: CUDA\d runs layers \S+ expert cache) "
                                         r"(\d+) slots", "\n".join(load), re.M)) or None,
            "pcie": "/".join(re.findall(r"pcie_frac (\d\.\d+)", "\n".join(load))) or None,
            "ecc": ecc(grep(t, r"^gpu ecc \(index, corrected, uncorrected\): (.*)$"))}


def after_info(results: Path, tag: str) -> dict:
    """What bench/ab_model_run.sh recorded after the run, and its captured lines; {} when there is no after record.
    Counts are ints, or None when the record says they are unknown; lines are lists, or None when the wrapper could
    not take them or the file is missing."""
    p = results / f"{tag}-after.txt"
    if not p.exists():
        return {}
    t = p.read_text()
    # "(length,cancel=False 44, stop,cancel=False 54)": the names hold a comma themselves
    finishes = {name: int(n) for name, n in re.findall(r"([a-z_]+,cancel=\w+) (\d+)",
                                                       grep(t, r"^journal done lines during the run: \S+ \((.*)\)$") or "")}
    lines = lambda count, kind: read_lines(results / f"{tag}-{kind}-errors.txt") if num(count) is not None else None  # noqa: E731
    return {"rc": num(grep(t, r"\brun_profile exit (\d+)")), "interrupted": grep(t, r"\binterrupted (\w+)\s*$"),
            "same_engine": grep(t, r"^same engine as before: (\w+)"), "idle_end": grep(t, r"^idle at the end: (\w+)"),
            "counted": num(grep(t, r"^metrics requests during the run: (\S+)")),
            "journal": grep(t, r"^journal: (\w+);"),
            "done": num(grep(t, r"^journal done lines during the run: (\S+)")), "finishes": finishes,
            "tracebacks": num(grep(t, r"^perf log Traceback lines: (\S+)")),
            "ecc": ecc(grep(t, r"^gpu ecc \(index, corrected, uncorrected\): (.*)$")),
            "engine_lines": lines(grep(t, r"^engine log lines during the run: (\S+)"), "engine"),
            "journal_lines": lines(grep(t, r"^journal: \w+; lines during the run: (\S+)"), "journal"),
            "kernel_lines": lines(grep(t, r"^kernel lines during the run: (\S+)"), "kernel")}


def journal_kinds(lines: list) -> dict:
    """The service journal's captured lines as faults, other clients' (refused requests) and unclear."""
    out = {"fault": [], "other": [], "unclear": []}
    malformed = any(J_MALFORMED.search(l) for l in lines)
    for l in lines:
        if J_DONE.search(l) or J_WAITED.search(l):
            continue
        if J_REFUSED.search(l):
            out["other"].append(l)
        elif malformed and J_TRACE.search(l):
            out["unclear"].append(l)
        else:
            out["fault"].append(l)
    return out


def kernel_kinds(lines: list) -> dict:
    """The kernel log's captured lines as faults (of the service or the whole host), other processes' and unclear."""
    out = {"fault": [], "other": [], "unclear": []}
    for l in lines:
        if K_FAULT.search(l) or K_SERVICE.search(l):
            out["fault"].append(l)
        elif K_OTHER.search(l):
            out["other"].append(l)
        else:
            out["unclear"].append(l)
    return out


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


def protocol(tags: list, info: dict, results: Path, want: dict) -> list:
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
            continue
        q = i["loaded"]
        if i["pack"] != q:
            bad.append(f"{t} loaded the {q} shard with the pack {i['pack']}")
        if i["ple"] != i["native"].replace("-00001-of-00002.gguf", "-00002-of-00002.gguf"):
            bad.append(f"{t}'s --ple-gguf is not shard 2 of its --native ({i['ple']})")
        if i["split"] != want["cuts"].get(q):
            bad.append(f"{t} ran the layer split {i['split']}, the tool's cuts for {q} are {want['cuts'].get(q)}")
        if i["profile_models"] != [q]:
            bad.append(f"{t}'s profile.conf ({i['profile']}) does not name --model {q} once")
        if i["ready"] is None or i["ready"] < SETTLE_S:
            bad.append(f"{t} started {i['ready']} s after the service became ready, the protocol {SETTLE_S} s")
        if i["engine_age"] is None or i["ready"] is None or i["engine_age"] + 5 < i["ready"]:
            bad.append(f"{t}'s engine ({i['engine_age']} s) is younger than the service's ready time ({i['ready']} s)")
        if i["arc"] != want["arc"]:
            bad.append(f"{t} ran with the ZFS ARC cap {i['arc']}, step 1 deploys {want['arc']}")
        if i["env_vars"].get("STRATA_RESIDENT_HEADROOM_GIB") != want["headroom"]:
            bad.append(f"{t} ran with STRATA_RESIDENT_HEADROOM_GIB {i['env_vars'].get('STRATA_RESIDENT_HEADROOM_GIB')}, "
                       f"step 1 deploys {want['headroom']}")
        if i["park_mib"] != want["park_mib"]:
            bad.append(f"{t} ran with --conversation-cache-min-free-mib {i['park_mib']}, step 1 deploys {want['park_mib']}")
        if i["host_units"] != "ok":
            bad.append(f"{t}: the host units were not as configured ({i['host_units']})")
        if not i["load"]:
            bad.append(f"{t} has no placement lines of its engine's load")
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
                       ("config_model_free", "the config without the model's fields"),
                       ("code_sha", "the service's code"), ("perf_sha", "the perf set"),
                       ("nonce", "serve_load's prompts (nonce)"), ("arc", "the ZFS ARC cap")):
        values = [info[t].get(key) for t in tags]
        if any(v in (None, "unknown") for v in values):
            bad.append(f"{label} is unknown for " + ", ".join(t for t, v in zip(tags, values) if v in (None, "unknown")))
        elif len(set(values)) != 1:
            bad.append(f"the runs differ in {label}: " + "; ".join(f"{t} {v}" for t, v in zip(tags, values)))
    first, last = info[tags[0]]["at"], info[tags[-1]]["at"]
    others = sorted(p.name[:-len("-arm.txt")] for p in results.glob("*-arm.txt") if p.name[:-len("-arm.txt")] not in tags
                    and first <= (grep(p.read_text(), r"^tag \S+\s+arm \S+\s+at (.+)$") or "") <= last)
    if others:
        bad.append(f"other runs were measured between the first and the last and are not included: {', '.join(others)}")
    return bad


def stability(after: dict, rec: dict, arm: dict) -> tuple[list, list, list]:
    """(not clean, cannot tell, notes) for one run."""
    a, dirty, unknown, notes = after, [], [], []
    if not a:
        return [], ["no after record"], []
    # the client side: did the perf set run to its end?
    if a.get("interrupted") != "no":
        unknown.append("the run was interrupted" if a.get("interrupted") == "yes" else "no interrupted line")
    if a.get("rc") != 0:
        unknown.append(f"run_profile.sh exited {a.get('rc')}" if a.get("rc") is not None else "no run_profile exit status")
    if a.get("tracebacks") is None:
        unknown.append("the perf log is missing")
    elif a["tracebacks"]:
        unknown.append(f"{a['tracebacks']} client Tracebacks in the perf log")
    wrong = [f"{k} {n}" for k, n in rec["counts"].items() if n != 1]
    if wrong:
        unknown.append("records not exactly once: " + ", ".join(wrong))
    complete = not unknown
    # the server side
    if a.get("same_engine") != "yes":
        (dirty if a.get("same_engine") == "NO" else unknown).append(
            "the engine restarted" if a.get("same_engine") == "NO" else "no same-engine line")
    if a.get("engine_lines") is None:
        unknown.append("the engine log lines are unknown")
    elif a["engine_lines"]:
        dirty.append(f"{len(a['engine_lines'])} engine log error lines")
    other_traffic = []
    if a.get("journal") != "system" or a.get("journal_lines") is None:
        unknown.append("the system journal was not read")
    else:
        jk = journal_kinds(a["journal_lines"])
        if jk["fault"]:
            dirty.append(f"{len(jk['fault'])} journal fault lines")
        if jk["unclear"]:
            unknown.append(f"{len(jk['unclear'])} journal lines of a malformed request's traceback or a fault")
        if jk["other"]:
            other_traffic.append(f"{len(jk['other'])} requests refused (4xx)")
    if a.get("kernel_lines") is None:
        unknown.append("the kernel log lines are unknown")
    else:
        kk = kernel_kinds(a["kernel_lines"])
        if kk["fault"]:
            dirty.append(f"{len(kk['fault'])} kernel fault lines (Xid, or an OOM kill of the service or the host)")
        if kk["unclear"]:
            unknown.append(f"{len(kk['unclear'])} kernel lines that name no process of the service")
        if kk["other"]:
            notes.append(f"{len(kk['other'])} kernel lines of other processes (a cgroup's own OOM, a crash)")
    if a.get("idle_end") != "yes":
        (other_traffic if a.get("idle_end") == "no" else unknown).append(
            "requests in flight at the end" if a.get("idle_end") == "no" else "the state at the end is unknown")
    counted, done = a.get("counted"), a.get("done")
    if counted is None or done is None:
        unknown.append("the completed-request count is unknown")
    elif counted != done:
        unknown.append(f"/metrics counted {counted} requests and the journal {done}")
    elif counted > EXPECTED_DONE:
        other_traffic.append(f"{counted} requests completed, {EXPECTED_DONE} sent")
    if other_traffic:
        unknown.append("other traffic during the run: " + "; ".join(other_traffic))
    # counts that are a fault only when the perf set ran to its end alone
    attributable = complete and not other_traffic and counted is not None and counted == done
    if attributable and counted < EXPECTED_DONE:
        dirty.append(f"{counted} requests completed, {EXPECTED_DONE} sent")
    odd = {k: v for k, v in a.get("finishes", {}).items() if k not in ("stop,cancel=False", "length,cancel=False")}
    if odd:
        text = "completed requests that were cancelled or ended otherwise: " + ", ".join(f"{k} {v}" for k, v in odd.items())
        (dirty if attributable else unknown).append(text)
    if rec["failed"]:
        dirty.append(f"{rec['failed']} failed requests")
    grown = [g for g, n in a.get("ecc", {}).items() if g in arm.get("ecc", {}) and n > arm["ecc"][g]]
    if grown:
        dirty.append("uncorrected ECC errors on GPU " + ", ".join(grown))
    return dirty, unknown, notes


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
          "| run | measured from | model | loaded / pack | engine started (ticks) | ready s | engine | exe sha256 | "
          "config sha256 (without the model) | service code / perf set sha256 | commit | ARC cap |",
          "|---|---|---|---|---|---:|---|---|---|---|---|---:|"]
    for t in chrono:
        i = info[t]
        o.append(f"| {t} | {i.get('at') or '?'} | {i.get('model') or '?'} | {i.get('loaded') or '?'} / {i.get('pack') or '?'} | "
                 f"{i.get('started') or '?'} ({i.get('ticks') or '?'}) | {fmt(i.get('ready'))} | {i.get('engine') or '?'} | "
                 f"{i.get('exe_sha') or '?'} | {i.get('config_sha') or '?'} ({i.get('config_model_free') or '?'}) | "
                 f"{i.get('code_sha') or '?'} / {i.get('perf_sha') or '?'} | {(i.get('commit') or '?')[:12]} | {i.get('arc') or '?'} |")
    o += ["", "Placement at each engine's load (its log): the experts resident, the expert cache slots per card, the PCIe "
          "fraction per card.", "", "| run | resident | slots | pcie_frac |", "|---|---:|---|---|"]
    for t in chrono:
        i = info[t]
        o.append(f"| {t} | {i.get('resident') or '?'}% | {i.get('slots') or '?'} | {i.get('pcie') or '?'} |")
    try:
        want = deployed()
    except Exception as e:                           # noqa: BLE001 - any failure to read it is "cannot be evaluated"
        return 2, o + ["", f"Cannot be evaluated: cannot read what step 1 deploys from this repository ({type(e).__name__}: {e})."]
    bad = protocol(chrono, info, results, want)
    if bad:
        return 2, o + ["", "Cannot be evaluated: the runs do not follow the protocol: " + "; ".join(bad) + "."]
    base = next(info[t]["model"] for t in chrono if info[t]["model"].endswith(BASELINE_SUFFIX))
    cand = next(info[t]["model"] for t in chrono if info[t]["model"] != base)
    o += ["", "## Each run", "", "| run | model | clean | not clean | cannot tell | notes |", "|---|---|---|---|---|---|"]
    unknown_any, cand_dirty, base_dirty = [], [], []
    for t in chrono:
        dirty, unknown, notes = stability(after[t], recs[t], info[t])
        o.append(f"| {t} | {info[t]['model']} | {'yes' if not dirty and not unknown else 'no'} | {'; '.join(dirty) or '-'} | "
                 f"{'; '.join(unknown) or '-'} | {'; '.join(notes) or '-'} |")
        if dirty:
            (cand_dirty if info[t]["model"] == cand else base_dirty).append(f"{t}: {'; '.join(dirty)}")
        unknown_any += [f"{t}: {u}" for u in unknown]
    unknown_any += [f"the baseline run {d} (the A/B cannot be judged when the baseline is not clean)" for d in base_dirty]
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
    status = 0
    if cand_dirty and not base_dirty:
        o.append("Stability criterion (perf runs): does NOT hold: " + "; ".join(cand_dirty) + ".")
        status = 3
    elif cand_dirty:
        o.append("Stability criterion (perf runs): cannot be judged: candidate runs are not clean (" + "; ".join(cand_dirty)
                 + "), and neither is a baseline run.")
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
