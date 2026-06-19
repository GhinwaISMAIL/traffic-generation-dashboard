"""Parse MGEN text logs.

MGEN writes one event per line, whitespace separated, with most fields as
key>value tokens:

    16:14:22.123456 RECV proto>UDP flow>10001 seq>0 src>192.168.70.163/5000 \
        dst>12.1.1.136/5000 sent>16:14:22.120000 size>1024

We only keep SEND (from a txlog sender) and RECV (from a receiver). The token
set varies a little between MGEN builds, so the parser ignores anything it
doesn't recognise rather than failing — check the columns against your real
logs once and adjust the few key names below if needed.
"""
from __future__ import annotations
import re
from pathlib import Path

import pandas as pd

_TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d+)$")


def _ts_to_seconds(tok):
    """HH:MM:SS.ffffff -> seconds since midnight (float). Runs are short, so
    we don't worry about a midnight rollover."""
    if tok is None:
        return None
    m = _TS.match(tok)
    if not m:
        return None
    h, mnt, s, frac = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + float("0." + frac)


def _split_addr(v):
    """'12.1.1.136/5000' -> ('12.1.1.136', 5000)"""
    if not v or "/" not in v:
        return v, None
    ip, _, port = v.partition("/")
    return ip, int(port) if port.isdigit() else None


def _as_int(v):
    return int(v) if v is not None and str(v).isdigit() else None


def parse_log(path) -> pd.DataFrame:
    """Return a DataFrame of SEND/RECV events from one MGEN log file."""
    rows = []
    for line in Path(path).open():
        parts = line.split()
        if len(parts) < 2 or parts[1] not in ("SEND", "RECV"):
            continue
        rec = {}
        for tok in parts[2:]:
            if ">" in tok:
                key, _, val = tok.partition(">")
                rec[key] = val
        src_ip, src_port = _split_addr(rec.get("src"))
        dst_ip, dst_port = _split_addr(rec.get("dst"))
        rows.append({
            "time": _ts_to_seconds(parts[0]),
            "event": parts[1],
            "flow_id": _as_int(rec.get("flow")),
            "seq": _as_int(rec.get("seq")),
            "src_ip": src_ip, "src_port": src_port,
            "dst_ip": dst_ip, "dst_port": dst_port,
            "size": _as_int(rec.get("size")),
            "sent_time": _ts_to_seconds(rec.get("sent")),
        })
    return pd.DataFrame(rows)


def parse_run_name(filename):
    """'ue1_dl_rx.log' -> ('ue1', 'dl'); 'dn_ul_rx.log' -> ('dn', 'ul')"""
    bits = Path(filename).stem.split("_")
    node = bits[0]
    direction = next((b for b in bits if b in ("dl", "ul")), None)
    return node, direction
