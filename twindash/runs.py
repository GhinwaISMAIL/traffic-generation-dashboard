"""Find and load run folders. Used by the CLI and the dashboard."""
import json
from pathlib import Path

import pandas as pd

from . import schema


def list_runs(profiles_dir):
    """Newest first."""
    base = Path(profiles_dir)
    return sorted((p for p in base.glob("run_*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def load_config(run_dir):
    cfg = Path(run_dir) / schema.CONFIG
    return json.loads(cfg.read_text()) if cfg.exists() else {}


def load_designed(run_dir):
    p = Path(run_dir) / schema.DESIGNED_KPIS
    return pd.read_parquet(p) if p.exists() else None


def load_observed(run_dir):
    p = Path(run_dir) / schema.OBSERVED_KPIS
    return pd.read_parquet(p) if p.exists() else None
