#!/usr/bin/env python3
# Usage: cfq_changelog.py init            <repo-root> <branch> <base> <batch>
#        cfq_changelog.py finish          <repo-root> <branch> <batch-dir>
#        cfq_changelog.py reserve         <repo-root> <batchNumber> <batch>
#        cfq_changelog.py rename-batch    <repo-root> <old-batch> <new-batch>
#        cfq_changelog.py branch-for      <repo-root> <batch>
#        cfq_changelog.py commit-message  <repo-root> <batch> <phase> <status> <message-file>
#        cfq_changelog.py ensure          <repo-root>
#        cfq_changelog.py migrate         <repo-root>
#        cfq_changelog.py max-batch-number <repo-root>
"""Appends/completes entries in <repo-root>/<changelogFile>, one block per batch.

Ported from cfq-changelog.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
file format, exit codes) is the invariant this file preserves. No YAML library is used on purpose:
a general parser would round-trip the file through its own emitter and destroy comments, field
order and layout, all of which this format relies on (batchNumber is always immediately followed
by batch, read positionally by cfq_batch_id-style tooling). Line-oriented parsing with `re`/plain
string ops is what preserves the file untouched outside the lines actually being written.

Version-free schema: no batch ever carries a version/appVersion/cfqVersion field.
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors, render  # noqa: E402

PROG = "cfq_changelog.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

NUMBERED_PREFIX_RE = re.compile(r"^([0-9]+)-[0-9]{4}-[0-9]{2}-[0-9]{2}-")


# ---- changelog file location -------------------------------------------------------------

def changelog_file(repo):
    """<repo>/<changelogFile>, or None when changelogFile is disabled (empty). Deliberately does
    not pass --repo to the dispatcher call -- matches cfq-changelog.sh's own (global-only) read,
    verbatim."""
    out = subprocess.run(
        [str(CFQ_BIN), "settings", "get", "changelogFile"], capture_output=True, text=True,
    )
    rel = out.stdout.strip()
    if not rel:
        return None
    return f"{repo}/{rel}"


# ---- report.json -> phases: yaml block ----------------------------------------------------

def phases_yaml(report_path):
    try:
        data = json.loads(pathlib.Path(report_path).read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    phases = data.get("phases") if isinstance(data, dict) else None
    if not isinstance(phases, list):
        return ""

    def val(p, key):
        v = p.get(key) if isinstance(p, dict) else None
        return "" if v is None or v is False else v

    blocks = []
    for p in phases:
        blocks.append(
            "    - phase: " + json.dumps(val(p, "phase"), ensure_ascii=False) + "\n"
            "      status: " + json.dumps(val(p, "status"), ensure_ascii=False) + "\n"
            "      summary: " + json.dumps(val(p, "summary"), ensure_ascii=False)
        )
    return "\n".join(blocks)


# ---- batch identity -------------------------------------------------------------------------

def parse_batch_number(batch):
    """New-format batch directory names are <digits>-<YYYY-MM-DD>-<slug> (the number precedes
    the date); legacy names start directly with the date. Returns the plain int on a match, None
    on no match -- never guesses from a version tag or branch name."""
    m = NUMBERED_PREFIX_RE.match(batch)
    return int(m.group(1)) if m else None


# ---- block rendering --------------------------------------------------------------------------

def render_block(number, batch, branch, base, started, status, legacy, finished, phases):
    """Renders one changelog block as text, no trailing newline (the caller adds exactly one when
    writing/appending -- mirrors cfq-changelog.sh's command-substitution + `printf '%s\\n'` shape)."""
    lines = [f"- batchNumber: {number}", f"  batch: {batch}"]
    if branch:
        lines.append(f"  branch: {branch}")
    if base:
        lines.append(f"  base: {base}")
    if started:
        lines.append(f"  started: {started}")
    lines.append(f"  status: {status}")
    lines.append(f"  legacy: {legacy}")
    if finished:
        lines.append(f"  finished: {finished}")
    if phases:
        lines.append("  phases:")
        lines.append(phases)
    return "\n".join(lines)


# ---- line-oriented block parsing ------------------------------------------------------------
# Read by scanning lines into blocks delimited by "- batchNumber:" markers, so an untouched entry
# can be written back exactly as it was read. Write by replacing only the lines that changed,
# never by re-emitting the whole document from a data structure.

def read_lines(path):
    return pathlib.Path(path).read_text().splitlines(keepends=True)


def block_starts(lines):
    return [i + 1 for i, l in enumerate(lines) if l.startswith("- batchNumber:")]


def block_end_line(lines, start):
    """Exclusive end line of the block starting at `start` (next block start - 1, or EOF)."""
    for i in range(start, len(lines)):
        if lines[i].startswith("- batchNumber:"):
            return i
    return len(lines)


def block_text(lines, start):
    return "".join(lines[start - 1:block_end_line(lines, start)])


def block_field(block, prefix):
    """First line in `block` starting with `prefix`, with `prefix` and any following spaces
    stripped -- None when no line matches (distinct from a line matching with an empty value)."""
    for line in block.split("\n"):
        if line.startswith(prefix):
            return line[len(prefix):].lstrip(" ")
    return None


def find_block_start(file, field, value, want_status=None):
    """Last matching block's start line (1-indexed), None on no match."""
    lines = read_lines(file)
    hit = None
    for start in block_starts(lines):
        block = block_text(lines, start)
        if (block_field(block, field) or "") != value:
            continue
        if want_status is not None and (block_field(block, "  status:") or "") != want_status:
            continue
        hit = start
    return hit


def append_block(target, block):
    with open(target, "a") as f:
        f.write(block + "\n")


def replace_block(target, start, new_block):
    lines = read_lines(target)
    end = block_end_line(lines, start)
    new_text = "".join(lines[:start - 1]) + new_block + "\n" + "".join(lines[end:])
    tmp = f"{target}.tmp"
    with open(tmp, "w") as f:
        f.write(new_text)
    os.replace(tmp, target)


# ---- git trailer scanning (ensure's one-time bootstrap) --------------------------------------

def is_git_repo(repo):
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--git-dir"], capture_output=True, text=True,
    ).returncode == 0


