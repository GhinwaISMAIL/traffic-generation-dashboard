from __future__ import annotations

import csv
from dataclasses import dataclass
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile

import pandas as pd


REQUIRED_COLUMNS = (
    "session_id", "experimental_ue", "start_s", "end_s",
    "traffic_model", "source_category", "scenario_role",
)
ROLES = ("baseline", "augmented")
SCHEDULE_CSV = "scenario_schedule.csv"
SCHEDULE_METADATA = "scenario_schedule.json"
SCHEDULE_CHECKSUMS = "scenario_schedule.sha256.json"


def population(identifiers, *, expected_count=None) -> tuple[str, ...]:
    values = identifiers.splitlines() if isinstance(identifiers, str) else list(identifiers)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("experimental UE identifiers must be text")
    names = tuple(value.strip() for value in values if value.strip())
    if not names:
        raise ValueError("provide the experimental UE population from the frozen scenario")
    if len(set(names)) != len(names):
        raise ValueError("experimental UE population contains duplicate identifiers")
    if expected_count is not None and len(names) != expected_count:
        raise ValueError(f"experimental UE population must contain {expected_count} identifiers; "
                         f"received {len(names)}")
    return names


def _seconds(value) -> float:
    text = str(value).strip()
    if ":" in text:
        match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", text)
        if not match:
            raise ValueError("expected seconds or HH:MM:SS")
        hours, minutes, seconds = map(float, match.groups())
        result = hours * 3600 + minutes * 60 + seconds
    else:
        result = float(text)
    if not math.isfinite(result) or result < 0:
        raise ValueError("time must be finite and nonnegative")
    return result


def _duration(value) -> float:
    duration = _seconds(value)
    if duration <= 0:
        raise ValueError("logical duration must be positive")
    return duration


def normalize(frame: pd.DataFrame, *, logical_duration_s=86400,
              available_models=None, model_mapping=None, expected_ues=None) -> pd.DataFrame:
    duration = _duration(logical_duration_s)
    result = frame.copy()
    result.columns = [str(column).strip() for column in result.columns]
    if result.columns.duplicated().any():
        raise ValueError("schedule contains duplicate column names")
    if "baseline_or_augmented" in result:
        alias = result["baseline_or_augmented"].fillna("").astype(str).str.strip()
        if "scenario_role" in result:
            role = result["scenario_role"].fillna("").astype(str).str.strip()
            if not role.equals(alias):
                raise ValueError("scenario_role and baseline_or_augmented disagree")
        result["scenario_role"] = alias
        result = result.drop(columns="baseline_or_augmented")
    missing = [column for column in REQUIRED_COLUMNS if column not in result]
    if missing:
        raise ValueError("missing schedule columns: " + ", ".join(missing))
    if result.empty:
        raise ValueError("schedule has no sessions")
    for column in REQUIRED_COLUMNS:
        if column in ("start_s", "end_s"):
            values = []
            for row, value in enumerate(result[column], start=2):
                try:
                    values.append(_seconds(value))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"row {row}, {column}: {exc}") from exc
            result[column] = values
        else:
            result[column] = result[column].fillna("").astype(str).str.strip()
            if result[column].eq("").any():
                raise ValueError(f"{column} must not be empty")
    duplicates = result.loc[result["session_id"].duplicated(), "session_id"].tolist()
    if duplicates:
        raise ValueError("session_id must be unique: " + ", ".join(duplicates[:5]))
    if expected_ues is not None:
        unknown = sorted(set(result["experimental_ue"]) - set(population(expected_ues)))
        if unknown:
            raise ValueError("UEs outside the experimental population: " + ", ".join(unknown))
    invalid = result["end_s"].le(result["start_s"])
    if invalid.any():
        raise ValueError("end_s must be greater than start_s for every session")
    if result["end_s"].gt(duration).any():
        raise ValueError(f"sessions extend beyond the logical duration ({duration:g} s)")
    invalid_roles = sorted(set(result["scenario_role"]) - set(ROLES))
    if invalid_roles:
        raise ValueError("scenario_role must be baseline or augmented: " +
                         ", ".join(invalid_roles))
    if "augmentation_iteration" in result:
        populated = result["augmentation_iteration"].notna() & result[
            "augmentation_iteration"].astype(str).str.strip().ne("")
        values = pd.to_numeric(result.loc[populated, "augmentation_iteration"],
                               errors="coerce")
        if (values.isna() | values.lt(0) | values.mod(1).ne(0)).any():
            raise ValueError("augmentation_iteration must be a nonnegative integer")
    mapping = model_mapping or {}
    if mapping:
        if "source_traffic_model" not in result:
            result["source_traffic_model"] = result["traffic_model"]
        result["traffic_model"] = result["traffic_model"].map(
            lambda value: mapping.get(value, value))
    if not result["traffic_model"].map(
            lambda value: isinstance(value, str) and
            re.fullmatch(r"[a-z0-9][a-z0-9_]*", value) is not None).all():
        raise ValueError("traffic_model must use lowercase letters, digits, or underscores")
    if available_models is not None:
        unknown = sorted(set(result["traffic_model"]) - set(available_models))
        if unknown:
            raise ValueError("application models not discovered: " + ", ".join(unknown))
    columns = list(REQUIRED_COLUMNS) + [column for column in result
                                        if column not in REQUIRED_COLUMNS]
    return result[columns].sort_values(
        ["start_s", "experimental_ue", "session_id"], kind="stable").reset_index(drop=True)


