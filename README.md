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
  designed_kpis.parquet
  observed_kpis.parquet
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
```

`flow_batch_map.csv` is the authoritative packet-attribution key after UPF NAT.
Ports stay shared by direction (UL 5000, DL 5001); flow IDs distinguish UE,
application, direction, and batch.

`run_timing.json` anchors each node's MGEN wall clock to epoch time. MGEN
throughput and FlexRIC PRB are joined on `(utc_second, ue)`, never on two
independently-normalized timelines.

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
- **Testbed** to enter the current core/cell POWDER hostnames;
- **Results** to run the distributed experiment, validate collected logs,
  rebuild KPIs, and view traffic/PRB efficiency.

## CLI

```bash
python -m twindash.cli list
python -m twindash.cli preflight <run_name>
python -m twindash.cli deploy <run_name>
python -m twindash.cli fetch <run_name>
python -m twindash.cli kpis <run_name>
```

`preflight` is local and side-effect free. `deploy` runs the remote experiment.
For the distributed profile, `fetch` verifies the artifacts already collected
by the runner and then builds `observed_kpis.parquet`.

## Measurement notes

- Throughput is UDP payload goodput over the configured run duration.
- Cross-node one-way latency requires synchronized clocks; request-response RTT
  derived on one UE does not.
- `rnti_map.csv` is a per-run snapshot. Never reuse it after UE reattachment.
- The xApp should run in a bounded window because raw `MAC_UE` data is roughly
  1 kHz per attached UE. The runner aggregates on the core before transfer.
