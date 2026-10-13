"""Parse MGEN *sender* scripts (.mgn) to get the projected (offered) load.

These are the ON/OFF event files your pipeline already writes, e.g.:

    0.018522 ON 110390000 UDP SRC 15039 DST 12.1.1.2/5000 PERIODIC [373.284 1363]
    12.51    OFF 110390000

For each flow we pair its ON windows with its OFF events in order; a window of
duration d at <pps> packets/s carries pps*d packets, each <bytes> long. Summed
per flow, that's the projected packets/bytes — the design side of the run.
"""
from __future__ import annotations
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

_ON = re.compile(
    r"^([\d.]+)\s+ON\s+(\d+)\s+UDP\s+SRC\s+(\d+)\s+DST\s+([\d.]+)/(\d+)"
    r"\s+PERIODIC\s+\[\s*([\d.]+)\s+(\d+)\s*\]"
)
_OFF = re.compile(r"^([\d.]+)\s+OFF\s+(\d+)")


def projected_from_script(path):
    """Return [{flow_id, dst_ip, proj_packets, proj_bytes}, ...] for one script."""
    on_by_flow, off_by_flow, dst = {}, {}, {}
    for line in Path(path).open():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ON.match(line)
        if m:
            t, flow, _src, dst_ip, _dport, pps, size = m.groups()
            flow = int(flow)
            on_by_flow.setdefault(flow, []).append((float(t), float(pps), int(size)))
            dst[flow] = dst_ip
            continue
        m = _OFF.match(line)
        if m:
            flow = int(m.group(2))
            off_by_flow.setdefault(flow, []).append(float(m.group(1)))

    rows = []
    for flow, ons in on_by_flow.items():
        ons.sort()
        offs = sorted(off_by_flow.get(flow, []))
        pkts = nbytes = 0.0
        for (t_on, pps, size), t_off in zip(ons, offs):
            dur = max(0.0, t_off - t_on)
            n = pps * dur
            pkts += n
            nbytes += n * size
        rows.append({
            "flow_id": flow,
            "dst_ip": dst.get(flow),
            "proj_packets": round(pkts),
            "proj_bytes": round(nbytes),
        })
    return rows


def _periodic_windows(path):
    number = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    on_pattern = re.compile(
        rf"({number})\s+ON\s+(\d+)\s+UDP\s+SRC\s+\d+\s+"
        rf"DST\s+[\d.]+/\d+\s+PERIODIC\s+\[\s*({number})\s+(\d+)\s*\]")
    off_pattern = re.compile(rf"({number})\s+OFF\s+(\d+)")
    events = []
    for line_number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        on = on_pattern.fullmatch(line)
        off = off_pattern.fullmatch(line)
        if on:
            at, flow, pps, size = on.groups()
            at, pps, size = float(at), float(pps), int(size)
            if not math.isfinite(at) or not math.isfinite(pps) or pps <= 0 or size <= 0:
                raise ValueError(f"{Path(path).name}:{line_number}: invalid PERIODIC values")
            events.append((at, line_number, int(flow), (pps, size)))
        elif off:
            at, flow = off.groups()
            at = float(at)
            if not math.isfinite(at):
                raise ValueError(f"{Path(path).name}:{line_number}: invalid OFF time")
            events.append((at, line_number, int(flow), None))
        else:
            raise ValueError(
                f"{Path(path).name}:{line_number}: expected a PERIODIC UDP ON or OFF event")
    active = {}
    windows = []
    for at, line_number, flow, traffic in sorted(events):
        if traffic is not None:
            if flow in active:
                raise ValueError(f"{Path(path).name}: flow {flow} has overlapping ON events")
            active[flow] = (at, *traffic)
        else:
            if flow not in active:
                raise ValueError(f"{Path(path).name}:{line_number}: OFF without ON for flow {flow}")
            start, pps, size = active.pop(flow)
            windows.append((flow, start, at, pps, size))
    if active:
        raise ValueError(f"{Path(path).name}: missing OFF for flows {sorted(active)}")
    return windows


