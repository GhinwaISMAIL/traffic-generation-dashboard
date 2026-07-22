"""Where the dashboard finds the run folders.

Resolution order:
  1. TWINDASH_PROFILES env var
  2. profiles_dir in dashboard_config.yaml (gitignored, machine-specific)
  3. ./traffic_profiles (fallback)

The path is machine-specific (it points into your MGEN repo), so it lives in
dashboard_config.yaml which is gitignored, with a committed .example template.
"""
import os
from pathlib import Path

import yaml


def profiles_dir() -> Path:
    env = os.environ.get("TWINDASH_PROFILES")
    if env:
        return Path(env).expanduser()
    cfg = Path("dashboard_config.yaml")
    if cfg.exists():
        data = yaml.safe_load(cfg.read_text()) or {}
        if data.get("profiles_dir"):
            return Path(data["profiles_dir"]).expanduser()
    return Path("traffic_profiles")


def repo_root() -> Path:
    """The MGEN pipeline repo root — the parent of traffic_profiles/."""
    return profiles_dir().parent


def scenario_config_path() -> Path:
    """scenario_config.yaml at the repo root — the file Notebook 1 reads."""
    return repo_root() / "scenario_config.yaml"


def artifacts_dir() -> Path:
    """artifacts/<app>/{downlink,uplink}/ — source of the discoverable apps."""
    return repo_root() / "artifacts"


def datasets_dir() -> Path:
    """Versioned dataset exports, kept outside individual traffic profiles."""
    return repo_root() / "datasets"
