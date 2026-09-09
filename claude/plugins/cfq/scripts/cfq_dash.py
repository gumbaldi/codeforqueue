#!/usr/bin/env python3
# Usage: cfq_dash.py [render] [--all] [<cwd>]
"""Single read-only aggregator for the /cfq dashboard: bundles plugin status (installed + the two
switches), the cross-repo scan rolled up per repo, and this repo's (or, outside a repo, the
global) settings-with-sources -- the four separate calls the skill used to make -- into one JSON
object. Default mode emits the JSON object, unaffected by `--all`. `render` formats the identical
aggregation as the terminal text the /cfq dashboard prints -- same data, no second aggregator.
`--all` (render only) lists every batch this repo ever had instead of only its open ones.

Ported from cfq-dash.sh -- a port, not a redesign: the CLI contract (argument order, JSON shape,
rendered text) is the invariant this file preserves. The render half in particular is carried
(mostly) verbatim -- whitespace differences in that layout do not raise, they just silently drift.
"""

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cfq_lib import paths, render  # noqa: E402

PROG = "cfq_dash.py"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"

REASON_TEXT = {"inProgress": "in progress", "priority": "priority high", "order": "next in order"}

ACTION_ROWS_TEMPLATE = [
    ("flag / unflag priority", "mark a batch high priority"),
    ("delete a batch", "removes the queue directory"),
    ("archive a batch", "moves it to impl/done/"),
    ("clean the registry", "drop repos that no longer exist"),
    ("set / remove a dependency", ".dependsOn between batches"),
    ("work off todo/ entries", "runs their check: commands"),
    ("change a setting", "just say it in plain language"),
    ("full batch list", "bin/cfq dash render --all"),
    ("settings, this repo", "bin/cfq settings list --repo {path} --sources"),
    ("settings, global", "bin/cfq settings list --sources"),
]


def cfq_run(*args, env=None):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True, env=env)


def jq_alt(*values):
    """Mirrors jq's `//`: the first value that is neither null nor false."""
    for v in values:
        if v is not None and v is not False:
            return v
    return None


def parse_args(argv):
    argv = list(argv)
    mode = "json"
    if argv and argv[0] == "render":
        mode = "render"
        argv = argv[1:]
    all_flag = False
    rest = []
    for a in argv:
        if a == "--all":
            all_flag = True
        else:
            rest.append(a)
    cwd = rest[0] if rest else os.getcwd()
    return mode, all_flag, cwd


def marker_for(source):
    if source == "default":
        return "D"
    if source == "global":
        return "G"
    if source == "repo":
        return "R"
    return "E"


def build_settings_json(settings_src, masked_src):
    rows = []
    for key, entry in settings_src.items():
        row = {
            "key": key, "value": entry["value"], "source": entry["source"],
            "marker": marker_for(entry["source"]),
        }
        if entry["source"].startswith("env"):
            masked = masked_src.get(key, {})
            row["maskedValue"] = masked.get("value")
            row["maskedSource"] = masked.get("source")
        rows.append(row)
    return rows


def batch_status(b):
    if b["blocked"]:
        return "BLOCKED"
    if b["planning"]:
        return "PLANNING"
    if b["inProgress"]:
        return "IN_PROGRESS"
    return "OK"


def repo_rollup(r):
    batches = r["batches"]
    return {
        "path": r["path"], "name": r["path"].split("/")[-1], "plan": r["plan"], "todo": r["todo"],
        "open": sum(1 for b in batches if not b["archived"]),
        "done": sum(1 for b in batches if b["archived"]),
        "status": batch_status({
            "blocked": any(b["blocked"] for b in batches),
            "planning": any(b["planning"] for b in batches),
            "inProgress": any(b["inProgress"] for b in batches),
        }),
    }


def this_repo_rollup(scan_repos, repo):
    r = next((x for x in scan_repos if x["path"] == repo), None)
    if r is None:
        return None
    return {
        "path": r["path"], "name": r["path"].split("/")[-1],
        "batches": [
            {"name": b["name"], "priority": b["priority"], "open": b["open"], "done": b["done"],
             "status": batch_status(b)}
            for b in r["batches"]
        ],
    }


def dash_line(status, repos, this_repo):
    if status == "MULTIPLE_IN_PROGRESS":
        names = ", ".join(b["name"] for b in this_repo["batches"] if b["status"] == "IN_PROGRESS")
        text = f"MULTIPLE_IN_PROGRESS in {this_repo['name']}: {names} — invariant violation, resolve manually"
        return "⚠️", text
    with_open = sum(1 for r in repos if r["open"] > 0)
    return "✅", f"{len(repos)} repos · {with_open} with open work"