def projected_load_timeseries(run_dir, interval_s=900.0, *, start_s=0.0,
                             end_s=None, group_by=()):
    run_dir = Path(run_dir)
    interval_s, start_s = float(interval_s), float(start_s)
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("interval_s must be finite and positive")
    if not math.isfinite(start_s) or start_s < 0:
        raise ValueError("start_s must be finite and nonnegative")
    if end_s is None:
        config = run_dir / "config.json"
        if not config.is_file():
            raise ValueError("provide end_s or a config.json with simulation_duration")
        end_s = json.loads(config.read_text()).get("simulation_duration")
        if end_s is None:
            raise ValueError("config.json has no simulation_duration; provide end_s")
    end_s = float(end_s)
    if not math.isfinite(end_s) or end_s <= start_s:
        raise ValueError("end_s must be finite and greater than start_s")
    if isinstance(group_by, str):
        group_by = (group_by,)
    group_by = tuple(group_by)
    reserved = {"direction", "interval_start_s", "interval_end_s", "offered_packets",
                "offered_bytes", "offered_mbps"}
    if len(set(group_by)) != len(group_by) or reserved.intersection(group_by):
        raise ValueError("group_by must contain unique metadata columns; direction is automatic")
    scripts = run_dir / "mgen_scripts"
    senders = [("dl", scripts / "dn_dl_tx.mgn")]
    senders += [("ul", path) for path in sorted(scripts.glob("ue*_ul_tx.mgn"))]
    senders = [(direction, path) for direction, path in senders if path.is_file()]
    if not senders:
        raise ValueError("no MGEN sender scripts found")

    metadata = {}
    if group_by:
        frame = pd.read_csv(scripts / "flow_batch_map.csv", dtype=str, keep_default_na=False)
        flow_column = "unique_flow_id" if "unique_flow_id" in frame else "flow_id"
        missing = set((flow_column, "direction", *group_by)) - set(frame.columns)
        if missing:
            raise ValueError("flow_batch_map.csv is missing columns: " + ", ".join(sorted(missing)))
        for _, row in frame.iterrows():
            key = (str(row["direction"]), int(row[flow_column]))
            if key[0] not in ("dl", "ul") or key in metadata:
                raise ValueError("flow_batch_map.csv needs unique flow/direction keys")
            values = tuple(row[column] for column in group_by)
            if any(not value.strip() for value in values):
                raise ValueError("flow_batch_map.csv has empty grouping metadata")
            metadata[key] = values

    count = math.ceil((end_s - start_s) / interval_s)
    starts = start_s + np.arange(count, dtype=float) * interval_s
    ends = np.minimum(starts + interval_s, end_s)
    totals = {} if group_by else {
        (direction,): (np.zeros(count), np.zeros(count)) for direction in ("dl", "ul")}
    seen = set()
    for direction, path in senders:
        windows = _periodic_windows(path)
        identities = {(direction, flow) for flow, *_ in windows}
        if seen.intersection(identities):
            raise ValueError("sender scripts reuse a flow ID in the same direction")
        seen.update(identities)
        for flow, on, off, pps, size in windows:
            identity = (direction, flow)
            if group_by and identity not in metadata:
                raise ValueError(f"flow_batch_map.csv has no metadata for {direction} flow {flow}")
            group = (direction, *metadata[identity]) if group_by else (direction,)
            if group not in totals:
                totals[group] = (np.zeros(count), np.zeros(count))
            left, right = max(on, start_s), min(off, end_s)
            if right <= left:
                continue
            first = max(0, int(math.floor((left - start_s) / interval_s)))
            last = min(count, int(math.ceil((right - start_s) / interval_s)))
            overlap = np.maximum(0, np.minimum(ends[first:last], right) -
                                 np.maximum(starts[first:last], left))
            packets, nbytes = totals[group]
            packets[first:last] += pps * overlap
            nbytes[first:last] += pps * size * overlap
    frames = []
    for group, (packets, nbytes) in sorted(totals.items()):
        frame = pd.DataFrame({
            "interval_start_s": starts, "interval_end_s": ends,
            "direction": group[0], "offered_packets": packets,
            "offered_bytes": nbytes, "offered_mbps": nbytes * 8 / (ends - starts) / 1e6,
        })
        for column, value in zip(group_by, group[1:]):
            frame[column] = value
        frames.append(frame)
    columns = ["interval_start_s", "interval_end_s", "direction", *group_by,
               "offered_packets", "offered_bytes", "offered_mbps"]
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)[columns].sort_values(
        ["interval_start_s", "direction", *group_by], kind="stable").reset_index(drop=True)