@dataclass(frozen=True)
class Schedule:
    sessions: pd.DataFrame
    logical_duration_s: float
    source_name: str
    source_sha256: str
    ue_population: tuple[str, ...] = ()

    def document(self) -> dict:
        sessions = normalize(self.sessions, logical_duration_s=self.logical_duration_s,
                             expected_ues=self.ue_population or None)
        return {
            "schema_version": 1,
            "scenario_mode": "schedule",
            "logical_duration_s": _duration(self.logical_duration_s),
            "time_origin": "scenario_start",
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "ue_population": list(self.ue_population),
            "sessions": json.loads(sessions.to_json(orient="records", double_precision=15)),
        }

    def to_json(self) -> str:
        return json.dumps(self.document(), indent=2, sort_keys=True, allow_nan=False) + "\n"

    def to_csv(self) -> str:
        frame = normalize(self.sessions, logical_duration_s=self.logical_duration_s,
                          expected_ues=self.ue_population or None)
        return frame.to_csv(index=False, lineterminator="\n", float_format="%.17g")


def load(source, *, logical_duration_s=86400, available_models=None,
         model_mapping=None, source_name=None, expected_ues=None) -> Schedule:
    names = population(expected_ues) if expected_ues is not None else ()
    if isinstance(source, (str, Path)):
        path = Path(source).expanduser()
        raw = path.read_bytes()
        name = path.name
    elif isinstance(source, bytes):
        raw = source
        name = source_name or "schedule.csv"
    else:
        raw = source.getvalue() if hasattr(source, "getvalue") else source.read()
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        name = source_name or Path(getattr(source, "name", "schedule.csv")).name
    try:
        text = raw.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"invalid UTF-8 CSV: {exc}") from exc
    rows = [row for row in rows if row]
    if not rows:
        raise ValueError("schedule CSV is empty")
    columns = [column.strip() for column in rows[0]]
    if len(set(columns)) != len(columns) or "" in columns:
        raise ValueError("schedule CSV needs unique, nonempty column names")
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != len(columns):
            raise ValueError(f"row {line} has {len(row)} fields; expected {len(columns)}")
    frame = normalize(pd.DataFrame(rows[1:], columns=columns),
                      logical_duration_s=logical_duration_s,
                      available_models=available_models, model_mapping=model_mapping,
                      expected_ues=names or None)
    return Schedule(frame, _duration(logical_duration_s), Path(name).name,
                    hashlib.sha256(raw).hexdigest(), names)


def validate(value: Schedule, *, expected_ues, available_models,
             model_mapping=None, expected_count=None) -> Schedule:
    if available_models is None:
        raise ValueError("provide the recognized application models for validation")
    names = population(expected_ues, expected_count=expected_count)
    frame = normalize(value.sessions, logical_duration_s=value.logical_duration_s,
                      expected_ues=names, available_models=available_models,
                      model_mapping=model_mapping)
    return Schedule(frame, _duration(value.logical_duration_s), value.source_name,
                    value.source_sha256, names)


