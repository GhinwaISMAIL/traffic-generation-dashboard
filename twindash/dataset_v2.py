"""Reconstructable Dataset Contract V2.

Packet outcomes are the lossless measurement layer.  Per-second observations,
channel segments, and the segment-level modelling table are deterministic
derivatives.  Keeping the packet layer makes non-additive statistics such as
p95 latency rebuildable at any later segment boundary.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from . import kpis, prb, run_profile, schema


QUARANTINED_RADIO_COLUMNS = (
    "wb_cqi_avg", "ul_bler_avg", "bsr_avg", "phr_avg",
)


def model_contract() -> dict:
    """Return the peer-reviewable field-role contract for schema V2."""
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "split_unit": "execution_id",
        "training_grain": (
            "execution_id, ue, direction, verified half-open channel segment"
        ),
        "tables": {
            schema.PACKET_OUTCOMES: "one row per transmitted packet",
            schema.UE_APP_SECOND_OBSERVED: (
                "one row per execution, UE, application, direction, UTC second"
            ),
            schema.CHANNEL_SEGMENTS: (
                "one row per execution, UE, direction, half-open channel interval"
            ),
            schema.SEGMENT_TRAINING_TABLE: (
                "one row per execution, UE, direction, channel segment"
            ),
        },
        "roles": {
            "pre_run_features": [
                "ue_class", "app_mix", "designed_offered_mbps",
                "parameter", "requested_value",
            ],
            "conditional_pre_run_features": {
                "applied_value": (
                    "usable only when verified=true and channel_agreement=true"
                ),
            },
            "targets": [
                "latency_ms_p50", "latency_ms_p95", "loss_rate",
                "received_mbps",
            ],
            "post_run_diagnostics": [
                "dl_mcs1_avg_segment_mean", "ul_mcs1_avg_segment_mean",
                "dl_prb_segment_mean", "ul_prb_segment_mean",
                "dl_bler_avg_segment_mean", "pusch_snr_avg_segment_mean",
                "pucch_snr_avg_segment_mean",
            ],
            "identity_only": [
                "execution_id", "profile_id", "segment_id", "cell", "ue",
                "ue_index", "nb_id", "rnti", "direction",
            ],
            "quality_only": [
                "verified", "channel_agreement", "packet_evidence",
                "model_mapping_valid", "valid_clock_fraction", "radio_samples",
                "source_wall_ratio", "radio_join_clock",
                "radio_clock_lag_samples", "radio_clock_lag_s_segment_mean",
                "radio_clock_lag_s_segment_p95", "radio_clock_lag_s_segment_max",
                "radio_clock_lag_warning", "training_eligible",
            ],
        },
        "radio_clock_policy": {
            "cross_system_join_clock": "core receipt UTC",
            "source_clock_role": "radio ordering and provenance only",
            "segment_radio_aggregates": "receipt-clock aligned post-run diagnostics",
            "lag_warning_threshold_s": prb.RADIO_LAG_WARNING_S,
            "near_realtime_source_wall_ratio": list(
                prb.REALTIME_SOURCE_WALL_RATIO),
            "interpretation": (
                "when radio_clock_lag_warning=true, do not interpret segment radio "
                "means as instantaneous causal responses to the current channel "
                "value or use them as pre-run model inputs"
            ),
        },
        "quarantined_fields": {
            name: "observed values were dead, broken, or scientifically suspect"
            for name in QUARANTINED_RADIO_COLUMNS
        },
        "rules": [
            "segment latency percentiles are computed directly from packet rows",
            "DL and UL are never combined",
            "UL is uncontrolled unless a verified stable UL mapping exists",
            "post-run radio outcomes are excluded from pre-run X",
            "radio segment means use core receipt time and carry explicit lag provenance",
            "train/validation/test splits are assigned by execution_id",
        ],
    }


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_value(left, right) -> bool:
    a, b = _number(left), _number(right)
    if a is not None and b is not None:
        return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
    return left is not None and right is not None and str(left) == str(right)


def _execution_id(run_dir: Path) -> str:
    timing = _json(run_dir / schema.LOGS_DIR / "run_timing.json", {}) or {}
    value = str(timing.get("run_id") or "").strip()
    if not value:
        raise ValueError("logs/run_timing.json has no run_id")
    return value


def _profile_id(run_dir: Path) -> str:
    metadata = _json(run_dir / schema.EXECUTION_METADATA, {}) or {}
    if metadata.get("profile_id"):
        return str(metadata["profile_id"])
    config = _json(run_dir / schema.CONFIG, {}) or {}
    return str(config.get("run_name") or run_dir.name)


def _manifest(run_dir: Path) -> pd.DataFrame:
    path = run_dir / schema.SCRIPTS_DIR / "manifest.csv"
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _ue_map(run_dir: Path) -> pd.DataFrame:
    manifest = _manifest(run_dir)
    if manifest.empty or "ue_name" not in manifest:
        return pd.DataFrame(columns=["ue", "ue_class"])
    columns = [c for c in ("ue_name", "ue_class") if c in manifest]
    result = manifest[columns].rename(columns={"ue_name": "ue"})
    if "ue_class" not in result:
        result["ue_class"] = pd.NA
    result = result.drop_duplicates("ue")

    mapping_path = run_dir / schema.LOGS_DIR / "rnti_map.csv"
    if mapping_path.exists():
        mapping = pd.read_csv(mapping_path)
        if "ue" not in mapping and "ue_name" in mapping:
            mapping = mapping.rename(columns={"ue_name": "ue"})
        wanted = [c for c in ("ue", "cell", "ue_index", "nb_id", "rnti")
                  if c in mapping]
        if "ue" in wanted:
            result = result.merge(mapping[wanted].drop_duplicates("ue"),
                                  on="ue", how="left")
    return result


def _designed_load(run_dir: Path) -> pd.DataFrame:
    path = run_dir / schema.DESIGNED_KPIS
    designed = pd.read_parquet(path) if path.exists() else kpis.build_designed(run_dir)
    if designed.empty:
        return pd.DataFrame(columns=["ue", "app", "direction", "designed_offered_mbps"])
    if "app" not in designed:
        designed["app"] = "unknown"
    keys = [column for column in ("ue", "app", "direction", "flow_id")
            if column in designed]
    result = designed.groupby(keys, as_index=False)["proj_throughput_bps"].sum()
    result["designed_offered_mbps"] = result.pop("proj_throughput_bps") / 1e6
    return result


def packet_outcomes(run_dir) -> pd.DataFrame:
    """Return one immutable row per transmitted packet."""
    run_dir = Path(run_dir)
    sent, received = kpis._load_events(run_dir)  # shared parser, one clock contract
    flow_map = kpis._flow_map(run_dir)
    if sent.empty:
        return pd.DataFrame()

    key = ["direction", "flow_id", "seq"]
    if sent.duplicated(key).any():
        examples = sent.loc[sent.duplicated(key, keep=False), key].head().to_dict("records")
        raise ValueError(f"duplicate transmitted packet keys: {examples}")

    metadata = [c for c in ("flow_id", "direction", "ue", "app") if c in flow_map]
    sent = sent.merge(flow_map[metadata].drop_duplicates(["flow_id", "direction"]),
                      on=["flow_id", "direction"], how="left", validate="many_to_one")
    if sent["ue"].isna().any():
        raise ValueError("one or more transmitted packets have no flow-map UE")

    receive_columns = key + (["utc_time"] if "utc_time" in received else [])
    if received.empty:
        receives = pd.DataFrame(columns=key + ["received_time_utc", "duplicate_receives"])
    else:
        unknown = received.merge(flow_map[["flow_id", "direction"]].drop_duplicates(),
                                 on=["flow_id", "direction"], how="left", indicator=True)
        if (unknown["_merge"] == "left_only").any():
            raise ValueError("one or more received packets have no flow-map entry")
        receives = received[receive_columns].copy()
        receives = receives.sort_values("utc_time")
        duplicate_counts = (receives.groupby(key).size() - 1).rename(
            "duplicate_receives")
        receives = receives.drop_duplicates(key, keep="first").merge(
            duplicate_counts, on=key, how="left")
        receives = receives.rename(columns={"utc_time": "received_time_utc"})

    packets = sent.merge(receives, on=key, how="left", validate="one_to_one")
    packets = packets.rename(columns={
        "seq": "sequence", "size": "size_bytes", "utc_time": "sent_time_utc",
    })
    packets["received"] = packets["received_time_utc"].notna()
    packets["lost"] = ~packets["received"]
    packets["duplicate_receives"] = packets.get(
        "duplicate_receives", pd.Series(0, index=packets.index)).fillna(0).astype(int)
    packets["latency_ms"] = (
        packets["received_time_utc"] - packets["sent_time_utc"]
    ) * 1000.0
    packets["negative_latency"] = packets["latency_ms"].lt(0).fillna(False)
    packets["packet_clock_valid"] = (
        packets["sent_time_utc"].notna() &
        (packets["lost"] | (packets["latency_ms"].notna() & ~packets["negative_latency"]))
    )
    packets["utc_second"] = packets["sent_time_utc"].map(math.floor).astype("Int64")

    packets = packets.merge(_ue_map(run_dir), on="ue", how="left")
    designed = _designed_load(run_dir)
    designed_keys = [column for column in ("ue", "app", "direction", "flow_id")
                     if column in designed and column in packets]
    packets = packets.merge(designed, on=designed_keys, how="left",
                            validate="many_to_one")
    packets["designed_offered_mbps"] = pd.to_numeric(
        packets["designed_offered_mbps"], errors="coerce").astype(float)
    packets.insert(0, "profile_id", _profile_id(run_dir))
    packets.insert(0, "execution_id", _execution_id(run_dir))
    packets.insert(2, "packet_id", packets["direction"].astype(str) + ":" +
                   packets["flow_id"].astype(str) + ":" +
                   packets["sequence"].astype(str))
    if packets["packet_id"].duplicated().any():
        raise ValueError("packet_id is not unique")

    order = [c for c in (
        "execution_id", "profile_id", "packet_id", "cell", "ue", "ue_index",
        "ue_class", "nb_id", "rnti", "app", "direction", "flow_id",
        "sequence", "size_bytes",
        "sent_time_utc", "received_time_utc", "utc_second", "received", "lost",
        "latency_ms", "duplicate_receives", "negative_latency", "packet_clock_valid",
        "designed_offered_mbps",
    ) if c in packets]
    return packets[order].sort_values(
        ["sent_time_utc", "direction", "flow_id", "sequence"]
    ).reset_index(drop=True)


def ue_app_second_observed(packets: pd.DataFrame) -> pd.DataFrame:
    """Derive one row per UE/application/direction/UTC second."""
    if packets.empty:
        return pd.DataFrame()
    group = [c for c in (
        "execution_id", "profile_id", "cell", "ue", "ue_index", "ue_class",
        "nb_id", "rnti",
        "app", "direction", "utc_second",
    ) if c in packets]
    rows = []
    for values, frame in packets.groupby(group, dropna=False, sort=True):
        row = dict(zip(group, values if isinstance(values, tuple) else (values,)))
        valid = frame.loc[frame["packet_clock_valid"] & frame["received"], "latency_ms"]
        row.update({
            "sent_packets": int(len(frame)),
            "received_packets": int(frame["received"].sum()),
            "lost_packets": int(frame["lost"].sum()),
            "sent_bytes": int(frame["size_bytes"].fillna(0).sum()),
            "received_bytes": int(frame.loc[frame["received"], "size_bytes"].fillna(0).sum()),
            "loss_rate": float(frame["lost"].mean()),
            "offered_mbps": float(frame["size_bytes"].fillna(0).sum() * 8 / 1e6),
            "received_mbps": float(frame.loc[frame["received"], "size_bytes"].fillna(0).sum() * 8 / 1e6),
            "designed_offered_mbps": (
                frame["designed_offered_mbps"].dropna().iloc[0]
                if "designed_offered_mbps" in frame and
                len(frame["designed_offered_mbps"].dropna()) else float("nan")
            ),
            "latency_samples": int(len(valid)),
            "latency_ms_mean": valid.mean(),
            "latency_ms_median": valid.median(),
            "latency_ms_p95": valid.quantile(.95) if len(valid) else float("nan"),
            "latency_ms_std": valid.std(),
            "invalid_clock_packets": int((~frame["packet_clock_valid"]).sum()),
        })
        rows.append(row)
    result = pd.DataFrame(rows)
    if not result.empty and result.duplicated(group).any():
        raise ValueError("UE/application/second key is not unique")
    return result


def _state_start(run_dir: Path, state: dict) -> float:
    value = state.get("traffic_start_reference_epoch") if state else None
    if value is not None:
        return float(value)
    timing = _json(run_dir / schema.LOGS_DIR / "run_timing.json", {}) or {}
    for key in (
            "senders_start_epoch", "traffic_start_epoch", "started_epoch",
            "wall_start"):
        if timing.get(key) is not None:
            return float(timing[key])
    raise ValueError("cannot determine traffic start for channel segmentation")


def channel_segments(run_dir) -> pd.DataFrame:
    """Build half-open verified channel intervals for every UE and direction."""
    run_dir = Path(run_dir)
    state = run_profile.channel_state(run_dir) or {}
    schedule = _json(run_dir / schema.CHANNEL_SCHEDULE, {}) or {}
    config = _json(run_dir / schema.CONFIG, {}) or {}
    start = _state_start(run_dir, state)
    duration = float(config.get("simulation_duration") or
                     config.get("duration_s") or config.get("duration") or
                     schedule.get("duration_s") or 0)
    if duration <= 0:
        raise ValueError("config.json has no positive traffic duration")
    end = start + duration
    ue_map = _ue_map(run_dir)
    if ue_map.empty:
        raise ValueError("cannot build channel segments without UE manifest")

    initial = state.get("initial_state") or []
    transitions = state.get("transitions") or []
    schedule_events = schedule.get("events") or schedule.get("transitions") or []
    state_success = bool(state.get("success"))
    rows = []
    execution = _execution_id(run_dir)
    profile_id = _profile_id(run_dir)

    for ue_row in ue_map.itertuples(index=False):
        ue = str(ue_row.ue)
        cell = getattr(ue_row, "cell", pd.NA)
        ue_index = getattr(ue_row, "ue_index", pd.NA)
        nb_id = getattr(ue_row, "nb_id", pd.NA)
        rnti = getattr(ue_row, "rnti", pd.NA)
        for direction in ("dl", "ul"):
            target = ue if direction == "dl" else f"cell{int(cell)}" if pd.notna(cell) else None
            states = [dict(item) for item in initial
                      if item.get("direction") == direction and str(item.get("target")) == str(target)]
            changes = [dict(item) for item in transitions
                       if item.get("direction") == direction and str(item.get("target")) == str(target)]

            def scheduled_match(item):
                candidates = [event for event in schedule_events
                              if event.get("direction") == direction and
                              str(event.get("target")) == str(target) and
                              event.get("parameter") == item.get("parameter")]
                if not candidates:
                    return None
                at_s = _number(item.get("at_s"))
                if at_s is not None:
                    return min(candidates, key=lambda event: abs(
                        (_number(event.get("at_s")) or 0) - at_s))
                observed = item.get("observed", item.get("value"))
                matching = [event for event in candidates
                            if _same_value(event.get("value"), observed)]
                return matching[0] if matching else candidates[0]

            def normalise(item):
                item = dict(item)
                event = scheduled_match(item) or {}
                item["_scheduled_at"] = (
                    _number(item.get("at_s")) if item.get("at_s") is not None
                    else _number(event.get("at_s"))
                )
                if item.get("requested") is None:
                    item["requested"] = event.get(
                        "value", item.get("value", item.get("observed")))
                if item.get("observed") is None:
                    item["observed"] = item.get("applied_value", item.get("value"))
                return item

            states = [normalise(item) for item in states]
            changes = [normalise(item) for item in changes]
            # The initial snapshot owns t=0. Runtime readbacks for t=0 would
            # otherwise create artificial short segments during startup.
            changes = [item for item in changes
                       if item.get("_scheduled_at") is None or
                       item["_scheduled_at"] > 1e-9]
            changes.sort(key=lambda item: float(
                item.get("applied_epoch") or item.get("scheduler_apply_epoch") or end))

            if not states and not changes:
                rows.append({
                    "execution_id": execution, "profile_id": profile_id,
                    "segment_id": f"{execution}:{ue}:{direction}:0", "cell": cell,
                    "ue": ue, "ue_index": ue_index, "nb_id": nb_id,
                    "rnti": rnti, "direction": direction,
                    "segment_start_utc": start, "segment_end_utc": end,
                    "duration_s": duration, "parameter": pd.NA,
                    "requested_value": pd.NA, "applied_value": pd.NA,
                    "model_name": pd.NA, "model_type": pd.NA,
                    "model_index": pd.NA, "model_mapping_valid": False,
                    "controlled": False, "verified": False,
                    "channel_agreement": False, "training_eligible": False,
                })
                continue

            current = dict(states[-1] if states else changes[0])
            boundary = start
            segment_index = 0
            for change in changes + [None]:
                next_boundary = end if change is None else float(
                    change.get("applied_epoch") or change.get("scheduler_apply_epoch") or end)
                next_boundary = min(max(next_boundary, boundary), end)
                observed = current.get("observed")
                requested = current.get("requested", current.get("value", observed))
                verified = bool(current.get("verified", True) and
                                current.get("status", "verified") == "verified" and
                                state_success)
                agreement = _same_value(requested, observed)
                model_index = _number(current.get("model_index"))
                model_mapping_valid = bool(
                    direction == "dl" and model_index == 0 and
                    current.get("model_name") == "rfsimu_channel_enB0"
                )
                if next_boundary > boundary:
                    rows.append({
                        "execution_id": execution, "profile_id": profile_id,
                        "segment_id": f"{execution}:{ue}:{direction}:{segment_index}",
                        "cell": cell, "ue": ue, "ue_index": ue_index,
                        "nb_id": nb_id, "rnti": rnti,
                        "direction": direction, "segment_start_utc": boundary,
                        "segment_end_utc": next_boundary,
                        "duration_s": next_boundary - boundary,
                        "parameter": current.get("parameter"),
                        "requested_value": requested, "applied_value": observed,
                        "model_name": current.get("model_name"),
                        "model_type": current.get("model_type"),
                        "model_index": current.get("model_index"),
                        "model_mapping_valid": model_mapping_valid,
                        "controlled": model_mapping_valid,
                        "verified": verified,
                        "channel_agreement": agreement,
                        "training_eligible": bool(
                            verified and agreement and model_mapping_valid),
                    })
                    segment_index += 1
                if change is None or next_boundary >= end:
                    break
                current = dict(change)
                boundary = next_boundary
    result = pd.DataFrame(rows).sort_values(
        ["cell", "ue", "direction", "segment_start_utc"]
    ).reset_index(drop=True)
    for column in ("requested_value", "applied_value", "model_index"):
        result[column] = pd.to_numeric(result[column], errors="coerce").astype(float)
    for column in ("parameter", "model_name", "model_type"):
        result[column] = result[column].astype("string")
    if result["segment_id"].duplicated().any():
        raise ValueError("channel segment identifier is not unique")
    return result


def enrich_radio_clock_provenance(training: pd.DataFrame,
                                  radio: pd.DataFrame) -> pd.DataFrame:
    """Add explicit dual-clock provenance to segment-level radio diagnostics.

    This helper is also used during export so older immutable V2 archives gain
    the additive fields without modifying their checksummed source files.
    """
    result = training.copy()
    fields = (
        "radio_join_clock", "radio_clock_lag_samples",
        "radio_clock_lag_s_segment_mean", "radio_clock_lag_s_segment_p95",
        "radio_clock_lag_s_segment_max", "radio_clock_lag_warning",
    )
    if result.empty:
        for field in fields:
            if field not in result:
                result[field] = pd.Series(dtype="object")
        return result

    for field in fields:
        if field not in result:
            result[field] = pd.NA
    radio = radio if radio is not None else pd.DataFrame()
    if radio.empty or "ue" not in radio:
        return result

    time_column = (
        "receipt_utc_second" if "receipt_utc_second" in radio else "utc_second")
    if time_column not in radio:
        return result
    for index, segment in result.iterrows():
        mask = (
            radio["ue"].astype(str).eq(str(segment["ue"])) &
            radio[time_column].ge(segment["segment_start_utc"]) &
            radio[time_column].lt(segment["segment_end_utc"])
        )
        part = radio.loc[mask]
        ratio = segment.get("source_wall_ratio")
        ratio = None if pd.isna(ratio) else float(ratio)
        summary = prb.clock_lag_summary(part, source_wall_ratio=ratio)
        if not summary["radio_clock_lag_samples"]:
            continue
        result.at[index, "radio_join_clock"] = summary["radio_join_clock"]
        result.at[index, "radio_clock_lag_samples"] = summary[
            "radio_clock_lag_samples"]
        result.at[index, "radio_clock_lag_s_segment_mean"] = summary[
            "radio_clock_lag_s_mean"]
        result.at[index, "radio_clock_lag_s_segment_p95"] = summary[
            "radio_clock_lag_s_p95"]
        result.at[index, "radio_clock_lag_s_segment_max"] = summary[
            "radio_clock_lag_s_max"]
        result.at[index, "radio_clock_lag_warning"] = summary[
            "radio_clock_lag_warning"]
    result["radio_join_clock"] = result["radio_join_clock"].astype("string")
    for field in (
            "radio_clock_lag_samples", "radio_clock_lag_s_segment_mean",
            "radio_clock_lag_s_segment_p95", "radio_clock_lag_s_segment_max"):
        result[field] = pd.to_numeric(result[field], errors="coerce")
    result["radio_clock_lag_warning"] = result[
        "radio_clock_lag_warning"].astype("boolean")
    return result


def segment_training_table(run_dir, packets: pd.DataFrame,
                           segments: pd.DataFrame, radio: pd.DataFrame) -> pd.DataFrame:
    """Aggregate packets and radio measurements at channel-segment grain."""
    if packets.empty or segments.empty:
        return pd.DataFrame()
    source_wall_ratio = radio.attrs.get("source_wall_ratio") if radio is not None else None
    rows = []
    radio = radio if radio is not None else pd.DataFrame()
    numeric_radio = []
    if not radio.empty:
        numeric_radio = [c for c in radio.select_dtypes(include="number").columns
                         if c not in QUARANTINED_RADIO_COLUMNS and c not in {
                             "utc_second", "t_s", "cell", "nb_id", "rnti",
                             "recv_tstamp_us", "source_tstamp_us",
                             "receipt_utc_second", "source_utc_second",
                         }]

    for segment in segments.to_dict("records"):
        mask = (
            packets["ue"].astype(str).eq(str(segment["ue"])) &
            packets["direction"].eq(segment["direction"]) &
            packets["sent_time_utc"].ge(segment["segment_start_utc"]) &
            packets["sent_time_utc"].lt(segment["segment_end_utc"])
        )
        part = packets.loc[mask]
        valid = part.loc[part["packet_clock_valid"] & part["received"], "latency_ms"]
        designed = (part[["flow_id", "designed_offered_mbps"]]
                    .dropna(subset=["designed_offered_mbps"])
                    .drop_duplicates("flow_id"))
        row = dict(segment)
        row.update({
            "app_mix": json.dumps(sorted(part["app"].dropna().astype(str).unique().tolist())),
            "designed_offered_mbps": float(
                designed["designed_offered_mbps"].sum()) if len(designed)
                else float("nan"),
            "sent_packets": int(len(part)),
            "received_packets": int(part["received"].sum()),
            "lost_packets": int(part["lost"].sum()),
            "loss_rate": float(part["lost"].mean()) if len(part) else float("nan"),
            "sent_bytes": int(part["size_bytes"].fillna(0).sum()),
            "received_bytes": int(part.loc[part["received"], "size_bytes"].fillna(0).sum()),
            "offered_mbps": (part["size_bytes"].fillna(0).sum() * 8 /
                              max(float(segment["duration_s"]), 1e-9) / 1e6),
            "received_mbps": (part.loc[part["received"], "size_bytes"].fillna(0).sum() * 8 /
                               max(float(segment["duration_s"]), 1e-9) / 1e6),
            "latency_samples": int(len(valid)),
            "latency_ms_p50": valid.quantile(.50) if len(valid) else float("nan"),
            "latency_ms_p95": valid.quantile(.95) if len(valid) else float("nan"),
            "latency_ms_mean": valid.mean(),
            "invalid_clock_packets": int((~part["packet_clock_valid"]).sum()),
            "valid_clock_fraction": (
                float(part["packet_clock_valid"].mean()) if len(part) else 0.0
            ),
            "source_wall_ratio": source_wall_ratio,
            "packet_evidence": bool(len(part)),
        })
        if not radio.empty and "ue" in radio:
            time_column = "receipt_utc_second" if "receipt_utc_second" in radio else "utc_second"
            rmask = (radio["ue"].astype(str).eq(str(segment["ue"])) &
                     radio[time_column].ge(segment["segment_start_utc"]) &
                     radio[time_column].lt(segment["segment_end_utc"]))
            rpart = radio.loc[rmask]
            row["radio_samples"] = int(len(rpart))
            for column in numeric_radio:
                row[f"{column}_segment_mean"] = rpart[column].mean()
        else:
            row["radio_samples"] = 0
        row["training_eligible"] = bool(
            row.get("training_eligible") and row["packet_evidence"] and
            row["valid_clock_fraction"] >= .95 and len(valid))
        rows.append(row)
    result = pd.DataFrame(rows)
    result = enrich_radio_clock_provenance(result, radio)
    if not result.empty and result["segment_id"].duplicated().any():
        raise ValueError("segment training table key is not unique")
    return result


def build_tables(run_dir) -> dict[str, pd.DataFrame]:
    """Build all V2 tables from the frozen run inputs."""
    run_dir = Path(run_dir)
    packets = packet_outcomes(run_dir)
    seconds = ue_app_second_observed(packets)
    segments = channel_segments(run_dir)
    radio = prb.prb_timeseries(run_dir)
    if radio.empty and radio.attrs.get("error"):
        raise ValueError(radio.attrs["error"])
    training = segment_training_table(run_dir, packets, segments, radio)
    return {
        schema.PACKET_OUTCOMES: packets,
        schema.UE_APP_SECOND_OBSERVED: seconds,
        schema.CHANNEL_SEGMENTS: segments,
        schema.SEGMENT_TRAINING_TABLE: training,
    }
