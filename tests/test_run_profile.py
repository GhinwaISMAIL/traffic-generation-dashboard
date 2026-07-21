import json

from twindash import run_profile, testbed, testbed_cfg


def ric5g_config(xapp=True):
    return testbed_cfg.build_ric5g(
        username="ghinwa", n_ue=24,
        core_host="pc798.emulab.net",
        cell_hosts=["pc05-fort.emulab.net", "pc11-fort.emulab.net"],
        xapp_enabled=xapp,
        flags={"allow_placeholder_hosts": False})


def test_ric5g_snapshot_declares_channel_and_radio_capabilities(tmp_path):
    run = tmp_path / "run_ric5g"
    run.mkdir()
    saved = run_profile.record(run, ric5g_config())

    assert (run / "run_profile.json").is_file()
    assert saved["testbed"] == run_profile.RIC5G
    assert saved["topology"] == {"cells": 2, "ues": 24}
    assert saved["capabilities"]["channel_model"] is True
    assert saved["capabilities"]["prb"] is True
    assert run_profile.load(run)["xapp"]["expected_subscriptions"] == 8


def test_xapp_disabled_removes_prb_but_keeps_channel_model(tmp_path):
    run = tmp_path / "run_no_xapp"
    run.mkdir()
    profile = run_profile.record(run, ric5g_config(xapp=False))

    assert profile["capabilities"]["channel_model"] is True
    assert profile["capabilities"]["ric"] is True
    assert profile["capabilities"]["xapp"] is False
    assert profile["capabilities"]["prb"] is False


def test_rfsim_snapshot_does_not_advertise_ric5g_measurements(tmp_path):
    run = tmp_path / "run_rfsim"
    run.mkdir()
    cfg = {
        "testbed": run_profile.RFSIM,
        "ues": {"boxes": {"ue1": {}, "ue2": {}}},
    }
    profile = run_profile.record(run, cfg)

    assert profile["capabilities"]["flow_kpis"] is True
    assert profile["capabilities"]["channel_model"] is False
    assert profile["capabilities"]["ric"] is False
    assert profile["capabilities"]["prb"] is False


def test_legacy_run_infers_ric5g_only_from_saved_artifacts(tmp_path):
    run = tmp_path / "run_legacy"
    logs = run / "logs"
    logs.mkdir(parents=True)
    (logs / "xapp.log").write_text("Test xApp run SUCCESSFULLY\n")
    (logs / "prb_by_second.csv").write_text("utc_second,nb_id,rnti\n")
    (logs / "run_timing.json").write_text("{}\n")
    (logs / "rnti_map.csv").write_text(
        "ue,cell,nb_id,rnti\nue1,1,3584,10\nue13,2,3585,20\n")

    profile = run_profile.load(run)
    assert profile["inferred"] is True
    assert profile["testbed"] == run_profile.RIC5G
    assert profile["capabilities"]["radio_efficiency"] is True
    assert profile["topology"] == {"cells": 2, "ues": 2}


def test_run_dispatch_records_profile_used_at_that_time(tmp_path, monkeypatch):
    run = tmp_path / "run_dispatch"
    (run / "deployment").mkdir(parents=True)
    cfg = {"testbed": run_profile.RFSIM, "ues": {"boxes": {"ue1": {}}}}
    monkeypatch.setattr(testbed, "run_script", lambda path: "ran")

    assert testbed.run_experiment(run, cfg) == "ran"
    manifest = json.loads((run / "run_profile.json").read_text())
    assert manifest["testbed"] == run_profile.RFSIM

    # Changing the global config later cannot reinterpret this historical run.
    assert run_profile.load(run)["testbed"] == run_profile.RFSIM
