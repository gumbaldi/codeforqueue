#!/usr/bin/env python3
# Usage: cfq_telemetry.py record <batch-dir> planning|phase [<phase-slug>]
#        cfq_telemetry.py record <batch-dir> bootstrap <skill-name> <callCount> <durationMs>
#        cfq_telemetry.py sync [<repo-root>]
"""Telemetry for cfq. Aggregates the running session's transcript into the batch report and a
repo-local JSONL. Numbers, timestamps and names only -- never prompt text, never tool arguments.

Ported from cfq-telemetry.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
JSON shapes, text output, exit codes) is the invariant this file preserves.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors  # noqa: E402
from cfq_lib import paths as cfq_lib_paths  # noqa: E402
from cfq_lib import render  # noqa: E402

PROG = "cfq_telemetry.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

USAGE = f"usage: {PROG} record <batch-dir> planning|phase [<phase-slug>] | sync [<repo-root>]"

RECOMMENDED_HEADING_RE = re.compile(r"^## Empfohlene Skills")
HEADING_RE = re.compile(r"^## ")
RECOMMENDED_ITEM_RE = re.compile(r"^- ([A-Za-z0-9:._-]+)")
TS_TRAILING_MS_RE = re.compile(r"\.\d+Z$")


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def git_toplevel(cwd):
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def jqor(value, default):
    """Mirrors jq's `//`: only null (None) and false trigger the fallback, "" stays "" ."""
    return default if value is None or value is False else value


def transcript_path():
    # pwd-based resolution (matches ctx_usage.py), shared via the runtime adapter.
    return cfq_run("runtime", "transcript-path").stdout.strip()


def extract_recommended_skills(dir_, phase):
    if not phase:
        return []
    pf = pathlib.Path(dir_) / f"{phase}.md"
    if not pf.is_file():
        pf = pathlib.Path(dir_) / "done" / f"{phase}.md"
    if not pf.is_file():
        return []
    lines = pf.read_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        if RECOMMENDED_HEADING_RE.match(line):
            start = i
            break
    if start is None:
        return []
    block = [lines[start]]
    for line in lines[start + 1:]:
        block.append(line)
        if HEADING_RE.match(line):
            break
    recommended = []
    for line in block:
        m = RECOMMENDED_ITEM_RE.match(line)
        if m:
            recommended.append(m.group(1))
    return recommended


def parse_transcript(tf):
    entries = []
    with open(tf) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def ts(value):
    if not value:
        return 0
    v = TS_TRAILING_MS_RE.sub("Z", value)
    try:
        dt = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def usage_field(item, field):
    return jqor(((item.get("message") or {}).get("usage") or {}).get(field), 0)


def sums(items):
    inp = sum(usage_field(it, "input_tokens") for it in items)
    out = sum(usage_field(it, "output_tokens") for it in items)
    cache_read = sum(usage_field(it, "cache_read_input_tokens") for it in items)
    cache_creation = sum(usage_field(it, "cache_creation_input_tokens") for it in items)
    return {
        "turns": len(items),
        "input": inp,
        "output": out,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        # cache_read repeats the same prefix on every turn -- summing it across turns counts
        # the same tokens dozens of times. billable_in is the part that is actually new per
        # turn and is the number to compare phases by; cache_read stays available as a raw
        # metric.
        "billable_in": inp + cache_creation,
    }


def bucket(items, keyfn):
    groups = {}
    order = []
    for it in items:
        k = keyfn(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)
    return {k: sums(groups[k]) for k in order}


