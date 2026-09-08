#!/usr/bin/env python3
# Usage: cfq_lint.py <batch-dir>
"""Lints a parked batch of phase plans before pfq hands it off. Reports, never repairs, never
aborts on soft findings (depends is warn-only and never affects the exit code).

Ported from cfq-lint.sh -- a port, not a redesign: every finding string, the `warn:` prefix, the
exit-code rule and the `## Size` degradation are carried over verbatim.
"""

import argparse
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import queue as cfq_queue  # noqa: E402

PROG = "cfq_lint.py"

BULLET_RE = re.compile(r"^- `([^`]*)`")


def has_heading(text, heading):
    pattern = re.compile(rf"^## {re.escape(heading)}(?:\s|$)", re.MULTILINE)
    return bool(pattern.search(text))


def extract_section(text, heading):
    """Mirrors the shell version's awk state machine, not a sed range: stops before the next
    "## " heading (or at end-of-file), excluding both the opening heading line and the closing
    one -- a sed range would print through EOF when the heading is the file's last one."""
    pattern_start = re.compile(rf"^## {re.escape(heading)}(?:\s|$)")
    out = []
    started = False
    for line in text.split("\n"):
        if not started:
            if pattern_start.match(line):
                started = True
            continue
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out)


def extract_files_section(text):
    """Mirrors `sed -n '/^## Affected Files/,/^## /p'`: inclusive of both the opening heading and
    the closing one (or EOF) -- fine here since only the `- \\`...\\`` bullet lines are used."""
    out = []
    started = False
    for line in text.split("\n"):
        if not started:
            if line.startswith("## Affected Files"):
                started = True
                out.append(line)
            continue
        out.append(line)
        if line.startswith("## "):
            break
    return "\n".join(out)


def phase_num(name):
    return name[:2] if len(name) >= 3 and name[2] == "-" and name[:2].isdigit() else None


def lint_open_file(f, findings):
    name = f.name
    text = f.read_text()

    for heading in ("Context", "Affected Files", "Changes", "Verification", "Size"):
        if not has_heading(text, heading):
            findings.append(f"{name}: sections: missing {heading} heading")

    if has_heading(text, "Verification"):
        verification = extract_section(text, "Verification")
        lines = verification.split("\n")
        has_cmd = any(line.startswith("```") or re.search(r"`[^`]+`", line) for line in lines)
        if not has_cmd:
            findings.append(f"{name}: verification-cmd: no runnable command in Verification")
        if not re.search(r"expect|PASS|exit 0|prints|must", verification, re.IGNORECASE):
            findings.append(f"{name}: verification-expect: no expected result stated")

    if has_heading(text, "Changes"):
        if not re.search(r"\S", extract_section(text, "Changes")):
            findings.append(f"{name}: changes-empty: Changes section is empty")

    if has_heading(text, "Affected Files"):
        files_section = extract_files_section(text)
        if not any(line.startswith("- `") for line in files_section.split("\n")):
            findings.append(f"{name}: files-empty: Affected Files has no entries")

        for line in files_section.split("\n"):
            if not line.startswith("- `"):
                continue
            m = BULLET_RE.match(line)
            path = m.group(1) if m else ""
            if not path:
                continue
            if path.startswith("/"):
                if "(new)" in line:
                    if os.path.exists(path):
                        findings.append(f"{name}: stale-new: {path} already exists")
                elif not os.path.exists(path):
                    findings.append(f"{name}: missing: {path} does not exist")
            else:
                findings.append(f"{name}: abspath: {path} is not absolute")


def check_numbering(all_files, batch_name, findings):
    nums = sorted(phase_num(f.name) for f in all_files if phase_num(f.name))
    seen = {}
    for num in nums:
        seen[num] = seen.get(num, 0) + 1
    dups = sorted(num for num, count in seen.items() if count > 1)
    if dups:
        findings.append(f"{batch_name}: numbering: duplicate phase number(s): {' '.join(dups)}")

    expected = 1
    for cur in sorted(seen):
        want = f"{expected:02d}"
        if cur != want:
            findings.append(f"{batch_name}: numbering: gap before {cur}, expected {want}")
            break
        expected += 1


def check_priority(d, batch_name, findings):
    priority_file = d / ".priority"
    if priority_file.is_file():
        p = priority_file.read_text().strip()
        if p != "high":
            findings.append(f"{batch_name}: priority: .priority is '{p}', want high or no file")


def check_batch_context(d, batch_name, findings):
    ctx_file = d / ".batch-context.md"
    if not ctx_file.is_file():
        findings.append(f"{batch_name}: batch-context: missing .batch-context.md")
        return
    ctx_text = ctx_file.read_text()
    if not has_heading(ctx_text, "Goal"):
        findings.append(f"{batch_name}: batch-context: .batch-context.md missing ## Goal heading")
        return
    if not re.search(r"\S", extract_section(ctx_text, "Goal")):
        findings.append(f"{batch_name}: batch-context: ## Goal section is empty")


def check_depends(d, batch_name, warn_findings):
    repo_qdir = d.parent
    for dep in cfq_queue.read_depends(d):
        if not (repo_qdir / dep).is_dir() and not (repo_qdir / "done" / dep).is_dir():
            warn_findings.append(f"warn: {batch_name}: depends: {dep} does not exist")


def cmd_lint(args):
    d = pathlib.Path(str(args.batch_dir).rstrip("/"))
    if not d.is_dir():
        print(f"{PROG}: no such batch directory: {d}", file=sys.stderr)
        sys.exit(1)
    batch_name = d.name

    open_files = sorted(f for f in d.glob("[0-9][0-9]-*.md") if f.is_file())
    done_files = sorted(f for f in (d / "done").glob("[0-9][0-9]-*.md") if f.is_file()) if (d / "done").is_dir() else []
    n = len(open_files) + len(done_files)

    findings = []
    warn_findings = []

    for f in open_files:
        lint_open_file(f, findings)

    check_numbering(open_files + done_files, batch_name, findings)
    check_priority(d, batch_name, findings)
    check_batch_context(d, batch_name, findings)
    check_depends(d, batch_name, warn_findings)

    for line in findings:
        print(line)
    for line in warn_findings:
        print(line)

    if not findings:
        print(f"OK {n} phases")
        sys.exit(0)
    sys.exit(1)


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("batch_dir")
    parser.set_defaults(func=cmd_lint)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
