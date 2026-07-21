"""Read the per-UE burst tables (the design detail) for summaries and coupling.

Each bursts/<ue>_<app>_<dir>_bursts.parquet holds the designed bursts for one
UE/app/direction. Request-response apps (igap, telegram) carry correlated_batch_id
linking a UL request to the DL bursts it triggered; streaming apps (youtube,
filimo, aparat) don't — their DL is a server push, not a response.
"""
import re
from pathlib import Path

import pandas as pd

from . import schema

_FN = re.compile(r"^(ue\d+)_([a-z0-9]+)_(dl|ul)_bursts$")


def load_bursts(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    bdir = run_dir / "bursts"
    frames = []
    for f in sorted(bdir.glob("ue*_*_*_bursts.parquet")):
        m = _FN.match(f.stem)
        if not m:
            continue
        ue, app, direction = m.groups()
        df = pd.read_parquet(f)
        df["ue"], df["app"], df["direction"] = ue, app, direction
        frames.append(df)
    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    man = run_dir / schema.SCRIPTS_DIR / "manifest.csv"
    if man.exists():
        cls = pd.read_csv(man).set_index("ue_name")["ue_class"].to_dict()
        out["ue_class"] = out["ue"].map(cls)
    # packets_export is the integer MGEN was told to send; fall back to packets
    out["_packets"] = out["packets_export"] if "packets_export" in out else out["packets"]
    return out


def summarize(bursts):
    """(per UE x direction, per app x direction) totals — packets and MB."""
    if bursts.empty:
        return pd.DataFrame(), pd.DataFrame()
    group = ["ue", "ue_class", "direction"] if "ue_class" in bursts else ["ue", "direction"]
    by_ue = (bursts.groupby(group)
                   .agg(bursts=("_packets", "size"),
                        packets=("_packets", "sum"),
                        MB=("bytes", lambda s: round(s.sum() / 1e6, 2)))
                   .reset_index())
    by_app = (bursts.groupby(["app", "direction"])
                    .agg(packets=("_packets", "sum"),
                         MB=("bytes", lambda s: round(s.sum() / 1e6, 2)))
                    .reset_index())
    return by_ue, by_app


def coupling(bursts) -> pd.DataFrame:
    """One row per request batch (apps that carry correlated_batch_id):
    DL bursts triggered, bytes each way, and designed RTT (first DL start −
    last UL end)."""
    if bursts.empty or "correlated_batch_id" not in bursts.columns:
        return pd.DataFrame()
    cp = bursts.dropna(subset=["correlated_batch_id"])
    if cp.empty:
        return pd.DataFrame()

    rows = []
    for (ue, app, bid), g in cp.groupby(["ue", "app", "correlated_batch_id"]):
        dl, ul = g[g.direction == "dl"], g[g.direction == "ul"]
        rtt = None
        if not dl.empty and not ul.empty:
            rtt = float(dl["absolute_start_time"].min() - ul["absolute_end_time"].max())
        rows.append({"ue": ue, "app": app, "batch": int(bid),
                     "dl_bursts": len(dl), "ul_bursts": len(ul),
                     "dl_bytes": int(dl["bytes"].sum()), "ul_bytes": int(ul["bytes"].sum()),
                     "rtt_ms": round(rtt * 1000, 1) if rtt is not None else None})
    return pd.DataFrame(rows)


def coupling_summary(cp) -> pd.DataFrame:
    """Per UE/app: number of requests, mean DL bursts per request, mean RTT."""
    if cp.empty:
        return pd.DataFrame()
    return (cp.groupby(["ue", "app"])
              .agg(requests=("batch", "nunique"),
                   mean_dl_per_request=("dl_bursts", lambda s: round(s.mean(), 1)),
                   mean_rtt_ms=("rtt_ms", lambda s: round(s.dropna().mean(), 1)))
              .reset_index())
