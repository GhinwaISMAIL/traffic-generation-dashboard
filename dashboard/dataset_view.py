"""Inspect captures, curate immutable executions, and export training datasets."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

import pandas as pd
import pyarrow.parquet as pq
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
            "type": str(features[column].dtype),
            "rows with a value": present,
            "row completeness": 100.0 * present / total if total else 0.0,
        })
    return pd.DataFrame(rows)


def _parquet_rows(path: Path) -> int:
    """Read a Parquet row count without loading the table into memory."""
    return int(pq.ParquetFile(path).metadata.num_rows)


def _parquet_head(path: Path, rows: int) -> pd.DataFrame:
    """Read only the first few rows of a Parquet file."""
    parquet = pq.ParquetFile(path)
    if rows <= 0 or parquet.num_row_groups == 0:
        return pd.DataFrame()
    return parquet.read_row_group(0).slice(0, rows).to_pandas()


def _export_plan(records: list[dataset.Execution]) -> pd.DataFrame:
    """Describe the exact archives and deterministic splits used by export()."""
    rows = []
    for record in records:
        quality = record.metadata.get("quality") or {}
        xapp = quality.get("xapp") or {}
        if quality.get("channel_schedule_enabled"):
            channel_labels = ("verified" if quality.get("channel_state_verified")
                              else "missing / failed")
        else:
            channel_labels = "not scheduled"
        flow_path = record.path / schema.OBSERVED_KPIS
        rows.append({
            "execution": record.execution_id,
            "profile": record.profile_id,
            "assigned split": dataset._split(record.execution_id),
            "UEs represented": (
                f"{quality.get('measured_ues', 0)}/"
                f"{quality.get('expected_ues', 0)}"
            ),
            "model rows": int(quality.get("feature_rows", 0)),
            "flow KPI records": _parquet_rows(flow_path),
            "radio-labelled rows": int(quality.get("radio_rows", 0)),
            "channel labels": channel_labels,
            "clean xApp shutdown": bool(xapp.get("clean_shutdown", False)),
        })
    return pd.DataFrame(rows)


def _export_sample_signature(records: list[dataset.Execution]) -> tuple:
    """Build a stable cache key for immutable archive preview files."""
    result = []
    for record in records:
        files = []
        for name in (schema.UE_SECOND_FEATURES, schema.OBSERVED_KPIS):
            target = record.path / name
            stat = target.stat()
            files.append((name, stat.st_mtime_ns, stat.st_size))
        result.append((record.execution_id, record.profile_id, str(record.path),
                       tuple(files)))
    return tuple(result)


@st.cache_data(show_spinner=False)
def _load_export_samples(signature: tuple, rows_per_execution: int = 5):
    """Load a small preview transformed exactly like dataset.export()."""
    feature_frames = []
    flow_frames = []
    for execution_id, profile_id, path_value, _ in signature:
        archive = Path(path_value)
        split = dataset._split(execution_id)

        features = _parquet_head(
            archive / schema.UE_SECOND_FEATURES, rows_per_execution).copy()
        features["split"] = split
        feature_frames.append(features)

        flows = _parquet_head(
            archive / schema.OBSERVED_KPIS, rows_per_execution).copy()
        flows["execution_id"] = execution_id
        flows["profile_id"] = profile_id
        flows["split"] = split
        flow_frames.append(flows)

    return (
        pd.concat(feature_frames, ignore_index=True) if feature_frames
        else pd.DataFrame(),
        pd.concat(flow_frames, ignore_index=True) if flow_frames
        else pd.DataFrame(),
    )


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
    if quality.get("radio_rows", 0) and not quality.get("radio_clock_valid", False):
        radio_status = "Invalid legacy clock"
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
                f"errors: {int(xapp.get('errors', 0))}; "
                f"clock: {quality.get('radio_clock', 'unknown')}; "
                f"source/wall ratio: "
                f"{quality.get('source_wall_ratio') if quality.get('source_wall_ratio') is not None else 'n/a'}"
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
    metrics[1].metric("UEs represented", f"{measured}/{expected}")
    metrics[2].metric("Model rows (UE × second)", f"{len(features):,}")
    metrics[3].metric("Per-flow KPI records", f"{len(observed):,}")
    metrics[4].metric("Distinct observed seconds", f"{seconds:,}")
    st.caption(
        "UEs represented counts expected UEs that appear at least once in the "
        "capture. It does not mean every UE has a row in every second.")

    issues = []
    if expected and measured < expected:
        issues.append(f"only {measured}/{expected} expected UEs have measurements")
    if observed.empty:
        issues.append("no flow KPI rows were found")
    xapp = quality.get("xapp") or {}
    if xapp.get("expected_subscriptions") and (
            not xapp.get("clean_shutdown") or xapp.get("errors")):
        issues.append("the xApp capture was not clean")
    if (xapp.get("expected_subscriptions") and
            not quality.get("radio_clock_valid", False)):
        issues.append("radio rows lack a verified core receipt clock")
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

    with st.expander(
            f"Inspect {len(features.columns)} exported columns and sample model rows"):
        st.caption(
            f"Row completeness = rows containing a measured value ÷ all "
            f"{len(features):,} model rows. Radio and channel columns can be lower "
            "because their capture windows cover only part of the traffic run. "
            "A missing value means ‘not measured for this UE-second,’ not zero.")
        coverage = _coverage_table(features)
        st.dataframe(
            coverage, hide_index=True, width="stretch",
            column_config={
                "row completeness": st.column_config.ProgressColumn(
                    "row completeness", min_value=0.0, max_value=100.0,
                    format="%.1f%%"),
            })
        st.caption("Sample of the exact UE × second table that will be archived:")
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
            "UEs represented": (
                f"{quality.get('measured_ues', 0)}/"
                f"{quality.get('expected_ues', 0)}"
            ),
            "model rows": quality.get("feature_rows", 0),
            "radio-labelled rows": quality.get("radio_rows", 0),
            "radio timing": quality.get("radio_clock", "legacy / unknown"),
            "channel-label status": channel_labels,
            "clean xApp shutdown": xapp.get("clean_shutdown", False),
            "tags": ", ".join(annotations.get("tags") or []),
            "notes": annotations.get("notes", ""),
            "archived": record.metadata.get("archived_at"),
        })
    catalog = st.data_editor(
        pd.DataFrame(table), hide_index=True, width="stretch",
        disabled=["execution", "profile", "UEs represented", "model rows",
                  "radio-labelled rows", "radio timing", "channel-label status",
                  "clean xApp shutdown", "archived"],
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
    st.markdown("#### Export preview")
    if selected:
        plan = _export_plan(selected)
        selected_rows = int(plan["model rows"].sum())
        flow_rows = int(plan["flow KPI records"].sum())
        summary = st.columns(4)
        summary[0].metric("Selected executions", f"{len(selected):,}")
        summary[1].metric("UE × second model rows", f"{selected_rows:,}")
        summary[2].metric("Per-flow KPI records", f"{flow_rows:,}")
        split_counts = plan["assigned split"].value_counts()
        split_text = " · ".join(
            f"{name} {int(split_counts.get(name, 0))}"
            for name in ("train", "validation", "test")
        )
        summary[3].metric("Execution splits", split_text)
        st.caption(
            "This is the exact archive selection currently marked Include. "
            "Each execution is assigned deterministically to one split, so rows "
            "from the same run cannot leak across train, validation, and test.")
        st.dataframe(plan, hide_index=True, width="stretch")

        split_summary = (plan.groupby("assigned split", as_index=False)
                         .agg(executions=("execution", "count"),
                              model_rows=("model rows", "sum"),
                              flow_KPI_records=("flow KPI records", "sum"),
                              radio_labelled_rows=("radio-labelled rows", "sum")))
        split_summary = split_summary.rename(columns={
            "model_rows": "model rows",
            "flow_KPI_records": "flow KPI records",
            "radio_labelled_rows": "radio-labelled rows",
        })
        split_summary["assigned split"] = pd.Categorical(
            split_summary["assigned split"],
            categories=["train", "validation", "test"], ordered=True)
        split_summary = split_summary.sort_values("assigned split")
        with st.expander("Inspect output tables and sample exported rows"):
            st.markdown(
                "- **`ue_second_features.parquet`** — one model row for each "
                "observed UE and second, with traffic, radio, channel, provenance, "
                "and `split` columns.\n"
                "- **`observed_kpis.parquet`** — one record for each observed MGEN "
                "flow KPI, with execution, profile, and `split` columns.\n"
                "- **`dataset_manifest.json`** — selected executions, quality "
                "metadata, annotations, split assignments, and checksum results.\n"
                "- Optional CSV copies contain the same two tables.")
            st.caption("Planned row counts by execution-level split:")
            st.dataframe(split_summary, hide_index=True, width="stretch")
            if st.checkbox("Load sample rows from the selected archives"):
                with st.spinner("Reading small samples from the immutable archives…"):
                    feature_sample, flow_sample = _load_export_samples(
                        _export_sample_signature(selected))
                st.caption("Sample from `ue_second_features.parquet`:")
                st.dataframe(feature_sample, hide_index=True, width="stretch")
                st.caption("Sample from `observed_kpis.parquet`:")
                st.dataframe(flow_sample, hide_index=True, width="stretch")
    else:
        st.info(
            "No archived executions are selected. Mark Include in the curation "
            "table above to preview and export a dataset.")
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
