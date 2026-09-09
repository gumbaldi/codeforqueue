#!/usr/bin/env python3
# Usage: cfq_lock.py acquire <repo-root> <batch>
#        cfq_lock.py release <repo-root>
#        cfq_lock.py status <repo-root>
"""One lock per repo: only one implement-for-queue session may work a repo at a time. Liveness
comes from the holder's transcript mtime, not from a fixed expiry.

Ported from cfq-lock.sh -- a port, not a redesign: the CLI contract (verbs, argument order, text
output, exit codes) is the invariant this file preserves.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402

PROG = "cfq_lock.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def settings_get(repo, key):
    return cfq_run("settings", "get", "--repo", repo, key).stdout.strip()


def mtime(path):
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return 0


def read_lock(f):
    try:
        return json.loads(pathlib.Path(f).read_text())
    except (OSError, ValueError):
        return {}


def liveness(data, fallback_epoch, stale_s):
    now = int(time.time())
    t = data.get("transcript") or ""
    if t and os.path.isfile(t):
        age = now - mtime(t)
    else:
        age = now - int(fallback_epoch or 0)
    return "alive" if age < int(stale_s) else "dead"


def cmd_acquire(args):
    repo, batch = args.repo, args.batch
    cfq_run("layout", "ensure", repo)
    stale_s = settings_get(repo, "sessionStaleSeconds")
    f = cfq_lib_paths.lockfile(repo)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")
    tpath = cfq_run("runtime", "transcript-path", "--repo", repo, "--exact").stdout.strip()

    if os.path.isfile(f):
        data = read_lock(f)
        holder = data.get("session_id", "")
        hbatch = data.get("batch", "")
        hepoch = data.get("epoch", 0)
        if holder == sid:
            print(f"OK {hbatch} (already held by this session)")
            return
        state = liveness(data, hepoch, stale_s)
        if state == "alive":
            print(f"LOCKED {holder} {hbatch} {data.get('at', '?')}", file=sys.stderr)
            sys.exit(1)
        print(f"TAKEOVER {holder} {hbatch} (stale)", file=sys.stderr)

    payload = {
        "session_id": sid,
        "batch": batch,
        "transcript": tpath,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "epoch": int(time.time()),
    }
    tmp = f"{f}.tmp"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(payload))
    os.replace(tmp, f)
    print(f"OK {batch}")


def cmd_release(args):
    repo = args.repo
    f = cfq_lib_paths.lockfile(repo)
    if not os.path.isfile(f):
        print("FREE")
        return
    data = read_lock(f)
    holder = data.get("session_id", "")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")
    if holder != sid:
        errors.die(f"{PROG}: lock held by {holder}, not releasing")
    os.remove(f)
    print("FREE")


def cmd_status(args):
    repo = args.repo
    f = cfq_lib_paths.lockfile(repo)
    if not os.path.isfile(f):
        print("FREE")
        return
    stale_s = settings_get(repo, "sessionStaleSeconds")
    data = read_lock(f)
    hepoch = data.get("epoch", 0)
    state = liveness(data, hepoch, stale_s)
    print(f"{state.upper()} {data.get('session_id', '')} {data.get('batch', '')} {data.get('at', '')}")


def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("acquire")
    p.add_argument("repo")
    p.add_argument("batch")
    p.set_defaults(func=cmd_acquire)

    p = sub.add_parser("release")
    p.add_argument("repo")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("status")
    p.add_argument("repo")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(f"usage: {PROG} acquire <repo-root> <batch> | release <repo-root> | status <repo-root>")
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
