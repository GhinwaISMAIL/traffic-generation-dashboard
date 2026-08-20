"""Author testbed_config.yaml from the Testbed page.

For RFsim the whole file (DN, per-UE boxes, ue_name_map) is generated from the
experiment name + UE count + a few constants, so the dynamic, error-prone parts
(the repeated FQDNs and container names) are produced, not hand-typed.  The
distributed RIC5G profile additionally maps global ``ueN`` names onto cell-local
containers.  UE IPs are placeholders — they are dynamic and resolved live at
deploy.  The previous file is backed up to testbed_config.yaml.bak first.
"""
import re
from datetime import datetime

import yaml

from . import settings

IP_PLACEHOLDER = "12.1.1.x"   # resolved live at deploy; never trusted as-is

TEMPLATES = {
    "ric5g": {
        "testbed": "powder_ric5g_distributed",
        "dn_container": "ric5g-oai-ext-dn",
        "dn_mgen_dir": "/logs/mgen",
        "ue_mgen_dir": "/logs/mgen",
        "ue_interface": "oaitun_ue1",
        "ue_container_tpl": "ric5g-ue-cell{cell}-{ue}",
        "remote_bin": "/local/repository/bin",
        "runner": "deploy_ric5g.sh",
        "has_container": True,
    },
    "rfsim": {
        "testbed": "powder_rfsim_docker",
        "dn_container": "rfsim5g-oai-ext-dn",
        "dn_host_tpl": "{user}@cn.{exp}.emulab.net",
        "dn_mgen_dir": "/tmp/mgen_runs",
        "ue_mgen_dir": "/tmp/mgen_runs",
        "ue_interface": "oaitun_ue1",
        "ue_host_tpl": "{user}@cn.{exp}.emulab.net",       # one node for all UEs
        "ue_container_tpl": "rfsim5g-oai-nr-ue{n}",
        "box_name_tpl": "ue{n}",
        "has_container": True,
    },
    "cots": {
        "testbed": "powder_emulab",
        "dn_container": "oai-ext-dn",
        "dn_host_tpl": "{user}@cn5g-docker-host.{exp}.emulab.net",
        "dn_mgen_dir": "/tmp/mgen_runs",
        "ue_mgen_dir": "/home/{user}/mgen_runs",
        "ue_interface": "qwwan0",
        "ue_host_tpl": "{user}@ota-nuc{n}-cots-ue.{exp}.emulab.net",  # distinct hosts
        "ue_container_tpl": None,
        "box_name_tpl": "nuc{n}",
        "has_container": False,
    },
}

_EXP = re.compile(r"@[^.]+\.([A-Za-z0-9_-]+)\.emulab\.net")


def load() -> dict:
    p = settings.repo_root() / "testbed_config.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def guess_experiment(cfg) -> str:
    """Pull the experiment name out of the DN ssh_host, '' if still a placeholder."""
    host = (cfg.get("dn") or {}).get("ssh_host", "") or ""
    m = _EXP.search(host)
    exp = m.group(1) if m else ""
    return "" if (not exp or "<" in exp) else exp


def guess_host(cfg) -> str:
    """The DN node FQDN (the part after user@), '' if still a placeholder."""
    h = (cfg.get("dn") or {}).get("ssh_host", "") or ""
    host = h.split("@", 1)[1] if "@" in h else h
    return "" if (not host or "<" in host) else host


def default_ue_mgen_dir(kind, username) -> str:
    return f"/home/{username}/mgen_runs" if kind == "cots" else "/tmp/mgen_runs"


def _ssh_host(username: str, host: str) -> str:
    """Accept either a bare FQDN or an already-qualified user@host value."""
    host = (host or "").strip()
    return host if "@" in host else f"{username}@{host}"


def ric5g_hosts(cfg) -> dict:
    """Return the editable bare hosts from an existing distributed config."""
    nodes = cfg.get("nodes") or {}

    def bare(value):
        value = value or ""
        return value.split("@", 1)[1] if "@" in value else value

    out = {"core": bare((nodes.get("core") or {}).get("ssh_host"))}
    for cell in nodes.get("cells") or []:
        out[f"cell{cell.get('cell')}"] = bare(cell.get("ssh_host"))
    return out


