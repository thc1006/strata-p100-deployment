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
          screen_exit=0, journal=(), kernel=(), error=None, no_after=False, idle="yes", errors="0", corpus="c0rpus",
          invalid_admit=0, salt_one=None):
    (r / f"{tag}-arm.txt").write_text(
        f"tag {tag}  arm {arm}  at {at}  loaded {at}  settle 300 s\nengine pid 1 exe /t/strata sha256 abc  start ticks 5\n"
        f"arm check: {check}\n")
    if not no_after:
        (r / f"{tag}-after.txt").write_text(
            f"tag {tag}  ended at {at}  engine_screen exit {screen_exit}\nsame engine as before: {same}\n"
            f"idle before the count: {idle}\nrequests counted by /metrics: {counted}  sent: 33\n"
            f"engine log error lines: {errors}\n--- the test unit's journal lines\n"
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
        bad = rd < invalid_admit
        recs.append({"phase": "admit", "round": rd, "joiner_ttft_s": 99.0 if bad else 23.2 / speed,
                     "longest_gap_s": 23.0 / speed, "makespan_s": 60.0, "valid": not bad,
                     "joiner": {"text_sha": f"j{rd}"}, "decoders": [{"text_sha": f"a{rd}{j}"} for j in range(3)]})
    for i in range(2):
        recs.append({"phase": "turns", "i": i, "first_sha": f"t{i}", "second_sha": f"u{i}", "second_cached": 2048})
    if error:
        recs[0]["error"] = error
    if salt_one is not None:                       # one request answered otherwise
        recs[salt_one]["text_sha"] += "x"
    for x in recs:
        x["corpus_sha"] = corpus
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
        self.assertIn("- C1: 32 requests; differ between its own runs: n/a (one run); differ from the baseline on 0 of "
                      "the 32 requests all its runs answer alike (0 it answers in more than one way)", out)
        self.assertIn("- C0: 32 requests; differ between its own 2 runs: 0", out)

    def test_an_arm_that_changes_the_answers(self):
        status, out = run(lambda r: three(r, sha_salt="x"))
        self.assertEqual(status, 0, out)
        self.assertIn("differ from the baseline on 9 of the 32 requests", out)

    def test_a_request_the_baseline_answers_two_ways_is_counted_apart(self):
        def b(r):
            for tag, arm, at in RUNS:
                write(r, tag, arm, at, **({"salt_one": 0} if tag == "c0-2" else {}))
        status, out = run(b)
        self.assertEqual(status, 0, out)
        self.assertIn("differ between its own 2 runs: 1", out)
        self.assertIn("differ from the baseline on 0 of the 31 requests all its runs answer alike (1 it answers in more "
                      "than one way)", out)

    def test_a_change_within_the_baselines_spread_is_marked(self):
        def b(r):
            write(r, "c0-1", "C0", "2026-10-19 03:00:00", speed=1.0)
            write(r, "c1-1", "C1", "2026-10-19 04:00:00", speed=1.01)
            write(r, "c0-2", "C0", "2026-10-19 05:00:00", speed=1.04)
        status, out = run(b)
        self.assertEqual(status, 0, out)
        self.assertRegex(out, r"\| 16K prompt: engine read tok/s .*: -1\.0% \([^)]*\) within the baseline's spread")

    def test_invalid_admission_rounds_are_left_out(self):
        status, out = run(lambda r: three(r, invalid_admit=1))
        self.assertEqual(status, 0, out)
        self.assertIn("Admission rounds not as designed (left out of the admission numbers): c0-1 0, c1-1 1, c0-2 0.", out)
        self.assertNotIn("99.00", out)                       # the invalid round's TTFT is not in any number

    def test_runs_of_different_screens_are_not_compared(self):
        def b(r):
            three(r)
            lines = (r / "engine-screen.jsonl").read_text().splitlines()
            out = []
            for l in lines:
                x = json.loads(l)
                x["phases"] = "prefill,decode,four,admit,turns,decode4" if x["tag"] == "c1-1" else "prefill,decode,four,admit,turns"
                out.append(json.dumps(x))
            (r / "engine-screen.jsonl").write_text("\n".join(out) + "\n")
        status, out = run(b)
        self.assertEqual(status, 2)
        self.assertIn("different screens", out)

    def test_runs_from_different_corpora_are_not_compared(self):
        status, out = run(lambda r: three(r, corpus="other"))
        self.assertEqual(status, 2)
        self.assertIn("different corpora", out)

    def test_runs_that_are_not_usable(self):
        cases = {"arm check": ({"check": "env STRATA_MMQ_GROUP_SORT is '', the arm says '1'"}, "the arm check"),
                 "restart": ({"same": "NO"}, "the engine restarted"),
                 "other traffic": ({"counted": 35}, "/metrics counted 35 requests, the run sent 33"),
                 "screen failed": ({"screen_exit": 1, "error": "cut off"}, "engine_screen exited 1"),
                 "journal": ({"journal": ["x python[1]: [strata] the engine had stopped"]}, "1 journal lines"),
                 "kernel": ({"kernel": ["x kernel: NVRM: Xid 79"]}, "1 kernel lines"),
                 "no after record": ({"no_after": True}, "no arm or after record"),
                 "still serving": ({"idle": "NO"}, "still serving when the requests were counted"),
                 "engine errors": ({"errors": "2"}, "2 engine error lines after the warm-up"),
                 "engine log unknown": ({"errors": "unknown"}, "unknown engine error lines"),
                 "journal unreadable": ({"journal": ["unreadable"]}, "journal unreadable"),
                 "kernel unreadable": ({"kernel": ["unreadable"]}, "kernel log unreadable")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                status, out = run(lambda r: three(r, **kw))
                self.assertEqual(status, 2, out)
                self.assertIn(text, out)

    def test_a_switch_must_show_in_the_engine_lines(self):
        arms = {"C0": {"never": ["on a thread of its own"]},
                "C1": {"engaged": ["^strata batch: read [0-9]+ tokens on a thread of its own"], "never": ["is off beside"]}}

        def b(r, c1_lines, c0_lines=("strata serve: prompt 1200 tokens = 0 reused + 1200 read",)):
            three(r)
            for tag, arm, _ in RUNS:
                lines = c1_lines if arm == "C1" else c0_lines
                (r / f"{tag}-engine-lines.txt").write_text("".join(l + "\n" for l in lines))
        on = ["strata batch: read 16021 tokens on a thread of its own in 23000 ms (concurrent, to 16021 of 16021)"]
        cases = {"it showed": (on, None, 0, None),
                 "it never showed": (["strata serve: prompt 16021 tokens"], None, 2, "its switch never showed"),
                 "it said it was off": (on + ["strata batch: STRATA_PIPED_CONCURRENT is off beside this expert source"],
                                        None, 2, "an engine line matches is off beside"),
                 "the baseline had it": (on, on, 2, "an engine line matches on a thread of its own")}
        for name, (c1, c0, want, text) in cases.items():
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    r = Path(tmp)
                    b(r, c1, c0) if c0 is not None else b(r, c1)
                    (r / "arms.json").write_text(json.dumps({"arms": arms}))
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        status = S.main([t for t, _, _ in RUNS] + ["--baseline", "C0", "--results", str(r),
                                                                  "--arms", str(r / "arms.json")])
                    self.assertEqual(status, want, out.getvalue())
                    if text:
                        self.assertIn(text, out.getvalue())

    def test_no_usable_baseline(self):
        def b(r):
            for tag, arm, at in RUNS:
                write(r, tag, arm, at, **({"same": "NO"} if arm == "C0" else {}))
        status, out = run(b)
        self.assertEqual(status, 2)
        self.assertIn("no usable run of the baseline arm C0", out)


if __name__ == "__main__":
    unittest.main()
