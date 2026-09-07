#!/usr/bin/env python3
# Usage: cfq_maintenance.py due <repo-root>
#        cfq_maintenance.py stamp <repo-root>
"""Due-check and marker for the periodic maintenance run (`maintenanceEvery` commits apart).

Ported from cfq-maintenance.sh -- a port, not a redesign: the CLI contract (verbs, argument
order, text output, exit codes) is the invariant this file preserves, including behavior nobody
would design on purpose (a failed `git rev-parse HEAD` on `stamp` swallowed into an empty sha).
"""

import argparse
import os
import pathlib
import subprocess
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402

PROG = "cfq_maintenance.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def cmd_due(args):
    repo = args.repo
    every = cfq_run("settings", "get", "maintenanceEvery").stdout.strip()
    if every == "0":
        print("OFF")
        return

    if git(repo, "rev-parse", "--git-dir").returncode != 0:
        print("DUE 0")
        return

    f = cfq_lib_paths.maintenance_marker(repo)
    if not os.path.isfile(f):
        print("DUE 0")
        return

    parts = pathlib.Path(f).read_text().split()
    sha = parts[1] if len(parts) >= 2 else ""
    if not sha or git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        print("DUE 0")
        return

    n = git(repo, "rev-list", "--count", f"{sha}..HEAD").stdout.strip()
    if int(n) >= int(every):
        print(f"DUE {n}")
    else:
        print(f"NOT_DUE {n}")


def cmd_stamp(args):
    repo = args.repo
    f = cfq_lib_paths.maintenance_marker(repo)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    proc = git(repo, "rev-parse", "HEAD")
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    tmp = f"{f}.tmp"
    with open(tmp, "w") as fh:
        fh.write(f"{date.today().isoformat()} {sha}\n")
    os.replace(tmp, f)
    print(f)


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("due")
    p.add_argument("repo")
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("stamp")
    p.add_argument("repo")
    p.set_defaults(func=cmd_stamp)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(f"usage: {PROG} due <repo-root> | stamp <repo-root>")
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