def plugins_line(p):
    mode_clause = None
    if p["ponytail"] and p["ponytailMode"] != "off":
        mode_clause = f"ponytail default mode: {p['ponytailMode']} · cfq expects off"

    if p["mattpocock"] is False and p["ponytail"] is False:
        base = {"icon": "➖", "text": "mattpocock-skills/ponytail not installed"}
    elif p["mattpocock"] is True and p["ponytail"] is True:
        off = []
        if not p["useMattpocockGrilling"]:
            off.append("grill: classic off")
        if not p["usePonytailAudit"]:
            off.append("maintenance audit: off")
        if not off:
            base = {"icon": "✅", "text": "mattpocock-skills and ponytail installed · classic grill on · maintenance audit: on"}
        else:
            base = {"icon": "➖", "text": "installed · " + ", ".join(off)}
    else:
        if p["mattpocock"]:
            missing = "ponytail"
            state = "classic grill on" if p["useMattpocockGrilling"] else "classic grill off"
        else:
            missing = "mattpocock-skills"
            state = "maintenance audit: on" if p["usePonytailAudit"] else "maintenance audit: off"
        base = {"icon": "➖", "text": f"{missing} not installed · {state}"}

    text = base["text"] if mode_clause is None else f"{base['text']} · {mode_clause}"
    icon = "⚠️" if mode_clause is not None else base["icon"]
    return icon, text


def impl_model(settings_json):
    for s in settings_json:
        if s["key"] == "implModels" and s["value"]:
            return s["value"][0]
    return "sonnet"


def build_next(repo, scan_repos):
    """Next-batch expansion: the batch `/ifq` would pick next (`bin/cfq scan --format=next`)
    rendered to its individual phases (`bin/cfq brief --with-done`) -- same lines as /ifq's own
    Step 4 briefing. Render mode only."""
    expanded, header, note = "", "", ""
    if not repo:
        return expanded, header, note

    next_json = json.loads(cfq_run("scan", "--format=next").stdout)
    next_row = next(
        (x for x in next_json["repos"] if x["path"] == repo),
        {"next": None, "reason": None, "blocked": [], "planning": []},
    )
    next_name = next_row.get("next")
    if next_name:
        next_reason = next_row.get("reason")
        reason_text = REASON_TEXT.get(next_reason, next_reason)
        batch_path = pathlib.Path(paths.impl_dir(repo)) / next_name
        expanded = cfq_run("brief", str(batch_path), "--with-done").stdout.rstrip("\n")
        header = f"{next_name} ({reason_text})"
    else:
        blocked = next_row.get("blocked") or []
        blocked_name = blocked[0] if blocked else None
        if blocked_name:
            target = next((x for x in scan_repos if x["path"] == repo), None)
            deps = []
            if target is not None:
                dep_batch = next((b for b in target["batches"] if b["name"] == blocked_name), None)
                if dep_batch is not None:
                    deps = dep_batch.get("dependsOn", [])
            note = f"Next batch blocked: {blocked_name} waiting on {', '.join(deps)}"
    return expanded, header, note


