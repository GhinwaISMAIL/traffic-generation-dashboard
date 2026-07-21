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
    "simulation": {
        "duration": 600, "num_cells": 1, "ues_per_cell": 4,
        "n_ue": 4, "random_seed": 42,
    },
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
    existing = load()
    base = copy.deepcopy(DEFAULTS)
    _merge(base, existing)
    old_sim = (existing.get("simulation") or {}) if existing else {}
    if "num_cells" not in old_sim:
        base["simulation"]["num_cells"] = 1
    if "ues_per_cell" not in old_sim:
        base["simulation"]["ues_per_cell"] = int(
            base["simulation"].get("n_ue", 1))
    return base


def cell_specs(cfg: dict) -> list[dict]:
    """Return the traffic specification for each cell.

    New scenarios carry an explicit ``cells`` list.  Older scenarios remain
    readable as a single logical cell using their top-level distribution or
    profiles block.
    """
    raw = cfg.get("cells") or []
    if raw:
        return sorted(copy.deepcopy(raw), key=lambda item: int(item["cell"]))

    sim = cfg.get("simulation") or {}
    n_ue = int(sim.get("n_ue", 0))
    return [{
        "cell": 1,
        "n_ue": n_ue,
        "distribution": copy.deepcopy(
            ((cfg.get("user_classes") or {}).get("distribution") or {})),
        "profiles": copy.deepcopy(cfg.get("profiles") or []),
    }]


def validate(cfg) -> list:
    """Return a list of human-readable errors ([] means valid)."""
    errs = []
    sim = cfg["simulation"]
    n_ue = int(sim["n_ue"])
    num_cells = int(sim.get("num_cells", 1))
    ues_per_cell = int(sim.get("ues_per_cell", n_ue))
    if not 1 <= num_cells <= 3:
        errs.append("num_cells must be between 1 and 3")
    if ues_per_cell < 1:
        errs.append("ues_per_cell must be at least 1")
    if n_ue != num_cells * ues_per_cell:
        errs.append(
            f"n_ue ({n_ue}) must equal num_cells x ues_per_cell "
            f"({num_cells * ues_per_cell})")

    cells = cell_specs(cfg)
    if cfg.get("cells"):
        ids = [int(cell.get("cell", 0)) for cell in cells]
        if ids != list(range(1, num_cells + 1)):
            errs.append("cell specifications must be consecutive from 1 to num_cells")
        if len(cells) != num_cells:
            errs.append(f"expected {num_cells} cell specifications, found {len(cells)}")

    cell_total = 0
    for cell in cells:
        cell_id = int(cell.get("cell", 0))
        cell_n = int(cell.get("n_ue", ues_per_cell))
        cell_total += cell_n
        if cfg.get("cells") and cell_n != ues_per_cell:
            errs.append(
                f"cell {cell_id} has {cell_n} UEs, expected {ues_per_cell}")
        profs = cell.get("profiles") or []
        if profs:
            ptot = sum(int(p.get("count", 0)) for p in profs)
            if ptot != cell_n:
                errs.append(
                    f"cell {cell_id} profile counts sum to {ptot}, "
                    f"must equal {cell_n}")
            for p in profs:
                nm = p.get("name", "?")
                if not (p.get("app_mix") or {}):
                    errs.append(f"cell {cell_id} profile '{nm}' has no apps selected")
                unknown_apps = sorted(set(p.get("app_mix") or {}) - set(cfg["apps"]))
                if unknown_apps:
                    errs.append(
                        f"cell {cell_id} profile '{nm}' uses unselected apps: "
                        f"{', '.join(unknown_apps)}")
                if p.get("base") not in ("heavy", "medium", "light"):
                    errs.append(
                        f"cell {cell_id} profile '{nm}' base must be "
                        "heavy/medium/light")
                if int(p.get("flows", 0)) < 1:
                    errs.append(
                        f"cell {cell_id} profile '{nm}' flows must be at least 1")
        else:
            distribution = cell.get("distribution") or {}
            total = sum(int(value) for value in distribution.values())
            if total != cell_n:
                errs.append(
                    f"cell {cell_id} user-class counts sum to {total}, "
                    f"must equal {cell_n}")
    if cell_total != n_ue:
        errs.append(f"cell UE counts sum to {cell_total}, must equal n_ue ({n_ue})")
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
