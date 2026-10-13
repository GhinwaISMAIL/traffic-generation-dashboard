#!/usr/bin/env python3
import csv
import json
from pathlib import Path
import re
import sqlite3
import sys

REQUIRED = ["tstamp", "recv_tstamp", "nb_id", "rnti",
            "dl_aggr_prb", "ul_aggr_prb"]
OPTIONAL = ["dl_mcs1", "ul_mcs1", "pusch_snr", "pucch_snr",
            "dl_bler", "ul_bler", "wb_cqi", "phr", "bsr"]


def monitor_quality(db, monitor_log):
    text = Path(monitor_log).read_text()
    counts = re.search(r"monitor_callbacks mac=(\d+) rlc=(\d+) pdcp=(\d+) gtp=(\d+)", text)
    marker = re.search(r"monitor_receipt_clock=(\S+) report_interval_ms=(\d+)", text)
    result = {"timestamp_stage": marker[1] if marker else "unverified",
              "report_interval_ms": int(marker[2]) if marker else None, "services": {}}
    if not counts:
        result["complete"] = None
        return result
    for table, expected in zip(("MAC_UE", "RLC_bearer", "PDCP_bearer", "GTP_NGUT"), map(int, counts.groups())):
        actual = db.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT nb_id,tstamp FROM {table})").fetchone()[0]
        result["services"][table] = {"callbacks": expected, "stored_reports": actual,
                                     "complete": expected == actual}
    result["complete"] = all(item["complete"] for item in result["services"].values())
    return result


def write_rlc(db, destination):
    cols = {row[1] for row in db.execute('PRAGMA table_info("RLC_bearer")')}
    wanted = ["txbuf_occ_bytes", "txbuf_occ_pkts", "rxbuf_occ_bytes", "rxbuf_occ_pkts"]
    if not {"recv_tstamp", "nb_id", "rnti", *wanted}.issubset(cols):
        return
    selected = ",".join(f"MAX({column})" for column in wanted)
    rows = db.execute(f"SELECT CAST(recv_tstamp/1000000 AS INTEGER) AS utc_second, nb_id, rnti, {selected} FROM RLC_bearer GROUP BY utc_second, nb_id, rnti ORDER BY utc_second, nb_id, rnti")
    with destination.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["utc_second", "nb_id", "rnti", *[name + "_max" for name in wanted]])
        writer.writerows(rows)


def frame_progress(db):
    cols = {row[1] for row in db.execute('PRAGMA table_info("MAC_UE")')}
    if not {"frame", "recv_tstamp", "nb_id"}.issubset(cols):
        return {}
    output = {}
    nodes = [row[0] for row in db.execute("SELECT DISTINCT nb_id FROM MAC_UE")]
    for node in nodes:
        rows = list(db.execute("SELECT recv_tstamp,MAX(frame) FROM MAC_UE WHERE nb_id=? GROUP BY recv_tstamp ORDER BY recv_tstamp", (node,)))
        steps = [(int(b[1]) - int(a[1])) % 1024 for a, b in zip(rows, rows[1:])]
        discontinuities = sum(step > 512 or (b[0] - a[0]) >= 10_240_000
                              for step, a, b in zip(steps, rows, rows[1:]))
        span = (rows[-1][0] - rows[0][0]) / 1e6 if len(rows) > 1 else 0
        output[str(node)] = {"samples": len(rows), "wall_span_s": span,
                             "frames_advanced": sum(steps), "discontinuities": discontinuities,
                             "frame_time_wall_ratio": sum(steps) * .01 / span
                             if span > 0 and not discontinuities else None}
    return output


def main(db_path, out_path, monitor_log=None):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cols = {r[1] for r in db.execute('PRAGMA table_info("MAC_UE")')}

    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        sys.exit(f"MAC_UE is missing required columns: {missing}")

    if monitor_log is not None:
        quality = monitor_quality(db, monitor_log)
        quality["radio_frame_progress"] = frame_progress(db)
        Path(out_path).with_suffix(".quality.json").write_text(json.dumps(quality, indent=2) + "\n")
        if quality["complete"] is False:
            raise ValueError("Monitoring reports were lost before database storage; preserving the source database")
        write_rlc(db, Path(out_path).with_suffix(".rlc.csv"))

    extras = [c for c in OPTIONAL if c in cols]
    extra_sel = "".join(f", AVG({c}) AS {c}_avg" for c in extras)

    rows = db.execute(f"""
        SELECT CAST(recv_tstamp / 1000000 AS INTEGER) AS utc_second,
               MAX(recv_tstamp) AS recv_tstamp_us,
               MAX(tstamp) AS source_tstamp_us,
               nb_id,
               rnti,
               MAX(dl_aggr_prb) AS dl_aggr_prb,
               MAX(ul_aggr_prb) AS ul_aggr_prb,
               COUNT(*) AS samples{extra_sel}
        FROM MAC_UE
        GROUP BY utc_second, nb_id, rnti
        ORDER BY utc_second, nb_id, rnti
    """)

    header = ["utc_second", "recv_tstamp_us", "source_tstamp_us",
              "nb_id", "rnti", "dl_aggr_prb", "ul_aggr_prb", "samples"]
    header += [f"{c}_avg" for c in extras]
    n = 0
    first_recv = last_recv = first_source = last_source = None
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
            n += 1
            recv = int(row[1])
            source = int(row[2])
            first_recv = recv if first_recv is None else min(first_recv, recv)
            last_recv = recv if last_recv is None else max(last_recv, recv)
            first_source = source if first_source is None else min(first_source, source)
            last_source = source if last_source is None else max(last_source, source)
    db.close()
    print(f"wrote {n} per-second rows to {out_path} (extras: {extras or 'none'})")
    if n > 0 and last_recv > first_recv:
        wall_span = (last_recv - first_recv) / 1_000_000
        source_span = (last_source - first_source) / 1_000_000
        print(
            f"clock spans: receipt={wall_span:.3f}s "
            f"source={source_span:.3f}s source/wall={source_span / wall_span:.6f}"
        )


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: agg_prb.py <xapp_db> <out_csv> [monitor_log]")
    main(*sys.argv[1:])
