"""Read, validate, and write scenario_config.yaml from the Design page.

Notebook 1 reads this file. The dashboard edits the common knobs and preserves
the advanced blocks (app_affinity, app_mix_override, mode_overrides,
mode_thresholds) untouched. Comments are not preserved — the form documents the
fields — and the previous file is backed up to scenario_config.yaml.bak first.
"""
import copy
from datetime import datetime

import yaml

from . import settings

DEFAULTS = {
    "simulation": {"duration": 600, "n_ue": 4, "random_seed": 42},
    "apps": [],
    "user_classes": {
        "distribution": {"heavy": 1, "medium": 2, "light": 1},
        "app_affinity": {
            "heavy":  {"vol_exp": 1.5, "int_exp": -0.5},
            "medium": {"vol_exp": 1.0, "int_exp": 0.0},
            "light":  {"vol_exp": -0.3, "int_exp": 0.5},
        },
        "app_mix_override": {},
    },
    "sampling_strategy": "stratified",
    "temporal_correlation": {
        "enabled": True,
        "rtt_delay_range": [0.010, 0.050],
        "dl_bursts_per_ul_request": [1, 5],
        "jitter": 0.005,
        "mode_overrides": {},
        "mode_thresholds": {},
    },
    "network": {
        "dn_ip": "192.168.72.135", "ue_ip_prefix": "12.1.1.",
        "ue_ip_start": 1, "dl_port": 5001, "ul_port": 5000,
    },
}


def discover_apps() -> list:
    """Apps that have both downlink/ and uplink/ burst data under artifacts/."""
    adir = settings.artifacts_dir()
    if not adir.exists():
        return []
    return [d.name for d in sorted(adir.iterdir())
            if d.is_dir() and (d / "downlink").exists() and (d / "uplink").exists()]


def load() -> dict:
    """Existing scenario_config.yaml as a dict (empty if absent)."""
    p = settings.scenario_config_path()
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def merged() -> dict:
    """Existing config deep-merged over DEFAULTS, so every key is present."""
    def _merge(dst, src):
        for k, v in (src or {}).items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _merge(dst[k], v)
            else:
                dst[k] = v
    base = copy.deepcopy(DEFAULTS)
    _merge(base, load())
    return base


def validate(cfg) -> list:
    """Return a list of human-readable errors ([] means valid)."""
    errs = []
    n_ue = cfg["simulation"]["n_ue"]
    profs = cfg.get("profiles") or []
    if profs:
        ptot = sum(int(p.get("count", 0)) for p in profs)
        if ptot != n_ue:
            errs.append(f"profile counts sum to {ptot}, must equal n_ue ({n_ue})")
        for p in profs:
            nm = p.get("name", "?")
            if not (p.get("app_mix") or {}):
                errs.append(f"profile '{nm}' has no apps selected")
            if p.get("base") not in ("heavy", "medium", "light"):
                errs.append(f"profile '{nm}' base must be heavy/medium/light")
    else:
        total = sum(cfg["user_classes"]["distribution"].values())
        if total != n_ue:
            errs.append(f"user-class counts sum to {total}, must equal n_ue ({n_ue})")
    if not cfg["apps"]:
        errs.append("no apps selected")
    lo, hi = cfg["temporal_correlation"]["rtt_delay_range"]
    if lo > hi:
        errs.append(f"RTT min ({lo}) is greater than RTT max ({hi})")
    dlo, dhi = cfg["temporal_correlation"]["dl_bursts_per_ul_request"]
    if dlo > dhi:
        errs.append(f"DL-bursts-per-request min ({dlo}) > max ({dhi})")
    return errs


def save(cfg):
    """Back up the existing file, then write the new config. Returns the path."""
    p = settings.scenario_config_path()
    if p.exists():
        p.with_suffix(".yaml.bak").write_text(p.read_text())
    header = (f"# scenario_config.yaml — written by the twindash Design page "
              f"{datetime.now():%Y-%m-%d %H:%M}\n"
              "# Advanced blocks (app_affinity, *_override, *_thresholds) carried "
              "over from the previous file.\n\n")
    p.write_text(header + yaml.safe_dump(cfg, sort_keys=False))
    return p
