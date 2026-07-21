"""Absolute-time anchors, so MGEN logs and xApp PRB can be joined.

MGEN prints HH:MM:SS.ffffff on the *local* clock of whichever node wrote the
log — no date, no zone. agg_prb.py emits `utc_second` as epoch seconds. The two
are not comparable until the MGEN side is lifted onto the same timeline.

The runner writes logs/run_timing.json holding, per node, one (epoch_s, sod_s)
pair captured at the same instant. Then

    midnight_epoch = epoch_s - sod_s

is the epoch second of that node's local midnight, so

    utc_time = midnight_epoch + seconds_since_midnight

recovers absolute time without knowing the date or the zone — and makes the
midnight rollover explicit instead of silently wrong.
"""
from __future__ import annotations

import json
from pathlib import Path

TIMING = "run_timing.json"
ROLLOVER_GUARD_S = 43200.0   # 12 h; a stamp this far behind the anchor is next-day


def load(run_dir) -> dict:
    p = Path(run_dir) / "logs" / TIMING
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def node_of(timing: dict, log_node: str):
    """'dn' -> 'core';  'ue7' -> the cell node that ran ue7."""
    if not timing:
        return None
    if log_node == "dn":
        return "core"
    return (timing.get("ue_node") or {}).get(log_node)


def midnight_epoch(timing: dict, node: str):
    a = (timing.get("nodes") or {}).get(node or "")
    if not a:
        return None
    if a.get("midnight_epoch") is not None:
        return float(a["midnight_epoch"])
    if a.get("epoch_s") is not None and a.get("sod_s") is not None:
        return float(a["epoch_s"]) - float(a["sod_s"])
    return None


def anchor_sod(timing: dict, node: str):
    a = (timing.get("nodes") or {}).get(node or "")
    return float(a["sod_s"]) if a and a.get("sod_s") is not None else None


def to_utc(sod, midnight_epoch_s, ref_sod=None):
    """Seconds-since-local-midnight -> epoch seconds. A stamp far behind the
    anchor means the run crossed midnight, so it belongs to the next day."""
    if sod is None or midnight_epoch_s is None:
        return None
    if ref_sod is not None and sod < ref_sod - ROLLOVER_GUARD_S:
        sod = sod + 86400.0
    return midnight_epoch_s + sod


def zones_agree(timing: dict) -> bool:
    """True when every node's local midnight is the same instant. Cross-node
    latency (DL sender on the core, receiver on a cell) is only meaningful
    when this holds."""
    vals = [midnight_epoch(timing, n) for n in (timing.get("nodes") or {})]
    vals = [v for v in vals if v is not None]
    return len(set(vals)) <= 1


def skew_bounds(timing: dict) -> dict:
    """Per-node clock offset vs the workstation, bracketed by the ssh round
    trip. Only available when the runner recorded local_before/local_after."""
    out = {}
    for name, a in (timing.get("nodes") or {}).items():
        b, af, e = a.get("local_before"), a.get("local_after"), a.get("epoch_s")
        if None in (b, af, e):
            continue
        out[name] = {"low_s": float(e) - float(af), "high_s": float(e) - float(b)}
    return out


def xapp_window(timing: dict):
    """(start_epoch, end_epoch) of the PRB measurement window, or None."""
    x = timing.get("xapp") or {}
    start, win = x.get("start_epoch"), x.get("window_s")
    if start is None or win is None:
        return None
    return float(start), float(start) + float(win)
