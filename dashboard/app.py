"""twindash dashboard.

    streamlit run dashboard/app.py

Results page  — pick a run, see projected -> sent -> received, loss, latency.
Design page   — edit scenario_config.yaml from a form (writes the file; you
                still run the notebook pipeline and deploy yourself).

It never SSHes on its own except the explicit "Fetch logs" button, which calls
the same twindash.testbed.fetch_logs the CLI uses.
"""
import copy
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

sys.path.append(str(Path(__file__).resolve().parents[1]))
from twindash import (bursts, kpis, realized, ric5g, runs, scenario, settings,
                      testbed, testbed_cfg)  # noqa: E402
from dashboard import results_view  # noqa: E402

PROFILES = settings.profiles_dir()

st.set_page_config(page_title="twindash", layout="wide")
page = st.sidebar.radio("Page", ["Results", "Design", "Testbed"])


if page == "Results":
    st.title("Run results")
    found = runs.list_runs(PROFILES)
    if not found:
        st.info("No runs under traffic_profiles/ yet.")
        st.stop()

    run = st.sidebar.selectbox("Run", [r.name for r in found])
    run_dir = Path(PROFILES) / run

    try:
        active_testbed = testbed.load_testbed_config()
    except (FileNotFoundError, TypeError, yaml.YAMLError):
        active_testbed = {}
    distributed = ric5g.is_config(active_testbed)
    configured = bool(active_testbed.get("testbed"))

    c1, c2, c3 = st.columns(3)
    if c1.button("Run configured experiment", disabled=not configured,
                 help="Uses the profile and hosts currently saved on the Testbed page"):
        try:
            with st.spinner("Running the configured experiment on POWDER…"):
                testbed.run_experiment(run_dir, active_testbed)
                kpis.save_observed(run_dir)
            st.success("Experiment completed and KPIs were rebuilt.")
        except Exception as exc:
            st.error(f"Experiment failed: {exc}")
    fetch_label = "Verify collected logs" if distributed else "Fetch logs from testbed"
    if c2.button(fetch_label, disabled=not configured):
        try:
            with st.spinner("Checking artifacts…" if distributed else
                            "Copying logs back…"):
                testbed.fetch_logs(run, run_dir, active_testbed)
                kpis.save_observed(run_dir)
            st.success("Artifacts verified and observed KPIs rebuilt.")
        except Exception as exc:
            st.error(f"Artifact verification failed: {exc}")
    if c3.button("Rebuild KPIs from existing logs"):
        try:
            kpis.save_observed(run_dir)
            st.success("Rebuilt.")
        except Exception as exc:
            st.error(f"KPI rebuild failed: {exc}")

    results_view.render(run_dir)

