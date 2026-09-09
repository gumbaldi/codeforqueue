#!/usr/bin/env python3
# Usage: cfq_resume.py <repo-root> <batch-dir>
"""Deterministic batch-state reconstruction for a fresh /ifq session -- no LLM summarization, only
facts read from disk, report.json, and git. Called unconditionally at Step 3b, fresh batch or
resumed alike -- same call, same shape either way.

Ported from cfq-resume.sh -- a port, not a redesign: the CLI contract (output keys, plain-text
error, exit codes) is the invariant this file preserves. `cfq_ifq_preflight.py:102` parses this
output verbatim, including the nested `branch` object from its own internal `branch plan` call.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors, render  # noqa: E402

PROG = "cfq_resume.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

PHASE_NUM_RE = re.compile(r"^([0-9][0-9])-")
DEFAULT_BRANCH_JSON = {
    "mode": None, "batch": None, "batchNumber": None, "branch": None, "base": None,
    "candidates": [],
}


def git(repo_root, *args):
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)


def git_verify(repo_root, ref):
    proc = git(repo_root, "rev-parse", "--verify", "-q", ref)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_branch(repo_root, batch_name):
    proc = subprocess.run(
        [str(CFQ_BIN), "branch", "plan", str(repo_root), batch_name],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return dict(DEFAULT_BRANCH_JSON)
    return json.loads(proc.stdout)


def parse_size(text):
    grabbing = False
    for line in text.splitlines():
        if not grabbing:
            if line.startswith("## Size"):
                grabbing = True
            continue
        if line.strip():
            return line.split()[0]
    return "M"


def scan_phase_files(dirpath):
    if not dirpath.is_dir():
        return []
    return sorted(dirpath.glob("[0-9][0-9]-*.md"))


def phase_num_slug(path):
    m = PHASE_NUM_RE.match(path.name)
    num = m.group(1) if m else ""
    return num, path.stem


def load_report(report_path):
    if not report_path.is_file():
        return None
    return json.loads(report_path.read_text())


def last_green_commit(report, slug):
    if report is None:
        return ""
    matches = [
        p for p in report.get("phases", []) if p.get("phase") == slug and p.get("status") == "green"
    ]
    return matches[-1].get("commit", "") if matches else ""


def cmd_resume(args):
    repo_root = args.repo_root
    batch_dir = pathlib.Path(str(args.batch_dir).rstrip("/"))
    if not batch_dir.is_dir():
        errors.die(f"{PROG}: no such batch directory: {batch_dir}")

    batch_name = batch_dir.name
    report = load_report(batch_dir / "report.json")

    branch_json = resolve_branch(repo_root, batch_name)
    branch_name = branch_json.get("branch") or ""

    phases_open = []
    for f in scan_phase_files(batch_dir):
        num, slug = phase_num_slug(f)
        phases_open.append({"num": num, "slug": slug, "size": parse_size(f.read_text())})

    phases_done = []
    for f in scan_phase_files(batch_dir / "done"):
        num, slug = phase_num_slug(f)
        commit = last_green_commit(report, slug)
        phases_done.append({"num": num, "slug": slug, "commit": commit or None})

    deviations = []
    red_phases = []
    last_commit = ""
    last_commit_source = None
    if report is not None:
        for p in report.get("phases", []):
            if p.get("status") != "green":
                continue
            for text in p.get("deviations", []):
                deviations.append({"phase": p.get("phase"), "text": text})
        red_phases = sorted({p.get("phase") for p in report.get("phases", []) if p.get("status") == "red"})
        green_with_commit = [
            p for p in report.get("phases", [])
            if p.get("status") == "green" and p.get("commit", "")
        ]
        if green_with_commit:
            last_commit = green_with_commit[-1]["commit"]

    if last_commit:
        last_commit_source = "report"
    elif branch_name:
        last_commit = git_verify(repo_root, branch_name) or git_verify(repo_root, f"origin/{branch_name}")
        if last_commit:
            last_commit_source = "branch-tip"

    ctx_path = batch_dir / ".batch-context.md"
    ctx_exists = ctx_path.is_file()

    print(render.dump_json({
        "batch": batch_name, "batchDir": str(batch_dir), "branch": branch_json,
        "phasesOpen": phases_open, "phasesDone": phases_done,
        "lastCommit": last_commit or None, "lastCommitSource": last_commit_source,
        "deviations": deviations, "redPhases": red_phases,
        "batchContext": {"exists": ctx_exists, "path": str(ctx_path) if ctx_exists else None},
    }))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo_root")
    parser.add_argument("batch_dir")
    parser.set_defaults(func=cmd_resume)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
