#!/usr/bin/env python3
"""Tune an existing Strata IQ3_XXS configuration for four P100 GPUs.

Audited source: a0116b2782a5b82622358c04a06496485d12b2d6.
This edits configuration only. It does not install, start, or benchmark Strata.

Profiles:
  deployed    what this machine runs (measured, results/): PLE table locked in RAM without read-ahead hints,
              layer cuts 13,26,38, 1536 MiB VRAM reserve per card, 8192 MiB of host RAM for parked conversations
  baseline    the original setup guide's first step: PLE mmap, equal cuts 12,24,36, 1536 MiB reserve
  aggressive  the original setup guide's second step as written: PLE ram, cuts 13,26,38, 1024 MiB reserve (not
              adopted here: it buys no experts on this box - all 24,576 fit at 1536 - and moves the last card toward
              the <500 MiB free VRAM where issue #2065 hangs)
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

SOURCE_COMMIT = "a0116b2782a5b82622358c04a06496485d12b2d6"
PROFILES = {
    "deployed": {"reserve": 1536, "prefill": "2048", "ple_io": "ram", "split": "last-light", "park_mib": 8192},
    "baseline": {"reserve": 1536, "prefill": "2048", "ple_io": "mmap", "split": "equal", "park_mib": 0},
    "aggressive": {"reserve": 1024, "prefill": "2048", "ple_io": "ram", "split": "last-light", "park_mib": 0},
}
# Conversation parking (docs/DETAILS.md "Multiple conversations"): up to PARK_SLOTS conversations of at least
# PARK_MIN_TOKENS tokens kept in host RAM, so a conversation whose batch slot was taken by other requests comes back
# without reading its history again (measured here, 6 clients on 4 slots: 0 -> all of the history reused per turn).
PARK_SLOTS, PARK_MIN_TOKENS = 8, 1024
# the engine stops parking when MemAvailable would fall below this (default 2560 MiB): 8 GiB keeps the original guide's
# headroom for the OS and the lab's other users while conversations are parked
PARK_MIN_FREE_MIB = 8192
PARK_MIB_MAX = 12288                 # RAM beside the locked PLE table (~20 GiB available after load)
CUTS = {"equal": [12, 24, 36], "last-light": [13, 26, 38]}
# Tuning-step overrides (the original guide's tuning order): one value changed at a time, kept by a later re-run only
# when given again.
PREFILL_CHOICES = ("1024", "2048", "4096", "auto")
# auto (default): the engine pipelines one group per stage (4 slots on 4 stages = 4 groups of 1) and, unlike a fixed 4,
# still divides the slots when VRAM holds fewer than 4.  1 = serial windows (measured here: -40% decode with 4 users).
BATCH_GROUPS_CHOICES = ("auto", "1", "2", "4")
POOL_WORKERS_RANGE = (1, 8)          # 8 vCPUs; the four stages share one expert pool
# The floor strata-preflight.sh enforces (RESERVE_CRITICAL_BELOW_MIB): a lower reserve would be written and then refused
# at the next start.  A P100 has 16384 MiB, so a reserve must be below that.
RESERVE_MIB_MIN, CARD_MIB = 1024, 16384

# These values are rebuilt once; every unrelated option and model path stays.
CONTROLLED_VALUES = {
    "--batch", "--slots", "--batch-groups", "--max-context", "--kv",
    "--expert-cache", "--prefill", "--pool-workers", "--vram-reserve-mib",
    "--ple-io", "--spec", "--layer-split", "--kv-resident", "--adapt-async",
    "--pipeline-windows", "--vram-reserve-later-mib",
    "--conversation-cache-mib", "--conversation-cache-slots", "--conversation-cache-min-tokens",
    "--conversation-cache-min-free-mib",
    "--rope-scaling", "--rope-scale",      # only for contexts past the trained 262144; this profile serves 32768
    "--rope-freq-base", "--rope-freq-scale", "--yarn-orig-ctx", "--yarn-ext-factor", "--yarn-attn-factor",
    "--yarn-beta-fast", "--yarn-beta-slow",
}
CONTROLLED_SWITCHES = {
    "--trim-stage-weights", "--resident-experts", "--resident-cpu-experts",
    "--mmap-experts", "--batch-mtp", "--split-skip-if-fits",
    "--kv-grow", "--no-kv-grow", "--no-ple-prefetch",
}
# Refused: they place work on other GPUs or budgets, select devices behind the server's back, or are not engine
# options at this pin.
REFUSED_FLAGS = {
    "--peer-device": "Helper-GPU placement conflicts with this layer-split profile",
    "--split-device": "Helper-GPU placement conflicts with this layer-split profile",
    "--expert-cache-device1": "Helper-GPU placement conflicts with this layer-split profile",
    "--expert-cache-device2": "Helper-GPU placement conflicts with this layer-split profile",
    "--expert-cache-device3": "Helper-GPU placement conflicts with this layer-split profile",
    "--resident-budget-gib": "A RAM budget conflicts with this profile's resident experts",
    "--gpu": "--gpu in args overrides the server's device selection; use the config's gpu field",
    "--cpu-prefill": "CPU prefill uses STRATA_PREFILL_CPU_SHARE, not an engine flag",
    "--cpu-prefill-share": "CPU prefill uses STRATA_PREFILL_CPU_SHARE, not an engine flag",
    "--cpu-share": "CPU prefill uses STRATA_PREFILL_CPU_SHARE, not an engine flag",
    "--no-ple": "An ablation or unsupported engine switch is present; regenerate the config",
    "--experimental-speed-projection": "An ablation or unsupported engine switch is present; regenerate the config",
    "--vision": "This text-service profile requires setup --vision no",
}
# All of these have a numeric/string zero-off test in the audited source.
ENV_DEFAULTS = {
    "STRATA_RESIDENT_HEADROOM_GIB": "8",
    "STRATA_ROUTE_TAIL_SKIP": "0",
    "STRATA_BATCH_MTP": "0",
    "STRATA_BATCH_MTP_SERIAL": "0",
    "STRATA_PREFILL_CPU_SHARE": "0",
    "STRATA_ADAPT_NOWAIT": "0",
    "STRATA_ADAPT_OVERLAP": "0",
    "STRATA_ADAPT_OVERLAP_RESIDENT": "0",
    "STRATA_PIPELINE_ADAPT_ASYNC": "0",
    "STRATA_KV_GROW": "0",
}
# The differences a start may have and only warns about (strata-preflight.sh): the tuning steps (--prefill,
# --pool-workers, --vram-reserve-mib) and the fields the web Settings view edits.  Every other difference in
# changes() changes memory placement or what the engine computes, and the service refuses to start on it.  So does
# a first-card VRAM reserve under 1024 MiB, including the engine's default when --vram-reserve-mib is missing
# (issue #2065: <500 MiB free hangs); --vram-reserve-later-mib, the later cards' reserve, is never written by this
# tool, so any value of it is already a refused difference.
NOTE_ONLY_KEYS = ("--prefill", "--pool-workers", "--vram-reserve-mib", "reasoning_effort", "reasoning_budget_tokens",
                  "fit_max_tokens", "reasoning_close_retry", "open_browser")
RESERVE_CRITICAL_BELOW_MIB = 1024
ENGINE_DEFAULT_RESERVE_MIB = 700     # generate.cpp Options::vram_reserve_mib, used when the flag is not given
# flags server.py takes out of args itself (both "--x v" and "--x=v"; server.py lift_reasoning_effort_arg, #1641)
SERVER_LIFTED = {"--reasoning-effort"}
SERVER_FIELDS = ("cuda", "gpu", "gpu_order", "layer_split", "parallel", "split_skip_if_fits", "reasoning_effort",
                 "reasoning_budget_tokens", "fit_max_tokens", "reasoning_close_retry", "open_browser")


class ConfigError(ValueError):
    """Invalid or incompatible input; messages never include user secrets."""


def option_values(args: list[str], flag: str) -> list[str]:
    values: list[str] = []
    for i, arg in enumerate(args):
        if arg == flag:
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                raise ConfigError(f"Missing value for {flag}")
            values.append(args[i + 1])
        elif arg.startswith(flag + "="):
            values.append(arg.partition("=")[2])
    return values


def _is_one(v: Any) -> bool:
    return type(v) is int and v == 1          # not True, not 1.0: the server's replicas check is as strict


def validate_config(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        raise ConfigError("Configuration must be a JSON object")
    if not isinstance(cfg.get("exe"), str) or not cfg["exe"]:
        raise ConfigError("A generated Strata config must contain exe")
    args = cfg.get("args")
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ConfigError("args must be an array of strings")
    env = cfg.get("env")
    if env is not None and not isinstance(env, dict):
        raise ConfigError("env must be a JSON object")
    for key in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "HIP_VISIBLE_DEVICES"):
        if key in (env or {}):
            raise ConfigError(f"Remove {key} from config env; device selection belongs in gpu")
    if cfg.get("backend") not in (None, "cuda"):
        raise ConfigError("This profile requires a CUDA engine")
    replicas = cfg.get("replicas")
    if not (replicas is None or replicas is False or _is_one(replicas)):
        raise ConfigError("This profile is one shared model; remove replicas first")
    if cfg.get("vision") not in (None, False):
        raise ConfigError("This text-service profile requires setup --vision no")
    if Path(cfg["exe"]).parent.name != "engine-cuda12":
        raise ConfigError("exe must point to engine-cuda12/strata; rerun setup --cuda 12")
    for x in args:
        flag = x.partition("=")[0]
        if flag in REFUSED_FLAGS:
            raise ConfigError(REFUSED_FLAGS[flag])
        # the engine matches flags exactly (generate.cpp: "unknown argument"); a controlled flag's = form is
        # stripped below, any other one would stop the engine at start
        if x.startswith("--") and "=" in x and flag not in CONTROLLED_VALUES | CONTROLLED_SWITCHES | SERVER_LIFTED:
            raise ConfigError(f"{flag}=...: the engine does not accept --flag=value; give the value separately")
    native = option_values(args, "--native")
    if len(native) != 1:
        raise ConfigError("Expected exactly one generated --native model path")
    # Use only reliable, explicit metadata. An unknown name is left untouched.
    native_name = Path(native[0]).name.lower()
    model_name = str(cfg.get("model_name") or "").lower()
    if "qwen3.8-flash-next" in native_name:
        if "iq3_xxs" not in native_name or "gsq-rco" not in native_name:
            raise ConfigError("Expected the Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS model")
        if "swift" in native_name or "coder" in native_name:
            raise ConfigError("This runbook targets the original Flash-Next model")
    # The Coder's files carry the original's names; its folders and profile do not (setup.py FAMILIES: tag "coder-",
    # profile expert-profile-coder.bin; Swift: "swift-")
    dirs = [Path(native[0]).parent.name.lower()] + [Path(p).name.lower() for p in option_values(args, "--pack")]
    profiles = [Path(p).name.lower() for p in option_values(args, "--expert-profile")]
    if any(re.fullmatch(r"(coder|swift)-[a-z0-9_]+", d) for d in dirs) or \
            any(p in ("expert-profile-coder.bin", "expert-profile-swift.bin") for p in profiles):
        raise ConfigError("This runbook targets the original Flash-Next model")
    if model_name.startswith(("swift-", "qwen3.8-flash-next-coder")):
        raise ConfigError("This runbook targets the original Flash-Next model")
    if model_name.startswith("qwen3.8-flash-next-") and not model_name.endswith("iq3_xxs"):
        raise ConfigError("The generated model name identifies a different quantization")


def tune_config(cfg: dict[str, Any], profile: str = "deployed",
                ple_io: str | None = None, split: str | None = None, prefill: str | None = None,
                pool_workers: int | None = None, reserve_mib: int | None = None,
                batch_groups: str | int | None = None, park_mib: int | None = None) -> dict[str, Any]:
    validate_config(cfg)
    if profile not in PROFILES:
        raise ConfigError("Unknown profile")
    settings = PROFILES[profile]
    ple_io = ple_io or str(settings["ple_io"])
    split = split or str(settings["split"])
    if ple_io not in ("mmap", "ram") or split not in CUTS:
        raise ConfigError("Invalid PLE mode or split")
    prefill = str(prefill) if prefill is not None else str(settings["prefill"])
    if prefill not in PREFILL_CHOICES:
        raise ConfigError(f"--prefill must be one of {', '.join(PREFILL_CHOICES)}")
    pool_workers = 6 if pool_workers is None else pool_workers
    if isinstance(pool_workers, bool) or not isinstance(pool_workers, int) or \
            not POOL_WORKERS_RANGE[0] <= pool_workers <= POOL_WORKERS_RANGE[1]:
        raise ConfigError(f"--pool-workers must be {POOL_WORKERS_RANGE[0]}..{POOL_WORKERS_RANGE[1]}")
    reserve_mib = int(settings["reserve"]) if reserve_mib is None else reserve_mib
    if isinstance(reserve_mib, bool) or not isinstance(reserve_mib, int) or not RESERVE_MIB_MIN <= reserve_mib < CARD_MIB:
        raise ConfigError(f"--reserve-mib must be {RESERVE_MIB_MIN}..{CARD_MIB - 1}")
    if isinstance(batch_groups, bool) or (batch_groups is not None and not isinstance(batch_groups, (int, str))):
        raise ConfigError(f"--batch-groups must be one of {', '.join(BATCH_GROUPS_CHOICES)}")
    park_mib = int(settings["park_mib"]) if park_mib is None else park_mib
    if isinstance(park_mib, bool) or not isinstance(park_mib, int) or not 0 <= park_mib <= PARK_MIB_MAX:
        raise ConfigError(f"--conversation-cache-mib must be 0..{PARK_MIB_MAX}")
    batch_groups = "auto" if batch_groups is None else str(batch_groups)
    if batch_groups not in BATCH_GROUPS_CHOICES:
        raise ConfigError(f"--batch-groups must be one of {', '.join(BATCH_GROUPS_CHOICES)}")
    args = cfg["args"]
    kept: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, equals, value = arg.partition("=")
        if flag in CONTROLLED_VALUES:
            if equals:
                if not value:
                    raise ConfigError(f"Missing value for {flag}")
                i += 1
            else:
                if i + 1 >= len(args) or args[i + 1].startswith("--"):
                    raise ConfigError(f"Missing value for {flag}")
                i += 2
        elif flag in CONTROLLED_SWITCHES:
            if equals:
                raise ConfigError(f"{flag} is a switch; do not assign a value")
            i += 1
        else:
            kept.append(arg)
            i += 1
    values = {
        "--max-context": "32768",
        "--kv": "int8",
        "--expert-cache": "auto",
        "--prefill": prefill,
        "--pool-workers": str(pool_workers),
        "--batch-groups": batch_groups,
        "--vram-reserve-mib": str(reserve_mib),
        "--ple-io": ple_io,
        "--spec": "4",
    }
    if park_mib > 0:
        values.update({"--conversation-cache-mib": str(park_mib), "--conversation-cache-slots": str(PARK_SLOTS),
                       "--conversation-cache-min-tokens": str(PARK_MIN_TOKENS),
                       "--conversation-cache-min-free-mib": str(PARK_MIN_FREE_MIB)})
    for flag, value in values.items():
        kept.extend((flag, value))
    kept.extend(("--resident-experts", "--trim-stage-weights", "--no-kv-grow"))
    # A locked PLE table needs no read-ahead, and on ZFS the prompt path's per-row MADV_WILLNEED (ngram.cpp) becomes
    # dmu_prefetch of whole 128K records: measured here, ~15.5 GB of disk reads for one 16K-token prompt.
    if ple_io == "ram":
        kept.append("--no-ple-prefetch")
    out = copy.deepcopy(cfg)
    out["args"] = kept
    out.update({
        "cuda": 12,
        "gpu": [0, 1, 2, 3],
        "gpu_order": "as_given",
        "layer_split": CUTS[split].copy(),
        "parallel": 4,
        "split_skip_if_fits": False,
        "reasoning_effort": "medium",
        "reasoning_budget_tokens": 1024,
        "fit_max_tokens": False,
        "reasoning_close_retry": True,
        "open_browser": False,
    })
    env = dict(out.get("env") or {})
    env.update(ENV_DEFAULTS)
    out["env"] = env
    return out


def _canon(cfg: Any) -> str:
    """Equal JSON text, not equal Python values: 4.0 == 4 and 1 == True in Python, but the server rejects
    "parallel": 4.0 and reads "reasoning_close_retry": 1 as off."""
    return json.dumps(cfg, sort_keys=True, ensure_ascii=False)


def _flag_state(args: list[str], flag: str) -> Any:
    if flag in CONTROLLED_SWITCHES:
        return flag in args
    try:
        v = option_values(args, flag)
    except ConfigError:
        return "?"
    return v[-1] if v else None


def critical(old: dict[str, Any], diff: list[str]) -> list[str]:
    """The entries of diff (from changes(old, ...)) that must stop a start, plus the conditions changes() cannot see."""
    out = [d for d in diff if d.split(":", 1)[0] not in NOTE_ONLY_KEYS]
    reserve = _flag_state(old.get("args") or [], "--vram-reserve-mib")
    if reserve is None:
        out.append(f"--vram-reserve-mib is missing: the engine uses {ENGINE_DEFAULT_RESERVE_MIB} MiB "
                   f"(below {RESERVE_CRITICAL_BELOW_MIB} MiB: issue #2065)")
    # the engine reads it with atoi: only digits count, and the upper bound also covers numbers atoi would wrap
    # (4294968296 becomes 1000)
    elif not re.fullmatch(r"[0-9]+", str(reserve)) or not RESERVE_CRITICAL_BELOW_MIB <= int(reserve) < CARD_MIB:
        out.append(f"--vram-reserve-mib is {reserve}, not {RESERVE_CRITICAL_BELOW_MIB}..{CARD_MIB - 1} "
                   f"(below {RESERVE_CRITICAL_BELOW_MIB} MiB: issue #2065)")
    if old.get("lazy_load") is True:
        out.append("lazy_load is true: the model would not load at start")
    # server.py passes args through (only --reasoning-effort is lifted out) and the engine rejects --flag=value as an
    # unknown argument (generate.cpp), so such a form in the live config is a failed start, not a formatting detail
    eq = sorted({a.partition("=")[0] for a in old.get("args") or []
                 if a.startswith("--") and "=" in a and a.partition("=")[0] not in SERVER_LIFTED})
    if eq:
        out.append("args use --flag=value, which the engine rejects: " + ", ".join(eq))
    return out


def changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What a write changes, as old -> new, for the settings this tool controls (no paths, no secrets)."""
    out = []
    oa, na = old.get("args") or [], new["args"]
    for flag in sorted(CONTROLLED_VALUES | CONTROLLED_SWITCHES):
        a, b = _flag_state(oa, flag), _flag_state(na, flag)
        if _canon(a) != _canon(b):
            out.append(f"{flag}: {a} -> {b}")
    for key in SERVER_FIELDS:
        if _canon(old.get(key)) != _canon(new.get(key)):
            out.append(f"{key}: {_canon(old.get(key))} -> {_canon(new.get(key))}")
    for key in sorted(ENV_DEFAULTS):
        a, b = (old.get("env") or {}).get(key), new["env"].get(key)
        if _canon(a) != _canon(b):
            out.append(f"env {key}: {_canon(a)} -> {_canon(b)}")
    return out


def _backup(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None = None) -> Path:
    """<name>.before-p100-q3, then .1, .2, ...: one past the highest existing number, so the highest is the newest."""
    stem = path.name + ".before-p100-q3"
    taken = [int(m.group(1) or 0) for p in path.parent.iterdir()
             if (m := re.fullmatch(re.escape(stem) + r"(?:\.([0-9]+))?", p.name))]
    start = max(taken) + 1 if taken else 0
    for n in range(start, start + 1000):
        backup = path.with_name(stem + (f".{n}" if n else ""))
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
        if owner is not None:                          # a 0600 config with an api_key stays readable by its owner
            os.fchown(fd, *owner)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return backup
    raise ConfigError("Cannot create a new backup; too many previous backups")


def update_file(path: Path, *, profile: str = "deployed", ple_io: str | None = None,
                split: str | None = None, prefill: str | None = None, pool_workers: int | None = None,
                reserve_mib: int | None = None, batch_groups: str | int | None = None,
                park_mib: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    path = path.resolve(strict=True)
    original = path.read_bytes()
    try:
        cfg = json.loads(original.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError("Configuration is not valid UTF-8 JSON") from e
    tuned = tune_config(cfg, profile, ple_io, split, prefill, pool_workers, reserve_mib, batch_groups, park_mib)
    try:
        payload = (json.dumps(tuned, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except UnicodeEncodeError as e:
        raise ConfigError("Configuration contains text that cannot be written as UTF-8") from e
    # changed: the file's bytes would change, so a write is needed (scripts/check-drift.sh compares bytes).  semantic:
    # a value the engine or server would see differently.  Indentation, a BOM, reordered args, a repeated arg with the
    # same value or an = form of a controlled flag are rewritten when written, but mean the same run.
    changed = payload != original
    diff = changes(cfg, tuned)
    semantic = bool(diff)
    if changed and not diff:
        diff = ["format: indentation, args order, repeats or --flag=value forms normalised (same values)"]
    backup: Path | None = None
    warnings: list[str] = []
    if changed and not dry_run:
        st = path.stat()
        mode = stat.S_IMODE(st.st_mode)
        if not os.access(path, os.W_OK):
            raise ConfigError("Configuration is read-only; make it writable first if the change is intended")
        # Refuse a concurrent edit rather than silently overwriting one.
        if path.read_bytes() != original:
            raise ConfigError("Configuration changed during this run; retry")
        replaced = False
        temp: Path | None = None
        try:
            backup = _backup(path, original, mode, (st.st_uid, st.st_gid) if os.geteuid() == 0 else None)
            fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
            temp = Path(name)
            with os.fdopen(fd, "wb") as f:                 # closes fd whatever fails below
                os.fchmod(f.fileno(), mode)
                if os.geteuid() == 0:                      # run under sudo: keep the owner's file the owner's
                    os.fchown(f.fileno(), st.st_uid, st.st_gid)
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if path.read_bytes() != original:
                raise ConfigError("Configuration changed during this run; retry")
            os.replace(temp, path)
            replaced = True
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as e:                       # the file is already in place: say so, do not fail
                warnings.append(f"directory fsync failed ({type(e).__name__}); the new file is in place")
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if not replaced and backup is not None:    # nothing was written: no orphan backup
                backup.unlink(missing_ok=True)
                backup = None
    # Do not print the config, env, or model paths: these may contain credentials.
    return {
        "changed": changed,
        "semantic_change": semantic,
        "critical_changes": critical(cfg, diff if semantic else []),
        "dry_run": dry_run,
        "profile": profile,
        "changes": diff,
        "gpu": tuned["gpu"],
        "layer_split": tuned["layer_split"],
        "slots_requested": tuned["parallel"],
        "context_per_slot": 32768,
        "kv": "int8",
        "prefill": option_values(tuned["args"], "--prefill")[0],
        "pool_workers": int(option_values(tuned["args"], "--pool-workers")[0]),
        "batch_groups": option_values(tuned["args"], "--batch-groups")[0],
        "vram_reserve_mib": int(option_values(tuned["args"], "--vram-reserve-mib")[0]),
        "ple_io": option_values(tuned["args"], "--ple-io")[0],
        "ple_prefetch": "--no-ple-prefetch" not in tuned["args"],
        "conversation_cache_mib": int((option_values(tuned["args"], "--conversation-cache-mib") or ["0"])[0]),
        "backup_created": backup is not None,
        "warnings": warnings,
        "source_commit": SOURCE_COMMIT,
        "generation_defaults": {"reasoning_effort": "medium", "reasoning_budget_tokens": 1024},
        "request_max_tokens": 4096,
        "notes": [
            "Set max_tokens=4096 per request; a top-level model-config max_tokens is ignored.",
            "Reasoning defaults are overridable; shared Chat settings may override them.",
            "Confirm actual batch_slots=4 after starting; this tool does not verify GPU capacity.",
            "Start with systemctl (strata-q3.service), not ./setup.sh: setup's start and ./update.sh rewrite "
            "--prefill 2048 to auto (setup.py upgrade_config); re-run this tool after any setup or update.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path, help="Existing generated strata-iq3_xxs.json")
    parser.add_argument("--profile", choices=PROFILES, default="deployed")
    parser.add_argument("--ple-io", choices=("mmap", "ram"), default=None,
                        help="Override the profile's PLE placement")
    parser.add_argument("--split", choices=CUTS, default=None,
                        help="Override equal / last-stage-light layer cuts")
    parser.add_argument("--prefill", choices=PREFILL_CHOICES, default=None,
                        help="Tuning step: override the profile's 2048-token prompt chunk")
    parser.add_argument("--pool-workers", type=int, default=None,
                        help=f"Tuning step: CPU expert workers ({POOL_WORKERS_RANGE[0]}..{POOL_WORKERS_RANGE[1]}, default 6)")
    parser.add_argument("--reserve-mib", type=int, default=None,
                        help=f"Tuning step: VRAM reserve per card in MiB ({RESERVE_MIB_MIN}..{CARD_MIB - 1})")
    parser.add_argument("--batch-groups", choices=BATCH_GROUPS_CHOICES, default=None,
                        help="auto (default): pipelined slot groups, one per stage; 1: serial windows - a long "
                             "prompt's admission then lets the other slots decode between its chunks (batch_read.hpp)")
    parser.add_argument("--conversation-cache-mib", type=int, default=None,
                        help=f"host RAM for parked conversations (0 = off; deployed profile 8192, at most {PARK_MIB_MAX})")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report; do not write")
    ns = parser.parse_args(argv)
    try:
        summary = update_file(ns.config, profile=ns.profile, ple_io=ns.ple_io, split=ns.split,
                              prefill=ns.prefill, pool_workers=ns.pool_workers, reserve_mib=ns.reserve_mib,
                              batch_groups=ns.batch_groups, park_mib=ns.conversation_cache_mib, dry_run=ns.dry_run)
    except (ConfigError, OSError) as e:
        # OSError includes a user-controlled path; report only the error class/errno.
        message = str(e) if isinstance(e, ConfigError) else f"File operation failed ({type(e).__name__}, errno={e.errno})"
        print("Configuration update failed: " + message, file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
