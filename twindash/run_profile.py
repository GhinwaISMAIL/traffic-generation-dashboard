"""Per-run deployment identity and measurement capabilities.

``testbed_config.yaml`` is mutable: it describes what the next deployment will
use.  Results must not use it to reinterpret an older run.  This module records
the relevant testbed identity beside the generated traffic artifacts and loads
that immutable snapshot later.

Older runs have no snapshot.  For those, inference is deliberately conservative:
RIC/xApp capabilities are enabled only when their artifacts are actually present.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import schema

RIC5G = "powder_ric5g_distributed"
RFSIM = "powder_rfsim_docker"
COTS = "powder_emulab"
UNKNOWN = "legacy_or_unknown"

LABELS = {
    RIC5G: "RIC5G distributed",
    RFSIM: "RFsim Docker",
    COTS: "COTS physical UEs",
    UNKNOWN: "Legacy / unknown",
}


def _capabilities(testbed: str, xapp_enabled: bool = False) -> dict:
    common = {
        "flow_kpis": True,
        "latency": True,
        "channel_model": False,
        "ric": False,
        "xapp": False,
        "prb": False,
        "radio_efficiency": False,
    }
    if testbed == RIC5G:
        common.update({
            "channel_model": True,
            "ric": True,
            "xapp": bool(xapp_enabled),
            "prb": bool(xapp_enabled),
            "radio_efficiency": bool(xapp_enabled),
        })
    return common


def from_config(cfg: dict) -> dict:
    """Build the immutable part of a run profile from testbed_config.yaml."""
    cfg = cfg or {}
    testbed = cfg.get("testbed") or UNKNOWN
    xapp = cfg.get("xapp") or {}
    cells = (cfg.get("nodes") or {}).get("cells") or []
    boxes = (cfg.get("ues") or {}).get("boxes") or {}
    capabilities = _capabilities(
        testbed, xapp_enabled=xapp.get("enabled", True) if testbed == RIC5G else False)
    # An explicit capability block is useful for future profiles and lets a
    # local deployment disable a nominal capability without changing code.
    capabilities.update({
        key: bool(value)
        for key, value in (cfg.get("capabilities") or {}).items()
        if key in capabilities
    })
    profile = {
        "schema_version": 1,
        "testbed": testbed,
        "label": LABELS.get(testbed, str(testbed)),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": capabilities,
        "topology": {
            "cells": len(cells) if testbed == RIC5G else None,
            "ues": len(boxes) or None,
        },
    }
    if testbed == RIC5G:
        profile["channel_model"] = {
            "supported": True,
            "scope": "per-cell and per-UE",
            "runtime_control": "telnet channelmod",
            # Exact live values are a separate artifact; support must not be
            # confused with proof of which impairment was active.
            "state_artifact": "logs/channel_state.json",
        }
        profile["xapp"] = {
            "enabled": capabilities["xapp"],
            "expected_subscriptions": xapp.get("expected_subscriptions"),
            "window_s": xapp.get("window_s"),
        }
    return profile


def path(run_dir) -> Path:
    return Path(run_dir) / schema.RUN_PROFILE


def record(run_dir, cfg: dict, *, overwrite: bool = True) -> dict:
    """Record the config used for a deployment and return the saved profile."""
    target = path(run_dir)
    if target.exists() and not overwrite:
        return load(run_dir)
    profile = from_config(cfg)
    target.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    return profile


def _legacy_topology(logs: Path) -> dict:
    cells, ues = set(), set()
    mapping = logs / "rnti_map.csv"
    if mapping.exists():
        try:
            with mapping.open(newline="") as stream:
                for row in csv.DictReader(stream):
                    if row.get("cell"):
                        cells.add(row["cell"])
                    if row.get("ue"):
                        ues.add(row["ue"])
        except (OSError, csv.Error):
            pass
    return {"cells": len(cells) or None, "ues": len(ues) or None}


def infer(run_dir) -> dict:
    """Best-effort identity for runs created before run_profile.json existed."""
    logs = Path(run_dir) / schema.LOGS_DIR
    have_xapp = (logs / "xapp.log").is_file()
    have_prb = (logs / "prb_by_second.csv").is_file()
    looks_ric5g = have_xapp or have_prb
    testbed = RIC5G if looks_ric5g else UNKNOWN
    capabilities = _capabilities(testbed, xapp_enabled=have_xapp or have_prb)
    capabilities["prb"] = have_prb
    capabilities["radio_efficiency"] = (
        have_prb and (logs / "run_timing.json").is_file())
    return {
        "schema_version": 1,
        "testbed": testbed,
        "label": LABELS[testbed] + " (inferred)",
        "recorded_at": None,
        "inferred": True,
        "capabilities": capabilities,
        "topology": _legacy_topology(logs),
        "channel_model": ({
            "supported": True,
            "scope": "per-cell and per-UE",
            "runtime_control": "telnet channelmod",
            "state_artifact": "logs/channel_state.json",
        } if looks_ric5g else {"supported": False}),
    }


def load(run_dir) -> dict:
    target = path(run_dir)
    if not target.exists():
        return infer(run_dir)
    try:
        profile = json.loads(target.read_text())
        if not isinstance(profile, dict):
            raise ValueError("run profile is not an object")
        profile.setdefault("label", LABELS.get(
            profile.get("testbed"), str(profile.get("testbed", UNKNOWN))))
        profile.setdefault("capabilities", {})
        profile.setdefault("topology", {})
        return profile
    except (OSError, ValueError, json.JSONDecodeError):
        fallback = infer(run_dir)
        fallback["profile_error"] = f"Could not read {target.name}"
        return fallback


def channel_state(run_dir):
    target = Path(run_dir) / schema.LOGS_DIR / "channel_state.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
