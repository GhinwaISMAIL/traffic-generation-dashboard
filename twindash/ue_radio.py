"""UE-side serving-cell radio measurements emitted by the OAI NR UE."""
from pathlib import Path

import numpy as np
import pandas as pd


RADIO_CSV = "ue_radio_by_second.csv"
REQUIRED_COLUMNS = (
    "utc_second", "emitted_epoch_us", "ue", "cell", "ue_index", "ssb",
    "samples", "ss_rsrp_dbm", "ss_rsrq_db", "ss_sinr_db",
)


def _empty(error: str | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(columns=[
        "utc_second", "t_s", "emitted_epoch_us", "ue", "cell", "ue_index",
        "ssb", "ue_radio_sample_count", "ss_rsrp_dbm", "ss_rsrq_db",
        "ss_sinr_db", "ue_radio_emit_lag_s",
    ])
    if error:
        frame.attrs["error"] = error
        frame.attrs["clock_valid"] = False
    return frame


def timeseries(run_dir) -> pd.DataFrame:
    path = Path(run_dir) / "logs" / RADIO_CSV
    if not path.exists():
        return _empty()
    frame = pd.read_csv(path)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame]
    if missing:
        raise ValueError(f"{RADIO_CSV} is missing columns: {missing}")
    if frame.empty:
        return _empty(f"{RADIO_CSV} contains no UE measurements")

    numeric = [column for column in REQUIRED_COLUMNS if column != "ue"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    if frame[numeric].isna().any().any():
        raise ValueError(f"{RADIO_CSV} contains non-numeric required values")
    if not np.isfinite(frame[numeric].to_numpy(dtype=float)).all():
        raise ValueError(f"{RADIO_CSV} contains non-finite required values")
    if frame.duplicated(["utc_second", "ue"]).any():
        raise ValueError(f"{RADIO_CSV} has duplicate (utc_second, ue) rows")
    if (frame["samples"] <= 0).any():
        raise ValueError(f"{RADIO_CSV} contains non-positive sample counts")
    limits = {
        "ss_rsrp_dbm": (-200, 0),
        "ss_rsrq_db": (-50, 10),
        "ss_sinr_db": (-100, 100),
    }
    for column, (lower, upper) in limits.items():
        if not frame[column].between(lower, upper, inclusive="both").all():
            raise ValueError(f"{RADIO_CSV} contains implausible {column} values")

    frame = frame.rename(columns={"samples": "ue_radio_sample_count"})
    frame["utc_second"] = frame["utc_second"].astype("int64")
    frame["ue_radio_emit_lag_s"] = (
        frame["emitted_epoch_us"] / 1_000_000 - (frame["utc_second"] + 1)
    )
    frame["t_s"] = frame["utc_second"] - frame["utc_second"].min()
    frame = frame.sort_values(["utc_second", "ue"]).reset_index(drop=True)
    absolute_lag = frame["ue_radio_emit_lag_s"].abs()
    frame.attrs["clock_valid"] = bool(absolute_lag.quantile(.95) <= .5)
    frame.attrs["emit_lag_s_p95"] = float(absolute_lag.quantile(.95))
    return frame


def merge_with_mac(mac: pd.DataFrame, ue: pd.DataFrame) -> pd.DataFrame:
    if mac.empty:
        result = ue.copy()
    elif ue.empty:
        result = mac.copy()
    else:
        result = mac.merge(
            ue, on=["utc_second", "ue"], how="outer", validate="one_to_one",
            suffixes=("", "_ue_radio"),
        )
        for column in ("cell", "t_s"):
            other = f"{column}_ue_radio"
            if other in result:
                result[column] = result[column].fillna(result.pop(other))
    result.attrs.update(mac.attrs)
    if not ue.empty:
        result.attrs["ue_radio_clock_valid"] = ue.attrs.get("clock_valid")
        result.attrs["ue_radio_emit_lag_s_p95"] = ue.attrs.get("emit_lag_s_p95")
    return result
