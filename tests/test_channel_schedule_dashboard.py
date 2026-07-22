import json

from twindash import channel, testbed_cfg


def run_and_config(tmp_path):
    run = tmp_path / "run_example"
    scripts = run / "mgen_scripts"
    scripts.mkdir(parents=True)
    (run / "config.json").write_text(json.dumps({"simulation_duration": 90}))
    (scripts / "manifest.csv").write_text(
        "ue_name,ue_ip,ue_class\n"
        "ue1,12.1.1.1,heavy\n"
        "ue2,12.1.1.2,light\n")
    cfg = testbed_cfg.build_ric5g(
        username="tester", n_ue=2, core_host="core.example",
        cell_hosts=["cell.example"], ues_per_cell=2)
    return run, cfg


def test_valid_schedule_is_normalized_and_saved(tmp_path):
    run, cfg = run_and_config(tmp_path)
    schedule = {
        "schema_version": 1,
        "enabled": True,
        "expected_model_type": "AWGN",
        "events": [
            {"at_s": 30, "target": "ue2", "direction": "dl",
             "parameter": "ploss", "value": 12},
            {"at_s": 0, "target": "cell1", "direction": "ul",
             "parameter": "noise_power_dB", "value": -30},
        ],
    }
    assert channel.validate(schedule, run, cfg) == []
    target = channel.save(run, schedule, cfg)
    saved = json.loads(target.read_text())
    assert [row["at_s"] for row in saved["events"]] == [0.0, 30.0]


def test_direction_scope_and_duration_are_enforced(tmp_path):
    run, cfg = run_and_config(tmp_path)
    schedule = {
        "schema_version": 1, "enabled": True,
        "expected_model_type": "AWGN",
        "events": [
            {"at_s": 91, "target": "cell1", "direction": "dl",
             "parameter": "ploss", "value": 10},
        ],
    }
    errors = channel.validate(schedule, run, cfg)
    assert any("between 0 and 90" in error for error in errors)
    assert any("DL target" in error for error in errors)


def test_disabled_schedule_needs_no_events(tmp_path):
    run, cfg = run_and_config(tmp_path)
    assert channel.validate(channel.empty(), run, cfg) == []


def test_group_targets_expand_to_exact_deployment_labels(tmp_path):
    run, cfg = run_and_config(tmp_path)
    expanded = channel.expand_groups({
        "schema_version": 1, "enabled": True,
        "expected_model_type": "AWGN",
        "events": [
            {"at_s": 0, "target": "all_ues", "direction": "dl",
             "parameter": "ploss", "value": 0},
            {"at_s": 10, "target": "all_cells", "direction": "ul",
             "parameter": "noise_power_dB", "value": -25},
        ],
    }, run, cfg)
    assert [row["target"] for row in expanded["events"]] == [
        "ue1", "ue2", "cell1"]
    assert channel.validate(expanded, run, cfg) == []
