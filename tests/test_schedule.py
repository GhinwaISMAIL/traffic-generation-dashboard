import io
import json

import pandas as pd
import pytest

from twindash import bursts, runs, schedule, testbed


POPULATION = [f"mobile_ue{index}" for index in range(1, 7)] + [
    f"static_ue{index}" for index in range(1, 7)]
MODELS = ["googlemaps", "whatsapp"]


def sessions():
    return pd.DataFrame([
        {"session_id": "001", "experimental_ue": "mobile_ue3",
         "start_s": "08:41:20", "end_s": "08:45:02", "traffic_model": "googlemaps",
         "source_category": "Navigation", "baseline_or_augmented": "baseline",
         "source_session_id": "trace_001", "augmentation_iteration": "0"},
        {"session_id": "002", "experimental_ue": "mobile_ue3",
         "start_s": "08:43:05", "end_s": "08:46:50", "traffic_model": "whatsapp",
         "source_category": "Social", "baseline_or_augmented": "augmented",
         "source_session_id": "trace_001", "augmentation_iteration": "1"},
    ])


def imported(frame=None, **kwargs):
    frame = sessions() if frame is None else frame
    return schedule.load(io.StringIO(frame.to_csv(index=False)), **kwargs)


def test_schedule_preserves_overlaps_identity_and_provenance():
    value = imported(available_models=["googlemaps", "whatsapp"])
    assert value.sessions["session_id"].tolist() == ["001", "002"]
    assert value.sessions["start_s"].tolist() == [31280.0, 31385.0]
    assert value.sessions["scenario_role"].tolist() == ["baseline", "augmented"]
    assert value.sessions["source_session_id"].tolist() == ["trace_001", "trace_001"]
    stats = schedule.summary(value)
    assert stats["ues"] == 1
    assert stats["baseline_sessions"] == stats["augmented_sessions"] == 1
    assert stats["peak_sessions_per_ue"] == 2
    assert stats["logical_duration_s"] == 86400


@pytest.mark.parametrize(("column", "value", "message"), [
    ("session_id", "001", "must be unique"),
    ("experimental_ue", "", "must not be empty"),
    ("start_s", "NaN", "finite"),
    ("start_s", "-1", "nonnegative"),
    ("start_s", "08:65:00", "HH:MM:SS"),
    ("end_s", "inf", "finite"),
    ("end_s", "08:43:05", "greater than"),
    ("end_s", "24:00:01", "beyond"),
    ("baseline_or_augmented", "extra", "baseline or augmented"),
    ("traffic_model", "WhatsApp", "lowercase"),
    ("source_category", "", "must not be empty"),
    ("augmentation_iteration", "0.5", "nonnegative integer"),
])
def test_invalid_sessions_are_rejected(column, value, message):
    frame = sessions()
    frame.loc[1, column] = value
    with pytest.raises(ValueError, match=message):
        imported(frame)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_logical_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        imported(logical_duration_s=duration)


def test_role_aliases_cannot_disagree():
    frame = sessions()
    frame["scenario_role"] = "baseline"
    with pytest.raises(ValueError, match="disagree"):
        imported(frame)


def test_missing_models_require_explicit_mapping():
    frame = sessions()
    frame.loc[0, "traffic_model"] = "google_maps"
    with pytest.raises(ValueError, match="not discovered: google_maps"):
        imported(frame, available_models=["googlemaps", "whatsapp"])
    value = imported(frame, available_models=["googlemaps", "whatsapp"],
                     model_mapping={"google_maps": "googlemaps"})
    assert value.sessions.loc[0, "source_traffic_model"] == "google_maps"
    assert value.sessions.loc[0, "traffic_model"] == "googlemaps"


@pytest.mark.parametrize("text", [
    "", "session_id,session_id\n1,2\n", "session_id,experimental_ue\n1,ue1,extra\n",
    "session_id,experimental_ue\n", "session_id,experimental_ue\n1,ue1\n",
])
def test_malformed_csv_is_rejected(text):
    with pytest.raises(ValueError):
        schedule.load(text.encode())


def test_saved_schedule_is_reproducible_and_does_not_replace_other_scenarios(tmp_path):
    value = imported()
    profile = tmp_path / "scenario_config.yaml"
    profile.write_text("existing profile configuration")
    target = schedule.save(value, tmp_path / "schedules")
    content = target.read_bytes()
    assert schedule.save(value, target.parent) == target
    assert target.read_bytes() == content
    assert profile.read_text() == "existing profile configuration"
    document = json.loads(content)
    assert document["scenario_mode"] == "schedule"
    assert document["logical_duration_s"] == 86400
    assert len(document["source_sha256"]) == 64
    assert document["sessions"][1]["scenario_role"] == "augmented"
    target.write_text("changed")
    with pytest.raises(ValueError, match="differs"):
        schedule.save(value, target.parent)
    assert target.read_text() == "changed"


