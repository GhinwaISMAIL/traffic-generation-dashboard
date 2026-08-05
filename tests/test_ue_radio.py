import pandas as pd
import pytest

from twindash import ue_radio


def _write(run, rows):
    logs = run / "logs"
    logs.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(logs / ue_radio.RADIO_CSV, index=False)


def test_ue_radio_uses_embedded_utc_and_computes_emit_lag(tmp_path):
    run = tmp_path / "run_radio"
    _write(run, [{
        "utc_second": 100, "emitted_epoch_us": 101_030_000,
        "ue": "ue1", "cell": 1, "ue_index": 1, "ssb": 0, "samples": 15,
        "ss_rsrp_dbm": -51, "ss_rsrq_db": -10.46, "ss_sinr_db": 38.1,
    }])

    frame = ue_radio.timeseries(run)

    assert frame.iloc[0]["ue_radio_emit_lag_s"] == pytest.approx(.03)
    assert frame.iloc[0]["ue_radio_sample_count"] == 15
    assert frame.attrs["clock_valid"] is True


def test_ue_radio_rejects_duplicate_ue_seconds(tmp_path):
    run = tmp_path / "run_duplicate"
    row = {
        "utc_second": 100, "emitted_epoch_us": 101_030_000,
        "ue": "ue1", "cell": 1, "ue_index": 1, "ssb": 0, "samples": 15,
        "ss_rsrp_dbm": -51, "ss_rsrq_db": -10.46, "ss_sinr_db": 38.1,
    }
    _write(run, [row, row])

    with pytest.raises(ValueError, match="duplicate"):
        ue_radio.timeseries(run)
