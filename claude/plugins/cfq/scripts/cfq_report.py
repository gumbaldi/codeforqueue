#!/usr/bin/env python3
# Usage: cfq_report.py append <batch-dir> <phase-json>
#        cfq_report.py security <batch-dir> <security-json>
#        cfq_report.py set-commit <batch-dir> <phase-slug> <sha>
#        cfq_report.py last-failure <batch-dir> <phase-slug>
#        cfq_report.py summary <batch-dir>
#        cfq_report.py html <batch-dir>
#        cfq_report.py index [--repo <substr>] [--batch <substr>] [--any <substr>] [--text]
#        cfq_report.py detail <batch-dir>
"""Implementation reports per batch. The report lives in the batch directory and travels with it.

Ported from cfq-report.sh -- a port, not a redesign: the CLI contract (verbs, argument order,
JSON shapes, text output, exit codes, error objects) is the invariant this file preserves.
`html.escape()`-equivalent output (`html_escape_jq`) is applied everywhere the shell version's
`@html` jq filter ran, matching jq's exact five-character escape table rather than Python's own
`html.escape` (which spells the apostrophe differently).
"""

import argparse
import json
import math
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import errors, render  # noqa: E402

PROG = "cfq_report.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

# Shared by html's per-batch report and its collected index.html -- one visual language, not two.
REPORT_STYLE_CSS = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fff}
@media (prefers-color-scheme: dark){body{color:#e8e8e8;background:#1a1a1a}code{background:#2a2a2a}}
.meta,.summary{color:#666}section.phase{border-left:4px solid #999;padding:0.5rem 1rem;margin:1rem 0}
section.phase.green{border-color:#2a8f4a}section.phase.red{border-color:#c0392b}
.badge{display:inline-block;padding:0.1rem 0.5rem;border-radius:0.3rem;font-size:0.8rem;color:#fff}
.badge.green{background:#2a8f4a}.badge.red{background:#c0392b}.badge.mixed{background:#c98a1b}
code{background:#f0f0f0;padding:0.1rem 0.3rem;border-radius:0.2rem}
.telemetry{color:#666;font-size:0.85rem}.kv{margin-right:0.4rem}
.goal{color:#666;font-style:italic}
section.repo{margin:1.5rem 0}"""

PHASE_ID_RE = re.compile(r"^[0-9]{2}-.+$")


# ---- jq-semantics helpers -----------------------------------------------------------------

def jq_alt(value, default):
    """Mirrors jq's `//` operator: `value` unless it is null or false."""
    if value is None or value is False:
        return default
    return value


def jq_add(values):
    """Mirrors jq's `add`: sum of the list, or null (None) for an empty list."""
    if not values:
        return None
    total = 0
    for v in values:
        total += v
    return total


def jq_round(x):
    """Mirrors jq's `round` (round-half-away-from-zero), not Python's round-half-to-even."""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&apos;", '"': "&quot;"}


def html_escape_jq(s):
    return "".join(_HTML_ESCAPES.get(ch, ch) for ch in s)


def esc(value):
    """Mirrors the shell script's `def esc: (. // "") | tostring | @html;`."""
    return html_escape_jq(render.tostring(jq_alt(value, "")))


# ---- shared path/settings helpers ----------------------------------------------------------

def repo_root_of(d):
    """Batch dir -> containing repo root, stripping the known queue suffixes. Empty string if
    it isn't nested under either suffix."""
    d = d.rstrip("/")
    try:
        abs_path = str(pathlib.Path(d).resolve()) if os.path.isdir(d) else d
    except OSError:
        abs_path = d
    idx = abs_path.rfind("/.claude/cfq/impl/done/")
    if idx != -1:
        return abs_path[:idx]
    idx = abs_path.rfind("/.claude/cfq/impl/")
    if idx != -1:
        return abs_path[:idx]
    return ""


def settings_get(repo_root, key):
    cmd = [str(CFQ_BIN), "settings", "get"]
    if repo_root:
        cmd += ["--repo", repo_root]
    cmd.append(key)
    out = subprocess.run(cmd, capture_output=True, text=True)
    return out.stdout.strip()


def resolve_html_path(dir_):
    """Path report.html lives (or would live) at for a batch directory, honoring the reportDir
    setting when configured -- same resolution `html` and `index --text`'s file:// lines both
    need. Read-only: a caller that's about to write creates the directory itself."""
    dir_ = dir_.rstrip("/")
    repo_root = repo_root_of(dir_)
    report_dir = settings_get(repo_root, "reportDir")
    if report_dir in ("", "null"):
        return f"{dir_}/report.html"
    return f"{report_dir}/{os.path.basename(repo_root)}/{os.path.basename(dir_)}.html"


def write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def ensure_report(dir_):
    """report.json is created by whoever writes to it first -- planning-time security snapshot
    or the first phase. Same shape in both cases."""
    path = os.path.join(dir_, "report.json")
    if os.path.isfile(path):
        return
    repo = subprocess.run(
        ["git", "-C", dir_, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    ).stdout.strip()
    started = subprocess.run(["date", "-Iseconds"], capture_output=True, text=True).stdout.strip()
    write_json(path, {"repo": repo, "batch": os.path.basename(dir_), "started": started, "phases": []})


def outcome(phases):
    """GREEN if no phase ever went red; RED if any phase's most recent attempt is still red;
    MIXED if every phase that ever went red now shows green as its latest attempt."""
    if not isinstance(phases, list):
        phases = []
    if not any(isinstance(p, dict) and p.get("status") == "red" for p in phases):
        return "GREEN"
    latest = {}
    for p in phases:
        if isinstance(p, dict):
            latest[p.get("phase", "")] = p.get("status")
    return "RED" if any(v == "red" for v in latest.values()) else "MIXED"


def bound_lines(value, n=5):
    s = jq_alt(value, "")
    if not isinstance(s, str):
        s = render.tostring(s)
    lines = s.split("\n")
    if len(lines) <= 2 * n:
        return s
    return "\n".join(lines[:n] + ["…"] + lines[-n:])


# ---- verbs: append / security / set-commit / last-failure / summary ------------------------

def cmd_append(args):
    dir_ = args.dir
    if not os.path.isdir(dir_):
        errors.die(f"{PROG}: no such batch directory: {dir_}")

    # The phase field is the plan file's slug (NN-slug) -- the same value that later goes to
    # set-commit, last-failure and `changelog commit-message`'s CFQ-Phase trailer. A bare number
    # or an empty value breaks every one of those lookups without an error, so it is refused
    # here, at the only point that sees the value before it is persisted.
    try:
        phase_obj = json.loads(args.phase)
    except json.JSONDecodeError:
        phase_obj = None
    phase_id = ""
    if isinstance(phase_obj, dict):
        v = phase_obj.get("phase")
        if isinstance(v, str):
            phase_id = v
    if not PHASE_ID_RE.match(phase_id):
        errors.die(f"{PROG} append: phase must be the full phase slug (NN-slug), got '{phase_id}'")

    f = os.path.join(dir_, "report.json")
    ensure_report(dir_)
    data = json.loads(pathlib.Path(f).read_text())
    data.setdefault("phases", []).append(phase_obj)
    write_json(f, data)
    # Telemetry attaches to the entry just written. Never fatal: a missing transcript must not
    # cost the phase its report.
    subprocess.run([str(CFQ_BIN), "telemetry", "record", dir_, "phase", phase_id])


def cmd_security(args):
    dir_ = args.dir
    if not os.path.isdir(dir_):
        errors.die(f"{PROG}: no such batch directory: {dir_}")
    try:
        snap_obj = json.loads(args.snap)
    except json.JSONDecodeError as e:
        errors.die(f"{PROG}: security: invalid JSON: {e}", code=2)

    f = os.path.join(dir_, "report.json")
    ensure_report(dir_)
    data = json.loads(pathlib.Path(f).read_text())
    at = subprocess.run(["date", "-Iseconds"], capture_output=True, text=True).stdout.strip()
    entry = dict(snap_obj) if isinstance(snap_obj, dict) else snap_obj
    if isinstance(entry, dict):
        entry["at"] = at
    data["security"] = jq_alt(data.get("security"), []) + [entry]
    write_json(f, data)


def cmd_set_commit(args):
    dir_, phase_slug, sha = args.dir, args.phase_slug, args.sha
    if not os.path.isdir(dir_):
        errors.die(f"{PROG}: no such batch directory: {dir_}")
    f = os.path.join(dir_, "report.json")
    if not os.path.isfile(f):
        errors.die(f"{PROG}: no report.json in {dir_}")
    data = json.loads(pathlib.Path(f).read_text())
    phases = data.get("phases", [])
    matches = [i for i, p in enumerate(phases) if isinstance(p, dict) and p.get("phase") == phase_slug]
    if not matches:
        errors.die(
            f"{PROG} set-commit: no phase entry '{phase_slug}' in {f} — the phase field carries "
            "the full phase slug (NN-slug), not the bare number"
        )
    phases[matches[-1]]["commit"] = sha
    write_json(f, data)


def cmd_last_failure(args):
    dir_, phase_slug = args.dir, args.phase_slug
    f = os.path.join(dir_, "report.json")
    if not os.path.isfile(f):
        print(render.dump_json({"found": False}))
        return
    data = json.loads(pathlib.Path(f).read_text())
    candidates = [
        p for p in data.get("phases", [])
        if isinstance(p, dict) and p.get("phase") == phase_slug and p.get("status") == "red"
    ]
    if not candidates:
        print(render.dump_json({"found": False}))
        return
    e = candidates[-1]
    print(render.dump_json({
        "found": True,
        "phase": e.get("phase"),
        "note": jq_alt(e.get("summary"), ""),
        "at": jq_alt(e.get("finished"), ""),
    }))


def _totals_field(totals, key):
    return jq_alt(totals.get(key) if isinstance(totals, dict) else None, 0)


def cmd_summary(args):
    dir_ = args.dir
    f = os.path.join(dir_, "report.json")
    if not os.path.isfile(f):
        sys.exit(1)
    data = json.loads(pathlib.Path(f).read_text())
    phases = data.get("phases", [])
    batch = data.get("batch")
    total = len(phases)
    green = sum(1 for p in phases if isinstance(p, dict) and p.get("status") == "green")
    red = sum(1 for p in phases if isinstance(p, dict) and p.get("status") == "red")
    deviations = 0
    for p in phases:
        d = jq_alt(p.get("deviations") if isinstance(p, dict) else None, [])
        if isinstance(d, list):
            deviations += len(d)

    last_finished = phases[-1].get("finished") if phases and isinstance(phases[-1], dict) else None
    date = jq_alt(jq_alt(last_finished, data.get("started")), "")

    planning = data.get("planning") if isinstance(data.get("planning"), dict) else None
    planning_totals = planning.get("totals") if isinstance(planning, dict) else None
    planning_output = _totals_field(planning_totals, "output")
    planning_turns = _totals_field(planning_totals, "turns")

    phase_outputs, phase_turns = [], []
    model_keys, effort_keys = [], []
    for p in phases:
        tel = p.get("telemetry") if isinstance(p, dict) else None
        totals = tel.get("totals") if isinstance(tel, dict) else None
        phase_outputs.append(_totals_field(totals, "output"))
        phase_turns.append(_totals_field(totals, "turns"))
        by_model = jq_alt(tel.get("by_model") if isinstance(tel, dict) else None, {})
        by_effort = jq_alt(tel.get("by_effort") if isinstance(tel, dict) else None, {})
        if isinstance(by_model, dict):
            model_keys.extend(by_model.keys())
        if isinstance(by_effort, dict):
            effort_keys.extend(by_effort.keys())

    planning_by_model = jq_alt(planning.get("by_model") if isinstance(planning, dict) else None, {})
    planning_by_effort = jq_alt(planning.get("by_effort") if isinstance(planning, dict) else None, {})
    if isinstance(planning_by_model, dict):
        model_keys = list(planning_by_model.keys()) + model_keys
    if isinstance(planning_by_effort, dict):
        effort_keys = list(planning_by_effort.keys()) + effort_keys

    total_output = planning_output + sum(phase_outputs)
    total_turns = planning_turns + sum(phase_turns)
    models = ",".join(sorted(set(model_keys)))
    efforts = ",".join(sorted(set(effort_keys)))

    row = [batch, total, green, red, deviations, date, total_output, planning_output, total_turns, models, efforts]
    print("\t".join(_tsv_field(v) for v in row))


def _tsv_field(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    s = str(v)
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


# ---- verb: html -----------------------------------------------------------------------------

def extract_goal(planfile):
    """First two non-empty lines after a `## Context` heading, truncated to 220 chars -- same
    extraction as cfq-brief.sh's k/ctx/n logic, carried over verbatim."""
    try:
        lines = pathlib.Path(planfile).read_text().splitlines()
    except OSError:
        return ""
    ctx = ""
    collecting = False
    count = 0
    for line in lines:
        if line.startswith("## Context"):
            collecting = True
            continue
        if collecting and line.strip() != "":
            ctx += line + " "
            count += 1
            if count >= 2:
                collecting = False
    return ctx[:220]


def extract_goals(dir_, data):
    phase_ids = sorted({
        p.get("phase") for p in data.get("phases", [])
        if isinstance(p, dict) and p.get("phase")
    })
    goals = {}
    for p in phase_ids:
        planfile = os.path.join(dir_, "done", f"{p}.md")
        if not os.path.isfile(planfile):
            planfile = os.path.join(dir_, f"{p}.md")
        if not os.path.isfile(planfile):
            continue
        goal = extract_goal(planfile)
        if goal:
            goals[p] = goal
    return goals


def section_list(items, title):
    items = jq_alt(items, [])
    if not isinstance(items, list) or len(items) == 0:
        return ""
    lis = "".join(f"<li>{esc(x)}</li>" for x in items)
    return f"<h4>{title}</h4><ul>{lis}</ul>"


def kv(label, value):
    return f'<span class="kv">{label} <b>{html_escape_jq(render.tostring(value))}</b></span>'


def skills_str(t):
    by_skill = t.get("by_skill") if isinstance(t, dict) else None
    if not isinstance(by_skill, dict):
        return "-"
    return ", ".join(sorted(k for k in by_skill.keys() if k != "-"))


def telemetry_html(phase):
    t = phase.get("telemetry")
    if not isinstance(t, dict):
        return ""
    totals = t.get("totals") if isinstance(t.get("totals"), dict) else {}
    by_model = t.get("by_model") if isinstance(t.get("by_model"), dict) else {}
    by_effort = t.get("by_effort") if isinstance(t.get("by_effort"), dict) else {}
    duration = f"{math.floor(jq_alt(t.get('wallclock_s'), 0))} s"
    pairs = [
        ("Turns", totals.get("turns")),
        ("Out", totals.get("output")),
        ("In", jq_alt(totals.get("billable_in"), 0)),
        ("Cache", jq_alt(totals.get("cache_read"), 0)),
        ("Dauer", duration),
        ("Model", ", ".join(sorted(by_model.keys()))),
        ("Effort", ", ".join(sorted(by_effort.keys()))),
        ("Skills", skills_str(t)),
    ]
    body = " · ".join(kv(label, value) for label, value in pairs)
    return f'<p class="telemetry">{body}</p>'


def phase_html(phase, goals):
    status = phase.get("status") or ""
    phase_id = phase.get("phase")
    goal = goals.get(phase_id or "", "")
    parts = [
        f'<section class="phase {status}">',
        f'<h3><span class="badge {status}">{html_escape_jq(status.upper())}</span> {esc(phase_id)}</h3>',
    ]
    if goal:
        parts.append(f'<p class="goal">{esc(goal)}</p>')
    parts.append(f'<p>{esc(phase.get("summary"))}</p>')
    parts.append(telemetry_html(phase))
    parts.append(section_list(phase.get("deviations"), "Deviations"))
    parts.append(section_list(phase.get("errors"), "Errors"))
    verification = jq_alt(phase.get("verification"), "")
    if verification != "":
        parts.append(f'<p class="verification"><code>{esc(phase.get("verification"))}</code></p>')
    commit = jq_alt(phase.get("commit"), "")
    if commit != "":
        parts.append(f'<p class="commit">Commit: <code>{esc(phase.get("commit"))}</code></p>')
    parts.append("</section>")
    return "".join(parts)


def render_report_html(data, goals):
    batch = data.get("batch")
    phases = data.get("phases", [])
    green_n = sum(1 for p in phases if isinstance(p, dict) and p.get("status") == "green")
    red_n = sum(1 for p in phases if isinstance(p, dict) and p.get("status") == "red")

    planning = data.get("planning")
    planning_block = ""
    if planning is not None:
        totals = planning.get("totals") if isinstance(planning, dict) else None
        by_model = planning.get("by_model") if isinstance(planning, dict) else None
        model_join = ", ".join(sorted(by_model.keys())) if isinstance(by_model, dict) else ""
        impl_outputs = []
        for p in phases:
            tel = p.get("telemetry") if isinstance(p, dict) else None
            impl_outputs.append(_totals_field(tel.get("totals") if isinstance(tel, dict) else None, "output"))
        impl_total = jq_add(impl_outputs)
        planning_block = (
            '<p class="summary">Planning: '
            + render.tostring(totals.get("output") if isinstance(totals, dict) else None)
            + ' out · '
            + render.tostring(totals.get("turns") if isinstance(totals, dict) else None)
            + ' Turns · ' + model_join
            + ' · Implementierung: ' + render.tostring(impl_total) + ' out</p>'
        )

    body = "".join(phase_html(p, goals) for p in phases if isinstance(p, dict))

    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>' + esc(batch) + ' report</title>'
        '<style>' + REPORT_STYLE_CSS + '</style></head><body>'
        '<h1>' + esc(batch) + '</h1>'
        '<p class="meta">Repo: ' + esc(data.get("repo")) + ' · Started: ' + esc(data.get("started")) + '</p>'
        '<p class="summary">Phases: ' + str(len(phases))
        + ' · Green: ' + str(green_n)
        + ' · Red: ' + str(red_n) + '</p>'
        + planning_block
        + body
        + '</body></html>'
    )


def cmd_html(args):
    dir_ = args.dir.rstrip("/")
    f = os.path.join(dir_, "report.json")
    if not os.path.isfile(f):
        errors.die(f"{PROG}: no report.json in {dir_}")
    data = json.loads(pathlib.Path(f).read_text())

    repo_root = repo_root_of(dir_)
    report_dir = settings_get(repo_root, "reportDir")
    out = resolve_html_path(dir_)
    if report_dir not in ("", "null"):
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
        except OSError:
            errors.die(f"{PROG}: cannot create {os.path.dirname(out)}")

    goals = extract_goals(dir_, data)
    html_doc = render_report_html(data, goals)
    tmp = f"{out}.tmp"
    pathlib.Path(tmp).write_text(html_doc + "\n")
    os.replace(tmp, out)
    print(out)

    # Collected-tree mode also regenerates the directory-of-everything index.
    if report_dir not in ("", "null"):
        regenerate_index(report_dir)


# ---- verbs: index / detail -------------------------------------------------------------------

def run_scan():
    # Direct sibling call, not the dispatcher: cfq_report.py resolves cfq-scan.sh relative to
    # its own real location, which would bypass a test double that shadows cfq-scan.sh in a copy
    # of this script's directory (see tests/test_report.py's index/scan-count test).
    out = subprocess.run([str(SCRIPT_DIR / "cfq-scan.sh")], capture_output=True, text=True)
    return json.loads(out.stdout)


def build_index_rows(repo_filter="", batch_filter="", any_filter=""):
    scan = run_scan()
    meta = []
    rf, bf, af = repo_filter.lower(), batch_filter.lower(), any_filter.lower()
    for r in scan.get("repos", []):
        rpath = r.get("path", "")
        for b in r.get("batches", []):
            if not b.get("report"):
                continue
            name = b.get("name", "")
            if rf and rf not in rpath.lower():
                continue
            if bf and bf not in name.lower():
                continue
            if af and af not in rpath.lower() and af not in name.lower():
                continue
            sub = "impl/done" if b.get("archived") else "impl"
            meta.append({
                "repo": rpath, "name": name,
                "path": f"{rpath}/.claude/cfq/{sub}/{name}/report.json",
            })

    rows = []
    for m in meta:
        try:
            data = json.loads(pathlib.Path(m["path"]).read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        phases = data.get("phases", []) if isinstance(data, dict) else []
        deviations = 0
        for p in phases:
            d = jq_alt(p.get("deviations") if isinstance(p, dict) else None, [])
            if isinstance(d, list):
                deviations += len(d)
        last_finished = phases[-1].get("finished") if phases and isinstance(phases[-1], dict) else None
        date = jq_alt(jq_alt(last_finished, data.get("started") if isinstance(data, dict) else None), "")

        planning = data.get("planning") if isinstance(data, dict) else None
        planning_totals = planning.get("totals") if isinstance(planning, dict) else None
        planning_output = _totals_field(planning_totals, "output")
        planning_turns = _totals_field(planning_totals, "turns")
        phase_outputs, phase_turns = [], []
        for p in phases:
            tel = p.get("telemetry") if isinstance(p, dict) else None
            totals = tel.get("totals") if isinstance(tel, dict) else None
            phase_outputs.append(_totals_field(totals, "output"))
            phase_turns.append(_totals_field(totals, "turns"))

        rows.append({
            "batch": m["name"],
            "repo": m["repo"],
            "date": date,
            "status": outcome(phases),
            "deviations": deviations,
            "cost": {
                "outputTokens": planning_output + sum(phase_outputs),
                "turns": planning_turns + sum(phase_turns),
            },
        })

    rows.sort(key=lambda r: r["date"])
    rows.reverse()
    return rows, meta


def cmd_index(args):
    rows, meta = build_index_rows(args.repo, args.batch, args.any_filter)
    if not args.text:
        print(render.dump_json(rows))
        return
    if not rows:
        print("No batch has a report yet — reports have existed only since v0.2, so older batches never got one.")
        return
    lines = ["| " + " | ".join(["Repo", "Batch", "Status", "Dev.", "Date", "Cost"]) + " |", "|---|---|---|---|---|---|"]
    for r in rows:
        repo_short = r["repo"].split("/")[-1]
        status_disp = f'**{r["status"]}**' if r["status"] in ("RED", "MIXED") else r["status"]
        cost = r["cost"]["outputTokens"]
        cost_disp = "–" if cost == 0 else f"{jq_round(cost / 1000)}k"
        lines.append("| " + " | ".join([repo_short, r["batch"], status_disp, str(r["deviations"]), r["date"], cost_disp]) + " |")
    print("\n".join(lines))
    for r in rows:
        m = next((mm for mm in meta if mm["repo"] == r["repo"] and mm["name"] == r["batch"]), None)
        if m is not None:
            print(f"file://{resolve_html_path(os.path.dirname(m['path']))}")


def cmd_detail(args):
    dir_ = args.dir.rstrip("/")
    f = os.path.join(dir_, "report.json")
    if not os.path.isfile(f):
        print(render.dump_json({"found": False}))
        return
    data = json.loads(pathlib.Path(f).read_text())

    repo_root = repo_root_of(dir_)
    todos = []
    if repo_root:
        todo_dir = pathlib.Path(repo_root) / ".claude" / "cfq" / "todo"
        if todo_dir.is_dir():
            for t in sorted(todo_dir.glob("*.md")):
                lines = t.read_text().splitlines()
                title = re.sub(r"^#+\s*", "", lines[0]) if lines else ""
                todos.append({"file": t.name, "title": title})

    phases = data.get("phases", [])
    deviations_total = 0
    for p in phases:
        d = jq_alt(p.get("deviations") if isinstance(p, dict) else None, [])
        if isinstance(d, list):
            deviations_total += len(d)

    planning = data.get("planning") if isinstance(data, dict) else None
    planning_totals = planning.get("totals") if isinstance(planning, dict) else None
    planning_output = _totals_field(planning_totals, "output")
    planning_turns = _totals_field(planning_totals, "turns")
    phase_outputs, phase_turns, out_phases = [], [], []
    for p in phases:
        tel = p.get("telemetry") if isinstance(p, dict) else None
        totals = tel.get("totals") if isinstance(tel, dict) else None
        phase_outputs.append(_totals_field(totals, "output"))
        phase_turns.append(_totals_field(totals, "turns"))
        out_phases.append({
            "phase": p.get("phase"),
            "status": p.get("status"),
            "summary": jq_alt(p.get("summary"), ""),
            "deviations": jq_alt(p.get("deviations"), []),
            "errors": jq_alt(p.get("errors"), []),
            "verification": bound_lines(p.get("verification"), 5),
            "commit": jq_alt(p.get("commit"), ""),
            "telemetry": jq_alt(p.get("telemetry"), None),
        })

    print(render.dump_json({
        "found": True,
        "batch": data.get("batch"),
        "repo": data.get("repo"),
        "started": data.get("started"),
        "status": outcome(phases),
        "deviationsTotal": deviations_total,
        "cost": {
            "outputTokens": planning_output + sum(phase_outputs),
            "turns": planning_turns + sum(phase_turns),
        },
        "phases": out_phases,
        "todos": todos,
    }))


# ---- collected index.html (reportDir mode) ---------------------------------------------------

def row_html(row):
    status = row.get("status") or ""
    if row.get("rendered"):
        batch_html = f'<a href="{esc(row["repoBase"])}/{esc(row["batch"])}.html">{esc(row["batch"])}</a>'
    else:
        batch_html = esc(row["batch"])
    out_tokens = jq_alt(row.get("cost", {}).get("outputTokens"), 0)
    turns = jq_alt(row.get("cost", {}).get("turns"), 0)
    deviations = row.get("deviations")
    dev_part = f' · {render.tostring(deviations)} Deviations' if isinstance(deviations, (int, float)) and deviations > 0 else ""
    return (
        f'<li><span class="badge {status.lower()}">{esc(status)}</span> '
        f'{batch_html} · {esc(row.get("date"))} · {render.tostring(out_tokens)} out, '
        f'{render.tostring(turns)} Turns{dev_part}</li>'
    )


def repo_section_html(items):
    lis = "".join(row_html(it) for it in items)
    return f'<section class="repo"><h2>{esc(items[0]["repoBase"])}</h2><ul>{lis}</ul></section>'


def regenerate_index(report_dir):
    rows, _ = build_index_rows()
    groups = {}
    for row in rows:
        repo_base = os.path.basename(row["repo"])
        rendered = os.path.isfile(os.path.join(report_dir, repo_base, f'{row["batch"]}.html'))
        enriched = {**row, "repoBase": repo_base, "rendered": rendered}
        groups.setdefault(repo_base, []).append(enriched)

    sections = [repo_section_html(groups[key]) for key in sorted(groups.keys())]
    body = "".join(sections) if sections else '<p class="meta">No reports yet.</p>'
    doc = (
        '<!doctype html><html><head><meta charset="utf-8"><title>cfq reports</title>'
        '<style>' + REPORT_STYLE_CSS + '</style></head><body>'
        '<h1>cfq reports</h1>' + body + '</body></html>'
    )
    idx_out = os.path.join(report_dir, "index.html")
    tmp = f"{idx_out}.tmp"
    pathlib.Path(tmp).write_text(doc + "\n")
    os.replace(tmp, idx_out)


# ---- argument parsing ------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog=PROG, add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("append")
    p.add_argument("dir")
    p.add_argument("phase")
    p.set_defaults(func=cmd_append)

    p = sub.add_parser("security")
    p.add_argument("dir")
    p.add_argument("snap")
    p.set_defaults(func=cmd_security)

    p = sub.add_parser("set-commit")
    p.add_argument("dir")
    p.add_argument("phase_slug")
    p.add_argument("sha")
    p.set_defaults(func=cmd_set_commit)

    p = sub.add_parser("last-failure")
    p.add_argument("dir")
    p.add_argument("phase_slug")
    p.set_defaults(func=cmd_last_failure)

    p = sub.add_parser("summary")
    p.add_argument("dir")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("html")
    p.add_argument("dir")
    p.set_defaults(func=cmd_html)

    p = sub.add_parser("index")
    p.add_argument("--repo", default="")
    p.add_argument("--batch", default="")
    p.add_argument("--any", dest="any_filter", default="")
    p.add_argument("--text", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("detail")
    p.add_argument("dir")
    p.set_defaults(func=cmd_detail)

    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        errors.die(
            f"usage: {PROG} append <batch-dir> <phase-json> | "
            f"security <batch-dir> <security-json> | "
            f"set-commit <batch-dir> <phase-slug> <sha> | "
            f"last-failure <batch-dir> <phase-slug> | "
            f"summary <batch-dir> | html <batch-dir> | "
            f"index [--repo <substr>] [--batch <substr>] [--any <substr>] [--text] | "
            f"detail <batch-dir>"
        )
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
