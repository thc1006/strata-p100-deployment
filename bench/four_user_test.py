#!/usr/bin/env python3
"""Four-user scenarios that tools/strata_bench.py and tools/serve_load.py do not cover (standard library only).

  burst    N clients send long prompts (--tokens each, e.g. 8192) at the same moment: TTFT of the first and of the
           last admission, per-request decode rate, wall time.  Admissions are read one at a time, so the last
           client's TTFT is the number that decides how a lab of four feels.
  admit    --decoders clients decode long answers; after --delay s one more client sends a long prompt.  Reports the
           decoders' stream rate before / during that admission (the window ends at the newcomer's first token or at
           a decoder's own last token, whichever is first), and the newcomer's TTFT.

Every prompt starts with its own nonce and a different slice of the corpus (Strata's own docs), each slice sized to
--tokens with the server's /v1/messages/count_tokens, so no prompt-cache reuse.  Thinking is off (reasoning_effort
none) so the runs are comparable.  The streaming client is bench/stress_matrix.py's chat(): a stream that
reports an error event or ends without its finish reason and usage counts as a failure.

  python3 bench/four_user_test.py burst --clients 4 --tokens 8192
  python3 bench/four_user_test.py admit --decoders 3 --tokens 16384 --delay 25
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_matrix as SM  # noqa: E402

ESSAY = "Write a clear, well organised essay of about 900 words on {}. Use plain prose, no lists."
TOPICS = ["how a mechanical watch keeps time", "the history of lighthouses and their lenses",
          "why bridges are built as arches", "the life of a honey bee colony through one year"]


def summary(r: dict, t0: float) -> dict:
    b = SM.brief(r)
    return {**{k: b.get(k) for k in ("prompt_tokens", "cached", "completion_tokens", "ttft_s", "total_s", "error",
                                     "finish") if b.get(k) is not None},
            "decode_tok_s": b.get("client_decode_tok_s"), "sent_at_s": round(r["sent"] - t0, 2)}


def rate_in(chunks: list[float], a: float, b: float, inclusive: bool = False) -> float | None:
    n = sum(1 for t in chunks if a <= t < b or (inclusive and t == b))
    return round(n / (b - a), 2) if b > a else None


def run_threads(jobs: list[tuple]) -> list[dict]:
    """Each job (fn, delay_s) starts after its delay on its own thread."""
    outs: list = [None] * len(jobs)

    def go(i, fn, d):
        time.sleep(d)
        outs[i] = fn()

    th = [threading.Thread(target=go, args=(i, fn, d), daemon=True) for i, (fn, d) in enumerate(jobs)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return outs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=("burst", "admit"))
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--clients", type=int, default=4, help="burst: requests sent at once")
    ap.add_argument("--decoders", type=int, default=3, help="admit: requests decoding before the long prompt arrives")
    ap.add_argument("--tokens", type=int, default=8192, help="long prompt size in tokens")
    ap.add_argument("--max-tokens", type=int, default=256, help="answer cap of a long-prompt request")
    ap.add_argument("--decode-tokens", type=int, default=2048, help="admit: answer cap of the decoders")
    ap.add_argument("--delay", type=float, default=20.0, help="admit: seconds before the long prompt is sent")
    ap.add_argument("--json", help="append the result as one JSON line to this file")
    a = ap.parse_args()
    SM.URL = a.url.rstrip("/")
    text = SM.docs_corpus()
    step = len(text) // (a.clients + 1)
    if a.scenario == "burst":
        prompts = [SM.sized(text, a.tokens, k * step) for k in range(a.clients)]
        t0 = time.monotonic()
        outs = run_threads([(lambda p=p: SM.chat(p, max_tokens=a.max_tokens), 0.0) for p in prompts])
        rows = [summary(o, t0) for o in outs]
        ttfts = sorted(r["ttft_s"] for r in rows if r.get("ttft_s") is not None)
        rates = [r["decode_tok_s"] for r in rows if r.get("decode_tok_s")]
        gen = sum(SM.streamed_tokens(o) or 0 for o in outs)
        wall = max(o["done"] for o in outs) - t0
        result = {"scenario": "burst", "clients": a.clients, "target_tokens": a.tokens, "requests": rows,
                  "ttft_first_s": ttfts[0] if ttfts else None, "ttft_last_s": ttfts[-1] if ttfts else None,
                  "decode_tok_s_median": statistics.median(rates) if rates else None,
                  "aggregate_tok_s": round(gen / wall, 2) if wall > 0 else None, "wall_s": round(wall, 2)}
    else:
        newcomer = SM.sized(text, a.tokens, step)
        t0 = time.monotonic()
        jobs = [(lambda q=ESSAY.format(TOPICS[i % len(TOPICS)]): SM.chat(q, max_tokens=a.decode_tokens), 0.0)
                for i in range(a.decoders)]
        jobs.append((lambda: SM.chat(newcomer, max_tokens=a.max_tokens), a.delay))
        outs = run_threads(jobs)
        dec, new = outs[:-1], outs[-1]
        sent = new["sent"]
        first = new["chunks"][0] if new.get("chunks") else new["done"]
        before = [rate_in(o["chunks"], max(o["chunks"][0], sent - 10), sent) if o.get("chunks") else None for o in dec]
        # the window closes at the newcomer's first token, or earlier at a decoder's own last token
        during = [rate_in(o["chunks"], sent, min(first, o["chunks"][-1]), inclusive=o["chunks"][-1] <= first)
                  if o.get("chunks") else None for o in dec]
        during_s = [round(min(first, o["chunks"][-1]) - sent, 2) if o.get("chunks") else None for o in dec]
        result = {"scenario": "admit", "decoders": a.decoders, "target_tokens": a.tokens,
                  "newcomer": summary(new, t0), "decoders_result": [summary(o, t0) for o in dec],
                  "decoder_stream_rate_10s_before": before, "decoder_stream_rate_during_admission": during,
                  "during_window_s": during_s}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **result}, ensure_ascii=False) + "\n")
    return 0 if all(not o.get("error") for o in outs) else 1


if __name__ == "__main__":
    sys.exit(main())
