#!/usr/bin/env python3
"""The engine-arm screen: one fixed set of requests, sent the same way to every arm of an engine experiment
(bench/ab_engine.sh), so that their speed and their answers can be compared request by request.  Standard library only.

Every prompt starts with a fixed nonce (the seed, the phase and the slice offset): nothing is reused inside a run, and
every arm reads the same text, cut by the server's own token count (stress_matrix.sized, from ~/dev/strata's docs).
Answers are greedy (temperature 0, reasoning off), so an arm that keeps the arithmetic answers byte for byte as the
baseline does.  The phases, in this order, with nothing else in flight:
  prefill  fresh prompts of about 1.2K, 4K and 16K tokens, --n of each, 32 tokens out: the engine's read rate
  decode   a ~1.5K-token slice to translate, 512 tokens out, --n times: one user
  four     four users at once, a ~1.5K-token slice each, 512 tokens out, twice: the total rate (the four prompts'
           reading included, so it moves with the prefill rate too)
  admit    as the deployment record's admission test (bench/four_user_test.py admit): three users decoding essays of
           up to 2,048 tokens from short prompts, and a 16K prompt joining 20 s later, twice: the joiner's TTFT, the
           decoders' longest gap and their tokens while it is read, and the makespan of the four.  A round is valid
           only if every decoder had its first token before the joiner was sent and was still decoding when the
           joiner got its first token (a decoder still reading its prompt would make the joiner queue behind it)
  turns    a two-turn conversation, twice: the second turn continues the first through the conversation cache, so a
           change to the state the cache keeps shows in its answer
Opt-in phases (--phases, or ENGINE_SCREEN_PHASES for a run of bench/ab_engine.sh), for what the default ones do not
show apart:
  decode4  four users decoding at once from short Traditional Chinese prompts, 1,024 tokens each, twice: the slots'
           steady decode, with no long prompt read in the measured time (the summed engine decode rate)
  eval4    four users in the evaluation's mode at once: thinking on (effort high), temperature 1.0, top_p 0.95,
           top_k 20, a fixed seed per request, 1,536 tokens each, once: sampled decoding of reasoning text, which
           keeps drafts at another rate than greedy decoding (78% of the MTP proposals in m1-1's eval4, 54-55% in
           its decode4)
  admit28  admit with a 28K-token joiner: a longer read beside the decoders
Requests "sent at once" (four users, the admission's decoders, decode4, eval4) go out STAGGER seconds apart.  Sent
in the same instant, the admission's decoders got their first tokens in an order that varied from run to run, and in
the two runs of 2026-10-19 where the third was first (c1-3, c0-4), two of them answered otherwise; staggered, the
order was the same in every round of m0-1 and m1-1.  --texts PATH also
writes every answer's text there, to find where two arms' answers part.
One JSON line per request (per admission, per conversation) to --json, each with the tag, the phase, the answer's
sha256, the corpus's sha256 (the prompts are cut from it) and the phases of the screen.  The last line of the
output is "engine_screen: requests sent: N" (bench/ab_engine.sh counts against it).  Exit status 1 when a request
failed (the lines still say which).
Usage: bench/engine_screen.py --tag T --json OUT [--n 3] [--seed engine-screen-1]
       bench/engine_screen.py --warm-up      one fixed ~2K-token prompt (the prefill path, not the short read), 8 out
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402

SIZES = (1200, 4000, 16000)
TRANSLATE = "Translate the following text into Traditional Chinese. Output only the translation.\n\n"
# bench/four_user_test.py's admission decoders
ESSAY = "Write a clear, well organised essay of about 900 words on {}. Use plain prose, no lists."
TOPICS = ["how a mechanical watch keeps time", "the history of lighthouses and their lenses",
          "why bridges are built as arches", "the life of a honey bee colony through one year"]
DEFAULT_PHASES = "prefill,decode,four,admit,turns"
ZH_ESSAY = "請用繁體中文寫一篇約一千字的文章，主題是{}。只寫文章本身，不要列點，不要標題。"
ZH_TOPICS = ["機械錶如何計時", "燈塔與它們的透鏡的歷史", "為什麼橋常建成拱形", "一年之中蜂群的生活",
             "台灣的梅雨季", "活字印刷如何改變了書", "城市裡的行道樹", "冰箱是怎麼保持低溫的"]
# the evaluation's thinking-mode sampling (~/dev/p100-q3-eval/driver2.py), every field sent
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
            "frequency_penalty": 0.0, "repetition_penalty": 1.0}
# four questions of the evaluation's kinds, written for this screen (not from a benchmark) and hard, so that the
# thinking reaches the 1536-token cap and the four requests decode the same amount (screen 2026-10-19b's questions
# ended after 98 to 874 tokens).  Answers: 2177; C (0.2934); B (2040); 884.
EVAL_QUESTIONS = [
    "How many permutations (p1, p2, ..., p10) of the numbers 1 to 10 satisfy |p_i - i| <= 2 for every i? Think it "
    "through, then end with a line 'Answer: <number>'.",
    "A fair six-sided die is rolled repeatedly and the results are added up until the total first exceeds 10. What is "
    "the probability that the final total is exactly 11, to four decimal places?\n\nA) 0.2419\nB) 0.2857\nC) 0.2934\n"
    "D) 0.3333\n\nThe last line of your answer must be 'Answer: X' with X one of A, B, C, D.",
    "請回答以下單選題，最後一行只寫「答案：X」，X 為 A、B、C、D 其中之一。請先逐步思考再作答。\n\n把一個 5×5 方格的每一格塗成"
    "黑色或白色，使每一列與每一行都恰好有兩格黑色。共有幾種塗法？\n\nA) 1800 種\nB) 2040 種\nC) 2160 種\nD) 2520 種",
    "求方程 x + 2y + 3z = 100 的非負整數解 (x, y, z) 共有幾組。請逐步推理，最後一行只寫「最終答案：」加上數字。"]
SENT = [0]          # requests this screen sent
SCREEN = "2026-10-19c"                  # the screen's version (eval4's questions); the summary compares only one
STAGGER = 0.3
TEXTS: list = []    # (label, text) of every answer when --texts is given
FOLLOW_UP = "Now list, in one sentence each, the three most important points of your answer."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def offset(seed: str, phase: str, i: int, n: int) -> int:
    """A fixed slice offset per (seed, phase, i): the same text for every arm, different text for every request."""
    return int(hashlib.sha256(f"{seed}|{phase}|{i}".encode()).hexdigest(), 16) % n


def ask(seed: str, label: str, body: str, max_tokens: int, delay: float = 0.0) -> dict:
    if delay:
        time.sleep(delay)
    SENT[0] += 1
    r = SM.chat(f"[nonce {seed}-{label}]\n" + body, max_tokens=max_tokens, nonce=False)
    TEXTS.append((label, r.get("text", "")))
    return {**SM.brief(r), "text_sha": sha(r.get("text", "")), "chunks": r["chunks"], "sent": r["sent"],
            "done": r["done"]}


def ask_sampled(seed: str, label: str, body: str, max_tokens: int, draw_seed: int, effort: str = "high",
                delay: float = 0.0) -> dict:
    """One streamed request in the evaluation's mode (stress_matrix.chat is greedy only): the same record."""
    if delay:
        time.sleep(delay)
    SENT[0] += 1
    req = {"model": "strata", "stream": True, "max_tokens": max_tokens, "reasoning_effort": effort,
           "reasoning_budget_tokens": 0, "seed": draw_seed, "stream_options": {"include_usage": True},
           "messages": [{"role": "user", "content": f"[nonce {seed}-{label}]\n" + body}], **SAMPLING}
    out: dict = {"sent": time.monotonic(), "chunks": [], "content_first": None, "reasoning_chunks": 0, "status": None}
    text = []
    try:
        with SM.urllib.request.urlopen(SM._req("/v1/chat/completions", req), timeout=3600) as r:
            out["status"] = r.status
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                ev = json.loads(line[6:])
                if ev.get("error"):
                    out["error"] = json.dumps(ev["error"], ensure_ascii=False)[:400]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    for k in ("reasoning_content", "content"):
                        if d.get(k):
                            out["chunks"].append(time.monotonic())
                            text.append(d[k])
                            if k == "reasoning_content":
                                out["reasoning_chunks"] += 1
                            elif out["content_first"] is None:
                                out["content_first"] = out["chunks"][-1]
                    if ch.get("finish_reason"):
                        out["finish"] = ch["finish_reason"]
                out["usage"] = ev.get("usage") or out.get("usage")
                out["timings"] = ev.get("timings") or out.get("timings")
    except Exception as e:                                   # noqa: BLE001 - recorded, the record fails
        out["error"] = f"{type(e).__name__}: {e}"[:400]
    if out["status"] == 200 and not out.get("error") and (not out.get("finish") or not out.get("usage")):
        out["error"] = "stream ended without finish_reason/usage (cut off)"
    out["done"] = time.monotonic()
    out["text"] = "".join(text)
    TEXTS.append((label, out["text"]))
    return {**SM.brief(out), "text_sha": sha(out["text"]), "chunks": out["chunks"], "sent": out["sent"],
            "done": out["done"]}


