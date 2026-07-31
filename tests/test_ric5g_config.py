import json

import pytest

from twindash import ric5g, testbed_cfg


def config(n_ue=24, n_cells=2):
    available = ["pc05-fort.emulab.net", "pc11-fort.emulab.net",
                 "pc12-fort.emulab.net"]
    return testbed_cfg.build_ric5g(
        username="ghinwa", n_ue=n_ue,
        core_host="pc798.emulab.net",
        cell_hosts=available[:n_cells],
        flags={"allow_placeholder_hosts": False})


def test_global_ue_names_map_to_cell_local_containers():
    cfg = config()
    boxes = cfg["ues"]["boxes"]
    assert boxes["ue1"]["container"] == "ric5g-ue-cell1-1"
    assert boxes["ue12"]["container"] == "ric5g-ue-cell1-12"
    assert boxes["ue13"]["container"] == "ric5g-ue-cell2-1"
    assert boxes["ue24"]["container"] == "ric5g-ue-cell2-12"
    assert cfg["xapp"]["expected_subscriptions"] == 8
    assert testbed_cfg.validate(cfg, allow_placeholder=False) == []


def test_capacity_error_is_reported():
    errors = testbed_cfg.validate(config(25), allow_placeholder=False)
    assert any("exceed" in error for error in errors)


def test_runner_environment_comes_from_config():
    cfg = config()
    env = ric5g.environment(cfg)
    assert env["CORE_HOST"] == "ghinwa@pc798.emulab.net"
    assert env["CELL1_HOST"] == "ghinwa@pc05-fort.emulab.net"
    assert env["CELL2_HOST"] == "ghinwa@pc11-fort.emulab.net"
    assert env["NB_ID_START"] == "3584"
    assert env["XAPP_SUBS"] == "8"


def test_three_cells_map_and_export_all_hosts():
    cfg = config(n_ue=36, n_cells=3)
    boxes = cfg["ues"]["boxes"]
    assert boxes["ue25"]["container"] == "ric5g-ue-cell3-1"
    assert boxes["ue36"]["container"] == "ric5g-ue-cell3-12"
    assert cfg["ric"]["expected_e2_nodes"] == 3
    assert cfg["xapp"]["expected_subscriptions"] == 12
    env = ric5g.environment(cfg)
    assert env["NUM_CELLS"] == "3"
    assert env["CELL3_HOST"] == "ghinwa@pc12-fort.emulab.net"
    assert env["XAPP_SUBS"] == "12"
    assert testbed_cfg.validate(cfg, allow_placeholder=False) == []


def test_one_cell_is_supported():
    cfg = config(n_ue=12, n_cells=1)
    assert ric5g.environment(cfg)["NUM_CELLS"] == "1"
    assert cfg["xapp"]["expected_subscriptions"] == 4
    assert testbed_cfg.validate(cfg, allow_placeholder=False) == []


def test_duplicate_cell_host_is_rejected():
    cfg = config()
    cfg["nodes"]["cells"][1]["ssh_host"] = cfg["nodes"]["cells"][0]["ssh_host"]
    errors = testbed_cfg.validate(cfg, allow_placeholder=False)
    assert any("must be distinct" in error for error in errors)


def test_nested_duration_is_supported(tmp_path):
    run = tmp_path / "run_nested"
    run.mkdir()
    (run / "config.json").write_text(json.dumps({"simulation": {"duration": 321}}))
    assert ric5g.duration_s(run) == 321


def test_local_contract_rejects_mismatched_ue_scripts(tmp_path):
    run = tmp_path / "run_bad_scripts"
    scripts = run / "mgen_scripts"
    scripts.mkdir(parents=True)
    for name in ("dn_dl_tx.mgn", "dn_ul_rx.mgn", "flow_batch_map.csv",
                 "ue1_ul_tx.mgn", "ue2_dl_rx.mgn"):
        (scripts / name).write_text("")

    cfg = config(n_ue=2)
    cfg["runner"]["script"] = __file__
    errors = ric5g.validate_local(run, cfg)
    assert any("sender/receiver scripts do not match" in error
               for error in errors)


def test_local_contract_rejects_xapp_window_after_traffic(tmp_path):
    run = tmp_path / "run_short"
    scripts = run / "mgen_scripts"
    scripts.mkdir(parents=True)
    (run / "config.json").write_text(json.dumps({"simulation_duration": 60}))
    for name in ("dn_dl_tx.mgn", "dn_ul_rx.mgn", "flow_batch_map.csv",
                 "ue1_ul_tx.mgn", "ue1_dl_rx.mgn"):
        (scripts / name).write_text("")

    cfg = config(n_ue=1)
    cfg["runner"]["script"] = __file__
    cfg["xapp"].update({"delay_s": 45, "window_s": 30})
    errors = ric5g.validate_local(run, cfg)
    assert any("after the 60s traffic run" in error for error in errors)


def test_same_run_cannot_be_deployed_twice(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()

    with ric5g._deployment_lock(logs):
        with pytest.raises(RuntimeError, match="already being deployed"):
            with ric5g._deployment_lock(logs):
                pass


def test_success_callback_runs_before_deployment_lock_is_released(
        tmp_path, monkeypatch):
    run = tmp_path / "run_locked_archive"
    scripts = run / "mgen_scripts"
    scripts.mkdir(parents=True)
    (run / "config.json").write_text(
        json.dumps({"simulation_duration": 100}))
    for name in ("dn_dl_tx.mgn", "dn_ul_rx.mgn", "flow_batch_map.csv",
                 "ue1_ul_tx.mgn", "ue1_dl_rx.mgn"):
        (scripts / name).write_text("")

    runner = tmp_path / "deploy.sh"
    runner.write_text("#!/usr/bin/env bash\n")
    cfg = config(n_ue=1, n_cells=1)
    cfg["runner"]["script"] = str(runner)
    cfg["xapp"].update({"delay_s": 20, "window_s": 60})

    class FakeProcess:
        def __init__(self):
            self.stdout = iter(["finished\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(
        ric5g.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    callback_calls = []

    def assert_lock_is_still_held():
        callback_calls.append(True)
        with pytest.raises(RuntimeError, match="already being deployed"):
            with ric5g._deployment_lock(run / "logs"):
                pass

    result = ric5g.run(run, cfg, on_success=assert_lock_is_still_held)

    assert result == run / "logs" / "deployment.log"
    assert callback_calls == [True]
    # The callback completed and the outer run then released the lock.
    with ric5g._deployment_lock(run / "logs"):
        pass
