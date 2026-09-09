#!/usr/bin/env python3
# Usage: cfq_queue_overlap.py <repo-root>
"""Bundles the "## Affected Files" extraction for every open phase across every open batch of a
repo into one process invocation, replacing plan-for-queue's per-file sed loop
(references/queue-check.md Step 5). Never fails, never writes: always prints one JSON object on
stdout and exits 0 -- the caller (a skill) decides what to do with the overlap.

Ported from cfq-queue-overlap.sh -- a port, not a redesign: the CLI contract (argument order, text
output, exit codes) is the invariant this file preserves.
"""

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import paths as cfq_lib_paths  # noqa: E402
from cfq_lib import queue as cfq_queue  # noqa: E402
from cfq_lib import render  # noqa: E402

PROG = "cfq_queue_overlap.py"

BULLET_RE = re.compile(r"^- `([^`]*)`")


def extract_affected_files(text):
    """Mirrors `sed -n '/^## Affected Files/,/^## /p'` piped through
    `sed -n 's/^- \\`\\([^\\`]*\\)\\`.*/\\1/p'`: the range starts at the "## Affected Files"
    heading and runs up to and including the next "## " heading, or to end-of-file if there is
    none; only lines inside that range naming a path in backticks are kept."""
    started = False
    block = []
    for line in text.split("\n"):
        if not started:
            if line.startswith("## Affected Files"):
                started = True
                block.append(line)
            continue
        block.append(line)
        if line.startswith("## "):
            break
    files = []
    for line in block:
        m = BULLET_RE.match(line)
        if m:
            files.append(m.group(1))
    return files


def batch_files(batch_dir):
    files = []
    for f in sorted(batch_dir.glob("[0-9][0-9]-*.md")):
        if not f.is_file():
            continue
        files.extend(extract_affected_files(f.read_text()))
    return files


def cmd_overlap(args):
    impl_dir = pathlib.Path(cfq_lib_paths.impl_dir(args.repo))
    batches = []
    for b in cfq_queue.list_batch_dirs(impl_dir):
        batches.append({"batch": b.name, "files": batch_files(b)})
    print(render.dump_json({"batches": batches}))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo")
    parser.set_defaults(func=cmd_overlap)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
