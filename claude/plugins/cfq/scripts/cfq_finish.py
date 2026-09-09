#!/usr/bin/env python3
# Usage: cfq_finish.py <repo-root> <batch-dir> <branch>
"""Runs the batch-done sequence (move, register, language check, maintenance check, security
diff, changelog, telemetry sync) in a fixed order and prints one JSON object. The lock release is
guaranteed via a `finally` -- a mid-sequence failure must never leave the repo locked.

Ported from cfq-finish.sh -- a port, not a redesign: the nine-call order, the per-call failure
handling (each recorded in `errors`, none aborts the sequence) and every output field are the
invariant this file preserves. The `try/finally` below replaces the shell version's `trap ... EXIT`
one-for-one.
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import paths, render  # noqa: E402

PROG = "cfq_finish.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def cfq_run_merged(*args):
    """Mirrors the shell version's `$("$cfq" ... 2>&1)` -- stdout and stderr combined."""
    return subprocess.run(
        [str(CFQ_BIN), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def capture(proc):
    return proc.stdout.rstrip("\n")


def git(repo_root, *args):
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)


def load_json_or(text, default):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def cmd_finish(args):
    repo_root = args.repo_root
    batch_dir = pathlib.Path(str(args.batch_dir).rstrip("/"))
    branch = args.branch

    errs = []

    def add_error(step, message):
        errs.append(f"{step}: {message}")

    try:
        done_dir = pathlib.Path(paths.impl_done_dir(str(repo_root)))
        done_dir.mkdir(parents=True, exist_ok=True)
        moved = done_dir / batch_dir.name
        if batch_dir.is_dir() and batch_dir != moved:
            shutil.move(str(batch_dir), str(moved))
        batch_dir = moved

        if cfq_run("registry", "add", str(repo_root)).returncode != 0:
            add_error("registry", "cfq_registry.py add failed")

        lang_json = {"issues": 0, "findings": []}
        proc = cfq_run_merged("lang", str(repo_root), "--changed", "main")
        out = capture(proc)
        if proc.returncode == 0:
            data = load_json_or(out, None)
            if data is not None:
                lang_json = {
                    "issues": len(data.get("missing", [])) + len(data.get("stray", []))
                    + len(data.get("unfiled", [])),
                    "findings": (
                        [f"missing: {m}" for m in data.get("missing", [])]
                        + [f"stray: {m}" for m in data.get("stray", [])]
                        + [f"unfiled: {m}" for m in data.get("unfiled", [])]
                    ),
                }
        else:
            add_error("lang", out)

        prose_proc = cfq_run_merged("lang", "prose", str(repo_root), "main")
        prose_out = capture(prose_proc)
        if prose_proc.returncode == 0:
            prose_val = load_json_or(prose_out, None)
            if prose_val is not None:
                lang_json = {**lang_json, "prose": prose_val}
        else:
            add_error("lang", prose_out)

        maintenance = "unknown"
        maint_proc = cfq_run_merged("maintenance", "due", str(repo_root))
        maint_out = capture(maint_proc)
        if maint_proc.returncode == 0:
            maintenance = maint_out
        else:
            add_error("maintenance", maint_out)

        report_path = batch_dir / "report.json"
        existed_before = 0
        if report_path.is_file():
            data = load_json_or(report_path.read_text(), None)
            if data is not None:
                existed_before = len(data.get("security", []))

        planning_json = {}
        now_json = {}
        new_json = {}
        sec_proc = cfq_run_merged("security", str(repo_root))
        sec_now = capture(sec_proc)
        if sec_proc.returncode == 0:
            if cfq_run("report", "security", str(batch_dir), sec_now).returncode == 0:
                data = load_json_or(report_path.read_text(), None) if report_path.is_file() else None
                security_list = data.get("security", []) if data is not None else []
                now_json = (security_list[-1].get("counts") if security_list else {}) or {}
                if existed_before > 0:
                    planning_json = (security_list[0].get("counts") if security_list else {}) or {}
                    for key, n_val in now_json.items():
                        delta = n_val - planning_json.get(key, 0)
                        if delta > 0:
                            new_json[key] = delta
            else:
                add_error("security", "cfq_report.py security failed")
        else:
            add_error("security", sec_now)

        changelog = "changelogFile empty"
        changelog_file = capture(cfq_run("settings", "get", "changelogFile"))
        if changelog_file:
            done_phase_dir = batch_dir / "done"
            phases = len(list(done_phase_dir.glob("[0-9][0-9]-*.md"))) if done_phase_dir.is_dir() else 0
            fin_proc = cfq_run_merged("changelog", "finish", str(repo_root), branch, str(batch_dir))
            fin_out = capture(fin_proc)
            if fin_proc.returncode == 0:
                changelog = f"{batch_dir.name} done · {phases} phases"
                status_out = git(repo_root, "status", "--porcelain", "--", changelog_file).stdout
                if status_out.strip():
                    ok = (
                        git(repo_root, "add", changelog_file).returncode == 0
                        and git(
                            repo_root, "commit", "-q",
                            "-m", f"Mark {branch} batch done in the changelog",
                            "-m", "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>",
                        ).returncode == 0
                        and git(repo_root, "push", "-q").returncode == 0
                    )
                    if ok:
                        changelog = f"{changelog} · committed"
                    else:
                        add_error("changelog", f"git commit/push of {changelog_file} failed")
            else:
                add_error("changelog", fin_out)
                changelog = "error"

        tel_proc = cfq_run_merged("telemetry", "sync", str(repo_root))
        telemetry = capture(tel_proc)
        if tel_proc.returncode != 0:
            add_error("telemetry", telemetry)

        html_report = capture(cfq_run("settings", "get", "--repo", str(repo_root), "htmlReport"))
        if html_report == "true":
            if cfq_run("report", "html", str(batch_dir)).returncode != 0:
                print(f"{PROG}: html report render failed for {batch_dir}", file=sys.stderr)

        print(render.dump_json({
            "moved": str(batch_dir), "lang": lang_json, "maintenance": maintenance,
            "security": {"planning": planning_json, "now": now_json, "new": new_json},
            "changelog": changelog, "telemetry": telemetry, "lock": "released", "errors": errs,
        }))
    finally:
        subprocess.run([str(CFQ_BIN), "lock", "release", str(repo_root)], capture_output=True, text=True)


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo_root")
    parser.add_argument("batch_dir")
    parser.add_argument("branch")
    parser.set_defaults(func=cmd_finish)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
