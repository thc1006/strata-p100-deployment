"""bench/ab_model_summary.py on synthetic result files: no service, GPU or network needed.

Each case builds the four runs of the protocol (ABBA: x1, y1, y2, x2) with every record bench/ab_model_run.sh and
bench/run_profile.sh write, then breaks one thing.  The journal, kernel and engine lines are real lines of this host's
logs or of server.py's own text (the host name redacted, as the wrapper writes them)."""
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
    "strata serve: layer split: 100% of the experts resident, the prompt path's streamed ring 96 slots",
    "FileExpertSource: mapped pinned cache complement ready: resident 1.12 GiB, pinned 1.12 GiB in 6 s; 1.12 GiB "
    "page-locked after taking the pages in steps; ",
    "strata generate: resident RAM mode: 1.12 GiB of experts in RAM (page-locked), 6656 in the GPU cache; adaptive swaps "
    "exchange them with the GPU cache (no file reads)"]
J = " 十  18 22:55:47 <host> python[694786]: "          # the journal's prefix, as journalctl prints it here
J_WAIT = J + ("[strata] parallel: a request has waited 104 s for a free slot (busy: [0, 1, 2, 3], in use: [0, 1, 2, 3], "
              "ending: []) (issue #1603)")
J_LEAK = J + ("[strata] parallel: slot(s) 2 were marked busy with no request using them (issue #1603); freed, the waiting "
              "request goes on")
J_400 = J + ("[strata] 400 invalid request: request exceeds the context window: prompt (27878 tokens) + max tokens (32768) "
             "exceeds the context (32768); requests are never truncated")
# server.py's two refusals that print no status (its parse_request and _foreign_page): another client's 403s
J_HOST = J + ("[strata] refused a request for Host 'p100.example:8080' from 100.64.0.7: not a name this server answers to "
              "(add it to \"allowed_hosts\" in the config or STRATA_ALLOWED_HOSTS, or set an API key)")
J_PAGE = J + ("[strata] refused an API request from the web page 'https://chat.example.org' (no API key; add its host to "
              "\"allowed_hosts\" or its origin to \"cors_origins\" in the config)")
J_503 = J + "[strata] 503: the engine's logits became non-finite (#879)"
J_SERVER_TRACE = [J + "Traceback (most recent call last):", J + "ConnectionResetError: [Errno 104] Connection reset by peer"]
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
ECC_ON = "0, 0, 0;1, 0, 0;"                          # cards with ECC on report counters; these P100s print [N/A]


