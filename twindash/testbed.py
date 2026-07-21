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

from . import schema, settings


def load_testbed_config(path=None):
    if path is None:
        path = settings.repo_root() / "testbed_config.yaml"
    return yaml.safe_load(Path(path).read_text())


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
    logs = Path(run_dir) / schema.LOGS_DIR
    logs.mkdir(exist_ok=True)
    if cfg.get("testbed") == "powder_rfsim_docker":
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