def lean(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("chunks", "sent", "done")}


def phase_prefill(seed, text, n, out):
    for size in SIZES:
        for i in range(n):
            off = offset(seed, f"prefill{size}", i, len(text))
            r = ask(seed, f"p{size}-{off}", SM.sized(text, size, off), 32)
            out({"phase": "prefill", "size": size, "i": i, **lean(r)})


def phase_decode(seed, text, n, out):
    for i in range(n):
        off = offset(seed, "decode", i, len(text))
        r = ask(seed, f"d-{off}", TRANSLATE + SM.sized(text, 1500, off), 512)
        out({"phase": "decode", "i": i, **lean(r)})


def phase_four(seed, text, out):
    for rd in range(2):
        bodies = []
        for j in range(4):
            off = offset(seed, f"four{rd}", j, len(text))
            bodies.append((f"f{rd}{j}-{off}", TRANSLATE + SM.sized(text, 1500, off)))
        t0 = time.monotonic()
        rs = SM.parallel([lambda b=b, j=j: ask(seed, b[0], b[1], 512, STAGGER * j) for j, b in enumerate(bodies)])
        wall = time.monotonic() - t0
        tokens = sum(SM.streamed_tokens({"usage": {"completion_tokens": r.get("completion_tokens")},
                                         "finish": r.get("finish")}) or 0 for r in rs)
        out({"phase": "four", "round": rd, "wall_s": round(wall, 2), "total_tok_s": round(tokens / wall, 2),
             "requests": [lean(r) for r in rs], "error": next((r.get("error") for r in rs if r.get("error")), None)})


def phase_concurrent(name, rounds, fns, out):
    """Requests sent at once per round; the summed engine decode rate (they decode side by side) and the tokens."""
    for rd in range(rounds):
        t0 = time.monotonic()
        rs = SM.parallel(fns(rd))
        wall = time.monotonic() - t0
        rates = [r.get("engine_decode_tok_s") for r in rs]
        out({"phase": name, "round": rd, "wall_s": round(wall, 2),
             "sum_engine_decode_tok_s": round(sum(rates), 2) if all(isinstance(x, (int, float)) for x in rates) else None,
             "tokens": sum(r.get("completion_tokens") or 0 for r in rs), "requests": [lean(r) for r in rs],
             "error": next((r.get("error") for r in rs if r.get("error")), None)})


def phase_decode4(seed, out):
    phase_concurrent("decode4", 2, lambda rd: [lambda j=j: ask(seed, f"z{rd}{j}", ZH_ESSAY.format(ZH_TOPICS[4 * rd + j]), 1024,
                                                               STAGGER * j) for j in range(4)], out)


def phase_eval4(seed, out):
    phase_concurrent("eval4", 1, lambda rd: [lambda j=j: ask_sampled(seed, f"e{rd}{j}", EVAL_QUESTIONS[j], 1536, 1000 * j + 7,
                                                                     delay=STAGGER * j) for j in range(4)], out)


def phase_admit(seed, text, out, delay=20.0, size=16000, name="admit"):
    for rd in range(2):
        dec = [(f"a{rd}{j}", ESSAY.format(TOPICS[(rd + j) % len(TOPICS)])) for j in range(3)]
        joff = offset(seed, f"join{rd}" if size == 16000 else f"join{size}-{rd}", 0, len(text))
        joiner = (f"j{rd}-{joff}" if size == 16000 else f"j{size}-{rd}-{joff}", SM.sized(text, size, joff))
        res: dict = {}
        t0 = time.monotonic()
        threads = [threading.Thread(target=lambda b=b, j=j: res.__setitem__(j, ask(seed, b[0], b[1], 2048, STAGGER * j)),
                                    daemon=True) for j, b in enumerate(dec)]
        for t in threads:
            t.start()
        time.sleep(delay)
        jr = ask(seed, joiner[0], joiner[1], 32)
        for t in threads:
            t.join()
        rs = [res.get(j) or {"error": "no result", "chunks": [], "sent": t0, "done": t0} for j in range(3)]
        start = jr["sent"]
        end = (jr["chunks"][0] if jr["chunks"] else jr["done"]) + 1.0
        first = jr["chunks"][0] if jr["chunks"] else jr["done"]
        gaps, during = [], 0
        for r in rs:                  # the longest time a decoder received nothing while the joiner was read
            ts = [start] + [c for c in r["chunks"] if start < c < end] + [max(start, min(end, r["done"]))]
            gaps.append(max((b - a for a, b in zip(ts, ts[1:])), default=0.0))
            during += sum(1 for c in r["chunks"] if start < c < end)
        # the design: every decoder decoding before the joiner is sent, and still decoding when it gets its first token
        began = [round(r["chunks"][0] - start, 2) if r["chunks"] else None for r in rs]
        ended = [round(r["chunks"][-1] - first, 2) if r["chunks"] else None for r in rs]
        why = ("a decoder had no token before the joiner was sent" if any(b is None or b >= 0 for b in began) else
               "a decoder stopped before the joiner's first token" if any(e is None or e < 0 for e in ended) else None)
        out({"phase": name, "round": rd, "joiner": lean(jr), "decoders": [lean(r) for r in rs],
             "joiner_ttft_s": jr.get("ttft_s"), "longest_gap_s": round(max(gaps), 2), "decoder_chunks_during": during,
             "makespan_s": round(max([jr["done"]] + [r["done"] for r in rs]) - t0, 2),
             "decoders_first_token_vs_join_s": began, "decoders_last_token_vs_joiner_first_s": ended,
             "valid": why is None, "invalid_reason": why,
             "error": next((r.get("error") for r in [jr] + rs if r.get("error")), None)})