def build_ric5g(*, username, n_ue, core_host, cell_hosts,
                ues_per_cell=12, nb_id_start=3584,
                dn_container="ric5g-oai-ext-dn",
                ue_container_tpl="ric5g-ue-cell{cell}-{ue}",
                ue_interface="oaitun_ue1",
                remote_bin="/local/repository/bin",
                runner="deploy_ric5g.sh", xapp_enabled=True,
                xapp_delay_s=270, xapp_window_s=60, flags=None):
    """Build the configuration for the three-node RIC5G POWDER profile.

    ``ue1..ueN`` remain the stable names used by generated MGEN scripts.  Each
    entry records its cell and cell-local UE index so the orchestration layer
    never has to infer that mapping from a hostname or an IP address.
    """
    flags = flags or {}
    ues_per_cell = int(ues_per_cell)
    cell_hosts = list(cell_hosts)
    if not 1 <= len(cell_hosts) <= 3:
        raise ValueError("RIC5G requires between 1 and 3 cell hosts")
    nodes = []
    for index, host in enumerate(cell_hosts, start=1):
        nodes.append({
            "name": f"cell{index}",
            "cell": index,
            "ssh_host": _ssh_host(username, host),
            "nb_id": int(nb_id_start) + index - 1,
            "gnb_container": f"ric5g-gnb-cell{index}",
        })

    boxes, name_map = {}, {}
    for global_index in range(1, int(n_ue) + 1):
        cell = (global_index - 1) // ues_per_cell + 1
        local_ue = (global_index - 1) % ues_per_cell + 1
        host = cell_hosts[cell - 1] if cell <= len(cell_hosts) else ""
        name = f"ue{global_index}"
        boxes[name] = {
            "ssh_host": _ssh_host(username, host),
            "container": ue_container_tpl.format(cell=cell, ue=local_ue,
                                                   n=global_index),
            "cell": cell,
            "ue_index": local_ue,
            "nb_id": int(nb_id_start) + cell - 1,
            "ip": IP_PLACEHOLDER,
            "interface": ue_interface,
        }
        name_map[name] = name

    return {
        "schema_version": 2,
        "testbed": TEMPLATES["ric5g"]["testbed"],
        "nodes": {
            "core": {"ssh_host": _ssh_host(username, core_host)},
            "cells": nodes,
        },
        "dn": {
            "container": dn_container,
            "ssh_host": _ssh_host(username, core_host),
            "ip": "192.168.72.135",
            "mgen_dir": "/logs/mgen",
        },
        "ues": {
            "username": username,
            "ues_per_cell": ues_per_cell,
            "mgen_dir": "/logs/mgen",
            "boxes": boxes,
        },
        "ue_name_map": name_map,
        "mgen": {
            "remote_bin": remote_bin,
            "ul_port": 5000,
            "dl_port": 5001,
        },
        "clock": {
            "guard_enabled": True,
            "ntp_server": "155.98.33.74",
            "max_abs_offset_ms": 5.0,
            "max_offset_spread_ms": 5.0,
            "max_jitter_ms": 1.0,
            "sync_timeout_s": 90.0,
        },
        "ric": {
            "e2_port": 36421,
            "e42_port": 36422,
            "expected_e2_nodes": len(cell_hosts),
        },
        "xapp": {
            "enabled": bool(xapp_enabled),
            "expected_subscriptions": 4 * len(cell_hosts),
            "delay_s": int(xapp_delay_s),
            "window_s": int(xapp_window_s),
        },
        "runner": {"script": runner},
        "flags": flags,
    }