def render_body(repos, this_repo, settings_json, all_flag, next_expanded, next_header, next_note):
    lines = []

    if not repos:
        lines += ["", "No repos with a queue yet."]
    else:
        lines += ["", "QUEUES", "| Repo | Plan | Todo | Batches | Status |", "|---|---|---|---|---|"]
        lines += [
            f"| {r['name']} | {r['plan']} | {r['todo']} | {r['open']}/{r['done']} | {r['status']} |"
            for r in repos
        ]

    if this_repo is not None:
        rows = [b for b in this_repo["batches"] if all_flag or b["open"] > 0]
        if rows:
            table = ["", f"THIS REPO · {this_repo['name']}", "| Batch | Priority | Open/Done | Status |", "|---|---|---|---|"]
            for b in rows:
                prio = b["priority"] if b["priority"] != "" else "-"
                table.append(f"| {b['name']} | {prio} | {b['open']}/{b['done']} | {b['status']} |")
        else:
            table = ["", f"THIS REPO · {this_repo['name']}"]

        if all_flag:
            summary = []
        else:
            done_n = sum(1 for b in this_repo["batches"] if b["open"] == 0)
            summary = [f"{done_n} batches done · full list: bin/cfq dash render --all"]

        if next_expanded != "":
            expansion = ["", next_header, next_expanded]
        elif next_note != "":
            expansion = ["", next_note]
        else:
            expansion = []

        lines += table + summary + expansion

    if this_repo is not None:
        rows = [s for s in settings_json if s["marker"] != "D"]
        lines += ["", f"CONFIG · {this_repo['name']}", f"{len(rows)} of {len(settings_json)} keys differ from default"]
        for s in rows:
            line = f"[{s['marker']}] {s['key']}  {render.tostring(s['value'])}"
            if "maskedValue" in s:
                line += f"\n   └ ⚠ masks {s['maskedSource']} value `{render.tostring(s['maskedValue'])}`"
            lines.append(line)

    if this_repo is not None:
        action_rows = [(a, b.format(path=this_repo["path"])) for a, b in ACTION_ROWS_TEMPLATE]
        lines += ["", "ACTIONS"] + [f"{a.ljust(27)}{b}" for a, b in action_rows]

    eligible = [r for r in repos if r["status"] != "BLOCKED" and r["open"] > 0]
    if eligible:
        if this_repo is not None:
            ordered = (
                [r for r in eligible if r["path"] == this_repo["path"]]
                + [r for r in eligible if r["path"] != this_repo["path"]]
            )
        else:
            ordered = eligible
        model = impl_model(settings_json)
        lines += ["", "NEXT"] + [f"\ncd {r['path']}\n/model {model}\n/ifq" for r in ordered]

    return "\n".join(lines)


def main(argv):
    mode, all_flag, cwd = parse_args(argv)

    runtime_json = json.loads(cfq_run("runtime", "plugins").stdout)
    if runtime_json.get("status") != "OK":
        if mode == "render":
            code = jq_alt(runtime_json.get("code"), runtime_json.get("cap"), "see detail")
            print("PRECHECKS")
            print(f"⚠️ {'Dash':<16}runtime degraded · {code}")
            print(f"➖ {'Plugins':<16}unknown · runtime degraded")
        else:
            print(render.dump_json_pretty({
                "status": "RUNTIME_DEGRADED", "runtimeDiagnostic": runtime_json,
                "plugins": None, "repos": [], "thisRepo": None, "settings": [],
            }))
        return

    plugins_list = runtime_json["plugins"]
    pony_mode = runtime_json["ponytailMode"]

    git_proc = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    )
    repo = git_proc.stdout.strip() if git_proc.returncode == 0 else ""
    repo_args = ["--repo", repo] if repo else []

    settings_src = json.loads(cfq_run("settings", "list", *repo_args, "--sources").stdout)

    masked_src = {}
    if any(entry["source"].startswith("env") for entry in settings_src.values()):
        masked_env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "")}
        masked_src = json.loads(
            cfq_run("settings", "list", *repo_args, "--sources", env=masked_env).stdout
        )

    settings_json = build_settings_json(settings_src, masked_src)

    use_grill = settings_src["useMattpocockGrilling"]["value"]
    use_pony = settings_src["usePonytailAudit"]["value"]
    has_mp = "mattpocock-skills" in plugins_list
    has_pt = "ponytail" in plugins_list
    plugins_obj = {
        "mattpocock": has_mp, "ponytail": has_pt, "useMattpocockGrilling": use_grill,
        "usePonytailAudit": use_pony, "ponytailMode": pony_mode,
    }

    scan_json = json.loads(cfq_run("scan").stdout)
    repos_json = [repo_rollup(r) for r in scan_json["repos"]]
    this_repo_json = this_repo_rollup(scan_json["repos"], repo) if repo else None

    status = "OK"
    if this_repo_json is not None and sum(
        1 for b in this_repo_json["batches"] if b["status"] == "IN_PROGRESS"
    ) > 1:
        status = "MULTIPLE_IN_PROGRESS"
    elif len(repos_json) == 0:
        status = "NO_REPO"

    if mode != "render":
        print(render.dump_json_pretty({
            "status": status, "plugins": plugins_obj, "repos": repos_json,
            "thisRepo": this_repo_json, "settings": settings_json,
        }))
        return

    next_expanded, next_header, next_note = build_next(repo, scan_json["repos"])

    d_icon, d_text = dash_line(status, repos_json, this_repo_json)
    p_icon, p_text = plugins_line(plugins_obj)

    print("PRECHECKS")
    print(f"{d_icon} {'Dash':<16}{d_text}")
    print(f"{p_icon} {'Plugins':<16}{p_text}")
    print(render_body(repos_json, this_repo_json, settings_json, all_flag, next_expanded, next_header, next_note))


if __name__ == "__main__":
    main(sys.argv[1:])