def phase_turns(seed, text, out):
    global TEXTS
    for i in range(2):
        off = offset(seed, "turns", i, len(text))
        first = f"[nonce {seed}-t{i}-{off}]\n" + SM.sized(text, 2000, off)
        h = [{"role": "user", "content": first}]
        SENT[0] += 2
        a = SM.converse([], first, 128)
        a_text = a.get("text", "")
        b = SM.converse(h + [{"role": "assistant", "content": a_text}], FOLLOW_UP, 128)
        TEXTS += [(f"t{i}-first", a_text), (f"t{i}-second", b.get("text", ""))]
        out({"phase": "turns", "i": i, "first_sha": sha(a_text), "second_sha": sha(b.get("text", "")),
             "second_cached": ((b.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens"),
             "second_prompt_tokens": (b.get("usage") or {}).get("prompt_tokens"),
             "error": a.get("error") or b.get("error")})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--warm-up", action="store_true", help="read one fixed ~2K-token prompt and exit")
    ap.add_argument("--url", default=SM.URL, help="the server (default %(default)s)")
    ap.add_argument("--corpus", help="the text the prompts are cut from (default: ~/dev/strata's docs)")
    ap.add_argument("--tag")
    ap.add_argument("--json")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--seed", default="engine-screen-1")
    ap.add_argument("--phases", default=os.environ.get("ENGINE_SCREEN_PHASES") or DEFAULT_PHASES)
    ap.add_argument("--admit-delay", type=float, default=20.0, help="seconds between the decoders and the joiner")
    ap.add_argument("--texts", help="also write every answer's text here (one JSON line per answer)")
    a = ap.parse_args(argv)
    SENT[0] = 0
    TEXTS.clear()
    SM.URL = a.url.rstrip("/")
    text = Path(a.corpus).read_text() if a.corpus else SM.docs_corpus()
    corpus_sha = sha(text)
    if a.warm_up:
        r = SM.chat("[nonce engine-screen-warm-up]\n" + SM.sized(text, 2000, 0), max_tokens=8, nonce=False)
        print(json.dumps({"corpus_sha": corpus_sha, **SM.brief(r)}, ensure_ascii=False))
        return 1 if r.get("error") or r.get("status") != 200 else 0
    if not a.tag or not a.json:
        ap.error("--tag and --json are needed (or --warm-up)")
    failed = []

    def out(rec):
        rec = {"tag": a.tag, "seed": a.seed, "corpus_sha": corpus_sha, "phases": a.phases, "screen": SCREEN,
               "at": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
        if rec.get("error"):
            failed.append(rec["phase"])
        with open(a.json, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps({k: v for k, v in rec.items() if k not in ("requests", "decoders", "joiner")},
                         ensure_ascii=False)[:300], flush=True)

    phases = {"prefill": lambda: phase_prefill(a.seed, text, a.n, out), "decode": lambda: phase_decode(a.seed, text, a.n, out),
              "four": lambda: phase_four(a.seed, text, out), "admit": lambda: phase_admit(a.seed, text, out, a.admit_delay),
              "turns": lambda: phase_turns(a.seed, text, out), "decode4": lambda: phase_decode4(a.seed, out),
              "eval4": lambda: phase_eval4(a.seed, out),
              "admit28": lambda: phase_admit(a.seed, text, out, a.admit_delay, 28000, "admit28")}
    unknown = [ph for ph in a.phases.split(",") if ph not in phases]
    if unknown:
        ap.error(f"unknown phases: {unknown}")
    for ph in a.phases.split(","):
        phases[ph]()
    if a.texts:
        with open(a.texts, "w", encoding="utf-8") as f:
            for lb, tx in TEXTS:
                f.write(json.dumps({"label": lb, "sha": sha(tx), "text": tx}, ensure_ascii=False) + "\n")
    print(f"engine_screen: requests sent: {SENT[0]}", flush=True)
    if failed:
        print(f"engine_screen: {len(failed)} failed records ({', '.join(sorted(set(failed)))})", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
