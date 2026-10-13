import json

import pandas as pd
import pytest

from twindash import mgen_script


def workload(tmp_path):
    scripts = tmp_path / "mgen_scripts"
    scripts.mkdir()
    (tmp_path / "config.json").write_text(json.dumps({"simulation_duration": 5}))
    (scripts / "dn_dl_tx.mgn").write_text(
        "1 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 PERIODIC [10 100]\n"
        "3 ON 102 UDP SRC 15002 DST 12.1.1.2/5000 PERIODIC [2 500]\n"
        "4 OFF 101\n5 OFF 102\n")
    (scripts / "ue1_ul_tx.mgn").write_text(
        "0 ON 201 UDP SRC 15003 DST 192.168.72.135/5001 PERIODIC [1 200]\n5 OFF 201\n")
    pd.DataFrame([
        {"unique_flow_id": 101, "direction": "dl", "scenario_role": "baseline",
         "session_id": "s1", "app": "googlemaps"},
        {"unique_flow_id": 102, "direction": "dl", "scenario_role": "augmented",
         "session_id": "s2", "app": "whatsapp"},
        {"unique_flow_id": 201, "direction": "ul", "scenario_role": "baseline",
         "session_id": "s1", "app": "googlemaps"},
    ]).to_csv(scripts / "flow_batch_map.csv", index=False)
    return tmp_path


def test_load_integrates_overlaps_and_uses_actual_final_bin_duration(tmp_path):
    run = workload(tmp_path)
    result = mgen_script.projected_load_timeseries(run, interval_s=2)
    dl = result[result.direction == "dl"]
    ul = result[result.direction == "ul"]
    assert dl.offered_bytes.tolist() == [1000, 3000, 1000]
    assert ul.offered_bytes.tolist() == [400, 400, 200]
    assert dl.offered_mbps.tolist() == pytest.approx([0.004, 0.012, 0.008])
    assert dl.offered_packets.tolist() == [10, 22, 2]
    legacy = mgen_script.projected_from_script(run / "mgen_scripts" / "dn_dl_tx.mgn")
    assert dl.offered_bytes.sum() == sum(row["proj_bytes"] for row in legacy)


def test_clipped_windows_include_traffic_already_active_at_the_start(tmp_path):
    result = mgen_script.projected_load_timeseries(
        workload(tmp_path), interval_s=2, start_s=1.5, end_s=4.5)
    dl = result[result.direction == "dl"]
    assert dl.offered_bytes.tolist() == [2500, 1500]
    assert dl.interval_end_s.tolist() == [3.5, 4.5]


def test_baseline_and_augmented_totals_reconcile_with_combined_load(tmp_path):
    run = workload(tmp_path)
    result = mgen_script.projected_load_timeseries(
        run, interval_s=2, group_by=("scenario_role", "session_id"))
    combined = result.groupby(["interval_start_s", "direction"]).offered_bytes.sum()
    expected = mgen_script.projected_load_timeseries(run, interval_s=2).set_index(
        ["interval_start_s", "direction"]).offered_bytes
    pd.testing.assert_series_equal(combined, expected)
    added = result[result.scenario_role == "augmented"]
    assert added.offered_bytes.sum() == 2000
    assert set(added.session_id) == {"s2"}


@pytest.mark.parametrize("text, message", [
    ("1 OFF 101\n", "OFF without ON"),
    ("1 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 PERIODIC [10 100]\n", "missing OFF"),
    ("1 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 POISSON [10 100]\n", "PERIODIC"),
    ("1 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 PERIODIC [0 100]\n", "invalid"),
    ("1 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 PERIODIC [10 100] COUNT 5\n", "PERIODIC"),
])
def test_invalid_scripts_fail_instead_of_underreporting_load(tmp_path, text, message):
    run = workload(tmp_path)
    (run / "mgen_scripts" / "dn_dl_tx.mgn").write_text(text)
    with pytest.raises(ValueError, match=message):
        mgen_script.projected_load_timeseries(run)


def test_reused_flow_windows_and_idle_bins(tmp_path):
    run = workload(tmp_path)
    (run / "mgen_scripts" / "dn_dl_tx.mgn").write_text(
        "0 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 PERIODIC [1e1 100]\n"
        "1 OFF 101\n3 ON 101 UDP SRC 15001 DST 12.1.1.2/5000 PERIODIC [5 100]\n"
        "4 OFF 101\n")
    result = mgen_script.projected_load_timeseries(run, interval_s=1)
    assert result[result.direction == "dl"].offered_bytes.tolist() == [1000, 0, 0, 500, 0]


def test_grouping_requires_complete_and_unambiguous_provenance(tmp_path):
    run = workload(tmp_path)
    path = run / "mgen_scripts" / "flow_batch_map.csv"
    frame = pd.read_csv(path)
    frame.iloc[1:].to_csv(path, index=False)
    with pytest.raises(ValueError, match="no metadata"):
        mgen_script.projected_load_timeseries(run, group_by="scenario_role")
    pd.concat([frame, frame.iloc[:1]]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="unique"):
        mgen_script.projected_load_timeseries(run, group_by="scenario_role")


@pytest.mark.parametrize("kwargs", [
    {"interval_s": 0}, {"interval_s": float("inf")}, {"start_s": -1},
    {"end_s": 0}, {"end_s": float("nan")}, {"group_by": ["direction"]},
])
def test_invalid_projection_parameters_are_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        mgen_script.projected_load_timeseries(workload(tmp_path), **kwargs)
