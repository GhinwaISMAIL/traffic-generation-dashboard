from copy import deepcopy

from twindash import scenario


def cell_config():
    cfg = deepcopy(scenario.DEFAULTS)
    cfg["simulation"].update({"num_cells": 2, "ues_per_cell": 3, "n_ue": 6})
    cfg["apps"] = ["youtube", "telegram"]
    cfg["cells"] = [
        {
            "cell": 1,
            "n_ue": 3,
            "profiles": [{
                "name": "video", "count": 3, "base": "heavy", "flows": 20,
                "app_mix": {"youtube": 1},
            }],
        },
        {
            "cell": 2,
            "n_ue": 3,
            "distribution": {"heavy": 0, "medium": 1, "light": 2},
        },
    ]
    return cfg


def test_two_cells_can_have_different_ue_specs():
    cfg = cell_config()
    assert scenario.validate(cfg) == []
    assert scenario.cell_specs(cfg)[0]["profiles"][0]["name"] == "video"
    assert scenario.cell_specs(cfg)[1]["distribution"]["light"] == 2


def test_each_cell_is_validated_independently():
    cfg = cell_config()
    cfg["cells"][1]["distribution"]["light"] = 1
    errors = scenario.validate(cfg)
    assert any("cell 2 user-class counts sum to 2" in error for error in errors)


def test_total_ue_count_must_match_cell_layout():
    cfg = cell_config()
    cfg["simulation"]["n_ue"] = 5
    errors = scenario.validate(cfg)
    assert any("num_cells x ues_per_cell" in error for error in errors)


def test_legacy_scenario_is_one_logical_cell():
    cfg = deepcopy(scenario.DEFAULTS)
    cfg["apps"] = ["youtube"]
    cfg.pop("cells", None)
    assert scenario.cell_specs(cfg)[0]["cell"] == 1
    assert scenario.validate(cfg) == []
