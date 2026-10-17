#!/usr/bin/env python3
"""Latency scenarios in the form upstream measures them (PR #2050 / #2326) plus open-loop Poisson arrivals.

  coldjoin   two streams decode long answers; a cold 16K prompt joins at +10 s and a cold 8K prompt 6 s later.
             Per decoding stream: longest pause, total time paused (gaps over 0.5 s), tokens received while the
             joiners were read; the joiners' TTFT.  (#2050 used 4 streams + 64K/32K joiners on --batch 6; this box
             has 4 slots and a 32K context, so 2 + 2.)
  preempt    nothing decoding; a cold ~28K prompt, and 1 s later a one-line question: the question's TTFT says
             whether a short request can get ahead of a long read (BYIELD), the long prompt's TTFT what it costs.
  poisson    open-loop arrivals at --rate requests/s (exponential gaps, seeded), a lab-like mix of short chats with
             thinking and 2K / 8K / 16K document prompts; per request TTFT, longest pause, TPOT and the SLO verdict.

SLO (fixed before any run, P100-scaled): TTFT <= 10 s + prompt_tokens / 400 tok/s, longest pause <= 10 s,
decode >= 5 tok/s.  Goodput = the share of requests that meet all three.

  python3 bench/latency_suite.py coldjoin preempt --tag R1 --out results/latency.jsonl
  python3 bench/latency_suite.py poisson --rate 0.1 --requests 30 --tag R1 --out results/latency.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402  (chat(), sized(), docs_corpus(), the HTTP helpers)

PAUSE_S = 0.5
SLO_TTFT_BASE_S, SLO_PREFILL_TOK_S, SLO_PAUSE_S, SLO_DECODE_TOK_S = 10.0, 400.0, 10.0, 5.0
SHORT = ["用三句話解釋什麼是梯度消失。", "幫我把這句話翻成英文：今天的實驗數據需要重新校正。",
         "列出三個常見的 Python 效能分析工具並各用一句話說明。", "解釋 TCP 三向交握的目的。",
         "簡述快速排序的平均與最壞時間複雜度。", "什麼是 MoE 模型中的 router？用兩句話回答。"]


def gaps(r: dict) -> list[float]:
    ch = r["chunks"]
    return [b - a for a, b in zip(ch, ch[1:])]


def pct_raw(xs, p):
    """Nearest-rank percentile (p in 0..1): the smallest value with at least p of the values at or below it."""
    xs = sorted(x for x in xs if x is not None)
    return xs[max(0, math.ceil(p * len(xs)) - 1)] if xs else None


def pct(xs, p):
    v = pct_raw(xs, p)
    return round(v, 2) if v is not None else None


def stream_stats(r: dict, t_from: float | None = None, t_to: float | None = None) -> dict:
    g = gaps(r)
    ch = r["chunks"]
    u = r.get("usage") or {}
    gen = u.get("completion_tokens")
    n = SM.streamed_tokens(r)
    # chunk gaps mix two regimes (MTP bursts ~3 ms apart on the solo path, one row per window in a batch), so their
    # percentiles are labelled as chunk gaps; TPOT and the pauses are the per-request figures
    out = {"ttft_s": round(ch[0] - r["sent"], 2) if ch else None,
           "ttft_content_s": round(r["content_first"] - r["sent"], 2) if r.get("content_first") else None,
           "total_s": round(r["done"] - r["sent"], 2),
           "longest_pause_s": round(max(g), 2) if g else None,
           "paused_s": round(sum(x for x in g if x > PAUSE_S), 2),
           "chunk_gap_p50_ms": round(1000 * statistics.median(g), 1) if g else None,
           "chunk_gap_p99_ms": round(1000 * pct_raw(g, 0.99), 1) if g else None,
           "tpot_ms": round(1000 * (ch[-1] - ch[0]) / (n - 1), 1) if n and n > 1 and len(ch) > 1 else None,
           "decode_tok_s": round((n - 1) / (ch[-1] - ch[0]), 2) if n and n > 1 and len(ch) > 1 and ch[-1] > ch[0] else None,
           "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": gen,
           "status": r.get("status"), "error": r.get("error")}
    if t_from is not None:
        win = [t for t in ch if t_from <= t <= t_to]
        out["chunks_in_window"] = len(win)
        # a stream that finished inside the window is not paused after its last chunk
        end = min(t_to, ch[-1]) if ch else t_to
        wg = [b - a for a, b in zip([t_from] + win, win + [end]) if b >= a]
        out["longest_pause_in_window_s"] = round(max(wg), 2) if wg else None
        out["finished_inside_window"] = bool(ch) and t_from <= ch[-1] < t_to
    return {k: v for k, v in out.items() if v is not None}


def slo(s: dict) -> bool:
    if s.get("status") != 200 or s.get("error") or s.get("ttft_s") is None:
        return False
    ttft_ok = s["ttft_s"] <= SLO_TTFT_BASE_S + (s.get("prompt_tokens") or 0) / SLO_PREFILL_TOK_S
    pause_ok = (s.get("longest_pause_s") or 0) <= SLO_PAUSE_S
    dec_ok = (s.get("decode_tok_s") or 0) >= SLO_DECODE_TOK_S or (s.get("completion_tokens") or 0) <= 2
    return ttft_ok and pause_ok and dec_ok


def run_at(jobs: list[tuple[float, callable]]) -> list[dict]:
    """Each job (delay_s, fn) starts at its offset from now, on its own thread."""
    res: list = [None] * len(jobs)
    t0 = time.time()

    def go(i, d, fn):
        time.sleep(max(0.0, t0 + d - time.time()))
        res[i] = fn()

    th = [threading.Thread(target=go, args=(i, d, fn), daemon=True) for i, (d, fn) in enumerate(jobs)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return res


def s_coldjoin(a) -> dict:
    text = SM.docs_corpus()
    j16, j8 = SM.sized(text, 16000, 90000), SM.sized(text, 8000, 400000)
    essay = "Write a detailed essay of about 1500 words on {}. Use plain prose."
    res = run_at([(0.0, lambda: SM.chat(essay.format("the history of lighthouses"), max_tokens=2000)),
                  (0.0, lambda: SM.chat(essay.format("how mechanical watches keep time"), max_tokens=2000)),
                  (10.0, lambda: SM.chat(j16, max_tokens=128)),
                  (16.0, lambda: SM.chat(j8, max_tokens=128))])
    dec, joins = res[:2], res[2:]
    w0 = joins[0]["sent"]
    w1 = max(j["chunks"][0] if j["chunks"] else j["done"] for j in joins)
    out = {"decoders": [stream_stats(r, w0, w1) for r in dec], "joiners": [stream_stats(r) for r in joins],
           "join_window_s": round(w1 - w0, 2)}
    print("  decoders longest pause:", [d.get("longest_pause_s") for d in out["decoders"]],
          "paused:", [d.get("paused_s") for d in out["decoders"]],
          "chunks while joining:", [d.get("chunks_in_window") for d in out["decoders"]],
          "joiners TTFT:", [j.get("ttft_s") for j in out["joiners"]], flush=True)
    return out


def s_preempt(a) -> dict:
    long = SM.sized(SM.docs_corpus(), 28000, 250000)
    res = run_at([(0.0, lambda: SM.chat(long, max_tokens=64)),
                  (1.0, lambda: SM.chat("一句話回答：台灣最高的山是哪一座？", max_tokens=48))])
    out = {"long": stream_stats(res[0]), "short": stream_stats(res[1])}
    print("  short question TTFT / done:", out["short"].get("ttft_s"), "/", out["short"].get("total_s"),
          "s; long prompt TTFT:", out["long"].get("ttft_s"), "s", flush=True)
    return out


def s_poisson(a) -> dict:
    rnd = random.Random(a.seed)
    text = SM.docs_corpus()
    docs = {n: SM.sized(text, n, rnd.randrange(0, 300000)) for n in (2000, 8000, 16000)}
    kinds = [("short", 45), ("doc2k", 25), ("doc8k", 20), ("doc16k", 10)]

    def make(kind):
        if kind == "short":
            q = rnd.choice(SHORT)
            return lambda: SM.chat(q, max_tokens=1024, effort="low", budget=512)
        n = {"doc2k": 2000, "doc8k": 8000, "doc16k": 16000}[kind]
        return lambda: SM.chat(docs[n], max_tokens=384 if n == 16000 else 512)

    t, jobs, plan = 0.0, [], []
    for _ in range(a.requests):
        t += rnd.expovariate(a.rate)
        kind = rnd.choices([k for k, _ in kinds], [w for _, w in kinds])[0]
        jobs.append((t, make(kind)))
        plan.append((round(t, 1), kind))
    t_start = time.time()
    res = run_at(jobs)
    wall = time.time() - t_start
    rows = [{"kind": k, "at_s": at, **stream_stats(r)} for (at, k), r in zip(plan, res)]
    for r in rows:
        r["slo"] = slo(r)
    by = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)

    out = {"rate_req_s": a.rate, "requests": a.requests, "seed": a.seed, "wall_s": round(wall, 1),
           "goodput_share": round(sum(r["slo"] for r in rows) / len(rows), 3),
           "errors": sum(1 for r in rows if r.get("status") != 200 or r.get("error")),
           "ttft_p50": pct([r.get("ttft_s") for r in rows], 0.5), "ttft_p95": pct([r.get("ttft_s") for r in rows], 0.95),
           "longest_pause_p95": pct([r.get("longest_pause_s") for r in rows], 0.95),
           "longest_pause_max": pct([r.get("longest_pause_s") for r in rows], 1.0),
           "by_kind": {k: {"n": len(v), "slo_ok": sum(r["slo"] for r in v),
                           "ttft_p50": pct([r.get("ttft_s") for r in v], 0.5),
                           "ttft_max": pct([r.get("ttft_s") for r in v], 1.0),
                           "decode_p50": pct([r.get("decode_tok_s") for r in v], 0.5)} for k, v in by.items()},
           "rows": rows}
    print(f"  rate {a.rate}/s: goodput {out['goodput_share']:.0%}, errors {out['errors']}, TTFT p50/p95 "
          f"{out['ttft_p50']}/{out['ttft_p95']} s, longest pause p95/max {out['longest_pause_p95']}/{out['longest_pause_max']} s",
          flush=True)
    return out


SCEN = {"coldjoin": s_coldjoin, "preempt": s_preempt, "poisson": s_poisson}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="+", choices=list(SCEN))
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="")
    ap.add_argument("--url", default=SM.URL)
    ap.add_argument("--rate", type=float, default=0.1)
    ap.add_argument("--requests", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20261016)
    a = ap.parse_args()
    SM.URL = a.url.rstrip("/")
    for name in a.scenarios:
        print(f"[{time.strftime('%H:%M:%S')}] {name} ({a.tag})", flush=True)
        t0 = time.time()
        res = SCEN[name](a)
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tag": a.tag, "scenario": name, "start": round(t0, 1), "end": round(time.time(), 1),
                                "result": res}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
