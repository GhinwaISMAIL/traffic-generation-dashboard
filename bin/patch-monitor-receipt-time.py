from pathlib import Path
import argparse
import re


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Expected one match for {old!r}")
    return text.replace(old, new, 1)


def prepare(root):
    names = [
        "src/xApp/db/db.c",
        "src/xApp/db/db_generic.h",
        "src/xApp/db/sqlite3/sqlite3_wrapper.c",
        "src/xApp/db/sqlite3/sqlite3_wrapper.h",
        "examples/xApp/c/monitor/xapp_gtp_mac_rlc_pdcp_moni.c",
    ]
    original = {name: (root / name).read_text() for name in names}
    if "write_db_at_gen" in original[names[0]]:
        raise ValueError("Receipt-time patch is already present")
    changed = dict(original)
    text = changed[names[0]]
    text = replace_once(text, "  sm_ag_if_rd_t rd;", "  sm_ag_if_rd_t rd;\n  int64_t recv_tstamp;")
    text = replace_once(text,
        "write_db_gen(db->handler, &data[i].id, &data[i].rd);",
        "write_db_at_gen(db->handler, &data[i].id, &data[i].rd, data[i].recv_tstamp);")
    text = replace_once(text,
        "  e2_node_ag_if_t d = { .rd = cp_sm_ag_if_rd(rd) ,",
        "  int64_t const recv_tstamp = (int64_t)time_now_us();\n"
        "  e2_node_ag_if_t d = { .recv_tstamp = recv_tstamp, .rd = cp_sm_ag_if_rd(rd) ,")
    changed[names[0]] = text
    changed[names[1]] = replace_once(changed[names[1]], "#endif",
        "#define write_db_at_gen(T,U,V,W) write_db_sqlite3_at(T,U,V,W)\n\n#endif")
    declaration = "void write_db_sqlite3_at(sqlite3* db, global_e2_node_id_t const* id, sm_ag_if_rd_t const* ag_rd, int64_t recv_tstamp)"
    changed[names[3]] = replace_once(changed[names[3]], "#endif", declaration + ";\n\n#endif")
    text = changed[names[2]]
    for name in ("mac", "rlc", "pdcp", "gtp"):
        pattern = rf"(void write_{name}_stats\([^\n]*?)(\s*\))"
        text, count = re.subn(pattern, r"\1, int64_t recv_tstamp\2", text)
        if count != 1:
            raise ValueError(f"Cannot update {name} writer")
        text = replace_once(text, f"write_{name}_stats(db, id, &rd->{name});",
                            f"write_{name}_stats(db, id, &rd->{name}, recv_tstamp);")
    old = "  int64_t const recv_tstamp = (int64_t)time_now_us();"
    if text.count(old) != 4:
        raise ValueError("Expected four database-time timestamp declarations")
    text = text.replace(old, "  assert(recv_tstamp > 0);")
    old = "void write_db_sqlite3(sqlite3* db, global_e2_node_id_t const* id, sm_ag_if_rd_t const* ag_rd)"
    wrapper = old + "\n{\n  write_db_sqlite3_at(db, id, ag_rd, (int64_t)time_now_us());\n}\n\n"
    text = replace_once(text, old, wrapper + declaration)
    changed[names[2]] = text
    text = changed[names[4]]
    for index in range(4):
        text = replace_once(text, f'const char* i_{index} = "1_ms";',
                            f'const char* i_{index} = "10_ms";')
    text = replace_once(text, '  printf("Test xApp run SUCCESSFULLY\\n");',
        '  printf("monitor_receipt_clock=pre_queue_utc report_interval_ms=10\\n");\n'
        '  printf("monitor_callbacks mac=%lu rlc=%lu pdcp=%lu gtp=%lu\\n", cnt_mac, cnt_rlc, cnt_pdcp, cnt_gtp);\n'
        '  printf("Test xApp run SUCCESSFULLY\\n");')
    changed[names[4]] = text
    return original, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    _, changed = prepare(args.source)
    if args.destination.exists():
        raise ValueError("Destination must be a new directory")
    for name, text in changed.items():
        output = args.destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    print(f"Prepared {len(changed)} files in {args.destination}")


if __name__ == "__main__":
    main()
