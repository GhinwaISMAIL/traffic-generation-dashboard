"""Author testbed_config.yaml from the Testbed page.

For RFsim the whole file (DN, per-UE boxes, ue_name_map) is generated from the
experiment name + UE count + a few constants, so the dynamic, error-prone parts
(the repeated FQDNs and container names) are produced, not hand-typed. UE IPs are
written as placeholders — they're dynamic and resolved live at deploy. The
previous file is backed up to testbed_config.yaml.bak first.
"""
import re
from datetime import datetime

import yaml

from . import settings

IP_PLACEHOLDER = "12.1.1.x"   # resolved live at deploy; never trusted as-is

TEMPLATES = {
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
    host = cfg["dn"]["ssh_host"]
    after = host.split("@", 1)[1] if "@" in host else host
    if (not after) or ("<" in after):
        if not allow_placeholder:
            errs.append("SSH host not set — enter your node FQDN (e.g. pc712.emulab.net), "
                        "or tick allow_placeholder_hosts to pre-stage")
    if not cfg["ue_name_map"]:
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