def write_run(r: Path, tag: str, model: str, at: str, ticks: int, speed: float = 1.0, ttft: float = 23.0, **k) -> None:
    """Every file of one run.  Keyword arguments break one thing (see the defaults below)."""
    q = (k.get("loaded") or model.rsplit("-", 1)[-1]).upper()
    pack, ple_q = k.get("pack", q.lower()), k.get("ple_q", q)
    run_set = k.get("set", "run_profile.sh")
    if not k.get("no_arm"):
        lines = [
            f"tag {tag}  arm A  at {at}",
            f"service checkout /srv/p100  commit {k.get('commit', 'abc123')}  tracked files changed: config/profile.conf",
            f"deployment code sha256 {k.get('code', 'c0de')}  (scripts tools systemd bench config, profile.conf aside)",
            f"perf set sha256 {k.get('perf', 'be4c')}  (/srv/p100/bench)",
            f"set: bench/{run_set}",
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
            f"metrics requests before the run: {k.get('other', 142)}",
            f"conversation cache before the run: parked {k.get('parked', 0)} bytes {k.get('parked_bytes', 0)}",
            "version line: session is up (engine 0.1.43.1)",
            f"zfs_arc_max {k.get('arc', ARC)}  MemAvailable 20000000 kB", f"host units: {k.get('host_units', 'ok')}",
            f"gpu ecc (index, corrected, uncorrected): {k.get('ecc0', '0, [N/A], [N/A];1, [N/A], [N/A];')}"]
        (r / f"{tag}-arm.txt").write_text("\n".join(lines) + "\n")
    if not k.get("no_after"):
        el, jl, kl = k.get("engine_lines", []), k.get("journal_lines", []), k.get("kernel_lines", [])
        done = k.get("done", 98)
        lines = [
            f"tag {tag}  ended at {at}  {run_set[:-3]} exit {k.get('rc', 0)}  interrupted {k.get('interrupted', 'no')}",
            f"engine pid 1  start ticks {ticks}", f"same engine as before: {k.get('same', 'yes')}",
            f"idle at the end: {k.get('idle_end', 'yes')}",
            f"metrics requests during the run: {k.get('counted', done)}",
            f"engine log lines during the run: {k.get('engine_count', len(el))}",
            f"journal: {k.get('journal', 'system')}; lines during the run: {k.get('journal_count', len(jl))}",
            f"journal done lines during the run: {done} ({k.get('finishes', 'length,cancel=False 44, stop,cancel=False 54')})",
            f"kernel lines during the run: {k.get('kernel_count', len(kl))}",
            *([] if k.get("old_wrapper") else [
                f"concurrent read lines during the run: {k.get('conc_lines', 3)}, with slots decoding: "
                f"{k.get('conc_beside', 2)}",
                f"concurrent read off beside: {k.get('conc_off', 'no such line')}"]),
            f"perf log Traceback lines: {k.get('tracebacks', 0)}",
            f"gpu ecc (index, corrected, uncorrected): {k.get('ecc1', '0, [N/A], [N/A];1, [N/A], [N/A];')}"]
        (r / f"{tag}-after.txt").write_text("\n".join(lines) + "\n")
        for kind, got in (("engine", el), ("journal", jl), ("kernel", kl)):
            if kind not in k.get("no_lines_file", ()):
                (r / f"{tag}-{kind}-errors.txt").write_text("".join(x + "\n" for x in got))
    if k.get("no_perf"):
        return
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


def write_robust(r: Path, tag: str = "r1", model: str = IQS, at: str = "2026-10-19 04:00:00", ticks: int = 5000,
                 frees=(1900, 1901, 1902, 1903), avail=15.1, **k) -> None:
    """The robustness run: the wrapper's records around bench/run_robustness.sh and its monitor's summary."""
    write_run(r, tag, model, at, ticks, **{"set": "run_robustness.sh", "no_perf": True, **k})
    if not k.get("no_monitor"):
        (r / f"{tag}-robust-monitor-all.json").write_text(json.dumps({
            **{f"gpu{g}": {"min_free_mib": v} for g, v in enumerate(frees)}, "host": {"min_mem_avail_gib": avail}}))


def four(r: Path, y=None, x=None, every=None, **one):
    """The protocol's four runs; every applies to all four, y / x to both runs of a model, then x1= ... y2= to one."""
    assert set(one) <= set(TAGS), one
    for tag, model, at, ticks in RUNS:
        kw = dict(every or {})
        kw.update((y if model == IQS else x) or {})
        kw.update(one.get(tag) or {})
        write_run(r, tag, model, at, ticks, **kw)


def run(build, tags=TAGS, extra=()):
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        build(r)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = S.main(list(tags) + ["--results", str(r), *extra])
        return status, out.getvalue()


