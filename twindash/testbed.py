"""Thin wrappers over the SSH/SCP/docker steps.

These shell out and stream output. The point is that the dashboard and CLI
*call* the testbed steps; they never reimplement the run ordering. Anything
that touches the testbed lives here, reads its hosts from testbed_config.yaml
(gitignored), and stays read-mostly: fetch only copies logs back.
"""
import subprocess
from pathlib import Path

import yaml

from . import schema


def load_testbed_config(path="testbed_config.yaml"):
    return yaml.safe_load(Path(path).read_text())


def _run(cmd):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True)


def fetch_logs(run_name, run_dir, cfg):
    """Pull DN + UE logs into <run>/logs. Read-only on the testbed side."""
    logs = Path(run_dir) / schema.LOGS_DIR
    logs.mkdir(exist_ok=True)
    dn = cfg["cn5g_ssh_host"]
    remote = f"/tmp/mgen_runs/{run_name}"

    # DN logs live inside the container: docker cp out, then scp back
    for name in ("dn_dl_tx.log", "dn_ul_rx.log"):
        _run(["ssh", dn, f"sudo docker cp oai-ext-dn:{remote}/{name} /tmp/{name}"])
        _run(["scp", f"{dn}:/tmp/{name}", str(logs / name)])

    # UE logs are on the NUC filesystem directly
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
