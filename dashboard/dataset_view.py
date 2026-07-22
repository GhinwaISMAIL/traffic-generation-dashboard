"""Streamlit workflow for immutable execution archives and dataset exports."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from twindash import dataset, settings


def render(run_dir: Path, profiles_dir: Path) -> None:
    st.title("Dataset")
    st.caption(
        "Archive each real POWDER execution separately, then export model-ready "
        "flow and UE-second tables. Splits are assigned by execution, never by row.")
    include_raw = st.checkbox(
        "Include compressed raw MGEN logs in this archive", value=False,
        help="Derived Parquet and provenance files are always saved. Raw logs use more space.")
    if st.button("Archive the selected run's latest execution", type="primary"):
        try:
            with st.spinner("Building per-UE/per-second features and checksums…"):
                target = dataset.archive_execution(run_dir, include_raw=include_raw)
            st.success(f"Archived {target.name} under {target.parent}.")
        except Exception as exc:
            st.error(f"Archive failed: {exc}")

    records = dataset.list_executions(profiles_dir)
    if not records:
        st.info("No archived executions yet. Complete a run or archive the latest one above.")
        return
    table = []
    for record in records:
        quality = record.metadata.get("quality") or {}
        xapp = quality.get("xapp") or {}
        annotations = dataset.annotations(record)
        table.append({
            "include": annotations.get("include", True),
            "execution": record.execution_id,
            "profile": record.profile_id,
            "UE coverage": f"{quality.get('measured_ues', 0)}/{quality.get('expected_ues', 0)}",
            "feature rows": quality.get("feature_rows", 0),
            "channel verified": quality.get("channel_state_verified", False),
            "xApp clean": xapp.get("clean_shutdown", False),
            "tags": ", ".join(annotations.get("tags") or []),
            "notes": annotations.get("notes", ""),
            "archived": record.metadata.get("archived_at"),
        })
    st.subheader("Execution catalog")
    catalog = st.data_editor(
        pd.DataFrame(table), hide_index=True, use_container_width=True,
        disabled=["execution", "profile", "UE coverage", "feature rows",
                  "channel verified", "xApp clean", "archived"],
        column_config={
            "include": st.column_config.CheckboxColumn(
                "Include", help="Include this complete execution in the next export"),
            "tags": st.column_config.TextColumn("Tags (comma-separated)"),
            "notes": st.column_config.TextColumn("Notes"),
        })
    by_id = {record.execution_id: record for record in records}
    if st.button("Save include flags, tags, and notes"):
        for row in catalog.itertuples(index=False):
            dataset.update_annotations(
                by_id[str(row.execution)], include=bool(row.include),
                tags=str(row.tags or ""), notes=str(row.notes or ""))
        st.success("Saved dataset curation without changing measurement checksums.")

    selected = [by_id[str(row.execution)] for row in catalog.itertuples(index=False)
                if bool(row.include)]
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
