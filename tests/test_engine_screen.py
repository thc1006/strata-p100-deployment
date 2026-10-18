"""bench/engine_screen.py against tests/fake_strata.py: every phase's records, the same answers for the same requests,
a cut-off reply counted as a failure.  No GPU, no service; the corpus is a fixed text of this test."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
import engine_screen as ES  # noqa: E402

TEXT = "\n".join(f"Paragraph {i}: the engine reads a prompt in chunks and decodes a reply token by token." for i in range(3000))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Screen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        cfg = self.dir / "cfg.json"
        cfg.write_text(json.dumps({"model_name": "fake", "env": {}}))
        self.cfg = cfg
        ES.SM.docs_corpus = lambda: TEXT

    def tearDown(self):
        self.tmp.cleanup()

    def server(self, env=None):
        port = free_port()
        p = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_strata.py"), "--port", str(port), "--config",
                              str(self.cfg)], env={**os.environ, **(env or {})})
        self.addCleanup(lambda: (p.kill(), p.wait()))
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        return f"http://127.0.0.1:{port}"

    def screen(self, url, tag, extra=()):
        out = self.dir / "screen.jsonl"
        rc = ES.main(["--url", url, "--tag", tag, "--json", str(out), "--n", "1", "--admit-delay", "0.2", *extra])
        recs = [json.loads(l) for l in out.read_text().splitlines() if l.strip()] if out.exists() else []
        return rc, [r for r in recs if r["tag"] == tag]

    def test_every_phase_and_the_same_answers_twice(self):
        url = self.server()
        rc, a = self.screen(url, "a")
        self.assertEqual(rc, 0)
        phases = [r["phase"] for r in a]
        self.assertEqual(phases, ["prefill"] * 3 + ["decode", "four", "four", "admit", "admit", "turns", "turns"])
        self.assertEqual(sorted(r["size"] for r in a if r["phase"] == "prefill"), [1200, 4000, 16000])
        adm = [r for r in a if r["phase"] == "admit"]
        for r in adm:
            self.assertIsNotNone(r["joiner_ttft_s"])
            self.assertGreaterEqual(r["longest_gap_s"], 0)
            self.assertGreater(r["makespan_s"], 0)
            self.assertEqual(len(r["decoders"]), 3)
        rc, b = self.screen(url, "b")
        self.assertEqual(rc, 0)
        sha = lambda rs: [(r["phase"], r.get("text_sha") or r.get("first_sha")) for r in rs]   # noqa: E731
        self.assertEqual(sha(a), sha(b))

    def test_a_cut_off_reply_is_a_failure(self):
        url = self.server({"FAKE_FAIL": "2"})
        rc, recs = self.screen(url, "c", ["--phases", "prefill"])
        self.assertEqual(rc, 1)
        self.assertEqual(sum(1 for r in recs if r.get("error")), 1)

    def test_the_warm_up(self):
        url = self.server()
        self.assertEqual(ES.main(["--url", url, "--warm-up"]), 0)


if __name__ == "__main__":
    unittest.main()
