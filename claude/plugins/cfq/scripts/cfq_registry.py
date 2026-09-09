#!/usr/bin/env python3
# Usage: cfq_registry.py add <repo-root> | prune | list
"""Manages ~/.claude/code-for-queue/repos.json -- the registry of known repos.

Ported from cfq-registry.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
text output, exit codes) is the invariant this file preserves, including behavior nobody would
design on purpose (no path normalization or directory-ness check on `add`).
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402

PROG = "cfq_registry.py"


def registry_dir():
    return pathlib.Path(os.environ["HOME"]) / ".claude" / "code-for-queue"


def registry_file():
    return registry_dir() / "repos.json"


def ensure():
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = registry_file()
    if not f.is_file():
        f.write_text('{"repos":[]}\n')


def read_repos():
    return json.loads(registry_file().read_text()).get("repos", [])


def write_repos(repos):
    f = registry_file()
    tmp = f"{f}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"repos": repos}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, f)


def cmd_add(args):
    ensure()
    repos = sorted(set(read_repos()) | {args.repo})
    write_repos(repos)


def cmd_prune(args):
    ensure()
    repos = read_repos()
    removed = [r for r in repos if not os.path.isdir(cfq_lib_paths.cfq_repo_dir(r))]
    if removed:
        kept = [r for r in repos if r not in removed]
        write_repos(kept)
        for r in removed:
            print(r)


def cmd_list(args):
    ensure()
    for r in read_repos():
        print(r)


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("add")
    p.add_argument("repo")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("prune")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(f"usage: {PROG} add <repo-root> | prune | list")
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
