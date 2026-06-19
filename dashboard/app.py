"""twindash dashboard.

    streamlit run dashboard/app.py

Results page  — pick a run, see projected -> sent -> received, loss, latency.
Design page   — edit scenario_config.yaml from a form (writes the file; you
                still run the notebook pipeline and deploy yourself).

It never SSHes on its own except the explicit "Fetch logs" button, which calls
the same twindash.testbed.fetch_logs the CLI uses.
"""
import sys
from pathlib import Path

import streamlit as st
import yaml

sys.path.append(str(Path(__file__).resolve().parents[1]))
from twindash import runs, kpis, testbed  # noqa: E402

PROFILES = "traffic_profiles"
APPS = ["aparat", "filimo", "igap", "telegram", "youtube"]

st.set_page_config(page_title="twindash", layout="wide")
page = st.sidebar.radio("Page", ["Results", "Design"])


if page == "Results":
    st.title("Run results")
    found = runs.list_runs(PROFILES)
    if not found:
        st.info("No runs under traffic_profiles/ yet.")
        st.stop()

    run = st.sidebar.selectbox("Run", [r.name for r in found])
    run_dir = Path(PROFILES) / run

    c1, c2 = st.columns(2)
    if c1.button("Fetch logs from testbed"):
        with st.spinner("Copying logs back…"):
            testbed.fetch_logs(run, run_dir, testbed.load_testbed_config())
            kpis.save_observed(run_dir)
        st.success("Fetched and rebuilt observed KPIs.")
    if c2.button("Rebuild KPIs from existing logs"):
        kpis.save_observed(run_dir)
        st.success("Rebuilt.")

    table = kpis.reconcile(run_dir)
    if table is None or table.empty:
        st.warning("No KPIs yet — fetch logs first.")
        st.stop()

    st.subheader("Projected \u2192 sent \u2192 received")
    st.dataframe(table, use_container_width=True)

    if "loss" in table and "ue" in table:
        st.subheader("Mean loss by UE")
        st.bar_chart(table.groupby("ue")["loss"].mean())

    if {"sent_packets", "recv_packets", "ue"}.issubset(table.columns):
        st.subheader("Sent vs received (packets)")
        st.bar_chart(table.groupby("ue")[["sent_packets", "recv_packets"]].sum())

    if any(c.startswith("latency_ms") for c in table.columns):
        st.caption("Latency is trustworthy only with chrony sync across UEs "
                   "(check `chronyc tracking` before the run).")

else:
    st.title("Design a scenario")
    cfg_path = Path("scenario_config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    dist = cfg.get("user_class_distribution", {})

    apps = st.multiselect("Apps", APPS, default=cfg.get("apps", ["filimo"]))
    n_ue = st.number_input("N_UE", 1, 16, cfg.get("n_ue", 6))
    duration = st.number_input("Duration (s)", 30, 3600, cfg.get("duration", 600))
    cols = st.columns(3)
    heavy = cols[0].number_input("heavy", 0, 16, dist.get("heavy", 2))
    medium = cols[1].number_input("medium", 0, 16, dist.get("medium", 3))
    light = cols[2].number_input("light", 0, 16, dist.get("light", 1))
    seed = st.number_input("seed", 0, 100_000, cfg.get("seed", 0))

    if st.button("Write scenario_config.yaml"):
        new = {
            "apps": apps,
            "n_ue": int(n_ue),
            "duration": int(duration),
            "user_class_distribution": {"heavy": int(heavy),
                                        "medium": int(medium),
                                        "light": int(light)},
            "seed": int(seed),
        }
        cfg_path.write_text(yaml.safe_dump(new, sort_keys=False))
        st.success(f"Wrote {cfg_path}. Run the notebook pipeline next, then deploy.")
        st.code(yaml.safe_dump(new, sort_keys=False), language="yaml")