def build(kind, *, username, n_ue, host=None, experiment=None,
          dn_container, ue_interface, dn_mgen_dir, ue_mgen_dir,
          ue_container_tpl=None, flags, existing_ips=None):
    """RFsim: one node host for the DN and every UE (host=pcXXX.emulab.net).
    COTS: per-box FQDNs generated from the experiment name."""
    existing_ips = existing_ips or {}
    t = TEMPLATES[kind]

    def ue_host(i):
        return (f"{username}@{host}" if kind == "rfsim"
                else t["ue_host_tpl"].format(user=username, exp=experiment, n=i))

    dn_host = (f"{username}@{host}" if kind == "rfsim"
               else t["dn_host_tpl"].format(user=username, exp=experiment))

    boxes, name_map = {}, {}
    for i in range(1, n_ue + 1):
        box = t["box_name_tpl"].format(n=i)
        entry = {"ssh_host": ue_host(i)}
        if t["has_container"]:
            entry["container"] = (ue_container_tpl or t["ue_container_tpl"]).format(n=i)
        entry["ip"] = existing_ips.get(box, IP_PLACEHOLDER)
        entry["interface"] = ue_interface
        boxes[box] = entry
        name_map[f"ue{i}"] = box

    return {
        "testbed": t["testbed"],
        "dn": {"container": dn_container, "ssh_host": dn_host, "mgen_dir": dn_mgen_dir},
        "ues": {"username": username, "mgen_dir": ue_mgen_dir, "boxes": boxes},
        "ue_name_map": name_map,
        "flags": flags,
    }


def validate(cfg, allow_placeholder) -> list:
    errs = []
    if cfg.get("testbed") == TEMPLATES["ric5g"]["testbed"]:
        nodes = cfg.get("nodes") or {}
        cells = nodes.get("cells") or []
        named_hosts = [("core", (nodes.get("core") or {}).get("ssh_host"))]
        named_hosts += [(f"cell{c.get('cell')}", c.get("ssh_host"))
                        for c in cells]
        if not 1 <= len(cells) <= 3:
            errs.append("RIC5G requires between 1 and 3 cell hosts")
        cell_ids = [c.get("cell") for c in cells]
        if cell_ids != list(range(1, len(cells) + 1)):
            errs.append("cell indices must be consecutive starting at 1")
        if not allow_placeholder:
            for name, host in named_hosts:
                after = (host or "").split("@", 1)[-1]
                if not after or "<" in after:
                    errs.append(f"{name} SSH host is not set")
        hosts = [host for _, host in named_hosts if host]
        if len(hosts) != len(set(hosts)):
            errs.append("core and cell SSH hosts must be distinct")

        boxes = (cfg.get("ues") or {}).get("boxes") or {}
        capacity = len(cells) * int(
            (cfg.get("ues") or {}).get("ues_per_cell", 0))
        if len(boxes) > capacity:
            errs.append(f"{len(boxes)} UEs exceed the configured cell capacity ({capacity})")
        nb_ids = [c.get("nb_id") for c in cells]
        if len(nb_ids) != len(set(nb_ids)):
            errs.append("cell nb_id values must be unique")
        if (cfg.get("ric") or {}).get("expected_e2_nodes") != len(cells):
            errs.append("expected_e2_nodes must equal the number of cells")
        expected_subscriptions = 4 * len(cells)
        if (cfg.get("xapp") or {}).get("expected_subscriptions") != expected_subscriptions:
            errs.append(
                f"expected_subscriptions must be {expected_subscriptions} "
                f"for {len(cells)} cell(s)")
        clock = cfg.get("clock") or {}
        if clock.get("guard_enabled", False):
            for field in (
                "max_abs_offset_ms", "max_offset_spread_ms",
                "max_jitter_ms", "sync_timeout_s",
            ):
                try:
                    valid = float(clock.get(field, 0)) > 0
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    errs.append(f"clock {field} must be positive")
            if not str(clock.get("ntp_server") or "").strip():
                errs.append("clock ntp_server is required")
    else:
        host = cfg["dn"]["ssh_host"]
        after = host.split("@", 1)[1] if "@" in host else host
        if (not after) or ("<" in after):
            if not allow_placeholder:
                errs.append("SSH host not set — enter your node FQDN "
                            "(e.g. pc712.emulab.net), or tick "
                            "allow_placeholder_hosts to pre-stage")
    if not cfg.get("ue_name_map"):
        errs.append("no UEs — set n_ue on the Design page")
    return errs


def save(cfg):
    """Back up the existing file, then write the new one. Returns the path."""
    p = settings.repo_root() / "testbed_config.yaml"
    if p.exists():
        p.with_suffix(".yaml.bak").write_text(p.read_text())
    header = (f"# testbed_config.yaml — written by the twindash Testbed page "
              f"{datetime.now():%Y-%m-%d %H:%M}\n"
              "# UE IPs are placeholders — resolved live at deploy.\n\n")
    p.write_text(header + yaml.safe_dump(cfg, sort_keys=False))
    return p
