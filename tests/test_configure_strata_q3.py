"""Configuration tests: no GPU, model, Strata runtime or network required.  The server integration test runs only
where the pinned Strata checkout and its .venv exist (it imports the real serve/server.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))   # tools/configure_strata_q3.py
import configure_strata_q3 as tool  # noqa: E402

STRATA = Path.home() / "dev" / "strata"


def fixture() -> dict:
    return {
        "exe": "/opt/strata/engine-cuda12/strata",
        "cwd": "/opt/strata",
        "tokenizer": "/models/中文字/pack/tokenizer",
        "lib_dirs": ["/opt/cuda-12.9/lib64"],
        "model_name": "qwen3.8-flash-next-iq3_xxs",
        "log": "/opt/strata/strata-iq3_xxs.log",
        "api_key": "private-fixture-key",
        "sampling": {"temperature": 0.8, "top_p": 0.95},
        "env": {"CUSTOM_KEEP": "private-fixture-env"},
        "args": [
            "--pack", "/models/中文字/pack",
            "--native", "/models/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf",
            "--ple-gguf", "/models/PLE-00002-of-00002.gguf",
            "--mtp", "/models/mtp/cjk",
            "--expert-profile", "/opt/strata/data/expert-profile.bin",
            "--expert-cache", "auto",
            "--prefill", "auto",
            "--spec", "4",
            "--spec-min-p", "0.5",
            "--max-context", "65536",
            "--kv", "q4_0",
            "--resident-experts",
            "--custom-future-option", "do-not-drop-this",
        ],
        "cuda": 12,
        "gpu": [0, 1, 2, 3],
        "layer_split": "auto",
        "parallel": 2,
    }


class TuningTests(unittest.TestCase):
    def test_baseline_preserves_paths_and_custom_fields(self):
        original = fixture()
        cfg = tool.tune_config(original, "baseline")
        self.assertEqual(original, fixture(), "Input must not mutate")
        for key in ("exe", "cwd", "tokenizer", "lib_dirs", "api_key", "sampling", "log"):
            self.assertEqual(cfg[key], original[key])
        for flag in ("--pack", "--native", "--ple-gguf", "--mtp", "--expert-profile",
                     "--spec-min-p", "--custom-future-option"):
            self.assertEqual(tool.option_values(cfg["args"], flag),
                             tool.option_values(original["args"], flag))
        self.assertEqual(cfg["env"]["CUSTOM_KEEP"], "private-fixture-env")
        self.assertEqual(cfg["gpu"], [0, 1, 2, 3])
        self.assertEqual(cfg["layer_split"], [12, 24, 36])
        self.assertEqual(cfg["parallel"], 4)
        self.assertEqual(tool.option_values(cfg["args"], "--vram-reserve-mib"), ["1536"])
        self.assertEqual(tool.option_values(cfg["args"], "--ple-io"), ["mmap"])
        self.assertNotIn("--no-ple-prefetch", cfg["args"])
        self.assertEqual(cfg["reasoning_effort"], "medium")
        self.assertEqual(cfg["reasoning_budget_tokens"], 1024)
        self.assertIs(cfg["fit_max_tokens"], False)
        self.assertIs(cfg["reasoning_close_retry"], True)
        self.assertNotIn("max_tokens", cfg)

    def test_deployed_is_the_measured_configuration(self):
        cfg = tool.tune_config(fixture())                      # the default profile
        self.assertEqual(cfg["layer_split"], [13, 26, 38])
        self.assertEqual(tool.option_values(cfg["args"], "--ple-io"), ["ram"])
        self.assertEqual(cfg["args"].count("--no-ple-prefetch"), 1)
        self.assertEqual(tool.option_values(cfg["args"], "--vram-reserve-mib"), ["1536"])
        self.assertEqual(tool.option_values(cfg["args"], "--prefill"), ["2048"])
        self.assertEqual(tool.option_values(cfg["args"], "--batch-groups"), ["auto"])
        self.assertEqual(tool.option_values(cfg["args"], "--conversation-cache-mib"), ["8192"])
        self.assertEqual(tool.option_values(cfg["args"], "--conversation-cache-slots"), ["8"])
        self.assertEqual(tool.option_values(cfg["args"], "--conversation-cache-min-tokens"), ["1024"])
        self.assertEqual(tool.option_values(cfg["args"], "--conversation-cache-min-free-mib"), ["6144"])
        self.assertEqual(cfg["env"]["STRATA_RESIDENT_HEADROOM_GIB"], "6")     # the same 6 GiB floor
        off = tool.tune_config(cfg, park_mib=0)
        self.assertNotIn("--conversation-cache-mib", off["args"])
        for bad in (-1, 99999, True, "8192"):
            with self.subTest(bad=bad), self.assertRaises(tool.ConfigError):
                tool.tune_config(fixture(), park_mib=bad)

    def test_aggressive_and_explicit_overrides(self):
        cfg = tool.tune_config(fixture(), "aggressive")
        self.assertEqual(cfg["layer_split"], [13, 26, 38])
        self.assertEqual(tool.option_values(cfg["args"], "--ple-io"), ["ram"])
        self.assertEqual(tool.option_values(cfg["args"], "--vram-reserve-mib"), ["1024"])
        self.assertEqual(cfg["args"].count("--no-ple-prefetch"), 1)
        override = tool.tune_config(fixture(), "aggressive", ple_io="mmap", split="equal")
        self.assertEqual(override["layer_split"], [12, 24, 36])
        self.assertNotIn("--conversation-cache-mib", cfg["args"])     # the original guide's profiles: parking off
        self.assertEqual(tool.option_values(override["args"], "--ple-io"), ["mmap"])
        self.assertNotIn("--no-ple-prefetch", override["args"])

    def test_conflicting_flags_and_env_normalized(self):
        cfg = fixture()
        cfg["args"] += [
            "--batch", "8", "--slots", "2", "--batch-groups", "1",
            "--batch-mtp", "--adapt-async", "1", "--pipeline-windows", "2",
            "--vram-reserve-later-mib", "4096", "--kv-resident", "16384",
            "--kv-grow", "--mmap-experts", "--resident-cpu-experts",
            "--prefill", "4096", "--pool-workers=21", "--trim-stage-weights",
            "--spec", "7", "--layer-split", "24", "--split-skip-if-fits",
            "--rope-scaling", "yarn", "--rope-scale", "2",
        ]
        cfg["env"].update({k: "1" for k in tool.ENV_DEFAULTS})
        cfg["split_skip_if_fits"] = True
        tuned = tool.tune_config(cfg)
        for flag in ("--batch", "--slots", "--batch-mtp", "--adapt-async", "--pipeline-windows",
                     "--vram-reserve-later-mib", "--kv-resident", "--kv-grow", "--mmap-experts",
                     "--resident-cpu-experts", "--layer-split", "--split-skip-if-fits",
                     "--rope-scaling", "--rope-scale"):
            self.assertNotIn(flag, tuned["args"])
        self.assertIn("--no-kv-grow", tuned["args"])
        self.assertEqual(tool.option_values(tuned["args"], "--prefill"), ["2048"])
        self.assertEqual(tool.option_values(tuned["args"], "--pool-workers"), ["6"])
        self.assertEqual(tool.option_values(tuned["args"], "--spec"), ["4"])
        self.assertEqual(tool.option_values(tuned["args"], "--kv"), ["int8"])
        for key, value in tool.ENV_DEFAULTS.items():
            self.assertEqual(tuned["env"][key], value)
        self.assertFalse(tuned["split_skip_if_fits"])

    def test_tuning_step_overrides(self):
        cfg = tool.tune_config(fixture(), "aggressive", prefill="4096", pool_workers=8, reserve_mib=1280, batch_groups=1)
        self.assertEqual(tool.option_values(cfg["args"], "--batch-groups"), ["1"])
        self.assertEqual(tool.option_values(cfg["args"], "--prefill"), ["4096"])
        self.assertEqual(tool.option_values(cfg["args"], "--pool-workers"), ["8"])
        self.assertEqual(tool.option_values(cfg["args"], "--vram-reserve-mib"), ["1280"])
        self.assertEqual(cfg["layer_split"], [13, 26, 38])
        self.assertEqual(tool.tune_config(cfg, "aggressive", prefill="4096", pool_workers=8, reserve_mib=1280,
                                          batch_groups="1"), cfg)
        # a run without them goes back to the profile's values
        again = tool.tune_config(cfg, "aggressive")
        self.assertEqual(tool.option_values(again["args"], "--prefill"), ["2048"])
        self.assertEqual(tool.option_values(again["args"], "--pool-workers"), ["6"])
        self.assertEqual(tool.option_values(again["args"], "--vram-reserve-mib"), ["1024"])
        self.assertEqual(tool.option_values(again["args"], "--batch-groups"), ["auto"])

    def test_tuning_step_overrides_refused(self):
        for kw in ({"prefill": "8192"}, {"prefill": "0"}, {"pool_workers": 0}, {"pool_workers": 9},
                   {"pool_workers": True}, {"reserve_mib": 0}, {"reserve_mib": 700}, {"reserve_mib": 1023},
                   {"reserve_mib": 16384}, {"reserve_mib": "1536"},
                   {"batch_groups": 3}, {"batch_groups": True}, {"batch_groups": "8"}, {"batch_groups": 1.0}):
            with self.subTest(kw=kw), self.assertRaises(tool.ConfigError):
                tool.tune_config(fixture(), **kw)

    def test_value_bounds(self):
        for kw in ({"reserve_mib": 1024}, {"reserve_mib": 16383}, {"park_mib": 12288}, {"park_mib": 0}):
            with self.subTest(kw=kw):
                tool.tune_config(fixture(), **kw)
        with self.assertRaises(tool.ConfigError):
            tool.tune_config(fixture(), park_mib=12289)

    def test_tuning_idempotent(self):
        for profile in tool.PROFILES:
            with self.subTest(profile=profile):
                once = tool.tune_config(fixture(), profile)
                self.assertEqual(tool.tune_config(once, profile), once)

    def test_invalid_modes_refused(self):
        changes = [
            {"exe": "/opt/strata/engine/strata"},
            {"backend": "hip"},
            {"replicas": 2}, {"replicas": True}, {"replicas": 1.0},
            {"vision": {"exe": "/encoder"}}, {"vision": True},
            {"env": {"CUDA_VISIBLE_DEVICES": "3,2,1,0"}},
            {"env": ["not", "an", "object"]},
            {"model_name": "qwen3.8-flash-next-iq3_s"},
        ]
        for change in changes:
            with self.subTest(change=change):
                cfg = fixture()
                cfg.update(change)
                with self.assertRaises(tool.ConfigError):
                    tool.tune_config(cfg)
        for extra in (["--peer-device", "1"], ["--resident-budget-gib", "40"], ["--gpu", "0,1"], ["--gpu=0"],
                      ["--cpu-prefill", "1"], ["--vision"], ["--no-ple"], ["--native-extra=1"]):
            with self.subTest(extra=extra):
                cfg = fixture()
                cfg["args"] += extra
                with self.assertRaises(tool.ConfigError):
                    tool.tune_config(cfg)

    def test_accepted_variants(self):
        for change in ({"replicas": 1}, {"replicas": None}, {"replicas": False}, {"vision": None}, {"env": None}):
            with self.subTest(change=change):
                cfg = fixture()
                cfg.update(change)
                self.assertEqual(tool.tune_config(cfg)["env"]["STRATA_KV_GROW"], "0")
        cfg = fixture()
        cfg["args"] += ["--prefill=4096"]            # a controlled flag's = form is stripped, not passed on
        self.assertEqual(tool.option_values(tool.tune_config(cfg)["args"], "--prefill"), ["2048"])

    def test_equals_form_of_kept_flag_refused(self):
        cfg = fixture()
        i = cfg["args"].index("--native")
        cfg["args"][i:i + 2] = ["--native=" + cfg["args"][i + 1]]
        with self.assertRaises(tool.ConfigError):
            tool.tune_config(cfg)

    def test_other_native_quant_refused(self):
        cfg = fixture()
        i = cfg["args"].index("--native") + 1
        cfg["args"][i] = cfg["args"][i].replace("IQ3_XXS", "IQ2_XS")
        with self.assertRaises(tool.ConfigError):
            tool.tune_config(cfg)

    def test_coder_and_swift_detected_by_folders(self):
        for flag, value in (("--pack", "/data/packs/coder-iq3_xxs"), ("--expert-profile", "/s/data/expert-profile-coder.bin"),
                            ("--pack", "/data/packs/swift-iq3_xxs"),
                            ("--native", "/data/models/coder-IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf")):
            with self.subTest(flag=flag, value=value):
                cfg = fixture()
                cfg["model_name"] = "my-renamed-model"
                cfg["args"][cfg["args"].index(flag) + 1] = value
                with self.assertRaises(tool.ConfigError):
                    tool.tune_config(cfg)

    def test_bad_controlled_options_refused(self):
        for extra in (["--pool-workers"], ["--pool-workers", "--prefill", "2048"],
                      ["--prefill="], ["--batch-mtp=1"]):
            cfg = fixture()
            cfg["args"] += extra
            with self.subTest(extra=extra), self.assertRaises(tool.ConfigError):
                tool.tune_config(cfg)

    def test_unknown_filename_does_not_guess_model(self):
        cfg = fixture()
        cfg["model_name"] = "my-research-model"
        i = cfg["args"].index("--native") + 1
        cfg["args"][i] = "/models/model.gguf"
        self.assertEqual(tool.option_values(tool.tune_config(cfg)["args"], "--native"),
                         ["/models/model.gguf"])

    def test_server_lifted_and_user_folders_accepted(self):
        cfg = fixture()
        cfg["args"] += ["--reasoning-effort=high"]          # server.py lifts it out of args (#1641), both forms
        self.assertIn("--reasoning-effort=high", tool.tune_config(cfg)["args"])
        cfg = fixture()
        cfg["args"][cfg["args"].index("--native") + 1] = "/data/team-coder/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
        cfg["args"][cfg["args"].index("--expert-profile") + 1] = "/data/my-coder-calibrated-profile.bin"
        self.assertEqual(tool.tune_config(cfg)["layer_split"], [13, 26, 38])

    def test_more_rope_and_yarn_flags_stripped(self):
        cfg = fixture()
        cfg["args"] += ["--rope-freq-base", "1e6", "--yarn-orig-ctx", "262144", "--yarn-ext-factor", "1"]
        args = tool.tune_config(cfg)["args"]
        for flag in ("--rope-freq-base", "--yarn-orig-ctx", "--yarn-ext-factor"):
            self.assertNotIn(flag, args)

    def test_changes_report_old_and_new_values(self):
        deployed = tool.tune_config(fixture())
        diff = tool.changes(deployed, tool.tune_config(deployed, "aggressive"))
        self.assertIn("--vram-reserve-mib: 1536 -> 1024", diff)
        self.assertEqual(tool.changes(deployed, tool.tune_config(deployed)), [])
        self.assertFalse(any("/models" in line or "private-fixture" in line for line in diff))


SETUP_JSON = Path(__file__).resolve().parents[1] / "config" / "strata-iq3_xxs.setup.json"


def setup_config() -> dict:
    """The config setup wrote on this machine: the model paths in setup's own layout."""
    return json.loads(SETUP_JSON.read_text(encoding="utf-8-sig"))


