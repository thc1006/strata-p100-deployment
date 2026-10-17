#!/usr/bin/env python3
"""Stress matrix for the Strata IQ3_XXS service on 4x P100 (standard library only).

Every scenario appends one JSON line (with its start/end unix time, for monitor.py summary --since/--until) to --out.

  replicate   the community 4x P100 report's method (~/dev/strata/bench/results/2026-10-06-community-4x-tesla-p100):
              fresh nonce, reasoning none, temperature 0, 256-token cap; its two canonical prompts, ~4.4K and ~32K-token
              docs prompts
  think4      four different requests at once, the way the lab uses it: reasoning medium, budget 1024, max_tokens 4096
  worst       four ~27K-token prompts at once (each slot filled to ~28K of its 32K context)
  boundary    prompt + max_tokens + 8 = 32768 is accepted, one token more is a 400 (fit_max_tokens false)
  disconnect  four streams, one client drops mid-answer: the others finish, a fifth request is admitted at once
  qa          16 questions with known answers, solo one at a time and then four at a time; answers parsed and checked
  soak        --minutes of four clients sending a random mix (short, thinking, 4K and 8K prompts); errors and latency
  multiturn   issue #1994's shape: four growing five-turn conversations while two more clients send short requests

  python3 bench/stress_matrix.py replicate --out results/baseline-stress.jsonl --tag baseline
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8080"
KEY = os.environ.get("STRATA_KEY", "")
ROOT = Path.home() / "dev" / "strata"
COMMUNITY = ROOT / "bench" / "results" / "2026-10-06-community-4x-tesla-p100"
DOCS_QUESTION = ("\n\nThe text above is documentation for an inference engine. In a few short paragraphs, explain what "
                 "the engine does and how it keeps a large mixture-of-experts model fast on a consumer PC.")

THINK4 = [
    "請以繁體中文說明 MoE 模型中 expert routing 的負載不平衡問題，並提出兩種緩解方法與各自代價。",
    "請寫一個 Python 函式，用動態規劃求兩個字串的最長共同子序列，回傳子序列本身，並分析時間與空間複雜度。",
    "請以繁體中文比較 TCP BBR 與 CUBIC 的壅塞控制設計差異，並說明各自適合的網路環境。",
    "請以繁體中文說明研究倫理審查（IRB）在人體研究中的角色，以及審查時最常關注的五個重點。",
]

QA = [  # (question, answer): unique numeric answers, checked after "最終答案："
    ("數列 3, 7, 15, 31, 63 的下一項是多少？", 127), ("17 × 23 等於多少？", 391),
    ("從 1 加到 100 的總和是多少？", 5050), ("一個長方形周長 30，長比寬多 5，面積是多少？", 50),
    ("2 的 20 次方是多少？", 1048576), ("某商品原價 800 元，先打 75 折再減 50 元，最後價格是多少元？", 550),
    ("12 和 18 的最小公倍數是多少？", 36), ("邊長 4 的正方體表面積是多少？", 96),
    ("若 x + 2y = 11 且 3x − y = 5，x 乘以 y 是多少？", 12), ("7 的 4 次方是多少？", 2401),
    ("甲單獨 3 小時完成一件工作，乙單獨 6 小時完成，兩人合作需要幾小時？", 2),
    ("三邊長為 5、12、13 的三角形面積是多少？", 30), ("2、3、5、7、11、13 這六個質數的總和是多少？", 41),
    ("1001 除以 7 的商是多少？", 143), ("時鐘在 3 點整時，時針與分針的夾角是幾度？", 90),
    ("二進位數 101101 等於十進位的多少？", 45),
]
QA_SUFFIX = "\n請簡短推理，最後一行只寫「最終答案：」加上阿拉伯數字。"


# ------------------------------------------------------------------------------------------------- HTTP helpers
def _req(path: str, body: dict) -> urllib.request.Request:
    h = {"Content-Type": "application/json"}
    if KEY:
        h["Authorization"] = f"Bearer {KEY}"
    return urllib.request.Request(URL + path, json.dumps(body).encode(), h)


def count_tokens(text: str) -> int:
    with urllib.request.urlopen(_req("/v1/messages/count_tokens",
                                     {"model": "strata", "messages": [{"role": "user", "content": text}]}), timeout=300) as r:
        return int(json.load(r)["input_tokens"])


def chat(content: str, *, max_tokens: int, effort: str = "none", budget: int | None = None, nonce: bool = True,
         drop_after: int | None = None, timeout: float = 3600) -> dict:
    """One streamed chat request: client TTFT, chunk times, usage and the engine's timings.  drop_after=N closes the
    connection after N streamed chunks (a client that goes away)."""
    msg = (f"[nonce {secrets.token_hex(8)}]\n" if nonce else "") + content
    body = {"model": "strata", "stream": True, "max_tokens": max_tokens, "temperature": 0, "reasoning_effort": effort,
            "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": msg}]}
    if budget is not None:
        body["reasoning_budget_tokens"] = budget
    # intervals on the monotonic clock; sent_wall only places the request in the monitor's (unix-time) windows
    out: dict = {"sent": time.monotonic(), "sent_wall": time.time(), "chunks": [], "content_first": None,
                 "reasoning_chunks": 0, "content": [], "status": None}
    try:
        with urllib.request.urlopen(_req("/v1/chat/completions", body), timeout=timeout) as r:
            out["status"] = r.status
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                ev = json.loads(line[6:])
                if ev.get("error"):           # a failure after the headers: an SSE event, then [DONE] (server.py)
                    out["error"] = json.dumps(ev["error"], ensure_ascii=False)[:400]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning_content"):
                        out["reasoning_chunks"] += 1
                        out["chunks"].append(time.monotonic())
                    if d.get("content"):
                        out["content"].append(d["content"])
                        out["chunks"].append(time.monotonic())
                        if out["content_first"] is None:
                            out["content_first"] = out["chunks"][-1]
                    if ch.get("finish_reason"):
                        out["finish"] = ch["finish_reason"]
                out["usage"] = ev.get("usage") or out.get("usage")
                out["timings"] = ev.get("timings") or out.get("timings")
                if drop_after is not None and len(out["chunks"]) >= drop_after:
                    out["dropped"] = True
                    break
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["error"] = e.read().decode(errors="replace")[:400]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    # HTTP/1.0 streams end by closing the connection, so a cut-off answer ends "cleanly": without a finish reason
    # and the usage block it is a failure, not a short success
    if out["status"] == 200 and not out.get("dropped") and not out.get("error") and \
            (not out.get("finish") or not out.get("usage")):
        out["error"] = "stream ended without finish_reason/usage (cut off)"
    out["done"] = time.monotonic()
    out["text"] = "".join(out.pop("content"))
    return out


def streamed_tokens(r: dict) -> int | None:
    """Tokens that reached the client: the server counts a stop token in completion_tokens but never sends it."""
    gen = (r.get("usage") or {}).get("completion_tokens")
    return None if gen is None else gen - (1 if r.get("finish") == "stop" else 0)


def brief(r: dict) -> dict:
    ch, u, t = r["chunks"], r.get("usage") or {}, r.get("timings") or {}
    gen = u.get("completion_tokens")
    n = streamed_tokens(r)
    rate = (n - 1) / (ch[-1] - ch[0]) if n and n > 1 and len(ch) > 1 and ch[-1] > ch[0] else None
    return {k: v for k, v in {
        "status": r["status"], "error": r.get("error"), "finish": r.get("finish"),
        "prompt_tokens": u.get("prompt_tokens"), "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "completion_tokens": gen, "reasoning_chunks": r["reasoning_chunks"] or None,
        "ttft_s": round(ch[0] - r["sent"], 2) if ch else None,
        "ttft_content_s": round(r["content_first"] - r["sent"], 2) if r.get("content_first") else None,
        "client_decode_tok_s": round(rate, 2) if rate else None,
        "total_s": round(r["done"] - r["sent"], 2),
        "engine_prompt_tok_s": t.get("prompt_per_second"), "engine_decode_tok_s": t.get("predicted_per_second"),
        "drafts": f"{t['draft_n_accepted']}/{t['draft_n']}" if t.get("draft_n") else None,
        "dropped": r.get("dropped")}.items() if v is not None}


def parallel(fns: list) -> list:
    res = [None] * len(fns)
    th = [threading.Thread(target=lambda i=i, f=f: res.__setitem__(i, f()), daemon=True) for i, f in enumerate(fns)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return res


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


# ------------------------------------------------------------------------------------------------- prompts
def docs_corpus() -> str:
    parts = [f"===== {p.name} =====\n" + p.read_text(errors="replace")
             for p in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]]
    return "\n\n".join(parts)


def sized(text: str, target: int, offset: int = 0) -> str:
    """A slice of `text` from `offset` plus the question, about `target` rendered prompt tokens (binary search on
    characters with the server's own count)."""
    body = (text[offset:] + text[:offset])
    lo, hi = 1000, len(body)
    best = body[:lo]
    while hi - lo > 400:
        mid = (lo + hi) // 2
        if count_tokens(body[:mid] + DOCS_QUESTION) <= target:
            lo, best = mid, body[:mid]
        else:
            hi = mid
    return best + DOCS_QUESTION


# ------------------------------------------------------------------------------------------------- scenarios
def s_replicate(a) -> dict:
    canon = {n: (COMMUNITY / f"canonical_{n}-v1.txt").read_text() for n in ("reasoning", "code")}
    text = docs_corpus()
    # The docs prompts take a different slice of the corpus in every run, so each run reads text no earlier run read
    # (the same text with only a new nonce reads warm pages on a --ple-io mmap config: the community report's method
    # measures that warm case).  The canonical prompts are fixed short texts, as in the report.
    step = len(text) // (a.runs + 1)
    cases = [("reasoning-v1", [canon["reasoning"]] * a.runs), ("code-v1", [canon["code"]] * a.runs),
             ("docs-4.4k", [sized(text, 4380, k * step) for k in range(a.runs)]),
             ("docs-32k", [sized(text, 32000, k * step + step // 2) for k in range(a.runs)])]
    chat("Say hello in five words.", max_tokens=16)            # one untimed warm-up, as the report did
    rows = {}
    for name, prompts in cases:
        runs = [brief(chat(prompt, max_tokens=256)) for prompt in prompts]
        rows[name] = {"runs": runs, "prompt_tokens": med([r.get("prompt_tokens") for r in runs]),
                      "engine_prompt_tok_s_median": med([r.get("engine_prompt_tok_s") for r in runs]),
                      "engine_decode_tok_s_median": med([r.get("engine_decode_tok_s") for r in runs]),
                      "ttft_s_median": med([r.get("ttft_s") for r in runs])}
        print(f"  {name}: prompt {rows[name]['prompt_tokens']} tok, prompt {rows[name]['engine_prompt_tok_s_median']} "
              f"tok/s, decode {rows[name]['engine_decode_tok_s_median']} tok/s, TTFT {rows[name]['ttft_s_median']} s",
              flush=True)
    return rows


def s_think4(a) -> dict:
    rounds = []
    for _ in range(a.runs):
        res = parallel([lambda q=q: chat(q, max_tokens=4096, effort="medium", budget=1024) for q in THINK4])
        rounds.append([brief(r) for r in res])
        b = rounds[-1]
        print("  per user client tok/s:", [r.get("client_decode_tok_s") for r in b], "total s:",
              [r.get("total_s") for r in b], "reasoning chunks:", [r.get("reasoning_chunks") for r in b], flush=True)
    return {"rounds": rounds}


def s_worst(a) -> dict:
    text = docs_corpus()
    step = len(text) // 4
    prompts = [sized(text, 27000, i * step) for i in range(4)]
    res = parallel([lambda p=p: chat(p, max_tokens=1024) for p in prompts])
    b = [brief(r) for r in res]
    ttfts = sorted(r["ttft_s"] for r in b if r.get("ttft_s") is not None)
    print("  TTFT first/last:", ttfts[:1], ttfts[-1:], "decode:", [r.get("client_decode_tok_s") for r in b], flush=True)
    return {"requests": b, "ttft_first_s": ttfts[0] if ttfts else None, "ttft_last_s": ttfts[-1] if ttfts else None}


def s_boundary(a) -> dict:
    prompt = sized(docs_corpus(), 28000)
    probe = chat(prompt, max_tokens=32768, nonce=False)       # refused before any engine work; says the prompt size
    m = re.search(r"prompt \((\d+) tokens\)", probe.get("error") or "")
    if not m:
        return {"error": "no 400 with the prompt size", "probe": brief(probe)}
    n = int(m.group(1))
    mr = re.search(r"at most (\d+) here", probe.get("error") or "")
    room = int(mr.group(1)) if mr else 32768 - 8 - n          # the server says the room itself (server.py)
    over = chat(prompt, max_tokens=room + 1, nonce=False)
    fits = chat(prompt, max_tokens=room, nonce=False, drop_after=1)   # accepted: read up to the first token, then go
    out = {"prompt_tokens": n, "room": room, "over_by_one_status": over["status"],
           "exact_fit_status": fits["status"], "exact_fit_first_token_s": brief(fits).get("ttft_s"),
           "pass": over["status"] == 400 and fits["status"] == 200 and bool(fits["chunks"])}
    print("  ", out, flush=True)
    return out


def s_disconnect(a) -> dict:
    essays = ["Write about 600 words on how a mechanical watch keeps time.",
              "Write about 600 words on the history of lighthouses.",
              "Write about 600 words on why bridges are built as arches.",
              "Write about 600 words on the life of a honey bee colony."]
    box: dict = {}

    def dropper():
        r = chat(essays[0], max_tokens=768, drop_after=30)
        box["fifth"] = chat("用一句話說明什麼是 GPU。", max_tokens=64)   # must get the freed slot now
        return r

    fns = [dropper] + [lambda e=e: chat(e, max_tokens=768) for e in essays[1:]]
    res = parallel(fns)
    after = box["fifth"]
    b = [brief(r) for r in res]
    first5 = after["chunks"][0] if after["chunks"] else None
    # a leaked slot makes the fifth request wait until an essay ends: then nobody is decoding at its first token
    still_decoding = sum(1 for r in res[1:] if first5 is not None and r["chunks"] and r["chunks"][-1] > first5)
    out = {"dropped": b[0], "others": b[1:], "fifth_after": brief(after),
           "others_decoding_at_fifth_first_token": still_decoding,
           "pass": all(r.get("status") == 200 and not r.get("error") and (r.get("completion_tokens") or 0) > 100
                       for r in b[1:]) and after["status"] == 200 and not after.get("error") and first5 is not None
                   and still_decoding >= 1}
    print("  others finished:", [r.get("completion_tokens") for r in b[1:]], "fifth TTFT:", out["fifth_after"].get("ttft_s"),
          "pass:", out["pass"], flush=True)
    return out


def qa_one(q: str, ans: int) -> dict:
    r = chat(q + QA_SUFFIX, max_tokens=2048, effort="low", budget=512)
    found = [x.replace(",", "") for x in re.findall(r"最[終终]答案[:：]\s*\**\s*(-?[\d,]+)", r["text"])]
    found = [x for x in found if x.lstrip("-").isdigit()]
    got = int(found[-1]) if found else None
    return {"q": q, "want": ans, "got": got, "ok": got == ans, "tail": r["text"][-160:], **{k: v for k, v in brief(r).items()
                                                                   if k in ("status", "error", "completion_tokens", "total_s")}}


def s_qa(a) -> dict:
    solo = [qa_one(q, x) for q, x in QA]
    conc = []
    for i in range(0, len(QA), 4):
        conc += parallel([lambda q=q, x=x: qa_one(q, x) for q, x in QA[i:i + 4]])
    out = {"solo_correct": sum(r["ok"] for r in solo), "concurrent_correct": sum(r["ok"] for r in conc),
           "n": len(QA), "solo": solo, "concurrent": conc}
    print(f"  solo {out['solo_correct']}/{len(QA)}, four at a time {out['concurrent_correct']}/{len(QA)}", flush=True)
    for s, c in zip(solo, conc):
        if not (s["ok"] and c["ok"]):
            print(f"    {s['q']} want {s['want']}: solo {s['got']}, concurrent {c['got']}", flush=True)
    return out


def s_soak(a) -> dict:
    text = docs_corpus()
    p4k, p8k = sized(text, 4000, 50000), sized(text, 8000, 150000)
    jobs = [("short", lambda rnd: chat("Write about 300 words on " + rnd.choice(
                ["rivers", "glaciers", "bread", "violins", "compilers", "satellites"]) + ".", max_tokens=rnd.choice([256, 512, 768]))),
            ("think", lambda rnd: chat(rnd.choice(THINK4), max_tokens=2048, effort="low", budget=512)),
            ("doc4k", lambda rnd: chat(p4k, max_tokens=256)),
            ("doc8k", lambda rnd: chat(p8k, max_tokens=256))]
    weights = [4, 2, 2, 1]
    end = time.time() + a.minutes * 60
    log, lock = [], threading.Lock()

    def worker(i):
        rnd = random.Random(a.seed * 100 + i)
        while time.time() < end:
            kind, fn = rnd.choices(jobs, weights)[0]
            r = brief(fn(rnd))
            with lock:
                log.append({"kind": kind, **r})

    parallel([lambda i=i: worker(i) for i in range(a.clients)])
    errs = [r for r in log if r.get("status") != 200 or r.get("error")]
    by = {}
    for r in log:
        by.setdefault(r["kind"], []).append(r)
    out = {"minutes": a.minutes, "clients": a.clients, "requests": len(log), "errors": len(errs),
           "error_samples": errs[:5],
           "by_kind": {k: {"n": len(v), "ttft_p50": med([r.get("ttft_s") for r in v]),
                           "ttft_max": max([r.get("ttft_s") or 0 for r in v]),
                           "decode_tok_s_p50": med([r.get("client_decode_tok_s") for r in v]),
                           "total_s_p50": med([r.get("total_s") for r in v])} for k, v in by.items()},
           "generated_tokens": sum(r.get("completion_tokens") or 0 for r in log)}
    print(f"  {out['requests']} requests, {out['errors']} errors, {out['generated_tokens']} tokens", flush=True)
    return out


def converse(history: list, user: str, max_tokens: int) -> dict:
    """One turn of a growing conversation (no nonce: the earlier turns are the conversation cache's to reuse)."""
    body = {"model": "strata", "stream": True, "max_tokens": max_tokens, "temperature": 0, "reasoning_effort": "none",
            "stream_options": {"include_usage": True}, "messages": history + [{"role": "user", "content": user}]}
    out: dict = {"sent": time.monotonic(), "chunks": [], "content_first": None, "reasoning_chunks": 0, "content": [],
                 "status": None}
    try:
        with urllib.request.urlopen(_req("/v1/chat/completions", body), timeout=3600) as r:
            out["status"] = r.status
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                ev = json.loads(line[6:])
                if ev.get("error"):
                    out["error"] = json.dumps(ev["error"], ensure_ascii=False)[:400]
                for ch in ev.get("choices") or []:
                    if (ch.get("delta") or {}).get("content"):
                        out["content"].append(ch["delta"]["content"])
                        out["chunks"].append(time.monotonic())
                        out["content_first"] = out["content_first"] or out["chunks"][-1]
                    if ch.get("finish_reason"):
                        out["finish"] = ch["finish_reason"]
                out["usage"] = ev.get("usage") or out.get("usage")
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["error"] = e.read().decode(errors="replace")[:400]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    if out["status"] == 200 and not out.get("error") and (not out.get("finish") or not out.get("usage")):
        out["error"] = "stream ended without finish_reason/usage (cut off)"
    out["done"] = time.monotonic()
    out["text"] = "".join(out.pop("content"))
    return out


def s_multiturn(a) -> dict:
    """#1994's shape: four conversations of five growing turns while two more clients send short requests, so six
    clients compete for four slots.  Every turn must succeed and later turns should reuse the cached conversation."""
    text = docs_corpus()
    step = len(text) // 4
    stop = time.time() + 3600
    extra_log: list = []

    def conversation(i):
        hist, turns = [], []
        tag = f"[conversation {secrets.token_hex(6)}]\n"     # its own prefix: no cache from other scenarios
        for t in range(5):
            part = text[i * step + t * 9000: i * step + (t + 1) * 9000]
            user = (tag if t == 0 else "") + part + "\n\nSummarise the new text above in three sentences."
            r = converse(hist, user, 192)
            b = brief(r)
            turns.append(b)
            if r.get("error") or r["status"] != 200:
                hist = hist + [{"role": "user", "content": user}]   # keep going: does the conversation recover?
                continue
            hist = hist + [{"role": "user", "content": user}, {"role": "assistant", "content": r["text"]}]
        return turns

    def extra():
        while time.time() < stop:
            extra_log.append(brief(chat("Write two sentences about " + random.choice(["rain", "maps", "tea"]) + ".",
                                        max_tokens=64)))

    th = [threading.Thread(target=extra, daemon=True) for _ in range(2)]
    for t in th:
        t.start()
    convs = parallel([lambda i=i: conversation(i) for i in range(4)])
    stop = time.time()
    for t in th:
        t.join()
    turns = [t for c in convs for t in c]
    bad = [t for t in turns + extra_log if t.get("status") != 200 or t.get("error")]
    out = {"turns": len(turns), "extra_requests": len(extra_log), "failures": len(bad), "failure_samples": bad[:5],
           "cached_tokens_by_turn": [[t.get("cached") for t in c] for c in convs],
           "prompt_tokens_by_turn": [[t.get("prompt_tokens") for t in c] for c in convs],
           "ttft_by_turn": [[t.get("ttft_s") for t in c] for c in convs]}
    print(f"  {out['turns']} turns + {out['extra_requests']} extra requests, {out['failures']} failures; cached by turn:",
          out["cached_tokens_by_turn"], flush=True)
    return out


def s_budget(a) -> dict:
    """The thinking budget path: a problem that thinks long, with budgets small enough to be reached.  The server
    closes the thinking at the budget (a wrap-up, not an exact cut) and the answer must still come."""
    q = ("一個 5x5 的格子，從左上走到右下，只能向右或向下，且不能經過中心格。共有幾條路徑？"
         "請詳細推理並驗算，最後一行只寫「最終答案：」加上阿拉伯數字。")
    rows = []
    for budget in (128, 512):
        r = chat(q, max_tokens=3072, effort="high", budget=budget)
        b = brief(r)
        found = re.findall(r"最[終终]答案[:：]\s*\**\s*(\d+)", r["text"])
        rows.append({"budget": budget, **b, "answer": int(found[-1]) if found else None, "want": 34,
                     "has_answer_text": bool(r["text"].strip())})
        print(f"  budget {budget}: reasoning chunks {b.get('reasoning_chunks')}, answer {rows[-1]['answer']} "
              f"(want 34), finish {b.get('finish')}, TTFT content {b.get('ttft_content_s')} s", flush=True)
    return {"rows": rows, "pass": all(x["has_answer_text"] and x.get("status") == 200 and not x.get("error") for x in rows)}


SCENARIOS = {"budget": s_budget, "replicate": s_replicate, "think4": s_think4, "worst": s_worst, "boundary": s_boundary,
             "disconnect": s_disconnect, "qa": s_qa, "soak": s_soak, "multiturn": s_multiturn}


def main() -> int:
    global URL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="+", choices=list(SCENARIOS))
    ap.add_argument("--out", required=True, help="append JSON lines here")
    ap.add_argument("--tag", default="", help="the configuration under test (baseline, aggressive, ...)")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=20, help="soak duration")
    ap.add_argument("--clients", type=int, default=4, help="soak clients")
    ap.add_argument("--seed", type=int, default=20261016, help="soak job mix")
    a = ap.parse_args()
    URL = a.url.rstrip("/")
    for name in a.scenarios:
        print(f"[{time.strftime('%H:%M:%S')}] {name} ({a.tag})", flush=True)
        t0 = time.time()
        res = SCENARIOS[name](a)
        rec = {"tag": a.tag, "scenario": name, "start": round(t0, 1), "end": round(time.time(), 1), "result": res}
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
