"""Turn parsed logs into the observed KPI table, and join it against the
projection to get the projected -> sent -> received view the dashboard shows.

  sent  = SEND events from the *_tx.log files (offered, as MGEN emitted it)
  recv  = RECV events from the *_rx.log files (delivered)
  loss  = 1 - recv/sent
  latency = recv_time - sent_time   (needs chrony sync; garbage without it)
"""
from pathlib import Path

import pandas as pd

from . import schema, mgen_log


def build_observed(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    logs = run_dir / schema.LOGS_DIR
    sent_frames, recv_frames = [], []

    for log in sorted(logs.glob("*.log")):
        node, direction = mgen_log.parse_run_name(log.name)
        df = mgen_log.parse_log(log)
        if df.empty:
            continue
        df["node"], df["direction"] = node, direction
        if log.name.endswith("_tx.log"):
            sent_frames.append(df[df.event == "SEND"])
        elif log.name.endswith("_rx.log"):
            recv_frames.append(df[df.event == "RECV"])

    sent = pd.concat(sent_frames, ignore_index=True) if sent_frames else pd.DataFrame()
    recv = pd.concat(recv_frames, ignore_index=True) if recv_frames else pd.DataFrame()

    # sender side: how much was offered per flow
    if not sent.empty:
        s = (sent.groupby(["flow_id", "direction"])
                 .agg(sent_packets=("seq", "count"), sent_bytes=("size", "sum"))
                 .reset_index())
    else:
        s = pd.DataFrame(columns=["flow_id", "direction", "sent_packets", "sent_bytes"])

    # receiver side: what arrived, and which UE saw it
    if not recv.empty:
        recv["latency_ms"] = (recv["time"] - recv["sent_time"]) * 1000.0
        r = (recv.groupby(["flow_id", "direction", "node"])
                 .agg(recv_packets=("seq", "count"),
                      recv_bytes=("size", "sum"),
                      latency_ms_mean=("latency_ms", "mean"),
                      latency_ms_p95=("latency_ms", lambda x: x.quantile(0.95)),
                      jitter_ms=("latency_ms", "std"))
                 .reset_index()
                 .rename(columns={"node": "ue"}))
    else:
        r = pd.DataFrame(columns=["flow_id", "direction", "ue", "recv_packets",
                                  "recv_bytes", "latency_ms_mean",
                                  "latency_ms_p95", "jitter_ms"])

    obs = r.merge(s, on=["flow_id", "direction"], how="outer")
    obs["sent_packets"] = obs["sent_packets"].fillna(0).astype(int)
    obs["recv_packets"] = obs["recv_packets"].fillna(0).astype(int)
    obs["loss"] = 1 - obs["recv_packets"] / obs["sent_packets"].replace(0, pd.NA)
    obs.insert(0, "run_id", run_dir.name)
    return obs


def save_observed(run_dir) -> Path:
    out = Path(run_dir) / schema.OBSERVED_KPIS
    build_observed(run_dir).to_parquet(out, index=False)
    return out


def reconcile(run_dir) -> pd.DataFrame:
    """Projected (design) vs sent (tx) vs received (rx), one row per flow."""
    run_dir = Path(run_dir)
    obs = build_observed(run_dir)
    designed = run_dir / schema.DESIGNED_KPIS
    if not designed.exists():
        return obs
    des = pd.read_parquet(designed)
    keys = [k for k in schema.KEYS if k in des.columns and k in obs.columns]
    return des.merge(obs, on=keys, how="outer", suffixes=("", "_obs"))
