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


def _import_csv(run_dir: Path, cfg: dict, model: str, method: str):
    if method == "Upload CSV":
        source = st.file_uploader(
            "Channel schedule CSV", type=["csv"], max_upload_size=1024,
            key=f"channel_upload_{run_dir.name}")
    else:
        filename = st.text_input(
            "Local channel CSV path", key=f"channel_csv_path_{run_dir.name}",
            help="A path on the computer running TwinDash.").strip()
        source = Path(filename).expanduser() if filename else None
    if source is None:
        st.info("Choose a channel CSV to validate and preview.")
        return None
    try:
        schedule = channel.load_csv(source, run_dir, cfg, expected_model_type=model)
    except (OSError, ValueError) as exc:
        st.error(str(exc))
        return None
    events = schedule["events"]
    st.success("Channel CSV valid")
    columns = st.columns(3)
    columns[0].metric("Channel changes", f"{len(events):,}")
    columns[1].metric("Downlink UEs", len({e["target"] for e in events if e["direction"] == "dl"}))
    columns[2].metric("Uplink cells", len({e["target"] for e in events if e["direction"] == "ul"}))
    st.caption(
        f"First change: {events[0]['at_s']:g} s · Last change: {events[-1]['at_s']:g} s. "
        "Showing the first 20 normalized rows; saving includes every validated change.")
    st.dataframe(pd.DataFrame(events[:20], columns=channel.CSV_COLUMNS),
                 hide_index=True, width="stretch")
    st.download_button(
        "Download normalized channel CSV", channel.to_csv(schedule),
        file_name="channel_schedule.csv", mime="text/csv",
        key=f"channel_normalized_download_{run_dir.name}")
    return schedule


def _disable_schedule(run_dir: Path, cfg: dict, model: str):
    value = channel.empty()
    value["expected_model_type"] = model
    try:
        target = channel.save(run_dir, value, cfg)
        st.session_state[f"channel_enabled_{run_dir.name}"] = False
        message = ("success", f"Disabled runtime channel changes in {target}.")
    except (OSError, ValueError) as exc:
        message = ("error", str(exc))
    st.session_state[f"channel_message_{run_dir.name}"] = message


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
        "Enable schedule", value=bool(current.get("enabled", False)),
        key=f"channel_enabled_{run_dir.name}")
    model = columns[1].selectbox(
        "Expected boot model", channel.MODEL_TYPES,
        index=channel.MODEL_TYPES.index(current.get("expected_model_type", "AWGN"))
        if current.get("expected_model_type", "AWGN") in channel.MODEL_TYPES else 0,
        help="Deployment aborts if the live model differs. This does not change the boot model.",
        key=f"channel_model_{run_dir.name}")
    columns[2].metric("Traffic duration", f"{run_duration:g} s")

    targets = (["all_ues"] + [f"cell{cell}_ues" for cell in sorted(cells)] +
               list(ues) + ["all_cells"] +
               [f"cell{cell}" for cell in sorted(cells)])
    st.caption(
        "Group targets expand into exact labels when saved: all_ues/cellN_ues "
        "are DL groups; all_cells/cellN are UL groups.")
    method = st.radio(
        "Channel schedule input", ["Manual table", "Upload CSV", "Local CSV file"],
        horizontal=True, key=f"channel_input_{run_dir.name}")
    with st.expander("Channel CSV schema and UE mapping"):
        st.code(
            "at_s,target,direction,parameter,value\n"
            "0,ue1,dl,noise_power_dB,-30\n"
            "0,ue1,dl,ploss,10\n"
            "60,ue2,dl,noise_power_dB,-25\n"
            "60,cell1,ul,ploss,12", language="text")
        st.caption("Example values show the format. Use your own calibrated values.")
        st.markdown(
            "Use UTF-8 CSV with these five columns. `at_s` is seconds after traffic start; "
            "decimal seconds are accepted. Use one row per parameter change. "
            "For noise use `noise_power_dB`; for path loss use `ploss`. Values are applied "
            "as supplied, with no calibration or unit conversion. Other supported parameters: "
            "`riceanf`, `aoa`, `offset`, `forgetf`.\n\n"
            "Downlink targets a generated UE such as `ue1`, or its experimental identifier "
            "from the mapping below. Uplink targets a cell such as `cell1`. "
            "Duplicate changes for the same time, target, direction and parameter are rejected. "
            "Values stay in effect until the next change; there is no interpolation.")
        st.download_button(
            "Download channel CSV template", channel.to_csv(channel.empty()),
            file_name="channel_schedule_template.csv", mime="text/csv",
            key=f"channel_template_{run_dir.name}")
        try:
            aliases = channel.ue_aliases(run_dir)
            names = {value: key for key, value in aliases.items()}
            mapping = [{"experimental_ue": names.get(name, ""), "target": name,
                        "cell": f"cell{item['cell']}"} for name, item in ues.items()]
            st.dataframe(pd.DataFrame(mapping), hide_index=True, width="stretch")
        except (OSError, ValueError) as exc:
            st.error(str(exc))
    errors = []
    if method == "Manual table":
        if len(current.get("events") or []) > 2000:
            events = current["events"]
            st.info(f"The saved schedule contains {len(events):,} changes. "
                    "Showing the first 20; upload a CSV to replace it.")
            st.dataframe(pd.DataFrame(events[:20]), hide_index=True, width="stretch")
        else:
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
            events = [
                {"at_s": row.at_s, "target": row.target, "direction": row.direction,
                 "parameter": row.parameter, "value": row.value}
                for row in edited.itertuples(index=False)
                if not pd.isna(row.target) and str(row.target or "").strip()
                and not pd.isna(row.at_s)
            ]
        schedule = {"schema_version": 1, "enabled": bool(enabled),
                    "expected_model_type": model, "events": events}
        try:
            schedule = channel.expand_groups(schedule, run_dir, cfg)
            errors = channel.validate(schedule, run_dir, cfg)
        except ValueError as exc:
            errors = [str(exc)]
        for error in errors[:20]:
            st.error(error)
    else:
        schedule = _import_csv(run_dir, cfg, model, method)
        if schedule is not None:
            schedule["enabled"] = bool(enabled)
            if not enabled:
                st.info("Turn on Enable schedule, then save to use this CSV for the next deployment.")

    active = channel.deployment_active(run_dir)
    if active:
        st.info("This run is being deployed. You can preview a CSV; saving is available after it finishes.")

    st.warning(
        "Use values you have calibrated for this topology. A schedule starts only "
        "after attachment, but severe noise/path loss can still disconnect a UE.")
    c1, c2 = st.columns(2)
    disabled = bool(errors) or schedule is None or active or (method != "Manual table" and not enabled)
    if c1.button("Save channel schedule", disabled=disabled, type="primary",
                 key=f"channel_save_{run_dir.name}"):
        try:
            target = channel.save(run_dir, schedule, cfg)
            st.success(f"Saved {target}. The next deployment will apply and verify it.")
        except (OSError, ValueError) as exc:
            st.error(str(exc))
    c2.button("Disable schedule", disabled=active, key=f"channel_disable_{run_dir.name}",
              on_click=_disable_schedule, args=(run_dir, cfg, model))
    message = st.session_state.pop(f"channel_message_{run_dir.name}", None)
    if message:
        getattr(st, message[0])(message[1])

    state = run_dir / "logs" / "channel_state.json"
    if state.exists():
        with st.expander("Latest verified channel state"):
            st.json(json.loads(state.read_text()))
