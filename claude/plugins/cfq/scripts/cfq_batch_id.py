#!/usr/bin/env python3
# Usage: cfq_batch_id.py next          <repo-root> <YYYY-MM-DD> <slug>
#        cfq_batch_id.py allocate      <repo-root> <YYYY-MM-DD> <slug>
#        cfq_batch_id.py migrate-width <repo-root>
#        cfq_batch_id.py reconcile     <repo-root> [--fix]
#
# `allocate` performs an automatic width migration itself when the next number needs an extra
# digit and the active queue is empty (BATCH_WIDTH_MIGRATION_BLOCKED otherwise) -- the normal PFQ
# path never needs a separate manual step. `migrate-width` is the same operation exposed directly,
# useful for recovery/testing; it is a no-op (`status: OK`) when no migration is currently needed.
# `reconcile` compares queue directories against the ledger's numbered entries and reports/repairs
# the gap BATCH_LEDGER_MISMATCH refuses to allocate through -- see reconcile() below.
"""Repository-local CFQ batch-number allocation: a stable, version-free identity assigned once at
PFQ park time. Never derives a number from a Git branch name or an application/package version;
the only Git-history fallback is cfq_changelog.py's one-time trailer bootstrap inside `ensure`, for
a ledger that does not exist yet. Width is an implementation invariant (see
BATCH_WIDTH_MIGRATION_REQUIRED below), not a user-facing setting.

Ported from cfq-batch-id.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
JSON shapes, exit codes) is the invariant this file preserves.
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors, render  # noqa: E402
from cfq_lib import paths  # noqa: E402

PROG = "cfq_batch_id.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"
CHANGELOG_PY = SCRIPT_DIR / "cfq_changelog.py"

# New-name grammar: <digits>-<YYYY-MM-DD>-<slug>, digits precede the date. A legacy
# <YYYY-MM-DD>-<slug> name never satisfies this (its own date would have to double as the digit
# run AND be immediately followed by another full date), so legacy directories are ignored, not
# specially cased.
NUMBERED_RE = re.compile(r"^([0-9]{3,})-([0-9]{4}-[0-9]{2}-[0-9]{2})-([a-z0-9][a-z0-9-]*)$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_WIDTH = 3

# mkdir-based mutex, same staleness spirit as cfq_lock.py: a lock older than LOCK_STALE_S is
# presumed abandoned (crashed holder) and reclaimed rather than waited on forever. Allocation is
# fast, so a short stale window is enough -- this is not a long-lived session lock.
LOCK_STALE_S = 10


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


# ---- digit-run width helpers ------------------------------------------------------------------

def numbered_width(name):
    """The digit-run width of a numbered identifier, None on no match. Never partially parses: a
    name either satisfies the full grammar or is ignored outright."""
    m = NUMBERED_RE.match(name)
    return len(m.group(1)) if m else None


def numbered_number(name):
    m = NUMBERED_RE.match(name)
    return int(m.group(1)) if m else None


def _dir_names(d):
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        if name == "done":
            continue
        if os.path.isdir(os.path.join(d, name)):
            yield name


# ---- changelog location -------------------------------------------------------------------------

def changelog_file_setting():
    """The raw, repo-relative changelogFile setting value, empty when disabled. Deliberately
    global-only (no --repo) -- matches cfq_changelog.py's own changelog_file() read, verbatim."""
    return cfq_run("settings", "get", "changelogFile").stdout.strip()


def changelog_path(repo):
    rel = changelog_file_setting()
    if not rel:
        return None
    return f"{repo}/{rel}"


# ---- widths and numbers observed across queue + ledger --------------------------------------

def observed_widths(repo):
    """Widths seen among existing numbered queue directories and changelog `batch:` entries,
    sorted unique. More than one distinct width is an unresolvable conflict."""
    widths = set()
    for name in _dir_names(paths.impl_dir(repo)):
        w = numbered_width(name)
        if w is not None:
            widths.add(w)
    target = changelog_path(repo)
    if target and os.path.isfile(target):
        for line in pathlib.Path(target).read_text().splitlines():
            if line.startswith("  batch: "):
                w = numbered_width(line[len("  batch: "):].lstrip(" "))
                if w is not None:
                    widths.add(w)
    return sorted(widths)


