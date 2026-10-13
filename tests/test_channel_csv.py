import io
import json
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from twindash import channel, ric5g
from test_channel_schedule_dashboard import run_and_config


HEADER = "at_s,target,direction,parameter,value\n"


def mapped_run(tmp_path):
    run, cfg = run_and_config(tmp_path)
    (run / "ue_mapping.csv").write_text(
        "experimental_ue,ue_name\nmobile_ue1,ue1\nstatic_ue1,ue2\n")
    return run, cfg


def test_csv_maps_ues_preserves_calibration_and_roundtrips(tmp_path):
    run, cfg = mapped_run(tmp_path)
    source = ("\ufeff" + HEADER +
              "20.125,static_ue1,dl,ploss,0\n"
              "0,mobile_ue1,dl,noise_power_dB,-31.23456789\n"
              "10,cell1,ul,ploss,12.5\n").encode()
    value = channel.load_csv(io.BytesIO(source), run, cfg)
    assert [e["at_s"] for e in value["events"]] == [0, 10, 20.125]
    assert [e["target"] for e in value["events"]] == ["ue1", "cell1", "ue2"]
    assert value["events"][0]["value"] == -31.23456789
    assert value["events"][-1]["value"] == 0
    assert not channel.path(run).exists()
    assert channel.load_csv(channel.to_csv(value).encode(), run, cfg) == value
    channel.save(run, value, cfg)
    assert channel.load(run) == value


@pytest.mark.parametrize("text,error", [
    ("", "missing columns"),
    (HEADER, "at least one transition"),
    ("at_s,target,direction,parameter,value,value\n", "duplicate column"),
    (HEADER.rstrip() + ",notes\n", "unexpected columns"),
    (HEADER + "0,ue1,dl,ploss,10,extra\n", "expected 5 fields"),
    (HEADER + "0,ue1,dl,ploss\n", "expected 5 fields"),
    (HEADER + "0,ue1,dl,ploss,\n", "all five fields"),
    (HEADER + '0,"ue1,dl,ploss,10\n', "invalid channel CSV"),
    (HEADER + "0,ue1,dl,ploss,wrong\n", "must be numeric"),
    (HEADER + "NaN,ue1,dl,ploss,10\n", "at_s must be between"),
    (HEADER + "0,ue1,dl,ploss,inf\n", "finite and numeric"),
    (HEADER + "-1,ue1,dl,ploss,10\n", "at_s must be between"),
    (HEADER + "91,ue1,dl,ploss,10\n", "at_s must be between"),
    (HEADER + "0,unknown,dl,ploss,10\n", "DL target"),
    (HEADER + "0,ue1,ul,ploss,10\n", "UL target"),
    (HEADER + "0,mobile_ue1,ul,ploss,10\n", "UL target"),
    (HEADER + "0,cell99,ul,ploss,10\n", "UL target"),
    (HEADER + "0,ue1,up,ploss,10\n", "direction must be"),
    (HEADER + "0,ue1,dl,sinr,10\n", "unsupported parameter"),
])
def test_bad_csv_never_changes_saved_schedule(tmp_path, text, error):
    run, cfg = mapped_run(tmp_path)
    channel.save(run, channel.empty(), cfg)
    before = channel.path(run).read_bytes()
    with pytest.raises(ValueError, match=error):
        channel.load_csv(text.encode(), run, cfg)
    assert channel.path(run).read_bytes() == before


def test_conflicts_are_found_after_alias_and_group_expansion(tmp_path):
    run, cfg = mapped_run(tmp_path)
    for rows in (
        "0,mobile_ue1,dl,ploss,10\n0,ue1,dl,ploss,11\n",
        "0,all_ues,dl,ploss,10\n0,ue2,dl,ploss,11\n",
    ):
        with pytest.raises(ValueError, match="duplicate transition"):
            channel.load_csv((HEADER + rows).encode(), run, cfg)
    value = channel.load_csv((HEADER + "0,all_ues,dl,ploss,10\n0,all_cells,ul,ploss,11\n").encode(), run, cfg)
    assert {e["target"] for e in value["events"]} == {"ue1", "ue2", "cell1"}


def test_ambiguous_mapping_is_rejected(tmp_path):
    run, cfg = mapped_run(tmp_path)
    (run / "ue_mapping.csv").write_text("experimental_ue,ue_name\nue1,ue2\n")
    with pytest.raises(ValueError, match="ambiguous"):
        channel.load_csv((HEADER + "0,ue1,dl,ploss,10\n").encode(), run, cfg)


