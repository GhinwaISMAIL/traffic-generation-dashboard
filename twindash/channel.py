"""Author validated, time-based runtime channel schedules for RIC5G runs.

The RFsim channel family is chosen when the gNB/UE processes boot.  A schedule
therefore declares the family it expects and changes only numeric parameters
that the remote helper can read back.  Downlink models are per UE; the current
profile's uplink model is shared by every UE in a cell.
"""
from __future__ import annotations

import csv
import fcntl
import io
import json
import math
from pathlib import Path
import re

import pandas as pd

from . import kpis, ric5g, schema


PARAMETERS = ("noise_power_dB", "ploss", "riceanf", "aoa", "offset", "forgetf")
MODEL_TYPES = ("AWGN", "TDL_A", "TDL_B", "TDL_C", "EPA", "EVA", "ETU")
CSV_COLUMNS = ("at_s", "target", "direction", "parameter", "value")


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


def ue_aliases(run_dir) -> dict[str, str]:
    target = Path(run_dir) / "ue_mapping.csv"
    if not target.exists():
        return {}
    frame = pd.read_csv(target, dtype=str, keep_default_na=False)
    if not {"experimental_ue", "ue_name"} <= set(frame.columns):
        raise ValueError("ue_mapping.csv needs experimental_ue and ue_name columns")
    names = set(run_ues(run_dir))
    aliases = {}
    for row in frame.itertuples(index=False):
        original, generated = row.experimental_ue.strip(), row.ue_name.strip()
        if not original or generated not in names:
            raise ValueError("ue_mapping.csv contains an empty or unknown UE")
        if ((original in names and original != generated)
                or (original in aliases and aliases[original] != generated)):
            raise ValueError(f"ambiguous experimental UE identifier: {original}")
        aliases[original] = generated
    return aliases


def load_csv(source, run_dir, cfg: dict, *, expected_model_type="AWGN") -> dict:
    if isinstance(source, (str, Path)):
        data = Path(source).expanduser().read_bytes()
    elif isinstance(source, bytes):
        data = source
    elif hasattr(source, "getvalue"):
        data = source.getvalue()
    else:
        data = source.read()
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
    except UnicodeDecodeError as exc:
        raise ValueError("channel CSV must use UTF-8 encoding") from exc
    aliases = ue_aliases(run_dir)
    events = []
    try:
        reader = csv.reader(io.StringIO(text), strict=True)
        columns = [value.strip() for value in next(reader, [])]
        if len(columns) != len(set(columns)):
            raise ValueError("channel CSV contains duplicate column names")
        missing = set(CSV_COLUMNS) - set(columns)
        extra = set(columns) - set(CSV_COLUMNS)
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing columns: {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unexpected columns: {', '.join(sorted(extra))}")
            raise ValueError("channel CSV " + "; ".join(details))
        for values in reader:
            if not any(value.strip() for value in values):
                continue
            if len(values) != len(columns):
                raise ValueError(f"CSV line {reader.line_num}: expected {len(columns)} fields")
            row = dict(zip(columns, (value.strip() for value in values)))
            if not all(row.values()):
                raise ValueError(f"CSV line {reader.line_num}: all five fields are required")
            try:
                row["at_s"] = float(row["at_s"])
                row["value"] = float(row["value"])
            except ValueError as exc:
                raise ValueError(f"CSV line {reader.line_num}: at_s and value must be numeric") from exc
            row["direction"] = row["direction"].lower()
            if row["direction"] == "dl":
                row["target"] = aliases.get(row["target"], row["target"])
            events.append(row)
    except csv.Error as exc:
        raise ValueError(f"invalid channel CSV: {exc}") from exc
    value = expand_groups({
        "schema_version": 1, "enabled": True,
        "expected_model_type": expected_model_type, "events": events,
    }, run_dir, cfg)
    errors = validate(value, run_dir, cfg)
    if errors:
        detail = "\n- ".join(errors[:20])
        remainder = f"\n({len(errors) - 20} more errors)" if len(errors) > 20 else ""
        raise ValueError(f"invalid channel schedule:\n- {detail}{remainder}")
    return normalized(value)


def to_csv(schedule: dict) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: row[key] for key in CSV_COLUMNS}
                     for row in schedule.get("events") or [])
    return stream.getvalue()


def deployment_active(run_dir) -> bool:
    target = Path(run_dir) / schema.LOGS_DIR / "deployment.lock"
    if not target.exists():
        return False
    with target.open("r") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


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
    logs = Path(run_dir) / schema.LOGS_DIR
    logs.mkdir(exist_ok=True)
    with (logs / "deployment.lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("this run is being deployed; save channel changes after it finishes") from None
        try:
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
            temporary.replace(target)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return target
