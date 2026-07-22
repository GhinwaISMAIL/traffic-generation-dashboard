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

from . import kpis, timing

PRB_CSV = "prb_by_second.csv"
RNTI_MAP = "rnti_map.csv"
DUAL_CLOCK_COLUMNS = ("recv_tstamp_us", "source_tstamp_us")
REQUIRED_PRB_COLUMNS = (
    "utc_second", *DUAL_CLOCK_COLUMNS, "nb_id", "rnti",
    "dl_aggr_prb", "ul_aggr_prb", "samples",
)


def _empty(error: str | None = None) -> pd.DataFrame:
    out = pd.DataFrame(columns=[
        "utc_second", "t_s", *DUAL_CLOCK_COLUMNS, "ue", "dl_prb", "ul_prb",
    ])
    if error:
        out.attrs["error"] = error
        out.attrs["clock_valid"] = False
    return out


def _logs(run_dir) -> Path:
    return Path(run_dir) / "logs"


def load_rnti_map(run_dir) -> pd.DataFrame:
    path = _logs(run_dir) / RNTI_MAP
    if not path.exists():
        return pd.DataFrame(columns=["ue", "cell", "nb_id", "rnti"])
    m = pd.read_csv(path)
    required = ["ue", "cell", "nb_id", "rnti"]
    missing = [c for c in required if c not in m]
    if missing:
        raise ValueError(f"rnti_map.csv is missing columns: {missing}")
    out = m[required].dropna().copy()
    out[["cell", "nb_id", "rnti"]] = out[["cell", "nb_id", "rnti"]].astype(int)
    duplicate = out.duplicated(["nb_id", "rnti"], keep=False)
    if duplicate.any():
        conflicts = out.loc[duplicate].sort_values(["nb_id", "rnti", "ue"])
        raise ValueError("one (nb_id, rnti) maps to multiple rows:\n"
                         + conflicts.to_string(index=False))
    return out


def prb_timeseries(run_dir) -> pd.DataFrame:
    """Per-UE PRB consumed per second, plus any radio averages the xApp carried.
    Seconds where the counter went backwards are dropped and counted as epochs."""
    path = _logs(run_dir) / PRB_CSV
    if not path.exists():
        return _empty()

    df = pd.read_csv(path)
    missing = [column for column in REQUIRED_PRB_COLUMNS if column not in df]
    if missing:
        if any(column in missing for column in DUAL_CLOCK_COLUMNS):
            return _empty(
                "legacy PRB capture has no core receipt timestamp. Its service-model "
                "timestamp is RFsim/radio time, not UTC; re-run after applying the "
                "FlexRIC dual-clock patch and exclude this execution from training")
        raise ValueError(f"prb_by_second.csv is missing columns: {missing}")
    if df.empty:
        return _empty()

    df = df.sort_values(["nb_id", "rnti", "recv_tstamp_us", "utc_second"])
    g = df.groupby(["nb_id", "rnti"])
    df["dl_prb"] = g["dl_aggr_prb"].diff()
    df["ul_prb"] = g["ul_aggr_prb"].diff()
    df["receipt_interval_s"] = g["recv_tstamp_us"].diff() / 1_000_000

    epochs = int(((df["dl_prb"] < 0) | (df["ul_prb"] < 0)).sum())
    df.loc[df["dl_prb"] < 0, "dl_prb"] = pd.NA
    df.loc[df["ul_prb"] < 0, "ul_prb"] = pd.NA
    df = df.dropna(subset=["dl_prb", "ul_prb", "receipt_interval_s"])

    # A counter delta covers the interval between two xApp receipts.  Joining a
    # shortened or delayed interval to a full MGEN second produces artificial
    # bits/PRB spikes, so retain only approximately one-second intervals.
    interval_ok = df["receipt_interval_s"].between(0.5, 1.5, inclusive="both")
    irregular_intervals = int((~interval_ok).sum())
    df = df.loc[interval_ok].copy()
    if df.empty:
        out = _empty(
            "dual-clock PRB capture has no complete approximately one-second "
            "receipt intervals; repeat the xApp window before archiving")
        out.attrs["irregular_intervals"] = irregular_intervals
        return out

    m = load_rnti_map(run_dir)
    if not m.empty:
        df = df.merge(m, on=["nb_id", "rnti"], how="left", validate="many_to_one")
        df["mapped"] = df["ue"].notna()
    else:
        df["ue"] = df["rnti"].astype(str)
        df["mapped"] = False

    df["t_s"] = df["utc_second"] - df["utc_second"].min()
    keep = ["utc_second", "t_s", *DUAL_CLOCK_COLUMNS, "receipt_interval_s",
            "ue", "mapped", "cell", "nb_id", "rnti", "dl_prb", "ul_prb",
            "samples"]
    keep += [c for c in df.columns if c.endswith("_avg")]
    out = df[[c for c in keep if c in df.columns]].reset_index(drop=True)
    out.attrs["epoch_boundaries"] = epochs
    out.attrs["irregular_intervals"] = irregular_intervals
    out.attrs["clock_valid"] = True
    receipt_span = (df["recv_tstamp_us"].max() - df["recv_tstamp_us"].min()) / 1e6
    source_span = (df["source_tstamp_us"].max() - df["source_tstamp_us"].min()) / 1e6
    out.attrs["receipt_span_s"] = float(receipt_span)
    out.attrs["source_span_s"] = float(source_span)
    out.attrs["source_wall_ratio"] = (
        float(source_span / receipt_span) if receipt_span > 0 else None)
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

    Joins throughput (MGEN logs) to PRB (xApp) on (utc_second, ue) — absolute
    epoch seconds on both sides. The two clocks start at different moments (the
    xApp window opens minutes into the run), so joining on each side's own
    relative t_s pairs unrelated seconds and silently yields plausible,
    wrong numbers. If the MGEN side has no absolute time — logs/run_timing.json
    missing — this returns empty rather than guessing.
    """
    if float(window_s) != 1.0:
        raise ValueError("PRB is aggregated per second; efficiency currently requires window_s=1")

    prb = prb_timeseries(run_dir)
    if prb.empty:
        out = pd.DataFrame()
        out.attrs.update(prb.attrs)
        return out

    tp = kpis.throughput_timeseries(run_dir, window_s=window_s, per="ue")
    if tp.empty:
        return pd.DataFrame()
    if "utc_second" not in tp.columns:
        out = pd.DataFrame()
        out.attrs["error"] = ("no run_timing.json in logs/ — MGEN timestamps "
                              "have no date/zone anchor, so PRB cannot be "
                              "aligned to traffic")
        return out

    tp = (tp.groupby(["utc_second", "ue", "direction"])["mbps"]
            .sum().reset_index())
    resource = prb[["utc_second", "ue", "dl_prb", "ul_prb"]].melt(
        id_vars=["utc_second", "ue"], var_name="direction", value_name="prb")
    resource["direction"] = resource["direction"].str.removesuffix("_prb")
    j = tp.merge(resource, on=["utc_second", "ue", "direction"], how="inner")
    j["bits_per_prb"] = (
        j["mbps"] * 1e6 * window_s) / j["prb"].replace(0, pd.NA)
    j["t_s"] = j["utc_second"] - j["utc_second"].min()
    j.attrs.update(prb.attrs)
    return j
