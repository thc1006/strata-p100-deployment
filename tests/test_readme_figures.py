"""docs/images/readme_figures.py: the committed figures are what it draws from the runs' records, the README shows
each in both versions and gives the same numbers, and a number it cannot read stops it (exit status 2, nothing
written) instead of drawing a figure from a default.  Each test loads its own copy of the script and points it at
copies of the files it changes, so neither another test nor the committed figures see the change."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "docs" / "images" / "readme_figures.py"
IMAGES = ROOT / "docs" / "images"
RESULTS = ROOT / "results"
RUN1, RUN2 = "ab3-iq3s-1", "ab3-iq3s-2"
FILES = [f"{RUN1}-arm.txt", f"{RUN2}-arm.txt", f"{RUN1}-monitor.csv", f"{RUN1}-perf.log", f"{RUN1}-serve_load.json",
         f"{RUN2}-serve_load.json", "stress.jsonl"]


def load():
    spec = importlib.util.spec_from_file_location("readme_figures_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(name: str) -> str:
    return (RESULTS / name).read_text(encoding="utf-8")


def run_main(mod, *args) -> tuple[int, str]:
    err = io.StringIO()
    with unittest.mock.patch.object(sys, "argv", ["readme_figures.py", *args]), contextlib.redirect_stderr(err):
        return mod.main(), err.getvalue()


class ReadmeFigures(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def copy(self, changed: dict[str, str] | None = None):
        """A copy of the script reading copies of the results, with `changed` (file name: text) written over them,
        and writing into an empty directory of its own."""
        res, out = self.tmp / "results", self.tmp / "out"
        shutil.rmtree(res, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
        res.mkdir()
        out.mkdir()
        for f in FILES:
            shutil.copy(RESULTS / f, res)
        for name, text in (changed or {}).items():
            (res / name).write_text(text, encoding="utf-8")
        mod = load()
        mod.RESULTS, mod.STRESS, mod.OUT = res, res / "stress.jsonl", out
        return mod

    def stops(self, mod, why: str):
        code, err = run_main(mod)
        self.assertEqual(code, 2, err)
        self.assertIn("readme_figures: cannot read Missing: ", err)
        self.assertIn(why, err)
        self.assertEqual(list(mod.OUT.iterdir()), [], "a figure was written")

    def test_the_committed_figures_are_what_the_script_draws(self):
        r = subprocess.run([sys.executable, "-B", str(TOOL), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_copy_draws_the_committed_figures(self):
        mod = self.copy()
        self.assertEqual(run_main(mod), (0, ""))
        self.assertEqual(sorted(f.name for f in mod.OUT.iterdir()), sorted(f.name for f in IMAGES.glob("*.svg")))
        self.assertEqual(len(list(IMAGES.glob("*.svg"))), 4)
        for f in IMAGES.glob("*.svg"):
            self.assertEqual((mod.OUT / f.name).read_text(encoding="utf-8"), f.read_text(encoding="utf-8"), f.name)

    def test_check_fails_on_a_figure_that_differs_or_is_missing(self):
        for f in IMAGES.glob("*.svg"):
            shutil.copy(f, self.tmp)
        p = self.tmp / "speed.svg"
        p.write_text(p.read_text(encoding="utf-8").replace("Tokens per second", "Tokens a second"), encoding="utf-8")
        (self.tmp / "deployment-dark.svg").unlink()
        mod = load()
        mod.OUT = self.tmp
        code, err = run_main(mod, "--check")
        self.assertEqual(code, 1)
        self.assertIn("docs/images/speed.svg is not what the script draws", err)
        self.assertIn("docs/images/deployment-dark.svg is not what the script draws", err)

    def test_the_readme_shows_each_figure_in_both_versions(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("deployment", "speed"):
            for f in (f"{name}.svg", f"{name}-dark.svg"):
                self.assertTrue((IMAGES / f).is_file(), f)
            self.assertIn(f'srcset="docs/images/{name}-dark.svg"', readme)
            self.assertIn(f'src="docs/images/{name}.svg"', readme)

    def test_the_readme_gives_the_figures_numbers(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        top = readme[:readme.index("</sub>", readme.index('src="docs/images/speed.svg"'))]
        for key, value in load().facts().items():
            self.assertIn(value, top, key)

    def test_the_figures_have_a_title_a_description_and_a_background(self):
        for f in sorted(IMAGES.glob("*.svg")):
            t = f.read_text(encoding="utf-8")
            self.assertRegex(t, rf'<title id="t">[^<]*{RUN1}[^<]*</title>', f.name)
            self.assertRegex(t, r'<desc id="d">[^<]{100,}</desc>', f.name)
            self.assertRegex(t, r'<rect x="0" y="0" width="960" height="\d+" rx="0" fill="#', f.name)
            self.assertNotIn('nan"', t, f.name)

    def test_numbers_are_rounded_half_up_as_written(self):
        mod = load()
        for x, digits, want in ((11.45, 1, "11.5"), (2.25, 1, "2.3"), (2.249, 1, "2.2"), (26.88, 0, "27"),
                                (0.05, 1, "0.1"), (14.6484375, 1, "14.6")):
            self.assertEqual(mod.fmt(x, digits), want, x)
        self.assertEqual(mod.rng([32.09, 32.13]), "32.1")
        self.assertEqual(mod.rng([94.63, 95.04]), "94.6–95.0")

    def test_a_line_of_the_run_it_cannot_read_stops_it(self):
        arm = read(f"{RUN1}-arm.txt")
        cases = {
            "the four cards' lines": arm.replace("CUDA2 runs layers", "CUDA2 ran layers"),
            "CUDA0's cache": arm.replace("expert cache 6656 slots, 11.63 GiB of VRAM", "expert cache"),
            "CUDA0's layers": arm.replace("CUDA0 runs layers 0-12", "CUDA0 runs"),
            "experts in RAM": arm.replace("resident RAM mode", "resident mode"),
            "four PCIe probes": arm.replace("CUDA3 PCIe probe", "CUDA3 probe"),
            "the engine version": arm.replace("(engine 0.1.45)\nzfs", "(engine)\nzfs"),
            "layers that follow on": arm.replace("runs layers 25-37", "runs layers 26-37"),
            "the share resident": arm.replace("98% of the experts resident", "0% of the experts resident"),
            "the RAM size": arm.replace("memory guard: limit 62.8 GiB", "memory guard: limit"),
            "its date": arm.replace(f"tag {RUN1}  arm B  at 2026-10-20", f"tag {RUN1}  arm B"),
            "the config's sha256": arm.replace("sha256 e3e5a7b4e22e72c1", "sha256 -"),
            "the model": arm.replace('"model": "qwen3.8-flash-next-iq3_s"', '"model": ""'),
            "--batch and its value": arm.replace(" --batch 4", ""),
            "--conversation-cache-mib and its value": arm.replace(" --conversation-cache-mib 8192", ""),
            "--layer-split as the cards ran it": arm.replace("--layer-split 13,25,38", "--layer-split 12,24,36"),
            "--ple-io ram": arm.replace("--ple-io ram", "--ple-io mmap"),
            "experts in RAM, page-locked": arm.replace("experts in RAM (page-locked)", "experts in RAM (pageable)"),
            "the engine's sha256": arm.replace("sha256 00869ac7b733d821", "sha256 -"),
            "the engine's environment": arm.replace("\nengine env: ", "\nengine environment: ")}
        for why, text in cases.items():
            with self.subTest(why):
                self.assertNotEqual(text, arm, why)
                self.stops(self.copy({f"{RUN1}-arm.txt": text}), why)

    def test_runs_that_differ_stop_it(self):
        arm2 = read(f"{RUN2}-arm.txt")
        for why, text in (("one config in every run", arm2.replace("e3e5a7b4e22e72c1", "0123456789abcdef")),
                          ("one day in every run", arm2.replace("at 2026-10-20", "at 2026-10-21")),
                          ("one engine in every run", arm2.replace("00869ac7b733d821", "0123456789ab0000")),
                          ("one engine version in every run",
                           arm2.replace("(engine 0.1.45)\nzfs", "(engine 0.1.4)\nzfs")),
                          ("one engine environment in every run",
                           arm2.replace("STRATA_PIPED_CONCURRENT=1", "STRATA_PIPED_CONCURRENT=0")),
                          ("one model in every run", arm2.replace("flash-next-iq3_s\"", "flash-next-iq3_xxs\""))):
            with self.subTest(why):
                self.assertNotEqual(text, arm2, why)
                self.stops(self.copy({f"{RUN2}-arm.txt": text}), why)

    def test_a_blank_or_bad_cell_of_the_monitor_stops_it(self):
        rows = read(f"{RUN1}-monitor.csv").splitlines()
        head = rows[0].split(",")
        for col, value in (("g2_mem_mib", ""), ("g0_mem_mib", "nan"), ("mlocked_gib", "-1"), ("t", "x")):
            with self.subTest(col=col, value=value):
                cells = rows[5].split(",")
                cells[head.index(col)] = value
                text = "\n".join(rows[:5] + [",".join(cells)] + rows[6:]) + "\n"
                self.stops(self.copy({f"{RUN1}-monitor.csv": text}), f"{col} on line 6")

    def test_a_monitor_that_misses_the_run_or_cannot_be_parsed_stops_it(self):
        rows = read(f"{RUN1}-monitor.csv").splitlines()
        with self.subTest("two VRAM sizes"):
            head = rows[0].split(",")
            cells = rows[7].split(",")
            cells[head.index("vram_total_mib")] = "12288"
            self.stops(self.copy({f"{RUN1}-monitor.csv": "\n".join(rows[:7] + [",".join(cells)] + rows[8:]) + "\n"}),
                       "one VRAM size")
        with self.subTest("cut short"):
            self.stops(self.copy({f"{RUN1}-monitor.csv": "\n".join(rows[:100]) + "\n"}),
                       "a monitor that covers the run")
        with self.subTest("a NUL byte"):
            self.stops(self.copy({f"{RUN1}-monitor.csv": "\n".join(rows[:5] + ["1\0,2"] + rows[5:]) + "\n"}),
                       f"{RUN1}-monitor.csv: ")

    def test_a_load_run_with_nan_an_error_or_a_missing_client_count_stops_it(self):
        src = json.loads(read(f"{RUN2}-serve_load.json"))
        cases = {f"{RUN2}: per_req_tps": json.dumps(src).replace(str(src[2]["per_req_tps"]), "NaN"),
                 "1, 2 and 4 clients in each run": json.dumps(src[:2]),
                 f"{RUN2}-serve_load.json: errors": json.dumps([dict(src[0], errors=1)] + src[1:])}
        for why, text in cases.items():
            with self.subTest(why):
                self.stops(self.copy({f"{RUN2}-serve_load.json": text}), why)

    def test_a_first_token_it_cannot_read_stops_it(self):
        lines = read("stress.jsonl").splitlines()
        k2 = next(n for n, line in enumerate(lines) if f'"tag": "{RUN2}", "scenario": "replicate"' in line)
        k1 = next(n for n, line in enumerate(lines) if f'"tag": "{RUN1}", "scenario": "replicate"' in line)
        for why, change in (("docs-32k: TTFT", {"ttft_s": None}), ("docs-32k served in full", {"cached": 100}),
                            ("docs-32k served in full", {"status": 500}),
                            ("docs-32k served in full", {"error": "IncompleteRead"}),
                            ("docs-32k served in full", {"finish": None})):
            with self.subTest(why=why, change=change):
                rec = json.loads(lines[k2])
                rec["result"]["docs-32k"]["runs"][1].update(change)
                text = "\n".join(lines[:k2] + [json.dumps(rec)] + lines[k2 + 1:]) + "\n"
                self.stops(self.copy({"stress.jsonl": text}), why)
        with self.subTest("the first run twice, the second not at all"):
            text = "\n".join(lines[:k2] + [lines[k1]] + lines[k2 + 1:]) + "\n"
            self.stops(self.copy({"stress.jsonl": text}), "3 requests per prompt and run")
        with self.subTest("four requests of a prompt in the first run, two in the second"):
            r1, r2 = json.loads(lines[k1]), json.loads(lines[k2])
            r1["result"]["docs-32k"]["runs"].append(r2["result"]["docs-32k"]["runs"].pop())
            lines2 = list(lines)
            lines2[k1], lines2[k2] = json.dumps(r1), json.dumps(r2)
            self.stops(self.copy({"stress.jsonl": "\n".join(lines2) + "\n"}), "3 requests per prompt and run")

    def test_prompts_other_than_two_short_and_two_long_stop_it(self):
        lines = read("stress.jsonl").splitlines()
        out = []
        for line in lines:
            if '"scenario": "replicate"' in line and any(f'"tag": "{r}"' in line for r in (RUN1, RUN2)):
                rec = json.loads(line)
                for r in rec["result"]["code-v1"]["runs"]:
                    r["prompt_tokens"] = 1500
                line = json.dumps(rec)
            out.append(line)
        self.stops(self.copy({"stress.jsonl": "\n".join(out) + "\n"}), "two short prompts and two long ones")

    def test_lines_that_touch_stop_it(self):
        mod = load()
        s = mod.Svg(200, 100, mod.THEMES["light"], "t", "d")
        s.text(10, 30, "first line", 13)
        with self.assertRaises(mod.Missing):
            s.text(20, 34, "second line", 13)
        s.text(10, 60, "third line", 13)

    def test_a_run_without_its_end_or_a_monitor_that_ends_early_stops_it(self):
        log = read(f"{RUN1}-perf.log")
        self.stops(self.copy({f"{RUN1}-perf.log": log.replace("] done\n", "] stopped\n")}), "its done line")
        self.stops(self.copy({f"{RUN1}-perf.log": log.replace("[12:24:13] done", "[11:00:00] done")}),
                   "a done line after the start")
        rows = read(f"{RUN1}-monitor.csv").splitlines()
        self.stops(self.copy({f"{RUN1}-monitor.csv": "\n".join(rows[:-60]) + "\n"}), "a monitor that covers the run")
        self.stops(self.copy({f"{RUN1}-monitor.csv": "\n".join(rows[:1] + rows[11:]) + "\n"}),
                   "a monitor that covers the run")
        self.stops(self.copy({f"{RUN1}-monitor.csv": "\n".join(rows[:300] + rows[310:]) + "\n"}),
                   "a monitor without gaps")

    def test_sizes_beyond_the_card_or_the_ram_stop_them(self):
        rows = read(f"{RUN1}-monitor.csv").splitlines()
        head = rows[0].split(",")
        for col, value, why in (("g1_mem_mib", "20000", "GPU 1: memory in use within the card"),
                                ("mlocked_gib", "52", "the RAM's rows within it"),
                                ("mlocked_gib", "1e30", "the RAM's rows within it")):
            with self.subTest(col=col, value=value):
                cells = rows[9].split(",")
                cells[head.index(col)] = value
                text = "\n".join(rows[:9] + [",".join(cells)] + rows[10:]) + "\n"
                self.stops(self.copy({f"{RUN1}-monitor.csv": text}), why)

    def test_a_number_too_large_to_round_stops_it_with_status_2(self):
        lines = read("stress.jsonl").splitlines()
        k = next(n for n, line in enumerate(lines) if f'"tag": "{RUN2}", "scenario": "replicate"' in line)
        rec = json.loads(lines[k])
        rec["result"]["docs-32k"]["runs"][0]["ttft_s"] = 1e30
        mod = self.copy({"stress.jsonl": "\n".join(lines[:k] + [json.dumps(rec)] + lines[k + 1:]) + "\n"})
        code, err = run_main(mod)
        self.assertEqual(code, 2, err)
        self.assertIn("readme_figures: cannot read InvalidOperation", err)
        self.assertEqual(list(mod.OUT.iterdir()), [])

    def test_a_line_past_the_canvas_stops_it(self):
        mod = load()
        s = mod.Svg(200, 100, mod.THEMES["light"], "t", "d")
        with self.assertRaises(mod.Missing):
            s.text(150, 30, "a line past the edge", 13)
        with self.assertRaises(mod.Missing):
            s.text(5, 60, "a line before the edge", 13, anchor="end")

    def test_an_expert_cache_above_the_memory_in_use_stops_it(self):
        arm = read(f"{RUN1}-arm.txt").replace("expert cache 6656 slots, 11.63 GiB of VRAM",
                                              "expert cache 6656 slots, 15.63 GiB of VRAM")
        self.stops(self.copy({f"{RUN1}-arm.txt": arm}), "GPU 0: an expert cache within the memory in use")

    def test_a_timer_without_its_interval_stops_it(self):
        mod = self.copy()
        mod.TIMER = self.tmp / "strata-q3-health.timer"
        mod.TIMER.write_text("[Timer]\nOnBootSec=10min\n", encoding="utf-8")
        self.stops(mod, "OnUnitActiveSec")

    def test_a_line_too_wide_for_its_place_stops_it(self):
        mod = self.copy()
        mod.EM = 1.0
        self.stops(mod, 'room for the text "Lab members\' clients"')

    def test_all_experts_on_the_cards_drops_the_share_off_them(self):
        arm = read(f"{RUN1}-arm.txt").replace("98% of the experts resident", "100% of the experts resident")
        figs = self.copy({f"{RUN1}-arm.txt": arm}).figures()
        self.assertIn("experts, page-locked: those of slots lent to prompts", figs["deployment.svg"])
        self.assertNotIn("the 0%", figs["deployment.svg"])


if __name__ == "__main__":
    unittest.main()