def scan_trailer_max(repo):
    """Highest CFQ-Batch-Number trailer reachable in repo history; 0 if none. Uses Git's own
    trailer-aware formatting, never free-text grep, so a stray "CFQ-Batch-Number" in a commit
    body/subject can't be mistaken for a real trailer."""
    out = subprocess.run(
        ["git", "-C", repo, "log", "--all", "--no-color",
         "--pretty=format:%(trailers:key=CFQ-Batch-Number,valueonly)"],
        capture_output=True, text=True,
    )
    m = 0
    for line in out.stdout.split("\n"):
        if re.fullmatch(r"[0-9]+", line):
            v = int(line)
            if v > m:
                m = v
    return m


def scan_trailer_batch_for_max(repo, max_number):
    """Human-readable CFQ-Batch trailer of the (first, newest-first) commit that carries the
    given max CFQ-Batch-Number. Each lookup is scoped to one commit at a time (-1 <hash>), never
    combined with --all -- a history-wide scan must always key off CFQ-Batch-Number, never off
    the human-readable CFQ-Batch trailer."""
    hashes = subprocess.run(
        ["git", "-C", repo, "log", "--all", "--format=%H"], capture_output=True, text=True,
    ).stdout.split()
    for h in hashes:
        val = subprocess.run(
            ["git", "-C", repo, "log", "-1",
             "--format=%(trailers:key=CFQ-Batch-Number,valueonly)", h],
            capture_output=True, text=True,
        ).stdout.strip()
        if val == str(max_number):
            return subprocess.run(
                ["git", "-C", repo, "log", "-1", "--format=%(trailers:key=CFQ-Batch,valueonly)", h],
                capture_output=True, text=True,
            ).stdout.strip()
    return ""


# ---- verbs ------------------------------------------------------------------------------------

def cmd_init(args):
    repo, branch, base, batch = args.repo, args.branch, args.base, args.batch
    target = changelog_file(repo)
    if target is None:
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)

    number = parse_batch_number(batch)
    legacy = "false" if number is not None else "true"
    number_str = str(number) if number is not None else "null"

    started = date.today().isoformat()
    reserved_start = None
    if os.path.isfile(target):
        reserved_start = find_block_start(target, "  batch:", batch, want_status="parked")

    new_block = render_block(number_str, batch, branch, base, started, "in-progress", legacy, "", "")
    if reserved_start is not None:
        replace_block(target, reserved_start, new_block)
    else:
        append_block(target, new_block)


