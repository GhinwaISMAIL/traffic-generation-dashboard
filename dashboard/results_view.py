"""Results page: throughput, delivery quality, and designed-vs-realized charts.

render(run_dir) draws the Plotly figures and PNG export buttons.
"""
from pathlib import Path
import io

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from twindash import kpis, bursts

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
    g = (obs.dropna(subset=["latency_ms_mean"])
            .groupby(["ue", "direction"])["latency_ms_mean"].median().reset_index())
    ues = sorted(g["ue"].unique())
    fig = go.Figure()
    for d, col in (("dl", DL), ("ul", UL)):
        sub = g[g.direction == d].set_index("ue").reindex(ues)
        fig.add_bar(name=d.upper(), x=ues, y=sub["latency_ms_mean"].values,
                    marker_color=col,
                    hovertemplate="%{x} "+d.upper()+": %{y:.0f} ms<extra></extra>")
    return _layout(fig, "Median latency by UE", "ms", height=300)


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
    st.caption("Latency uses UE/DN wall clocks; the shape is reliable, absolute "
               "values depend on clock sync.")

    st.markdown("#### Designed vs realized")
    dvr_fig = _designed_vs_realized_fig(obs, designed)
    st.plotly_chart(dvr_fig, use_container_width=True)

    st.markdown("#### Export figures")
    figs = {"throughput_dl": tp_dl, "throughput_ul": tp_ul,
            "loss": _loss_fig(obs), "latency": _latency_fig(obs),
            "designed_vs_realized": dvr_fig}
    cols = st.columns(len(figs))
    for (name, fig), col in zip(figs.items(), cols):
        try:
            col.download_button(f"⬇ {name}.png", data=_png_bytes(fig),
                                file_name=f"{run_dir.name}_{name}.png",
                                mime="image/png")
        except Exception as e:
            col.caption(f"{name}: install kaleido to export")
