"""Thin wrappers over the SSH/SCP/docker steps.

These shell out and stream output. The point is that the dashboard and CLI
*call* the testbed steps; they never reimplement the run ordering. Anything
that touches the testbed lives here, reads its hosts from testbed_config.yaml
(gitignored), and stays read-mostly: fetch only copies logs back.
"""
import re
import subprocess
from pathlib import Path

import yaml

from . import dataset, kpis, ric5g, run_profile, schema, settings


def load_testbed_config(path=None):
    if path is None:
        path = settings.repo_root() / "testbed_config.yaml"
    return yaml.safe_load(Path(path).read_text()) or {}


def _run(cmd):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True)


def _node_run_name(run_dir, fallback):
    """On-node mgen run dir name, read from the deploy script (DN_RUN/UE_RUN)."""
    script = Path(run_dir) / "deployment" / "deploy_rfsim.sh"
    if script.exists():
        m = re.search(r'/tmp/mgen_runs/(mgen_[0-9]{8}_[0-9]{6})', script.read_text())
        if m:
            return m.group(1)
    return fallback


def fetch_logs(run_name, run_dir, cfg):
    """Pull DN + UE logs into <run>/logs. Read-only on the testbed side.
    Handles RFsim (single node, logs in containers) and COTS (NUC files)."""
    # Preserve an existing deployment identity.  For a legacy run this action
    # is the first point where the dashboard knows which testbed supplied it.
    run_profile.record(run_dir, cfg, overwrite=False)
    logs = Path(run_dir) / schema.LOGS_DIR
    logs.mkdir(exist_ok=True)
    if ric5g.is_config(cfg):
        # The distributed runner already streams every remote artifact into the
        # local run folder.  Treat this action as a contract check instead of
        # repeating 50+ SSH/SCP operations with stale container assumptions.
        missing = ric5g.missing_logs(run_dir, cfg)
        if missing:
            raise FileNotFoundError(
                "distributed run is incomplete; missing logs: " + ", ".join(missing))
    elif cfg.get("testbed") == "powder_rfsim_docker":
        _fetch_logs_rfsim(run_dir, logs, cfg)
    else:
        _fetch_logs_cots(run_name, logs, cfg)


def _fetch_logs_rfsim(run_dir, logs, cfg):
    node_run = _node_run_name(run_dir, Path(run_dir).name)
    dn_host = cfg["dn"]["ssh_host"]
    dn_ctr = cfg["dn"]["container"]
    dn_dir = cfg["dn"].get("mgen_dir", "/tmp/mgen_runs")
    remote = f"{dn_dir}/{node_run}"
    for name in ("dn_dl_tx.log", "dn_ul_rx.log"):
        _run(["ssh", dn_host,
              f"sudo docker cp {dn_ctr}:{remote}/{name} /tmp/{name} && sudo chmod 644 /tmp/{name}"])
        _run(["scp", f"{dn_host}:/tmp/{name}", str(logs / name)])
    ue_dir = cfg["ues"].get("mgen_dir", "/tmp/mgen_runs")
    ue_remote = f"{ue_dir}/{node_run}"
    for ue_name, box in cfg["ues"]["boxes"].items():
        host = box["ssh_host"]
        ctr = box["container"]
        for suffix in ("dl_rx", "ul_tx"):
            name = f"{ue_name}_{suffix}.log"
            _run(["ssh", host,
                  f"sudo docker cp {ctr}:{ue_remote}/{name} /tmp/{name} && sudo chmod 644 /tmp/{name}"])
            _run(["scp", f"{host}:/tmp/{name}", str(logs / name)])


def _fetch_logs_cots(run_name, logs, cfg):
    dn = cfg["cn5g_ssh_host"]
    remote = f"/tmp/mgen_runs/{run_name}"
    for name in ("dn_dl_tx.log", "dn_ul_rx.log"):
        _run(["ssh", dn, f"sudo docker cp oai-ext-dn:{remote}/{name} /tmp/{name}"])
        _run(["scp", f"{dn}:/tmp/{name}", str(logs / name)])
    for ue in cfg["physical_ues"]:
        host = ue["ssh_host"]
        for suffix in ("dl_rx", "ul_tx"):
            name = f"{ue['name']}_{suffix}.log"
            _run(["scp", f"{host}:mgen_runs/{run_name}/{name}", str(logs / name)])


def run_script(path):
    """Run a generated deploy/start script, streaming its output. This is the
    one safe way to trigger testbed work from the CLI: the script stays the
    single, validated source of truth for the run sequence."""
    return _run(["bash", str(path)])


def _dispatch_experiment(run_dir, cfg, *, on_success=None):
    """Dispatch a run and optionally freeze its outputs before ownership ends."""
    # testbed_config.yaml describes the next run and can change afterwards.
    # Snapshot it before dispatch so Results never depends on today's selection.
    run_profile.record(run_dir, cfg, overwrite=True)
    if ric5g.is_config(cfg):
        return ric5g.run(run_dir, cfg, on_success=on_success)
    result = run_script(Path(run_dir) / "deployment" / "dn_commands.sh")
    if on_success is not None:
        on_success()
    return result


def run_experiment(run_dir, cfg):
    """Dispatch to the topology-specific, validated execution path."""
    return _dispatch_experiment(run_dir, cfg)


def run_and_archive(run_dir, cfg, *, include_raw=True):
    """Run once, build KPIs, and freeze an immutable execution archive.

    For the distributed RIC5G path the success callback runs while the
    deployment lock is still held. A second dashboard/CLI invocation cannot
    overwrite mutable ``logs/`` before its raw MGEN events enter the archive.
    """
    run_dir = Path(run_dir)
    frozen = {}

    def freeze_outputs():
        frozen["observed"] = kpis.save_observed(run_dir)
        frozen["archive"] = dataset.archive_execution(
            run_dir, include_raw=include_raw)

    deployment = _dispatch_experiment(
        run_dir, cfg, on_success=freeze_outputs)
    return deployment, frozen["observed"], frozen["archive"]
