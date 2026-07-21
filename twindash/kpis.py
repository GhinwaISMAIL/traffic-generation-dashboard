"""Observed KPI table + projected-vs-realized reconciliation.

  sent  = SEND events from *_tx.log   (offered, as MGEN emitted)
  recv  = RECV events from *_rx.log   (delivered)
  loss  = 1 - recv/sent
  latency = recv_time - sent_time     (needs chrony sync; garbage without it)
  throughput = recv_bytes * 8 / RUN_DURATION   (app-layer goodput, bits/s)

Per-UE/per-app attribution comes from flow_batch_map.csv, joined on flow_id
(the DN NATs every UE to one source, so the log's own address can't identify
the UE — the flow id is the only reliable key back to (ue, app)).

Throughput is bytes over the RUN duration, not over a flow's burst span. Dividing
by a burst's own sub-second span gives a burst *peak* (tens of Gbps) that is not
a meaningful throughput and cannot be summed across flows. Run-average is
comparable across flows/apps/UEs and additive. For the burst structure over time,
use throughput_timeseries() (fixed windows), which IS summable per window and is
the right thing to overlay against per-UE PRB.

Bytes are UDP payload (app layer) => effectively goodput (no TCP retransmit).
They exclude 5G/GTP-U/RLC/MAC overhead, so throughput-per-PRB is app-layer bits
per resource block — a meaningful efficiency proxy, not raw spectral efficiency.
"""
import json
from pathlib import Path

import pandas as pd

from . import schema, mgen_log, mgen_script, timing


def _flow_map(run_dir: Path) -> pd.DataFrame:
    fbm = pd.read_csv(run_dir / schema.SCRIPTS_DIR / "flow_batch_map.csv")
    return (fbm.rename(columns={"unique_flow_id": "flow_id", "ue_name": "ue"})
               [["flow_id", "ue", "app", "direction"]])


def run_duration(run_dir: Path, observed_span: float | None = None) -> float | None:
    """Run duration in seconds. Prefer the configured value (config.json flat key
    simulation_duration); fall back to the observed whole-run span if no config."""
    cfg = run_dir / "config.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text()).get("simulation_duration")
            if d:
                return float(d)
        except Exception:
            pass
    return observed_span


def _load_events(run_dir: Path):
    logs = run_dir / schema.LOGS_DIR
    tm = timing.load(run_dir)
    sent_frames, recv_frames = [], []
    for log in sorted(logs.glob("*.log")):
        node_tag, direction = mgen_log.parse_run_name(log.name)
        node = timing.node_of(tm, node_tag)
        df = mgen_log.parse_log(
            log,
            midnight_epoch=timing.midnight_epoch(tm, node),
            ref_sod=timing.anchor_sod(tm, node))
        if df.empty:
            continue
        df["direction"] = direction
        if log.name.endswith("_tx.log"):
            sent_frames.append(df[df.event == "SEND"])
        elif log.name.endswith("_rx.log"):
            recv_frames.append(df[df.event == "RECV"])
    sent = pd.concat(sent_frames, ignore_index=True) if sent_frames else pd.DataFrame()
    recv = pd.concat(recv_frames, ignore_index=True) if recv_frames else pd.DataFrame()
    return sent, recv