class Verdicts(unittest.TestCase):
    def check(self, build, status, *texts, tags=TAGS, extra=()):
        got, out = run(build, tags, extra)
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

    def test_the_limits_themselves_hold(self):
        # values whose quotients are the limits exactly in binary floating point: 2 / 40, -4 / 40, 3 / 20
        self.check(lambda r: four(r, x1={"decode_1": 41.0}, x2={"decode_1": 39.0}, y={"decode_1": 40.0}), 0,
                   "| 1 client total tok/s | 41.00, 39.00 | yes |", "Speed criterion: holds")
        self.check(lambda r: four(r, x={"decode_1": 40.0}, y={"decode_1": 36.0}), 0,
                   "| 1 client total tok/s | 40.00, 40.00 | yes | 36.00, 36.00 | -10.00% (-10.00%, -10.00%) | -10% | yes |")
        self.check(lambda r: four(r, x={"ttft": 20.0}, y={"ttft": 23.0}), 0,
                   "| 16K prompt joining 3 decoders: TTFT s | 20.00, 20.00 | yes | 23.00, 23.00 | +15.00% (+15.00%, +15.00%) "
                   "| +15% | yes |")
        # just past each limit
        self.check(lambda r: four(r, x1={"decode_1": 41.1}, x2={"decode_1": 39.0}, y={"decode_1": 40.0}), 2,
                   "the baseline runs differ by")
        self.check(lambda r: four(r, x={"decode_1": 40.0}, y={"decode_1": 35.9}), 1, "Speed criterion: does NOT hold")
        self.check(lambda r: four(r, x={"ttft": 20.0}, y={"ttft": 23.1}), 1, "Speed criterion: does NOT hold")

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
                 "a 503 for non-finite logits": ({"journal_lines": [J_503]}, "1 journal fault lines"),
                 "the server's own traceback": ({"journal_lines": J_SERVER_TRACE}, "2 journal fault lines"),
                 "an Xid": ({"kernel_lines": [K_XID]}, "1 kernel fault lines"),
                 "a global OOM kill": ({"kernel_lines": [K_GLOBAL_OOM]}, "1 kernel fault lines"),
                 "an OOM kill in the service": ({"kernel_lines": [K_SERVICE_OOM]}, "1 kernel fault lines"),
                 "lost requests": ({"done": 97}, "97 requests completed, 98 sent"),
                 "a cancelled request": ({"finishes": "length,cancel=False 44, stop,cancel=False 53, disconnect,cancel=True 1",
                                          "journal_lines": [J_DISCONNECT]}, "disconnect,cancel=True 1"),
                 "a request that ended in an error": ({"finishes": "length,cancel=False 44, stop,cancel=False 53, "
                                                                   "error,cancel=False 1"}, "error,cancel=False 1"),
                 "a worst-case request failed": ({"worst_500": True}, "1 failed requests"),
                 "an empty error field": ({"empty_error": True}, "failed requests"),
                 "an uncorrected ECC error": ({"ecc0": ECC_ON, "ecc1": "0, 0, 0;1, 3, 2;"}, "uncorrected ECC errors on GPU 1")}
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

    def test_a_fault_with_a_reason_to_measure_again_cannot_be_judged(self):
        # decision 0004: a baseline run that must be measured again, or a reason to measure the faulty candidate run
        # itself again, makes the A/B impossible to judge (2), not failed (3)
        cases = {"a hung txg_sync and an engine error in y1": (
                     {"y1": {"kernel_lines": [K_HUNG], "engine_lines": [E_LINE]}},
                     "each candidate run with a fault has a reason to be measured again too (y1: 1 kernel lines"),
                 "x2 interrupted, y1 restarted": ({"x2": {"interrupted": "yes"}, "y1": {"same": "NO"}},
                                                  "a baseline run must be measured again (x2: the run was interrupted)"),
                 "x1 with other traffic, y2 restarted": ({"x1": {"done": 99}, "y2": {"same": "NO"}},
                                                         "a baseline run must be measured again (x1: other traffic"),
                 "y1 restarted and lost records": ({"y1": {"same": "NO", "drop": ("think4",)}},
                                                   "has a reason to be measured again too (y1: records not exactly once")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                out = self.check(lambda r: four(r, **kw), 2, "Stability criterion (perf runs): cannot be judged", text)
                self.assertNotIn("does NOT hold", out)
        # a fault with no such reason still fails, whatever another candidate run must be measured again for
        self.check(lambda r: four(r, y1={"same": "NO"}, y2={"done": 99}), 3,
                   "Stability criterion (perf runs): does NOT hold: y1: the engine restarted.", "y2: other traffic")

    def test_what_makes_a_run_impossible_to_judge(self):
        cases = {"no after record": ({"no_after": True}, "no after record"),
                 "interrupted": ({"interrupted": "yes"}, "the run was interrupted"),
                 "interrupted after 50 requests": ({"interrupted": "yes", "rc": 130, "done": 50}, "the run was interrupted"),
                 "run_profile.sh refused to run": ({"rc": 2, "done": 0, "tracebacks": "unknown"}, "run_profile.sh exited 2"),
                 "other traffic": ({"done": 99}, "other traffic during the run: 99 requests completed, 98 sent"),
                 "another client's disconnect": ({"done": 99, "journal_lines": [J_DISCONNECT],
                                                  "finishes": "length,cancel=False 44, stop,cancel=False 54, disconnect,cancel=True 1"},
                                                 "cancelled or ended otherwise: disconnect,cancel=True 1"),
                 "another client's refused request": ({"journal_lines": [J_400]}, "1 requests refused (a 4xx"),
                 "another client's request for a foreign Host": ({"journal_lines": [J_HOST]}, "1 requests refused (a 4xx"),
                 "another site's web page": ({"journal_lines": [J_PAGE]}, "1 requests refused (a 4xx"),
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
                self.assertRegex(out, r"\| y1 \| qwen3\.8-flash-next-iq3_s \| (no|yes) \| - \|")    # no fault
        # the refusals in a baseline run are other traffic too, not a fault of the baseline
        for line in (J_HOST, J_PAGE):
            with self.subTest(baseline=line[60:100]):
                out = self.check(lambda r: four(r, x2={"journal_lines": [line]}), 2, "x2: other traffic during the run")
                self.assertNotIn("the baseline run x2", out)

    def test_runs_that_do_not_follow_the_protocol(self):
        def extra(r, at="2026-10-19 02:00:00"):
            four(r)
            write_run(r, "z1", IQS, at, 5000)

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
                 "the robustness set as an A/B run": (lambda r: four(r, y2={"set": "run_robustness.sh"}), TAGS,
                                                      "y2 ran bench/run_robustness.sh, not bench/run_profile.sh"),
                 "too soon after the start": (lambda r: four(r, y1={"ready": 120}),
                                              TAGS, "y1 started 120 s after the service became ready, the protocol 300 s"),
                 "an engine started again after the service was ready": (lambda r: four(r, y1={"age": 40}), TAGS,
                                                                         "y1's engine (40 s) is younger"),
                 "an engine 6 s younger than the ready time": (lambda r: four(r, y1={"age": 894}), TAGS,
                                                               "y1's engine (894 s) is younger than the service's ready time (900 s)"),
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
                 "an omitted run as the first began": (lambda r: extra(r, RUNS[0][2]), TAGS, "not included: z1"),
                 "an omitted run as the last began": (lambda r: extra(r, RUNS[-1][2]), TAGS, "not included: z1"),
                 "a damaged file": (damaged, TAGS, "latency.jsonl line")}
        for name, (build, tags, text) in cases.items():
            with self.subTest(name):
                self.check(build, 2, "Cannot be evaluated", text, tags=tags)
        # the boundaries themselves: an engine 5 s younger (the rounding of the two clocks), a run just outside
        self.check(lambda r: four(r, y1={"age": 895}), 0, "Stability criterion (perf runs): holds")
        self.check(lambda r: extra(r, "2026-10-19 00:59:59"), 0, "Stability criterion (perf runs): holds")
        self.check(lambda r: extra(r, "2026-10-19 03:00:01"), 0, "Stability criterion (perf runs): holds")

    def test_baseline_runs_that_disagree(self):
        self.check(lambda r: four(r, x2={"speed": 0.92}), 2, "the baseline runs differ by 8.33%")

    def test_the_tables_keep_the_measured_order(self):
        self.check(four, 0, "Runs in the order they were measured: x1 -> y1 -> y2 -> x2.",
                   "Note: the arguments were in another order", tags=("y2", "x1", "x2", "y1"))


class MeasuredAgain(unittest.TestCase):
    """--remeasured OLD=NEW: a run that cannot be told is measured again; the old one is accepted as replaced only
    when the summary itself rates it "cannot tell" with no fault."""
    AGAIN = ("x1", "y1r", "y2", "x2")

    def check(self, build, status, *texts, tags=AGAIN, extra=("--remeasured", "y1=y1r")):
        got, out = run(build, tags, extra)
        self.assertEqual(got, status, out)
        for t in texts:
            self.assertIn(t, out)
        return out

    @staticmethod
    def again(y1, y1r=None, **more):
        """The four runs with y1 measured again as y1r ten minutes later; more: x1= ... to break one run."""
        def build(r):
            four(r, y1=y1, **more)
            write_run(r, "y1r", IQS, "2026-10-19 01:50:00", 2500, **(y1r or {}))
        return build

    def test_a_run_that_cannot_be_told_is_replaced(self):
        cases = {"other traffic": {"done": 99}, "interrupted": {"interrupted": "yes", "rc": 130, "done": 40},
                 "a refused request": {"journal_lines": [J_HOST]}, "a kernel problem of no process": {"kernel_lines": [K_HUNG]},
                 "no after record": {"no_after": True}, "it broke the protocol": {"ready": 120}}
        for name, kw in cases.items():
            with self.subTest(name):
                out = self.check(self.again(kw), 0, "Measured again (--remeasured): y1 as y1r",
                                 "| y1 | qwen3.8-flash-next-iq3_s | measured again as y1r |",
                                 "Stability criterion (perf runs): holds: every run clean (y1 measured again as y1r).",
                                 "Runs in the order they were measured: x1 -> y1r -> y2 -> x2.")
                self.assertIn("| y1r | 2026-10-19 01:50:00 |", out)
        # the review's case: without the replacement the old run blocks the summary
        run_ = self.check(self.again({"done": 99}), 2, "not included: y1", extra=())
        self.assertNotIn("measured again as", run_)
        # a chain: y1 measured again as y1r, which was interrupted, and then as y1r2
        def chain(r):
            self.again({"done": 99}, {"interrupted": "yes"})(r)
            write_run(r, "y1r2", IQS, "2026-10-19 02:00:00", 2600)
        self.check(chain, 0, "| y1r | qwen3.8-flash-next-iq3_s | measured again as y1r2 |",
                   tags=("x1", "y1r2", "y2", "x2"), extra=("--remeasured", "y1=y1r", "--remeasured", "y1r=y1r2"))

    def test_a_run_with_a_fault_is_not_replaced(self):
        out = self.check(self.again({"same": "NO"}), 3, "Stability criterion (perf runs): does NOT hold: y1: the engine restarted.",
                         "a run with a fault is not replaced: its fault counts")
        self.assertNotIn("measured again as y1r |", out)
        # a fault and a reason to measure it again: cannot be judged, and still not replaced
        self.check(self.again({"same": "NO", "done": 99}), 2, "has a reason to be measured again too (y1: other traffic")
        # a baseline run with a fault, measured again: the A/B cannot be judged
        def base(r):
            four(r, x1={"same": "NO"})
            write_run(r, "x1r", XXS, "2026-10-19 01:10:00", 1500)
        self.check(base, 2, "the baseline run x1: the engine restarted", tags=("x1r", "y1", "y2", "x2"),
                   extra=("--remeasured", "x1=x1r"))

    def test_a_clean_run_is_not_replaced(self):
        out = self.check(self.again({}), 2, "y1 is said to be measured again as y1r, but this summary rates it clean")
        self.assertNotIn("Speed criterion", out)

    def test_replacements_that_do_not_fit(self):
        b = self.again({"done": 99})
        cases = {"not OLD=NEW": (("--remeasured", "y1"), "--remeasured 'y1' is not OLD=NEW"),
                 "a run given too": (("--remeasured", "x1=y1r"), "x1 is given as a run too"),
                 "the new run not given": (("--remeasured", "y1=y9"), "y9 is not one of the runs given"),
                 "itself": (("--remeasured", "y1=y1"), "a run cannot measure itself again"),
                 "twice": (("--remeasured", "y1=y1r", "--remeasured", "y1=y2"), "y1 is said to be measured again twice"),
                 "one run for two": (("--remeasured", "y1=y1r", "--remeasured", "z9=y1r"), "y1r is said to measure two runs again"),
                 "no arm record": (("--remeasured", "y1=y1r", "--remeasured", "z9=x2"), "no arm record (or no model or measured-from "
                                                                                       "time in it) for z9"),
                 "another model": (("--remeasured", "y1=x2"), "another model")}
        for name, (extra, text) in cases.items():
            with self.subTest(name):
                tags = ("x1", "y2", "x2", "y1r") if name == "another model" else self.AGAIN
                self.check(b, 2, "Cannot be evaluated", text, tags=tags, extra=extra)
        # the new run measured before the old one
        def late(r):
            four(r, y1={"done": 99})
            write_run(r, "y0", IQS, "2026-10-19 01:30:00", 1800)
        self.check(late, 2, "y0 was measured before y1", tags=("x1", "y0", "y2", "x2"), extra=("--remeasured", "y1=y0"))


class Robustness(unittest.TestCase):
    """--robustness TAG: criterion 1's robustness run, bench/run_robustness.sh on the candidate after the four runs."""

    def check(self, build, status, *texts, extra=("--robustness", "r1")):
        got, out = run(build, TAGS, extra)
        self.assertEqual(got, status, out)
        for t in texts:
            self.assertIn(t, out)
        return out

    @staticmethod
    def build(**k):
        def b(r):
            four(r)
            write_robust(r, **k)
        return b

    def test_the_floors_kept(self):
        out = self.check(self.build(), 0, "Stability criterion (robustness run): holds: every card kept at least 700 MiB",
                         "| r1 | qwen3.8-flash-next-iq3_s | 1900 / 1901 / 1902 / 1903 | 15.10 | 142 (0) | yes |",
                         "| r1 (robustness) |",
                         "Not evaluated here (decision record 0004): criterion 3, which rests on published measurements.")
        self.assertNotIn("quality", out)
        # exactly at the floors
        self.check(self.build(frees=(700, 700, 700, 700), avail=6.0), 0, "Stability criterion (robustness run): holds")

    def test_a_floor_broken(self):
        self.check(self.build(frees=(1900, 1901, 699, 1903)), 3, "Stability criterion (robustness run): does NOT hold: "
                   "GPU 2 kept 699 MiB free (at least 700).")
        self.check(self.build(avail=5.99), 3, "does NOT hold: MemAvailable fell to 5.99 GiB (at least 6)")
        # a floor broken and the engine restarted: the floor decides
        self.check(self.build(avail=5.2, same="NO"), 3, "does NOT hold: MemAvailable fell to 5.2 GiB")

    def test_a_robustness_run_that_cannot_be_judged(self):
        cases = {"the baseline model": ({"model": XXS}, "it ran qwen3.8-flash-next-iq3_xxs, not the candidate"),
                 "before the last run": ({"at": "2026-10-19 02:40:00"}, "it was measured before the last of the four runs (x2)"),
                 "the perf set": ({"set": "run_profile.sh"}, "r1 ran bench/run_profile.sh, not bench/run_robustness.sh"),
                 "another engine": ({"exe": "f00d"}, "the runs differ in the engine:"),
                 "the old floors": ({"park": 8192}, "--conversation-cache-min-free-mib 8192, step 1 deploys 6144"),
                 "no monitor": ({"no_monitor": True}, "no r1-robust-monitor-all.json"),
                 "a card missing": ({"frees": (1900, 1901, None, 1903)}, "its monitor lacks"),
                 "interrupted": ({"interrupted": "yes"}, "the run was interrupted"),
                 "a non-zero exit": ({"rc": 1}, "run_robustness.sh exited 1"),
                 "no after record": ({"no_after": True}, "no after record"),
                 "no arm record": ({"no_arm": True}, "no arm record"),
                 "the engine restarted": ({"same": "NO"}, "it kept the floors, but the engine restarted"),
                 "an Xid": ({"kernel_lines": [K_XID]}, "it kept the floors, but 1 kernel fault lines")}
        for name, (kw, text) in cases.items():
            with self.subTest(name):
                out = self.check(self.build(**kw), 2, "Stability criterion (robustness run): cannot be judged", text,
                                 "the robustness run cannot be judged")
                self.assertNotIn("does NOT hold", out)
        # the robustness run is no A/B run and no run measured again
        self.check(self.build(), 2, "the robustness run y2 is given as one of the four runs", extra=("--robustness", "y2"))
        # the robustness run between the A/B runs is not an omitted run (it then cannot be judged itself)
        out = self.check(self.build(at="2026-10-19 02:40:00"), 2, "it was measured before the last")
        self.assertNotIn("not included", out)

    def test_without_it(self):
        out = self.check(four, 0, "Not evaluated here (decision record 0004): the robustness run of criterion 1 (give its tag "
                         "with --robustness", extra=())
        self.assertNotIn("quality", out)
        self.assertNotIn("Stability criterion (robustness run)", out)


class Reported(unittest.TestCase):
    """What the summary reports without judging it."""

    def test_other_clients_before_the_runs(self):
        out = run(lambda r: four(r, x1={"other": 0}, y1={"other": 11}, y2={"other": 0}, x2={"other": 0}))[1]
        self.assertIn("Flag (reported, not judged by decision 0004): the runs began after different amounts of other "
                      "clients' work since their restart: requests completed x1 0, y1 11, y2 0, x2 0; conversations parked "
                      "x1 0, y1 0, y2 0, x2 0.", out)
        self.assertIn("| 142 (0) |", run(four)[1])
        for name, kw in {"a spread of 10": {"every": {"other": 0}, "y1": {"other": 10}},
                         "the same": {"every": {"other": 7}}}.items():
            with self.subTest(name):
                status, out = run(lambda r: four(r, **kw))
                self.assertEqual(status, 0, out)
                self.assertNotIn("Flag", out)
        status, out = run(lambda r: four(r, y2={"parked": 3}))
        self.assertEqual(status, 0, out)                 # reported, not judged
        self.assertIn("conversations parked x1 0, y1 0, y2 3, x2 0.", out)

    def test_the_complement_page_locked_in_ram(self):
        out = run(four)[1]
        self.assertIn("| x1 | 100% | 6656/6656/6144/5120 | 0.32/0.31/0.30/0.30 | 1.12 / 1.12 |", out)
        self.assertIn("| y2 | 100% | 6656/6656/6144/5120 | 0.32/0.31/0.30/0.30 | ? |",
                      run(lambda r: four(r, y2={"load": LOAD[:-2]}))[1])

    def test_the_concurrent_reads(self):
        status, out = run(four)
        self.assertEqual(status, 0, out)
        self.assertIn("| x1 | 3 | 2 | no such line |", out)
        self.assertNotIn("Flag", out)
        # an IQ3_S engine that keeps the concurrent read off: reported and flagged, not judged
        status, out = run(lambda r: four(r, y={"conc_lines": 0, "conc_beside": 0, "conc_off": "this expert source"}))
        self.assertEqual(status, 0, out)
        self.assertIn("| y1 | 0 | 0 | this expert source |", out)
        self.assertIn("Flag (reported, not judged by decision 0004): the runs differ in the concurrent read of decision "
                      "0005: x1 3 lines, y1 0 lines, kept off beside this expert source, y2 0 lines, kept off beside "
                      "this expert source, x2 3 lines.", out)
        # no line at all in one run (no line that says why) is flagged too
        self.assertIn("y2 0 lines, x2 3 lines.", run(lambda r: four(r, y2={"conc_lines": 0, "conc_beside": 0}))[1])
        # lines with no slot decoding still say the read was on: no flag
        self.assertNotIn("Flag", run(lambda r: four(r, y={"conc_beside": 0}))[1])
        # a count the wrapper could not take (the log replaced during the run) is left out, not taken for 0
        status, out = run(lambda r: four(r, y2={"conc_lines": "unknown", "conc_beside": "unknown"}))
        self.assertEqual(status, 0, out)
        self.assertIn("| y2 | - | - | no such line |", out)
        self.assertNotIn("Flag", out)
        # records of the wrapper before these lines: shown as unknown, no flag
        status, out = run(lambda r: four(r, every={"old_wrapper": True}))
        self.assertEqual(status, 0, out)
        self.assertIn("| x1 | - | - | ? |", out)
        self.assertNotIn("Flag", out)

    def test_ecc_that_cannot_fire(self):
        self.assertIn("ECC: no card gave ECC error counters in any run", run(four)[1])
        out = run(lambda r: four(r, every={"ecc0": ECC_ON, "ecc1": ECC_ON}))[1]
        self.assertNotIn("ECC: no card", out)


if __name__ == "__main__":
    unittest.main()
