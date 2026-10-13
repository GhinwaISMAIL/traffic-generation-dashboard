"""Results page: throughput, delivery quality, and designed-vs-realized charts.

render(run_dir) draws the Plotly figures and vector PDF export buttons.
"""
from pathlib import Path
import json
import math
from html import escape
import re

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from twindash import kpis, prb, run_profile, timing

INK       = "#3D5A6C"   # text, axes
DL        = "#8BA9C9"   # downlink
UL        = "#D4A574"   # uplink
GHOST     = "#C9D2DB"   # designed (ghost) bars
GRID      = "#EAEef2"
APP_COLORS = {
    "aparat":   "#7A9B76",
    "filimo":   "#D4A574",
    "igap":     "#9B92C4",
    "telegram": "#8BA9C9",
    "youtube":  "#C77B7B",
}
FONT = dict(family="Inter, Helvetica, Arial, sans-serif", color=INK)

CAPABILITY_LABELS = {
    "flow_kpis": "MGEN traffic KPIs",
    "latency": "flow latency",
    "channel_model": "per-UE channel model",
    "ric": "near-RT RIC",
    "xapp": "xApp monitoring",
    "prb": "PRB counters",
    "radio_efficiency": "bits/PRB",
}


def _render_profile(profile):
    topology = profile.get("topology") or {}
    parts = [profile.get("label", "Unknown profile")]
    if topology.get("cells"):
        parts.append(f"{topology['cells']} cell(s)")
    if topology.get("ues"):
        parts.append(f"{topology['ues']} UE(s)")
    caps = profile.get("capabilities") or {}
    enabled = [label for key, label in CAPABILITY_LABELS.items()
               if caps.get(key)]
    unavailable = [label for key, label in CAPABILITY_LABELS.items()
                   if not caps.get(key)]
    st.info("**Run profile:** " + " · ".join(parts) + "\n\n"
            + "Measurements enabled: " + ", ".join(enabled))
    if unavailable:
        st.caption("Not provided by this run profile: " + ", ".join(unavailable))
    if profile.get("inferred"):
        st.caption(
            "This is a legacy run with no run_profile.json. Capabilities were "
            "inferred conservatively from the artifacts saved with the run.")
    if profile.get("profile_error"):
        st.warning(profile["profile_error"])


def _render_channel_context(run_dir, profile):
    caps = profile.get("capabilities") or {}
    if not caps.get("channel_model"):
        return
    st.markdown("#### Channel context")
    state = run_profile.channel_state(run_dir)
    if state is None:
        st.info(
            "This profile supports per-cell/per-UE channel models and runtime "
            "`channelmod` control. This run did not save `logs/channel_state.json`, "
            "so the exact live impairment values cannot be labelled retroactively.")
    else:
        st.caption("Channel state captured with this run:")
        st.json(state, expanded=False)


def _render_xapp_health(run_dir, profile):
    caps = profile.get("capabilities") or {}
    if not caps.get("ric"):
        return
    st.markdown("#### RIC / xApp")
    if not caps.get("xapp"):
        st.caption("The RIC was part of this profile, but xApp collection was disabled.")
        return
    path = Path(run_dir) / "logs" / "xapp.log"
    if not path.exists():
        st.warning("xApp monitoring was enabled for this run, but logs/xapp.log is missing.")
        return
    text = path.read_text(errors="replace")
    error_lines = [
        line for line in text.splitlines()
        if re.search(
            r"assert|abort|timeout|pending event|connection lost|segmentation|error",
            line, re.IGNORECASE)
    ]
    expected = (profile.get("xapp") or {}).get("expected_subscriptions")
    subscriptions = text.count("Successfully subscribed")
    deletes = text.count("SUBSCRIPTION DELETE RESPONSE rx")
    columns = st.columns(4)
    columns[0].metric("Subscriptions", f"{subscriptions}/{expected}"
                      if expected is not None else subscriptions)
    columns[1].metric("Delete responses", deletes)
    columns[2].metric("xApp errors", len(error_lines))
    columns[3].metric(
        "Clean shutdown",
        "yes" if "Test xApp run SUCCESSFULLY" in text else "not recorded")
    if error_lines:
        with st.expander("xApp error lines"):
            st.code("\n".join(error_lines[-30:]))


