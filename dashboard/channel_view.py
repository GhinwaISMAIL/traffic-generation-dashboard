"""Streamlit editor for verified RFsim runtime channel schedules."""
from __future__ import annotations

from pathlib import Path
import json

import pandas as pd
import streamlit as st

from twindash import channel, ric5g


def _rows(value: dict) -> pd.DataFrame:
    rows = value.get("events") or []
    if not rows:
        rows = [{
            "at_s": 0.0, "target": "", "direction": "dl",
            "parameter": "noise_power_dB", "value": -30.0,
        }]
    return pd.DataFrame(rows, columns=(
        "at_s", "target", "direction", "parameter", "value"))


def render(run_dir: Path, cfg: dict) -> None:
    st.markdown("#### Channel schedule")
    st.caption(
        "Define RFsim impairments for this generated run before deployment. "
        "Transitions run on the "
        "traffic clock, are read back from the live model, and are saved in "
        "logs/channel_state.json for dataset labels.")
    if not ric5g.is_config(cfg):
        st.info("Select and save a RIC5G distributed testbed to use runtime channel control.")
        return
    ues, cells = channel.topology(cfg, run_dir)
    if not ues:
        st.error("The generated run UEs do not match the active Testbed configuration.")
        return
    try:
        current = channel.load(run_dir)
    except (OSError, ValueError) as exc:
        st.error(f"Could not read channel_schedule.json: {exc}")
        current = channel.empty()
    run_duration = channel.duration(run_dir)

    st.info(
        "The channel family is a boot-time profile parameter. This panel validates "
        "that family and schedules numeric changes only after UEs have attached. "
        "Downlink targets one UE; uplink is shared by all UEs in a cell.")
    columns = st.columns(3)
    enabled = columns[0].toggle(
        "Enable schedule", value=bool(current.get("enabled", False)))
    model = columns[1].selectbox(
        "Expected boot model", channel.MODEL_TYPES,
        index=channel.MODEL_TYPES.index(current.get("expected_model_type", "AWGN"))
        if current.get("expected_model_type", "AWGN") in channel.MODEL_TYPES else 0,
        help="Deployment aborts if the live model differs. This does not change the boot model.")
    columns[2].metric("Traffic duration", f"{run_duration:g} s")

    targets = (["all_ues"] + [f"cell{cell}_ues" for cell in sorted(cells)] +
               list(ues) + ["all_cells"] +
               [f"cell{cell}" for cell in sorted(cells)])
    st.caption(
        "Group targets expand into exact labels when saved: all_ues/cellN_ues "
        "are DL groups; all_cells/cellN are UL groups.")
    edited = st.data_editor(
        _rows(current), num_rows="dynamic", hide_index=True,
        width="stretch", key=f"channel_{run_dir.name}",
        column_config={
            "at_s": st.column_config.NumberColumn(
                "Time after traffic start (s)", min_value=0.0,
                max_value=run_duration, step=1.0),
            "target": st.column_config.SelectboxColumn("Target", options=targets),
            "direction": st.column_config.SelectboxColumn(
                "Direction", options=["dl", "ul"]),
            "parameter": st.column_config.SelectboxColumn(
                "Parameter", options=channel.PARAMETERS),
            "value": st.column_config.NumberColumn("Value", step=1.0),
        })
    schedule = {
        "schema_version": 1,
        "enabled": bool(enabled),
        "expected_model_type": model,
        "events": [
            {"at_s": row.at_s, "target": row.target,
             "direction": row.direction, "parameter": row.parameter,
             "value": row.value}
            for row in edited.itertuples(index=False)
            if not pd.isna(row.target) and str(row.target or "").strip()
            and not pd.isna(row.at_s)
        ],
    }
    try:
        schedule = channel.expand_groups(schedule, run_dir, cfg)
        errors = channel.validate(schedule, run_dir, cfg)
    except ValueError as exc:
        errors = [str(exc)]
    for error in errors:
        st.error(error)

    st.warning(
        "Use values you have calibrated for this topology. A schedule starts only "
        "after attachment, but severe noise/path loss can still disconnect a UE.")
    c1, c2 = st.columns(2)
    if c1.button("Save channel schedule", disabled=bool(errors), type="primary"):
        target = channel.save(run_dir, schedule, cfg)
        st.success(f"Saved {target}. The next deployment will apply and verify it.")
    if c2.button("Disable schedule"):
        disabled = channel.empty()
        disabled["expected_model_type"] = model
        target = channel.save(run_dir, disabled, cfg)
        st.success(f"Disabled runtime channel changes in {target}.")

    state = run_dir / "logs" / "channel_state.json"
    if state.exists():
        with st.expander("Latest verified channel state"):
            st.json(json.loads(state.read_text()))
