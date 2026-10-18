"""bench/ab_model_summary.py on synthetic result files: no service, GPU or network needed.

Each case builds the four runs of the protocol (ABBA: x1, y1, y2, x2) with every record bench/ab_model_run.sh and
bench/run_profile.sh write, then breaks one thing.  The journal, kernel and engine lines are real lines of this host's
logs (the host name redacted, as the wrapper writes them)."""
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
# what decision record 0004's step 1 deploys: the ARC cap, the two RAM floors, each model's layer cuts
ARC, HEADROOM, PARK = 2147483648, "6", 6144
CUTS = {"IQ3_XXS": "13,26,38", "IQ3_S": "13,25,38"}
LOAD = [
    "strata generate: memory guard: limit 62.8 GiB (source: meminfo), current 11.3 GiB, reclaimable cache credited 0.0 GiB; "
    "available 51.5 GiB (MemAvailable 51.5 GiB), floor 6144 MiB",
    "strata generate: PCIe probe: 11.7 GB/s host->device (best of 11.7 11.6 11.4 11.4) -> pcie_frac 0.32 (default 0.55)",
    "strata generate: layer split: CUDA1 PCIe probe 11.2 GB/s (best of 11.2 11.2 11.0 11.2) -> pcie_frac 0.31",
    "strata generate: layer split: CUDA2 PCIe probe 11.0 GB/s (best of 10.7 11.0 10.9 10.8) -> pcie_frac 0.30",
    "strata generate: layer split: CUDA3 PCIe probe 11.0 GB/s (best of 11.0 11.0 10.6 11.0) -> pcie_frac 0.30",
    "strata generate: expert cache auto: 13.81 GiB free, 1536 MiB reserved (+0 MiB for the draft head) -> 5672 slots",
    "strata generate: expert cache 6656 slots, 9.41 GiB of VRAM; policy is",
    "strata generate: layer split: CUDA1 runs layers 13-25, expert cache 6656 slots (9.88 GiB), 6656 of its 6656 profiled pairs; slot 0 verified",
    "strata generate: layer split: CUDA2 runs layers 26-37, expert cache 6144 slots (11.43 GiB), 6144 of its 6144 profiled pairs; slot 0 verified",
    "strata generate: layer split: CUDA3 runs layers 38-47, expert cache 5120 slots (9.25 GiB), 5120 of its 5120 profiled pairs; slot 0 verified",
    "strata generate: layer split: CUDA0 runs layers 0-12",
    "strata generate: session is up (engine 0.1.43.1)",
    "strata serve: layer split: 100% of the experts resident, the prompt path's streamed ring 96 slots"]
J = " 十  18 22:55:47 <host> python[694786]: "          # the journal's prefix, as journalctl prints it here
J_WAIT = J + ("[strata] parallel: a request has waited 104 s for a free slot (busy: [0, 1, 2, 3], in use: [0, 1, 2, 3], "
              "ending: []) (issue #1603)")
J_LEAK = J + ("[strata] parallel: slot(s) 2 were marked busy with no request using them (issue #1603); freed, the waiting "
              "request goes on")
J_400 = J + ("[strata] 400 invalid request: request exceeds the context window: prompt (27878 tokens) + max tokens (32768) "
             "exceeds the context (32768); requests are never truncated")
J_DISCONNECT = J + "[strata] done: 312 tokens in 62 s (5.0 tok/s) (disconnect, cancel=True)"
J_STOPPED = J + "[strata] the engine had stopped (exit code -9); starting it again (a minute or two) ..."
J_RESTART = " 十  18 02:24:29 <host> systemd[1]: strata-q3.service: Scheduled restart job, restart counter is at 1."
J_MALFORMED = [J + "[strata] 400 malformed request (TypeError: 'int' object is not iterable):",
               J + "Traceback (most recent call last):", J + "TypeError: 'int' object is not iterable"]
K = " 十  18 21:15:15 <host> kernel: "
K_XID = K + "NVRM: Xid (PCI:0000:3b:00): 79, pid=694812, name=strata, GPU has fallen off the bus."
K_GLOBAL_OOM = K + ("Out of memory: Killed process 694812 (strata) total-vm:96000000kB, anon-rss:52000000kB, "
                    "file-rss:0kB, shmem-rss:0kB, UID:1003 pgtables:120000kB oom_score_adj:500")