def _layout(fig, title, ylab, height=320, log_y=False):
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=INK),
                   x=0, xanchor="left", y=0.97, yanchor="top"),
        font=FONT, plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(l=8, r=8, t=40, b=52), height=height,
        legend=dict(orientation="h", yanchor="top", y=-0.18,
                    xanchor="center", x=0.5, font=dict(size=11)),
        bargap=0.28, bargroupgap=0.08,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickfont=dict(color=INK))
    fig.update_yaxes(title=dict(text=ylab, font=dict(size=11, color=INK)),
                     showgrid=True, gridcolor=GRID, zeroline=False,
                     tickfont=dict(color=INK),
                     type="log" if log_y else "linear")
    return fig


def _throughput_fig(obs, group_by="app", direction="dl"):
    """Throughput bars for one direction (linear axis), grouped by app or UE."""
    key = "app" if group_by == "app" else "ue"
    col = DL if direction == "dl" else UL
    g = (obs[obs.direction == direction].groupby(key)["recv_mbps"].sum()
            .reset_index())
    cats = sorted(g[key].unique())
    g = g.set_index(key).reindex(cats).reset_index()
    fig = go.Figure(go.Bar(
        x=g[key], y=g["recv_mbps"], marker_color=col,
        hovertemplate="%{x}: %{y:.3f} Mbps<extra></extra>"))
    label = "Downlink" if direction == "dl" else "Uplink"
    return _layout(fig, f"{label} throughput by {key}", "Mbps", height=320)


def _loss_fig(obs):
    grouped = obs.groupby(["ue", "direction"])[["sent_packets", "recv_packets"]].sum()
    grouped["loss_pct"] = 100 * (grouped["sent_packets"] - grouped["recv_packets"]) / grouped["sent_packets"].replace(0, float("nan"))
    fig = go.Figure()
    for direction, color in (("dl", DL), ("ul", UL)):
        if direction not in grouped.index.get_level_values("direction"):
            continue
        part = grouped.xs(direction, level="direction")
        fig.add_bar(name=direction.upper(), x=part.index, y=part["loss_pct"],
                    marker_color=color, text=part["loss_pct"], texttemplate="%{text:.1f}%",
                    textposition="outside", cliponaxis=False,
                    hovertemplate="%{x}: %{y:.2f}% lost<extra>" + direction.upper() + "</extra>")
    maximum = grouped["loss_pct"].max()
    fig.update_yaxes(range=[0, max(1, float(maximum) * 1.2)] if pd.notna(maximum) else [0, 100])
    return _layout(fig, "Packet loss", "Packets lost (%)", height=330)


def _latency_fig(obs, packets=None, percentile="p50"):
    if packets is not None:
        metric = f"latency_ms_{percentile}"
        g = packets.dropna(subset=[metric])
        title = f"Packet latency {percentile} by UE"
    else:
        metric = "latency_ms_median" if "latency_ms_median" in obs else "latency_ms_mean"
        g = obs.dropna(subset=[metric]).groupby(["ue", "direction"])[metric].median().reset_index()
        title = "Median of flow latency summaries by UE"
    seconds = not g.empty and g[metric].max() >= 10000
    scale, unit = (1000, "s") if seconds else (1, "ms")
    ues = sorted(g["ue"].unique())
    fig = go.Figure()
    for direction, color in (("dl", DL), ("ul", UL)):
        part = g[g.direction == direction].set_index("ue").reindex(ues)
        fig.add_bar(name=direction.upper(), x=ues, y=part[metric] / scale,
                    marker_color=color,
                    hovertemplate="%{x}: %{y:.2f} " + unit + "<extra>" + direction.upper() + "</extra>")
    return _layout(fig, title, f"Delay ({unit})", height=330)


def _prb_fig(data, selected):
    fig = go.Figure()
    chosen = data[data["ue"].isin(selected)]
    for ue_idx, ue in enumerate(selected):
        rows = chosen[chosen["ue"] == ue]
        color = _PLOTLY_COLORWAY[ue_idx % len(_PLOTLY_COLORWAY)]
        for direction, dash in (("dl", "solid"), ("ul", "dot")):
            fig.add_scatter(
                name=f"{ue} {direction.upper()}", x=rows["t_s"],
                y=rows[f"{direction}_prb"] / rows.get("receipt_interval_s", 1), mode="lines",
                line=dict(dash=dash, color=color),
                hovertemplate="t=%{x:.0f}s %{y:.0f} PRB<extra>" + ue + " "
                              + direction.upper() + "</extra>")
    return _layout(fig, "PRB consumed during the xApp window", "PRB / s", height=360)


