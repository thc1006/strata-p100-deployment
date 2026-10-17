#!/usr/bin/env python3
"""Does a multi-turn conversation reuse its cached history?  (standard library only)

  M1  one conversation alone, --turns turns
  M2  --conversations conversations at once, --turns turns each, nothing else running
Each turn appends ~2K tokens of new text and the model's previous answer; the server's usage reports how many prompt
tokens came from the cache (prompt_tokens_details.cached_tokens).  The engine log line "prompt N tokens = R reused +
X read" says the same from the engine's side.

  python3 bench/conversation_cache_probe.py M1 --turns 4
  python3 bench/conversation_cache_probe.py M2 --conversations 4 --turns 4
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402


def conversation(i: int, turns: int, text: str, step: int) -> list[dict]:
    hist, rows = [], []
    tag = f"[conversation {secrets.token_hex(6)}]\n"
    for t in range(turns):
        part = text[i * step + t * 9000: i * step + (t + 1) * 9000]
        user = (tag if t == 0 else "") + part + "\n\nSummarise the new text above in three sentences."
        r = SM.converse(hist, user, 160)
        b = SM.brief(r)
        rows.append({"turn": t + 1, "prompt_tokens": b.get("prompt_tokens"), "cached": b.get("cached"),
                     "ttft_s": b.get("ttft_s"), "error": b.get("error")})
        hist = hist + [{"role": "user", "content": user}, {"role": "assistant", "content": r["text"]}]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("M1", "M2"))
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--conversations", type=int, default=4)
    ap.add_argument("--json", help="append the result here")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    text = SM.docs_corpus()
    n = 1 if a.mode == "M1" else a.conversations
    step = len(text) // (n + 1)                       # conversation i reads text[i*step + t*9000 ...]: inside the corpus
    assert n * step + a.turns * 9000 <= len(text), "the corpus is too short for this many conversations and turns"
    t0 = time.time()
    convs = SM.parallel([lambda i=i: conversation(i, a.turns, text, step) for i in range(n)])
    out = {"mode": a.mode, "tag": a.tag, "conversations": convs, "wall_s": round(time.time() - t0, 1)}
    for i, c in enumerate(convs):
        print(f"  conversation {i}: " + "  ".join(f"t{r['turn']} {r['prompt_tokens']}tok cached {r['cached']} "
                                                  f"TTFT {r['ttft_s']}s" for r in c), flush=True)
    if a.json:
        with open(a.json, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