class ModelTests(unittest.TestCase):
    """--model: ISTA-DASLab's GSQ-RCO IQ3_XXS and IQ3_S, and setup's paths rewritten from one to the other."""
    D = "/home/thc1006/dev/Strata-data"

    def test_iq3_s_points_every_model_path_at_its_files(self):
        old = setup_config()
        ref = tool.tune_config(old)
        cfg = tool.tune_config(old, model="iq3_s")
        renamed = {
            f"{self.D}/models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf":
                f"{self.D}/models/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf",
            f"{self.D}/models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf":
                f"{self.D}/models/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf",
            f"{self.D}/packs/iq3_xxs": f"{self.D}/packs/iq3_s",
        }
        self.assertTrue(set(renamed) <= set(ref["args"]), "the setup snapshot no longer has setup's paths")
        self.assertEqual(cfg["args"], [renamed.get(a, a) for a in ref["args"]])
        self.assertEqual(cfg["tokenizer"], f"{self.D}/packs/iq3_s/tokenizer")
        self.assertEqual(cfg["model_name"], "qwen3.8-flash-next-iq3_s")
        self.assertEqual(cfg["layer_split"], [13, 25, 38])
        self.assertEqual(tool.model_of(cfg), "iq3_s")
        for key in set(ref) | set(cfg):                       # the log, exe, env, server fields: the deployment's
            if key not in ("args", "tokenizer", "model_name", "layer_split"):
                self.assertEqual(cfg.get(key), ref.get(key), key)
        self.assertEqual(old, setup_config(), "Input must not mutate")

    def test_switching_is_idempotent_and_reversible(self):
        old = setup_config()
        there = tool.tune_config(old, model="iq3_s")
        self.assertEqual(tool._canon(tool.tune_config(there, model="iq3_s")), tool._canon(there))
        self.assertEqual(tool._canon(tool.tune_config(there)), tool._canon(tool.tune_config(old)))

    def test_model_change_is_reported_and_critical(self):
        old = tool.tune_config(setup_config())
        new = tool.tune_config(old, model="iq3_s")
        diff = tool.changes(old, new)
        self.assertIn("model: iq3_xxs -> iq3_s", diff)
        self.assertIn("layer_split: [13, 26, 38] -> [13, 25, 38]", diff)
        self.assertIn("model: iq3_xxs -> iq3_s", tool.critical(old, diff))
        self.assertFalse(any("/home/" in line for line in diff), "no paths in the report")
        self.assertEqual(tool.changes(new, tool.tune_config(new, model="iq3_s")), [])

    def test_paths_outside_setups_layout_are_refused_not_guessed(self):
        with self.assertRaises(tool.ConfigError):             # the fixture's pack and PLE paths are not setup's
            tool.tune_config(fixture(), model="iq3_s")
        tool.tune_config(fixture())                           # while keeping its model is fine, as before
        cases = {
            "native in another folder": ("--native", lambda v: v.replace("/models/IQ3_XXS/", "/models/other/")),
            "PLE path names shard 1": ("--ple-gguf", lambda v: v.replace("00002-of-00002", "00001-of-00002")),
            "PLE in another folder": ("--ple-gguf", lambda v: v.replace("/models/IQ3_XXS/", "/models/")),
            "pack named differently": ("--pack", lambda v: v + "-copy"),
        }
        for name, (flag, change) in cases.items():
            with self.subTest(name):
                cfg = setup_config()
                i = cfg["args"].index(flag) + 1
                cfg["args"][i] = change(cfg["args"][i])
                with self.assertRaises(tool.ConfigError):
                    tool.tune_config(cfg, model="iq3_s")
        for key, value in (("tokenizer", "/elsewhere/tokenizer"), ("model_name", "my-model")):
            with self.subTest(key):
                cfg = setup_config()
                cfg[key] = value
                with self.assertRaises(tool.ConfigError):
                    tool.tune_config(cfg, model="iq3_s")

    def test_model_name_must_agree_with_the_files(self):
        cfg = tool.tune_config(setup_config(), model="iq3_s")
        cfg["model_name"] = "qwen3.8-flash-next-iq3_xxs"
        with self.assertRaises(tool.ConfigError):
            tool.validate_config(cfg)
        with self.assertRaises(tool.ConfigError):
            tool.tune_config(setup_config(), model="q4_0")

    def test_cli_dry_run_reports_the_switch_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strata-iq3_xxs.json"
            path.write_text(json.dumps(tool.tune_config(setup_config()), indent=2), encoding="utf-8")
            before = path.read_bytes()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = tool.main([str(path), "--profile", "deployed", "--model", "iq3_s", "--dry-run"])
            self.assertEqual(rc, 0)
            summary = json.loads(out.getvalue())
            self.assertEqual(summary["model"], "iq3_s")
            self.assertIn("model: iq3_xxs -> iq3_s", summary["critical_changes"])
            self.assertEqual(summary["layer_split"], [13, 25, 38])
            self.assertEqual(path.read_bytes(), before)


