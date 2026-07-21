"""twindash CLI — operate the testbed and build KPIs from a terminal.

    python -m twindash.cli list
    python -m twindash.cli fetch  run_filimo_20260618
    python -m twindash.cli kpis   run_filimo_20260618
    python -m twindash.cli deploy run_filimo_20260618

A calibration sweep is then just a shell loop:
    for r in $(python -m twindash.cli list -q); do python -m twindash.cli fetch $r; done
"""
import argparse
from pathlib import Path

from . import runs, kpis, testbed, schema, settings


def _profiles():
    return settings.profiles_dir()


def _dir(name):
    return _profiles() / name


def cmd_list(args):
    for r in runs.list_runs(_profiles()):
        if args.quiet:
            print(r.name)
        else:
            have = "observed" if (r / schema.OBSERVED_KPIS).exists() else "-"
            print(f"{r.name:42s} {have}")


def cmd_fetch(args):
    cfg = testbed.load_testbed_config()
    testbed.fetch_logs(args.run, _dir(args.run), cfg)
    print("wrote", kpis.save_observed(_dir(args.run)))


def cmd_kpis(args):
    print("wrote", kpis.save_observed(_dir(args.run)))


def cmd_deploy(args):
    testbed.run_script(_dir(args.run) / "deployment" / "dn_commands.sh")


def main():
    ap = argparse.ArgumentParser(prog="twindash")
    sub = ap.add_subparsers(required=True)

    p = sub.add_parser("list"); p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_list)

    for name, fn in (("fetch", cmd_fetch), ("kpis", cmd_kpis), ("deploy", cmd_deploy)):
        p = sub.add_parser(name); p.add_argument("run"); p.set_defaults(func=fn)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
