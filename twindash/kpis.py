"""Observed KPI table + projected-vs-realized reconciliation.

  sent  = SEND events from *_tx.log   (offered, as MGEN emitted)
  recv  = RECV events from *_rx.log   (delivered)
  loss  = 1 - recv/sent
  latency = recv_time - sent_time     (needs chrony sync; garbage without it)
  throughput = recv_bytes * 8 / RUN_DURATION   (app-layer goodput, bits/s)

Per-UE/per-app attribution comes from flow_batch_map.csv, joined on flow_id
(the DN NATs every UE to one source, so the log's own address can't identify
the UE — the flow id is the only reliable key back to (ue, app)).

Throughput is bytes over the RUN duration, not over a flow's burst span. Dividing
by a burst's own sub-second span gives a burst *peak* (tens of Gbps) that is not
a meaningful throughput and cannot be summed across flows. Run-average is
comparable across flows/apps/UEs and additive. For the burst structure over time,
use throughput_timeseries() (fixed windows), which IS summable per window and is
the right thing to overlay against per-UE PRB.

Bytes are UDP payload (app layer) => effectively goodput (no TCP retransmit).
They exclude 5G/GTP-U/RLC/MAC overhead, so throughput-per-PRB is app-layer bits
per resource block — a meaningful efficiency proxy, not raw spectral efficiency.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import schema, mgen_log, mgen_script, timing


def _flow_map(run_dir: Path) -> pd.DataFrame:
    fbm = pd.read_csv(run_dir / schema.SCRIPTS_DIR / "flow_batch_map.csv")
    out = (fbm.rename(columns={"unique_flow_id": "flow_id", "ue_name": "ue"})
              [["flow_id", "ue", "app", "direction"]])
    duplicate = out.duplicated(["flow_id", "direction"], keep=False)
    if duplicate.any():
        values = out.loc[duplicate, ["flow_id", "direction"]].drop_duplicates()
        raise ValueError("flow_batch_map.csv has non-unique flow/direction keys: "
                         + values.to_dict("records").__repr__())
    return out


def run_duration(run_dir: Path, observed_span: float | None = None) -> float | None:
    """Run duration in seconds. Prefer the configured value (config.json flat key
    simulation_duration); fall back to the observed whole-run span if no config."""
    cfg = run_dir / "config.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text()).get("simulation_duration")
            if d:
                return float(d)
        except Exception:
            pass
    return observed_span


def _load_events(run_dir: Path):
    logs = run_dir / schema.LOGS_DIR
    tm = timing.load(run_dir)
    fbm = _flow_map(run_dir)
    flow_to_ue = {(int(r.flow_id), r.direction): r.ue
                  for r in fbm.itertuples(index=False)}
    sent_frames, recv_frames = [], []
    event_logs = sorted(logs.glob("*_tx.log")) + sorted(logs.glob("*_rx.log"))
    for log in event_logs:
        node_tag, direction = mgen_log.parse_run_name(log.name)
        node = timing.node_of(tm, node_tag)
        df = mgen_log.parse_log(
            log,
            midnight_epoch=timing.midnight_epoch(tm, node),
            ref_sod=timing.anchor_sod(tm, node))
        if df.empty:
            continue
        df["direction"] = direction
        df["clock_node"] = node
        if log.name.endswith("_rx.log") and tm:
            # The event stamp is written by the receiver, while MGEN's sent>
            # stamp originated on the sender.  UL receive logs on the DN can
            # contain UEs from both cells, so the sender anchor is per row.
            def sender_utc(row):
                ue = flow_to_ue.get((int(row["flow_id"]), direction)) \
                    if pd.notna(row.get("flow_id")) else None
                sender = "core" if direction == "dl" else timing.node_of(tm, ue)
                return timing.to_utc(
                    row.get("sent_time"), timing.midnight_epoch(tm, sender),
                    timing.anchor_sod(tm, sender))

            df["sent_utc_time"] = df.apply(sender_utc, axis=1)
        if log.name.endswith("_tx.log"):
            sent_frames.append(df[df.event == "SEND"])
        elif log.name.endswith("_rx.log"):
            recv_frames.append(df[df.event == "RECV"])
    sent = pd.concat(sent_frames, ignore_index=True) if sent_frames else pd.DataFrame()
    recv = pd.concat(recv_frames, ignore_index=True) if recv_frames else pd.DataFrame()
    return sent, recv


def packet_records(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    sent, received = _load_events(run_dir)
    key = ["direction", "flow_id", "seq"]
    columns = [*key, "ue", "app", "size_bytes", "sent_time_utc",
               "received_time_utc", "sent_sod", "received_sod", "received",
               "lost", "duplicate_receives", "latency_ms", "negative_latency",
               "packet_clock_valid"]
    if sent.empty:
        if not received.empty:
            raise ValueError("Receive events exist without transmitted packet logs")
        return pd.DataFrame(columns=columns)
    if sent[key].isna().any().any():
        raise ValueError("Transmitted packets have missing identifiers")
    if sent.duplicated(key).any():
        raise ValueError("Duplicate transmitted packet identifiers prevent calculation")
    sent = sent.reindex(columns=key + ["time", "utc_time", "size"]).rename(
        columns={"time": "sent_sod", "utc_time": "sent_time_utc", "size": "size_bytes"})
    if sent["size_bytes"].isna().any() or sent["size_bytes"].lt(0).any():
        raise ValueError("Transmitted packets have missing or invalid payload sizes")
    sent = sent.merge(_flow_map(run_dir), on=["direction", "flow_id"],
                      how="left", validate="many_to_one")
    if sent["ue"].isna().any():
        raise ValueError("Transmitted packets have no matching UE in the flow map")
    if received.empty:
        rx = pd.DataFrame(columns=key + ["received_sod", "received_time_utc",
                                        "received_size", "duplicate_receives", "received"])
    else:
        if received[key].isna().any().any():
            raise ValueError("Received packets have missing identifiers")
        rx = received.reindex(columns=key + ["time", "utc_time", "size"])
        if rx["size"].isna().any() or rx["size"].lt(0).any():
            raise ValueError("Received packets have missing or invalid payload sizes")
        unknown = rx[key].merge(sent[key], on=key, how="left", indicator=True)
        if unknown["_merge"].eq("left_only").any():
            raise ValueError("Received packet identifiers have no matching SEND event")
        duplicates = (rx.groupby(key).size() - 1).rename("duplicate_receives")
        rx = (rx.sort_values(["utc_time", "time"]).drop_duplicates(key, keep="first")
              .merge(duplicates, on=key, validate="one_to_one")
              .rename(columns={"time": "received_sod", "utc_time": "received_time_utc",
                               "size": "received_size"}))
        rx["received"] = True
    records = sent.merge(rx, on=key, how="left", validate="one_to_one")
    records["received"] = records["received"].eq(True)
    records["lost"] = ~records["received"]
    records["duplicate_receives"] = records["duplicate_receives"].fillna(0).astype(int)
    sizes = records["size_bytes"].notna() & records["received_size"].notna()
    if records.loc[sizes, "size_bytes"].ne(records.loc[sizes, "received_size"]).any():
        raise ValueError("Matched SEND/RECV packets have different payload sizes")
    records["latency_ms"] = records.apply(
        lambda row: mgen_log.elapsed_seconds(row["sent_sod"], row["received_sod"]),
        axis=1) * 1000.0
    anchored = records["sent_time_utc"].notna() & records["received_time_utc"].notna()
    records.loc[anchored, "latency_ms"] = (
        records.loc[anchored, "received_time_utc"] - records.loc[anchored, "sent_time_utc"]
    ) * 1000.0
    records["negative_latency"] = records["latency_ms"].lt(0)
    records["packet_clock_valid"] = records["sent_time_utc"].notna() & (
        records["lost"] | (anchored & ~records["negative_latency"]))
    records.attrs["invalid_latency_packets"] = int((records["received"] & (
        records["latency_ms"].isna() | records["negative_latency"])).sum())
    records.attrs["unanchored_packets"] = int((records["received"] & ~anchored).sum())
    return records[columns]


def summarize_packets(packets, group_by) -> pd.DataFrame:
    columns = [*group_by, "sent_packets", "recv_packets", "sent_bytes", "recv_bytes",
               "latency_ms_mean", "latency_ms_median", "latency_ms_p95", "jitter_ms", "loss"]
    if packets.empty:
        return pd.DataFrame(columns=columns)
    frame = packets.copy()
    frame["received_bytes"] = frame["size_bytes"].where(frame["received"], 0)
    frame["valid_latency"] = frame["latency_ms"].where(
        frame["received"] & frame["latency_ms"].ge(0))
    result = (frame.groupby(group_by).agg(
        sent_packets=("seq", "size"), recv_packets=("received", "sum"),
        sent_bytes=("size_bytes", "sum"), recv_bytes=("received_bytes", "sum"),
        latency_ms_mean=("valid_latency", "mean"),
        latency_ms_median=("valid_latency", "median"),
        latency_ms_p95=("valid_latency", lambda values: values.quantile(.95)),
        jitter_ms=("valid_latency", "std")).reset_index())
    result["loss"] = 1 - result["recv_packets"] / result["sent_packets"]
    return result[columns]


def _packet_duration(run_dir, packets):
    clock = packets["received_time_utc"].dropna()
    if clock.empty:
        clock = packets["received_sod"].dropna()
    span = float(clock.max() - clock.min()) if len(clock) else None
    return run_duration(run_dir, observed_span=span)


def build_observed(run_dir, *, packets=None) -> pd.DataFrame:
    run_dir = Path(run_dir)
    packets = packet_records(run_dir) if packets is None else packets
    obs = summarize_packets(packets, ["flow_id", "direction", "ue", "app"])
    duration = _packet_duration(run_dir, packets)
    obs["recv_mbps"] = obs["recv_bytes"] * 8 / duration / 1e6 if duration and duration > 0 else float("nan")
    obs.insert(0, "run_id", run_dir.name)
    obs.attrs.update(packets.attrs)
    obs.attrs["run_duration_s"] = duration
    return obs


def by_ue(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    packets = packet_records(run_dir)
    result = summarize_packets(packets, ["ue", "direction"])
    duration = _packet_duration(run_dir, packets)
    result["recv_mbps"] = result["recv_bytes"] * 8 / duration / 1e6 if duration and duration > 0 else float("nan")
    result.insert(0, "run_id", run_dir.name)
    return result


def packet_latency(run_dir, *, packets=None) -> pd.DataFrame:
    packets = packet_records(run_dir) if packets is None else packets
    valid = packets.loc[packets["received"] & packets["latency_ms"].ge(0)]
    result = (valid.groupby(["ue", "direction"]).agg(
        packets=("seq", "size"), latency_ms_p50=("latency_ms", "median"),
        latency_ms_p95=("latency_ms", lambda values: values.quantile(.95))).reset_index())
    result.attrs.update(packets.attrs)
    return result


def by_app(run_dir) -> pd.DataFrame:
    obs = build_observed(run_dir)
    if obs.empty:
        return obs
    result = (obs.groupby(["run_id", "app", "direction"])
              .agg(sent_packets=("sent_packets", "sum"), recv_packets=("recv_packets", "sum"),
                   recv_bytes=("recv_bytes", "sum"), recv_mbps=("recv_mbps", "sum"))
              .reset_index())
    result["loss"] = 1 - result["recv_packets"] / result["sent_packets"]
    return result


def throughput_timeseries(run_dir, window_s: float = 1.0, per: str = "ue", *, packets=None) -> pd.DataFrame:
    if window_s <= 0:
        raise ValueError("Throughput window must be positive")
    run_dir = Path(run_dir)
    packets = packet_records(run_dir) if packets is None else packets
    recv = packets.loc[packets["received"]].copy()
    keycol = {"ue": "ue", "app": "app", "flow": "flow_id"}[per]
    if recv.empty:
        return pd.DataFrame(columns=["t_s", keycol, "direction", "mbps"])
    has_utc = recv["received_time_utc"].notna().all()
    if has_utc:
        recv["win"] = (recv["received_time_utc"] // window_s) * window_s
    else:
        recv["win"] = ((recv["received_sod"] - recv["received_sod"].min()) // window_s) * window_s
    result = recv.groupby(["win", keycol, "direction"])["size_bytes"].sum().reset_index()
    result["mbps"] = result.pop("size_bytes") * 8 / window_s / 1e6
    if has_utc:
        result["utc_second"] = result["win"]
        start = timing.load(run_dir).get("senders_start_epoch", result["win"].min())
        result["t_s"] = result.pop("win") - float(start)
    else:
        result = result.rename(columns={"win": "t_s"})
    return result


def save_observed(run_dir) -> Path:
    out = Path(run_dir) / schema.OBSERVED_KPIS
    build_observed(run_dir).to_parquet(out, index=False)
    return out


def build_designed(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    scripts = run_dir / schema.SCRIPTS_DIR
    flow_map = _flow_map(run_dir)
    flow_app = {(int(r.flow_id), r.direction): r.app
                for r in flow_map.itertuples(index=False)}
    duration = run_duration(run_dir) or 1.0
    ip2ue, ue_class = {}, {}
    man = scripts / "manifest.csv"
    if man.exists():
        m = pd.read_csv(man)
        ip2ue = dict(zip(m["ue_ip"].astype(str), m["ue_name"]))
        ue_class = dict(zip(m["ue_name"], m["ue_class"]))
    rows = []
    dn = scripts / "dn_dl_tx.mgn"
    if dn.exists():
        for r in mgen_script.projected_from_script(dn):
            ue = ip2ue.get(str(r["dst_ip"]), r["dst_ip"])
            rows.append({"run_id": run_dir.name, "ue": ue, "direction": "dl",
                         "ue_class": ue_class.get(ue), "flow_id": r["flow_id"],
                         "app": flow_app.get((r["flow_id"], "dl")),
                         "proj_packets": r["proj_packets"], "proj_bytes": r["proj_bytes"],
                         "proj_duration_s": duration,
                         "proj_throughput_bps": r["proj_bytes"] * 8 / duration})
    for ul in sorted(scripts.glob("ue*_ul_tx.mgn")):
        ue = ul.name.split("_")[0]
        for r in mgen_script.projected_from_script(ul):
            rows.append({"run_id": run_dir.name, "ue": ue, "direction": "ul",
                         "ue_class": ue_class.get(ue), "flow_id": r["flow_id"],
                         "app": flow_app.get((r["flow_id"], "ul")),
                         "proj_packets": r["proj_packets"], "proj_bytes": r["proj_bytes"],
                         "proj_duration_s": duration,
                         "proj_throughput_bps": r["proj_bytes"] * 8 / duration})
    return pd.DataFrame(rows)


def reconcile(run_dir) -> pd.DataFrame:
    run_dir = Path(run_dir)
    obs = build_observed(run_dir)
    designed = run_dir / schema.DESIGNED_KPIS
    des = pd.read_parquet(designed) if designed.exists() else build_designed(run_dir)
    if des is None or des.empty:
        return obs
    if obs is None or obs.empty:
        return des
    keys = [k for k in schema.KEYS if k in des.columns and k in obs.columns]
    return des.merge(obs, on=keys, how="outer", suffixes=("", "_obs"))