def save(value: Schedule, directory) -> Path:
    content = value.to_json()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"schedule_{digest[:20]}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                                         prefix=".schedule-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != content:
                raise ValueError(f"saved schedule content differs: {target}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def summary(value: Schedule) -> dict:
    frame = value.sessions
    peaks = []
    active_ue_changes = []
    for _, sessions in frame.groupby("experimental_ue"):
        starts = pd.Series(1, index=sessions["start_s"].to_numpy())
        ends = pd.Series(-1, index=sessions["end_s"].to_numpy())
        changes = pd.concat([starts, ends]).groupby(level=0).sum().sort_index()
        counts = changes.cumsum()
        peaks.append(int(counts.max()))
        active = counts.gt(0).astype(int)
        active_ue_changes.append(active - active.shift(fill_value=0))
    concurrent = (pd.concat(active_ue_changes).groupby(level=0).sum().sort_index().cumsum()
                  if active_ue_changes else pd.Series(dtype=int))
    return {
        "ues": int(frame["experimental_ue"].nunique()),
        "population_size": len(value.ue_population),
        "sessions": len(frame),
        "baseline_sessions": int(frame["scenario_role"].eq("baseline").sum()),
        "augmented_sessions": int(frame["scenario_role"].eq("augmented").sum()),
        "applications": sorted(frame["traffic_model"].unique()),
        "peak_sessions_per_ue": max(peaks, default=0),
        "max_concurrent_ues": int(concurrent.max()) if not concurrent.empty else 0,
        "overlapping_sessions": max(peaks, default=0) > 1,
        "earliest_session_s": float(frame["start_s"].min()),
        "latest_session_s": float(frame["end_s"].max()),
        "logical_duration_s": value.logical_duration_s,
    }


def import_run(value: Schedule, profiles_dir, *, available_models) -> Path:
    value = validate(value, expected_ues=value.ue_population,
                     available_models=available_models)
    content = value.to_csv().encode("utf-8")
    metadata = {
        "schema_version": 1,
        "scenario_mode": "schedule",
        "time_origin": "scenario_start",
        "logical_duration_s": value.logical_duration_s,
        "source_name": value.source_name,
        "source_sha256": value.source_sha256,
        "normalized_sha256": hashlib.sha256(content).hexdigest(),
        "ue_population": list(value.ue_population),
        "validated_traffic_models": sorted(value.sessions["traffic_model"].unique()),
        "summary": summary(value),
    }
    metadata_bytes = (json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    identifier = hashlib.sha256(metadata_bytes).hexdigest()[:20]
    root = Path(profiles_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"run_schedule_{identifier}"

    def existing():
        load_frozen(target)
        if ((target / SCHEDULE_CSV).read_bytes() != content or
                (target / SCHEDULE_METADATA).read_bytes() != metadata_bytes):
            raise ValueError(f"existing imported schedule differs: {target}")
        return target

    if target.exists():
        return existing()
    with tempfile.TemporaryDirectory(prefix=".schedule-import-", dir=root) as temporary:
        staging = Path(temporary)
        files = {SCHEDULE_CSV: content, SCHEDULE_METADATA: metadata_bytes}
        for name, data in files.items():
            (staging / name).write_bytes(data)
        checksums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        (staging / SCHEDULE_CHECKSUMS).write_text(
            json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = {
            "scenario_mode": "schedule", "generation_status": "pending",
            "logical_duration_s": value.logical_duration_s,
            "schedule_file": SCHEDULE_CSV, "schedule_metadata": SCHEDULE_METADATA,
        }
        (staging / "config.json").write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            staging.rename(target)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            return existing()
    return target


def load_frozen(run_dir) -> Schedule:
    root = Path(run_dir)
    try:
        checksums = json.loads((root / SCHEDULE_CHECKSUMS).read_text(encoding="utf-8"))
        if not isinstance(checksums, dict) or set(checksums) != {SCHEDULE_CSV, SCHEDULE_METADATA}:
            raise ValueError("invalid frozen schedule checksum manifest")
        for name, expected in checksums.items():
            path = root / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"frozen schedule checksum mismatch: {name}")
        metadata = json.loads((root / SCHEDULE_METADATA).read_text(encoding="utf-8"))
        if (not isinstance(metadata, dict) or metadata.get("schema_version") != 1 or
                metadata.get("scenario_mode") != "schedule" or
                metadata.get("time_origin") != "scenario_start"):
            raise ValueError("unsupported frozen schedule metadata")
        if metadata.get("normalized_sha256") != checksums[SCHEDULE_CSV]:
            raise ValueError("frozen schedule normalized checksum disagrees with metadata")
        names = population(metadata["ue_population"])
        loaded = load(root / SCHEDULE_CSV,
                      logical_duration_s=metadata["logical_duration_s"],
                      available_models=metadata["validated_traffic_models"], expected_ues=names)
        result = Schedule(loaded.sessions, loaded.logical_duration_s,
                          metadata["source_name"], metadata["source_sha256"], names)
        if summary(result) != metadata.get("summary"):
            raise ValueError("frozen schedule summary disagrees with its sessions")
        return result
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid frozen schedule at {root}: {exc}") from exc


def execution_window(value: Schedule, start_s, end_s) -> pd.DataFrame:
    start = _seconds(start_s)
    end = _seconds(end_s)
    if not start < end <= value.logical_duration_s:
        raise ValueError("execution window must lie within the logical scenario")
    sessions = value.sessions
    selected = sessions.loc[sessions["start_s"].lt(end) &
                            sessions["end_s"].gt(start)].copy()
    selected["execution_start_s"] = selected["start_s"].clip(lower=start) - start
    selected["execution_end_s"] = selected["end_s"].clip(upper=end) - start
    selected["session_offset_s"] = (start - selected["start_s"]).clip(lower=0)
    return selected.reset_index(drop=True)