def _efficiency_fig(data, selected):
    fig = go.Figure()
    chosen = data[data["ue"].isin(selected)].copy()
    # Captures created before the numeric PRB fix can contain pd.NA in an
    # object-dtype column.  Plotly renders those values interactively, but
    # Kaleido/orjson cannot serialize NAType while exporting PDF.
    chosen["bits_per_prb"] = pd.to_numeric(
        chosen["bits_per_prb"], errors="coerce")
    chosen = chosen.dropna(subset=["t_s", "bits_per_prb"])
    chosen["bits_per_prb"] = chosen["bits_per_prb"].astype(float)
    ue_color = {ue: _PLOTLY_COLORWAY[i % len(_PLOTLY_COLORWAY)]
                for i, ue in enumerate(selected)}
    for (ue, direction), rows in chosen.groupby(["ue", "direction"]):
        fig.add_scatter(
            name=f"{ue} {direction.upper()}", x=rows["t_s"],
            y=rows["bits_per_prb"], mode="lines",
            line=dict(dash="solid" if direction == "dl" else "dot",
                      color=ue_color.get(str(ue), _PLOTLY_COLORWAY[0])),
            hovertemplate="t=%{x:.0f}s %{y:.0f} bits/PRB<extra>"
                          + str(ue) + " " + direction.upper() + "</extra>")
    return _layout(fig, "Application-layer efficiency", "bits / PRB", height=360)


def _designed_vs_realized_fig(obs, designed):
    totals = obs.groupby("ue")[["sent_packets", "recv_packets"]].sum()
    fig = go.Figure()
    if designed is not None and not designed.empty and "ue" in designed:
        planned = designed.groupby("ue")["proj_packets"].sum()
        totals = totals.reindex(totals.index.union(planned.index))
        fig.add_bar(name="Planned", x=totals.index, y=planned.reindex(totals.index), marker_color=GHOST)
    for name, column, color in (("Transmitted", "sent_packets", INK), ("Received", "recv_packets", DL)):
        fig.add_bar(name=name, x=totals.index, y=totals[column], marker_color=color)
    fig.update_traces(hovertemplate="%{x}: %{y:,.0f} packets<extra>%{fullData.name}</extra>")
    return _layout(fig, "Planned → transmitted → received", "Packets", height=350)


_PLOTLY_COLORWAY = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]


def _pdf_bytes(fig):
    export = go.Figure(fig)
    export.update_layout(
        margin=dict(l=70, r=30, t=60, b=90),
        legend=dict(orientation="h", yanchor="top", y=-0.16,
                    xanchor="center", x=0.5),
        title=dict(font=dict(size=18)),
    )
    export.update_yaxes(automargin=True)
    export.update_xaxes(automargin=True)
    return export.to_image(format="pdf", width=1100, height=600)


HELP = {
    "throughput": "Received payload bytes × 8 ÷ execution duration ÷ 1,000,000. The average includes idle seconds. Duplicate receives count once; network headers are excluded.",
    "loss": "Transmitted packet identifiers without a matching receive ÷ transmitted packet identifiers × 100. Loss is measured at the end of the collected observation; missing logs make it provisional.",
    "latency": "Receive timestamp minus the matching SEND timestamp. p50 is the median; p95 is the delay within which 95% of received packets arrived. Lost packets have no measured delay. Cross-node results require synchronized clocks.",
    "timeline": "Payload bits sent or received in each displayed time interval, divided by that interval's duration. Solid lines show received traffic; dotted lines show transmitted traffic. Zeros mean no logged packets in that interval.",
    "prb": "Change in the cumulative PRB counter divided by elapsed receipt time. Counter resets and irregular intervals are excluded. Resources are reported per UE, not per application.",
    "efficiency": "Received application bits divided by PRB counter increments for matching UE, direction and approximately one-second UTC intervals. This is a diagnostic application-layer ratio, not spectral efficiency or a causal packet-to-resource attribution.",
}


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _duration(value):
    if value is None:
        return "Unavailable"
    value = float(value)
    if value >= 3600 and value % 3600 == 0:
        return f"{value / 3600:g} h"
    if value >= 60 and value % 60 == 0:
        return f"{value / 60:g} min"
    return f"{value:g} s"


def _delay(value):
    if pd.isna(value):
        return "Unavailable"
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.1f} ms"


def _traffic_comparison(obs, group_by="app"):
    totals = obs.groupby([group_by, "direction"])["recv_mbps"].sum(min_count=1).unstack("direction")
    totals = totals.reindex(columns=["dl", "ul"]).fillna(0)
    totals = totals.loc[totals.sum(axis=1).sort_values(ascending=False).index]
    fig = go.Figure()
    for direction, color, label in (("dl", DL, "Downlink"), ("ul", UL, "Uplink")):
        fig.add_bar(name=label, x=totals.index, y=totals[direction], marker_color=color,
                    hovertemplate="%{x}<br>%{y:.5f} Mbps<extra>" + label + "</extra>")
    fig.update_layout(barmode="group")
    return _layout(fig, "Downlink and uplink · shared scale", "Average delivered Mbps", height=360)


def _traffic_timeline(packets, start, duration, window_s):
    fig = go.Figure()
    if packets.empty or start is None or packets["sent_time_utc"].isna().any():
        return fig
    endpoint = max(float(duration or 0), float(packets["received_time_utc"].max() - start))
    last_event = max(packets["sent_time_utc"].max(), packets["received_time_utc"].max())
    event_bins = math.floor((last_event - start) / window_s) + 1
    bins = pd.RangeIndex(max(1, math.ceil(endpoint / window_s), event_bins))
    widths = pd.Series(float(window_s), index=bins)
    if duration and duration > 0 and duration % window_s:
        final = math.ceil(duration / window_s) - 1
        widths.loc[final] = duration - final * window_s
    for direction, color in (("dl", DL), ("ul", UL)):
        for received, label, dash in ((False, "transmitted", "dot"), (True, "received", "solid")):
            rows = packets[packets.direction.eq(direction)]
            if received:
                rows = rows[rows.received]
            clock = "received_time_utc" if received else "sent_time_utc"
            rows = rows.dropna(subset=[clock]).copy()
            rows["bin"] = ((rows[clock] - start) // window_s).astype(int)
            counts = rows.groupby("bin")["size_bytes"].sum().reindex(bins, fill_value=0)
            rates = counts * 8 / widths / 1e6
            fig.add_scatter(name=f"{direction.upper()} {label}", x=bins * window_s,
                            y=rates, mode="lines", line=dict(color=color, dash=dash,
                            width=2.5 if received else 1.2),
                            hovertemplate="%{x:.0f} s: %{y:.4f} Mbps<extra>" + direction.upper() + " " + label + "</extra>")
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(title_text="Seconds after traffic start")
    return _layout(fig, f"Traffic over time · {window_s:g} s intervals", "Mbps", height=360)


def _summary_table(obs, group):
    values = obs.groupby([group, "direction"])[["recv_bytes", "recv_mbps"]].sum(min_count=1)
    result = pd.DataFrame(index=sorted(obs[group].unique()))
    result.index.name = group
    for direction in ("dl", "ul"):
        part = values.xs(direction, level="direction") if direction in values.index.get_level_values("direction") else pd.DataFrame(columns=["recv_bytes", "recv_mbps"])
        result[f"{direction.upper()} MB"] = part["recv_bytes"].reindex(result.index).fillna(0) / 1e6
        result[f"{direction.upper()} Mbps"] = part["recv_mbps"].reindex(result.index)
    result["DL / UL bytes"] = result["DL MB"] / result["UL MB"].replace(0, float("nan"))
    return result.reset_index()


def _render_status(run_dir, packets, capture, profile):
    pre = _read_json(run_dir / "logs/clock_preflight.json")
    post = _read_json(run_dir / "logs/clock_postflight.json")
    clocks_ok = pre.get("passed") is True and post.get("passed") is True
    clocks_failed = pre.get("passed") is False or post.get("passed") is False
    radio_enabled = (profile.get("capabilities") or {}).get("prb", False)
    ratios = [progress.get("frame_time_wall_ratio") for progress in
              (capture.get("radio_frame_progress") or {}).values()]
    ratios = [float(value) for value in ratios if value is not None]
    slow = any(not .9 <= value <= 1.1 for value in ratios)
    cols = st.columns(4)
    cols[0].metric("Packet matching", "Checked", help="SEND and RECV identifiers match. Duplicate sends, unknown receives, and payload-size mismatches are rejected. This does not prove that every expected log was collected.", border=True)
    cols[1].metric("Node clocks", "Passed" if clocks_ok else "Failed" if clocks_failed else "Unverified", help="Both the preflight and postflight clock checks must pass. UTC anchors alone do not establish clock synchronization.", border=True)
    cols[2].metric("Radio capture", "Not enabled" if not radio_enabled else "Complete" if capture.get("complete") is True else "Incomplete" if capture.get("complete") is False else "Unverified", help="Compares monitor callbacks with stored reports for each service. Completeness describes captured reports, not the full execution duration.", border=True)
    cols[3].metric("Radio speed", f"{min(ratios) * 100:.1f}%" if ratios else "Unavailable", help="Unwrapped radio frames × 10 ms ÷ elapsed wall time. 100% means real-time speed. If several nodes are captured, the slowest is shown.", border=True)
    if slow:
        speed = f"{min(ratios) * 100:.1f}%" if min(ratios) == max(ratios) else f"{min(ratios) * 100:.1f}–{max(ratios) * 100:.1f}%"
        st.warning(f"Radio frame speed is {speed} of real time. Packet delays describe this simulator execution; they are not a real-time latency baseline.", icon=":material/speed:")
    if capture.get("complete") is False:
        st.error("Some monitoring reports are missing. Treat radio measurements as incomplete.")
    if not clocks_ok:
        st.warning("Clock synchronization is not verified for this execution. Absolute one-way latency may include clock error.")
    invalid = packets.attrs.get("invalid_latency_packets", 0)
    if invalid:
        st.warning(f"{invalid:,} received packets have missing or negative delays and are excluded from latency percentiles.")
    unanchored = packets.attrs.get("unanchored_packets", 0)
    if unanchored:
        st.warning(f"{unanchored:,} received packets use time-of-day fallback because UTC anchors are missing.")
    if not (run_dir / "executions" / str(timing.load(run_dir).get("run_id", "")) / "metadata.json").exists():
        st.info("No completed execution archive was found for these logs. Delivery counts are provisional until collection is complete.")


def _render_radio(run_dir, profile, tm, capture):
    _render_channel_context(run_dir, profile)
    _render_xapp_health(run_dir, profile)
    if not (profile.get("capabilities") or {}).get("prb"):
        st.info("This run profile does not provide PRB measurements.")
        return {}
    try:
        radio = prb.prb_timeseries(run_dir)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        st.warning(f"Radio measurements are unavailable: {exc}")
        return {}
    if radio.empty:
        st.info(radio.attrs.get("error", "No usable radio samples were captured."))
        return {}
    if capture.get("timestamp_stage") != "pre_queue_utc":
        st.warning("Radio receipt timestamp provenance is unverified. Historical captures may include database waiting time.")
    if prb.clock_lag_summary(radio)["radio_clock_lag_warning"]:
        st.warning("Report and receipt timestamps diverge. Traffic-to-radio alignment requires review.")
    start = tm.get("senders_start_epoch")
    if start is not None:
        st.caption(f"Usable capture: {radio.utc_second.min() - start:.0f}–{radio.utc_second.max() - start:.0f} s after traffic start. Missing intervals are not zero usage.")
    available = sorted(radio["ue"].dropna().unique())
    selected = st.multiselect("UEs", available, default=available[:4], key=f"radio_ues_{run_dir.name}", help="Radio counters describe the selected UEs. At most four are initially selected to keep the chart readable.")
    figures = {}
    if selected:
        st.subheader("Radio resources", help=HELP["prb"])
        figure = _prb_fig(radio, selected)
        st.plotly_chart(figure, width="stretch")
        figures["Radio resources"] = figure
        st.subheader("Application bits per PRB", help=HELP["efficiency"])
        efficiency = prb.efficiency(run_dir)
        if efficiency.empty:
            st.info(efficiency.attrs.get("error", "No overlapping traffic and radio samples."))
        else:
            figure = _efficiency_fig(efficiency, selected)
            st.plotly_chart(figure, width="stretch")
            figures["Bits per PRB"] = figure
    with st.expander("Capture details"):
        st.json(capture)
        st.caption(f"Mapped samples: {int(radio['mapped'].sum()):,} / {len(radio):,}. Counter resets excluded: {radio.attrs.get('epoch_boundaries', 0)}. Irregular intervals excluded: {radio.attrs.get('irregular_intervals', 0)}.")
    return figures


def render(run_dir):
    run_dir = Path(run_dir)
    profile = run_profile.load(run_dir)
    config = _read_json(run_dir / "config.json")
    tm = timing.load(run_dir)
    capture = prb.capture_quality(run_dir)
    try:
        packets = kpis.packet_records(run_dir)
        obs = kpis.build_observed(run_dir, packets=packets)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        st.error(f"Results cannot be verified: {exc}")
        return
    if packets.empty:
        st.info("No transmitted packet logs are available yet. Run an experiment and collect its logs to view results.")
        return
    try:
        designed = kpis.build_designed(run_dir)
    except (OSError, ValueError, KeyError):
        designed = None
    duration = kpis.run_duration(run_dir)
    with st.container(key="results_report"):
        st.html("""<style>
        .st-key-results_report [data-testid="stMetric"] {background: #f7f9fc; border-color: #e5ebf1; border-radius: 12px;}
        .st-key-results_report [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums; font-size: 1.65rem; color: #294859;}
        .st-key-results_report [data-testid="stMetricValue"] > div {white-space:normal; overflow:visible; text-overflow:clip;}
        .st-key-results_report [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) > [data-testid="stColumn"] {min-width:150px; flex:1 1 150px;}
        .st-key-results_report [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) {flex-wrap:wrap;}
        .st-key-delivery_charts [data-testid="stHorizontalBlock"] {flex-wrap:wrap;}
        .st-key-delivery_charts [data-testid="stColumn"] {min-width:300px; flex:1 1 300px;}
        .st-key-results_report .run-context {display:flex; flex-wrap:wrap; gap:10px; margin: 0 0 18px;}
        .st-key-results_report .run-context span {padding:6px 12px; background:#eef3f8; color:#3d5a6c; border-radius:6px; font-size:14px;}
        .st-key-results_report [data-baseweb="tab-list"] {gap:24px; margin-top:14px;}
        .st-key-results_report [data-testid="stCaptionContainer"] {color:#607180;}
        </style>""")
        context = [profile.get("label", "Run"), f"{packets.ue.nunique()} UEs", f"{packets.app.nunique()} apps",
                   f"Execution · {_duration(duration)}"]
        if config.get("logical_duration_s"):
            context.append(f"Logical scenario · {_duration(config['logical_duration_s'])}")
        st.html('<div class="run-context">' + ''.join(f'<span>{escape(str(item))}</span>' for item in context) + '</div>')
        st.caption(f"Execution {tm.get('run_id', run_dir.name)} · Hover or focus the help icon beside a label for its definition.")
        _render_status(run_dir, packets, capture, profile)
        traffic_tab, delivery_tab, radio_tab, data_tab = st.tabs(["Traffic", "Delivery", "Radio", "Data & methods"])
        figures = {}
        with traffic_tab:
            st.subheader("Traffic at a glance", help=HELP["throughput"])
            cols = st.columns(3)
            for direction, col in zip(("dl", "ul"), cols):
                part = obs[obs.direction.eq(direction)]
                rate = part.recv_mbps.sum(min_count=1)
                col.metric("Downlink · Mbps" if direction == "dl" else "Uplink · Mbps",
                           f"{rate:.4f}" if pd.notna(rate) else "Unavailable", help=HELP["throughput"], border=True)
            cols[2].metric("Delivered data", f"{obs.recv_bytes.sum() / 1e6:.2f} MB", help="Unique received payload bytes across both directions. MB uses 1,000,000 bytes. Transport and radio headers are excluded.", border=True)
            st.caption(f"Averages cover the entire {_duration(duration)} execution, including idle time.")
            group = st.segmented_control("Compare by", ["Application", "UE"], default="Application", required=True, key="results_group")
            group = "app" if group == "Application" else "ue"
            if duration and duration > 0:
                comparison = _traffic_comparison(obs, group)
                st.plotly_chart(comparison, width="stretch")
                figures["Traffic comparison"] = comparison
            else:
                st.info("Execution duration is unavailable, so average throughput cannot be compared reliably.")
            with st.expander("Exact values and DL/UL ratios"):
                st.dataframe(_summary_table(obs, group), hide_index=True, column_config={
                    "DL MB": st.column_config.NumberColumn(format="%.3f"),
                    "UL MB": st.column_config.NumberColumn(format="%.3f"),
                    "DL Mbps": st.column_config.NumberColumn(format="%.5f"),
                    "UL Mbps": st.column_config.NumberColumn(format="%.5f"),
                    "DL / UL bytes": st.column_config.NumberColumn(format="%.2f", help="Downlink received bytes divided by uplink received bytes. Undefined when uplink is zero.")})
            st.subheader("Traffic over time", help=HELP["timeline"])
            window = st.selectbox("Time interval", [1, 5, 10, 30, 60], index=4 if duration and duration > 7200 else 0,
                                  format_func=lambda value: f"{value} s", help="Larger intervals smooth bursts. This affects the timeline only, not the execution-average values.")
            if tm.get("senders_start_epoch") is None or packets["sent_time_utc"].isna().any() or packets.loc[packets.received, "received_time_utc"].isna().any():
                st.info("The traffic timeline needs complete UTC clock anchors.")
            else:
                figure = _traffic_timeline(packets, tm["senders_start_epoch"], duration, window)
                st.plotly_chart(figure, width="stretch")
                figures["Traffic timeline"] = figure
        with delivery_tab:
            st.subheader("Packet delivery", help=HELP["loss"])
            cols = st.columns(3)
            sent_count, recv_count = len(packets), int(packets.received.sum())
            cols[0].metric("Transmitted", f"{sent_count:,}", help="Unique packets recorded by the sender logs.", border=True)
            cols[1].metric("Received", f"{recv_count:,}", help="Transmitted packets with a matching receive. Repeated receives count once.", border=True)
            cols[2].metric("Lost", f"{sent_count - recv_count:,}", delta=f"{100 * (sent_count - recv_count) / sent_count:.2f}% of transmitted", delta_color="off", delta_arrow="off", help=HELP["loss"], border=True)
            loss = _loss_fig(obs)
            progress = _designed_vs_realized_fig(obs, designed)
            with st.container(key="delivery_charts"):
                left, right = st.columns(2)
                left.plotly_chart(loss, width="stretch")
                right.plotly_chart(progress, width="stretch")
            figures.update({"Packet loss": loss, "Delivery stages": progress})
            if designed is None or designed.empty:
                st.caption("Planned packet counts are unavailable; only transmitted and received counts are shown.")
            st.subheader("Packet delay", help=HELP["latency"])
            valid = packets.loc[packets.received & packets.latency_ms.ge(0)]
            cols = st.columns(4)
            for index, direction in enumerate(("dl", "ul")):
                values = valid.loc[valid.direction.eq(direction), "latency_ms"]
                cols[index * 2].metric(f"{direction.upper()} median · p50", _delay(values.median()), help=HELP["latency"], border=True)
                cols[index * 2 + 1].metric(f"{direction.upper()} tail · p95", _delay(values.quantile(.95)), help=HELP["latency"], border=True)
            percentile = st.segmented_control("Percentile by UE", ["p50", "p95"], default="p50", required=True, help=HELP["latency"])
            latencies = kpis.packet_latency(run_dir, packets=packets)
            latency = _latency_fig(obs, latencies, percentile)
            st.plotly_chart(latency, width="stretch")
            st.caption(f"{len(valid):,} received packets with valid delay. Each received packet contributes equally to its direction's percentile.")
            figures["Packet latency"] = latency
        with radio_tab:
            figures.update(_render_radio(run_dir, profile, tm, capture))
        with data_tab:
            st.subheader("Definitions and calculations")
            for title, key in (("Average throughput", "throughput"), ("Packet loss", "loss"), ("Packet latency", "latency"), ("Traffic intervals", "timeline"), ("PRB rate", "prb"), ("Application bits per PRB", "efficiency")):
                with st.expander(title):
                    st.write(HELP[key])
            st.caption(f"Repeated receives counted once: {int(packets.duplicate_receives.sum()):,}. Delay variability in the exported jitter_ms column is the standard deviation of valid packet delays.")
            with st.expander("Run configuration and monitoring"):
                _render_profile(profile)
                st.json(config)
            st.subheader("Export this view")
            name = st.selectbox("Chart", list(figures))
            figure = figures[name]
            slug = name.lower().replace(" ", "_")
            st.download_button("Download chart PDF", data=lambda: _pdf_bytes(figure),
                               file_name=f"{run_dir.name}_{slug}.pdf", mime="application/pdf",
                               help="Exports the selected chart with the current display options.")
            st.download_button("Download flow summary CSV", obs.to_csv(index=False),
                               file_name=f"{run_dir.name}_flow_summary.csv", mime="text/csv",
                               help="The same matched packet records used for the charts, summarized by flow.")
            st.dataframe(obs, hide_index=True)
