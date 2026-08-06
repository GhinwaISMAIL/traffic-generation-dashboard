import json

import pandas as pd
import pytest

from twindash import dataset, dataset_v2, schema


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
    (logs / schema.UE_RADIO_BY_SECOND).write_text(
        "utc_second,emitted_epoch_us,ue,cell,ue_index,ssb,samples,ss_rsrp_dbm,ss_rsrq_db,ss_sinr_db\n"
        "1000010,1000011030000,ue1,1,1,0,15,-41,-10.47,48.1\n"
        "1000011,1000012030000,ue1,1,1,0,15,-51,-10.46,38.1\n")
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
            "model_type": "AWGN", "model_name": "rfsimu_channel_enB0",
            "model_index": 0,
        }],
        "transitions": [{
            "target": "ue1", "direction": "dl",
            "parameter": "noise_power_dB", "observed": -20,
            "model_type": "AWGN", "model_name": "rfsimu_channel_enB0",
            "model_index": 0,
            "applied_epoch": 1_000_010.2, "verified": True,
            "status": "verified",
        }],
    }))
    return run


def sample_joint_channel_run(tmp_path, *, observed_ploss=-3):
    run = sample_run(tmp_path)
    (run / schema.CHANNEL_SCHEDULE).write_text(json.dumps({
        "schema_version": 1,
        "enabled": True,
        "expected_model_type": "AWGN",
        "events": [
            {
                "at_s": 0,
                "target": "ue1",
                "direction": "dl",
                "parameter": "ploss",
                "value": -3,
            },
            {
                "at_s": 0,
                "target": "ue1",
                "direction": "dl",
                "parameter": "noise_power_dB",
                "value": -2,
            },
        ],
    }))
    model = {
        "target": "ue1",
        "direction": "dl",
        "model_type": "AWGN",
        "model_name": "rfsimu_channel_enB0",
        "model_index": 0,
    }
    (run / schema.LOGS_DIR / "channel_state.json").write_text(json.dumps({
        "success": True,
        "traffic_start_reference_epoch": 1_000_009.0,
        "initial_state": [
            {
                **model,
                "parameter": "ploss",
                "observed": observed_ploss,
            },
            {
                **model,
                "parameter": "noise_power_dB",
                "observed": -2,
            },
        ],
        "transitions": [],
    }))
    return run


def test_training_frame_joins_radio_traffic_and_verified_channel(tmp_path):
    frame = dataset.training_frame(sample_run(tmp_path))
    row = frame[frame["utc_second"] == 1_000_011].iloc[0]
    assert row["execution_id"] == "mgen-20260722-120000"
    assert row["ue_class"] == "heavy"
    assert row["dl_prb"] == 10
    assert row["dl_mbps"] == 0.008
    assert row["ss_rsrp_dbm"] == -51
    assert row["dl_noise_power_dB"] == -20
    assert bool(row["channel_verified"])
    partial = frame[frame["utc_second"] == 1_000_010].iloc[0]
    assert bool(partial["channel_transition_partial"])
    assert not bool(partial["channel_verified"])


def test_archive_is_immutable_and_export_splits_by_execution(tmp_path):
    run = sample_run(tmp_path)
    first = dataset.archive_execution(run, include_raw=True)
    second = dataset.archive_execution(run)
    assert first == second
    assert dataset.verify_checksums(first) > 0
    assert all((first / name).exists() for name in schema.V2_TABLES)
    assert (first / "raw_mgen_logs.tar.gz").exists()
    before = (first / "SHA256SUMS.json").read_bytes()
    dataset.archive_execution(run, include_raw=False)
    assert (first / "SHA256SUMS.json").read_bytes() == before
    metadata = json.loads((first / schema.EXECUTION_METADATA).read_text())
    assert metadata["schema_version"] == 2
    assert metadata["include_raw_logs"] is True
    assert metadata["reconstructable_from_archive"] is True
    assert metadata["table_rows"][schema.PACKET_OUTCOMES] == 2
    assert metadata["quality"]["channel_state_verified"] is True
    assert metadata["quality"]["radio_clock_valid"] is True
    assert metadata["quality"]["radio_clock"] == "dual"
    assert metadata["quality"]["radio_join_clock"] == "core_receipt_utc"
    assert metadata["quality"]["radio_clock_lag_warning"] is True
    assert metadata["quality"]["radio_clock_lag_s_p95"] > 0
    records = dataset.list_executions(tmp_path)
    dataset.update_annotations(
        records[0], include=True, tags="calibration, awgn", notes="clean run")
    target = dataset.export(
        records, tmp_path / "datasets" / "example", include_csv=True)
    packets = pd.read_parquet(target / schema.PACKET_OUTCOMES)
    assert packets["split"].nunique() == 1
    assert set(packets["execution_id"]) == {"mgen-20260722-120000"}
    assert (target / "packet_outcomes.csv").exists()
    assert (target / schema.MODEL_CONTRACT).exists()
    assert dataset.verify_checksums(target) > 0
    exported = json.loads((target / schema.DATASET_MANIFEST).read_text())
    assert exported["archive_checksums_verified"] is True
    assert exported["executions"][0]["annotations"]["tags"] == [
        "calibration", "awgn"]


