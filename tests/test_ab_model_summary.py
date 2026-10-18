"""bench/ab_model_summary.py on synthetic result files: no service, GPU or network needed.

Each case builds the four runs of the protocol (ABBA: x1, y1, y2, x2) with every record bench/ab_model_run.sh and
bench/run_profile.sh write, then breaks one thing."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
import ab_model_summary as S  # noqa: E402

XXS, IQS = "qwen3.8-flash-next-iq3_xxs", "qwen3.8-flash-next-iq3_s"
RUNS = (("x1", XXS, "2026-10-19 01:00:00", 1000), ("y1", IQS, "2026-10-19 01:40:00", 2000),
        ("y2", IQS, "2026-10-19 02:20:00", 3000), ("x2", XXS, "2026-10-19 03:00:00", 4000))
TAGS = tuple(r[0] for r in RUNS)


def write_run(r: Path, tag: str, model: str, at: str, ticks: int, speed: float = 1.0, ttft: float = 23.0, **k) -> None:
    """Every file of one run.  Keyword arguments break one thing (see the defaults below)."""
    q = (k.get("loaded") or model.rsplit("-", 1)[-1]).upper()
    pack = k.get("pack", q.lower())
    cuts = "13,25,38" if q == "IQ3_S" else "13,26,38"
    if not k.get("no_arm"):
        (r / f"{tag}-arm.txt").write_text(
            f"tag {tag}  arm A  at {at}\ndeployment commit abc123\nconfig /c.json sha256 c0ffee\n"
            f"shared settings /s.json sha256 absent\nserver.py sha256 5e5e  serve_load.py sha256 10ad\n"
            f"engine pid 1 exe /e/strata sha256 {k.get('exe', 'beef')}\nengine start ticks {ticks}  engine age 900 s  settle 300 s\n"
            f"engine args: /e/strata --serve --pack /d/packs/{pack} --native /d/models/{q}/Qwen3.8-Flash-Next-GSQ-RCO-{q}-00001-of-00002.gguf "
            f"--ple-gguf /d/models/{q}/Qwen3.8-Flash-Next-GSQ-RCO-{q}-00002-of-00002.gguf --max-context 32768 --layer-split {cuts} --batch 4\n"
            f"engine env: {k.get('env', 'CUDA_VISIBLE_DEVICES=0,1,2,3 STRATA_RESIDENT_HEADROOM_GIB=6')}\nengine log: /e/log\n"
            f"engine started: Mon Oct 19 01:00:00 2026\n"
            f'health: {{"status": "ok", "model": "{model}", "loaded": true}}\nversion line: session is up (engine 0.1.43.1)\n'
            f"zfs_arc_max {k.get('arc', 2147483648)}  MemAvailable 20000000 kB\ngpu ecc (index, corrected, uncorrected): 0, [N/A], [N/A];\n")
    if not k.get("no_after"):
        (r / f"{tag}-after.txt").write_text(
            f"tag {tag}  ended at {at}  run_profile exit 0  interrupted {k.get('interrupted', 'no')}\nengine pid 1  start ticks {ticks}\n"
            f"same engine as before: {k.get('same', 'yes')}\nengine log error lines during the run: {k.get('engine_errors', 0)}\n"
            f"journal: {k.get('journal', 'system')}; error lines during the run: {k.get('journal_errors', 0)}\n"
            f"journal done lines during the run: {k.get('done', 98)} ({k.get('finishes', 'length,cancel=False 44, stop,cancel=False 54')})\n"
            f"kernel error lines during the run: {k.get('kernel_errors', 0)}\nperf log Traceback lines: {k.get('tracebacks', 0)}\n")
    runs = lambda v: [{"status": 200, "finish": "length", "engine_prompt_tok_s": v} for _ in range(3)]   # noqa: E731
    rep = {"reasoning-v1": {"runs": runs(110.0)}, "code-v1": {"runs": runs(100.0)},
           "docs-4.4k": {"runs": runs(390.0 * speed)}, "docs-32k": {"runs": runs(780.0 * speed)}}
    worst = [{"status": 200, "finish": "length"} for _ in range(4)]
    if k.get("worst_500"):
        worst[2] = {"status": 500, "finish": None}
    think = [[{"status": 200, "finish": "stop", "client_decode_tok_s": 30.0} for _ in range(4)] for _ in range(3)]
    with open(r / "stress.jsonl", "a") as f:
        for scen, res in (("replicate", rep), ("think4", {"rounds": think}), ("worst", {"requests": worst})):
            if scen not in k.get("drop", ()):
                f.write(json.dumps({"tag": tag, "scenario": scen, "result": res}) + "\n")
    rows = [{"status": 200, "error": None} for _ in range(30)]
    with open(r / "latency.jsonl", "a") as f:
        f.write(json.dumps({"tag": tag, "scenario": "coldjoin", "result": {"decoders": [{"status": 200}] * 2,
                                                                         "joiners": [{"status": 200}] * 2}}) + "\n")
        f.write(json.dumps({"tag": tag, "scenario": "preempt", "result": {"short": {"ttft_s": 35.8, "status": 200},
                                                                        "long": {"ttft_s": 35.7, "status": 200}}}) + "\n")
        f.write(json.dumps({"tag": tag, "scenario": "poisson", "result": {"rate_req_s": 0.1, "requests": 30, "errors": 0,
                                                                        "rows": rows}}) + "\n")
    reqs = [{"finish": "length"} for _ in range(4)]
    if k.get("empty_error"):
        reqs[1] = {"finish": "length", "error": ""}
    with open(r / f"{tag}-four_user.jsonl", "w") as f:
        for t in (8192, 16384):
            f.write(json.dumps({"scenario": "burst", "target_tokens": t, "ttft_last_s": 94.0, "requests": reqs}) + "\n")
        for _ in range(2 if k.get("double_admit") else 1):
            f.write(json.dumps({"scenario": "admit", "target_tokens": 16384, "newcomer": {"ttft_s": ttft, "finish": "length"},
                                "decoders_result": [{"finish": "length"}] * 3}) + "\n")
    (r / f"{tag}-serve_load.json").write_text(json.dumps([
        {"clients": 1, "total_tps": k.get("decode_1", 34.0 * speed), "errors": 0},
        {"clients": 2, "total_tps": 60.0 * speed, "errors": 0}, {"clients": 4, "total_tps": 100.0 * speed, "errors": 0}]))
    (r / f"{tag}-monitor-all.json").write_text(json.dumps({
        **{f"gpu{g}": {"min_free_mib": 1900 + g} for g in range(4)}, "host": {"min_mem_avail_gib": 15.1}}))


def four(r: Path, y=None, x=None, y1=None, x2=None):
    """The protocol's four runs; y / x apply to both runs of a model, then y1 / x2 to that one run."""
    for tag, model, at, ticks in RUNS:
        kw = dict((y if model == IQS else x) or {})
        kw.update((y1 if tag == "y1" else x2 if tag == "x2" else None) or {})
        write_run(r, tag, model, at, ticks, **kw)


