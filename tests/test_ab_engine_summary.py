"""bench/ab_engine_summary.py on synthetic result files: which runs are usable, the numbers, the answers."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
import ab_engine_summary as S  # noqa: E402

RUNS = (("c0-1", "C0", "2026-10-19 03:00:00"), ("c1-1", "C1", "2026-10-19 04:00:00"), ("c0-2", "C0", "2026-10-19 05:00:00"))


def write(r: Path, tag: str, arm: str, at: str, *, speed=1.0, sha_salt="", check="ok", same="yes", counted=33,
          screen_exit=0, journal=(), kernel=(), error=None, no_after=False):
    (r / f"{tag}-arm.txt").write_text(
        f"tag {tag}  arm {arm}  at {at}  loaded {at}  settle 300 s\nengine pid 1 exe /t/strata sha256 abc  start ticks 5\n"
        f"arm check: {check}\n")
    if not no_after:
        (r / f"{tag}-after.txt").write_text(
            f"tag {tag}  ended at {at}  engine_screen exit {screen_exit}\nsame engine as before: {same}\n"
            f"requests counted by /metrics: {counted}  sent: 33\n--- the test unit's journal lines\n"
            + "".join(l + "\n" for l in journal) + "--- kernel lines\n" + "".join(l + "\n" for l in kernel))
    recs = []
    for size, base in ((1200, 300.0), (4000, 400.0), (16000, 700.0)):
        for i in range(3):
            recs.append({"phase": "prefill", "size": size, "i": i, "engine_prompt_tok_s": base * speed,
                         "ttft_s": 23.0 / speed if size == 16000 else 3.0, "text_sha": f"p{size}{i}{sha_salt}"})
    for i in range(3):
        recs.append({"phase": "decode", "i": i, "engine_decode_tok_s": 34.0, "text_sha": f"d{i}"})
    for rd in range(2):
        recs.append({"phase": "four", "round": rd, "total_tok_s": 100.0,
                     "requests": [{"text_sha": f"f{rd}{j}"} for j in range(4)]})
    for rd in range(2):
        recs.append({"phase": "admit", "round": rd, "joiner_ttft_s": 23.2 / speed, "longest_gap_s": 23.0 / speed,
                     "makespan_s": 60.0, "joiner": {"text_sha": f"j{rd}"}, "decoders": [{"text_sha": f"a{rd}{j}"} for j in range(3)]})
    for i in range(2):
        recs.append({"phase": "turns", "i": i, "first_sha": f"t{i}", "second_sha": f"u{i}"})
    if error:
        recs[0]["error"] = error
    with open(r / "engine-screen.jsonl", "a") as f:
        for x in recs:
            f.write(json.dumps({"tag": tag, **x}) + "\n")


def run(build, tags=tuple(t for t, _, _ in RUNS)):
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        build(r)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = S.main(list(tags) + ["--baseline", "C0", "--results", str(r)])
        return status, out.getvalue()


def three(r, **c1):
    for tag, arm, at in RUNS:
        write(r, tag, arm, at, **(c1 if arm == "C1" else {}))


class Summary(unittest.TestCase):
    def test_a_faster_arm_with_the_same_answers(self):
        status, out = run(lambda r: three(r, speed=1.10))
        self.assertEqual(status, 0, out)
        self.assertIn("| 16K prompt: engine read tok/s (higher is better) | 700.00 (700.00, 700.00); spread +0.0% | "
                      "770.00 (770.00): +10.0% (+10.0%) |", out)
        # 9 prefill + 3 decode + 8 four-user + 8 admission (2 joiners, 6 decoders) + 4 conversation turns
        self.assertIn("- C1: 32 requests; differ between its own runs: 0; differ from c0-1 (C0): 0", out)

    def test_an_arm_that_changes_the_answers(self):
        status, out = run(lambda r: three(r, sha_salt="x"))
        self.assertEqual(status, 0, out)
        self.assertIn("differ from c0-1 (C0): 9", out)

    def test_runs_that_are_not_usable(self):
        cases = {"arm check": ({"check": "env STRATA_MMQ_GROUP_SORT is '', the arm says '1'"}, "the arm check"),
                 "restart": ({"same": "NO"}, "the engine restarted"),
                 "other traffic": ({"counted": 35}, "/metrics counted 35 requests, the run sent 33"),
                 "screen failed": ({"screen_exit": 1, "error": "cut off"}, "engine_screen exited 1"),
                 "journal": ({"journal": ["x python[1]: [strata] the engine had stopped"]}, "1 journal lines"),
                 "kernel": ({"kernel": ["x kernel: NVRM: Xid 79"]}, "1 kernel lines"),
                 "no after record": ({"no_after": True}, "no arm or after record")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                status, out = run(lambda r: three(r, **kw))
                self.assertEqual(status, 2, out)
                self.assertIn(text, out)

    def test_no_usable_baseline(self):
        def b(r):
            for tag, arm, at in RUNS:
                write(r, tag, arm, at, **({"same": "NO"} if arm == "C0" else {}))
        status, out = run(b)
        self.assertEqual(status, 2)
        self.assertIn("no usable run of the baseline arm C0", out)


if __name__ == "__main__":
    unittest.main()
