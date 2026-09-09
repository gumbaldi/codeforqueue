#!/usr/bin/env python3
# Usage: cfq_pfq_preflight.py <repo-root>
"""Single read-only preflight aggregator for plan-for-queue's Step 1: batches every
cfq_settings.py/cfq_scan.py/cfq_registry.py/cfq_maintenance.py call the skill used to issue
separately (Steps 1, 3, 5, 7, 9a, 11) into one JSON object, one process invocation of
cfq_settings.py covering every policy/language key at once. Security (Step 8) stays a separate
call on purpose -- this only reports capability (a security backend reachable at all), never live
finding counts, which need a network round-trip.

Ported from cfq-pfq-preflight.sh -- a port, not a redesign: every output key is frozen.
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import render  # noqa: E402

PROG = "cfq_pfq_preflight.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def cmd_preflight(args):
    repo = args.repo_root

    resolved = git(repo, "rev-parse", "--show-toplevel")
    if resolved.returncode != 0:
        print(render.dump_json({"status": "NO_REPO", "repo": {"root": repo, "known": False}}))
        return
    repo = resolved.stdout.strip()

    settings = json.loads(cfq_run("settings", "list", "--repo", repo).stdout)

    known = False
    registry = cfq_run("registry", "list").stdout
    if repo in registry.splitlines():
        known = True

    scan_data = json.loads(cfq_run("scan").stdout)
    batches = []
    for r in scan_data.get("repos", []):
        if r.get("path") != repo:
            continue
        for b in r.get("batches", []):
            if not b.get("archived") and b.get("open", 0) > 0:
                batches.append({
                    "name": b["name"], "priority": b["priority"], "open": b["open"],
                    "dependsOn": b["dependsOn"],
                })

    maint_raw = cfq_run("maintenance", "due", repo).stdout.split()
    maint_status = maint_raw[0] if maint_raw else ""
    maint_n = int(maint_raw[1]) if len(maint_raw) > 1 else None

    sec_available = bool(shutil.which("gh") or shutil.which("tea"))

    print(render.dump_json({
        "status": "OK",
        "repo": {"root": repo, "known": known},
        "planningPolicy": {
            "planModels": settings["planModels"],
            "allowAnyModel": settings["allowAnyModel"],
            "planExploreModel": settings["planExploreModel"],
            "planExploreModelComplex": settings["planExploreModelComplex"],
            "planBlockedPlugins": settings["planBlockedPlugins"],
            "grillMode": settings["grillMode"],
            "useMattpocockGrilling": settings["useMattpocockGrilling"],
            "usePonytailAudit": settings["usePonytailAudit"],
        },
        "language": {
            "codeLanguage": settings["codeLanguage"],
            "docLanguages": settings["docLanguages"],
            "docLevel": settings["docLevel"],
        },
        "queue": {"openBatches": batches},
        "maintenance": {"status": maint_status, "n": maint_n},
        "security": {"available": sec_available},
        "reporting": {"reportDir": settings["reportDir"], "htmlReport": settings["htmlReport"]},
    }))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    parser.add_argument("repo_root")
    parser.set_defaults(func=cmd_preflight)
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