def run(build, tags=TAGS):
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        build(r)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = S.main(list(tags) + ["--results", str(r)])
        return status, out.getvalue()


class Verdicts(unittest.TestCase):
    def check(self, build, status, *texts, tags=TAGS):
        got, out = run(build, tags)
        self.assertEqual(got, status, out)
        for t in texts:
            self.assertIn(t, out)
        return out

    def test_clean_and_within_the_limits(self):
        out = self.check(lambda r: four(r, y={"speed": 0.95, "ttft": 25.0}), 0, "Speed criterion: holds",
                         "Stability criterion (perf runs): holds: every run clean.", "Not evaluated here")
        self.assertIn("| 4 clients total tok/s | 100.00, 100.00 | yes | 95.00, 95.00 | -5.00% (-5.00%, -5.00%) | -10% | yes |", out)

    def test_faster_never_fails(self):
        self.check(lambda r: four(r, y={"speed": 1.4, "ttft": 10.0}), 0, "Speed criterion: holds")

    def test_the_speed_criterion_failing_alone(self):
        self.check(lambda r: four(r, y={"speed": 0.88}), 1, "Speed criterion: does NOT hold")
        self.check(lambda r: four(r, y={"ttft": 26.5}), 1, "Speed criterion: does NOT hold")
        # the mean 9.5% lower, inside the limit, but one run 17% lower: each run must hold too
        self.check(lambda r: four(r, y={"speed": 0.83}, y1={"speed": 0.98}), 1, "Speed criterion: does NOT hold", "-17.00%")

    def test_a_candidate_run_that_is_not_clean_fails_stability(self):
        cases = {"restart": ({"same": "NO"}, "the engine restarted"),
                 "engine errors": ({"engine_errors": 2}, "2 engine log error lines"),
                 "journal errors only": ({"journal_errors": 5}, "5 journal error lines"),
                 "kernel errors": ({"kernel_errors": 1}, "1 kernel log error lines"),
                 "lost requests": ({"done": 97}, "97 requests completed, 98 sent"),
                 "a cancelled request": ({"finishes": "length,cancel=False 44, stop,cancel=False 53, stop,cancel=True 1"},
                                         "stop,cancel=True 1"),
                 "a worst-case request failed": ({"worst_500": True}, "1 failed requests"),
                 "an empty error field": ({"empty_error": True}, "failed requests"),
                 "restart and lost records": ({"same": "NO", "drop": ("think4",)}, "records not exactly once: stress think4 0")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                self.check(lambda r: four(r, y1=kw), 3, "Stability criterion (perf runs): does NOT hold", text)

    def test_a_missing_number_does_not_hide_a_stability_failure(self):
        # the engine restarted during y1 and a number is missing: the verdict is the failure (3), not "cannot tell" (2)
        self.check(lambda r: four(r, y1={"same": "NO", "decode_1": float("nan")}), 3,
                   "Stability criterion (perf runs): does NOT hold", "Cannot be evaluated: no usable value")

    def test_a_baseline_run_that_is_not_clean_cannot_be_judged(self):
        self.check(lambda r: four(r, x2={"same": "NO"}), 2, "the baseline run x2 is not clean (the engine restarted)")

    def test_what_makes_a_run_impossible_to_judge(self):
        cases = {"no after record": ({"no_after": True}, "no after record"),
                 "interrupted": ({"interrupted": "yes"}, "the run was interrupted"),
                 "other traffic": ({"done": 99}, "other traffic during the run"),
                 "a client crashed": ({"tracebacks": 1}, "1 client Tracebacks"),
                 "journal not read": ({"journal": "unreadable", "journal_errors": "unknown"}, "the system journal was not read"),
                 "log truncated": ({"engine_errors": "unknown (the log was replaced, truncated or removed during the run)"},
                                   "the engine log error count is unknown"),
                 "a record missing": ({"drop": ("think4",)}, "records not exactly once: stress think4 0"),
                 "a record twice": ({"double_admit": True}, "four_user admit 16384 2"),
                 "a NaN": ({"decode_1": float("nan")}, "no usable value for 1 client total tok/s")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                self.check(lambda r: four(r, y1=kw), 2, "Cannot be evaluated", text)

    def test_runs_that_do_not_follow_the_protocol(self):
        def extra(r):
            four(r)
            write_run(r, "z1", IQS, "2026-10-19 02:00:00", 5000)
        def damaged(r):
            four(r)
            with open(r / "latency.jsonl", "a") as f:
                f.write('{"tag": "y2", "scenario": "pois')
        def aabb(r):
            for t, m, a, n in (("x1", XXS, "2026-10-19 01:00:00", 1), ("x2", XXS, "2026-10-19 01:40:00", 2),
                               ("y1", IQS, "2026-10-19 02:20:00", 3), ("y2", IQS, "2026-10-19 03:00:00", 4)):
                write_run(r, t, m, a, n)
        cases = {"a tag twice": (four, ("x1", "y1", "y1", "x2"), "a tag is given more than once"),
                 "no arm record": (lambda r: four(r, y1={"no_arm": True}), TAGS, "no arm record"),
                 "name is not the file": (lambda r: four(r, y1={"loaded": "iq3_xxs", "pack": "iq3_xxs"}), TAGS,
                                          "y1 answered as qwen3.8-flash-next-iq3_s but loaded iq3_xxs"),
                 "pack of the other model": (lambda r: four(r, y1={"pack": "iq3_xxs"}), TAGS,
                                             "y1 loaded the iq3_s shard with the pack iq3_xxs"),
                 "one run per model": (four, ("x1", "y1"), "has 1 runs, the protocol 2"),
                 "order AABB": (aabb, ("x1", "x2", "y1", "y2"), "the order AABB, the protocol is ABBA"),
                 "one engine start for two runs": (lambda r: [write_run(r, t, m, a, 7) for t, m, a, _ in RUNS], TAGS,
                                                   "do not each have an engine start of their own"),
                 "another ARC cap": (lambda r: four(r, x2={"arc": 8589934592}), TAGS, "the runs differ in the ZFS ARC cap"),
                 "another environment": (lambda r: four(r, y={"env": "STRATA_RESIDENT_HEADROOM_GIB=8"}), TAGS,
                                         "the runs differ in the engine's environment"),
                 "another engine": (lambda r: four(r, y={"exe": "f00d"}), TAGS, "the runs differ in the engine:"),
                 "an omitted run": (extra, TAGS, "not included: z1"),
                 "a damaged file": (damaged, TAGS, "latency.jsonl line")}
        for name, (build, tags, text) in cases.items():
            with self.subTest(name):
                self.check(build, 2, "Cannot be evaluated", text, tags=tags)

    def test_baseline_runs_that_disagree(self):
        self.check(lambda r: four(r, x2={"speed": 0.92}), 2, "the baseline runs differ by 8.33%")

    def test_the_tables_keep_the_measured_order(self):
        self.check(four, 0, "Runs in the order they were measured: x1 -> y1 -> y2 -> x2.",
                   "Note: the arguments were in another order", tags=("y2", "x1", "x2", "y1"))


if __name__ == "__main__":
    unittest.main()
