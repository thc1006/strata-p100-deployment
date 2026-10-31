#!/usr/bin/env python3
"""Draws the README's two figures from the records of measured runs, as SVG with real text, in a light and a dark
version.

    python3 docs/images/readme_figures.py            # writes docs/images/*.svg
    python3 docs/images/readme_figures.py --check    # exits 1 when a committed figure differs from what it would write

Both figures show the runs in RUNS (decision 0004's two IQ3_S perf runs) and say so; they do not read the config the
service runs now.  After a measured change of the deployed configuration, set RUNS to the new runs (each needs the
arm record, perf log, monitor and serve_load files that bench/ab_model_run.sh writes, and its lines in
results/stress.jsonl), run this script, and update the README's text around the figures (tests/test_readme_figures.py
compares its numbers with facts()).  The only input that is not a run's record is the watchdog's interval in
systemd/strata-q3-health.timer: after a change to it, run the script again.

deployment*.svg: where the service's parts ran in the first run.  From its arm record (results/<run>-arm.txt): the
engine's arguments (--batch, --conversation-cache-mib, --layer-split, --ple-io), its version, and its lines at the
load: the memory guard's RAM size, the layer ranges, expert caches and PCIe rates of the four cards, the share of the
experts on the cards and the experts kept in RAM, page-locked.  The run lasts from the arm record's start to the
`done` line of its perf log (results/<run>-perf.log).  From its monitor (results/<run>-monitor.csv), the most
memory in use on each card and the most RAM locked (the machine's Mlocked, which the PLE table, locked with
--ple-io ram, makes up).

speed*.svg: what a client got in the runs.  Left, the time to the first token of one request at a time, by prompt
length: every request of their `replicate` tests (results/stress.jsonl; 3 per prompt and run).  Right, serve_load's
tokens per second with 1, 2 and 4 clients at once (results/<run>-serve_load.json): each run's median over its 3
rounds of the tokens all clients received over the round's time, and the median over the run's requests of each
request's tokens over its time after its first token.

Every number is rounded half up from its shortest decimal form (11.45 is 11.5).  The script stops with exit status 2,
and draws nothing, when a number is missing, not finite or not positive, when the runs differ in config, engine,
engine environment, model or day, when the monitor does not cover the first run or has a gap, when a card's memory
in use exceeds the card or the RAM's three rows together exceed the RAM, or when a text line would not fit its place,
or would touch another, at a wide font's width.
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
OUT = ROOT / "docs" / "images"
RUNS = ("ab3-iq3s-1", "ab3-iq3s-2")
STRESS = RESULTS / "stress.jsonl"
TIMER = ROOT / "systemd" / "strata-q3-health.timer"
CLOCK = datetime.timezone(datetime.timedelta(hours=8))   # the records' clock times are Taiwan time (UTC+8)
SLACK = 5                                                 # seconds between the records' clocks and the monitor's
GAP = 10                                                  # the longest the monitor may go without a sample, seconds

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
EM = 0.62    # the widest average glyph width of the fonts above, in em (DejaVu Sans, a fallback, is about 0.6)
BOLD = 1.1   # how much wider a bold line is
# Data colours with 4.5:1 or more on their background: light, Okabe-Ito blue and a darkened vermilion; dark,
# Okabe-Ito sky blue and orange.  Text and lines are GitHub's own light and dark colours
THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "line": "#d1d9e0", "box": "#f6f8fa", "edge": "#59636e",
              "blue": "#0072B2", "orange": "#B34700", "fill": "#cfe3f1", "ram": "#f3e3d9", "bg": "#ffffff"},
    "dark": {"text": "#f0f6fc", "muted": "#9198a1", "line": "#3d444d", "box": "#151b23", "edge": "#9198a1",
             "blue": "#56B4E9", "orange": "#E69F00", "fill": "#1d3a52", "ram": "#3d2d1c", "bg": "#0d1117"},
}


class Missing(Exception):
    pass


def need(m, what: str):
    if m is None or m is False:
        raise Missing(what)
    return m


def num(v, what: str) -> float:
    """A finite positive number, or Missing."""
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            raise Missing(what) from None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise Missing(what)
    return float(v)


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def read_arm(tag: str) -> dict:
    p = RESULTS / f"{tag}-arm.txt"
    t = read_text(p)

    def line(rx: str, what: str):
        return need(re.search(rx, t, re.M), f"{p.name}: {what}")

    gpus = {}
    m = line(r"strata generate: expert cache (\d+) slots, ([\d.]+) GiB of VRAM", "CUDA0's cache")
    gpus[0] = {"gib": num(m[2], "CUDA0's cache")}
    m = line(r"layer split: CUDA0 runs layers (\d+)-(\d+)", "CUDA0's layers")
    gpus[0]["layers"] = (int(m[1]), int(m[2]))
    card = r"layer split: CUDA(\d) runs layers (\d+)-(\d+), expert cache (\d+) slots \(([\d.]+) GiB\)"
    for m in re.finditer(card, t):
        gpus[int(m[1])] = {"layers": (int(m[2]), int(m[3])), "gib": num(m[5], f"CUDA{m[1]}'s cache")}
    need(sorted(gpus) == [0, 1, 2, 3], f"{p.name}: the four cards' lines")
    need(all(gpus[i]["layers"][0] == (gpus[i - 1]["layers"][1] + 1 if i else 0) for i in range(4)),
         f"{p.name}: layers that follow on")
    pcie = [num(x, "a PCIe probe") for x in re.findall(r"PCIe probe:? ([\d.]+) GB/s", t)]
    need(len(pcie) == 4, f"{p.name}: four PCIe probes")
    resident = int(line(r"layer split: (\d+)% of the experts resident", "the share resident")[1])
    need(0 < resident <= 100, f"{p.name}: the share resident")
    args = line(r"^engine args: (.+)$", "the engine's arguments")[1].split()

    def arg(flag: str) -> str:
        need(flag in args and args.index(flag) + 1 < len(args), f"{p.name}: {flag} and its value")
        return args[args.index(flag) + 1]

    split = [int(x) for x in need(re.fullmatch(r"\d+,\d+,\d+", arg("--layer-split")), "--layer-split")[0].split(",")]
    need(split == [gpus[i]["layers"][0] for i in (1, 2, 3)], f"{p.name}: --layer-split as the cards ran it")
    need(arg("--ple-io") == "ram", f"{p.name}: --ple-io ram, which locks the PLE table")
    return {"gpus": gpus, "pcie": (min(pcie), max(pcie)), "resident": resident,
            "ram": num(line(r"memory guard: limit ([\d.]+) GiB \(source: meminfo\)", "the RAM size")[1], "RAM"),
            "experts_ram": num(line(r"resident RAM mode: ([\d.]+) GiB of experts in RAM \(page-locked\)",
                                    "experts in RAM, page-locked")[1], "experts in RAM"),
            "version": line(r"^version line: .*\(engine ([\d.]+)\)", "the engine version")[1],
            "day": line(rf"^tag {re.escape(tag)} .* at (\d{{4}}-\d{{2}}-\d{{2}}) ", "its date")[1],
            "start": line(rf"^tag {re.escape(tag)} .* at (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})$",
                          "its start")[1],
            "config": line(r"^config \S+ sha256 ([0-9a-f]{16})\b", "the config's sha256")[1],
            "exe": line(r"^engine pid \d+ exe \S+ sha256 ([0-9a-f]{16})\b", "the engine's sha256")[1],
            "env": line(r"^engine env: (.+)$", "the engine's environment")[1],
            "quant": line(r'^health: .*"model": "[^"]*-(iq\d_[a-z]+)"', "the model")[1].upper(),
            "slots": int(num(arg("--batch"), "--batch")),
            "cache_gib": num(arg("--conversation-cache-mib"), "--conversation-cache-mib") / 1024}


def run_span(tag: str, start: str) -> tuple[float, float]:
    """The run from its arm record's start to its perf log's `done` line, as Unix times."""
    p = RESULTS / f"{tag}-perf.log"
    done = need(re.findall(r"^\[(\d{2}:\d{2}:\d{2})\] done$", read_text(p), re.M) or None,
                f"{p.name}: its done line")[-1]
    t0 = datetime.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CLOCK)
    t1 = datetime.datetime.strptime(f"{start[:10]} {done}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CLOCK)
    need(t0 < t1, f"{p.name}: a done line after the start")
    return t0.timestamp(), t1.timestamp()


def read_monitor(tag: str, span: tuple[float, float]) -> dict:
    p = RESULTS / f"{tag}-monitor.csv"
    try:
        with p.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except csv.Error as e:
        raise Missing(f"{p.name}: {e}") from None
    need(len(rows) > 1, f"{p.name}: rows")
    cols = ["t", "vram_total_mib", "mlocked_gib"] + [f"g{i}_mem_mib" for i in range(4)]
    v = {k: [num(r.get(k), f"{p.name}: {k} on line {n + 2}") for n, r in enumerate(rows)] for k in cols}
    need(len(set(v["vram_total_mib"])) == 1, f"{p.name}: one VRAM size")
    need(v["t"][0] <= span[0] + SLACK and span[1] - SLACK <= v["t"][-1], f"{p.name}: a monitor that covers the run")
    need(max(b - a for a, b in zip(v["t"], v["t"][1:])) <= GAP, f"{p.name}: a monitor without gaps")
    return {"locked": max(v["mlocked_gib"]), "vram_gib": v["vram_total_mib"][0] / 1024,
            "used": [max(v[f"g{i}_mem_mib"]) / 1024 for i in range(4)], "minutes": (v["t"][-1] - v["t"][0]) / 60}


def read_timer() -> int:
    m = need(re.search(r"^OnUnitActiveSec=(\d+)min$", read_text(TIMER), re.M), f"{TIMER.name}: OnUnitActiveSec")
    return int(num(m[1], "OnUnitActiveSec"))


def read_speed() -> dict:
    ttft = {}
    for line in read_text(STRESS).splitlines():
        if not any(f'"tag": "{tag}"' in line for tag in RUNS):
            continue
        d = json.loads(line)
        tag = d.get("tag")
        if tag not in RUNS:
            continue
        if d.get("scenario") != "replicate":
            continue
        for name, p in d["result"].items():
            for r in p["runs"]:
                need(r.get("status") == 200 and r.get("cached") == 0 and r.get("error") is None
                     and r.get("finish") in ("length", "stop"), f"stress.jsonl: {name} served in full")
                n = int(num(r.get("prompt_tokens"), f"{name}: prompt tokens"))
                ttft.setdefault(name, {}).setdefault(tag, []).append((n, num(r.get("ttft_s"), f"{name}: TTFT")))
    need(len(ttft) == 4 and all(sorted(v) == sorted(RUNS) and all(len(x) == 3 for x in v.values())
                                for v in ttft.values()), "stress.jsonl: 4 prompts, 3 requests per prompt and run")
    load = {}
    for tag in RUNS:
        for s in json.loads(read_text(RESULTS / f"{tag}-serve_load.json")):
            need(s.get("errors") == 0, f"{tag}-serve_load.json: errors")
            load.setdefault(s.get("clients"), []).append((num(s.get("total_tps"), f"{tag}: total_tps"),
                                                          num(s.get("per_req_tps"), f"{tag}: per_req_tps")))
    need(sorted(load) == [1, 2, 4] and all(len(v) == len(RUNS) for v in load.values()),
         "serve_load: 1, 2 and 4 clients in each run")
    return {"ttft": {k: [p for x in v.values() for p in x] for k, v in ttft.items()}, "load": load}


def fmt(x: float, digits: int = 1) -> str:
    """x rounded half up to `digits` decimals, from its shortest decimal form (11.45 is 11.5, as written)."""
    return str(Decimal(repr(x)).quantize(Decimal(1).scaleb(-digits), ROUND_HALF_UP))


def rng(values, digits: int = 1) -> str:
    """lo–hi, each rounded half up, or one number when both ends round alike."""
    lo, hi = fmt(min(values), digits), fmt(max(values), digits)
    return lo if lo == hi else f"{lo}–{hi}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def width(s: str, size: int, bold: bool = False) -> float:
    return len(s) * size * EM * (BOLD if bold else 1)


class Svg:
    def __init__(self, w: int, h: int, theme: dict, title: str, desc: str):
        self.c, self.w, self.boxes = theme, w, []
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
                      f'role="img" aria-labelledby="t d" font-family="{esc(FONT)}">',
                      f'<title id="t">{esc(title)}</title>', f'<desc id="d">{esc(desc)}</desc>']
        self.rect(0, 0, w, h, theme["bg"], rx=0)

    def rect(self, x, y, w, h, fill, stroke="none", rx=6, dash=""):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx}" fill="{fill}" '
                          f'stroke="{stroke}"{d}/>')

    def line(self, x1, y1, x2, y2, stroke, width=1.0, arrow=False, dash=""):
        a = ' marker-end="url(#arrow)"' if arrow else ""
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" stroke="{stroke}" '
                          f'stroke-width="{width:g}"{d}{a}/>')

    def text(self, x, y, s, size=14, fill=None, anchor="start", weight="normal", room=None):
        """`room`: the width the line may take (to the canvas's edge when not given).  A line wider than its room, or
        whose box meets another line's, stops the script."""
        wd = width(s, size, weight == "bold")
        lo = x - (wd if anchor == "end" else wd / 2 if anchor == "middle" else 0)
        box = (lo, y - size, lo + wd, y + size * 0.25)
        need(lo >= 0 and box[2] <= self.w and (room is None or wd <= room), f"room for the text {s!r}")
        for o, other in self.boxes:
            need(box[2] <= o[0] or o[2] <= box[0] or box[3] <= o[1] or o[3] <= box[1],
                 f"room for the text {s!r} beside {other!r}")
        self.boxes.append((box, s))
        w = ' font-weight="600"' if weight == "bold" else ""
        self.parts.append(f'<text x="{x:g}" y="{y:g}" font-size="{size}" fill="{fill or self.c["text"]}" '
                          f'text-anchor="{anchor}"{w}>{esc(s)}</text>')

    def dot(self, x, y, fill, stroke, r=4.5):
        self.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="{stroke}" '
                          f'stroke-width="1.5"/>')

    def arrow_marker(self):
        self.parts.append(f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                          f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" '
                          f'fill="{self.c["edge"]}"/></marker></defs>')

    def done(self) -> str:
        return "\n".join(self.parts + ["</svg>"]) + "\n"


