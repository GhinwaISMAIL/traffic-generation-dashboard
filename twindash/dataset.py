"""Immutable execution archives and model-ready dataset exports.

Generated traffic profiles are reusable designs.  A POWDER deployment is an
execution of that design with its own live IP/RNTI mapping, clocks, radio
measurements, and channel history.  This module snapshots that mutable latest
state immediately after a run, then exports selected executions without mixing
rows from one execution across train/validation/test splits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tarfile

import pandas as pd

from . import dataset_v2, kpis, prb, run_profile, schema


CONTROL_LOGS = (
    "run_timing.json", "rnti_map.csv", "prb_by_second.csv", "xapp.log",
    "channel_state.json", "ue_ips.txt", "dn_dl_tx.mgn", "deployment.log",
)


@dataclass(frozen=True)
class Execution:
    execution_id: str
    profile_id: str
    path: Path
    metadata: dict


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def execution_id(run_dir) -> str:
    timing = _json(Path(run_dir) / schema.LOGS_DIR / "run_timing.json", {}) or {}
    value = str(timing.get("run_id") or "").strip()
    if not re.fullmatch(r"mgen-[A-Za-z0-9_-]+", value):
        raise ValueError("logs/run_timing.json has no valid run_id; cannot archive safely")
    return value


def _manifest(run_dir: Path) -> pd.DataFrame:
    target = run_dir / schema.SCRIPTS_DIR / "manifest.csv"
    return pd.read_csv(target) if target.exists() else pd.DataFrame()


def _ue_metadata(run_dir: Path) -> pd.DataFrame:
    manifest = _manifest(run_dir)
    if manifest.empty or "ue_name" not in manifest:
        return pd.DataFrame(columns=["ue", "ue_class"])
    columns = [column for column in ("ue_name", "ue_class") if column in manifest]
    result = manifest[columns].rename(columns={"ue_name": "ue"}).drop_duplicates("ue")
    if "ue_class" not in result:
        result["ue_class"] = pd.NA
    return result


def _traffic_frame(run_dir: Path) -> pd.DataFrame:
    frame = kpis.throughput_timeseries(run_dir, window_s=1, per="ue")
    if frame.empty:
        return pd.DataFrame(columns=["utc_second", "ue", "dl_mbps", "ul_mbps"])
    keys = ["ue", "direction"]
    time_key = "utc_second" if "utc_second" in frame else "t_s"
    grouped = frame.groupby([time_key] + keys, as_index=False)["mbps"].sum()
    wide = (grouped.pivot_table(index=[time_key, "ue"], columns="direction",
                                values="mbps", aggfunc="sum")
                   .reset_index().rename_axis(None, axis=1))
    return wide.rename(columns={"dl": "dl_mbps", "ul": "ul_mbps"})


def _derive_cells(run_dir: Path) -> dict[str, int]:
    mapping = prb.load_rnti_map(run_dir)
    if not mapping.empty:
        return {str(row.ue): int(row.cell) for row in mapping.itertuples(index=False)}
    timing = _json(run_dir / schema.LOGS_DIR / "run_timing.json", {}) or {}
    result = {}
    for ue, node in (timing.get("ue_node") or {}).items():
        match = re.fullmatch(r"cell(\d+)", str(node))
        if match:
            result[str(ue)] = int(match.group(1))
    return result


def _label_channel(frame: pd.DataFrame, run_dir: Path) -> pd.DataFrame:
    frame = frame.copy()
    state = run_profile.channel_state(run_dir)
    scheduled = _json(run_dir / schema.CHANNEL_SCHEDULE, {}) or {}
    frame["channel_schedule_enabled"] = bool(scheduled.get("enabled", False))
    frame["channel_state_success"] = bool(state and state.get("success"))
    frame["channel_verified"] = False
    frame["channel_transition_partial"] = False
    if not state or "utc_second" not in frame:
        return frame

    start = float(state.get("traffic_start_reference_epoch") or float("-inf"))
    labels = []
    for item in state.get("initial_state") or []:
        row = dict(item)
        row["effective_second"] = math.floor(start)
        row["is_initial"] = True
        labels.append(row)
    for item in state.get("transitions") or []:
        if item.get("status") != "verified" or not item.get("verified", False):
            continue
        row = dict(item)
        row["effective_second"] = math.ceil(float(
            item.get("applied_epoch") or item.get("scheduler_apply_epoch")))
        row["is_initial"] = False
        labels.append(row)
    labels.sort(key=lambda row: (row["effective_second"], row.get("is_initial") is False))

    for item in labels:
        direction = item.get("direction")
        target = item.get("target")
        parameter = item.get("parameter")
        if direction not in {"dl", "ul"} or not target or not parameter:
            continue
        target_mask = pd.Series(True, index=frame.index)
        if direction == "dl":
            target_mask &= frame["ue"].astype(str) == str(target)
        else:
            match = re.fullmatch(r"cell(\d+)", str(target))
            if not match or "cell" not in frame:
                continue
            target_mask &= frame["cell"].astype("Int64") == int(match.group(1))
        mask = target_mask & (frame["utc_second"] >= item["effective_second"])
        if not item.get("is_initial"):
            applied = float(item.get("applied_epoch") or
                            item.get("scheduler_apply_epoch"))
            if not math.isclose(applied, round(applied), abs_tol=1e-9):
                partial = target_mask & (
                    frame["utc_second"] == math.floor(applied))
                frame.loc[partial, "channel_transition_partial"] = True
                frame.loc[partial, "channel_verified"] = False
        value = item.get("observed")
        frame.loc[mask, f"{direction}_{parameter}"] = value
        if item.get("model_type"):
            frame.loc[mask, f"{direction}_model_type"] = item["model_type"]
        if item.get("model_name"):
            frame.loc[mask, f"{direction}_model_name"] = item["model_name"]
        frame.loc[mask, "channel_verified"] = True
    return frame


def training_frame(run_dir) -> pd.DataFrame:
    """One row per measured UE/UTC second, suitable for model training."""
    run_dir = Path(run_dir)
    radio = prb.prb_timeseries(run_dir)
    if radio.empty and radio.attrs.get("error"):
        raise ValueError(radio.attrs["error"])
    traffic = _traffic_frame(run_dir)
    if radio.empty and traffic.empty:
        return pd.DataFrame()

    join_key = "utc_second" if "utc_second" in traffic and "utc_second" in radio else "t_s"
    if radio.empty:
        frame = traffic.copy()
    elif traffic.empty:
        frame = radio.copy()
    else:
        frame = radio.merge(traffic, on=[join_key, "ue"], how="outer",
                            suffixes=("", "_traffic"))
        if "t_s_traffic" in frame:
            frame["t_s"] = frame.get("t_s").fillna(frame.pop("t_s_traffic"))
    for column in ("dl_mbps", "ul_mbps"):
        if column not in frame:
            frame[column] = 0.0
        else:
            frame[column] = frame[column].fillna(0.0)
    if "utc_second" in frame:
        frame["t_s"] = frame["utc_second"] - frame["utc_second"].min()

    cells = _derive_cells(run_dir)
    if "cell" not in frame:
        frame["cell"] = frame["ue"].map(cells)
    else:
        frame["cell"] = frame["cell"].fillna(frame["ue"].map(cells))
    frame = frame.merge(_ue_metadata(run_dir), on="ue", how="left")
    for direction in ("dl", "ul"):
        if f"{direction}_prb" in frame:
            frame[f"{direction}_bits_per_prb"] = (
                frame[f"{direction}_mbps"] * 1e6 /
                frame[f"{direction}_prb"].replace(0, float("nan")))

    frame.insert(0, "profile_id", run_dir.name)
    frame.insert(0, "execution_id", execution_id(run_dir))
    frame = _label_channel(frame, run_dir)
    order = [column for column in (
        "execution_id", "profile_id", "utc_second", "t_s", "cell", "ue",
        "ue_class", "nb_id", "rnti", "dl_mbps", "ul_mbps", "dl_prb",
        "ul_prb", "dl_bits_per_prb", "ul_bits_per_prb") if column in frame]
    order += [column for column in frame if column not in order]
    return frame[order].sort_values(
        [column for column in ("utc_second", "t_s", "cell", "ue") if column in frame]
    ).reset_index(drop=True)


def _xapp_quality(run_dir: Path) -> dict:
    target = run_dir / schema.LOGS_DIR / "xapp.log"
    text = target.read_text(errors="replace") if target.exists() else ""
    profile = run_profile.load(run_dir)
    expected = ((profile.get("xapp") or {}).get("expected_subscriptions") or 0)
    subscriptions = text.count("Successfully subscribed")
    deletes = text.count("SUBSCRIPTION DELETE RESPONSE rx")
    pattern = re.compile(
        r"assert|aborted|timeout|pending event|connection lost|segmentation|SCTP_SEND_FAILED|(^|[^A-Za-z])ERROR:",
        re.IGNORECASE | re.MULTILINE)
    errors = len(pattern.findall(text))
    return {
        "expected_subscriptions": int(expected),
        "subscriptions": subscriptions,
        "delete_responses": deletes,
        "errors": errors,
        "clean_shutdown": "Test xApp run SUCCESSFULLY" in text,
    }


def quality(run_dir: Path, features: pd.DataFrame) -> dict:
    state = run_profile.channel_state(run_dir)
    schedule = _json(run_dir / schema.CHANNEL_SCHEDULE, {}) or {}
    expected_ues = len(_manifest(run_dir))
    measured_ues = int(features["ue"].nunique()) if not features.empty else 0
    radio_rows = int(features.get("dl_prb", pd.Series(dtype=float)).notna().sum())
    channel_rows = int(features.get(
        "channel_verified", pd.Series(dtype=bool)).fillna(False).sum())
    dual_clock = all(column in features for column in prb.DUAL_CLOCK_COLUMNS)
    if dual_clock and not features.empty:
        receipt_span = (
            features["recv_tstamp_us"].max() - features["recv_tstamp_us"].min()
        ) / 1e6
        source_span = (
            features["source_tstamp_us"].max() - features["source_tstamp_us"].min()
        ) / 1e6
        source_wall_ratio = (
            float(source_span / receipt_span) if receipt_span > 0 else None)
    else:
        source_wall_ratio = None
    return {
        "expected_ues": expected_ues,
        "measured_ues": measured_ues,
        "ue_coverage": (measured_ues / expected_ues if expected_ues else None),
        "feature_rows": len(features),
        "radio_rows": radio_rows,
        "radio_clock": "dual" if dual_clock else (
            "not captured" if not radio_rows else "legacy"),
        "radio_clock_valid": bool(dual_clock or not radio_rows),
        "source_wall_ratio": source_wall_ratio,
        "channel_labeled_rows": channel_rows,
        "channel_schedule_enabled": bool(schedule.get("enabled", False)),
        "channel_state_verified": bool(state and state.get("success")),
        "channel_transitions": len((state or {}).get("transitions") or []),
        "xapp": _xapp_quality(run_dir),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_raw_logs(run_dir: Path, destination: Path) -> None:
    with tarfile.open(destination / "raw_mgen_logs.tar.gz", "w:gz") as archive:
        for source in sorted((run_dir / schema.LOGS_DIR).glob("*.log")):
            archive.add(source, arcname=source.name)


def _copy_reconstruction_inputs(run_dir: Path, destination: Path) -> None:
    """Freeze every regular input needed to rebuild all V2 tables."""
    for name in (
            schema.CONFIG, schema.RUN_PROFILE, schema.CHANNEL_SCHEDULE,
            schema.DESIGNED_KPIS):
        source = run_dir / name
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, destination / name)

    for directory_name in (schema.SCRIPTS_DIR, schema.LOGS_DIR):
        source_root = run_dir / directory_name
        target_root = destination / directory_name
        target_root.mkdir(parents=True, exist_ok=True)
        if not source_root.exists():
            continue
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(source_root)
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _write_checksums(destination: Path) -> None:
    checksums = {}
    for source in sorted(path for path in destination.rglob("*") if path.is_file()):
        relative = str(source.relative_to(destination))
        if relative in {"SHA256SUMS.json", "annotations.json"}:
            continue
        checksums[relative] = _sha256(source)
    (destination / "SHA256SUMS.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n")


def verify_checksums(destination: Path) -> int:
    """Verify an immutable archive before it is used for dataset export.

    ``annotations.json`` is deliberately excluded because curation remains
    editable after archival.  Every other regular file must be listed in the
    manifest and retain its original digest.
    """
    destination = Path(destination)
    manifest_path = destination / "SHA256SUMS.json"
    checksums = _json(manifest_path)
    if not isinstance(checksums, dict) or not checksums:
        raise ValueError(
            f"archive checksum manifest is missing or invalid: {manifest_path}")

    ignored_root_files = {"SHA256SUMS.json", "annotations.json"}
    actual_files = set()
    for source in destination.rglob("*"):
        if not (source.is_file() or source.is_symlink()):
            continue
        relative = str(source.relative_to(destination))
        if relative not in ignored_root_files:
            actual_files.add(relative)
    expected_files = set(checksums)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        unexpected = sorted(actual_files - expected_files)
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unexpected={unexpected}")
        raise ValueError(
            "archive file set does not match checksum manifest: " +
            "; ".join(details))

    root = destination.resolve()
    for relative, expected in checksums.items():
        relative_path = Path(relative)
        if (relative_path.is_absolute() or ".." in relative_path.parts or
                not re.fullmatch(r"[0-9a-f]{64}", str(expected))):
            raise ValueError(
                f"archive checksum manifest contains an invalid entry: {relative}")
        source = destination / relative_path
        if source.is_symlink():
            raise ValueError(f"archive contains a symbolic link: {relative}")
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError):
            raise ValueError(
                f"archive checksum path is missing or unsafe: {relative}") from None
        if not resolved.is_file():
            raise ValueError(f"archive checksum path is not a file: {relative}")
        if _sha256(resolved) != expected:
            raise ValueError(f"archive checksum mismatch: {relative}")
    return len(checksums)


def archive_execution(run_dir, *, include_raw: bool = False) -> Path:
    """Snapshot the current latest execution once; never overwrite it."""
    run_dir = Path(run_dir)
    identifier = execution_id(run_dir)
    root = run_dir / schema.EXECUTIONS_DIR
    destination = root / identifier
    if destination.exists():
        verify_checksums(destination)
        return destination

    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{identifier}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    (temporary / schema.LOGS_DIR).mkdir(parents=True)
    features = training_frame(run_dir)
    if features.empty:
        raise ValueError("no per-second traffic or radio measurements were produced")
    features.to_parquet(temporary / schema.UE_SECOND_FEATURES, index=False)

    tables = dataset_v2.build_tables(run_dir)
    empty = [name for name, frame in tables.items() if frame.empty]
    if empty:
        raise ValueError(f"V2 tables are empty: {empty}")
    for name, frame in tables.items():
        frame.to_parquet(temporary / name, index=False)

    observed = run_dir / schema.OBSERVED_KPIS
    if not observed.exists():
        kpis.save_observed(run_dir)
    shutil.copy2(observed, temporary / schema.OBSERVED_KPIS)
    _copy_reconstruction_inputs(run_dir, temporary)

    if include_raw:
        _write_raw_logs(run_dir, temporary)

    (temporary / schema.MODEL_CONTRACT).write_text(
        json.dumps(dataset_v2.model_contract(), indent=2, sort_keys=True) + "\n")

    q = quality(run_dir, features)
    metadata = {
        "schema_version": schema.SCHEMA_VERSION,
        "execution_id": identifier,
        "profile_id": run_dir.name,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "include_raw_logs": True,
        "compressed_raw_log_copy": bool(include_raw),
        "reconstructable_from_archive": True,
        "table_rows": {name: int(len(frame)) for name, frame in tables.items()},
        "quality": q,
    }
    (temporary / schema.EXECUTION_METADATA).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    _write_checksums(temporary)
    temporary.replace(destination)
    return destination


def list_executions(profiles_dir) -> list[Execution]:
    records = []
    for target in sorted(Path(profiles_dir).glob(
            f"run_*/{schema.EXECUTIONS_DIR}/*/{schema.EXECUTION_METADATA}")):
        metadata = _json(target, {}) or {}
        records.append(Execution(
            str(metadata.get("execution_id") or target.parent.name),
            str(metadata.get("profile_id") or target.parents[2].name),
            target.parent, metadata))
    return sorted(records, key=lambda item: item.metadata.get("archived_at", ""),
                  reverse=True)


def annotations(record: Execution) -> dict:
    return _json(record.path / "annotations.json", {}) or {}


def update_annotations(record: Execution, *, include: bool, tags: str, notes: str) -> Path:
    """Save mutable human curation without changing checksummed measurements."""
    target = record.path / "annotations.json"
    target.write_text(json.dumps({
        "include": bool(include),
        "tags": [item.strip() for item in str(tags).split(",") if item.strip()],
        "notes": str(notes).strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, sort_keys=True) + "\n")
    return target


def _split(identifier: str) -> str:
    bucket = int(hashlib.sha256(identifier.encode()).hexdigest()[:8], 16) % 10
    return "train" if bucket < 8 else ("validation" if bucket == 8 else "test")


def export(records: list[Execution], destination: Path, *, include_csv: bool = False) -> Path:
    if not records:
        raise ValueError("select at least one archived execution")
    verified = {}
    for record in records:
        verified[record.execution_id] = verify_checksums(record.path)
        if int(record.metadata.get("schema_version", 0)) != schema.SCHEMA_VERSION:
            raise ValueError(
                f"{record.execution_id} uses schema version "
                f"{record.metadata.get('schema_version')}; V2 export accepts only "
                f"schema version {schema.SCHEMA_VERSION}")
        missing = [name for name in schema.V2_TABLES
                   if not (record.path / name).is_file()]
        if missing:
            raise ValueError(
                f"{record.execution_id} is missing V2 tables: {missing}")
        quality_doc = record.metadata.get("quality", {}) or {}
        if (quality_doc.get("radio_rows", 0) and
                not quality_doc.get("radio_clock_valid", False)):
            raise ValueError(
                f"{record.execution_id} is a pre-dual-clock radio capture. "
                "Unselect it or repeat the execution after the FlexRIC receipt-"
                "timestamp patch; RFsim source time cannot be joined to MGEN UTC")

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"dataset destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        table_frames = {name: [] for name in schema.V2_TABLES}
        manifest = []
        for record in records:
            split = _split(record.execution_id)
            for name in schema.V2_TABLES:
                frame = pd.read_parquet(record.path / name)
                frame["split"] = split
                table_frames[name].append(frame)
            manifest.append({
                "execution_id": record.execution_id,
                "profile_id": record.profile_id,
                "split": split,
                "archive_files_verified": verified[record.execution_id],
                "quality": record.metadata.get("quality", {}),
                "annotations": annotations(record),
            })
        table_rows = {}
        for name, frames in table_frames.items():
            combined = pd.concat(frames, ignore_index=True)
            combined.to_parquet(temporary / name, index=False)
            table_rows[name] = int(len(combined))
            if include_csv:
                combined.to_csv(temporary / Path(name).with_suffix(".csv"), index=False)
        (temporary / schema.MODEL_CONTRACT).write_text(
            json.dumps(dataset_v2.model_contract(), indent=2, sort_keys=True) + "\n")
        (temporary / schema.DATASET_MANIFEST).write_text(json.dumps({
            "schema_version": schema.SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "split_unit": "execution_id",
            "archive_checksums_verified": True,
            "table_rows": table_rows,
            "executions": manifest,
        }, indent=2, sort_keys=True) + "\n")
        _write_checksums(temporary)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination
