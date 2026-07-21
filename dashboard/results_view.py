"""Results page: throughput, delivery quality, and designed-vs-realized charts.

render(run_dir) draws the Plotly figures and PNG export buttons.
"""
from pathlib import Path
import io
import re

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from twindash import bursts, kpis, prb, run_profile, timing

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
            "so the radio results are valid but the exact live impairment values "
            "cannot be labelled retroactively.")
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
    g = (obs.groupby("ue").apply(
            lambda x: 1 - x["recv_packets"].sum()/max(x["sent_packets"].sum(), 1))
            .reset_index(name="loss"))
    g["loss_pct"] = g["loss"] * 100
    fig = go.Figure(go.Bar(x=g["ue"], y=g["loss_pct"], marker_color=INK,
                    hovertemplate="%{x}: %{y:.2f}%<extra></extra>"))
    return _layout(fig, "Packet loss by UE", "loss %", height=300)


def _latency_fig(obs):
    metric = "latency_ms_median" if "latency_ms_median" in obs else "latency_ms_mean"
    g = (obs.dropna(subset=[metric])
            .groupby(["ue", "direction"])[metric].median().reset_index())
    ues = sorted(g["ue"].unique())
    fig = go.Figure()
    for d, col in (("dl", DL), ("ul", UL)):
        sub = g[g.direction == d].set_index("ue").reindex(ues)
        fig.add_bar(name=d.upper(), x=ues, y=sub[metric].values,
                    marker_color=col,
                    hovertemplate="%{x} "+d.upper()+": %{y:.0f} ms<extra></extra>")
    return _layout(fig, "Median flow latency by UE", "ms", height=300)


def _prb_fig(data, selected):
    fig = go.Figure()
    chosen = data[data["ue"].isin(selected)]
    for ue in selected:
        rows = chosen[chosen["ue"] == ue]
        for direction, dash in (("dl", "solid"), ("ul", "dot")):
            fig.add_scatter(
                name=f"{ue} {direction.upper()}", x=rows["t_s"],
                y=rows[f"{direction}_prb"], mode="lines",
                line=dict(dash=dash),
                hovertemplate="t=%{x:.0f}s %{y:.0f} PRB<extra>" + ue + " "
                              + direction.upper() + "</extra>")
    return _layout(fig, "PRB consumed during the xApp window", "PRB / s", height=360)


def _efficiency_fig(data, selected):
    fig = go.Figure()
    chosen = data[data["ue"].isin(selected)]
    for (ue, direction), rows in chosen.groupby(["ue", "direction"]):
        fig.add_scatter(
            name=f"{ue} {direction.upper()}", x=rows["t_s"],
            y=rows["bits_per_prb"], mode="lines",
            line=dict(dash="solid" if direction == "dl" else "dot"),
            hovertemplate="t=%{x:.0f}s %{y:.0f} bits/PRB<extra>"
                          + str(ue) + " " + direction.upper() + "</extra>")
    return _layout(fig, "Application-layer efficiency", "bits / PRB", height=360)


def _designed_vs_realized_fig(obs, designed):
    """Designed (ghost) vs realized packets per UE, overlaid."""
    real = obs.groupby("ue")["recv_packets"].sum().reset_index(name="realized")
    if designed is not None and not designed.empty and "ue" in designed.columns:
        des = designed.groupby("ue")["proj_packets"].sum().reset_index(name="designed")
    else:
        des = pd.DataFrame({"ue": real["ue"], "designed": [0]*len(real)})
    m = des.merge(real, on="ue", how="outer").fillna(0)
    ues = sorted(m["ue"])
    m = m.set_index("ue").reindex(ues).reset_index()
    fig = go.Figure()
    fig.add_bar(name="Designed", x=m["ue"], y=m["designed"], marker_color=GHOST,
                hovertemplate="%{x} designed: %{y:.0f}<extra></extra>")
    fig.add_bar(name="Realized", x=m["ue"], y=m["realized"], marker_color=INK,
                width=0.5,
                hovertemplate="%{x} realized: %{y:.0f}<extra></extra>")
    fig.update_layout(barmode="overlay")
    return _layout(fig, "Designed vs realized (packets)", "packets")


def _png_bytes(fig):
    export = go.Figure(fig)
    export.update_layout(
        margin=dict(l=70, r=30, t=60, b=90),
        legend=dict(orientation="h", yanchor="top", y=-0.16,
                    xanchor="center", x=0.5),
        title=dict(font=dict(size=18)),
    )
    export.update_yaxes(automargin=True)
    export.update_xaxes(automargin=True)
    return export.to_image(format="png", scale=2, width=1100, height=600)


def render(run_dir):
    run_dir = Path(run_dir)
    profile = run_profile.load(run_dir)
    capabilities = profile.get("capabilities") or {}
    _render_profile(profile)

    obs = kpis.build_observed(run_dir)
    if obs is None or obs.empty:
        st.info("No flow-level KPIs yet — run traffic and fetch logs to populate "
                "sent / received.")
        return

    try:
        designed = kpis.build_designed(run_dir)
    except Exception:
        designed = None

    st.markdown("#### Throughput")
    grp = st.radio("Group by", ["app", "UE"], horizontal=True,
                   label_visibility="collapsed")
    gb = "app" if grp == "app" else "ue"
    tp_dl = _throughput_fig(obs, group_by=gb, direction="dl")
    tp_ul = _throughput_fig(obs, group_by=gb, direction="ul")
    tcol1, tcol2 = st.columns(2)
    tcol1.plotly_chart(tp_dl, use_container_width=True)
    tcol2.plotly_chart(tp_ul, use_container_width=True)

    st.markdown("#### Delivery quality")
    a, b = st.columns(2)
    with a:
        st.plotly_chart(_loss_fig(obs), use_container_width=True)
    with b:
        st.plotly_chart(_latency_fig(obs), use_container_width=True)
    tm = timing.load(run_dir)
    if tm:
        st.caption("Latency is converted to UTC with per-node run anchors. Absolute "
                   "values still depend on the POWDER nodes being clock-synchronized.")
    elif profile.get("testbed") == run_profile.RFSIM:
        st.caption("RFsim containers share one node clock; no cross-node UTC anchor "
                   "was saved for this run.")
    else:
        st.warning("No run_timing.json was saved. Cross-node latency can include "
                   "clock skew and should not be treated as calibrated latency.")

    st.markdown("#### Designed vs realized")
    dvr_fig = _designed_vs_realized_fig(obs, designed)
    st.plotly_chart(dvr_fig, use_container_width=True)

    _render_channel_context(run_dir, profile)
    _render_xapp_health(run_dir, profile)

    extra_figs = {}
    prb_data = pd.DataFrame()
    if capabilities.get("prb"):
        try:
            prb_data = prb.prb_timeseries(run_dir)
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            st.warning(f"PRB data could not be loaded: {exc}")
        if prb_data.empty:
            st.warning("PRB collection belongs to this run profile, but no usable "
                       "logs/prb_by_second.csv rows were found.")
    if capabilities.get("prb") and not prb_data.empty:
        st.markdown("#### Radio resources")
        mapped = int(prb_data.get("mapped", prb_data["ue"].notna()).sum())
        st.caption(
            f"Mapped PRB rows: {mapped}/{len(prb_data)}; "
            f"counter-reset boundaries dropped: {prb_data.attrs.get('epoch_boundaries', 0)}.")
        available = sorted(prb_data["ue"].dropna().astype(str).unique())
        defaults = available[:min(4, len(available))]
        selected = st.multiselect("UEs shown in radio charts", available,
                                  default=defaults, key="radio_ues")
        if selected:
            prb_fig = _prb_fig(prb_data, selected)
            st.plotly_chart(prb_fig, use_container_width=True)
            extra_figs["prb_timeseries"] = prb_fig

            efficiency = prb.efficiency(run_dir)
            if efficiency.empty:
                reason = efficiency.attrs.get(
                    "error", "No overlapping MGEN and xApp UTC seconds were found.")
                st.warning(f"Efficiency unavailable: {reason}")
            else:
                eff_fig = _efficiency_fig(efficiency, selected)
                st.plotly_chart(eff_fig, use_container_width=True)
                extra_figs["bits_per_prb"] = eff_fig

        window = timing.xapp_window(tm) if tm else None
        if window:
            st.caption(
                f"xApp UTC window: {window[0]:.3f}–{window[1]:.3f}; "
                "MGEN and PRB were joined on absolute epoch seconds and UE.")
    elif not capabilities.get("prb"):
        st.caption(
            "Radio-resource and bits/PRB charts are hidden because this run's "
            "deployment profile did not provide RIC/xApp PRB measurements.")

    st.markdown("#### Export figures")
    figs = {"throughput_dl": tp_dl, "throughput_ul": tp_ul,
            "loss": _loss_fig(obs), "latency": _latency_fig(obs),
            "designed_vs_realized": dvr_fig}
    figs.update(extra_figs)
    cols = st.columns(len(figs))
    for (name, fig), col in zip(figs.items(), cols):
        try:
            col.download_button(f"⬇ {name}.png", data=_png_bytes(fig),
                                file_name=f"{run_dir.name}_{name}.png",
                                mime="image/png")
        except Exception as e:
            col.caption(f"{name}: install kaleido to export")
