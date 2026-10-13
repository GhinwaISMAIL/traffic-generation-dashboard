import plotly.graph_objects as go
import streamlit as st

from twindash import scenario, schedule, settings
from dashboard.results_view import APP_COLORS, FONT, GRID


def _timeline(frame, duration):
    fig = go.Figure()
    colors = {**APP_COLORS, "whatsapp": "#76A691", "googlemaps": "#6D9EA7"}
    for (app, role), rows in frame.groupby(["traffic_model", "scenario_role"]):
        fig.add_trace(go.Bar(
            name=f"{app} · {role}", orientation="h",
            y=rows["experimental_ue"], x=rows["end_s"] - rows["start_s"],
            base=rows["start_s"], customdata=rows[["session_id", "start_s", "end_s"]],
            marker_color=colors.get(app, "#9B92C4"),
            marker_pattern_shape="/" if role == "augmented" else "",
            hovertemplate=("%{y}<br>%{customdata[0]}<br>"
                           "%{customdata[1]}–%{customdata[2]} s<extra>%{fullData.name}</extra>"),
        ))
    ticks = [duration * index / 8 for index in range(9)]
    fig.update_layout(
        barmode="overlay", template="plotly_white", font=FONT,
        height=max(300, min(850, 120 + frame["experimental_ue"].nunique() * 32)),
        margin=dict(l=10, r=10, t=20, b=10),
        xaxis=dict(title="Logical scenario time", range=[0, duration], tickvals=ticks,
                   gridcolor=GRID,
                   ticktext=[f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}" for t in ticks]),
        yaxis=dict(title="UE", autorange="reversed", gridcolor=GRID),
        legend=dict(orientation="h", y=-0.25),
    )
    return fig


def _clock(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}".rstrip("0").rstrip(".")


def render():
    st.subheader("Measurement-driven schedule")
    st.caption(
        "Import application sessions on a shared logical clock. Overlapping "
        "sessions on the same UE are allowed. Baseline and augmented sessions "
        "retain their original identifiers and source fields.")
    duration = 86400.0
    st.caption("Logical duration: 24 h (86400 s). Every session must satisfy 0 ≤ start_s < end_s ≤ 86400.")
    method = st.radio("Schedule input", ["Upload CSV", "Local CSV file"],
                      horizontal=True, key="schedule_input_method")
    if method == "Upload CSV":
        source = st.file_uploader(
            "Schedule file", type=["csv"], key="schedule_upload", max_upload_size=1024)
    else:
        source = st.text_input("Schedule CSV path", key="schedule_csv_path").strip()
    with st.expander("Required schedule columns"):
        st.code(
            "session_id,experimental_ue,start_s,end_s,traffic_model,"
            "source_category,scenario_role", language="text")
        st.write(
            "Use seconds from scenario start or HH:MM:SS for times. "
            "scenario_role accepts baseline or augmented. "
            "source_session_id and other provenance columns are optional and preserved.")
    with st.expander("Experimental UE population", expanded=True):
        expected_ues = st.text_area(
            "Experimental UE identifiers", key="schedule_ue_population",
            help="Paste one identifier per line from the frozen scenario population. "
                 "The number of identifiers sets the UE count. Include inactive UEs. "
                 "This list is independent of the uploaded sessions.")
    if not source:
        st.info("Select a schedule to validate and preview it.")
        return
    try:
        imported = schedule.load(source, logical_duration_s=duration)
    except (OSError, ValueError) as exc:
        st.error(f"Could not import schedule: {exc}")
        return

    available = scenario.discover_apps()
    models = sorted(imported.sessions["traffic_model"].unique())
    mapping = {}
    with st.expander("Application model mapping", expanded=not set(models) <= set(available)):
        st.caption(
            "Choose the application artifact folder for each imported model. "
            "The original model name is retained when saving. Model contents "
            "must be validated before traffic generation.")
        for model in models:
            choices = sorted(set([model] + available))
            mapping[model] = st.selectbox(
                model, choices, index=choices.index(model),
                key=f"schedule_model_{imported.source_sha256}_{model}")
    changed = {name: target for name, target in mapping.items() if name != target}
    try:
        imported = schedule.validate(imported, expected_ues=expected_ues,
                                     available_models=available, model_mapping=changed)
    except ValueError as exc:
        st.error(str(exc))
        return

    stats = schedule.summary(imported)
    st.success("Schedule valid")
    columns = st.columns(4)
    columns[0].metric("Logical duration", "24 h")
    columns[1].metric("Experimental UEs", stats["population_size"])
    columns[2].metric("Scheduled UEs", stats["ues"])
    columns[3].metric("Total sessions", stats["sessions"])
    columns = st.columns(4)
    columns[0].metric("Baseline sessions", stats["baseline_sessions"])
    columns[1].metric("Augmented sessions", stats["augmented_sessions"])
    columns[2].metric("Maximum concurrent UEs", stats["max_concurrent_ues"])
    columns[3].metric("Peak simultaneous sessions / UE", stats["peak_sessions_per_ue"])
    st.write("Applications: " + ", ".join(stats["applications"]))
    st.write(f"Earliest session: {_clock(stats['earliest_session_s'])} · "
             f"Latest session end: {_clock(stats['latest_session_s'])}")
    overlap = "Yes" if stats["overlapping_sessions"] else "No"
    st.write(f"Overlapping sessions on the same UE: {overlap}. Overlaps are valid.")
    st.caption("Normalized schedule preview: first 20 rows. The import includes all validated sessions.")
    st.dataframe(imported.sessions.head(20), hide_index=True, width="stretch")

    st.caption("Import freezes the validated schedule in a new run folder, ready for traffic generation.")
    columns = st.columns(2)
    if columns[0].button("Import measurement-driven schedule", key="import_measurement_schedule",
                         type="primary"):
        try:
            target = schedule.import_run(imported, settings.profiles_dir(),
                                         available_models=available)
            st.success(f"Imported {target / schedule.SCHEDULE_CSV}")
        except (OSError, ValueError) as exc:
            st.error(f"Could not import schedule: {exc}")
    columns[1].download_button(
        "Download normalized CSV", data=imported.to_csv(),
        file_name=schedule.SCHEDULE_CSV, mime="text/csv")

    with st.expander("Traffic generation", expanded=True):
        seed = st.number_input("Generation seed", min_value=0, max_value=4294967295,
                               value=42, key="measurement_generation_seed")
        st.caption(
            "Sample one learned flow per session and direction using the existing application "
            "timing models. Source byte counts remain a reference; generated demand is reported "
            "separately. Network settings are taken from the saved scenario configuration.")
        if st.button("Write measurement-driven scenario", key="write_measurement_scenario"):
            try:
                target = schedule.import_run(imported, settings.profiles_dir(),
                                             available_models=available)
                config = scenario.measurement_config(target, random_seed=seed)
                saved = scenario.save(config)
                st.success(f"Saved frozen schedule and updated {saved}. "
                           "Run build_scenario_1.ipynb, then mgen_script_2.ipynb.")
                st.code(config["measurement_schedule"]["output_run"], language="text")
            except (OSError, ValueError) as exc:
                st.error(f"Could not write measurement-driven scenario: {exc}")

    with st.expander("Application timeline"):
        preview = imported.sessions.head(2000)
        if len(preview) < len(imported.sessions):
            st.caption("Timeline shows the first 2,000 sessions; counts and saved data include all sessions.")
        st.plotly_chart(_timeline(preview, duration), width="stretch")
    with st.expander("Execution window preview"):
        st.caption("Original session times are retained alongside offsets within the selected window.")
        columns = st.columns(2)
        start = columns[0].number_input(
            "Window start (s)", min_value=0.0, value=0.0, step=60.0,
            key="schedule_window_start")
        end = columns[1].number_input(
            "Window end (s)", min_value=0.0, value=min(900.0, duration), step=60.0,
            key="schedule_window_end")
        try:
            selected = schedule.execution_window(imported, start, end)
            st.caption(f"{len(selected)} sessions intersect this window; showing the first 20.")
            st.dataframe(selected.head(20), hide_index=True, width="stretch")
        except ValueError as exc:
            st.error(str(exc))
