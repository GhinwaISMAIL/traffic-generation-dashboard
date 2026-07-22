import json

import pandas as pd

from twindash import dataset, schema


def sample_run(tmp_path):
    run = tmp_path / "run_dataset"
    scripts = run / schema.SCRIPTS_DIR
    logs = run / schema.LOGS_DIR
    scripts.mkdir(parents=True)
    logs.mkdir()
    (run / schema.CONFIG).write_text(json.dumps({"simulation_duration": 30}))
    (run / schema.RUN_PROFILE).write_text(json.dumps({
        "testbed": "powder_ric5g_distributed",
        "xapp": {"expected_subscriptions": 4},
        "capabilities": {"xapp": True},
    }))
    (run / schema.CHANNEL_SCHEDULE).write_text(json.dumps({
        "schema_version": 1, "enabled": True,
        "expected_model_type": "AWGN",
        "events": [{"at_s": 1, "target": "ue1", "direction": "dl",
                    "parameter": "noise_power_dB", "value": -20}],
    }))
    (scripts / "manifest.csv").write_text(
        "ue_name,ue_ip,ue_class\nue1,12.1.1.2,heavy\n")
    (scripts / "flow_batch_map.csv").write_text(
        "unique_flow_id,ue_name,app,direction\n101,ue1,video,dl\n")
    (logs / "run_timing.json").write_text(json.dumps({
        "run_id": "mgen-20260722-120000",
        "senders_start_epoch": 1_000_009.0,
        "nodes": {
            "core": {"midnight_epoch": 1_000_000, "sod_s": 10},
            "cell1": {"midnight_epoch": 1_000_000, "sod_s": 10},
        },
        "ue_node": {"ue1": "cell1"},
    }))
    (logs / "dn_dl_tx.log").write_text(
        "00:00:10.000000 SEND proto>UDP flow>101 seq>1 "
        "src>192.168.72.135/5001 dst>12.1.1.2/5001 size>1000\n"
        "00:00:11.000000 SEND proto>UDP flow>101 seq>2 "
        "src>192.168.72.135/5001 dst>12.1.1.2/5001 size>1000\n")
    (logs / "ue1_dl_rx.log").write_text(
        "00:00:10.010000 RECV proto>UDP flow>101 seq>1 "
        "src>192.168.72.135/5001 dst>12.1.1.2/5001 "
        "sent>00:00:10.000000 size>1000\n"
        "00:00:11.010000 RECV proto>UDP flow>101 seq>2 "
        "src>192.168.72.135/5001 dst>12.1.1.2/5001 "
        "sent>00:00:11.000000 size>1000\n")
    (logs / "rnti_map.csv").write_text(
        "ue,cell,ue_index,nb_id,rnti,pdu_ip\n"
        "ue1,1,1,3584,100,12.1.1.2\n")
    (logs / "prb_by_second.csv").write_text(
        "utc_second,recv_tstamp_us,source_tstamp_us,nb_id,rnti,dl_aggr_prb,ul_aggr_prb,samples\n"
        "1000010,1000010000000,5000000,3584,100,10,20,1000\n"
        "1000011,1000011000000,5500000,3584,100,20,25,1000\n")
    (logs / "xapp.log").write_text(
        ("[xApp]: Successfully subscribed\n" * 4) +
        ("[xApp]: E42 SUBSCRIPTION DELETE RESPONSE rx\n" * 4) +
        "Test xApp run SUCCESSFULLY\n")
    (logs / "channel_state.json").write_text(json.dumps({
        "success": True,
        "traffic_start_reference_epoch": 1_000_009.0,
        "initial_state": [{
            "target": "ue1", "direction": "dl",
            "parameter": "noise_power_dB", "observed": -30,
            "model_type": "AWGN", "model_name": "rfsimu_channel_ue0",
        }],
        "transitions": [{
            "target": "ue1", "direction": "dl",
            "parameter": "noise_power_dB", "observed": -20,
            "model_type": "AWGN", "model_name": "rfsimu_channel_ue0",
            "applied_epoch": 1_000_010.2, "verified": True,
            "status": "verified",
        }],
    }))
    return run


def test_training_frame_joins_radio_traffic_and_verified_channel(tmp_path):
    frame = dataset.training_frame(sample_run(tmp_path))
    row = frame[frame["utc_second"] == 1_000_011].iloc[0]
    assert row["execution_id"] == "mgen-20260722-120000"
    assert row["ue_class"] == "heavy"
    assert row["dl_prb"] == 10
    assert row["dl_mbps"] == 0.008
    assert row["dl_noise_power_dB"] == -20
    assert bool(row["channel_verified"])
    partial = frame[frame["utc_second"] == 1_000_010].iloc[0]
    assert bool(partial["channel_transition_partial"])
    assert not bool(partial["channel_verified"])


def test_archive_is_immutable_and_export_splits_by_execution(tmp_path):
    run = sample_run(tmp_path)
    first = dataset.archive_execution(run)
    second = dataset.archive_execution(run)
    assert first == second
    assert (first / schema.UE_SECOND_FEATURES).exists()
    dataset.archive_execution(run, include_raw=True)
    assert (first / "raw_mgen_logs.tar.gz").exists()
    metadata = json.loads((first / schema.EXECUTION_METADATA).read_text())
    assert metadata["include_raw_logs"] is True
    assert metadata["quality"]["channel_state_verified"] is True
    assert metadata["quality"]["radio_clock_valid"] is True
    assert metadata["quality"]["radio_clock"] == "dual"
    records = dataset.list_executions(tmp_path)
    dataset.update_annotations(
        records[0], include=True, tags="calibration, awgn", notes="clean run")
    target = dataset.export(
        records, tmp_path / "datasets" / "example", include_csv=True)
    features = pd.read_parquet(target / schema.UE_SECOND_FEATURES)
    assert features["split"].nunique() == 1
    assert set(features["execution_id"]) == {"mgen-20260722-120000"}
    assert (target / "ue_second_features.csv").exists()
    exported = json.loads((target / "dataset_manifest.json").read_text())
    assert exported["executions"][0]["annotations"]["tags"] == [
        "calibration", "awgn"]
