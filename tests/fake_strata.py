#!/usr/bin/env python3
"""A fake Strata server for the engine-experiment tests: the HTTP surface bench/engine_screen.py and bench/ab_engine.sh
use, nothing behind it.  Standard library only; no GPU, no model.

  python3 tests/fake_strata.py --port P --config CFG.json [--engine PATH]

- GET /health: loaded; GET /metrics: live idle (or busy while a reply streams) and totals.requests, one per reply.
- POST /v1/messages/count_tokens: a quarter of the characters.
- POST /v1/chat/completions (streamed): a reply made from the request's hash, so the same request always gets the
  same text, sent in 8 chunks a few milliseconds apart (40 chunks over about a second when max_tokens is 1024 or
  more, as a long answer decodes; 8 s when the prompt holds "[slow]", another client's long reply), then the finish,
  the usage and llama.cpp-style timings (prompt_per_second falls with the prompt's length).
- With --engine: a child process whose command line starts with that path and "--serve" and whose stderr is the
  config's "log", started with the config's "env", as server.py starts the engine.  This server writes the engine's
  start-up lines to that log, as 0.1.45 does: the verify line every start prints ("upload no error, sync no error"),
  and the GROUP_SORT plan line when the env asks for it (the engine plans the MMQ sort at start-up).
FAKE_FAIL=<n> cuts the n-th reply off (no finish, no usage) to test failure handling; FAKE_NO_GS=1 leaves the
GROUP_SORT line out whatever the env says (a switch that did not take); FAKE_EXTRA=1 counts one more reply at the
first chat request (another client's request inside a measurement); FAKE_BUSY=1 reports a reply in flight forever;
FAKE_ENV_DROP=<name> starts the engine without that variable of the config's env (a server.py that lost it), while
the log lines still follow the config; FAKE_RESTART=<n> kills the engine at the n-th chat request and starts a new
one (an engine that restarted during a measurement); FAKE_ENGINE_ERR=<n> writes an engine error line to the log at
the n-th chat request.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import signal
import subprocess
import threading
import time

LOCK = threading.Lock()
STATE = {"requests": 0, "busy": 0, "first_prompt": True, "n": 0}


def main() -> None:
    # as systemd starts a service: default signal handling (a driver that ignores TERM while it restores the service
    # passes that on to what it starts directly, which only the stubs do)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_DFL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--engine")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    env = cfg.get("env") or {}
    log = cfg.get("log")
    fail_at = int(os.environ.get("FAKE_FAIL", "0"))
    restart_at = int(os.environ.get("FAKE_RESTART", "0"))
    err_at = int(os.environ.get("FAKE_ENGINE_ERR", "0"))
    no_gs, extra, busy_forever = (os.environ.get(k) == "1" for k in ("FAKE_NO_GS", "FAKE_EXTRA", "FAKE_BUSY"))

    def logline(s: str) -> None:
        if log:
            with open(log, "a") as f:
                f.write(s + "\n")

    if a.engine:
        logline(f"[strata] {time.strftime('%Y-%m-%d %H:%M:%S')} engine started: {a.engine} --serve --fake")
        logline("strata generate: memory guard: limit 62.8 GiB, floor 8192 MiB")
        logline("strata generate: session is up (engine 0.1.45)")
        logline("strata verify: captured the 4-token window (upload no error, sync no error)")
        if str(env.get("STRATA_MMQ_GROUP_SORT")) == "1" and not no_gs:
            logline("strata: prompt MMQ sorts the full expert groups (STRATA_MMQ_GROUP_SORT=1): 48 of 48 layers")
    child_env = {**os.environ, **{k: str(v) for k, v in env.items()}}
    child_env.pop(os.environ.get("FAKE_ENV_DROP") or "", None)
    engine = []

    def start_engine() -> None:
        # the engine: the binary at that path (in the tests a program that only waits), its command line the
        # engine's, its stderr the log
        engine.append(subprocess.Popen([a.engine, "--serve", "--fake"], env=child_env,
                                       stderr=open(log, "a") if log else None, stdout=subprocess.DEVNULL))

    if a.engine:
        start_engine()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def send_json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.send_json({"status": "ok", "model": cfg.get("model_name", "fake"), "loaded": True})
            elif self.path == "/metrics":
                with LOCK:
                    busy = STATE["busy"] + (1 if busy_forever else 0)
                    self.send_json({"live": {"state": "decoding" if busy else "idle", "running": busy, "queued": 0},
                                    "totals": {"requests": STATE["requests"], "since": 1.0}})
            else:
                self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/messages/count_tokens":
                text = "".join(m["content"] for m in body["messages"])
                self.send_json({"input_tokens": max(1, len(text) // 4)})
                return
            if self.path != "/v1/chat/completions":
                self.send_json({"error": "not found"}, 404)
                return
            with LOCK:
                STATE["n"] += 1
                n = STATE["n"]
                STATE["busy"] += 1
                first = STATE["first_prompt"]
                STATE["first_prompt"] = False
            if first and extra:
                with LOCK:
                    STATE["requests"] += 1
            if a.engine and restart_at and n == restart_at:
                engine[-1].kill()
                engine[-1].wait()
                logline(f"[strata] {time.strftime('%Y-%m-%d %H:%M:%S')} engine started: {a.engine} --serve --fake")
                start_engine()
            logline(f"strata verify: captured the {n}-token window (upload no error, sync no error)")   # every read
            if err_at and n == err_at:
                logline("strata serve: CUDA error 700 in the prompt read (an illegal memory access)")
            prompt = json.dumps(body["messages"], sort_keys=True)
            ptoks = max(1, len(prompt) // 4)
            words = hashlib.sha256(prompt.encode()).hexdigest() * 5
            long_reply = (body.get("max_tokens") or 0) >= 1024
            chunks = [words[i:i + 8] + " " for i in range(0, 320 if long_reply else 64, 8)]
            pause = 8.0 / len(chunks) if "[slow]" in prompt else 0.025 if long_reply else 0.005
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for c in chunks:
                    ev = {"choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]}
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(pause)
                if fail_at and n == fail_at:
                    return                                          # cut off: no finish, no usage
                last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": ptoks, "completion_tokens": len(chunks) + 1,
                                  "prompt_tokens_details": {"cached_tokens": 0}},
                        "timings": {"prompt_per_second": 400.0 + ptoks / 100, "predicted_per_second": 30.0}}
                self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
                logline(f"strata serve: prompt {ptoks} tokens = 0 reused + {ptoks} read in 10 ms (400.0 tok/s), "
                        f"{len(chunks)} generated")
                with LOCK:
                    STATE["requests"] += 1
            finally:
                with LOCK:
                    STATE["busy"] -= 1

        def log_message(self, *args):
            pass

    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
