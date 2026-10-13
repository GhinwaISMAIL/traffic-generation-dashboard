import copy
from pathlib import Path

import pandas as pd
import pytest
import yaml

from twindash import scenario, schedule
from test_schedule_view import POPULATION, dashboard_with_schedule


def artifacts(root, app="whatsapp"):
    for direction in ("downlink", "uplink"):
        target = root / "artifacts" / app / direction / "run_20260101" / "data"
        target.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"synthetic_flow_id": 1, "synthetic_burst_idx": 0,
                       "on_dur_s": 0.5, "off_dur_s": 0.1, "bytes": 1000,
                       "packets": 10}]).to_parquet(target / "synth_test_bursts_markov.parquet")


def test_write_measurement_config_and_switch_back(tmp_path, monkeypatch):
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch)
    artifacts(tmp_path)
    original = copy.deepcopy(scenario.DEFAULTS)
    original["network"]["dn_ip"] = "192.0.2.10"
    (tmp_path / "scenario_config.yaml").write_text(yaml.safe_dump(original))
    dashboard.button(key="write_measurement_scenario").click().run()
    assert not dashboard.exception
    assert not dashboard.error
    cfg = yaml.safe_load((tmp_path / "scenario_config.yaml").read_text())
    assert cfg["scenario_source"] == "measurement_driven"
    assert cfg["apps"] == ["whatsapp"]
    assert cfg["network"]["dn_ip"] == "192.0.2.10"
    assert "user_classes" not in cfg
    assert cfg["measurement_schedule"]["ue_mapping"]["mobile_ue3"] == "ue3"
    assert (tmp_path / cfg["measurement_schedule"]["path"]).is_file()
    assert yaml.safe_load((tmp_path / "scenario_config.yaml.bak").read_text()) == original
    first_output = cfg["measurement_schedule"]["output_run"]
    dashboard.number_input(key="measurement_generation_seed").set_value(99).run()
    dashboard.button(key="write_measurement_scenario").click().run()
    updated = yaml.safe_load((tmp_path / "scenario_config.yaml").read_text())
    assert updated["measurement_schedule"]["output_run"] != first_output
    dashboard.radio(key="scenario_source").set_value("Profile-based generation").run()
    assert not dashboard.exception
    preview = next(item.value for item in dashboard.code if "profile_based" in item.value)
    assert "measurement_schedule:" not in preview


def test_missing_artifact_preserves_current_scenario(tmp_path, monkeypatch):
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch)
    target = tmp_path / "scenario_config.yaml"
    target.write_text("simulation:\n  duration: 600\n")
    original = target.read_bytes()
    dashboard.button(key="write_measurement_scenario").click().run()
    assert not dashboard.exception
    assert "prepare the model first" in dashboard.error[0].value
    assert target.read_bytes() == original


def test_config_pins_artifacts_and_retains_frozen_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("TWINDASH_PROFILES", str(tmp_path / "traffic_profiles"))
    artifacts(tmp_path)
    raw = b"session_id,experimental_ue,start_s,end_s,traffic_model,source_category,scenario_role\n0001,mobile_ue1,10,20,whatsapp,chat,baseline\n"
    value = schedule.load(raw, expected_ues=POPULATION)
    run = schedule.import_run(value, tmp_path / "traffic_profiles", available_models=["whatsapp"])
    cfg = scenario.measurement_config(run)
    path = tmp_path / cfg["measurement_schedule"]["artifacts"]["whatsapp"]["downlink"]["path"]
    data = pd.read_parquet(path)
    data["bytes"] *= 2
    data.to_parquet(path)
    changed = scenario.measurement_config(run)
    assert cfg["measurement_schedule"]["output_run"] != changed["measurement_schedule"]["output_run"]
    assert schedule.load_frozen(run).sessions.session_id.tolist() == ["0001"]
    with pytest.raises(ValueError, match="random_seed"):
        scenario.measurement_config(run, random_seed=-1)


def test_invalid_timing_preserves_current_scenario(tmp_path, monkeypatch):
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch)
    artifacts(tmp_path)
    target = tmp_path / "scenario_config.yaml"
    target.write_text("simulation:\n  duration: 600\n")
    original = target.read_bytes()
    path = next((tmp_path / "artifacts" / "whatsapp" / "downlink").rglob("*.parquet"))
    data = pd.read_parquet(path)
    data["on_dur_s"] = 0.0
    data.to_parquet(path)
    dashboard.button(key="write_measurement_scenario").click().run()
    assert not dashboard.exception
    assert "invalid burst timing" in dashboard.error[0].value
    assert target.read_bytes() == original