def queue_max(repo):
    """Highest number among numbered queue directories only (never legacy ones), 0 if none."""
    max_n = 0
    for name in _dir_names(paths.impl_dir(repo)):
        n = numbered_number(name)
        if n is not None and n > max_n:
            max_n = n
    return max_n


def queue_is_empty(repo):
    """True when the active queue -- every planning/open/in-progress batch, i.e. every directory
    directly under impl/ other than "done" -- is completely empty. A width migration may only run
    while this holds; archived/completed batches under impl/done/ don't count."""
    for _ in _dir_names(paths.impl_dir(repo)):
        return False
    return True


# ---- next-identity computation -----------------------------------------------------------------

def compute_next(repo, date, slug):
    """Computes the next identity. Returns (result, ok). Read-only except for cfq_changelog.py's
    own one-time missing-ledger bootstrap, which must run before any number can be computed at
    all."""
    cf = changelog_file_setting()
    if not cf:
        return errors.error_object(
            "BATCH_CHANGELOG_REQUIRED",
            "changelogFile is disabled; numbered batch identity needs the CFQ workflow changelog",
            "set changelogFile before allocating a numbered batch",
        ), False

    cfq_run("changelog", "ensure", repo)
    changelog_max = int(cfq_run("changelog", "max-batch-number", repo).stdout.strip())
    qmax = queue_max(repo)

    if qmax > changelog_max:
        return errors.error_object(
            "BATCH_LEDGER_MISMATCH",
            f"queue directory number {qmax} exceeds changelog max {changelog_max}",
            f"reconcile the queue directory against {cf} -- "
            "`cfq batch reconcile <repo>` reports the gap and `--fix` can close it",
        ), False

    widths = observed_widths(repo)
    if len(widths) > 1:
        return errors.error_object(
            "BATCH_LEDGER_MISMATCH",
            "numbered batch identifiers disagree on width: " + " ".join(str(w) for w in widths),
            "run the width migration before allocating",
        ), False
    width = widths[0] if widths else DEFAULT_WIDTH

    next_n = changelog_max + 1
    digits = len(str(next_n))
    if digits > width:
        return {
            "status": "BATCH_WIDTH_MIGRATION_REQUIRED",
            "nextNumber": next_n,
            "currentWidth": width,
            "requiredWidth": digits,
            "detail": (
                f"next batch number {next_n} needs {digits} digits, current width is {width}"
            ),
            "action": "run the width migration first, then allocate again",
        }, False

    formatted = str(next_n).zfill(width)
    return {
        "status": "OK",
        "batchNumber": next_n,
        "width": width,
        "formatted": formatted,
        "date": date,
        "slug": slug,
        "batch": f"{formatted}-{date}-{slug}",
    }, True


# ---- width migration ------------------------------------------------------------------------