def cmd_finish(args):
    repo, branch, batch_dir = args.repo, args.branch, args.batch_dir
    target = changelog_file(repo)
    if target is None:
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    batch = os.path.basename(os.path.normpath(batch_dir))
    report = os.path.join(batch_dir, "report.json")
    finished = date.today().isoformat()
    phases = phases_yaml(report)

    matched_start = None
    if os.path.isfile(target):
        matched_start = find_block_start(target, "  branch:", branch, want_status="in-progress")

    if matched_start is not None:
        lines = read_lines(target)
        block = block_text(lines, matched_start)
        number = block_field(block, "- batchNumber:") or "null"
        batchname = block_field(block, "  batch:")
        if batchname:
            batch = batchname
        base = block_field(block, "  base:") or ""
        started = block_field(block, "  started:") or ""
        legacy = block_field(block, "  legacy:") or "true"
        new_block = render_block(number, batch, branch, base, started, "done", legacy, finished, phases)
        replace_block(target, matched_start, new_block)
    else:
        # No matching in-progress block (different branch last, malformed file, missing file, or
        # an init that never ran): append a new done entry -- a stray extra entry beats one that
        # silently drops phases.
        number = parse_batch_number(batch)
        legacy = "false" if number is not None else "true"
        number_str = str(number) if number is not None else "null"
        started = ""
        if os.path.isfile(report):
            try:
                data = json.loads(pathlib.Path(report).read_text())
                s = data.get("started") if isinstance(data, dict) else None
                started = (s if isinstance(s, str) else "")[:10]
            except (OSError, json.JSONDecodeError):
                started = ""
        new_block = render_block(number_str, batch, branch, "", started, "done", legacy, finished, phases)
        append_block(target, new_block)


def cmd_reserve(args):
    repo, number, batch = args.repo, args.number, args.batch
    if not re.fullmatch(r"[0-9]+", number or ""):
        errors.die(f"{PROG}: reserve: batchNumber must be a positive integer, got '{number}'")
    target = changelog_file(repo)
    if target is None:
        errors.die(f"{PROG}: reserve: changelogFile is disabled, cannot reserve a numbered batch")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.isfile(target):
        existing_lines = set(pathlib.Path(target).read_text().split("\n"))
        if f"- batchNumber: {number}" in existing_lines or f"  batch: {batch}" in existing_lines:
            errors.die(f"{PROG}: reserve: duplicate batchNumber/batch identity: {number}/{batch}")
    block = "\n".join([f"- batchNumber: {number}", f"  batch: {batch}", "  status: parked"])
    append_block(target, block)


def cmd_rename_batch(args):
    """Narrow width-migration helper: rewrites only the `batch:` field of the block currently
    matching <old-batch>. A no-op when the changelog is disabled, missing, or no block matches --
    the caller re-derives its rewrite set from current state, so an already-renamed entry
    harmlessly matching nothing here is expected, not an error."""
    repo, old, new = args.repo, args.old, args.new
    target = changelog_file(repo)
    if target is None or not os.path.isfile(target):
        return
    start = find_block_start(target, "  batch:", old)
    if start is None:
        return
    lines = read_lines(target)
    block = block_text(lines, start)
    new_lines = [f"  batch: {new}" if line.startswith("  batch: ") else line for line in block.split("\n")]
    replace_block(target, start, "\n".join(new_lines))


def cmd_branch_for(args):
    """Read-only: the branch persisted for <batch> in the ledger (any status), no output on no
    match, missing file or disabled changelog. Authoritative source for cfq-branch.sh's
    continue-mode check -- more precise than re-deriving a branch from a slug suffix match."""
    repo, batch = args.repo, args.batch
    target = changelog_file(repo)
    if target is None or not os.path.isfile(target):
        return
    start = find_block_start(target, "  batch:", batch)
    if start is None:
        return
    lines = read_lines(target)
    block = block_text(lines, start)
    val = block_field(block, "  branch:")
    if val is not None:
        print(val)


def cmd_commit_message(args):
    """Appends the standard CFQ-* trailer block to a numbered batch's phase-commit message via
    git interpret-trailers, so the trailers land in the same trailing block as any existing
    trailer (e.g. Co-Authored-By) rather than a second machine section. Legacy (unnumbered)
    batches pass the message through unchanged -- no number is ever invented for them."""
    repo, batch, phase, status, message_file = (
        args.repo, args.batch, args.phase, args.status, args.message_file,
    )
    if status != "green":
        errors.die(f"{PROG}: commit-message: status must be 'green', got '{status}'")
    number = parse_batch_number(batch)
    if number is not None:
        subprocess.run(
            [
                "git", "-C", repo, "interpret-trailers", "--trim-empty",
                "--trailer", f"CFQ-Batch-Number={number}",
                "--trailer", f"CFQ-Batch={batch}",
                "--trailer", f"CFQ-Phase={phase}",
                "--trailer", f"CFQ-Phase-Status={status}",
                message_file,
            ],
            check=True,
        )
    else:
        with open(message_file, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)


