"""Parse MGEN *sender* scripts (.mgn) to get the projected (offered) load.

These are the ON/OFF event files your pipeline already writes, e.g.:

    0.018522 ON 110390000 UDP SRC 15039 DST 12.1.1.2/5000 PERIODIC [373.284 1363]
    12.51    OFF 110390000

For each flow we pair its ON windows with its OFF events in order; a window of
duration d at <pps> packets/s carries pps*d packets, each <bytes> long. Summed
per flow, that's the projected packets/bytes — the design side of the run.
"""
from __future__ import annotations
import re
from pathlib import Path

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
