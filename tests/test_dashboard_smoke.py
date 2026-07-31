from pathlib import Path

from streamlit.testing.v1 import AppTest
import yaml

from twindash import testbed_cfg


def test_all_dashboard_pages_render_without_exceptions(monkeypatch, tmp_path):
    profiles = tmp_path / "traffic_profiles"
    profiles.mkdir()
    monkeypatch.setenv("TWINDASH_PROFILES", str(profiles))
    config = testbed_cfg.build_ric5g(
        username="tester",
        n_ue=4,
        core_host="core.example.test",
        cell_hosts=["cell1.example.test"],
        ues_per_cell=4,
    )
    (tmp_path / "testbed_config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False)
    )

    app = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    dashboard = AppTest.from_file(str(app), default_timeout=10)
    dashboard.run()

    assert not dashboard.exception
    assert not dashboard.error
    assert dashboard.title[0].value == "Design a scenario"

    expected = {
        "Testbed": "Testbed configuration",
        "Results": None,
        "Dataset": None,
    }
    for page, title in expected.items():
        dashboard.sidebar.radio[0].set_value(page).run()
        assert not dashboard.exception, page
        assert not dashboard.error, page
        if title:
            assert dashboard.title[0].value == title
