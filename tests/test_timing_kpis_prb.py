import json
from pathlib import Path

import pandas as pd
import pytest

from twindash import kpis, mgen_log, prb, timing


def _write(path: Path, text: str):
    path.write_text(text.strip() + "\n")


def sample_run(tmp_path: Path) -> Path:
    run = tmp_path / "run_sample"
    scripts = run / "mgen_scripts"
    logs = run / "logs"
    scripts.mkdir(parents=True)
    logs.mkdir()
    (run / "config.json").write_text(json.dumps({"simulation_duration": 60}))
    _write(scripts / "flow_batch_map.csv", """
unique_flow_id,ue_name,app,direction
101,ue1,video,dl
201,ue13,chat,ul
""")

    # The nodes intentionally use different civil time zones.  Both events are
    # converted to the same epoch timeline by their own midnight anchors.
    timing_doc = {
        "nodes": {
            "core": {"midnight_epoch": 1_000_000, "sod_s": 36_000},
            "cell1": {"midnight_epoch": 1_003_600, "sod_s": 32_400},
            "cell2": {"midnight_epoch": 996_400, "sod_s": 39_600},
        },
        "ue_node": {"ue1": "cell1", "ue13": "cell2"},
        "xapp": {"start_epoch": 1_036_000, "window_s": 60},
    }
    (logs / "run_timing.json").write_text(json.dumps(timing_doc))

    _write(logs / "dn_dl_tx.log", """
10:00:00.000000 SEND proto>UDP flow>101 seq>1 src>192.168.72.135/5001 dst>12.1.1.10/5001 size>1000
""")
    _write(logs / "ue1_dl_rx.log", """
09:00:00.010000 RECV proto>UDP flow>101 seq>1 src>192.168.72.135/5001 dst>12.1.1.10/5001 sent>10:00:00.000000 size>1000
""")
    _write(logs / "ue13_ul_tx.log", """
11:00:00.000000 SEND proto>UDP flow>201 seq>1 src>12.1.1.30/5000 dst>192.168.72.135/5000 size>500
""")
    _write(logs / "dn_ul_rx.log", """
10:00:00.020000 RECV proto>UDP flow>201 seq>1 src>12.1.1.30/5000 dst>192.168.72.135/5000 sent>11:00:00.000000 size>500
""")
    _write(logs / "xapp.log", "10:00:00.000000 diagnostic only")

    _write(logs / "rnti_map.csv", """
ue,cell,ue_index,nb_id,rnti,pdu_ip
ue1,1,1,3584,100,12.1.1.10
ue13,2,1,3585,200,12.1.1.30
""")
    _write(logs / "prb_by_second.csv", """
utc_second,recv_tstamp_us,source_tstamp_us,nb_id,rnti,dl_aggr_prb,ul_aggr_prb,samples
1035999,1035999000000,5000000,3584,100,10,20,1000
1036000,1036000000000,5500000,3584,100,20,25,1000
1035999,1035999000000,5000000,3585,200,50,60,1000
1036000,1036000000000,5500000,3585,200,52,70,1000
""")
    return run


def test_sender_and_receiver_use_their_own_clock_anchors(tmp_path):
    observed = kpis.build_observed(sample_run(tmp_path)).set_index("flow_id")
    assert observed.loc[101, "latency_ms_median"] == pytest.approx(10.0)
    assert observed.loc[201, "latency_ms_median"] == pytest.approx(20.0)


def test_throughput_and_prb_join_on_absolute_second_and_direction(tmp_path):
    run = sample_run(tmp_path)
    throughput = kpis.throughput_timeseries(run)
    assert set(throughput["utc_second"]) == {1_036_000.0}

    efficiency = prb.efficiency(run)
    assert set(efficiency["direction"]) == {"dl", "ul"}
    dl = efficiency[(efficiency["ue"] == "ue1") &
                    (efficiency["direction"] == "dl")].iloc[0]
    ul = efficiency[(efficiency["ue"] == "ue13") &
                    (efficiency["direction"] == "ul")].iloc[0]
    assert dl["prb"] == 10
    assert dl["bits_per_prb"] == pytest.approx(800.0)
    assert ul["prb"] == 10
    assert ul["bits_per_prb"] == pytest.approx(400.0)


def test_midnight_rollover_helpers():
    assert timing.to_utc(5.0, 1_000_000, ref_sod=86_390.0) == 1_086_405.0
    assert mgen_log.elapsed_seconds(86_399.9, 0.1) == pytest.approx(0.2)


def test_duplicate_rnti_mapping_is_rejected(tmp_path):
    run = sample_run(tmp_path)
    with (run / "logs" / "rnti_map.csv").open("a") as stream:
        stream.write("ue2,1,2,3584,100,12.1.1.11\n")
    with pytest.raises(ValueError, match="maps to multiple"):
        prb.load_rnti_map(run)


def test_legacy_source_clock_is_rejected(tmp_path):
    run = sample_run(tmp_path)
    path = run / "logs" / "prb_by_second.csv"
    frame = pd.read_csv(path).drop(columns=["recv_tstamp_us", "source_tstamp_us"])
    frame.to_csv(path, index=False)
    radio = prb.prb_timeseries(run)
    assert radio.empty
    assert "RFsim/radio time" in radio.attrs["error"]


def test_irregular_receipt_interval_is_not_joined_to_full_mgen_second(tmp_path):
    run = sample_run(tmp_path)
    path = run / "logs" / "prb_by_second.csv"
    frame = pd.read_csv(path)
    extra = frame[frame["utc_second"] == 1036000].copy()
    extra["utc_second"] = 1036001
    extra["recv_tstamp_us"] += 100_000
    extra["source_tstamp_us"] += 50_000
    extra[["dl_aggr_prb", "ul_aggr_prb"]] += 1
    pd.concat([frame, extra], ignore_index=True).to_csv(path, index=False)
    radio = prb.prb_timeseries(run)
    assert radio.attrs["irregular_intervals"] == 2
    assert set(radio["utc_second"]) == {1036000}