def test_large_csv_retains_every_event(tmp_path):
    run, cfg = run_and_config(tmp_path)
    (run / "config.json").write_text(json.dumps({"simulation_duration": 86400}))
    text = HEADER + "".join(
        f"{at_s},ue{ue},dl,noise_power_dB,{-30 - ue / 10}\n"
        for at_s in range(50000) for ue in (1, 2))
    value = channel.load_csv(text.encode(), run, cfg)
    assert len(value["events"]) == 100000
    channel.save(run, value, cfg)
    assert channel.load(run) == value


def test_active_deployment_blocks_schedule_mutation(tmp_path):
    run, cfg = run_and_config(tmp_path)
    channel.save(run, channel.empty(), cfg)
    original = channel.path(run).read_bytes()
    with ric5g._deployment_lock(run / "logs"):
        assert channel.deployment_active(run)
        value = channel.load_csv((HEADER + "0,ue1,dl,ploss,10\n").encode(), run, cfg)
        with pytest.raises(ValueError, match="being deployed"):
            channel.save(run, value, cfg)
        assert channel.path(run).read_bytes() == original
    assert not channel.deployment_active(run)


def panel(tmp_path):
    run, cfg = mapped_run(tmp_path)
    app = tmp_path / "channel_panel.py"
    app.write_text(
        "from pathlib import Path\nfrom dashboard.channel_view import render\n"
        f"render(Path({str(run)!r}), {cfg!r})\n")
    return AppTest.from_file(str(app), default_timeout=10).run(), run, cfg


def test_uploaded_csv_preview_save_disable_and_invalid_replacement(tmp_path, monkeypatch):
    uploaded = {"data": (HEADER + "".join(
        f"{i},mobile_ue1,dl,ploss,{i / 10}\n" for i in range(30))).encode()}
    monkeypatch.setattr(st, "file_uploader", lambda *a, **kw: io.BytesIO(uploaded["data"]))
    app, run, cfg = panel(tmp_path)
    app.radio(key=f"channel_input_{run.name}").set_value("Upload CSV").run()
    assert not app.exception and not app.error
    assert any(s.value == "Channel CSV valid" for s in app.success)
    assert app.button(key=f"channel_save_{run.name}").disabled
    assert not channel.path(run).exists()
    preview = next(d.value for d in app.dataframe if "at_s" in d.value.columns)
    assert len(preview) == 20
    app.toggle(key=f"channel_enabled_{run.name}").set_value(True).run()
    app.button(key=f"channel_save_{run.name}").click().run()
    assert not app.exception and not app.error
    saved = channel.load(run)
    assert saved["enabled"] and len(saved["events"]) == 30
    before = channel.path(run).read_bytes()
    uploaded["data"] = (HEADER + "0,ue1,ul,ploss,10\n").encode()
    app.run()
    assert not app.exception and app.error
    assert app.button(key=f"channel_save_{run.name}").disabled
    assert channel.path(run).read_bytes() == before
    app.button(key=f"channel_disable_{run.name}").click().run()
    assert not app.exception
    assert not channel.load(run)["enabled"]
    assert not app.toggle(key=f"channel_enabled_{run.name}").value


def test_local_csv_input_and_active_run_preview(tmp_path):
    app, run, cfg = panel(tmp_path)
    path = tmp_path / "channel.csv"
    path.write_text(HEADER + "0,static_ue1,dl,noise_power_dB,-31\n")
    app.radio(key=f"channel_input_{run.name}").set_value("Local CSV file").run()
    app.text_input(key=f"channel_csv_path_{run.name}").set_value(str(path)).run()
    app.toggle(key=f"channel_enabled_{run.name}").set_value(True).run()
    (run / "logs").mkdir(exist_ok=True)
    with ric5g._deployment_lock(run / "logs"):
        app.run()
        assert not app.exception and not app.error
        assert app.button(key=f"channel_save_{run.name}").disabled
        assert app.button(key=f"channel_disable_{run.name}").disabled
    app.run()
    assert not app.button(key=f"channel_save_{run.name}").disabled


def test_manual_table_remains_available_and_large_saved_schedule_is_not_truncated(tmp_path):
    app, run, cfg = panel(tmp_path)
    assert not app.exception
    assert app.radio(key=f"channel_input_{run.name}").value == "Manual table"
    assert not app.button(key=f"channel_save_{run.name}").disabled
    app.button(key=f"channel_save_{run.name}").click().run()
    assert channel.load(run) == channel.empty()
    value = channel.load_csv((HEADER + "".join(
        f"{i / 100},ue1,dl,ploss,10\n" for i in range(2100))).encode(), run, cfg)
    channel.save(run, value, cfg)
    app.run()
    app.toggle(key=f"channel_enabled_{run.name}").set_value(True).run()
    app.button(key=f"channel_save_{run.name}").click().run()
    assert not app.exception and not app.error
    assert len(channel.load(run)["events"]) == 2100
