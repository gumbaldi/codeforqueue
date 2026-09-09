#!/usr/bin/env python3
# Usage: cfq_layout.py ensure <repo-root>
#        cfq_layout.py status <repo-root>
#        cfq_layout.py sync-git-policy <repo-root>
"""Owns the canonical `<repo>/.claude/cfq/` layout and its local Git-state policy. Knows nothing
about the old `.claude/code-for-queue` layout -- that is the isolated migration utility's job
(scripts/migrations/cfq-layout-v1.sh). No `migrate` subcommand here on purpose.

Ported from cfq-layout.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
text output, exit codes) is the invariant this file preserves.
"""

import argparse
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors, render  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402

PROG = "cfq_layout.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

BLOCK_BEGIN = "# BEGIN cfq-managed (do not edit this block by hand)"
BLOCK_END = "# END cfq-managed"
# Paths excluded when gitStatePolicy=local. changelog.yml is CFQ's numbered-batch allocation
# ledger as well as workflow history, so it follows the same local/trackable policy as the rest
# of the queue -- no second Git-state mechanism.
BLOCK_ENTRIES = [
    ".claude/cfq/plan/",
    ".claude/cfq/impl/",
    ".claude/cfq/todo/",
    ".claude/cfq/changelog.yml",
    ".claude/cfq/.lock",
    ".claude/cfq/.maintenance",
    ".claude/cfq/telemetry.jsonl",
]


def settings_get(repo, key):
    cmd = [str(CFQ_BIN), "settings", "get", "--repo", repo, key]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return out.stdout.strip()


def exclude_file(repo):
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return f"{proc.stdout.strip()}/info/exclude"


def strip_block(text):
    """Strips any existing cfq-managed block, returns the rest with no trailing newline --
    mirrors the shell version's `awk ... | $(...)` shape (command substitution strips trailing
    newlines)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    out = []
    skip = False
    for line in lines:
        if line == BLOCK_BEGIN:
            skip = True
            continue
        if line == BLOCK_END:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "\n".join(out)


def do_ensure_dirs(repo):
    os.makedirs(cfq_lib_paths.plan_dir(repo), exist_ok=True)
    os.makedirs(cfq_lib_paths.impl_done_dir(repo), exist_ok=True)
    os.makedirs(cfq_lib_paths.todo_dir(repo), exist_ok=True)


def do_sync_git_policy(repo):
    policy = settings_get(repo, "gitStatePolicy")
    ef = exclude_file(repo)
    if not ef:
        return

    stripped = ""
    if os.path.isfile(ef):
        stripped = strip_block(pathlib.Path(ef).read_text())

    os.makedirs(os.path.dirname(ef), exist_ok=True)
    if policy == "local":
        parts = []
        if stripped:
            parts.append(stripped + "\n")
        parts.append(BLOCK_BEGIN + "\n")
        for entry in BLOCK_ENTRIES:
            parts.append(entry + "\n")
        parts.append(BLOCK_END + "\n")
        tmp = f"{ef}.tmp"
        with open(tmp, "w") as f:
            f.write("".join(parts))
        os.replace(tmp, ef)
    elif policy == "trackable":
        if os.path.isfile(ef):
            tmp = f"{ef}.tmp"
            with open(tmp, "w") as f:
                f.write(stripped + "\n")
            os.replace(tmp, ef)


def cmd_ensure(args):
    do_ensure_dirs(args.repo)
    do_sync_git_policy(args.repo)
    print("OK")


def cmd_status(args):
    ef = exclude_file(args.repo)
    block = "absent"
    if ef and os.path.isfile(ef):
        for line in pathlib.Path(ef).read_text().split("\n"):
            if line == BLOCK_BEGIN:
                block = "present"
                break
    policy = settings_get(args.repo, "gitStatePolicy")
    canonical = os.path.isdir(cfq_lib_paths.cfq_repo_dir(args.repo))
    print(render.dump_json({"gitStatePolicy": policy, "excludeBlock": block, "canonical": canonical}))


def cmd_sync_git_policy(args):
    do_sync_git_policy(args.repo)
    print("OK")


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("ensure")
    p.add_argument("repo")
    p.set_defaults(func=cmd_ensure)

    p = sub.add_parser("status")
    p.add_argument("repo")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync-git-policy")
    p.add_argument("repo")
    p.set_defaults(func=cmd_sync_git_policy)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(f"usage: {PROG} ensure|status|sync-git-policy <repo-root>")
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