K_SERVICE_OOM = K + ("oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,"
                     "oom_memcg=/system.slice/strata-q3.service,task_memcg=/system.slice/strata-q3.service,task=strata,"
                     "pid=694812,uid=1003")
K_USER_OOM = [K + "python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=200",
              K + ("oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=user.slice,mems_allowed=0,"
                   "oom_memcg=/user.slice/user-1003.slice/user@1003.service/app.slice/run-r3260.scope,"
                   "task_memcg=/user.slice/user-1003.slice/user@1003.service/app.slice/run-r3260.scope,task=python3,pid=4242,uid=1003"),
              K + ("Memory cgroup out of memory: Killed process 4242 (python3) total-vm:2100000kB, anon-rss:1900000kB, "
                   "file-rss:0kB, shmem-rss:0kB, UID:1003 pgtables:4000kB oom_score_adj:200")]
K_HUNG = K + "INFO: task txg_sync:1234 blocked for more than 122 seconds."
E_LINE = "strata serve: engine error: CUDA error 700 (an illegal memory access was encountered)"


def write_run(r: Path, tag: str, model: str, at: str, ticks: int, speed: float = 1.0, ttft: float = 23.0, **k) -> None:
    """Every file of one run.  Keyword arguments break one thing (see the defaults below)."""
    q = (k.get("loaded") or model.rsplit("-", 1)[-1]).upper()
    pack, ple_q = k.get("pack", q.lower()), k.get("ple_q", q)
    if not k.get("no_arm"):
        lines = [
            f"tag {tag}  arm A  at {at}",
            f"service checkout /srv/p100  commit {k.get('commit', 'abc123')}  tracked files changed: config/profile.conf",
            f"deployment code sha256 {k.get('code', 'c0de')}  (scripts tools systemd bench config, profile.conf aside)",
            f"perf set sha256 {k.get('perf', 'be4c')}  (/srv/p100/bench)",
            f"profile.conf: {k.get('profile', '--profile deployed --model ' + q.lower())}",
            f"config /c.json sha256 {q.lower()}0ff  without the model's fields {k.get('cfg', 'f1e1d5')}",
            "shared settings /s.json sha256 absent", "server.py sha256 5e5e  serve_load.py sha256 10ad",
            f"serve_load nonce {k.get('nonce', 'ab-0004')}",
            f"engine pid 1 exe /e/strata sha256 {k.get('exe', 'beef')}",
            f"engine start ticks {ticks}  engine age {k.get('age', 1350)} s",
            f"service ready for {k.get('ready', 900)} s  settle 300 s",
            f"engine args: /e/strata --serve --pack /d/packs/{pack} "
            f"--native /d/models/{q}/Qwen3.8-Flash-Next-GSQ-RCO-{q}-00001-of-00002.gguf "
            f"--ple-gguf /d/models/{ple_q}/Qwen3.8-Flash-Next-GSQ-RCO-{ple_q}-00002-of-00002.gguf --max-context 32768 "
            f"--conversation-cache-min-free-mib {k.get('park', PARK)} --layer-split {k.get('cuts', CUTS[q])} --batch 4",
            f"engine env: {k.get('env', 'CUDA_VISIBLE_DEVICES=0,1,2,3 STRATA_RESIDENT_HEADROOM_GIB=' + HEADROOM)}",
            "engine log: /e/log", "engine started: Mon Oct 19 01:00:00 2026",
            *[f"load: {x}" for x in k.get("load", LOAD)],
            f'health: {{"status": "ok", "model": "{model}", "loaded": true}}',
            "metrics requests before the run: 142", "version line: session is up (engine 0.1.43.1)",
            f"zfs_arc_max {k.get('arc', ARC)}  MemAvailable 20000000 kB", f"host units: {k.get('host_units', 'ok')}",
            f"gpu ecc (index, corrected, uncorrected): {k.get('ecc0', '0, [N/A], [N/A];1, [N/A], [N/A];')}"]
        (r / f"{tag}-arm.txt").write_text("\n".join(lines) + "\n")
    if not k.get("no_after"):
        el, jl, kl = k.get("engine_lines", []), k.get("journal_lines", []), k.get("kernel_lines", [])
        done = k.get("done", 98)
        lines = [
            f"tag {tag}  ended at {at}  run_profile exit {k.get('rc', 0)}  interrupted {k.get('interrupted', 'no')}",
            f"engine pid 1  start ticks {ticks}", f"same engine as before: {k.get('same', 'yes')}",
            f"idle at the end: {k.get('idle_end', 'yes')}",
            f"metrics requests during the run: {k.get('counted', done)}",
            f"engine log lines during the run: {k.get('engine_count', len(el))}",
            f"journal: {k.get('journal', 'system')}; lines during the run: {k.get('journal_count', len(jl))}",
            f"journal done lines during the run: {done} ({k.get('finishes', 'length,cancel=False 44, stop,cancel=False 54')})",
            f"kernel lines during the run: {k.get('kernel_count', len(kl))}",
            f"perf log Traceback lines: {k.get('tracebacks', 0)}",
            f"gpu ecc (index, corrected, uncorrected): {k.get('ecc1', '0, [N/A], [N/A];1, [N/A], [N/A];')}"]
        (r / f"{tag}-after.txt").write_text("\n".join(lines) + "\n")
        for kind, got in (("engine", el), ("journal", jl), ("kernel", kl)):
            if kind not in k.get("no_lines_file", ()):
                (r / f"{tag}-{kind}-errors.txt").write_text("".join(x + "\n" for x in got))
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


def four(r: Path, y=None, x=None, every=None, **one):
    """The protocol's four runs; every applies to all four, y / x to both runs of a model, then x1= ... y2= to one."""
    assert set(one) <= set(TAGS), one
    for tag, model, at, ticks in RUNS:
        kw = dict(every or {})
        kw.update((y if model == IQS else x) or {})
        kw.update(one.get(tag) or {})
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
        self.assertIn("| x1 | 100% | 6656/6656/6144/5120 | 0.32/0.31/0.30/0.30 |", out)

    def test_what_step_1_deploys_is_read_from_the_repository(self):
        self.assertEqual(S.deployed(), {"arc": str(ARC), "headroom": HEADROOM, "park_mib": str(PARK),
                                        "cuts": {"iq3_xxs": CUTS["IQ3_XXS"], "iq3_s": CUTS["IQ3_S"]}})

    def test_faster_never_fails(self):
        self.check(lambda r: four(r, y={"speed": 1.4, "ttft": 10.0}), 0, "Speed criterion: holds")

    def test_the_speed_criterion_failing_alone(self):
        self.check(lambda r: four(r, y={"speed": 0.88}), 1, "Speed criterion: does NOT hold")
        self.check(lambda r: four(r, y={"ttft": 26.5}), 1, "Speed criterion: does NOT hold")
        # the mean 9.5% lower, inside the limit, but one run 17% lower: each run must hold too
        self.check(lambda r: four(r, y={"speed": 0.83}, y1={"speed": 0.98}), 1, "Speed criterion: does NOT hold", "-17.00%")

    def test_what_is_not_a_fault(self):
        # a request that waited 90 s or more for a slot while the Poisson phase overloads the 4 slots: queueing
        # (about one run in two had such a line); another process's OOM inside its own cgroup limit: a note
        cases = {"a slot wait": ({"journal_lines": [J_WAIT, J_WAIT]}, "Stability criterion (perf runs): holds"),
                 "a user scope's own OOM": ({"kernel_lines": K_USER_OOM}, "3 kernel lines of other processes"),
                 "a results commit between runs": ({"commit": "def456"}, "Stability criterion (perf runs): holds")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                self.check(lambda r: four(r, y1=kw), 0, text)

    def test_a_candidate_run_that_is_not_clean_fails_stability(self):
        cases = {"restart": ({"same": "NO"}, "the engine restarted"),
                 "engine errors": ({"engine_lines": [E_LINE, E_LINE]}, "2 engine log error lines"),
                 "the engine stopped": ({"journal_lines": [J_STOPPED]}, "1 journal fault lines"),
                 "systemd restarted the service": ({"journal_lines": [J_RESTART]}, "1 journal fault lines"),
                 "a leaked slot": ({"journal_lines": [J_WAIT, J_LEAK]}, "1 journal fault lines"),
                 "an Xid": ({"kernel_lines": [K_XID]}, "1 kernel fault lines"),
                 "a global OOM kill": ({"kernel_lines": [K_GLOBAL_OOM]}, "1 kernel fault lines"),
                 "an OOM kill in the service": ({"kernel_lines": [K_SERVICE_OOM]}, "1 kernel fault lines"),
                 "lost requests": ({"done": 97}, "97 requests completed, 98 sent"),
                 "a cancelled request": ({"finishes": "length,cancel=False 44, stop,cancel=False 53, disconnect,cancel=True 1",
                                          "journal_lines": [J_DISCONNECT]}, "disconnect,cancel=True 1"),
                 "a worst-case request failed": ({"worst_500": True}, "1 failed requests"),
                 "an empty error field": ({"empty_error": True}, "failed requests"),
                 "an uncorrected ECC error": ({"ecc0": "0, 0, 0;1, 0, 0;", "ecc1": "0, 0, 0;1, 3, 2;"},
                                              "uncorrected ECC errors on GPU 1"),
                 "restart and lost records": ({"same": "NO", "drop": ("think4",)}, "records not exactly once: stress think4 0")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                self.check(lambda r: four(r, y1=kw), 3, "Stability criterion (perf runs): does NOT hold", text)

    def test_a_missing_number_does_not_hide_a_stability_failure(self):
        # the engine restarted during y1 and a number is missing: the verdict is the failure (3), not "cannot tell" (2)
        self.check(lambda r: four(r, y1={"same": "NO", "decode_1": float("nan")}), 3,
                   "Stability criterion (perf runs): does NOT hold", "Cannot be evaluated: no usable value")

    def test_a_baseline_run_that_is_not_clean_cannot_be_judged(self):
        self.check(lambda r: four(r, x2={"same": "NO"}), 2, "the baseline run x2: the engine restarted")
        # an Xid in a baseline run and in a candidate run: the host, not the model
        self.check(lambda r: four(r, x2={"kernel_lines": [K_XID]}, y1={"kernel_lines": [K_XID]}), 2,
                   "cannot be judged: candidate runs are not clean", "and neither is a baseline run")

    def test_what_makes_a_run_impossible_to_judge(self):
        cases = {"no after record": ({"no_after": True}, "no after record"),
                 "interrupted": ({"interrupted": "yes"}, "the run was interrupted"),
                 "interrupted after 50 requests": ({"interrupted": "yes", "rc": 130, "done": 50}, "the run was interrupted"),
                 "run_profile.sh refused to run": ({"rc": 2, "done": 0, "tracebacks": "unknown"}, "run_profile.sh exited 2"),
                 "other traffic": ({"done": 99}, "other traffic during the run: 99 requests completed, 98 sent"),
                 "another client's disconnect": ({"done": 99, "journal_lines": [J_DISCONNECT],
                                                  "finishes": "length,cancel=False 44, stop,cancel=False 54, disconnect,cancel=True 1"},
                                                 "cancelled or ended otherwise: disconnect,cancel=True 1"),
                 "another client's refused request": ({"journal_lines": [J_400]}, "1 requests refused (4xx)"),
                 "another client's malformed request": ({"journal_lines": J_MALFORMED}, "2 journal lines of a malformed"),
                 "requests in flight at the end": ({"idle_end": "no"}, "requests in flight at the end"),
                 "/metrics and the journal disagree": ({"counted": 98, "done": 99}, "/metrics counted 98 requests and the journal 99"),
                 "a client crashed": ({"tracebacks": 1, "done": 70}, "1 client Tracebacks"),
                 "a kernel problem of no process of the service": ({"kernel_lines": [K_HUNG]}, "1 kernel lines that name no process"),
                 "journal not read": ({"journal": "unreadable", "journal_count": "unknown"}, "the system journal was not read"),
                 "log truncated": ({"engine_count": "unknown (the log was replaced, truncated or removed during the run)"},
                                   "the engine log lines are unknown"),
                 "a lines file missing": ({"no_lines_file": ("kernel",)}, "the kernel log lines are unknown"),
                 "a record missing": ({"drop": ("think4",)}, "records not exactly once: stress think4 0"),
                 "a record twice": ({"double_admit": True}, "four_user admit 16384 2"),
                 "a NaN": ({"decode_1": float("nan")}, "no usable value for 1 client total tok/s")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                out = self.check(lambda r: four(r, y1=kw), 2, "Cannot be evaluated", text)
                self.assertNotIn("does NOT hold", out)

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
                 "PLE of the other model": (lambda r: four(r, y1={"ple_q": "IQ3_XXS"}), TAGS,
                                            "y1's --ple-gguf is not shard 2 of its --native"),
                 "the other model's cuts": (lambda r: four(r, y={"cuts": CUTS["IQ3_XXS"]}), TAGS,
                                            "y1 ran the layer split 13,26,38, the tool's cuts for iq3_s are 13,25,38"),
                 "profile.conf names the other model": (lambda r: four(r, y1={"profile": "--profile deployed --model iq3_xxs"}),
                                                        TAGS, "y1's profile.conf (--profile deployed --model iq3_xxs) does not name"),
                 "profile.conf names no model": (lambda r: four(r, x2={"profile": "--profile deployed"}), TAGS,
                                                 "x2's profile.conf (--profile deployed) does not name --model iq3_xxs"),
                 "too soon after the start": (lambda r: four(r, y1={"ready": 120}),
                                              TAGS, "y1 started 120 s after the service became ready, the protocol 300 s"),
                 "an engine started again after the service was ready": (lambda r: four(r, y1={"age": 40}), TAGS,
                                                                         "y1's engine (40 s) is younger"),
                 "the old ARC cap everywhere": (lambda r: four(r, every={"arc": 8589934592}), TAGS,
                                                "x1 ran with the ZFS ARC cap 8589934592, step 1 deploys 2147483648"),
                 "the old resident headroom everywhere": (lambda r: four(r, every={"env": "STRATA_RESIDENT_HEADROOM_GIB=8"}),
                                                          TAGS, "ran with STRATA_RESIDENT_HEADROOM_GIB 8, step 1 deploys 6"),
                 "the old parking floor everywhere": (lambda r: four(r, every={"park": 8192}), TAGS,
                                                      "--conversation-cache-min-free-mib 8192, step 1 deploys 6144"),
                 "the host units drifted": (lambda r: four(r, x2={"host_units": "drift"}), TAGS, "x2: the host units"),
                 "no placement lines": (lambda r: four(r, y2={"load": []}), TAGS, "y2 has no placement lines"),
                 "one run per model": (four, ("x1", "y1"), "has 1 runs, the protocol 2"),
                 "order AABB": (aabb, ("x1", "x2", "y1", "y2"), "the order AABB, the protocol is ABBA"),
                 "one engine start for two runs": (lambda r: [write_run(r, t, m, a, 7) for t, m, a, _ in RUNS], TAGS,
                                                   "do not each have an engine start of their own"),
                 "another ARC cap": (lambda r: four(r, x2={"arc": 8589934592}), TAGS, "the runs differ in the ZFS ARC cap"),
                 "another environment": (lambda r: four(r, y={"env": f"STRATA_RESIDENT_HEADROOM_GIB={HEADROOM} STRATA_X=1"}),
                                         TAGS, "the runs differ in the engine's environment"),
                 "another engine": (lambda r: four(r, y={"exe": "f00d"}), TAGS, "the runs differ in the engine:"),
                 "another server config": (lambda r: four(r, y2={"cfg": "0dd"}), TAGS,
                                           "the runs differ in the config without the model's fields"),
                 "another service code": (lambda r: four(r, y={"code": "c0de2"}), TAGS, "the runs differ in the service's code"),
                 "code that could not be hashed": (lambda r: four(r, every={"code": "unknown"}), TAGS,
                                                   "the service's code is unknown for x1, y1, y2, x2"),
                 "another perf set": (lambda r: four(r, x2={"perf": "be4d"}), TAGS, "the runs differ in the perf set"),
                 "other prompts": (lambda r: four(r, x2={"nonce": "1792312345"}), TAGS, "the runs differ in serve_load's prompts"),
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