def test_v2_packet_accounting_keys_segments_and_exact_percentile(tmp_path):
    tables = dataset_v2.build_tables(sample_run(tmp_path))
    packets = tables[schema.PACKET_OUTCOMES]
    seconds = tables[schema.UE_APP_SECOND_OBSERVED]
    segments = tables[schema.CHANNEL_SEGMENTS]
    training = tables[schema.SEGMENT_TRAINING_TABLE]

    assert len(packets) == 2
    assert packets["packet_id"].is_unique
    assert int(packets["received"].sum()) + int(packets["lost"].sum()) == 2
    second_key = [
        "execution_id", "ue", "app", "direction", "utc_second",
    ]
    assert not seconds.duplicated(second_key).any()
    assert {"nb_id", "rnti"}.issubset(packets.columns)
    assert {"nb_id", "rnti", "model_mapping_valid"}.issubset(segments.columns)

    ue1_dl = segments[(segments["ue"] == "ue1") &
                      (segments["direction"] == "dl")].sort_values(
                          "segment_start_utc")
    assert len(ue1_dl) == 2
    assert ue1_dl.iloc[0]["segment_end_utc"] == pytest.approx(
        ue1_dl.iloc[1]["segment_start_utc"])
    assert bool(ue1_dl["model_mapping_valid"].all())
    ul = segments[segments["direction"] == "ul"].iloc[0]
    assert not bool(ul["controlled"])
    assert not bool(ul["training_eligible"])

    treated = training[(training["direction"] == "dl") &
                       (training["requested_value"] == -20)].sort_values(
                           "segment_start_utc").iloc[-1]
    expected = packets.loc[
        (packets["sent_time_utc"] >= treated["segment_start_utc"]) &
        (packets["sent_time_utc"] < treated["segment_end_utc"]) &
        packets["received"], "latency_ms"].quantile(.95)
    assert treated["latency_ms_p95"] == pytest.approx(expected)
    assert treated["ue_radio_samples"] > 0
    assert treated["ss_rsrp_dbm_segment_mean"] == pytest.approx(-51)
    assert bool(treated["ue_radio_clock_valid"])
    assert treated["ue_radio_emit_lag_s_p95"] == pytest.approx(.03)
    radio_row = training[training["radio_join_clock"].notna()].iloc[0]
    assert radio_row["radio_join_clock"] == "core_receipt_utc"
    unmatched = dataset_v2.enrich_radio_clock_provenance(
        training.iloc[[0]],
        pd.DataFrame({"ue": ["other"], "receipt_utc_second": [0]}))
    assert unmatched["radio_join_clock"].isna().all()
    assert bool(radio_row["radio_clock_lag_warning"])
    assert radio_row["radio_clock_lag_s_segment_p95"] > 0

    contract = dataset_v2.model_contract()
    assert contract["radio_clock_policy"]["cross_system_join_clock"] == (
        "core receipt UTC")
    assert "radio_clock_lag_warning" in contract["roles"]["quality_only"]
    assert "radio segment means use core receipt time" in " ".join(
        contract["rules"])


def test_joint_channel_controls_share_one_training_segment(tmp_path):
    tables = dataset_v2.build_tables(sample_joint_channel_run(tmp_path))
    segments = tables[schema.CHANNEL_SEGMENTS]
    training = tables[schema.SEGMENT_TRAINING_TABLE]
    segment = segments[
        segments["ue"].eq("ue1") & segments["direction"].eq("dl")
    ].iloc[0]
    trained = training[training["segment_id"].eq(segment["segment_id"])].iloc[0]

    assert segment["parameter"] == "joint"
    assert int(segment["control_count"]) == 2
    assert segment["requested_channel_state"] == (
        '{"noise_power_dB":-2,"ploss":-3}'
    )
    assert segment["applied_channel_state"] == (
        '{"noise_power_dB":-2,"ploss":-3}'
    )
    assert segment["requested_ploss"] == -3
    assert segment["applied_ploss"] == -3
    assert segment["requested_noise_power_dB"] == -2
    assert segment["applied_noise_power_dB"] == -2
    assert bool(segment["ploss_verified"])
    assert bool(segment["ploss_agreement"])
    assert bool(segment["noise_power_dB_verified"])
    assert bool(segment["noise_power_dB_agreement"])
    assert bool(segment["channel_agreement"])
    assert bool(segment["training_eligible"])
    assert bool(trained["training_eligible"])

    contract = dataset_v2.model_contract()
    assert "requested_ploss" in contract["roles"]["pre_run_features"]
    assert "requested_noise_power_dB" in contract["roles"]["pre_run_features"]


