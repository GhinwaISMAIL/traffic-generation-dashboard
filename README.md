# twindash

`twindash` is the local integration layer between the multimodal MGEN pipeline
and POWDER. The notebooks design a run, the distributed runner operates the
core/cell nodes and FlexRIC xApp, and the dashboard reads the resulting run
folder. The CLI and Streamlit use the same Python modules and configuration.

## Repository connections

1. `dashboard_config.yaml` points `profiles_dir` at the traffic pipeline's
   `traffic_profiles/` directory.
2. The Testbed page writes the pipeline repository's gitignored
   `testbed_config.yaml`.
3. For `powder_ric5g_distributed`, `twindash` launches the pipeline's tracked
   `deploy_ric5g.sh`. That script uses SSH to the core and 1–3 selected cells and calls
   `/local/repository/bin/mgen-{core,cell}.sh` on the nodes.
4. The runner collects MGEN logs, the per-run RNTI map, xApp output, compact PRB
   CSV, and clock anchors into `<run>/logs/`. Results are then local and
   reproducible; Streamlit never queries a live SQLite database.

## Run-folder contract

```text
traffic_profiles/run_<id>/
  config.json
  run_profile.json
  designed_kpis.parquet
  observed_kpis.parquet
  channel_schedule.json       # optional, authored before deployment
  mgen_scripts/
    manifest.csv
    flow_batch_map.csv
    dn_dl_tx.mgn
    dn_ul_rx.mgn
    ue*_ul_tx.mgn
    ue*_dl_rx.mgn
  logs/
    deployment.log
    run_timing.json
    rnti_map.csv
    prb_by_second.csv
    xapp.log
    dn_*_*.log
    ue*_*_*.log
  executions/
    mgen-<timestamp>/          # immutable snapshot of one real deployment
      metadata.json
      ue_second_features.parquet
      observed_kpis.parquet
      logs/                    # timing, mapping, xApp, PRB, channel provenance
```

`flow_batch_map.csv` is the authoritative packet-attribution key after UPF NAT.
Ports stay shared by direction (UL 5000, DL 5001); flow IDs distinguish UE,
application, direction, and batch.

`run_timing.json` anchors each node's MGEN wall clock to epoch time. FlexRIC
radio rows retain both the RFsim/service-model source timestamp and the core
receipt timestamp. `utc_second` is derived only from receipt time, so MGEN
throughput and PRB are joined on `(utc_second, ue)` without treating simulated
radio time as UTC. Legacy single-clock PRB captures are visible as invalid but
cannot be archived or exported into a training dataset.

`run_profile.json` is written when the experiment is launched. It snapshots the
deployment type and measurement capabilities used for that run, so changing
`testbed_config.yaml` later cannot reinterpret historical results. RFsim and
COTS runs show their MGEN traffic KPIs; RIC5G runs additionally expose channel
context, RIC/xApp health, PRB, and bits/PRB when xApp collection was enabled.
Legacy runs without this file are labelled as inferred and use only the
artifacts that are actually present.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,export]'
cp dashboard_config.example.yaml dashboard_config.yaml
streamlit run dashboard/app.py
```

Set `profiles_dir` in `dashboard_config.yaml`, then use:

- **Design** to choose 1–3 cells, set the common UEs-per-cell reservation, and
  give every cell its own class distribution or named traffic profiles before
  writing `scenario_config.yaml` for the notebook pipeline;
- **Testbed** to enter the current core/cell POWDER hostnames and, only for the
  RIC5G profile, declare a boot-model expectation and schedule verified per-UE
  downlink or per-cell uplink parameter changes on the traffic clock;
- **Results** to run the configured experiment, validate collected logs,
  rebuild KPIs, and view only the KPI sections supported by that run's saved
  deployment profile;
- **Dataset** to inspect the latest capture's UE coverage, flow/radio/channel
  row counts, provenance, feature completeness, and sample training rows before
  archiving it; curated executions can then be exported as model-ready Parquet
  tables. Train/validation/test assignment is by execution ID, which prevents
  seconds from the same run leaking across splits.

The sidebar follows the run lifecycle: **Design → Testbed → Results → Dataset**.
Channel control is intentionally part of Testbed rather than a separate page,
because only the RIC5G distributed profile supports it.

## CLI

```bash
python -m twindash.cli list
python -m twindash.cli preflight <run_name>
python -m twindash.cli deploy <run_name>
python -m twindash.cli deploy <run_name> --include-raw
python -m twindash.cli archive <run_name>
python -m twindash.cli dataset <dataset_name> \
  --execution mgen-<timestamp> [--execution mgen-<timestamp> ...]
python -m twindash.cli fetch <run_name>
python -m twindash.cli kpis <run_name>
```

`preflight` is local and side-effect free. `deploy` runs the remote experiment,
rebuilds flow KPIs, and immediately creates an immutable execution archive.
For the distributed profile, `fetch` verifies the artifacts already collected
by the runner and then builds `observed_kpis.parquet`.

## Measurement notes

- Throughput is UDP payload goodput over the configured run duration.
- Cross-node one-way latency requires synchronized clocks; request-response RTT
  derived on one UE does not.
- `rnti_map.csv` is a per-run snapshot. Never reuse it after UE reattachment.
- RIC5G channel family (`AWGN`, `TDL_*`, and similar) is selected at boot. The
  runtime schedule changes only readable numeric parameters. Every transition
  is read back; a missing verification fails deployment rather than producing
  an incorrect training label. Exact settings are displayed only when the run
  contains a successful `logs/channel_state.json`.
- The xApp should run in a bounded window because raw `MAC_UE` data is roughly
  1 kHz per attached UE. The runner aggregates on the core before transfer.
  Counter deltas whose receipt interval is outside 0.5–1.5 seconds are excluded
  from one-second bits/PRB joins and reported in Results.
