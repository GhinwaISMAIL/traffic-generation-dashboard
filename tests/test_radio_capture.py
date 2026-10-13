import importlib.util
from pathlib import Path
import sqlite3

def load_module():
    spec = importlib.util.spec_from_file_location(
        "aggregate_radio", Path(__file__).parents[1] / "bin/aggregate-radio.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_monitor_completeness_counts_reports_per_node(tmp_path):
    module = load_module()
    db = sqlite3.connect(":memory:")
    for table in ("MAC_UE", "RLC_bearer", "PDCP_bearer", "GTP_NGUT"):
        db.execute(f"CREATE TABLE {table}(nb_id INT,tstamp INT,rnti INT)")
        db.executemany(f"INSERT INTO {table} VALUES (?,?,?)",
                       [(1, 100, 5), (1, 100, 6), (2, 100, 7), (2, 100, 8)])
    log = tmp_path / "xapp.log"
    log.write_text("monitor_receipt_clock=pre_queue_utc report_interval_ms=10\n"
                   "monitor_callbacks mac=2 rlc=2 pdcp=2 gtp=2\n")
    result = module.monitor_quality(db, log)
    assert result["complete"] is True
    assert result["timestamp_stage"] == "pre_queue_utc"
    db.execute("DELETE FROM MAC_UE WHERE nb_id=2")
    assert module.monitor_quality(db, log)["complete"] is False
    log.write_text("legacy monitor\n")
    assert module.monitor_quality(db, log)["complete"] is None


def test_capture_uses_receipt_time_and_preserves_buffer_occupancy(tmp_path):
    module = load_module()
    path = tmp_path / "radio.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE MAC_UE(tstamp INT,recv_tstamp INT,nb_id INT,rnti INT,dl_aggr_prb INT,ul_aggr_prb INT)")
    db.executemany("INSERT INTO MAC_UE VALUES (?,?,?,?,?,?)", [
        (100_000, 1_000_000, 1, 5, 10, 20),
        (200_000, 1_500_000, 1, 5, 30, 40),
        (300_000, 2_000_000, 1, 5, 50, 60)])
    db.execute("CREATE TABLE RLC_bearer(recv_tstamp INT,nb_id INT,rnti INT,txbuf_occ_bytes INT,txbuf_occ_pkts INT,rxbuf_occ_bytes INT,rxbuf_occ_pkts INT)")
    db.executemany("INSERT INTO RLC_bearer VALUES (?,?,?,?,?,?,?)", [
        (1_000_000, 1, 5, 100, 2, 20, 1), (1_500_000, 1, 5, 400, 4, 10, 1)])
    db.commit()
    output = tmp_path / "prb.csv"
    module.main(str(path), str(output))
    rows = list(module.csv.DictReader(output.open()))
    assert [int(row["utc_second"]) for row in rows] == [1, 2]
    assert int(rows[0]["dl_aggr_prb"]) == 30
    assert int(rows[0]["source_tstamp_us"]) == 200_000
    rlc = tmp_path / "rlc.csv"
    module.write_rlc(db, rlc)
    record = next(module.csv.DictReader(rlc.open()))
    assert int(record["txbuf_occ_bytes_max"]) == 400


def test_radio_frame_progress_handles_wrap_and_rejects_reset():
    module = load_module()
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE MAC_UE(nb_id INT,recv_tstamp INT,frame INT)")
    db.executemany("INSERT INTO MAC_UE VALUES (?,?,?)", [(1, 1_000_000, 1020),
        (1, 1_050_000, 1), (1, 1_100_000, 6), (1, 1_100_000, 6)])
    result = module.frame_progress(db)["1"]
    assert result["frames_advanced"] == 10
    assert result["frame_time_wall_ratio"] == 1.
    db.execute("INSERT INTO MAC_UE VALUES (1,1150000,5)")
    invalid = module.frame_progress(db)["1"]
    assert invalid["discontinuities"] == 1
    assert invalid["frame_time_wall_ratio"] is None