def test_joint_channel_mismatch_rejects_the_complete_segment(tmp_path):
    segments = dataset_v2.channel_segments(
        sample_joint_channel_run(tmp_path, observed_ploss=-2))
    segment = segments[
        segments["ue"].eq("ue1") & segments["direction"].eq("dl")
    ].iloc[0]

    assert segment["requested_ploss"] == -3
    assert segment["applied_ploss"] == -2
    assert not bool(segment["ploss_agreement"])
    assert bool(segment["noise_power_dB_agreement"])
    assert not bool(segment["channel_agreement"])
    assert not bool(segment["training_eligible"])


def test_uncontrolled_run_uses_sender_start_for_channel_segments(tmp_path):
    run = sample_run(tmp_path)
    (run / schema.LOGS_DIR / "channel_state.json").unlink()
    (run / schema.CHANNEL_SCHEDULE).write_text(json.dumps({
        "schema_version": 1,
        "enabled": False,
        "expected_model_type": "AWGN",
        "events": [],
    }))

    segments = dataset_v2.channel_segments(run)

    assert len(segments) == 2
    assert set(segments["segment_start_utc"]) == {1_000_009.0}
    assert not segments["controlled"].any()
    assert not segments["training_eligible"].any()

    archive = dataset.archive_execution(run)
    frozen = pd.read_parquet(archive / schema.CHANNEL_SEGMENTS)
    rebuilt = dataset_v2.channel_segments(archive)
    pd.testing.assert_frame_equal(
        frozen.reset_index(drop=True), rebuilt.reset_index(drop=True),
        check_dtype=False)


def test_v2_archive_reconstructs_all_tables_from_frozen_inputs(tmp_path):
    run = sample_run(tmp_path)
    archive = dataset.archive_execution(run)
    assert (archive / schema.LOGS_DIR / "dn_dl_tx.log").exists()
    assert (archive / schema.SCRIPTS_DIR / "flow_batch_map.csv").exists()

    rebuilt = dataset_v2.build_tables(archive)
    for name, frame in rebuilt.items():
        frozen = pd.read_parquet(archive / name)
        pd.testing.assert_frame_equal(
            frozen.reset_index(drop=True), frame.reset_index(drop=True),
            check_dtype=False)


def test_v2_export_rejects_schema_v1_archive(tmp_path):
    archive = dataset.archive_execution(sample_run(tmp_path))
    metadata_path = archive / schema.EXECUTION_METADATA
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = 1
    metadata_path.write_text(json.dumps(metadata))
    dataset._write_checksums(archive)

    with pytest.raises(ValueError, match="V2 export accepts only"):
        dataset.export(dataset.list_executions(tmp_path), tmp_path / "v1-export")


def test_v2_export_adds_radio_lag_fields_to_older_immutable_archive(tmp_path):
    archive = dataset.archive_execution(sample_run(tmp_path))
    target = archive / schema.SEGMENT_TRAINING_TABLE
    old = pd.read_parquet(target).drop(columns=[
        "radio_join_clock", "radio_clock_lag_samples",
        "radio_clock_lag_s_segment_mean", "radio_clock_lag_s_segment_p95",
        "radio_clock_lag_s_segment_max", "radio_clock_lag_warning",
    ])
    old.to_parquet(target, index=False)
    dataset._write_checksums(archive)

    exported = dataset.export(
        dataset.list_executions(tmp_path), tmp_path / "lag-enriched")
    training = pd.read_parquet(exported / schema.SEGMENT_TRAINING_TABLE)

    assert "radio_clock_lag_s_segment_p95" in training
    assert training["radio_join_clock"].eq("core_receipt_utc").any()
    assert training["radio_clock_lag_warning"].fillna(False).any()


def test_export_rejects_tampered_archive_without_partial_output(tmp_path):
    run = sample_run(tmp_path)
    archive = dataset.archive_execution(run)
    features = archive / schema.UE_SECOND_FEATURES
    features.write_bytes(features.read_bytes() + b"tampered")

    target = tmp_path / "datasets" / "tampered"
    with pytest.raises(ValueError, match="archive checksum mismatch"):
        dataset.export(dataset.list_executions(tmp_path), target)

    assert not target.exists()
    assert not target.with_name(f".{target.name}.tmp").exists()


def test_export_rejects_unexpected_archive_file(tmp_path):
    run = sample_run(tmp_path)
    archive = dataset.archive_execution(run)
    (archive / "untracked.txt").write_text("not checksummed")

    with pytest.raises(ValueError, match="unexpected"):
        dataset.export(
            dataset.list_executions(tmp_path),
            tmp_path / "datasets" / "untracked")


def test_nested_checksum_control_names_are_not_excluded(tmp_path):
    archive = dataset.archive_execution(sample_run(tmp_path))
    nested = archive / "logs" / "annotations.json"
    nested.write_text("nested measurement metadata")

    dataset._write_checksums(archive)

    manifest = json.loads((archive / "SHA256SUMS.json").read_text())
    assert "logs/annotations.json" in manifest
    assert dataset.verify_checksums(archive) == len(manifest)