def gib(x: float) -> str:
    return f"{fmt(x)} GiB"


def ram_rows(arm: dict, mon: dict) -> list[tuple[float, str, bool]]:
    """Host RAM's rows: size, label, and whether the size is a limit rather than a measurement."""
    off = 100 - arm["resident"]
    experts = (f"experts, page-locked: the {off}% off the cards, and those of slots lent to prompts" if off else
               "experts, page-locked: those of slots lent to prompts")
    return [(mon["locked"], "per-layer embedding table, locked", False), (arm["experts_ram"], experts, False),
            (arm["cache_gib"], "parked conversations, at most", True)]


def deployment(theme: dict, arm: dict, mon: dict, every: int) -> str:
    c = theme
    g = arm["gpus"]
    off = 100 - arm["resident"]
    run = f"run {RUNS[0]} on {arm['day']}"
    desc = (f"Where the service ran in {run}. Clients send requests to server.py, which runs {arm['slots']} requests "
            f"at a time on one Strata {arm['version']} engine. The engine splits the model's "
            f"{g[3]['layers'][1] + 1} layers over four Tesla P100 cards, and a token passes them in turn: "
            + "; ".join(f"GPU {i} runs layers {g[i]['layers'][0]} to {g[i]['layers'][1]} with {gib(g[i]['gib'])} "
                        f"of expert cache and had {gib(mon['used'][i])} in use at most" for i in range(4))
            + f". Each card has {fmt(mon['vram_gib'], 0)} GiB; the memory in use is the most seen in the "
            f"{fmt(mon['minutes'], 0)} minutes of the run. {arm['resident']}% of the experts are on the cards. "
            f"Host RAM, {gib(arm['ram'])}, holds the per-layer embedding table, {gib(mon['locked'])} locked; "
            f"{gib(arm['experts_ram'])} of experts, page-locked: "
            + (f"the {off}% no card holds, and " if off else "")
            + "copies of the experts in the card slots that reading a prompt borrows; and the parked conversations, "
            f"{gib(arm['cache_gib'])} at most. Each card reads RAM over PCIe at {rng(arm['pcie'])} GB/s. This "
            f"repository checks the config before every start, waits for the load, checks the service every {every} "
            "minutes, and compares the machine with the repository when asked.")
    s = Svg(960, 600, c, f"Where the service ran: run {RUNS[0]}, {arm['day']}", desc)
    s.arrow_marker()
    # the request path
    s.rect(20, 20, 250, 60, c["box"], c["line"])
    s.text(145, 45, "Lab members' clients", 15, anchor="middle", weight="bold", room=230)
    s.text(145, 66, "OpenAI or Anthropic API", 13, c["muted"], "middle", room=230)
    s.line(270, 50, 355, 50, c["edge"], 1.5, True)
    s.text(312, 41, "HTTP", 13, c["muted"], "middle", room=80)
    s.rect(355, 20, 290, 60, c["box"], c["line"])
    s.text(500, 45, "server.py", 15, anchor="middle", weight="bold", room=270)
    s.text(500, 66, f"{arm['slots']} requests at a time", 13, c["muted"], "middle", room=270)
    s.line(500, 80, 500, 110, c["edge"], 1.5, True)
    s.text(924, 45, run, 13, c["muted"], "end", room=260)
    # the engine and its four cards, in the order a token passes them
    s.rect(20, 110, 920, 236, "none", c["edge"], 8)
    s.text(36, 136, f"Strata {arm['version']} engine, one process", 15, weight="bold", room=440)
    s.text(924, 136, f"{arm['resident']}% of the experts on the cards", 13, c["muted"], "end", room=420)
    w, gap, x0, y0 = 206, 20, 36, 150
    for i in range(4):
        x = x0 + i * (w + gap)
        s.rect(x, y0, w, 158, c["box"], c["line"])
        s.text(x + 12, y0 + 24, f"GPU {i}", 15, weight="bold", room=70)
        s.text(x + w - 12, y0 + 24, "Tesla P100", 13, c["muted"], "end", room=100)
        lo, hi = g[i]["layers"]
        s.text(x + 12, y0 + 52, f"layers {lo}–{hi}", 15, room=w - 24)
        s.text(x + 12, y0 + 80, f"expert cache {gib(g[i]['gib'])}", 13, room=w - 24)
        bw = w - 24
        used = bw * mon["used"][i] / mon["vram_gib"]
        s.rect(x + 12, y0 + 92, bw, 16, c["bg"], c["edge"], 3)
        s.rect(x + 12, y0 + 92, used, 16, c["fill"], "none", 3)
        s.rect(x + 12, y0 + 92, bw * g[i]["gib"] / mon["vram_gib"], 16, c["blue"], "none", 3)
        s.line(x + 12 + used, y0 + 92, x + 12 + used, y0 + 108, c["edge"], 1.5)
        s.text(x + 12, y0 + 128, f"peak in use {gib(mon['used'][i])}", 13, c["muted"], room=w - 24)
        s.text(x + 12, y0 + 145, f"of {fmt(mon['vram_gib'], 0)} GiB", 13, c["muted"], room=w - 24)
        if i < 3:
            s.line(x + w, y0 + 48, x + w + gap, y0 + 48, c["edge"], 1.5, True)
    ky, kx = y0 + 172, x0
    peak = f"other memory in use, peak of the run ({fmt(mon['minutes'], 0)} min)"
    for swatch, stroke, label in ((c["blue"], "none", "expert cache"), (c["fill"], c["edge"], peak),
                                  (c["bg"], c["edge"], "free at that peak")):
        s.rect(kx, ky, 14, 12, swatch, stroke, 2)
        s.text(kx + 20, ky + 11, label, 13, c["muted"])
        kx += 20 + width(label, 13) + 24
    # host RAM, linked to every card
    for i in range(4):
        x = x0 + i * (w + gap) + w / 2
        s.line(x, 346, x, 374, c["edge"], 1.2, dash="4 3")
    s.rect(20, 374, 920, 120, c["box"], c["line"])
    s.text(36, 400, f"Host RAM, {gib(arm['ram'])}", 15, weight="bold", room=400)
    s.text(924, 400, f"PCIe to each card {rng(arm['pcie'])} GB/s", 13, c["muted"], "end", room=400)
    track = 180
    for k, (v, label, limit) in enumerate(ram_rows(arm, mon)):
        y = 414 + k * 24
        s.rect(36, y, track, 16, c["bg"], c["line"], 3)
        if limit:
            s.rect(36, y, track * v / arm["ram"], 16, "none", c["orange"], 3, "4 3")
        else:
            s.rect(36, y, track * v / arm["ram"], 16, c["ram"], c["orange"], 3)
        s.text(36 + track + 12, y + 13, f"{gib(v)} {label}", 13, room=924 - 36 - track - 12)
    # what this repository does to the service
    s.rect(20, 512, 920, 70, "none", c["edge"], 6, "6 4")
    s.text(36, 534, "This repository", 14, weight="bold", room=880)
    s.text(36, 554, f"checks the config before every start · waits for the load · checks the service every {every} "
                    "minutes", 13, c["muted"], room=880)
    s.text(36, 572, "compares the machine with the repository when asked", 13, c["muted"], room=880)
    return s.done()


