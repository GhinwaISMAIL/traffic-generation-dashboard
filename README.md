# twindash

The integration layer between the notebook pipeline and the POWDER testbed.
The notebooks design a run; `twindash` operates the testbed and builds KPIs;
the dashboard reads them back. One module, used by both the CLI and Streamlit.

## Layout

```
twindash/
  schema.py     run-folder contract + shared join keys
  mgen_log.py   parse MGEN tx/rx logs
  kpis.py       build observed_kpis, reconcile against designed_kpis
  runs.py       discover / load run folders
  testbed.py    fetch logs (scp + docker cp), run generated scripts
  cli.py        list | fetch | kpis | deploy
dashboard/
  app.py        Results + Design pages
```

## Per-run workflow

1. Author a scenario (Design page, or edit `scenario_config.yaml`), run the
   notebook pipeline. Notebook 2 also writes `designed_kpis.parquet`
   (columns in `schema.DESIGNED_COLUMNS`).
2. `python -m twindash.cli deploy <run>`  — copy scripts out.
3. Run the experiment (your scripted receivers-then-senders start).
4. `python -m twindash.cli fetch <run>`   — pull logs back, build observed KPIs.
5. `streamlit run dashboard/app.py`        — inspect projected -> sent -> received.

A calibration sweep is just a loop over steps 2–4 with different seeds.

## Notes

- `testbed_config.yaml` (gitignored) holds the SSH hosts; see the example below.
- Latency columns need chrony sync across UEs; volume and loss don't.
- `observed_kpis.parquet` doubles as the conformal-prediction calibration set.
- The MGEN token names in `mgen_log.py` follow the documented format — check
  them against your real logs once and adjust if your build differs.

```yaml
# testbed_config.example.yaml
cn5g_ssh_host: ghinwa@cn5g-docker-host.<experiment>.emulab.net
physical_ues:
  - {name: nuc1, ssh_host: ghinwa@ota-nuc1-cots-ue.<experiment>.emulab.net}
  - {name: nuc2, ssh_host: ghinwa@ota-nuc2-cots-ue.<experiment>.emulab.net}
```

## Deps

```
pip install pandas pyarrow pyyaml streamlit
```
