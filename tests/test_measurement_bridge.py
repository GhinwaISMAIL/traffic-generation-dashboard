import json
import os
from pathlib import Path
import shutil

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from twindash import bursts, kpis, mgen_script, runs, scenario, schedule, settings
from test_measurement_config import artifacts
from test_schedule_view import POPULATION


@pytest.mark.parametrize("ue_count", [6, 7, 12])
def test_dashboard_configuration_to_notebook_outputs(tmp_path, monkeypatch, ue_count):
    generator = Path(os.environ.get("TWINDASH_GENERATOR_REPO", settings.repo_root()))
    if not (generator / "measurement_generation.py").is_file():
        pytest.skip("Set TWINDASH_GENERATOR_REPO to the updated traffic generator repository")
    builder = "build_scenario_measurement_1.ipynb"
    if not (generator / "notebooks" / builder).is_file():
        builder = "build_scenario_1.ipynb"
    notebooks = (builder, "mgen_script_2.ipynb")
    monkeypatch.setenv("TWINDASH_PROFILES", str(tmp_path / "traffic_profiles"))
    (tmp_path / "data").mkdir()
    (tmp_path / "notebooks").mkdir()
    shutil.copyfile(generator / "measurement_generation.py", tmp_path / "measurement_generation.py")
    for name in notebooks:
        shutil.copyfile(generator / "notebooks" / name, tmp_path / "notebooks" / name)
    for app in ("whatsapp", "googlemaps"):
        artifacts(tmp_path, app)
    frame = pd.DataFrame([
        {"session_id": "0001", "experimental_ue": "mobile_ue3", "start_s": 64810,
         "end_s": 64810.5, "traffic_model": "whatsapp", "source_category": "chat",
         "scenario_role": "baseline", "source_traffic_bytes": "4200000", "source_session_id": ""},
        {"session_id": "aug_1", "experimental_ue": "mobile_ue3", "start_s": 64810,
         "end_s": 64810.5, "traffic_model": "whatsapp", "source_category": "chat",
         "scenario_role": "augmented", "source_traffic_bytes": "4200000", "source_session_id": "0001"},
        {"session_id": "maps", "experimental_ue": "static_ue1", "start_s": 86399,
         "end_s": 86400, "traffic_model": "googlemaps", "source_category": "navigation",
         "scenario_role": "baseline", "source_traffic_bytes": "1000", "source_session_id": ""},
    ])
    population = ["mobile_ue1", "mobile_ue2", "mobile_ue3", "static_ue1"] + [
        f"unused_ue{i}" for i in range(ue_count - 4)]
    value = schedule.load(frame.to_csv(index=False).encode(), expected_ues=population)
    frozen = schedule.import_run(value, tmp_path / "traffic_profiles", available_models=["whatsapp", "googlemaps"])
    cfg = scenario.measurement_config(frozen)
    assert cfg["simulation"]["n_ue"] == ue_count
    scenario.save(cfg)
    monkeypatch.chdir(tmp_path / "notebooks")
    output = tmp_path / cfg["measurement_schedule"]["output_run"]
    for name in notebooks:
        document = json.loads((tmp_path / "notebooks" / name).read_text())
        namespace = {}
        for index, cell in enumerate(document["cells"]):
            if cell["cell_type"] == "code":
                exec(compile("".join(cell["source"]), f"{name}:cell{index}", "exec"), namespace)
        if name == builder:
            assert runs.list_runs(tmp_path / "traffic_profiles") == []
    assert runs.list_runs(tmp_path / "traffic_profiles") == [output]
    assert runs.load_config(output)["n_ue"] == ue_count
    assert len(pd.read_csv(output / "mgen_scripts" / "manifest.csv")) == ue_count
    assert schedule.load_frozen(output).sessions.session_id.tolist() == ["0001", "aug_1", "maps"]
    offered = mgen_script.projected_load_timeseries(
        output, interval_s=900, group_by=("session_id", "ue_name", "scenario_role"))
    report = pd.read_csv(output / "session_generation.csv", dtype={"session_id": str})
    totals = offered.groupby("session_id").offered_bytes.sum()
    assert totals.to_dict() == pytest.approx(report.set_index("session_id").generated_total_bytes.to_dict())
    designed = kpis.build_designed(output)
    assert set(designed.ue) == {"ue3", "ue4"}
    assert set(designed.app) == {"whatsapp", "googlemaps"}
    assert designed.proj_bytes.sum() == pytest.approx(report.generated_total_bytes.sum(), abs=1)
    data = bursts.load_bursts(output)
    assert not bursts.coupling(data).empty
    assert (data.absolute_start_time >= data.start_s).all()
    app = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    ui = AppTest.from_file(str(app), default_timeout=15).run()
    for page in ("Results", "Dataset"):
        ui.sidebar.radio[0].set_value(page).run()
        assert not ui.exception