def build_observed(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    sent, recv = _load_events(run_dir)
    fbm = _flow_map(run_dir)
    strays = 0

    # whole-run observed span (fallback duration) from all recv timestamps
    span = None
    if not recv.empty:
        span = recv["time"].max() - recv["time"].min()
    dur = run_duration(run_dir, observed_span=span) or 1.0

    if not sent.empty:
        sent = sent.merge(fbm, on=["flow_id", "direction"], how="left").dropna(subset=["ue"])
        s = (sent.groupby(["flow_id", "direction", "ue", "app"])
                 .agg(sent_packets=("seq", "count"), sent_bytes=("size", "sum"))
                 .reset_index())
    else:
        s = pd.DataFrame(columns=["flow_id","direction","ue","app","sent_packets","sent_bytes"])

    if not recv.empty:
        recv["latency_ms"] = (recv["time"] - recv["sent_time"]) * 1000.0
        recv = recv.merge(fbm, on=["flow_id", "direction"], how="left")
        strays = int(recv["ue"].isna().sum())
        recv = recv.dropna(subset=["ue"])
        r = (recv.groupby(["flow_id", "direction", "ue", "app"])
                 .agg(recv_packets=("seq", "count"),
                      recv_bytes=("size", "sum"),
                      latency_ms_mean=("latency_ms", "mean"),
                      latency_ms_p95=("latency_ms", lambda x: x.quantile(0.95)),
                      jitter_ms=("latency_ms", "std"))
                 .reset_index())
        # run-average throughput: bytes over the RUN duration (comparable, additive)
        r["recv_mbps"] = (r["recv_bytes"] * 8) / dur / 1e6
    else:
        r = pd.DataFrame(columns=["flow_id","direction","ue","app","recv_packets",
                                  "recv_bytes","latency_ms_mean","latency_ms_p95",
                                  "jitter_ms","recv_mbps"])

    obs = r.merge(s, on=["flow_id", "direction", "ue", "app"], how="outer")
    obs["sent_packets"] = obs["sent_packets"].fillna(0).astype(int)
    obs["recv_packets"] = obs["recv_packets"].fillna(0).astype(int)
    obs["loss"] = 1 - obs["recv_packets"] / obs["sent_packets"].replace(0, pd.NA)
    obs.insert(0, "run_id", run_dir.name)
    obs.attrs["run_duration_s"] = dur
    if strays:
        obs.attrs["strays"] = strays
    return obs


def by_ue(run_dir) -> pd.DataFrame:
    """Per-UE rollup (sum over apps) — pairs with PRB (allocated per UE/RNTI)."""
    obs = build_observed(run_dir)
    if obs.empty:
        return obs
    g = (obs.groupby(["run_id", "ue", "direction"])
             .agg(sent_packets=("sent_packets", "sum"),
                  recv_packets=("recv_packets", "sum"),
                  recv_bytes=("recv_bytes", "sum"),
                  recv_mbps=("recv_mbps", "sum"),
                  latency_ms_mean=("latency_ms_mean", "mean"),
                  latency_ms_p95=("latency_ms_p95", "max"))
             .reset_index())
    g["loss"] = 1 - g["recv_packets"] / g["sent_packets"].replace(0, pd.NA)
    return g


def by_app(run_dir) -> pd.DataFrame:
    """Per-app rollup (sum over UEs) — traffic characterization."""
    obs = build_observed(run_dir)
    if obs.empty:
        return obs
    g = (obs.groupby(["run_id", "app", "direction"])
             .agg(sent_packets=("sent_packets", "sum"),
                  recv_packets=("recv_packets", "sum"),
                  recv_bytes=("recv_bytes", "sum"),
                  recv_mbps=("recv_mbps", "sum"))
             .reset_index())
    g["loss"] = 1 - g["recv_packets"] / g["sent_packets"].replace(0, pd.NA)
    return g


def throughput_timeseries(run_dir, window_s: float = 1.0, per: str = "ue") -> pd.DataFrame:
    """Delivered throughput over fixed time windows — shows burst structure and
    overlays against PRB. `per` is 'ue' (PRB-compatible), 'app', or 'flow'.

    When logs/run_timing.json is present the windows are absolute epoch seconds
    (`utc_second`), which is what PRB joins on; `t_s` is then that column made
    relative for plotting. Without it only the relative `t_s` exists and the
    PRB join is refused rather than silently misaligned."""
    run_dir = Path(run_dir)
    _sent, recv = _load_events(run_dir)
    if recv.empty:
        return pd.DataFrame(columns=["t_s", per, "direction", "mbps"])
    fbm = _flow_map(run_dir)
    recv = recv.merge(fbm, on=["flow_id", "direction"], how="left").dropna(subset=["ue"])
    keycol = {"ue": "ue", "app": "app", "flow": "flow_id"}[per]
    has_utc = "utc_time" in recv.columns and recv["utc_time"].notna().any()

    if has_utc:
        recv["win"] = (recv["utc_time"] // window_s) * window_s
    else:
        t0 = recv["time"].min()
        recv["win"] = ((recv["time"] - t0) // window_s) * window_s

    g = (recv.groupby(["win", keycol, "direction"])
             .agg(bytes=("size", "sum")).reset_index())
    g["mbps"] = (g["bytes"] * 8) / window_s / 1e6

    cols = ["t_s", keycol, "direction", "mbps"]
    if has_utc:
        g["utc_second"] = g["win"]
        g["t_s"] = g["win"] - g["win"].min()
        cols.append("utc_second")
    else:
        g = g.rename(columns={"win": "t_s"})
    return g[cols]


def save_observed(run_dir) -> Path:
    out = Path(run_dir) / schema.OBSERVED_KPIS
    build_observed(run_dir).to_parquet(out, index=False)
    return out


def build_designed(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    scripts = run_dir / schema.SCRIPTS_DIR
    ip2ue, ue_class = {}, {}
    man = scripts / "manifest.csv"
    if man.exists():
        m = pd.read_csv(man)
        ip2ue = dict(zip(m["ue_ip"].astype(str), m["ue_name"]))
        ue_class = dict(zip(m["ue_name"], m["ue_class"]))
    rows = []
    dn = scripts / "dn_dl_tx.mgn"
    if dn.exists():
        for r in mgen_script.projected_from_script(dn):
            ue = ip2ue.get(str(r["dst_ip"]), r["dst_ip"])
            rows.append({"run_id": run_dir.name, "ue": ue, "direction": "dl",
                         "ue_class": ue_class.get(ue), "flow_id": r["flow_id"],
                         "proj_packets": r["proj_packets"], "proj_bytes": r["proj_bytes"]})
    for ul in sorted(scripts.glob("ue*_ul_tx.mgn")):
        ue = ul.name.split("_")[0]
        for r in mgen_script.projected_from_script(ul):
            rows.append({"run_id": run_dir.name, "ue": ue, "direction": "ul",
                         "ue_class": ue_class.get(ue), "flow_id": r["flow_id"],
                         "proj_packets": r["proj_packets"], "proj_bytes": r["proj_bytes"]})
    return pd.DataFrame(rows)


def reconcile(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    obs = build_observed(run_dir)
    designed = run_dir / schema.DESIGNED_KPIS
    des = pd.read_parquet(designed) if designed.exists() else build_designed(run_dir)
    if des is None or des.empty:
        return obs
    if obs is None or obs.empty:
        return des
    keys = [k for k in schema.KEYS if k in des.columns and k in obs.columns]
    return des.merge(obs, on=keys, how="outer", suffixes=("", "_obs"))