def groups(sp: dict) -> list[list[tuple[int, float]]]:
    """The first-token times: the two short prompts together, then each long one, shortest first."""
    by_len = sorted(sp["ttft"].values(), key=lambda v: min(p[0] for p in v))
    short = [p for v in by_len[:2] for p in v]
    need(max(p[0] for p in short) < 1000 <= min(p[0] for p in by_len[2]), "two short prompts and two long ones")
    return [short, by_len[2], by_len[3]]


def tokens(v) -> str:
    lo, hi = min(p[0] for p in v), max(p[0] for p in v)
    return f"{lo}–{hi} tokens" if hi < 1000 else (f"{fmt(hi / 1000)}K" if hi < 10000 else f"{fmt(hi / 1000, 0)}K")


def speed(theme: dict, sp: dict, arm: dict) -> str:
    c = theme
    load = sp["load"]
    pts = sorted(p for v in sp["ttft"].values() for p in v)
    title = f"Measured speed of {arm['quant']} on Strata {arm['version']}, {arm['day']} (runs {', '.join(RUNS)})"
    desc = (f"{title}. Left: time to the first token of one request at a time: "
            + "; ".join(f"{tokens(v)}, {rng([p[1] for p in v])} s" for v in groups(sp))
            + ". Right: tokens per second with 1, 2 and 4 clients at once: all clients together in a run's median "
            "round, and a run's median request after its first token: "
            + "; ".join(f"{n} {'client' if n == 1 else 'clients'}: {rng([t for t, _ in v])} together, "
                        f"{rng([p for _, p in v])} per request" for n, v in sorted(load.items())) + ".")
    s = Svg(960, 420, c, title, desc)
    s.text(20, 406, f"{arm['quant']} on Strata {arm['version']}, runs {' and '.join(RUNS)} on {arm['day']}", 13,
           c["muted"])
    # left: first token by prompt length
    L, T, W, H = 70, 60, 380, 270
    ymax = 10 * (int(max(p[1] for p in pts) // 10) + 1)
    xmax = max(p[0] for p in pts) * 1.05
    s.text(20, 28, "Time to the first token, one request at a time", 15, weight="bold", room=474)
    for v in range(0, ymax + 1, 10):
        y = T + H - H * v / ymax
        s.line(L, y, L + W, y, c["line"])
        s.text(L - 8, y + 4, f"{v} s", 13, c["muted"], "end")
    for v in range(0, int(xmax) + 1, 8000):
        s.text(L + W * v / xmax, T + H + 20, f"{v // 1000}K" if v else "0", 13, c["muted"], "middle")
    s.text(L + W / 2, T + H + 42, "prompt tokens", 13, c["muted"], "middle")
    for n, t in pts:
        s.dot(L + W * n / xmax, T + H - H * t / ymax, c["fill"], c["blue"])
    for v in groups(sp):
        n, hi = max(p[0] for p in v), max(p[1] for p in v)
        x, y = L + W * n / xmax, T + H - H * hi / ymax
        far = n > xmax / 2
        label = f"{tokens(v)}: {rng([p[1] for p in v])} s"
        s.text(x + (-10 if far else 14), y - 10, label, 13, anchor="end" if far else "start",
               room=(x - 10 - L) if far else (L + W - x - 14))
    # right: tokens per second by clients
    L, W = 560, 360
    tmax = 20 * (int(max(t for v in load.values() for t, _ in v) // 20) + 1)
    s.text(500, 28, "Tokens per second, clients at once", 15, weight="bold", room=440)
    for v in range(0, tmax + 1, 20):
        y = T + H - H * v / tmax
        s.line(L, y, L + W, y, c["line"])
        s.text(L - 8, y + 4, str(v), 13, c["muted"], "end")
    xs = {n: L + W * (k + 0.5) / 3 for k, n in enumerate(sorted(load))}
    for n, x in xs.items():
        s.text(x, T + H + 20, str(n), 13, c["muted"], "middle")
        tot, per = [t for t, _ in load[n]], [p for _, p in load[n]]
        for t in tot:
            s.dot(x - 7, T + H - H * t / tmax, c["fill"], c["blue"])
        for p in per:
            s.dot(x + 7, T + H - H * p / tmax, "none", c["orange"])
        s.text(x - 16, T + H - H * max(tot) / tmax + 4, rng(tot), 13, c["blue"], "end", room=W / 3 - 20)
        s.text(x + 16, T + H - H * max(per) / tmax + 4, rng(per), 13, c["orange"], room=W / 3 - 20)
    s.text(L + W / 2, T + H + 42, "clients sending at the same time", 13, c["muted"], "middle")
    n4, n1 = max(load), min(load)
    s.text(xs[n4] - 16, T + H - H * max(t for t, _ in load[n4]) / tmax - 14, "all clients together", 13, c["blue"],
           "end")
    y1 = T + H - H * max(p for _, p in load[n1]) / tmax
    s.text(xs[n1] + 16, y1 - 32, "a request (median),", 13, c["orange"])
    s.text(xs[n1] + 16, y1 - 15, "after its first token", 13, c["orange"])
    return s.done()


def read_all() -> tuple[dict, dict, dict, int]:
    arms = [read_arm(tag) for tag in RUNS]
    for key, what in (("config", "config"), ("exe", "engine"), ("version", "engine version"),
                      ("env", "engine environment"), ("quant", "model"), ("day", "day")):
        need(len({a[key] for a in arms}) == 1, f"one {what} in every run")
    mon = read_monitor(RUNS[0], run_span(RUNS[0], arms[0]["start"]))
    for i in range(4):
        need(arms[0]["gpus"][i]["gib"] <= mon["used"][i], f"GPU {i}: an expert cache within the memory in use")
        need(mon["used"][i] <= mon["vram_gib"], f"GPU {i}: memory in use within the card")
    need(mon["locked"] + arms[0]["experts_ram"] + arms[0]["cache_gib"] <= arms[0]["ram"], "the RAM's rows within it")
    return arms[0], mon, read_speed(), read_timer()


def facts() -> dict[str, str]:
    """What the README's text above the figures and in their captions says, as the figures say it."""
    arm, mon, sp, _ = read_all()
    short, mid, long_ = groups(sp)
    four = sp["load"][4]
    return {"layers": f"{arm['gpus'][3]['layers'][1] + 1} layers", "resident": f"{arm['resident']}% of the experts",
            "config": arm["config"][:8], "version": f"Strata {arm['version']}", "day": arm["day"],
            "short": f"{rng([p[1] for p in short])} s", "mid": f"{rng([p[1] for p in mid])} s after {tokens(mid)}",
            "long": f"{rng([p[1] for p in long_])} s after {tokens(long_)}",
            "together": f"{rng([t for t, _ in four])} tokens per second together",
            "request": f"{rng([p for _, p in four])} tokens per second after its first token",
            "run 1": f"`{RUNS[0]}`", "run 2": f"`{RUNS[1]}`", "arm": f"`results/{RUNS[0]}-arm.txt`",
            "monitor": f"`results/{RUNS[0]}-monitor.csv`"}


def figures() -> dict[str, str]:
    arm, mon, sp, every = read_all()
    out = {}
    for name, th in THEMES.items():
        suffix = "" if name == "light" else "-dark"
        out[f"deployment{suffix}.svg"] = deployment(th, arm, mon, every)
        out[f"speed{suffix}.svg"] = speed(th, sp, arm)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed figures instead of writing")
    a = ap.parse_args()
    try:
        figs = figures()
    except (Missing, OSError, KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError) as e:
        print(f"readme_figures: cannot read {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if a.check:
        bad = [n for n, t in figs.items() if not (OUT / n).is_file() or read_text(OUT / n) != t]
        for n in bad:
            print(f"readme_figures: docs/images/{n} is not what the script draws", file=sys.stderr)
        return 1 if bad else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n, t in figs.items():
        (OUT / n).write_text(t, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