def build_record(tf, since, kind, phase, batch, repo, recommended):
    entries = parse_transcript(tf)
    t = [
        e for e in entries
        if e.get("type") == "assistant"
        and (e.get("timestamp") or "") != ""
        and (since == "" or e.get("timestamp") > since)
    ]
    sub = [e for e in t if e.get("isSidechain") is True]

    tools = {}
    for it in t:
        content = (it.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                name = c.get("name")
                tools[name] = tools.get(name, 0) + 1

    first_ts = t[0].get("timestamp") if t else None
    last_ts = t[-1].get("timestamp") if t else None
    last = t[-1] if t else {}

    return {
        "schema": 1,
        "kind": kind,
        "repo": repo,
        "batch": batch,
        "phase": phase,
        "session_id": jqor(last.get("sessionId"), ""),
        "branch": jqor(last.get("gitBranch"), ""),
        "cc_version": jqor(last.get("version"), ""),
        "from": jqor(first_ts, ""),
        "until": jqor(last_ts, ""),
        "wallclock_s": (ts(last_ts) - ts(first_ts)) if t else 0,
        "totals": sums(t),
        "by_model": bucket(t, lambda it: jqor((it.get("message") or {}).get("model"), "?")),
        "by_effort": bucket(t, lambda it: jqor(it.get("effort"), "?")),
        "by_skill": bucket(t, lambda it: jqor(it.get("attributionSkill"), "-")),
        "by_plugin": bucket(t, lambda it: jqor(it.get("attributionPlugin"), "-")),
        "tools": tools,
        "subagent": sums(sub),
        "skills_recommended": recommended,
    }


def last_until_for_session(jsonl_path, sid):
    matches = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("session_id") == sid:
                until = obj.get("until")
                matches.append("null" if until is None else until)
    since = matches[-1] if matches else ""
    return "" if since == "null" else since


def cmd_record_bootstrap(dir_, skill, call_count_s, duration_ms_s):
    if not call_count_s.isdigit():
        errors.die(f"{PROG}: callCount must be a non-negative integer")
    if not duration_ms_s.isdigit():
        errors.die(f"{PROG}: durationMs must be a non-negative integer")

    repo = git_toplevel(dir_)
    jsonl = cfq_lib_paths.telemetry_log(repo)
    rec = {
        "schema": 1,
        "kind": "bootstrap",
        "repo": repo,
        "batch": pathlib.Path(dir_).name,
        "skill": skill,
        "call_count": int(call_count_s),
        "duration_ms": int(duration_ms_s),
        "timestamp": now_iso(),
    }
    os.makedirs(os.path.dirname(jsonl), exist_ok=True)
    with open(jsonl, "a") as f:
        f.write(render.dump_json(rec) + "\n")
    print(f"telemetry: bootstrap {rec['skill']} {rec['call_count']} calls / {rec['duration_ms']} ms")


def cmd_record_phase_or_planning(dir_, kind, phase):
    tf = transcript_path()
    if not tf or not os.path.isfile(tf):
        print(f"{PROG}: no transcript found — skipped", file=sys.stderr)
        return

    repo = git_toplevel(dir_)
    jsonl = cfq_lib_paths.telemetry_log(repo)
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")

    since = ""
    if os.path.isfile(jsonl) and sid:
        since = last_until_for_session(jsonl, sid)

    recommended = extract_recommended_skills(dir_, phase)
    batch = pathlib.Path(dir_).name
    rec = build_record(tf, since, kind, phase, batch, repo, recommended)

    os.makedirs(os.path.dirname(jsonl), exist_ok=True)
    with open(jsonl, "a") as f:
        f.write(render.dump_json(rec) + "\n")

    report_path = os.path.join(dir_, "report.json")
    if not os.path.isfile(report_path):
        write_json(report_path, {"repo": repo, "batch": batch, "started": now_iso(), "phases": []})
    data = json.loads(pathlib.Path(report_path).read_text())
    if kind == "planning":
        data["planning"] = rec
    elif data.get("phases"):
        data["phases"][-1]["telemetry"] = rec
    write_json(report_path, data)

    totals = rec["totals"]
    print(
        f"telemetry: {totals['turns']} turns, {totals['output']} out / "
        f"{totals['input'] + totals['cache_read'] + totals['cache_creation']} in"
    )


def cmd_record(rest):
    if len(rest) < 2:
        errors.die(USAGE)
    dir_, kind = rest[0], rest[1]
    if not os.path.isdir(dir_):
        errors.die(f"{PROG}: no such batch directory: {dir_}")

    if kind == "bootstrap":
        if len(rest) < 5:
            errors.die(USAGE)
        cmd_record_bootstrap(dir_, rest[2], rest[3], rest[4])
        return

    phase = rest[2] if len(rest) > 2 else ""
    cmd_record_phase_or_planning(dir_, kind, phase)


def cmd_sync(rest):
    repo = rest[0] if rest else git_toplevel(os.getcwd())
    if not repo:
        return

    target = cfq_run("settings", "get", "telemetrySyncRepo").stdout.strip()
    if target in ("", "null"):
        return

    src = cfq_lib_paths.telemetry_log(repo)
    if not os.path.isfile(src):
        return

    if not os.path.isdir(os.path.join(target, ".git")):
        print(f"{PROG}: {target} is no git repo — sync skipped", file=sys.stderr)
        return

    marker = os.path.join(cfq_lib_paths.cfq_repo_dir(repo), ".telemetry-synced")
    n = 0
    if os.path.isfile(marker):
        raw = pathlib.Path(marker).read_text().strip()
        n = int(raw) if raw.isdigit() else 0

    with open(src) as f:
        lines = f.readlines()
    total = len(lines)
    if total <= n:
        print("telemetry sync: nothing new")
        return

    name = f"{pathlib.Path(repo).name}.jsonl"
    target_file = os.path.join(target, name)
    with open(target_file, "a") as f:
        f.writelines(lines[n:])
    pathlib.Path(marker).write_text(f"{total}\n")

    added = total - n
    # Never fatal: a broken sync repo must not abort an implementation session.
    ok = (
        subprocess.run(["git", "-C", target, "add", "--", name]).returncode == 0
        and subprocess.run(
            ["git", "-C", target, "commit", "-q", "-m", f"telemetry: {pathlib.Path(repo).name} +{added}"]
        ).returncode == 0
        and subprocess.run(["git", "-C", target, "push", "-q"]).returncode == 0
    )
    if ok:
        print(f"telemetry sync: +{added} -> {target_file} (committed, pushed)")
    else:
        print(f"telemetry sync: +{added} written to {target_file}, git step failed (non-fatal)", file=sys.stderr)


def main(argv):
    cmd = argv[0] if argv else ""
    rest = argv[1:]
    if cmd == "record":
        cmd_record(rest)
    elif cmd == "sync":
        cmd_sync(rest)
    else:
        errors.die(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
