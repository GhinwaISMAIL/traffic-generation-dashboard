"""Adapter for the distributed ``oai-5g-ric`` POWDER profile.

The proven shell runner remains the single source of truth for remote ordering.
This module only validates the local contract, translates ``testbed_config.yaml``
into runner environment variables, records the runner output, and verifies the
artifacts that the dashboard consumes afterwards.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import subprocess
from pathlib import Path

from . import schema, settings, testbed_cfg

TESTBED = "powder_ric5g_distributed"


def is_config(cfg: dict) -> bool:
    return (cfg or {}).get("testbed") == TESTBED


def runner_path(cfg: dict) -> Path:
    value = ((cfg.get("runner") or {}).get("script") or "deploy_ric5g.sh")
    path = Path(value).expanduser()
    return path if path.is_absolute() else settings.repo_root() / path


def duration_s(run_dir) -> int:
    config = Path(run_dir) / schema.CONFIG
    if config.exists():
        try:
            data = json.loads(config.read_text())
            value = data.get("simulation_duration")
            if value is None:
                value = (data.get("simulation") or {}).get("duration")
            if value is not None:
                return int(value)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return 600


def validate_local(run_dir, cfg: dict) -> list[str]:
    """Validate everything that does not require contacting the testbed."""
    run_dir = Path(run_dir)
    cfg = cfg or {}
    errors = testbed_cfg.validate(
        cfg, bool((cfg.get("flags") or {}).get("allow_placeholder_hosts")))
    script = runner_path(cfg)
    if not script.is_file():
        errors.append(f"distributed runner not found: {script}")
    scripts = run_dir / schema.SCRIPTS_DIR
    for name in ("dn_dl_tx.mgn", "dn_ul_rx.mgn", "flow_batch_map.csv"):
        if not (scripts / name).is_file():
            errors.append(f"run is missing {schema.SCRIPTS_DIR}/{name}")
    ul_ues = {path.name.removesuffix("_ul_tx.mgn")
              for path in scripts.glob("ue*_ul_tx.mgn")}
    dl_ues = {path.name.removesuffix("_dl_rx.mgn")
              for path in scripts.glob("ue*_dl_rx.mgn")}
    if not ul_ues:
        errors.append("run has no ue*_ul_tx.mgn scripts")
    if ul_ues != dl_ues:
        errors.append(
            "UE sender/receiver scripts do not match: "
            f"UL-only={sorted(ul_ues - dl_ues)}; "
            f"DL-only={sorted(dl_ues - ul_ues)}")
    configured_ues = set(((cfg.get("ues") or {}).get("boxes") or {}).keys())
    if configured_ues and ul_ues != configured_ues:
        errors.append(
            "run UE scripts do not match testbed_config.yaml: "
            f"scripts-only={sorted(ul_ues - configured_ues)}; "
            f"config-only={sorted(configured_ues - ul_ues)}")
    xapp = cfg.get("xapp") or {}
    if xapp.get("enabled", True):
        end = int(xapp.get("delay_s", 0)) + int(xapp.get("window_s", 0))
        duration = duration_s(run_dir)
        if end > duration:
            errors.append(
                f"xApp window ends at {end}s, after the {duration}s traffic run")
    return errors


def environment(cfg: dict) -> dict[str, str]:
    """Environment consumed by ``deploy_ric5g.sh``."""
    nodes = cfg.get("nodes") or {}
    cells = sorted(nodes.get("cells") or [], key=lambda c: int(c["cell"]))
    xapp = cfg.get("xapp") or {}
    ues = cfg.get("ues") or {}
    env = {
        "CORE_HOST": (nodes.get("core") or {}).get("ssh_host", ""),
        "NUM_CELLS": str(len(cells)),
        "UES_PER_CELL": str(ues.get("ues_per_cell", 12)),
        "XAPP": "1" if xapp.get("enabled", True) else "0",
        "XAPP_SUBS": str(xapp.get("expected_subscriptions", 4 * len(cells))),
        "XAPP_DELAY": str(xapp.get("delay_s", 270)),
        "XAPP_WINDOW": str(xapp.get("window_s", 60)),
        "REMOTE_BIN": str((cfg.get("mgen") or {}).get(
            "remote_bin", "/local/repository/bin")),
        "DN_CONTAINER": str((cfg.get("dn") or {}).get(
            "container", "ric5g-oai-ext-dn")),
    }
    if cells:
        env["NB_ID_START"] = str(min(int(cell["nb_id"]) for cell in cells))
    for cell in cells:
        env[f"CELL{int(cell['cell'])}_HOST"] = cell.get("ssh_host", "")
    return env


@contextmanager
def _deployment_lock(logs: Path):
    """Allow only one deployment process to own a run at a time."""
    target = logs / "deployment.lock"
    stream = target.open("a+")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "this run is already being deployed by another dashboard "
                "session or CLI process") from None
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid()}) + "\n")
        stream.flush()
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def run(run_dir, cfg: dict, *, on_success=None):
    """Run the distributed experiment synchronously and tee output to the run.

    Streamlit can call this under a spinner; the CLI streams the same lines to
    its terminal.  Remote execution remains inside ``deploy_ric5g.sh``.
    """
    errors = validate_local(run_dir, cfg)
    if errors:
        raise ValueError("; ".join(errors))

    run_dir = Path(run_dir).resolve()
    logs = run_dir / schema.LOGS_DIR
    logs.mkdir(exist_ok=True)
    deployment_log = logs / "deployment.log"
    cmd = ["bash", str(runner_path(cfg)), str(run_dir), str(duration_s(run_dir))]
    child_env = os.environ.copy()
    child_env.update(environment(cfg))

    with _deployment_lock(logs), deployment_log.open("w") as output:
        process = subprocess.Popen(
            cmd, cwd=settings.repo_root(), env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            output.write(line)
            output.flush()
        rc = process.wait()
        if rc:
            raise subprocess.CalledProcessError(rc, cmd)
        if on_success is not None:
            on_success()
    return deployment_log


def expected_logs(run_dir, cfg: dict) -> set[str]:
    scripts = Path(run_dir) / schema.SCRIPTS_DIR
    expected = {"dn_dl_tx.log", "dn_ul_rx.log", "rnti_map.csv",
                "run_timing.json"}
    for path in scripts.glob("ue*_ul_tx.mgn"):
        ue = path.name.removesuffix("_ul_tx.mgn")
        expected.update({f"{ue}_ul_tx.log", f"{ue}_dl_rx.log"})
    if (cfg.get("xapp") or {}).get("enabled", True):
        expected.update({"prb_by_second.csv", "xapp.log"})
    schedule = Path(run_dir) / schema.CHANNEL_SCHEDULE
    if schedule.exists():
        try:
            if json.loads(schedule.read_text()).get("enabled", True):
                expected.add("channel_state.json")
        except (OSError, ValueError, json.JSONDecodeError):
            # The runner will report the malformed schedule more precisely.
            expected.add("channel_state.json")
    return expected


def missing_logs(run_dir, cfg: dict) -> list[str]:
    logs = Path(run_dir) / schema.LOGS_DIR
    return sorted(name for name in expected_logs(run_dir, cfg)
                  if not (logs / name).is_file())
