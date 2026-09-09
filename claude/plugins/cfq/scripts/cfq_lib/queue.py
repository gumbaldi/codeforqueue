"""Shared batch-directory traversal for scripts that walk <repo>/.claude/cfq/impl/ -- the
second-caller module for cfq_scan.py and cfq_queue_overlap.py (both enumerate batch directories
and apply the same batch-name predicate). Directory enumeration, the batch-name predicate and
marker-file reading only -- no formatting, no ranking, no report logic; those stay in the callers.
"""

import re

BATCH_NAME_RE = re.compile(r"^([0-9]+-)?[0-9]{4}-[0-9]{2}-[0-9]{2}-.+")


def is_batch_name(name):
    return bool(BATCH_NAME_RE.match(name))


def list_batch_dirs(impl_dir):
    """Sorted immediate subdirectories of impl_dir whose name matches the batch-name pattern."""
    if not impl_dir.is_dir():
        return []
    return sorted(
        (p for p in impl_dir.iterdir() if p.is_dir() and is_batch_name(p.name)),
        key=lambda p: p.name,
    )


def read_priority(batch_dir):
    """"high" if .priority contains exactly that (after trimming), "" otherwise -- including a
    stale pre-priority-refactor value, a missing file, or no file at all."""
    f = batch_dir / ".priority"
    if not f.is_file():
        return ""
    return "high" if f.read_text().strip() == "high" else ""


def read_depends(batch_dir):
    """Dependency batch names from .dependsOn: one per line, '#' starts a trailing comment,
    blank lines dropped. [] if the file is absent."""
    f = batch_dir / ".dependsOn"
    if not f.is_file():
        return []
    deps = []
    for line in f.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            deps.append(line)
    return deps
