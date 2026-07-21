"""The run-folder contract. One place that both the notebooks, the CLI and the
dashboard agree on, so nobody hardcodes a path twice.

traffic_profiles/run_<id>/
    config.json            # written by the notebook pipeline
    run_profile.json       # deployment/testbed capabilities captured at run time
    designed_kpis.parquet  # the projection (offered load) per flow
    observed_kpis.parquet  # built after a run from the logs
    mgen_scripts/          # the .mgn files
    logs/                  # raw dn + ue logs pulled back from the testbed
"""

CONFIG = "config.json"
RUN_PROFILE = "run_profile.json"
DESIGNED_KPIS = "designed_kpis.parquet"
OBSERVED_KPIS = "observed_kpis.parquet"
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
