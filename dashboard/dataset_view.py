"""Inspect captures, curate immutable executions, and export training datasets."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from twindash import dataset, schema, settings


def _capture_signature(run_dir: Path) -> tuple:
    """Invalidate the cached preview whenever a collected artifact changes."""
    candidates = [
        run_dir / schema.CONFIG,
        run_dir / schema.RUN_PROFILE,
        run_dir / schema.CHANNEL_SCHEDULE,
        run_dir / schema.OBSERVED_KPIS,
    ]
    candidates.extend((run_dir / schema.LOGS_DIR).glob("*"))
    candidates.extend((run_dir / schema.SCRIPTS_DIR).glob("*.csv"))
    result = []
    for target in sorted(path for path in candidates if path.is_file()):
        stat = target.stat()
        result.append((str(target.relative_to(run_dir)), stat.st_mtime_ns,
                       stat.st_size))
    return tuple(result)


@st.cache_data(show_spinner=False)
def _load_capture(run_dir_value: str, signature: tuple):
    """Build the same tables that will be frozen by archive_execution()."""
    del signature  # its value is the Streamlit cache key
    run_dir = Path(run_dir_value)
    features = dataset.training_frame(run_dir)
    observed_path = run_dir / schema.OBSERVED_KPIS
    observed = (pd.read_parquet(observed_path) if observed_path.exists()
                else pd.DataFrame())
    return features, observed, dataset.quality(run_dir, features)


def _coverage_table(features: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = len(features)
    for column in features.columns:
        present = int(features[column].notna().sum())
        rows.append({
            "feature": column,
            "dtype": str(features[column].dtype),
            "non-null rows": present,
            "coverage": 100.0 * present / total if total else 0.0,
        })
    return pd.DataFrame(rows)


def _capture_inventory(run_dir: Path, features: pd.DataFrame,
                       observed: pd.DataFrame, quality: dict) -> pd.DataFrame:
    traffic_rows = int(features[[
        column for column in ("dl_mbps", "ul_mbps") if column in features
    ]].notna().any(axis=1).sum()) if not features.empty else 0
    xapp = quality.get("xapp") or {}
    expected_subscriptions = int(xapp.get("expected_subscriptions", 0))
    subscriptions = int(xapp.get("subscriptions", 0))
    radio_status = "Captured" if quality.get("radio_rows", 0) else (
        "Not requested" if not expected_subscriptions else "Missing")
    if quality.get("channel_schedule_enabled"):
        channel_status = ("Verified" if quality.get("channel_state_verified")
                          else "Missing / failed")
    else:
        channel_status = "Not scheduled"

    provenance = [schema.CONFIG, schema.RUN_PROFILE]
    provenance += [f"{schema.LOGS_DIR}/{name}" for name in dataset.CONTROL_LOGS]
    provenance_present = sum((run_dir / name).exists() for name in provenance)
    return pd.DataFrame([
        {
            "capture group": "MGEN traffic and flow KPIs",
            "status": "Captured" if len(observed) and traffic_rows else "Missing",
            "rows": f"{len(observed):,} flows; {traffic_rows:,} UE-seconds",
            "details": "throughput, packets, bytes, loss, latency, jitter, app, direction",
        },
        {
            "capture group": "RIC / xApp radio measurements",
            "status": radio_status,
            "rows": f"{int(quality.get('radio_rows', 0)):,} UE-seconds",
            "details": (
                f"subscriptions {subscriptions}/{expected_subscriptions}; "
                f"clean shutdown: {'yes' if xapp.get('clean_shutdown') else 'no'}; "
                f"errors: {int(xapp.get('errors', 0))}"
            ),
        },
        {
            "capture group": "Verified channel labels",
            "status": channel_status,
            "rows": f"{int(quality.get('channel_labeled_rows', 0)):,} UE-seconds",
            "details": (
                f"{int(quality.get('channel_transitions', 0))} transition(s); "
                "initial state and every applied change are read back"
            ),
        },
        {
            "capture group": "Mapping, clocks, and provenance",
            "status": "Captured" if provenance_present else "Missing",
            "rows": f"{provenance_present}/{len(provenance)} artifacts",
            "details": (
                "run profile, timing anchors, UE/RNTI map, schedule, "
                "xApp log, checksums"
            ),
        },
    ])


def _render_latest_capture(run_dir: Path):
    st.subheader("1. Latest captured execution")
    st.caption(
        "This is the mutable capture currently under the selected run. The preview "
        "uses the same transformation that will produce the archived training table.")
    try:
        with st.spinner("Inspecting the collected traffic, radio, and channel artifacts…"):
            features, observed, quality = _load_capture(
                str(run_dir), _capture_signature(run_dir))
    except Exception as exc:
        st.warning(
            "No complete execution can be inspected yet. Run the experiment and "
            f"collect its logs first. Details: {exc}")
        return None, pd.DataFrame(), pd.DataFrame(), {}
    if features.empty:
        st.warning(
            "The collected artifacts did not produce any model-ready UE-second "
            "rows. Review the Results page before archiving this execution.")
        return None, features, observed, quality

    execution = (str(features["execution_id"].iloc[0])
                 if not features.empty and "execution_id" in features else "unknown")
    expected = int(quality.get("expected_ues", 0))
    measured = int(quality.get("measured_ues", 0))
    time_column = "utc_second" if "utc_second" in features else "t_s"
    seconds = int(features[time_column].nunique()) if time_column in features else 0
    metrics = st.columns(5)
    metrics[0].metric("Execution", execution)
    metrics[1].metric("UE coverage", f"{measured}/{expected}")
    metrics[2].metric("UE-second rows", f"{len(features):,}")
    metrics[3].metric("Flow KPI rows", f"{len(observed):,}")
    metrics[4].metric("UTC-second buckets", f"{seconds:,}")

    issues = []
    if expected and measured < expected:
        issues.append(f"only {measured}/{expected} expected UEs have measurements")
    if observed.empty:
        issues.append("no flow KPI rows were found")
    xapp = quality.get("xapp") or {}
    if xapp.get("expected_subscriptions") and (
            not xapp.get("clean_shutdown") or xapp.get("errors")):
        issues.append("the xApp capture was not clean")
    if (quality.get("channel_schedule_enabled") and
            not quality.get("channel_state_verified")):
        issues.append("the requested channel schedule was not fully verified")
    if issues:
        st.warning("Capture needs review: " + "; ".join(issues) + ".")
    else:
        channel_note = ("verified channel labels included" if
                        quality.get("channel_state_verified") else
                        "no channel schedule was requested")
        st.success(f"Capture is internally consistent; {channel_note}.")

    st.markdown("#### What was captured")
    st.dataframe(
        _capture_inventory(run_dir, features, observed, quality),
        hide_index=True, width="stretch")

    with st.expander(f"Inspect {len(features.columns)} model-ready features and sample rows"):
        coverage = _coverage_table(features)
        st.dataframe(
            coverage, hide_index=True, width="stretch",
            column_config={
                "coverage": st.column_config.ProgressColumn(
                    "coverage", min_value=0.0, max_value=100.0, format="%.1f%%"),
            })
        st.caption("Sample of the exact UE-second table that will be archived:")
        st.dataframe(features.head(25), hide_index=True, width="stretch")
    return execution, features, observed, quality


def render(run_dir: Path, profiles_dir: Path) -> None:
    st.title("Dataset")
    st.caption(
        "Inspect what the selected POWDER execution captured, freeze clean runs "
        "as immutable archives, then combine selected executions into model-ready "
        "train/validation/test tables. Splits are assigned by execution, never by row.")

    execution, _, _, _ = _render_latest_capture(run_dir)

    st.divider()
    st.subheader("2. Archive this capture")
    records = dataset.list_executions(profiles_dir)
    archived_current = next((record for record in records
                             if record.execution_id == execution and
                             record.profile_id == run_dir.name), None)
    if archived_current:
        st.info(
            f"{execution} is already archived with checksums at "
            f"{archived_current.path}.")
    include_raw = st.checkbox(
        "Include compressed raw MGEN logs in this archive", value=False,
        help="Derived Parquet and provenance files are always saved. Raw logs use more space.")
    if st.button("Archive the latest captured execution", type="primary",
                 disabled=execution is None):
        try:
            with st.spinner("Building per-UE/per-second features and checksums…"):
                target = dataset.archive_execution(run_dir, include_raw=include_raw)
            st.success(f"Archived {target.name} under {target.parent}.")
            records = dataset.list_executions(profiles_dir)
        except Exception as exc:
            st.error(f"Archive failed: {exc}")

    st.divider()
    st.subheader("3. Curate archived executions")
    if not records:
        st.info("No archived executions yet. Archive the inspected capture above first.")
        return
    table = []
    for record in records:
        quality = record.metadata.get("quality") or {}
        xapp = quality.get("xapp") or {}
        annotations = dataset.annotations(record)
        if quality.get("channel_schedule_enabled"):
            channel_labels = ("verified" if quality.get("channel_state_verified")
                              else "missing / failed")
        else:
            channel_labels = "not scheduled"
        table.append({
            "include": annotations.get("include", True),
            "execution": record.execution_id,
            "profile": record.profile_id,
            "UE coverage": f"{quality.get('measured_ues', 0)}/{quality.get('expected_ues', 0)}",
            "feature rows": quality.get("feature_rows", 0),
            "radio rows": quality.get("radio_rows", 0),
            "channel labels": channel_labels,
            "xApp clean": xapp.get("clean_shutdown", False),
            "tags": ", ".join(annotations.get("tags") or []),
            "notes": annotations.get("notes", ""),
            "archived": record.metadata.get("archived_at"),
        })
    catalog = st.data_editor(
        pd.DataFrame(table), hide_index=True, width="stretch",
        disabled=["execution", "profile", "UE coverage", "feature rows",
                  "radio rows", "channel labels", "xApp clean", "archived"],
        column_config={
            "include": st.column_config.CheckboxColumn(
                "Include", help="Include this complete execution in the next export"),
            "tags": st.column_config.TextColumn("Tags (comma-separated)"),
            "notes": st.column_config.TextColumn("Notes"),
        })
    by_key = {(record.execution_id, record.profile_id): record for record in records}
    if st.button("Save include flags, tags, and notes"):
        for row in catalog.itertuples(index=False):
            dataset.update_annotations(
                by_key[(str(row.execution), str(row.profile))],
                include=bool(row.include), tags=str(row.tags or ""),
                notes=str(row.notes or ""))
        st.success("Saved dataset curation without changing measurement checksums.")

    selected = [
        by_key[(str(row.execution), str(row.profile))]
        for row in catalog.itertuples(index=False) if bool(row.include)
    ]
    st.divider()
    st.subheader("4. Export a training dataset")
    selected_rows = sum(int(record.metadata.get("quality", {}).get(
        "feature_rows", 0)) for record in selected)
    st.caption(
        f"Selected: {len(selected)} complete execution(s), "
        f"{selected_rows:,} UE-second rows. Execution-level splitting prevents "
        "measurements from the same run leaking across data splits.")
    default_name = "ric5g_dataset_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    name = st.text_input("Dataset name", default_name)
    include_csv = st.checkbox("Also write CSV copies", value=False)
    valid_name = bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""))
    if name and not valid_name:
        st.error("Dataset name may contain only letters, digits, dot, dash, and underscore.")
    if st.button("Export selected executions", disabled=not selected or not valid_name):
        try:
            target = dataset.export(
                selected, settings.datasets_dir() / name, include_csv=include_csv)
            st.success(
                f"Exported {target}. It contains ue_second_features.parquet, "
                "observed_kpis.parquet, and dataset_manifest.json.")
        except Exception as exc:
            st.error(f"Dataset export failed: {exc}")