def test_window_offsets_preserve_logical_times_and_carry_in_sessions():
    value = imported()
    window = schedule.execution_window(value, "08:44:00", "08:46:00")
    assert window["start_s"].tolist() == value.sessions["start_s"].tolist()
    assert window["execution_start_s"].tolist() == [0, 0]
    assert window["execution_end_s"].tolist() == [62, 120]
    assert window["session_offset_s"].tolist() == [160, 55]
    assert schedule.execution_window(value, 0, 60).empty


def test_adjacent_sessions_are_not_counted_as_concurrent():
    frame = sessions()
    frame.loc[1, "start_s"] = frame.loc[0, "end_s"]
    value = imported(frame)
    assert schedule.summary(value)["peak_sessions_per_ue"] == 1
    window = schedule.execution_window(value, "08:45:02", "08:47:00")
    assert window["session_id"].tolist() == ["002"]


def test_twelve_ues_and_full_day_are_imported_without_user_classes():
    frame = pd.concat([sessions().assign(experimental_ue=f"ue{index}",
                                         session_id=[f"{index}_1", f"{index}_2"])
                       for index in range(1, 13)], ignore_index=True)
    frame.loc[0, ["start_s", "end_s"]] = ["0", "86400"]
    value = imported(frame)
    assert schedule.summary(value)["ues"] == 12
    assert "ue_class" not in value.sessions
    assert "user_classes" not in value.document()


def test_burst_names_accept_application_underscores(tmp_path):
    folder = tmp_path / "bursts"
    folder.mkdir()
    pd.DataFrame({"packets": [3], "bytes": [300]}).to_parquet(
        folder / "ue3_google_maps_dl_bursts.parquet")
    frame = bursts.load_bursts(tmp_path)
    assert frame.loc[0, ["ue", "app", "direction"]].tolist() == ["ue3", "google_maps", "dl"]


def validated():
    return schedule.validate(imported(), expected_ues=POPULATION, available_models=MODELS)


def test_population_validation_allows_inactive_ues_but_rejects_unknown_ones():
    value = validated()
    assert value.ue_population == tuple(POPULATION)
    assert schedule.summary(value)["population_size"] == 12
    assert schedule.summary(value)["ues"] == 1
    incorrect = [name.replace("mobile_ue3", "mobile_ue13") for name in POPULATION]
    with pytest.raises(ValueError, match="outside the experimental population: mobile_ue3"):
        schedule.validate(imported(), expected_ues=incorrect, available_models=MODELS)


@pytest.mark.parametrize("names", [[],
                                   POPULATION[:11] + [POPULATION[0]], [None] * 12])
def test_invalid_population_cannot_be_validated(names):
    with pytest.raises(ValueError):
        schedule.validate(imported(), expected_ues=names, available_models=MODELS)


@pytest.mark.parametrize("count", [1, 6, 7, 12])
def test_population_size_is_configurable(tmp_path, count):
    names = ["mobile_ue3"] + [f"unused_ue{i}" for i in range(count - 1)]
    value = schedule.validate(imported(), expected_ues=names, available_models=MODELS)
    run = schedule.import_run(value, tmp_path, available_models=MODELS)
    assert len(schedule.load_frozen(run).ue_population) == count


def test_explicit_population_size_can_still_be_required():
    with pytest.raises(ValueError, match="must contain 12"):
        schedule.validate(imported(), expected_ues=POPULATION[:6], available_models=MODELS,
                          expected_count=12)


def test_concurrent_ues_counts_each_ue_once_and_respects_half_open_boundaries():
    frame = pd.DataFrame([
        ("s1", "ue1", 0, 10), ("s2", "ue1", 2, 4),
        ("s3", "ue2", 3, 6), ("s4", "ue3", 10, 12),
        ("s5", "ue4", 12, 14),
    ], columns=["session_id", "experimental_ue", "start_s", "end_s"])
    frame["traffic_model"] = "whatsapp"
    frame["source_category"] = "Social"
    frame["scenario_role"] = "baseline"
    stats = schedule.summary(imported(frame))
    assert stats["max_concurrent_ues"] == 2
    assert stats["peak_sessions_per_ue"] == 2
    assert stats["overlapping_sessions"] is True
    assert stats["earliest_session_s"] == 0
    assert stats["latest_session_s"] == 14
    frame["experimental_ue"] = "ue1"
    assert schedule.summary(imported(frame))["max_concurrent_ues"] == 1


