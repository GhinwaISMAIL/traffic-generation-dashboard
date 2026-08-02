"""The run-folder contract. One place that both the notebooks, the CLI and the
dashboard agree on, so nobody hardcodes a path twice.

traffic_profiles/run_<id>/
    config.json            # written by the notebook pipeline
    run_profile.json       # deployment/testbed capabilities captured at run time
    designed_kpis.parquet  # the projection (offered load) per flow
    observed_kpis.parquet  # built after a run from the logs
    mgen_scripts/          # the .mgn files
    logs/                  # raw dn + ue logs pulled back from the testbed
    channel_schedule.json  # optional verified runtime impairment timeline
    executions/<id>/       # immutable archive of one real deployment
"""

CONFIG = "config.json"
RUN_PROFILE = "run_profile.json"
DESIGNED_KPIS = "designed_kpis.parquet"
OBSERVED_KPIS = "observed_kpis.parquet"
CHANNEL_SCHEDULE = "channel_schedule.json"
EXECUTIONS_DIR = "executions"
EXECUTION_METADATA = "metadata.json"
UE_SECOND_FEATURES = "ue_second_features.parquet"
SCHEMA_VERSION = 2
PACKET_OUTCOMES = "packet_outcomes.parquet"
UE_APP_SECOND_OBSERVED = "ue_app_second_observed.parquet"
CHANNEL_SEGMENTS = "channel_segments.parquet"
SEGMENT_TRAINING_TABLE = "segment_training_table.parquet"
MODEL_CONTRACT = "model_contract.json"
DATASET_MANIFEST = "dataset_manifest.json"
V2_TABLES = (
    PACKET_OUTCOMES,
    UE_APP_SECOND_OBSERVED,
    CHANNEL_SEGMENTS,
    SEGMENT_TRAINING_TABLE,
)
LOGS_DIR = "logs"
SCRIPTS_DIR = "mgen_scripts"

# join keys shared by the designed and observed tables
KEYS = ["run_id", "flow_id", "direction"]

# columns the notebook should fill into designed_kpis.parquet. These are the
# *offered load* — the only side you can project. Loss/latency have no
# projected counterpart; they only exist on the observed side.
DESIGNED_COLUMNS = [
    "run_id", "ue", "flow_id", "direction", "app",
    "proj_packets", "proj_bytes", "proj_duration_s", "proj_throughput_bps",
]
