#!/usr/bin/env python3
# Usage: cfq_park.py <repo-root> <batch-dir-name> <high|normal> [<dependsOn-entry>...]
"""Parks a batch directory: creates it, writes .priority/.dependsOn, ensures the local git-exclude
entry, and registers the repo. Idempotent -- safe to re-run with the same arguments. Does not
write phase files; only the planning session that knows their content does that.

Ported from cfq-park.sh -- a port, not a redesign: the CLI contract (verbs, argument order, text
output, exit codes) is the invariant this file preserves.
"""

import argparse
import os
import pathlib
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402

PROG = "cfq_park.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"


def cmd_park(args):
    if args.priority not in ("high", "normal"):
        errors.die(f"{PROG}: priority must be high|normal, got '{args.priority}'")

    subprocess.run([str(CFQ_BIN), "layout", "ensure", args.repo], stdout=subprocess.DEVNULL, check=True)

    d = pathlib.Path(cfq_lib_paths.impl_dir(args.repo)) / args.batch
    d.mkdir(parents=True, exist_ok=True)
    # .planning is written on creation, idempotent (a re-run during the same pfq session
    # refreshes the timestamp as a heartbeat), and removed by plan-for-queue's lint step once
    # the batch is complete -- this is what keeps ifq from picking up a batch pfq is still
    # writing.
    (d / ".planning").write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")

    priority_file = d / ".priority"
    if args.priority == "high":
        priority_file.write_text("high\n")
    else:
        priority_file.unlink(missing_ok=True)

    depends_file = d / ".dependsOn"
    if args.depends:
        depends_file.write_text("\n".join(args.depends) + "\n")

    subprocess.run([str(CFQ_BIN), "registry", "add", args.repo], stdout=subprocess.DEVNULL, check=True)

    print(str(d))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo")
    parser.add_argument("batch")
    parser.add_argument("priority")
    parser.add_argument("depends", nargs="*")
    parser.set_defaults(func=cmd_park)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