def test_frozen_run_round_trip_preserves_provenance_and_canonical_csv(tmp_path):
    value = validated()
    target = schedule.import_run(value, tmp_path, available_models=MODELS)
    assert target.name.startswith("run_schedule_")
    csv = (target / schedule.SCHEDULE_CSV).read_text()
    assert "scenario_role" in csv.splitlines()[0]
    assert "baseline_or_augmented" not in csv
    recovered = schedule.load_frozen(target)
    pd.testing.assert_frame_equal(recovered.sessions, value.sessions)
    assert recovered.ue_population == value.ue_population
    assert recovered.source_sha256 == value.source_sha256
    assert recovered.sessions["source_session_id"].tolist() == ["trace_001", "trace_001"]
    metadata = json.loads((target / schedule.SCHEDULE_METADATA).read_text())
    assert metadata["summary"]["max_concurrent_ues"] == 1
    assert set(json.loads((target / schedule.SCHEDULE_CHECKSUMS).read_text())) == {
        schedule.SCHEDULE_CSV, schedule.SCHEDULE_METADATA}
    config = json.loads((target / "config.json").read_text())
    assert config["generation_status"] == "pending"
    assert "simulation_duration" not in config
    assert not list(tmp_path.glob(".schedule-import-*"))


def test_repeat_import_is_idempotent_and_never_replaces_frozen_data(tmp_path):
    value = validated()
    target = schedule.import_run(value, tmp_path, available_models=MODELS)
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    assert schedule.import_run(value, tmp_path, available_models=MODELS) == target
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    assert len(list(tmp_path.glob("run_*"))) == 1
    (target / schedule.SCHEDULE_CSV).write_text("changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        schedule.import_run(value, tmp_path, available_models=MODELS)
    assert (target / schedule.SCHEDULE_CSV).read_text() == "changed"


@pytest.mark.parametrize("filename", [schedule.SCHEDULE_CSV, schedule.SCHEDULE_METADATA,
                                     schedule.SCHEDULE_CHECKSUMS])
def test_downstream_reader_rejects_changed_frozen_inputs(tmp_path, filename):
    target = schedule.import_run(validated(), tmp_path, available_models=MODELS)
    (target / filename).write_text("{}")
    with pytest.raises(ValueError):
        schedule.load_frozen(target)


def test_modified_plan_gets_its_own_run(tmp_path):
    first = validated()
    original = schedule.import_run(first, tmp_path, available_models=MODELS)
    first.sessions.loc[0, "end_s"] += 1
    updated = schedule.import_run(first, tmp_path, available_models=MODELS)
    assert original != updated
    assert (original / schedule.SCHEDULE_CSV).read_bytes() != (
        updated / schedule.SCHEDULE_CSV).read_bytes()


def test_failed_import_does_not_publish_a_partial_run(tmp_path, monkeypatch):
    from pathlib import Path

    def fail_publish(self, target):
        raise OSError("publication failed")

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="publication failed"):
        schedule.import_run(validated(), tmp_path, available_models=MODELS)
    assert list(tmp_path.iterdir()) == []


def test_import_requires_membership_and_model_validation(tmp_path):
    with pytest.raises(ValueError, match="population"):
        schedule.import_run(imported(), tmp_path, available_models=MODELS)
    with pytest.raises(ValueError, match="not discovered"):
        schedule.import_run(validated(), tmp_path, available_models=["googlemaps"])
    with pytest.raises(ValueError, match="recognized application models"):
        schedule.import_run(validated(), tmp_path, available_models=None)
    assert list(tmp_path.iterdir()) == []


def test_pending_imports_do_not_enter_existing_run_selection_or_remote_actions(tmp_path, monkeypatch):
    pending = schedule.import_run(validated(), tmp_path, available_models=MODELS)
    legacy = tmp_path / "run_legacy"
    legacy.mkdir()
    assert runs.list_runs(tmp_path) == [legacy]

    def unexpected(*args, **kwargs):
        pytest.fail("pending schedule attempted testbed work")

    monkeypatch.setattr(testbed.run_profile, "record", unexpected)
    monkeypatch.setattr(testbed, "_run", unexpected)
    with pytest.raises(ValueError, match="generate application bursts"):
        testbed.run_experiment(pending, {})
    with pytest.raises(ValueError, match="generate application bursts"):
        testbed.fetch_logs(pending.name, pending, {})
    assert not (pending / "logs").exists()
    config = json.loads((pending / "config.json").read_text())
    config["generation_status"] = "complete"
    (pending / "config.json").write_text(json.dumps(config))
    assert set(runs.list_runs(tmp_path)) == {legacy, pending}