elif page == "Design":
    st.title("Design a scenario")
    st.caption(f"Writes {settings.scenario_config_path()} — the file Notebook 1 "
               "reads. Advanced blocks are preserved; the previous file is backed "
               "up to .bak. After writing, re-run Notebook 1 \u2192 Notebook 2.")

    base = scenario.merged()
    discovered = scenario.discover_apps()
    if not discovered:
        st.warning(f"No apps discovered under {settings.artifacts_dir()} — check "
                   "the profiles_dir path in dashboard_config.yaml.")
    app_options = discovered or base["apps"] or ["filimo", "igap", "youtube",
                                                 "telegram", "aparat"]

    st.subheader("Simulation")
    existing_cell_specs = scenario.cell_specs(base)
    sim = base["simulation"]
    default_cells = min(3, max(1, len(existing_cell_specs)))
    default_ues_per_cell = int(sim.get(
        "ues_per_cell", existing_cell_specs[0].get("n_ue", 4)))
    sc = st.columns(4)
    duration = sc[0].number_input("Duration (s)", 30, 7200,
                                  int(sim["duration"]))
    num_cells = int(sc[1].number_input(
        "Cells", 1, 3, int(sim.get("num_cells", default_cells))))
    ues_per_cell = int(sc[2].number_input(
        "UEs per cell", 1, 32, default_ues_per_cell))
    seed = sc[3].number_input("random_seed", 0, 1_000_000,
                              int(sim["random_seed"]))
    n_ue = num_cells * ues_per_cell
    st.info(
        f"Scenario size: {num_cells} cell(s) x {ues_per_cell} UE(s) = "
        f"{n_ue} UEs. Use the same values when reserving the POWDER profile.")

    st.subheader("Apps")
    default_apps = [a for a in base["apps"] if a in app_options] or app_options
    apps = st.multiselect("Apps (discovered from artifacts/)", app_options,
                          default=default_apps)

    st.subheader("Per-cell UE specifications")
    st.caption(
        "Each cell has its own UE mix. Profiles control the learned realism base, "
        "per-UE flow target, and application weights. Counts are checked separately "
        "for every cell, so traffic assigned to Cell 1 cannot spill into Cell 2.")

    def default_distribution(count):
        heavy_count = count // 4
        light_count = count // 4
        return {"heavy": heavy_count,
                "medium": count - heavy_count - light_count,
                "light": light_count}

    cell_configs = []
    tabs = st.tabs([f"Cell {index}" for index in range(1, num_cells + 1)])
    for cell_index, tab in enumerate(tabs, start=1):
        with tab:
            existing_cell = next(
                (item for item in existing_cell_specs
                 if int(item.get("cell", 0)) == cell_index), {})
            existing_profiles = existing_cell.get("profiles") or []
            use_profiles = st.toggle(
                "Use named profiles", value=bool(existing_profiles),
                key=f"cell_{cell_index}_use_profiles",
                help="Turn off to assign only heavy/medium/light class counts.")

            profiles = []
            if use_profiles:
                st.caption(
                    f"Profile counts must sum to {ues_per_cell}. App values are "
                    "relative weights; zero excludes that app from the profile.")
                rows = []
                for profile in existing_profiles:
                    flows = profile.get("flows", 10)
                    row = {
                        "name": profile.get("name", ""),
                        "count": int(profile.get("count", 0)),
                        "base": profile.get("base", "medium"),
                        "flows": int(flows[0] if isinstance(
                            flows, (list, tuple)) else flows),
                    }
                    mix = profile.get("app_mix") or {}
                    for app in apps:
                        row[app] = float(mix.get(app, 0))
                    rows.append(row)
                if not rows:
                    rows = [{
                        "name": f"cell{cell_index}_medium",
                        "count": ues_per_cell, "base": "medium", "flows": 10,
                        **{app: 1.0 for app in apps},
                    }]
                prof_df = pd.DataFrame(
                    rows, columns=["name", "count", "base", "flows"] + apps)
                edited = st.data_editor(
                    prof_df, num_rows="dynamic", use_container_width=True,
                    hide_index=True,
                    column_config={
                        "name": st.column_config.TextColumn("name"),
                        "count": st.column_config.NumberColumn(
                            "count", min_value=0, step=1),
                        "base": st.column_config.SelectboxColumn(
                            "base", options=["heavy", "medium", "light"]),
                        "flows": st.column_config.NumberColumn(
                            "flows / UE", min_value=1, step=1),
                        **{app: st.column_config.NumberColumn(
                            app, min_value=0.0, step=1.0) for app in apps},
                    },
                    key=f"cell_{cell_index}_profiles_{'_'.join(apps)}")
                for _, row in edited.iterrows():
                    name = str(row.get("name") or "").strip()
                    if not name or name.lower() == "nan":
                        continue
                    mix = {
                        app: float(row[app]) for app in apps
                        if not pd.isna(row.get(app)) and float(row.get(app)) > 0
                    }
                    profiles.append({
                        "name": name,
                        "count": 0 if pd.isna(row.get("count")) else int(
                            row.get("count")),
                        "base": "medium" if pd.isna(row.get("base")) else str(
                            row.get("base")),
                        "flows": 1 if pd.isna(row.get("flows")) else int(
                            row.get("flows")),
                        "app_mix": mix,
                    })
                profile_total = sum(profile["count"] for profile in profiles)
                (st.success if profile_total == ues_per_cell else st.error)(
                    f"Cell {cell_index}: profile counts sum to {profile_total}; "
                    f"expected {ues_per_cell}")
                distribution = {}
            else:
                distribution = (existing_cell.get("distribution") or
                                default_distribution(ues_per_cell))
                columns = st.columns(3)
                heavy = int(columns[0].number_input(
                    "heavy", 0, 32, int(distribution.get("heavy", 0)),
                    key=f"cell_{cell_index}_heavy"))
                medium = int(columns[1].number_input(
                    "medium", 0, 32, int(distribution.get("medium", 0)),
                    key=f"cell_{cell_index}_medium"))
                light = int(columns[2].number_input(
                    "light", 0, 32, int(distribution.get("light", 0)),
                    key=f"cell_{cell_index}_light"))
                distribution = {"heavy": heavy, "medium": medium, "light": light}
                class_total = sum(distribution.values())
                (st.success if class_total == ues_per_cell else st.error)(
                    f"Cell {cell_index}: class counts sum to {class_total}; "
                    f"expected {ues_per_cell}")

            cell_config = {"cell": cell_index, "n_ue": ues_per_cell}
            if profiles:
                cell_config["profiles"] = profiles
            else:
                cell_config["distribution"] = distribution
            cell_configs.append(cell_config)

    sampling = st.selectbox("Sampling strategy", ["stratified", "random"],
                            index=0 if base["sampling_strategy"] != "random" else 1)

    st.subheader("Temporal correlation")
    tc = base["temporal_correlation"]
    enabled = st.checkbox("enabled", value=bool(tc["enabled"]))
    rc = st.columns(2)
    rtt_lo = rc[0].number_input("RTT min (s)", 0.0, 5.0,
                                float(tc["rtt_delay_range"][0]), step=0.005, format="%.3f")
    rtt_hi = rc[1].number_input("RTT max (s)", 0.0, 5.0,
                                float(tc["rtt_delay_range"][1]), step=0.005, format="%.3f")
    bc = st.columns(2)
    dl_lo = bc[0].number_input("DL bursts / request — min", 1, 50,
                               int(tc["dl_bursts_per_ul_request"][0]))
    dl_hi = bc[1].number_input("DL bursts / request — max", 1, 50,
                               int(tc["dl_bursts_per_ul_request"][1]))
    jitter = st.number_input("jitter (s)", 0.0, 1.0, float(tc["jitter"]),
                             step=0.001, format="%.3f")

    st.subheader("Network addressing")
    net = base["network"]
    nc = st.columns(2)
    dn_ip = nc[0].text_input("DN IP", str(net["dn_ip"]))
    ue_prefix = nc[1].text_input("UE IP prefix", str(net["ue_ip_prefix"]))
    pc = st.columns(3)
    ue_start = pc[0].number_input("UE IP start", 0, 255, int(net["ue_ip_start"]))
    dl_port = pc[1].number_input("DL port (DN\u2192UE)", 1, 65535, int(net["dl_port"]))
    ul_port = pc[2].number_input("UL port (UE\u2192DN)", 1, 65535, int(net["ul_port"]))

    new_cfg = copy.deepcopy(base)  # carries the advanced blocks through untouched
    new_cfg["simulation"] = {
        "duration": int(duration), "num_cells": num_cells,
        "ues_per_cell": ues_per_cell, "n_ue": n_ue,
        "random_seed": int(seed),
    }
    new_cfg["apps"] = apps
    new_cfg["cells"] = cell_configs
    new_cfg["user_classes"]["distribution"] = {
        class_name: sum(int((cell.get("distribution") or {}).get(class_name, 0))
                        for cell in cell_configs)
        for class_name in ("heavy", "medium", "light")
    }
    new_cfg.pop("profiles", None)
    new_cfg["sampling_strategy"] = sampling
    new_cfg["temporal_correlation"].update({
        "enabled": bool(enabled),
        "rtt_delay_range": [float(rtt_lo), float(rtt_hi)],
        "dl_bursts_per_ul_request": [int(dl_lo), int(dl_hi)],
        "jitter": float(jitter),
    })
    new_cfg["network"] = {"dn_ip": dn_ip, "ue_ip_prefix": ue_prefix,
                          "ue_ip_start": int(ue_start),
                          "dl_port": int(dl_port), "ul_port": int(ul_port)}

    errs = scenario.validate(new_cfg)
    with st.expander("Preview scenario_config.yaml"):
        st.code(yaml.safe_dump(new_cfg, sort_keys=False), language="yaml")
    for e in errs:
        st.error(e)

    if st.button("Write scenario_config.yaml", disabled=bool(errs)):
        p = scenario.save(new_cfg)
        st.success(f"Wrote {p}  (backup at {p.with_suffix('.yaml.bak')}). "
                   "Re-run Notebook 1 \u2192 Notebook 2 to regenerate.")


