"""Author validated, time-based runtime channel schedules for RIC5G runs.

The RFsim channel family is chosen when the gNB/UE processes boot.  A schedule
therefore declares the family it expects and changes only numeric parameters
that the remote helper can read back.  Downlink models are per UE; the current
profile's uplink model is shared by every UE in a cell.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

import pandas as pd

from . import kpis, ric5g, schema


PARAMETERS = ("noise_power_dB", "ploss", "riceanf", "aoa", "offset", "forgetf")
MODEL_TYPES = ("AWGN", "TDL_A", "TDL_B", "TDL_C", "EPA", "EVA", "ETU")


def path(run_dir) -> Path:
    return Path(run_dir) / schema.CHANNEL_SCHEDULE


def empty() -> dict:
    return {
        "schema_version": 1,
        "enabled": False,
        "expected_model_type": "AWGN",
        "events": [],
    }


def load(run_dir) -> dict:
    target = path(run_dir)
    if not target.exists():
        return empty()
    value = json.loads(target.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{target.name} must contain a JSON object")
    return value


def run_ues(run_dir) -> list[str]:
    manifest = Path(run_dir) / schema.SCRIPTS_DIR / "manifest.csv"
    if not manifest.exists():
        return []
    frame = pd.read_csv(manifest)
    if "ue_name" not in frame:
        return []
    return [str(value) for value in frame["ue_name"].dropna()]


def topology(cfg: dict, run_dir) -> tuple[dict[str, dict], dict[int, dict]]:
    """Return the deployable UE and cell targets for this generated run."""
    if not ric5g.is_config(cfg):
        return {}, {}
    boxes = (cfg.get("ues") or {}).get("boxes") or {}
    names = set(run_ues(run_dir))
    ues = {
        name: {
            "cell": int(box["cell"]),
            "ue_index": int(box["ue_index"]),
            "nb_id": int(box.get("nb_id", 0)),
        }
        for name, box in boxes.items()
        if name in names and box.get("cell") is not None
    }
    cells = {
        int(item["cell"]): item
        for item in ((cfg.get("nodes") or {}).get("cells") or [])
        if item.get("cell") is not None
    }
    return ues, cells


def duration(run_dir) -> float:
    value = kpis.run_duration(Path(run_dir))
    if value is None or value <= 0:
        raise ValueError("the generated run has no positive simulation duration")
    return float(value)


def validate(schedule: dict, run_dir, cfg: dict) -> list[str]:
    errors: list[str] = []
    if schedule.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    model = schedule.get("expected_model_type")
    if model not in MODEL_TYPES:
        errors.append(f"expected_model_type must be one of {', '.join(MODEL_TYPES)}")
    if not schedule.get("enabled", True):
        return errors
    if not ric5g.is_config(cfg):
        errors.append("runtime channel schedules require the RIC5G distributed profile")
        return errors
    ues, cells = topology(cfg, run_dir)
    if not ues:
        errors.append("no generated UEs match the active RIC5G testbed configuration")
    try:
        run_duration = duration(run_dir)
    except ValueError as exc:
        errors.append(str(exc))
        run_duration = 0
    events = schedule.get("events")
    if not isinstance(events, list) or not events:
        errors.append("an enabled schedule needs at least one transition")
        return errors

    identities = set()
    for index, event in enumerate(events, start=1):
        prefix = f"row {index}"
        if not isinstance(event, dict):
            errors.append(f"{prefix}: transition must be an object")
            continue
        try:
            at_s = float(event.get("at_s"))
            if not math.isfinite(at_s) or at_s < 0 or at_s > run_duration:
                errors.append(f"{prefix}: at_s must be between 0 and {run_duration:g}")
        except (TypeError, ValueError):
            errors.append(f"{prefix}: at_s must be numeric")
            at_s = None
        target = str(event.get("target") or "")
        direction = event.get("direction")
        parameter = event.get("parameter")
        if direction not in {"dl", "ul"}:
            errors.append(f"{prefix}: direction must be dl or ul")
        if parameter not in PARAMETERS:
            errors.append(f"{prefix}: unsupported parameter {parameter!r}")
        try:
            value = float(event.get("value"))
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{prefix}: value must be finite and numeric")
        if direction == "dl" and target not in ues:
            errors.append(f"{prefix}: DL target must be one of {', '.join(ues)}")
        if direction == "ul":
            match = re.fullmatch(r"cell(\d+)", target)
            if not match or int(match.group(1)) not in cells:
                errors.append(f"{prefix}: UL target must be an active cell")
        identity = (at_s, target, direction, parameter)
        if identity in identities:
            errors.append(f"{prefix}: duplicate transition {identity}")
        identities.add(identity)
    return errors


def expand_groups(schedule: dict, run_dir, cfg: dict) -> dict:
    """Expand UI group targets into the exact targets stored for deployment."""
    result = dict(schedule)
    ues, cells = topology(cfg, run_dir)
    expanded = []
    for event in schedule.get("events") or []:
        target = str(event.get("target") or "")
        direction = event.get("direction")
        targets = [target]
        if target == "all_ues":
            if direction != "dl":
                raise ValueError("all_ues is a downlink-only target")
            targets = list(ues)
        elif target == "all_cells":
            if direction != "ul":
                raise ValueError("all_cells is an uplink-only target")
            targets = [f"cell{cell}" for cell in sorted(cells)]
        else:
            match = re.fullmatch(r"cell(\d+)_ues", target)
            if match:
                if direction != "dl":
                    raise ValueError(f"{target} is a downlink-only target")
                cell = int(match.group(1))
                targets = [name for name, item in ues.items() if item["cell"] == cell]
                if not targets:
                    raise ValueError(f"{target} contains no generated UEs")
        for exact in targets:
            row = dict(event)
            row["target"] = exact
            expanded.append(row)
    result["events"] = expanded
    return result


def normalized(schedule: dict) -> dict:
    result = {
        "schema_version": 1,
        "enabled": bool(schedule.get("enabled", True)),
        "expected_model_type": str(schedule.get("expected_model_type", "AWGN")),
        "events": [],
    }
    for event in schedule.get("events") or []:
        result["events"].append({
            "at_s": float(event["at_s"]),
            "target": str(event["target"]),
            "direction": str(event["direction"]),
            "parameter": str(event["parameter"]),
            "value": float(event["value"]),
        })
    result["events"].sort(
        key=lambda row: (row["at_s"], row["target"], row["direction"], row["parameter"]))
    return result


def save(run_dir, schedule: dict, cfg: dict) -> Path:
    value = normalized(schedule)
    errors = validate(value, run_dir, cfg)
    if errors:
        raise ValueError("invalid channel schedule:\n- " + "\n- ".join(errors))
    target = path(run_dir)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(target)
    return target
