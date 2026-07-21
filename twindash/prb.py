"""Per-UE PRB from the FlexRIC xApp database.

The runner aggregates MAC_UE to per-second cumulative counters on the core and
pulls prb_by_second.csv into <run>/logs. This module diffs those counters and
maps (nb_id, rnti) to ue names via rnti_map.csv, which the runner snapshots at
the start of every run.

Counters are cumulative within a UE's MAC context and reset on reattach, so a
negative difference marks an epoch boundary rather than a measurement.
"""
from pathlib import Path

import pandas as pd

from . import kpis

PRB_CSV = "prb_by_second.csv"
RNTI_MAP = "rnti_map.csv"


def _logs(run_dir) -> Path:
    return Path(run_dir) / "logs"


def load_rnti_map(run_dir) -> pd.DataFrame:
    path = _logs(run_dir) / RNTI_MAP
    if not path.exists():
        return pd.DataFrame(columns=["ue", "cell", "nb_id", "rnti"])
    m = pd.read_csv(path)
    return m[["ue", "cell", "nb_id", "rnti"]]


def prb_timeseries(run_dir) -> pd.DataFrame:
    """Per-UE PRB consumed per second, plus any radio averages the xApp carried.
    Seconds where the counter went backwards are dropped and counted as epochs."""
    path = _logs(run_dir) / PRB_CSV
    if not path.exists():
        return pd.DataFrame(columns=["t_s", "ue", "dl_prb", "ul_prb"])

    df = pd.read_csv(path).sort_values(["nb_id", "rnti", "utc_second"])
    g = df.groupby(["nb_id", "rnti"])
    df["dl_prb"] = g["dl_aggr_prb"].diff()
    df["ul_prb"] = g["ul_aggr_prb"].diff()

    epochs = int(((df["dl_prb"] < 0) | (df["ul_prb"] < 0)).sum())
    df.loc[df["dl_prb"] < 0, "dl_prb"] = pd.NA
    df.loc[df["ul_prb"] < 0, "ul_prb"] = pd.NA
    df = df.dropna(subset=["dl_prb", "ul_prb"])

    m = load_rnti_map(run_dir)
    if not m.empty:
        df = df.merge(m, on=["nb_id", "rnti"], how="left")
    else:
        df["ue"] = df["rnti"].astype(str)

    df["t_s"] = df["utc_second"] - df["utc_second"].min()
    keep = ["t_s", "ue", "cell", "nb_id", "rnti", "dl_prb", "ul_prb", "samples"]
    keep += [c for c in df.columns if c.endswith("_avg")]
    out = df[[c for c in keep if c in df.columns]].reset_index(drop=True)
    out.attrs["epoch_boundaries"] = epochs
    return out


def idle_floor(prb: pd.DataFrame, driven: set) -> pd.DataFrame:
    """Median per-second PRB of attached but undriven UEs — the signalling floor
    to subtract before interpreting driven-UE consumption."""
    if prb.empty or "ue" not in prb.columns:
        return pd.DataFrame()
    idle = prb[~prb["ue"].isin(driven)]
    if idle.empty:
        return pd.DataFrame()
    return (idle.groupby("cell")[["dl_prb", "ul_prb"]]
               .median().reset_index()
               .rename(columns={"dl_prb": "dl_prb_floor", "ul_prb": "ul_prb_floor"}))


def efficiency(run_dir, window_s: float = 1.0) -> pd.DataFrame:
    """App-layer bits per resource block, per UE per second.
    Joins throughput (MGEN logs) to PRB (xApp) on (t_s, ue). Both sides are
    wall-clock seconds, so this relies on NTP sync across core and cell nodes."""
    prb = prb_timeseries(run_dir)
    if prb.empty:
        return pd.DataFrame()

    tp = kpis.throughput_timeseries(run_dir, window_s=window_s, per="ue")
    if tp.empty:
        return pd.DataFrame()

    tp = (tp.groupby(["t_s", "ue"])["mbps"].sum().reset_index())
    j = tp.merge(prb[["t_s", "ue", "dl_prb", "ul_prb"]], on=["t_s", "ue"], how="inner")
    total_prb = j["dl_prb"] + j["ul_prb"]
    j["bits_per_prb"] = (j["mbps"] * 1e6 * window_s) / total_prb.replace(0, pd.NA)
    return j
