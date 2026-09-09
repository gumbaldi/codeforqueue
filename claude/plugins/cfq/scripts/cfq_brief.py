#!/usr/bin/env python3
# Usage: cfq_brief.py <batch-dir> [--phase <NN>|--with-done]
"""Prints the batch briefing block shown before a batch is offered for implementation, or (with
--phase <NN>) a single-phase announcement block, or (with --with-done) the same batch briefing
with done phases listed first, ticked. Read-only.

Ported from cfq-brief.sh -- a port, not a redesign: the CLI contract (argument order, text
output, exit codes) is the invariant this file preserves. The output is read by an agent, so
every blank line and indent is part of the contract.
"""

import argparse
import pathlib
import re
import sys

PROG = "cfq_brief.py"

TITLE_PREFIX_RE = re.compile(r"^# +")


def parse_phase_body(text):
    """Mirrors the shell version's `brief_awk`: pulls the first `# ` heading (title), the token
    on the first non-empty line after `## Size`, the first two non-empty lines after `##
    Context` (raw, untruncated), the last path segment of each `- \\`...\\`` bullet under `##
    Affected Files`, and the first non-empty line inside the first fenced code block under `##
    Verification`."""
    title = None
    size = None
    g = False
    context_lines = []
    k = False
    af = False
    vf = False
    incode = False
    check = None
    files = []

    for line in text.split("\n"):
        non_empty = bool(line.strip())

        if title is None and line.startswith("# "):
            title = TITLE_PREFIX_RE.sub("", line, count=1)
            continue
        if line.startswith("## Size"):
            g = True
            continue
        if g and non_empty:
            size = line.split()[0]
            g = False
            continue
        if line.startswith("## Context"):
            k = True
            continue
        if k and non_empty:
            context_lines.append(line)
            if len(context_lines) >= 2:
                k = False
            continue
        if line.startswith("## Affected Files"):
            af = True
            continue
        if line.startswith("## Verification"):
            af = False
            vf = True
            continue
        if line.startswith("## "):
            af = False
            vf = False
            continue
        if af and line.startswith("- `"):
            p = line[3:].split("`", 1)[0]
            files.append(p.split("/")[-1])
            continue
        if vf and line.startswith("```"):
            incode = not incode
            continue
        if vf and incode and non_empty and check is None:
            check = line

    context = "".join(f"{line} " for line in context_lines)
    return {"title": title, "size": size, "context": context, "files": files, "check": check}


def render_phase(num, fields):
    goal = fields["context"][:220].rstrip(" ")
    lines = [f"PHASE {num} · {fields['title']} · Size {fields['size'] or 'M'}", f"  Goal     {goal}"]
    if fields["files"]:
        lines.append(f"  Files    {', '.join(fields['files'])}")
    if fields["check"]:
        lines.append(f"  Check    {fields['check']}")
    return "\n".join(lines)


def render_brief(num, fields):
    goal = fields["context"][:220]
    return f"{num}  {fields['title']}  [{fields['size'] or 'M'}]  {goal}"


def phase_num(path):
    name = path.name
    return name[:2] if len(name) >= 3 and name[2] == "-" and name[:2].isdigit() else None


def cmd_brief(args):
    d = pathlib.Path(str(args.batch_dir).rstrip("/"))
    if not d.is_dir():
        print(f"{PROG}: no such batch directory: {d}", file=sys.stderr)
        sys.exit(1)

    if args.phase is not None:
        matches = sorted(d.glob(f"{args.phase}-*.md")) + sorted((d / "done").glob(f"{args.phase}-*.md"))
        if not matches:
            print(f"{PROG}: no phase {args.phase} in {d}", file=sys.stderr)
            sys.exit(1)
        f = matches[0]
        print(render_phase(phase_num(f), parse_phase_body(f.read_text())))
        return

    name = d.name
    priority_file = d / ".priority"
    priority = priority_file.read_text().strip() if priority_file.is_file() else ""

    files = sorted(d.glob("[0-9][0-9]-*.md"))
    if priority == "high":
        print(f"{name}  priority=high  phases={len(files)}")
    else:
        print(f"{name}  phases={len(files)}")

    depends_file = d / ".dependsOn"
    if depends_file.is_file():
        for dep in depends_file.read_text().splitlines():
            if dep:
                print(f"dependsOn: {dep}")

    if args.with_done:
        for f in sorted((d / "done").glob("[0-9][0-9]-*.md")):
            print(f"✔ {render_brief(phase_num(f), parse_phase_body(f.read_text()))}")

    for f in files:
        print(render_brief(phase_num(f), parse_phase_body(f.read_text())))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("batch_dir")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--phase")
    group.add_argument("--with-done", action="store_true")
    parser.set_defaults(func=cmd_brief)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
