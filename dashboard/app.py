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
from twindash import runs, kpis, testbed, settings, bursts, realized, scenario, testbed_cfg  # noqa: E402
import results_view  # noqa: E402

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

    c1, c2 = st.columns(2)
    if c1.button("Fetch logs from testbed"):
        with st.spinner("Copying logs back…"):
            testbed.fetch_logs(run, run_dir, testbed.load_testbed_config())
            kpis.save_observed(run_dir)
        st.success("Fetched and rebuilt observed KPIs.")
    if c2.button("Rebuild KPIs from existing logs"):
        kpis.save_observed(run_dir)
        st.success("Rebuilt.")

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
    sc = st.columns(3)
    duration = sc[0].number_input("Duration (s)", 30, 7200,
                                  int(base["simulation"]["duration"]))
    n_ue = sc[1].number_input("n_ue", 1, 32, int(base["simulation"]["n_ue"]))
    seed = sc[2].number_input("random_seed", 0, 1_000_000,
                              int(base["simulation"]["random_seed"]))

    st.subheader("Apps")
    default_apps = [a for a in base["apps"] if a in app_options] or app_options
    apps = st.multiselect("Apps (discovered from artifacts/)", app_options,
                          default=default_apps)

    st.subheader("User classes")
    st.caption("Realism templates learned from your data — their DL/UL split and "
               "cluster weights feed each profile's `base`. The counts below only "
               "assign UEs when no profiles are defined.")
    d = base["user_classes"]["distribution"]
    uc = st.columns(3)
    heavy = uc[0].number_input("heavy", 0, 32, int(d.get("heavy", 0)))
    medium = uc[1].number_input("medium", 0, 32, int(d.get("medium", 0)))
    light = uc[2].number_input("light", 0, 32, int(d.get("light", 0)))

    st.subheader("Profiles")
    existing_profiles = base.get("profiles") or []
    use_profiles = st.toggle(
        "Use profiles", value=bool(existing_profiles),
        help="When on, the table below assigns UEs. When off, the class counts "
             "above assign UEs and no profiles are written.")

    profiles = []
    if use_profiles:
        st.caption("Each row assigns UEs (count), a flow target, a realism `base`, "
                   "and an app mix (relative weights; 0 excludes the app). Add rows "
                   "with the + at the bottom of the table. Counts must sum to n_ue.")
        rows = []
        for p in existing_profiles:
            f = p.get("flows", 10)
            row = {"name": p.get("name", ""), "count": int(p.get("count", 0)),
                   "base": p.get("base", "medium"),
                   "flows": int(f[0] if isinstance(f, (list, tuple)) else f)}
            mix = p.get("app_mix") or {}
            for a in apps:
                row[a] = float(mix.get(a, 0))
            rows.append(row)
        prof_df = pd.DataFrame(rows, columns=["name", "count", "base", "flows"] + apps)
        edited = st.data_editor(
            prof_df, num_rows="dynamic", use_container_width=True, hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("name"),
                "count": st.column_config.NumberColumn("count", min_value=0, step=1),
                "base": st.column_config.SelectboxColumn(
                    "base", options=["heavy", "medium", "light"]),
                "flows": st.column_config.NumberColumn("flows", min_value=1, step=1),
                **{a: st.column_config.NumberColumn(a, min_value=0.0, step=1.0)
                   for a in apps},
            },
            key=f"profiles_editor_{'_'.join(apps)}")

        for _, r in edited.iterrows():
            nm = str(r.get("name") or "").strip()
            if not nm or nm.lower() == "nan":
                continue
            mix = {a: float(r[a]) for a in apps
                   if not pd.isna(r.get(a)) and float(r.get(a)) > 0}
            profiles.append({
                "name": nm,
                "count": 0 if pd.isna(r.get("count")) else int(r.get("count")),
                "base": "medium" if pd.isna(r.get("base")) else str(r.get("base")),
                "flows": 1 if pd.isna(r.get("flows")) else int(r.get("flows")),
                "app_mix": mix,
            })

        ptot = sum(p["count"] for p in profiles)
        (st.success if ptot == n_ue else st.error)(
            f"profile counts sum to {ptot} — must equal n_ue ({n_ue})")
    else:
        st.caption("Profiles off — UEs are assigned by the class counts above.")
        total = heavy + medium + light
        (st.success if total == n_ue else st.error)(
            f"user-class counts sum to {total} — must equal n_ue ({n_ue})")

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
    new_cfg["simulation"] = {"duration": int(duration), "n_ue": int(n_ue),
                             "random_seed": int(seed)}
    new_cfg["apps"] = apps
    new_cfg["user_classes"]["distribution"] = {"heavy": int(heavy),
                                               "medium": int(medium),
                                               "light": int(light)}
    if profiles:
        new_cfg["profiles"] = profiles
    else:
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
               "For RFsim the UE boxes and ue_name_map are generated from the "
               "experiment name and UE count. UE IPs are placeholders — resolved "
               "live at deploy. Previous file backed up to .bak.")

    existing = testbed_cfg.load()
    kind_default = "cots" if existing.get("testbed") == "powder_emulab" else "rfsim"
    kind_label = st.selectbox(
        "Testbed type",
        ["RFsim (Docker, single node)", "COTS (physical UEs)"],
        index=0 if kind_default == "rfsim" else 1)
    kind = "rfsim" if kind_label.startswith("RFsim") else "cots"
    t = testbed_cfg.TEMPLATES[kind]

    # selected type matches the existing file's type? only then prefill from it
    same_kind = existing.get("testbed") == t["testbed"]
    cur_user = (existing.get("ues") or {}).get("username", "ghinwa")
    cur_exp = testbed_cfg.guess_experiment(existing)
    # selected type matches the existing file's type? only then prefill from it
    same_kind = existing.get("testbed") == t["testbed"]
    cur_user = (existing.get("ues") or {}).get("username", "ghinwa")
    cur_exp = testbed_cfg.guess_experiment(existing) if same_kind else ""
    cur_host = testbed_cfg.guess_host(existing) if same_kind else ""
    scen_n_ue = scenario.merged()["simulation"]["n_ue"]

    c = st.columns(2)
    if kind == "rfsim":
        host = c[0].text_input(
            "Node SSH host (FQDN)", cur_host,
            help="The pcXXX.emulab.net node from your POWDER experiment — it "
                 "changes each time you instantiate, e.g. pc712.emulab.net").strip()
        experiment = None
    else:
        experiment = c[0].text_input("Emulab experiment name", cur_exp,
                                     help="Replaces <experiment> in every FQDN").strip()
        host = None
    username = c[1].text_input("SSH username", cur_user).strip()
    n_ue = int(scen_n_ue)
    st.info(f"Generating {n_ue} UEs to match scenario_config.yaml. The UE count is "
            "set on the Design page — this follows it automatically.")

    # defaults follow the SELECTED type; prefill from the file only if it's the same type
    dn = (existing.get("dn") or {}) if same_kind else {}
    ex_boxes = (existing.get("ues") or {}).get("boxes", {}) if same_kind else {}
    first_box = next(iter(ex_boxes.values()), {})
    ue_mgen_default = ((existing.get("ues") or {}).get("mgen_dir") if same_kind else None) \
        or testbed_cfg.default_ue_mgen_dir(kind, username)
    with st.expander("Advanced (containers, interface, paths)"):
        dn_container = st.text_input("DN container", dn.get("container", t["dn_container"]),
                                     key=f"dnc_{kind}")
        ue_interface = st.text_input("UE interface", first_box.get("interface", t["ue_interface"]),
                                     key=f"uei_{kind}")
        dn_mgen_dir = st.text_input("DN mgen dir", dn.get("mgen_dir", t["dn_mgen_dir"]),
                                    key=f"dnm_{kind}")
        ue_mgen_dir = st.text_input("UE mgen dir", ue_mgen_default, key=f"uem_{kind}")
        ue_container_tpl = None
        if t["has_container"]:
            ue_container_tpl = st.text_input("UE container pattern ({n} = index)",
                                             t["ue_container_tpl"], key=f"uec_{kind}")

    fc = st.columns(2)
    allow_ph = fc[0].checkbox("allow_placeholder_hosts",
                              value=(existing.get("flags") or {}).get("allow_placeholder_hosts", False))
    allow_inv = fc[1].checkbox("allow_invalid_run",
                               value=(existing.get("flags") or {}).get("allow_invalid_run", False))

    existing_ips = {b: v.get("ip") for b, v in ex_boxes.items()
                    if v.get("ip") and v.get("ip") != testbed_cfg.IP_PLACEHOLDER}

    cfg = testbed_cfg.build(
        kind, username=username, n_ue=n_ue, host=host, experiment=experiment,
        dn_container=dn_container, ue_interface=ue_interface,
        dn_mgen_dir=dn_mgen_dir, ue_mgen_dir=ue_mgen_dir,
        ue_container_tpl=ue_container_tpl,
        flags={"allow_placeholder_hosts": allow_ph, "allow_invalid_run": allow_inv},
        existing_ips=existing_ips)

    errs = testbed_cfg.validate(cfg, allow_ph)
    with st.expander("Preview testbed_config.yaml"):
        st.code(yaml.safe_dump(cfg, sort_keys=False), language="yaml")
    for e in errs:
        st.error(e)

    if st.button("Write testbed_config.yaml", disabled=bool(errs)):
        p = testbed_cfg.save(cfg)
        st.success(f"Wrote {p}  (backup at {p.with_suffix('.yaml.bak')}).")