else:  # Testbed
    st.title("Testbed configuration")
    st.caption(f"Writes {settings.repo_root() / 'testbed_config.yaml'} (gitignored). "
               "UE boxes and ue_name_map are generated from the scenario UE count. "
               "UE IPs are resolved live at deploy. Previous file is backed up to .bak.")

    existing = testbed_cfg.load()
    kind_for_testbed = {
        "powder_ric5g_distributed": "ric5g",
        "powder_rfsim_docker": "rfsim",
        "powder_emulab": "cots",
    }
    kind_default = kind_for_testbed.get(existing.get("testbed"), "ric5g")
    labels = {
        "ric5g": "RIC5G distributed (core + 1–3 cells)",
        "rfsim": "RFsim (Docker, single node)",
        "cots": "COTS (physical UEs)",
    }
    choices = list(labels)
    kind_label = st.selectbox(
        "Testbed type", [labels[k] for k in choices],
        index=choices.index(kind_default))
    kind = next(k for k, label in labels.items() if label == kind_label)
    t = testbed_cfg.TEMPLATES[kind]

    same_kind = existing.get("testbed") == t["testbed"]
    cur_user = (existing.get("ues") or {}).get("username", "ghinwa")
    design_sim = scenario.merged()["simulation"]
    n_ue = int(design_sim["n_ue"])
    design_cells = int(design_sim.get("num_cells", 1))
    design_ues_per_cell = int(design_sim.get("ues_per_cell", n_ue))
    st.info(
        f"Generating {n_ue} UEs as {design_cells} cell(s) x "
        f"{design_ues_per_cell} UE(s), exactly as configured on the Design page.")

    fc = st.columns(2)
    allow_ph = fc[0].checkbox("allow_placeholder_hosts",
                              value=(existing.get("flags") or {}).get("allow_placeholder_hosts", False))
    allow_inv = fc[1].checkbox("allow_invalid_run",
                               value=(existing.get("flags") or {}).get("allow_invalid_run", False))

    if kind == "ric5g":
        hosts = testbed_cfg.ric5g_hosts(existing) if same_kind else {}
        identity = st.columns(2)
        username = identity[0].text_input("SSH username", cur_user).strip()
        core_host = identity[1].text_input(
            "Core host", hosts.get("core", ""),
            placeholder="pcXXX.emulab.net").strip()
        num_cells = design_cells
        st.caption(
            f"Enter one host for each of the {num_cells} cell nodes reserved in "
            "POWDER. To change this count, update the Design page first.")
        host_columns = st.columns(num_cells)
        cell_hosts = [
            host_columns[index - 1].text_input(
                f"Cell {index} host", hosts.get(f"cell{index}", ""),
                placeholder="pcXX-site.emulab.net", key=f"ric5g_cell_{index}").strip()
            for index in range(1, num_cells + 1)
        ]
        old_xapp = (existing.get("xapp") or {}) if same_kind else {}
        old_dn = (existing.get("dn") or {}) if same_kind else {}
        old_mgen = (existing.get("mgen") or {}) if same_kind else {}
        old_runner = (existing.get("runner") or {}) if same_kind else {}
        with st.expander("Advanced distributed settings"):
            ac = st.columns(3)
            ac[0].number_input(
                "UEs per cell (from Design)", 1, 32,
                design_ues_per_cell, disabled=True)
            ues_per_cell = design_ues_per_cell
            xapp_enabled = ac[1].checkbox(
                "Collect PRB with xApp", value=old_xapp.get("enabled", True))
            xapp_delay = ac[2].number_input(
                "xApp delay (s)", 0, 7200, int(old_xapp.get("delay_s", 270)))
            xapp_window = st.number_input(
                "xApp collection window (s)", 1, 3600,
                int(old_xapp.get("window_s", 60)))
            dn_container = st.text_input(
                "DN container", old_dn.get("container", t["dn_container"]))
            ue_container_tpl = st.text_input(
                "UE container pattern", t["ue_container_tpl"],
                help="Available fields: {cell}, {ue}, and global {n}")
            remote_bin = st.text_input(
                "Remote helper directory", old_mgen.get("remote_bin", t["remote_bin"]))
            runner = st.text_input(
                "Local runner", old_runner.get("script", t["runner"]))

        cfg = testbed_cfg.build_ric5g(
            username=username, n_ue=n_ue, core_host=core_host,
            cell_hosts=cell_hosts, ues_per_cell=int(ues_per_cell),
            dn_container=dn_container, ue_container_tpl=ue_container_tpl,
            remote_bin=remote_bin, runner=runner, xapp_enabled=xapp_enabled,
            xapp_delay_s=int(xapp_delay), xapp_window_s=int(xapp_window),
            flags={"allow_placeholder_hosts": allow_ph,
                   "allow_invalid_run": allow_inv})
    else:
        cur_exp = testbed_cfg.guess_experiment(existing) if same_kind else ""
        cur_host = testbed_cfg.guess_host(existing) if same_kind else ""
        c = st.columns(2)
        if kind == "rfsim":
            host = c[0].text_input("Node SSH host (FQDN)", cur_host).strip()
            experiment = None
        else:
            experiment = c[0].text_input("Emulab experiment name", cur_exp).strip()
            host = None
        username = c[1].text_input("SSH username", cur_user).strip()
        dn = (existing.get("dn") or {}) if same_kind else {}
        ex_boxes = (existing.get("ues") or {}).get("boxes", {}) if same_kind else {}
        first_box = next(iter(ex_boxes.values()), {})
        ue_mgen_default = ((existing.get("ues") or {}).get("mgen_dir")
                           if same_kind else None) or \
            testbed_cfg.default_ue_mgen_dir(kind, username)
        with st.expander("Advanced (containers, interface, paths)"):
            dn_container = st.text_input(
                "DN container", dn.get("container", t["dn_container"]), key=f"dnc_{kind}")
            ue_interface = st.text_input(
                "UE interface", first_box.get("interface", t["ue_interface"]),
                key=f"uei_{kind}")
            dn_mgen_dir = st.text_input(
                "DN mgen dir", dn.get("mgen_dir", t["dn_mgen_dir"]), key=f"dnm_{kind}")
            ue_mgen_dir = st.text_input(
                "UE mgen dir", ue_mgen_default, key=f"uem_{kind}")
            ue_container_tpl = None
            if t["has_container"]:
                ue_container_tpl = st.text_input(
                    "UE container pattern ({n} = index)", t["ue_container_tpl"],
                    key=f"uec_{kind}")
        existing_ips = {b: v.get("ip") for b, v in ex_boxes.items()
                        if v.get("ip") and v.get("ip") != testbed_cfg.IP_PLACEHOLDER}
        cfg = testbed_cfg.build(
            kind, username=username, n_ue=n_ue, host=host, experiment=experiment,
            dn_container=dn_container, ue_interface=ue_interface,
            dn_mgen_dir=dn_mgen_dir, ue_mgen_dir=ue_mgen_dir,
            ue_container_tpl=ue_container_tpl,
            flags={"allow_placeholder_hosts": allow_ph,
                   "allow_invalid_run": allow_inv}, existing_ips=existing_ips)

    errs = testbed_cfg.validate(cfg, allow_ph)
    with st.expander("Preview testbed_config.yaml"):
        st.code(yaml.safe_dump(cfg, sort_keys=False), language="yaml")
    for e in errs:
        st.error(e)

    if st.button("Write testbed_config.yaml", disabled=bool(errs)):
        p = testbed_cfg.save(cfg)
        st.success(f"Wrote {p}  (backup at {p.with_suffix('.yaml.bak')}).")
