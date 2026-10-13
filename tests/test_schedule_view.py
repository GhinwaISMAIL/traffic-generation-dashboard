from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest
import yaml

from twindash import runs, schedule, testbed_cfg


POPULATION = [f"mobile_ue{index}" for index in range(1, 7)] + [
    f"static_ue{index}" for index in range(1, 7)]


def dashboard_with_schedule(tmp_path, monkeypatch, model="whatsapp"):
    monkeypatch.setenv("TWINDASH_PROFILES", str(tmp_path / "traffic_profiles"))
    for direction in ("downlink", "uplink"):
        (tmp_path / "artifacts" / "whatsapp" / direction).mkdir(parents=True)
    path = tmp_path / "measured_day.csv"
    pd.DataFrame([
        {"session_id": "s1", "experimental_ue": "mobile_ue3", "start_s": 64810,
         "end_s": 65150, "traffic_model": model, "source_category": "Social",
         "scenario_role": "baseline"},
        {"session_id": "s2", "experimental_ue": "mobile_ue3", "start_s": 64820,
         "end_s": 65160, "traffic_model": model, "source_category": "Social",
         "scenario_role": "augmented"},
    ]).to_csv(path, index=False)
    app = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    dashboard = AppTest.from_file(str(app), default_timeout=10).run()
    dashboard.radio(key="scenario_source").set_value("Measurement-driven schedule").run()
    dashboard.text_area(key="schedule_ue_population").set_value("\n".join(POPULATION)).run()
    dashboard.radio(key="schedule_input_method").set_value("Local CSV file").run()
    dashboard.text_input(key="schedule_csv_path").set_value(str(path)).run()
    return dashboard, path


def test_schedule_mode_import_save_and_return_to_profiles(tmp_path, monkeypatch):
    profile = tmp_path / "scenario_config.yaml"
    profile.write_text("simulation:\n  duration: 600\n")
    original = profile.read_bytes()
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch)
    assert not dashboard.exception
    assert not dashboard.error
    metrics = {metric.label: metric.value for metric in dashboard.metric}
    assert metrics == {
        "Logical duration": "24 h", "Experimental UEs": "12", "Scheduled UEs": "1",
        "Total sessions": "2", "Baseline sessions": "1", "Augmented sessions": "1",
        "Maximum concurrent UEs": "1", "Peak simultaneous sessions / UE": "2",
    }
    assert any(message.value == "Schedule valid" for message in dashboard.success)
    assert not (tmp_path / "traffic_profiles").exists()
    dashboard.button(key="import_measurement_schedule").click().run()
    assert not dashboard.exception
    assert not dashboard.error
    saved = list((tmp_path / "traffic_profiles").glob("run_*/scenario_schedule.csv"))
    assert len(saved) == 1
    assert schedule.load_frozen(saved[0].parent).sessions.iloc[1]["scenario_role"] == "augmented"
    assert profile.read_bytes() == original
    assert runs.list_runs(tmp_path / "traffic_profiles") == []
    dashboard.button(key="import_measurement_schedule").click().run()
    assert len(list((tmp_path / "traffic_profiles").glob("run_*"))) == 1
    dashboard.radio(key="scenario_source").set_value("Profile-based generation").run()
    assert not dashboard.exception
    assert any(button.label == "Write scenario_config.yaml" for button in dashboard.button)
    assert profile.read_bytes() == original


def test_missing_model_is_blocked_until_explicitly_mapped(tmp_path, monkeypatch):
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch, model="source_chat")
    assert not dashboard.exception
    assert "not discovered" in dashboard.error[0].value
    assert not any(button.label == "Import measurement-driven schedule" for button in dashboard.button)
    model = next(widget for widget in dashboard.selectbox if widget.label == "source_chat")
    model.set_value("whatsapp").run()
    assert not dashboard.exception
    assert not dashboard.error
    dashboard.button(key="import_measurement_schedule").click().run()
    saved = next((tmp_path / "traffic_profiles").glob("run_*"))
    row = schedule.load_frozen(saved).sessions.iloc[0]
    assert row["traffic_model"] == "whatsapp"
    assert row["source_traffic_model"] == "source_chat"


def test_invalid_replacement_schedule_cannot_save_previous_preview(tmp_path, monkeypatch):
    dashboard, source = dashboard_with_schedule(tmp_path, monkeypatch)
    source.write_text("invalid,columns\n1,2\n")
    dashboard.run()
    assert not dashboard.exception
    assert "missing schedule columns" in dashboard.error[0].value
    assert not any(button.label == "Import measurement-driven schedule" for button in dashboard.button)


def test_ue_membership_uses_the_independent_population(tmp_path, monkeypatch):
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch)
    wrong = [value.replace("mobile_ue3", "mobile_ue13") for value in POPULATION]
    dashboard.text_area(key="schedule_ue_population").set_value("\n".join(wrong)).run()
    assert not dashboard.exception
    assert "outside the experimental population: mobile_ue3" in dashboard.error[0].value
    assert not any(message.value == "Schedule valid" for message in dashboard.success)
    assert not any(button.label == "Import measurement-driven schedule" for button in dashboard.button)
    dashboard.text_area(key="schedule_ue_population").set_value("").run()
    assert "provide the experimental UE population" in dashboard.error[0].value
    assert not (tmp_path / "traffic_profiles").exists()


def test_preview_limits_rows_but_import_keeps_all_sessions(tmp_path, monkeypatch):
    dashboard, source = dashboard_with_schedule(tmp_path, monkeypatch)
    frame = pd.read_csv(source)
    expanded = pd.concat([frame.assign(session_id=[f"{i}_1", f"{i}_2"])
                          for i in range(15)], ignore_index=True)
    expanded.to_csv(source, index=False)
    dashboard.run()
    assert not dashboard.exception
    assert not dashboard.error
    assert len(dashboard.dataframe[0].value) == 20
    dashboard.button(key="import_measurement_schedule").click().run()
    saved = next((tmp_path / "traffic_profiles").glob("run_*"))
    assert len(schedule.load_frozen(saved).sessions) == 30


def test_existing_pages_ignore_schedule_only_runs(tmp_path, monkeypatch):
    config = testbed_cfg.build_ric5g(
        username="tester", n_ue=4, core_host="core.example.test",
        cell_hosts=["cell1.example.test"], ues_per_cell=4)
    (tmp_path / "testbed_config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    dashboard, _ = dashboard_with_schedule(tmp_path, monkeypatch)
    dashboard.button(key="import_measurement_schedule").click().run()
    for page in ("Results", "Dataset", "Testbed"):
        dashboard.sidebar.radio[0].set_value(page).run()
        assert not dashboard.exception
        assert not dashboard.error
