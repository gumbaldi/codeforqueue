#!/usr/bin/env python3
# Usage: cfq_scan.py [--format=json|md|tsv|overview|next]
"""The single source of numbers for the dashboard. json (default): one JSON object on stdout:
{ "repos": [ { "path", "plan", "todo", "batches": [
  {name, priority, open, done, archived, report, dependsOn, blocked, unknownDeps, inProgress,
   planning} ] } ] } -- byte-identical to the no-flag output for every existing caller.
md/tsv: one row per batch across all repos (Repo, Batch, Priority, Open/Done, Status), status
one of BLOCKED/PLANNING/IN_PROGRESS/OK per CLAUDE.md's Status Vocabulary (IN_PROGRESS is an
additive per-batch-row extension, same pattern as RFQ's GREEN/RED/MIXED), sorted open-first,
then flagged priority first, then name.
overview: one row per repo (Repo, Plan, Todo, Batches, Status) -- Batches is open/done batch
counts, Status the most severe status among the repo's own batches, same vocabulary as above.
next: one object per repo -- { path, next, reason, blocked, planning } -- next is the batch name
`/ifq` would pick (null if none selectable), reason one of inProgress/priority/order/
multipleInProgress/null, blocked/planning are arrays of batch names. This is the one place the
`/ifq` selection ranking (non-archived, open>0, planning/blocked drop out, a single inProgress
batch wins, otherwise flagged priority first then name) is decided -- see
cfq-ifq-preflight.sh, which consumes this format rather than re-deciding the ranking itself.

Ported from cfq-scan.sh -- a port, not a redesign: the CLI contract (verbs, argument order, text
output, exit codes) is the invariant this file preserves.
"""

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402
from cfq_lib import queue as cfq_queue  # noqa: E402
from cfq_lib import render  # noqa: E402

PROG = "cfq_scan.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

FORMATS = ("json", "md", "tsv", "overview", "next")


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def parse_args(argv):
    fmt = "json"
    for arg in argv:
        if arg.startswith("--format="):
            fmt = arg[len("--format="):]
        else:
            errors.die(f"{PROG}: unknown argument: {arg}")
    if fmt not in FORMATS:
        errors.die(f"{PROG}: unknown --format value '{fmt}' (expected {'|'.join(FORMATS)})")
    return fmt


# ---- candidate discovery ----------------------------------------------------------------------

