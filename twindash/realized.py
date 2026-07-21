"""Realized request->response RTT, from MGEN logs + flow_batch_map.csv.

flow_batch_map.csv (written by Notebook 2) links each MGEN unique_flow_id to the
correlated_batch_id of the request it belongs to. For each batch we take, on the
UE itself: when the UL request was SENT (ue*_ul_tx.log) and when the first DL
response was RECEIVED (ue*_dl_rx.log). Both timestamps are UE-local, so their
difference is a request->response latency with no UE<->DN clock sync needed —
directly comparable to the designed RTT carried in the burst tables.
"""
from pathlib import Path

import pandas as pd

from . import schema, mgen_log, bursts


def _load_ue_events(run_dir) -> pd.DataFrame:
    logs = Path(run_dir) / schema.LOGS_DIR
    frames = []
    for log in sorted(logs.glob("ue*_ul_tx.log")) + sorted(logs.glob("ue*_dl_rx.log")):
        node, _ = mgen_log.parse_run_name(log.name)
        ev = mgen_log.parse_log(log)
        if ev.empty:
            continue
        ev["node"] = node
        frames.append(ev)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def realized_coupling(run_dir) -> pd.DataFrame:
    """Per (ue, app, batch): realized request->response RTT from UE-local logs."""
    run_dir = Path(run_dir)
    map_path = run_dir / schema.SCRIPTS_DIR / "flow_batch_map.csv"
    if not map_path.exists():
        return pd.DataFrame()
    fbm = pd.read_csv(map_path).dropna(subset=["correlated_batch_id"])
    if fbm.empty:
        return pd.DataFrame()

    ev = _load_ue_events(run_dir)
    if ev.empty:
        return pd.DataFrame()

    fbm = fbm.set_index("unique_flow_id")
    ev = ev.join(fbm[["ue_name", "app", "direction", "correlated_batch_id"]], on="flow_id")
    ev = ev.dropna(subset=["correlated_batch_id"])
    if ev.empty:
        return pd.DataFrame()

    rows = []
    for (ue, app, b), g in ev.groupby(["ue_name", "app", "correlated_batch_id"]):
        ul_send = g.loc[(g["direction"] == "ul") & (g["event"] == "SEND"), "time"]
        dl_recv = g.loc[(g["direction"] == "dl") & (g["event"] == "RECV"), "time"]
        rtt = None
        if not ul_send.empty and not dl_recv.empty:
            rtt = (dl_recv.min() - ul_send.min()) * 1000.0
        rows.append({"ue": ue, "app": app, "batch": int(b),
                     "realized_rtt_ms": round(rtt, 1) if rtt is not None else None,
                     "responded": bool(not dl_recv.empty)})
    return pd.DataFrame(rows)


def rtt_gap(run_dir) -> pd.DataFrame:
    """Designed vs realized RTT per request batch. Returns designed-only (with
    realized/gap blank) when no map or no logs are present yet."""
    des = bursts.coupling(bursts.load_bursts(run_dir))
    if des.empty:
        return pd.DataFrame()
    des = des[["ue", "app", "batch", "rtt_ms"]].rename(columns={"rtt_ms": "designed_rtt_ms"})

    real = realized_coupling(run_dir)
    if real.empty:
        des["realized_rtt_ms"] = pd.NA
        des["gap_ms"] = pd.NA
        des["responded"] = pd.NA
        return des

    out = des.merge(real, on=["ue", "app", "batch"], how="left")
    out["gap_ms"] = out["realized_rtt_ms"] - out["designed_rtt_ms"]
    return out


def rtt_gap_summary(gap) -> pd.DataFrame:
    """Per (ue, app): mean designed RTT, mean realized RTT, mean gap, and the
    fraction of requests that got a response."""
    if gap.empty:
        return pd.DataFrame()

    def _mean(s):
        s = s.dropna()
        return round(s.mean(), 1) if len(s) else None

    def _rate(s):
        s = s.dropna()
        return round(s.mean(), 2) if len(s) else None

    return (gap.groupby(["ue", "app"])
               .agg(batches=("batch", "nunique"),
                    designed_rtt_ms=("designed_rtt_ms", _mean),
                    realized_rtt_ms=("realized_rtt_ms", _mean),
                    gap_ms=("gap_ms", _mean),
                    response_rate=("responded", _rate))
               .reset_index())
