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


def test_packet_latency_respects_node_anchors(tmp_path):
    result = kpis.packet_latency(sample_run(tmp_path)).set_index("ue")
    assert result.loc["ue1", "latency_ms_p50"] == pytest.approx(10.0)
    assert result.loc["ue13", "latency_ms_p95"] == pytest.approx(20.0)


def test_duplicate_receives_do_not_inflate_delivery_or_throughput(tmp_path):
    run = sample_run(tmp_path)
    log = run / "logs/ue1_dl_rx.log"
    log.write_text(log.read_text() * 2)
    observed = kpis.build_observed(run).set_index("flow_id")
    assert observed.loc[101, "recv_packets"] == 1
    assert observed.loc[101, "loss"] == 0
    assert observed.loc[101, "recv_mbps"] == pytest.approx(8000 / 60 / 1e6)
    timeline = kpis.throughput_timeseries(run)
    assert timeline.loc[timeline.direction.eq("dl"), "mbps"].sum() == pytest.approx(.008)
    assert kpis.packet_records(run).duplicate_receives.sum() == 1


def test_unmatched_and_size_mismatched_receives_are_rejected(tmp_path):
    run = sample_run(tmp_path)
    log = run / "logs/ue1_dl_rx.log"
    original = log.read_text()
    log.write_text(original.replace("seq>1", "seq>2"))
    with pytest.raises(ValueError, match="no matching SEND"):
        kpis.build_observed(run)
    log.write_text(original.replace("size>1000", "size>900"))
    with pytest.raises(ValueError, match="different payload sizes"):
        kpis.build_observed(run)


def test_missing_receives_are_loss_not_zero_latency(tmp_path):
    run = sample_run(tmp_path)
    (run / "logs/ue1_dl_rx.log").write_text("")
    observed = kpis.build_observed(run).set_index("flow_id")
    assert observed.loc[101, "sent_packets"] == 1
    assert observed.loc[101, "recv_packets"] == 0
    assert observed.loc[101, "loss"] == 1
    assert pd.isna(observed.loc[101, "latency_ms_median"])
    assert "ue1" not in set(kpis.packet_latency(run).ue)


def test_packet_latency_weights_packets_and_deduplicates_receives(tmp_path, monkeypatch):
    run = sample_run(tmp_path)
    pd.DataFrame({"unique_flow_id": [101, 102], "ue_name": ["ue1", "ue1"],
                  "app": ["video", "video"], "direction": ["dl", "dl"]}).to_csv(
        run / "mgen_scripts/flow_batch_map.csv", index=False)
    sent = pd.DataFrame({"direction": ["dl"] * 11, "flow_id": [101] * 10 + [102],
                         "seq": list(range(10)) + [0], "time": [0.] * 11,
                         "utc_time": [1000.] * 11})
    received = sent.copy()
    sent["size"] = 100
    received["size"] = 100
    received["time"] = [1.] * 10 + [.01]
    received["utc_time"] += received["time"]
    received = pd.concat([received, received.tail(1)], ignore_index=True)
    monkeypatch.setattr(kpis, "_load_events", lambda path: (sent, received))
    result = kpis.packet_latency(run)
    assert result.iloc[0]["packets"] == 11
    assert result.iloc[0]["latency_ms_p50"] == pytest.approx(1000.)
    assert result.iloc[0]["latency_ms_p95"] == pytest.approx(1000.)
    assert kpis.by_ue(run).iloc[0]["latency_ms_median"] == pytest.approx(1000.)
    assert kpis.by_ue(run).iloc[0]["latency_ms_p95"] == pytest.approx(1000.)
    received.loc[0, "utc_time"] = 999.
    invalid = kpis.packet_latency(run)
    assert invalid.attrs["invalid_latency_packets"] == 1
    assert invalid.iloc[0]["packets"] == 10
    sent.loc[1, "seq"] = 0
    with pytest.raises(ValueError, match="Duplicate transmitted"):
        kpis.packet_latency(run)


def test_throughput_and_prb_join_on_absolute_second_and_direction(tmp_path):
    run = sample_run(tmp_path)
    throughput = kpis.throughput_timeseries(run)
    assert set(throughput["utc_second"]) == {1_036_000.0}

    efficiency = prb.efficiency(run)
    assert set(efficiency["direction"]) == {"dl", "ul"}
    assert efficiency["bits_per_prb"].dtype.kind == "f"
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


def test_traffic_and_radio_axes_share_the_run_start(tmp_path):
    run = sample_run(tmp_path)
    path = run / "logs/run_timing.json"
    document = json.loads(path.read_text())
    document["senders_start_epoch"] = 1_035_990
    path.write_text(json.dumps(document))
    for frame in (kpis.throughput_timeseries(run), prb.prb_timeseries(run), prb.efficiency(run)):
        assert set(frame["t_s"]) == {10.0}


def test_capture_quality_does_not_assume_legacy_timestamps_are_verified(tmp_path):
    run = sample_run(tmp_path)
    assert prb.capture_quality(run)["timestamp_stage"] == "unverified"
    path = run / "logs/radio_capture_quality.json"
    path.write_text(json.dumps({"timestamp_stage": "pre_queue_utc", "complete": True}))
    assert prb.prb_timeseries(run).attrs["capture_quality"]["complete"] is True
    path.write_text("[]")
    assert "error" in prb.capture_quality(run)


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
    assert "no core receipt timestamp" in radio.attrs["error"]


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