def find_queue_dirs(root, mindepth=2, maxdepth=4):
    """Directories named "cfq" whose parent is ".claude", between mindepth and maxdepth levels
    below root -- mirrors `find "$root" -mindepth 2 -maxdepth 4 -type d -path '*/.claude/cfq'`.
    Unreadable directories are skipped silently, matching the shell version's `2>/dev/null`."""
    found = []

    def walk(dir_path, depth):
        if depth >= maxdepth:
            return
        try:
            entries = sorted(dir_path.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            child_depth = depth + 1
            if child_depth >= mindepth and entry.name == "cfq" and entry.parent.name == ".claude":
                found.append(entry)
            walk(entry, child_depth)

    walk(root, 0)
    return found


def gather_candidates():
    candidates = set()
    registry_out = cfq_run("registry", "list").stdout
    for line in registry_out.splitlines():
        if line:
            candidates.add(line)

    scan_roots = cfq_run("settings", "get", "scanRoots").stdout.strip()
    for root in filter(None, scan_roots.split(",")):
        if root.startswith("~"):
            root = str(pathlib.Path(os.environ["HOME"])) + root[1:]
        root_path = pathlib.Path(root)
        if not root_path.is_dir():
            continue
        for qdir in find_queue_dirs(root_path):
            candidates.add(str(qdir.parent.parent))

    for repo in candidates:
        # Inner-loop call: a dispatcher exec resolves ../bin/cfq fresh on every iteration, so the
        # direct sibling call is the cheaper trade here -- see CLAUDE.md's dispatcher-loop
        # exception.
        subprocess.run(
            ["python3", str(SCRIPT_DIR / "cfq_registry.py"), "add", repo],
            stdout=subprocess.DEVNULL,
        )

    return sorted(candidates)


# ---- record collection -------------------------------------------------------------------------

def md_count(dir_path, pattern):
    if not dir_path.is_dir():
        return 0
    return sum(1 for f in dir_path.glob(pattern) if f.is_file())


def resolve_deps(deps, impl_dir):
    blocked = False
    unknown = []
    for d in deps:
        if (impl_dir / d).is_dir():
            blocked = True
        elif (impl_dir / "done" / d).is_dir():
            pass
        else:
            unknown.append(d)
    return blocked, unknown


def open_batch_record(batch_dir, impl_dir, stale_s):
    deps = cfq_queue.read_depends(batch_dir)
    blocked, unknown = resolve_deps(deps, impl_dir)
    open_n = md_count(batch_dir, "[0-9][0-9]-*.md")
    done_n = md_count(batch_dir / "done", "*.md")

    planning = False
    marker = batch_dir / ".planning"
    if marker.is_file():
        age = time.time() - marker.stat().st_mtime
        planning = age < stale_s

    return {
        "name": batch_dir.name,
        "priority": cfq_queue.read_priority(batch_dir),
        "open": open_n,
        "done": done_n,
        "archived": False,
        "report": (batch_dir / "report.json").is_file(),
        "dependsOn": deps,
        "blocked": blocked,
        "unknownDeps": unknown,
        "inProgress": open_n > 0 and done_n > 0,
        "planning": planning,
    }


def archived_batch_record(batch_dir):
    return {
        "name": batch_dir.name,
        "priority": cfq_queue.read_priority(batch_dir),
        "open": 0,
        "done": md_count(batch_dir, "[0-9][0-9]-*.md"),
        "archived": True,
        "report": (batch_dir / "report.json").is_file(),
        "dependsOn": [],
        "blocked": False,
        "unknownDeps": [],
        "inProgress": False,
        "planning": False,
    }


def scan_repo(repo):
    qdir = pathlib.Path(cfq_lib_paths.cfq_repo_dir(repo))
    if not qdir.is_dir():
        return None

    stale_s_raw = subprocess.run(
        ["python3", str(SCRIPT_DIR / "cfq_settings.py"), "get", "--repo", repo, "sessionStaleSeconds"],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        stale_s = int(stale_s_raw)
    except ValueError:
        stale_s = 0

    plan = md_count(qdir / "plan", "*.md")
    todo = md_count(qdir / "todo", "*.md")

    impl_dir = pathlib.Path(cfq_lib_paths.impl_dir(repo))
    impl_done_dir = pathlib.Path(cfq_lib_paths.impl_done_dir(repo))
    batches = [open_batch_record(b, impl_dir, stale_s) for b in cfq_queue.list_batch_dirs(impl_dir)]
    batches += [archived_batch_record(b) for b in cfq_queue.list_batch_dirs(impl_done_dir)]

    return {"path": repo, "plan": plan, "todo": todo, "batches": batches}


def build_scan_data():
    repos = [r for r in (scan_repo(repo) for repo in gather_candidates()) if r is not None]
    return {"repos": repos}


# ---- format rendering ---------------------------------------------------------------------------

def batch_status(b):
    if b["blocked"]:
        return "BLOCKED"
    if b["planning"]:
        return "PLANNING"
    if b["inProgress"]:
        return "IN_PROGRESS"
    return "OK"


def batch_rows(data):
    rows = []
    for r in data["repos"]:
        repo_name = r["path"].rstrip("/").split("/")[-1]
        for b in r["batches"]:
            rows.append({
                "repo": repo_name,
                "name": b["name"],
                "priority": b["priority"] if b["priority"] != "" else "-",
                "openDone": f'{b["open"]}/{b["done"]}',
                "archived": b["archived"],
                "status": batch_status(b),
            })
    rows.sort(key=lambda row: (row["archived"], 0 if row["priority"] == "high" else 1, row["name"]))
    return rows


def render_md(data):
    lines = ["| Repo | Batch | Priority | Open/Done | Status |", "|---|---|---|---|---|"]
    for row in batch_rows(data):
        lines.append(
            f'| {row["repo"]} | {row["name"]} | {row["priority"]} | {row["openDone"]} | {row["status"]} |'
        )
    return "\n".join(lines)


def render_tsv(data):
    rows = batch_rows(data)
    if not rows:
        # jq's `.[] | @tsv` over an empty array prints nothing at all -- not even a blank line.
        return None
    lines = [
        "\t".join([row["repo"], row["name"], row["priority"], row["openDone"], row["status"]])
        for row in rows
    ]
    return "\n".join(lines)


def repo_status(batches):
    if any(b["blocked"] for b in batches):
        return "BLOCKED"
    if any(b["planning"] for b in batches):
        return "PLANNING"
    if any(b["inProgress"] for b in batches):
        return "IN_PROGRESS"
    return "OK"


def render_overview(data):
    lines = ["| Repo | Plan | Todo | Batches | Status |", "|---|---|---|---|---|"]
    for r in data["repos"]:
        repo_name = r["path"].rstrip("/").split("/")[-1]
        open_n = sum(1 for b in r["batches"] if not b["archived"])
        done_n = sum(1 for b in r["batches"] if b["archived"])
        lines.append(
            f'| {repo_name} | {r["plan"]} | {r["todo"]} | {open_n}/{done_n} | {repo_status(r["batches"])} |'
        )
    return "\n".join(lines)


def rank_next(batches):
    candidates = [b for b in batches if not b["archived"] and b["open"] > 0]
    planning = [b["name"] for b in candidates if b["planning"]]
    blocked = [b["name"] for b in candidates if not b["planning"] and b["blocked"]]
    eligible = [b for b in candidates if not b["planning"] and not b["blocked"]]
    inprog = [b["name"] for b in eligible if b["inProgress"]]

    if len(inprog) > 1:
        result = {"next": None, "reason": "multipleInProgress"}
    elif len(inprog) == 1:
        result = {"next": inprog[0], "reason": "inProgress"}
    else:
        sorted_eligible = sorted(
            eligible, key=lambda b: (0 if b["priority"] == "high" else 1, b["name"])
        )
        if not sorted_eligible:
            result = {"next": None, "reason": None}
        else:
            top = sorted_eligible[0]
            result = {"next": top["name"], "reason": "priority" if top["priority"] == "high" else "order"}

    result["blocked"] = blocked
    result["planning"] = planning
    return result


def render_next(data):
    repos = []
    for r in data["repos"]:
        entry = {"path": r["path"]}
        entry.update(rank_next(r["batches"]))
        repos.append(entry)
    return render.dump_json({"repos": repos})


def main(argv):
    fmt = parse_args(argv)
    data = build_scan_data()

    if fmt == "json":
        print(render.dump_json_pretty(data))
    elif fmt == "md":
        print(render_md(data))
    elif fmt == "tsv":
        out = render_tsv(data)
        if out is not None:
            print(out)
    elif fmt == "overview":
        print(render_overview(data))
    elif fmt == "next":
        print(render_next(data))


if __name__ == "__main__":
    main(sys.argv[1:])
