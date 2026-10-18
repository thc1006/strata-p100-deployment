#!/usr/bin/env python3
"""The engine-arm screen: one fixed set of requests, sent the same way to every arm of an engine experiment
(bench/ab_engine.sh), so that their speed and their answers can be compared request by request.  Standard library only.

Every prompt starts with a fixed nonce (the seed, the phase and the slice offset): nothing is reused inside a run, and
every arm reads the same text, cut by the server's own token count (stress_matrix.sized, from ~/dev/strata's docs).
Answers are greedy (temperature 0, reasoning off), so an arm that keeps the arithmetic answers byte for byte as the
baseline does.  The phases, in this order, with nothing else in flight:
  prefill  fresh prompts of about 1.2K, 4K and 16K tokens, --n of each, 32 tokens out: the engine's read rate
  decode   a ~1.5K-token slice to translate, 512 tokens out, --n times: one user
  four     four users at once, a ~1.5K-token slice each, 512 tokens out, twice: the total rate
  admit    three users decoding up to 1,024 tokens and a 16K prompt joining 20 s later, twice: the joiner's TTFT, the
           decoders' longest gap and their tokens while it is read, and the makespan of the four
  turns    a two-turn conversation, twice: the second turn continues the first through the conversation cache, so a
           change to the state the cache keeps shows in its answer
One JSON line per request (per admission, per conversation) to --json, each with the tag, the phase and the answer's
sha256.  Exit status 1 when a request failed (the lines still say which).
Usage: bench/engine_screen.py --tag T --json OUT [--n 3] [--seed engine-screen-1]
       bench/engine_screen.py --warm-up      one fixed ~2K-token prompt (the prefill path, not the short read), 8 out
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402

SIZES = (1200, 4000, 16000)
TRANSLATE = "Translate the following text into Traditional Chinese. Output only the translation.\n\n"
FOLLOW_UP = "Now list, in one sentence each, the three most important points of your answer."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def offset(seed: str, phase: str, i: int, n: int) -> int:
    """A fixed slice offset per (seed, phase, i): the same text for every arm, different text for every request."""
    return int(hashlib.sha256(f"{seed}|{phase}|{i}".encode()).hexdigest(), 16) % n


def ask(seed: str, label: str, body: str, max_tokens: int) -> dict:
    r = SM.chat(f"[nonce {seed}-{label}]\n" + body, max_tokens=max_tokens, nonce=False)
    return {**SM.brief(r), "text_sha": sha(r.get("text", "")), "chunks": r["chunks"], "sent": r["sent"],
            "done": r["done"]}


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
        rs = SM.parallel([lambda b=b: ask(seed, b[0], b[1], 512) for b in bodies])
        wall = time.monotonic() - t0
        tokens = sum(SM.streamed_tokens({"usage": {"completion_tokens": r.get("completion_tokens")},
                                         "finish": r.get("finish")}) or 0 for r in rs)
        out({"phase": "four", "round": rd, "wall_s": round(wall, 2), "total_tok_s": round(tokens / wall, 2),
             "requests": [lean(r) for r in rs], "error": next((r.get("error") for r in rs if r.get("error")), None)})


def phase_admit(seed, text, out, delay=20.0):
    for rd in range(2):
        dec = []
        for j in range(3):
            off = offset(seed, f"admit{rd}", j, len(text))
            dec.append((f"a{rd}{j}-{off}", TRANSLATE + SM.sized(text, 1500, off)))
        joff = offset(seed, f"join{rd}", 0, len(text))
        joiner = (f"j{rd}-{joff}", SM.sized(text, 16000, joff))
        res: dict = {}
        t0 = time.monotonic()
        threads = [threading.Thread(target=lambda b=b, j=j: res.__setitem__(j, ask(seed, b[0], b[1], 1024)), daemon=True)
                   for j, b in enumerate(dec)]
        for t in threads:
            t.start()
        time.sleep(delay)
        jr = ask(seed, joiner[0], joiner[1], 32)
        for t in threads:
            t.join()
        rs = [res.get(j) or {"error": "no result", "chunks": [], "sent": t0, "done": t0} for j in range(3)]
        start = jr["sent"]
        end = (jr["chunks"][0] if jr["chunks"] else jr["done"]) + 1.0
        gaps, during = [], 0
        for r in rs:                  # the longest time a decoder received nothing while the joiner was read
            ts = [start] + [c for c in r["chunks"] if start < c < end] + [max(start, min(end, r["done"]))]
            gaps.append(max((b - a for a, b in zip(ts, ts[1:])), default=0.0))
            during += sum(1 for c in r["chunks"] if start < c < end)
        out({"phase": "admit", "round": rd, "joiner": lean(jr), "decoders": [lean(r) for r in rs],
             "joiner_ttft_s": jr.get("ttft_s"), "longest_gap_s": round(max(gaps), 2), "decoder_chunks_during": during,
             "makespan_s": round(max([jr["done"]] + [r["done"] for r in rs]) - t0, 2),
             "error": next((r.get("error") for r in [jr] + rs if r.get("error")), None)})


def phase_turns(seed, text, out):
    for i in range(2):
        off = offset(seed, "turns", i, len(text))
        first = f"[nonce {seed}-t{i}-{off}]\n" + SM.sized(text, 2000, off)
        h = [{"role": "user", "content": first}]
        a = SM.converse([], first, 128)
        a_text = a.get("text", "")
        b = SM.converse(h + [{"role": "assistant", "content": a_text}], FOLLOW_UP, 128)
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
    ap.add_argument("--phases", default="prefill,decode,four,admit,turns")
    ap.add_argument("--admit-delay", type=float, default=20.0, help="seconds between the decoders and the joiner")
    a = ap.parse_args(argv)
    SM.URL = a.url.rstrip("/")
    text = Path(a.corpus).read_text() if a.corpus else SM.docs_corpus()
    if a.warm_up:
        r = SM.chat("[nonce engine-screen-warm-up]\n" + SM.sized(text, 2000, 0), max_tokens=8, nonce=False)
        print(json.dumps(SM.brief(r), ensure_ascii=False))
        return 1 if r.get("error") or r.get("status") != 200 else 0
    if not a.tag or not a.json:
        ap.error("--tag and --json are needed (or --warm-up)")
    failed = []

    def out(rec):
        rec = {"tag": a.tag, "seed": a.seed, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
        if rec.get("error"):
            failed.append(rec["phase"])
        with open(a.json, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps({k: v for k, v in rec.items() if k not in ("requests", "decoders", "joiner")},
                         ensure_ascii=False)[:300], flush=True)

    for ph in a.phases.split(","):
        {"prefill": lambda: phase_prefill(a.seed, text, a.n, out), "decode": lambda: phase_decode(a.seed, text, a.n, out),
         "four": lambda: phase_four(a.seed, text, out), "admit": lambda: phase_admit(a.seed, text, out, a.admit_delay),
         "turns": lambda: phase_turns(a.seed, text, out)}[ph]()
    if failed:
        print(f"engine_screen: {len(failed)} failed records ({', '.join(sorted(set(failed)))})", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
