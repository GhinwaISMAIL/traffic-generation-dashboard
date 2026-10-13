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


@pytest.mark.parametrize("serving_present", [True, False])
def test_unmapped_cell_measurements_do_not_enter_ue_merge(serving_present):
    mac = pd.DataFrame({"utc_second": [100, 100, 100], "ue": ["ue1", None, None],
                        "rnti": [10, 20, 30], "dl_prb": [5, 6, 7]})
    mac.attrs["clock_valid"] = True
    ue = (pd.DataFrame({"utc_second": [100, 101], "ue": ["ue1", "ue1"],
                        "ss_sinr_db": [30, 31]}) if serving_present else pd.DataFrame())
    merged = ue_radio.merge_with_mac(mac, ue)
    assert merged.ue.tolist() == (["ue1", "ue1"] if serving_present else ["ue1"])
    assert merged.iloc[0].dl_prb == 5
    assert merged.attrs["unmapped_mac_rows"] == 2
    assert merged.attrs["clock_valid"] is True
    assert len(mac) == 3


def test_duplicate_mapped_mac_measurements_still_fail():
    mac = pd.DataFrame({"utc_second": [100, 100], "ue": ["ue1", "ue1"], "dl_prb": [5, 6]})
    ue = pd.DataFrame({"utc_second": [100], "ue": ["ue1"], "ss_sinr_db": [30]})
    with pytest.raises(pd.errors.MergeError):
        ue_radio.merge_with_mac(mac, ue)