def migrate_width(repo):
    """Deterministic, idempotent width migration. Only two locations can hold a numbered identifier
    while the active queue is empty (the caller's job to verify -- this function doesn't re-check):
    impl/done/ directory names and changelog.yml `batch:` fields. Never touches impl/ itself (empty
    by precondition), Git history, branch names, or legacy unnumbered batches. Re-derives the
    rewrite set from current disk/changelog state on every call rather than tracking a marker, so a
    repeated or interrupted invocation converges to the same end state.

    Target width is the max of the default, the digits the next number needs, and every width
    already observed -- not just "the" current width. compute_next refuses to run at all while more
    than one width is observed (BATCH_LEDGER_MISMATCH), but this function must still be able to
    finish an interrupted prior migration on its own, so it never assumes a single incoming width
    the way compute_next does."""
    target = changelog_path(repo)
    d = paths.impl_done_dir(repo)

    changelog_max = int(cfq_run("changelog", "max-batch-number", repo).stdout.strip())
    next_n = changelog_max + 1
    to = DEFAULT_WIDTH
    if len(str(next_n)) > to:
        to = len(str(next_n))
    for w in observed_widths(repo):
        if w > to:
            to = w

    old_names = []
    if target and os.path.isfile(target):
        for line in pathlib.Path(target).read_text().splitlines():
            if not line.startswith("  batch: "):
                continue
            name = line[len("  batch: "):].lstrip(" ")
            if not name:
                continue
            w = numbered_width(name)
            if w is None or not w < to:
                continue
            old_names.append(name)
    for name in _dir_names(d):
        w = numbered_width(name)
        if w is None or not w < to:
            continue
        old_names.append(name)

    old_names = sorted(set(old_names))

    if not old_names:
        return {"status": "OK", "width": to, "migrated": 0}, True

    # Preflight: compute every destination and fail closed on any collision before mutating
    # anything.
    target_lines = set()
    if target and os.path.isfile(target):
        target_lines = set(pathlib.Path(target).read_text().splitlines())

    pairs = []
    for name in old_names:
        num = numbered_number(name)
        suffix = re.sub(r"^[0-9]+-", "", name)
        new_name = f"{str(num).zfill(to)}-{suffix}"
        if os.path.exists(os.path.join(d, new_name)):
            return errors.error_object(
                "BATCH_WIDTH_MIGRATION_COLLISION",
                f"migration destination already exists on disk: {new_name}",
                f"resolve the collision under {d} before retrying",
            ), False
        if f"  batch: {new_name}" in target_lines:
            return errors.error_object(
                "BATCH_WIDTH_MIGRATION_COLLISION",
                f"migration destination already exists in the changelog: {new_name}",
                f"resolve the collision in {target} before retrying",
            ), False
        pairs.append((name, new_name))

    for old_n, new_n in pairs:
        old_path = os.path.join(d, old_n)
        if os.path.isdir(old_path):
            os.rename(old_path, os.path.join(d, new_n))
        if target and os.path.isfile(target):
            # Direct sibling call: inside a per-pair loop, where a dispatcher exec per iteration
            # is the more expensive trade (see CLAUDE.md's dispatcher-loop-exception note).
            subprocess.run(["python3", str(CHANGELOG_PY), "rename-batch", repo, old_n, new_n])

    return {"status": "OK", "width": to, "migrated": len(pairs)}, True


# ---- reconcile ----------------------------------------------------------------------------------

def numbered_dir_names(repo):
    """All numbered directory names under impl/ (excluding "done" itself) and impl/done/ -- both
    count against the ledger, since a finished batch's directory persists there."""
    names = []
    for d in (paths.impl_dir(repo), paths.impl_done_dir(repo)):
        for name in _dir_names(d):
            if numbered_width(name) is not None:
                names.append(name)
    return names


def ledger_numbered_pairs(target):
    """Numbered ledger entries as (number, batch) pairs. Relies on cfq_changelog.py's fixed field
    order (batchNumber is always immediately followed by batch)."""
    if not target or not os.path.isfile(target):
        return []
    pairs = []
    bn = None
    for line in pathlib.Path(target).read_text().splitlines():
        if line.startswith("- batchNumber: "):
            bn = line[len("- batchNumber: "):]
            continue
        if line.startswith("  batch: "):
            b = line[len("  batch: "):]
            if bn and re.fullmatch(r"[0-9]+", bn):
                pairs.append((bn, b))
            bn = None
    return pairs


