from pathlib import Path

import pandas as pd

from dashboard import dataset_view
from twindash import dataset, schema


def _execution(tmp_path: Path, execution_id: str, profile_id: str,
               feature_rows: int, flow_rows: int) -> dataset.Execution:
    archive = tmp_path / execution_id
    archive.mkdir()
    row_counts = {
        schema.PACKET_OUTCOMES: flow_rows,
        schema.UE_APP_SECOND_OBSERVED: feature_rows,
        schema.CHANNEL_SEGMENTS: 1,
        schema.SEGMENT_TRAINING_TABLE: 1,
    }
    for name, rows in row_counts.items():
        pd.DataFrame({
            "execution_id": [execution_id] * rows,
            "profile_id": [profile_id] * rows,
            "ue": ["ue1"] * rows,
            "value": range(rows),
        }).to_parquet(archive / name, index=False)
    return dataset.Execution(
        execution_id=execution_id,
        profile_id=profile_id,
        path=archive,
        metadata={
            "schema_version": 2,
            "table_rows": row_counts,
            "quality": {
                "expected_ues": 2,
                "measured_ues": 1,
                "feature_rows": feature_rows,
                "radio_rows": max(feature_rows - 1, 0),
                "channel_schedule_enabled": True,
                "channel_state_verified": True,
                "xapp": {"clean_shutdown": True},
            }
        },
    )


def test_coverage_table_uses_plain_language_and_counts_missing_values():
    features = pd.DataFrame({
        "complete": [1.0, 2.0],
        "partial": [1.0, None],
    })

    result = dataset_view._coverage_table(features)

    assert list(result.columns) == [
        "feature", "type", "rows with a value", "row completeness"
    ]
    partial = result.set_index("feature").loc["partial"]
    assert partial["rows with a value"] == 1
    assert partial["row completeness"] == 50.0


def test_export_preview_matches_export_split_and_row_counts(tmp_path):
    first = _execution(tmp_path, "mgen-preview-a", "profile-a", 3, 4)
    second = _execution(tmp_path, "mgen-preview-b", "profile-b", 2, 5)

    plan = dataset_view._export_plan([first, second]).set_index("execution")

    assert plan.loc[first.execution_id, "assigned split"] == dataset._split(
        first.execution_id)
    assert plan.loc[second.execution_id, "assigned split"] == dataset._split(
        second.execution_id)
    assert plan["UE-app-second rows"].sum() == 5
    assert plan["packet rows"].sum() == 9
    assert plan["training rows"].sum() == 2
    assert plan.loc[first.execution_id, "UEs represented"] == "1/2"

    samples = dataset_view._load_export_samples(
        dataset_view._export_sample_signature([first, second]),
        rows_per_execution=1,
    )

    assert all(len(frame) == 2 for frame in samples.values())
    for record in (first, second):
        expected_split = dataset._split(record.execution_id)
        for frame in samples.values():
            row = frame.loc[frame["execution_id"] == record.execution_id].iloc[0]
            assert row["profile_id"] == record.profile_id
            assert row["split"] == expected_split
