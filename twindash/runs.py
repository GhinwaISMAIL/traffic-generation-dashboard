"""Find and load run folders. Used by the CLI and the dashboard."""
import json
from pathlib import Path

import pandas as pd

from . import schema


def list_runs(profiles_dir):
    """Newest first."""
    base = Path(profiles_dir)
    return sorted((p for p in base.glob("run_*") if p.is_dir() and not schedule_pending(p)),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def schedule_pending(run_dir):
    path = Path(run_dir)
    has_schedule = (path / "scenario_schedule.json").exists()
    try:
        config = load_config(path)
    except (OSError, ValueError):
        return has_schedule
    if not isinstance(config, dict):
        return has_schedule
    return (has_schedule or config.get("scenario_mode") == "schedule") and (
        config.get("generation_status") != "complete")


def require_generated(run_dir):
    if schedule_pending(run_dir):
        raise ValueError("Schedule is imported; generate application bursts and MGEN scripts "
                         "before deploying or fetching logs.")


def load_config(run_dir):
    cfg = Path(run_dir) / schema.CONFIG
    return json.loads(cfg.read_text()) if cfg.exists() else {}


def load_designed(run_dir):
    p = Path(run_dir) / schema.DESIGNED_KPIS
    return pd.read_parquet(p) if p.exists() else None


def load_observed(run_dir):
    p = Path(run_dir) / schema.OBSERVED_KPIS
    return pd.read_parquet(p) if p.exists() else None