def reconcile(repo, fix):
    """Read-only comparison of queue directories against the ledger's numbered entries, plus
    (fix=True) reservation of every orphaned directory, ascending by number, driven through
    cfq_changelog.py reserve rather than writing YAML here. Never deletes anything, never touches
    an orphaned ledger entry (a reserved-but-abandoned number is a legitimate state, not a gap to
    close). Returns (result, ok) -- ok is False when orphanDirs is non-empty in the final
    (post-fix, if requested) state."""
    target = changelog_path(repo)
    if not target:
        return errors.error_object(
            "BATCH_CHANGELOG_REQUIRED",
            "changelogFile is disabled; reconcile needs the CFQ workflow changelog",
            "set changelogFile before reconciling",
        ), False

    dir_pairs = sorted({(numbered_number(n), n) for n in numbered_dir_names(repo)})
    ledger_pairs = sorted(set(ledger_numbered_pairs(target)))
    ledger_names = {name for _, name in ledger_pairs}
    dir_names = {name for _, name in dir_pairs}

    orphan_dirs = [(num, name) for num, name in dir_pairs if name not in ledger_names]
    orphan_entries = [name for _, name in ledger_pairs if name not in dir_names]

    if fix and orphan_dirs:
        for num, name in sorted(orphan_dirs):
            proc = subprocess.run(
                ["python3", str(CHANGELOG_PY), "reserve", repo, str(num), name],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                reserve_out = (proc.stdout + proc.stderr).strip()
                return errors.error_object(
                    "BATCH_ID_CONFLICT",
                    f"reconcile --fix: reserving {name} failed: {reserve_out}",
                    "resolve manually, then retry reconcile",
                ), False
        orphan_dirs = []

    dir_max = max((num for num, _ in dir_pairs), default=0)
    ledger_max = int(cfq_run("changelog", "max-batch-number", repo).stdout.strip())

    result = {
        "status": "OK",
        "orphanDirs": [name for _, name in orphan_dirs],
        "orphanEntries": orphan_entries,
        "dirMax": dir_max,
        "ledgerMax": ledger_max,
    }
    return result, len(orphan_dirs) == 0


# ---- argument validation -----------------------------------------------------------------------

def validate_args(date, slug):
    if not DATE_RE.match(date):
        return errors.error_object(
            "INVALID_ARGUMENT", f"date must be YYYY-MM-DD, got '{date}'", "",
        )
    if not SLUG_RE.match(slug):
        return errors.error_object(
            "INVALID_ARGUMENT", f"slug must match [a-z0-9][a-z0-9-]*, got '{slug}'", "",
        )
    return None


# ---- allocation lock ------------------------------------------------------------------------

def alloc_lockdir(repo):
    return f"{paths.cfq_repo_dir(repo)}/.batch-id.lock"


def acquire_alloc_lock(repo):
    lockdir = alloc_lockdir(repo)
    os.makedirs(os.path.dirname(lockdir), exist_ok=True)
    tries = 0
    while True:
        try:
            os.mkdir(lockdir)
            return True
        except OSError:
            try:
                age = time.time() - os.stat(lockdir).st_mtime
            except OSError:
                age = LOCK_STALE_S + 1
            if age > LOCK_STALE_S:
                try:
                    os.rmdir(lockdir)
                except OSError:
                    pass
                continue
            tries += 1
            if tries > 200:
                return False
            time.sleep(0.05)


def release_alloc_lock(repo):
    try:
        os.rmdir(alloc_lockdir(repo))
    except OSError:
        pass


# ---- verbs ------------------------------------------------------------------------------------

def cmd_next(args):
    err_obj = validate_args(args.date, args.slug)
    if err_obj is not None:
        print(render.dump_json(err_obj))
        sys.exit(1)
    result, ok = compute_next(args.repo, args.date, args.slug)
    print(render.dump_json(result))
    sys.exit(0 if ok else 1)


def cmd_allocate(args):
    repo, date, slug = args.repo, args.date, args.slug
    err_obj = validate_args(date, slug)
    if err_obj is not None:
        print(render.dump_json(err_obj))
        sys.exit(1)

    cfq_run("layout", "ensure", repo)

    if not acquire_alloc_lock(repo):
        print(render.dump_json(errors.error_object(
            "INTERNAL_ERROR",
            "could not acquire the allocation lock",
            "retry once the concurrent allocation finishes",
        )))
        sys.exit(1)

    try:
        result, ok = compute_next(repo, date, slug)
        if not ok:
            if result.get("status") != "BATCH_WIDTH_MIGRATION_REQUIRED":
                print(render.dump_json(result))
                sys.exit(1)

            if not queue_is_empty(repo):
                print(render.dump_json({
                    "status": "BATCH_WIDTH_MIGRATION_BLOCKED",
                    "currentWidth": result["currentWidth"],
                    "nextNumber": result["nextNumber"],
                    "requiredWidth": result["requiredWidth"],
                    "action": "finish or clear all active CFQ batches before parking the next batch",
                }))
                sys.exit(1)

            migrate_result, migrate_ok = migrate_width(repo)
            if not migrate_ok:
                print(render.dump_json(migrate_result))
                sys.exit(1)

            result, ok = compute_next(repo, date, slug)
            if not ok:
                print(render.dump_json(result))
                sys.exit(1)

        number = result["batchNumber"]
        batch = result["batch"]

        # Invariant: a queue directory never exists without its ledger entry. The reservation is
        # written first and the directory second; a number is never un-reserved once burned (see
        # BATCH_ID_CONFLICT below) -- so on a directory-creation failure below, the ledger entry is
        # the state left standing on purpose. That's the recoverable half: `cfq batch reconcile`
        # reports a ledger entry with no directory and a later allocate simply moves past it,
        # whereas the reverse (a directory with no entry) is exactly what breaks numbering.
        reserve_proc = cfq_run("changelog", "reserve", repo, str(number), batch)
        if reserve_proc.returncode != 0:
            reserve_out = (reserve_proc.stdout + reserve_proc.stderr).strip()
            print(render.dump_json(errors.error_object(
                "BATCH_ID_CONFLICT",
                f"changelog reservation for {batch} failed: {reserve_out}",
                "retry; the failed number stays consumed and is never reused",
            )))
            sys.exit(1)

        target_dir = os.path.join(paths.impl_dir(repo), batch)
        try:
            os.mkdir(target_dir)
        except OSError:
            cf_target = changelog_path(repo) or ""
            print(render.dump_json(errors.error_object(
                "INTERNAL_ERROR",
                f"changelog reserved {batch} but the queue directory could not be created; "
                "the number stays consumed",
                f"inspect {target_dir} and {cf_target} manually, then park under a new slug/date",
            )))
            sys.exit(1)

        print(render.dump_json(result))
    finally:
        release_alloc_lock(repo)


def cmd_migrate_width(args):
    repo = args.repo
    if not acquire_alloc_lock(repo):
        print(render.dump_json(errors.error_object(
            "INTERNAL_ERROR",
            "could not acquire the allocation lock",
            "retry once the concurrent allocation finishes",
        )))
        sys.exit(1)
    try:
        result, ok = migrate_width(repo)
        print(render.dump_json(result))
        sys.exit(0 if ok else 1)
    finally:
        release_alloc_lock(repo)


def cmd_reconcile(args):
    result, ok = reconcile(args.repo, args.fix_flag == "--fix")
    print(render.dump_json(result))
    sys.exit(0 if ok else 1)


# ---- argument parsing ---------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("next")
    p.add_argument("repo")
    p.add_argument("date")
    p.add_argument("slug")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("allocate")
    p.add_argument("repo")
    p.add_argument("date")
    p.add_argument("slug")
    p.set_defaults(func=cmd_allocate)

    p = sub.add_parser("migrate-width")
    p.add_argument("repo")
    p.set_defaults(func=cmd_migrate_width)

    p = sub.add_parser("reconcile")
    p.add_argument("repo")
    p.add_argument("--fix", dest="fix_flag", action="store_const", const="--fix", default="")
    p.set_defaults(func=cmd_reconcile)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(
            f"usage: {PROG} next <repo-root> <YYYY-MM-DD> <slug> | "
            "allocate <repo-root> <YYYY-MM-DD> <slug> | "
            "migrate-width <repo-root> | reconcile <repo-root> [--fix]"
        )
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