def cmd_ensure(args):
    repo = args.repo
    target = changelog_file(repo)
    if target is None:
        print(render.dump_json({"source": "disabled", "max": 0, "path": None}))
        return
    if os.path.isfile(target):
        print(render.dump_json({"source": "exists", "max": None, "path": target}))
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)

    max_number = 0
    source = "empty"
    batchctx = ""
    if is_git_repo(repo):
        scanned = scan_trailer_max(repo)
        if scanned > 0:
            max_number = scanned
            source = "git-trailer"
            batchctx = scan_trailer_batch_for_max(repo, max_number)

    if source == "git-trailer":
        lines = [f"- batchNumber: {max_number}"]
        if batchctx:
            lines.append(f"  batch: {batchctx}")
        lines.append("  status: recovered")
        pathlib.Path(target).write_text("\n".join(lines) + "\n")
    else:
        pathlib.Path(target).write_text("")

    print(render.dump_json({"source": source, "max": max_number, "path": target}))


def cmd_migrate(args):
    """Deliberately not resolved via changelogFile for the OLD file: `cfq.changelog.yml` is the
    old, no-longer-configurable root-level default (predates the .claude/cfq/ relocation), not a
    copy of the current schema default."""
    repo = args.repo
    old = os.path.join(repo, "cfq.changelog.yml")
    target = changelog_file(repo)
    if target is None or not os.path.isfile(old):
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    pathlib.Path(target).touch(exist_ok=True)

    existing_batches = set()
    for line in pathlib.Path(target).read_text().split("\n"):
        if line.startswith("  batch: "):
            existing_batches.add(line[len("  batch: "):].lstrip(" "))

    old_lines = read_lines(old)
    starts = [i + 1 for i, l in enumerate(old_lines) if l.startswith("- version:")]

    new_blocks = []
    for start in starts:
        # block_end_line keys off "- batchNumber:"; the legacy file uses "- version:" instead, so
        # the end of each block is computed against that marker here.
        end = next((s - 1 for s in starts if s > start), len(old_lines))
        block = "".join(old_lines[start - 1:end])

        obatch = block_field(block, "  batch:")
        if not obatch or obatch in existing_batches:
            continue

        obranch = block_field(block, "  branch:") or ""
        obase = block_field(block, "  base:") or ""
        ostarted = block_field(block, "  started:") or ""
        ofinished = block_field(block, "  finished:") or ""
        ostatus = block_field(block, "  status:") or "done"

        ophases_lines = []
        capturing = False
        for line in block.split("\n"):
            if capturing:
                ophases_lines.append(line)
            if line.startswith("  phases:"):
                capturing = True
        ophases = "\n".join(ophases_lines)

        new_blocks.append(render_block("null", obatch, obranch, obase, ostarted, ostatus, "true", ofinished, ophases))

    if new_blocks:
        with open(target, "a") as f:
            for b in new_blocks:
                f.write(b + "\n")


def cmd_max_batch_number(args):
    repo = args.repo
    target = changelog_file(repo)
    if target is None or not os.path.isfile(target):
        print(0)
        return
    m = 0
    prefix = "- batchNumber: "
    for line in pathlib.Path(target).read_text().split("\n"):
        if line.startswith(prefix):
            v = line[len(prefix):]
            if re.fullmatch(r"[0-9]+", v) and int(v) > m:
                m = int(v)
    print(m)


# ---- argument parsing ---------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("init")
    p.add_argument("repo")
    p.add_argument("branch")
    p.add_argument("base")
    p.add_argument("batch")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("finish")
    p.add_argument("repo")
    p.add_argument("branch")
    p.add_argument("batch_dir")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("reserve")
    p.add_argument("repo")
    p.add_argument("number")
    p.add_argument("batch")
    p.set_defaults(func=cmd_reserve)

    p = sub.add_parser("rename-batch")
    p.add_argument("repo")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename_batch)

    p = sub.add_parser("branch-for")
    p.add_argument("repo")
    p.add_argument("batch")
    p.set_defaults(func=cmd_branch_for)

    p = sub.add_parser("commit-message")
    p.add_argument("repo")
    p.add_argument("batch")
    p.add_argument("phase")
    p.add_argument("status")
    p.add_argument("message_file")
    p.set_defaults(func=cmd_commit_message)

    p = sub.add_parser("ensure")
    p.add_argument("repo")
    p.set_defaults(func=cmd_ensure)

    p = sub.add_parser("migrate")
    p.add_argument("repo")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("max-batch-number")
    p.add_argument("repo")
    p.set_defaults(func=cmd_max_batch_number)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(
            f"usage: {PROG} init <repo-root> <branch> <base> <batch> | "
            f"finish <repo-root> <branch> <batch-dir> | "
            f"reserve <repo-root> <batchNumber> <batch> | "
            f"rename-batch <repo-root> <old-batch> <new-batch> | "
            f"branch-for <repo-root> <batch> | "
            f"commit-message <repo-root> <batch> <phase> <status> <message-file> | "
            f"ensure <repo-root> | migrate <repo-root> | max-batch-number <repo-root>"
        )
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