class FileTests(unittest.TestCase):
    def write(self, tmp: str, cfg: dict | None = None, mode: int = 0o664, bom: bool = False) -> tuple[Path, bytes]:
        path = Path(tmp) / "strata-iq3_xxs.json"
        data = (("\ufeff" if bom else "") + json.dumps(cfg or fixture(), ensure_ascii=False)).encode("utf-8")
        path.write_bytes(data)
        path.chmod(mode)
        return path, data

    def test_backup_atomic_write_mode_and_no_duplicate_backup_on_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, original = self.write(tmp, bom=True)      # 0664: mkstemp's 0600 must not leak through
            first = tool.update_file(path)
            self.assertTrue(first["changed"])
            self.assertTrue(first["backup_created"])
            self.assertEqual(path.with_name(path.name + ".before-p100-q3").read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o664)
            final = path.read_bytes()
            second = tool.update_file(path)
            self.assertFalse(second["changed"])
            self.assertFalse(second["backup_created"])
            self.assertEqual(path.read_bytes(), final)
            self.assertEqual(len(list(Path(tmp).glob("*.before-p100-q3*"))), 1)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_format_only_difference_is_rewritten_but_not_semantic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tuned = tool.tune_config(fixture())
            path = Path(tmp) / "strata-iq3_xxs.json"
            path.write_text(json.dumps(tuned, ensure_ascii=False, indent=1))     # how setup.py writes it
            r = tool.update_file(path, dry_run=True)
            self.assertEqual((r["changed"], r["semantic_change"], r["critical_changes"]), (True, False, []))
            tool.update_file(path)
            self.assertEqual(path.read_bytes(), (json.dumps(tuned, ensure_ascii=False, indent=2) + "\n").encode())
            self.assertFalse(tool.update_file(path, dry_run=True)["changed"])

    def test_backup_numbers_continue_after_the_highest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self.write(tmp)
            stem = path.name + ".before-p100-q3"
            for name in (stem, stem + ".2", stem + ".x"):
                path.with_name(name).write_text("old")
            tool.update_file(path)
            self.assertTrue(path.with_name(stem + ".3").exists())
            self.assertFalse(path.with_name(stem + ".1").exists())

    def test_backup_keeps_a_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self.write(tmp, mode=0o600)
            tool.update_file(path)
            self.assertEqual(stat.S_IMODE(path.with_name(path.name + ".before-p100-q3").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_concurrent_edit_refused_without_leftovers(self):
        for calls in (2, 3):              # changed before the backup, or after the new file was written
            with self.subTest(calls=calls), tempfile.TemporaryDirectory() as tmp:
                path, original = self.write(tmp)
                seen = [original] * (calls - 1) + [b"edited by someone else"]
                with mock.patch.object(Path, "read_bytes", side_effect=seen):
                    with self.assertRaises(tool.ConfigError):
                        tool.update_file(path)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [path.name])

    def test_cli_errors_exit_nonzero_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strata-iq3_xxs.json"
            path.write_bytes(b"{invalid}")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(tool.main([str(path)]), 2)
            self.assertIn("not valid UTF-8 JSON", err.getvalue())
            good, original = self.write(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tool.main([str(good), "--reserve-mib", "700"]), 2)
            self.assertEqual(good.read_bytes(), original)

    def test_python_equal_but_json_different_values_are_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            tuned = tool.tune_config(fixture())
            tuned.update({"parallel": 4.0, "layer_split": [13.0, 26, 38], "reasoning_close_retry": 1})
            path, _ = self.write(tmp, tuned)
            result = tool.update_file(path)
            self.assertTrue(result["changed"])
            written = json.loads(path.read_text())
            self.assertIs(type(written["parallel"]), int)
            self.assertIs(written["reasoning_close_retry"], True)
            self.assertEqual(written["layer_split"], [13, 26, 38])

    def test_profile_change_keeps_old_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, original = self.write(tmp)
            tool.update_file(path, profile="baseline")
            baseline = path.read_bytes()
            tool.update_file(path, profile="deployed")
            self.assertEqual(path.with_name(path.name + ".before-p100-q3").read_bytes(), original)
            self.assertEqual(path.with_name(path.name + ".before-p100-q3.1").read_bytes(), baseline)

    def test_read_only_config_refused(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        with tempfile.TemporaryDirectory() as tmp:
            path, original = self.write(tmp, mode=0o444)
            with self.assertRaises(tool.ConfigError):
                tool.update_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_reordered_args_are_not_a_semantic_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tuned = tool.tune_config(fixture())
            i = tuned["args"].index("--vram-reserve-mib")
            tuned["args"] = tuned["args"][:i] + tuned["args"][i + 2:] + tuned["args"][i:i + 2]   # the web Settings shape
            path, _ = self.write(tmp, tuned)
            r = tool.update_file(path, dry_run=True)
            self.assertTrue(r["changed"])
            self.assertFalse(r["semantic_change"])
            self.assertEqual(len(r["changes"]), 1)
            tuned["env"]["STRATA_KV_GROW"] = 0                   # a number where the tool writes "0": a real change
            path.write_text(json.dumps(tuned))
            r = tool.update_file(path, dry_run=True)
            self.assertTrue(r["semantic_change"])
            self.assertIn('env STRATA_KV_GROW: 0 -> "0"', r["changes"])

    def test_critical_versus_warning_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tuned = tool.tune_config(fixture())
            tuned["reasoning_budget_tokens"] = 2048                 # the web Settings view: a warning only
            path, _ = self.write(tmp, tuned)
            r = tool.update_file(path, dry_run=True)
            self.assertTrue(r["semantic_change"])
            self.assertEqual(r["critical_changes"], [])
            for edit, key in ((lambda c: c["args"].__setitem__(c["args"].index("--ple-io") + 1, "mmap"), "--ple-io"),
                              (lambda c: c.__setitem__("parallel", 2), "parallel"),
                              (lambda c: c["args"].__setitem__(c["args"].index("--vram-reserve-mib") + 1, "700"),
                               "--vram-reserve-mib is 700"),
                              (lambda c: c.__setitem__("lazy_load", True), "lazy_load"),
                              (lambda c: c["args"].__setitem__(c["args"].index("--conversation-cache-mib") + 1, "12288"),
                               "--conversation-cache-mib")):
                with self.subTest(key=key):
                    bad = tool.tune_config(fixture())
                    edit(bad)
                    path.write_text(json.dumps(bad))
                    crit = tool.update_file(path, dry_run=True)["critical_changes"]
                    self.assertTrue(any(c.startswith(key) for c in crit), crit)

    def test_only_tuning_steps_and_web_settings_fields_are_note_only(self):
        def arg(flag, value):
            return lambda c: c["args"].__setitem__(c["args"].index(flag) + 1, value)

        def drop(flag, n=1):
            return lambda c: c["args"].__delitem__(slice(c["args"].index(flag), c["args"].index(flag) + n))

        refused = {
            "--batch-groups 1": arg("--batch-groups", "1"),
            "layer_split equal": lambda c: c.__setitem__("layer_split", [12, 24, 36]),
            "--no-ple-prefetch removed": drop("--no-ple-prefetch"),
            "--ple-io mmap": arg("--ple-io", "mmap"),
            "--conversation-cache-mib 4096": arg("--conversation-cache-mib", "4096"),
            "--conversation-cache-min-tokens 0": arg("--conversation-cache-min-tokens", "0"),
            "--expert-cache 4000": arg("--expert-cache", "4000"),
            "--spec 0": arg("--spec", "0"),
            "--no-kv-grow removed": drop("--no-kv-grow"),
            "--vram-reserve-later-mib 0": lambda c: c["args"].extend(["--vram-reserve-later-mib", "0"]),
            "--vram-reserve-mib missing": drop("--vram-reserve-mib", 2),
            "--vram-reserve-mib 700": arg("--vram-reserve-mib", "700"),
            "--vram-reserve-mib abc": arg("--vram-reserve-mib", "abc"),
            "--vram-reserve-mib superscript": arg("--vram-reserve-mib", "\u00b2"),
            "--vram-reserve-mib 16384": arg("--vram-reserve-mib", "16384"),
            "--vram-reserve-mib atoi wrap": arg("--vram-reserve-mib", "4294968296"),
            "--rope-scaling yarn": lambda c: c["args"].extend(["--rope-scaling", "yarn"]),
            "env STRATA_BATCH_MTP 1": lambda c: c["env"].__setitem__("STRATA_BATCH_MTP", "1"),
            "env STRATA_KV_GROW 1": lambda c: c["env"].__setitem__("STRATA_KV_GROW", "1"),
            "split_skip_if_fits": lambda c: c.__setitem__("split_skip_if_fits", True),
            "gpu_order": lambda c: c.__setitem__("gpu_order", "pci_bus_id"),
            "parallel 4.0": lambda c: c.__setitem__("parallel", 4.0),
            "lazy_load true": lambda c: c.__setitem__("lazy_load", True),
            "--prefill=2048 form": lambda c: c["args"].__setitem__(slice(c["args"].index("--prefill"),
                                                                       c["args"].index("--prefill") + 2), ["--prefill=2048"]),
        }
        allowed = {
            "--prefill auto": arg("--prefill", "auto"),
            "--pool-workers 4": arg("--pool-workers", "4"),
            "--vram-reserve-mib 2048": arg("--vram-reserve-mib", "2048"),
            "--vram-reserve-mib 16383": arg("--vram-reserve-mib", "16383"),
            "--vram-reserve-mib 0001536": arg("--vram-reserve-mib", "0001536"),     # atoi reads 1536
            "reasoning_effort": lambda c: c.__setitem__("reasoning_effort", "high"),
            "reasoning_budget_tokens": lambda c: c.__setitem__("reasoning_budget_tokens", 2048),
            "fit_max_tokens": lambda c: c.__setitem__("fit_max_tokens", True),
            "reasoning_close_retry": lambda c: c.__setitem__("reasoning_close_retry", False),
            "open_browser": lambda c: c.__setitem__("open_browser", True),
            "lazy_load 1": lambda c: c.__setitem__("lazy_load", 1),        # server.py: only True is lazy
            "--reasoning-effort=high form": lambda c: c["args"].append("--reasoning-effort=high"),   # server.py lifts it
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strata-iq3_xxs.json"
            for expect_refused, cases in ((True, refused), (False, allowed)):
                for name, edit in cases.items():
                    with self.subTest(name=name):
                        cfg = tool.tune_config(fixture())
                        edit(cfg)
                        path.write_text(json.dumps(cfg))
                        crit = tool.update_file(path, dry_run=True)["critical_changes"]
                        self.assertEqual(bool(crit), expect_refused, crit)

    def test_missing_reserve_names_the_engine_default(self):
        cfg = tool.tune_config(fixture())
        i = cfg["args"].index("--vram-reserve-mib")
        del cfg["args"][i:i + 2]
        crit = tool.critical(cfg, tool.changes(cfg, tool.tune_config(cfg)))
        self.assertEqual(crit, ["--vram-reserve-mib is missing: the engine uses 700 MiB (below 1024 MiB: issue #2065)"])

    def test_every_profile_passes_preflight_once_written(self):
        for profile in tool.PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                path, _ = self.write(tmp)
                tool.update_file(path, profile=profile)
                r = tool.update_file(path, profile=profile, dry_run=True)
                self.assertEqual((r["changed"], r["critical_changes"]), (False, []))

    def test_failed_mkstemp_leaves_no_orphan_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, original = self.write(tmp)
            with mock.patch.object(tool.tempfile, "mkstemp", side_effect=OSError(28, "no space")):
                with self.assertRaises(OSError):
                    tool.update_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [path.name])

    def test_failed_replace_leaves_no_orphan_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, original = self.write(tmp)
            with mock.patch.object(tool.os, "replace", side_effect=OSError(5, "simulated")):
                with self.assertRaises(OSError):
                    tool.update_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [path.name])

    def test_dry_run_and_cli_output_do_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, original = self.write(tmp)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = tool.main([str(path), "--profile", "aggressive", "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
            self.assertNotIn("private-fixture", stdout.getvalue())
            self.assertNotIn("/models", stdout.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["ple_io"], "ram")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = tool.main([str(path), "--prefill", "1024", "--pool-workers", "4", "--reserve-mib", "1280",
                                "--batch-groups", "1", "--dry-run"])
            self.assertEqual(rc, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual((result["prefill"], result["pool_workers"], result["vram_reserve_mib"],
                              result["batch_groups"]), ("1024", 4, 1280, "1"))
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_json_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strata-iq3_xxs.json"
            path.write_bytes(b"{invalid}")
            with self.assertRaises(tool.ConfigError):
                tool.update_file(path)
            self.assertEqual(path.read_bytes(), b"{invalid}")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_nonobject_config_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strata-iq3_xxs.json"
            path.write_text("[]")
            with self.assertRaises(tool.ConfigError):
                tool.update_file(path)
            self.assertEqual(path.read_text(), "[]")


@unittest.skipUnless((STRATA / ".venv" / "bin" / "python").exists() and (STRATA / "serve" / "server.py").exists(),
                     "the pinned Strata checkout is not here")
class ServerIntegrationTests(unittest.TestCase):
    """The tuned args through the real server.py, and every emitted flag against the engine's own parser."""

    PROBE = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import server\n"
        "cfg = json.load(sys.stdin)\n"
        "print(json.dumps({'args': server.engine_args(cfg), 'split': server.layer_split_value(cfg)}))\n"
    )

    def test_server_builds_a_valid_engine_command(self):
        engine_src = (STRATA / "src" / "program" / "generate.cpp").read_text(errors="replace")
        for profile in tool.PROFILES:
            with self.subTest(profile=profile):
                cfg = tool.tune_config(fixture(), profile)
                r = subprocess.run([str(STRATA / ".venv" / "bin" / "python"), "-c", self.PROBE, str(STRATA / "serve")],
                                   input=json.dumps(cfg), capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                out = json.loads(r.stdout.strip().splitlines()[-1])
                args = out["args"]
                self.assertEqual(out["split"], ",".join(map(str, cfg["layer_split"])))
                self.assertEqual(tool.option_values(args, "--batch"), ["4"])
                self.assertEqual(tool.option_values(args, "--layer-split"), [out["split"]])
                for flag in [a for a in args if a.startswith("--")]:
                    self.assertTrue(re.search(r'"' + re.escape(flag) + r'"', engine_src) or flag == "--custom-future-option",
                                    f"{flag} is not an engine option")
                controlled = [a for a in args if a in tool.CONTROLLED_VALUES | tool.CONTROLLED_SWITCHES]
                self.assertEqual(len(controlled), len(set(controlled)), "a controlled flag appears twice")


if __name__ == "__main__":
    unittest.main()
