"""Canonical repo-local CFQ path helpers.

Pure string work only, no mutation, no I/O.
"""


def cfq_repo_dir(repo):
    return f"{repo}/.claude/cfq"


def plan_dir(repo):
    return f"{repo}/.claude/cfq/plan"


def impl_dir(repo):
    return f"{repo}/.claude/cfq/impl"


def impl_done_dir(repo):
    return f"{repo}/.claude/cfq/impl/done"


def todo_dir(repo):
    return f"{repo}/.claude/cfq/todo"


def repo_settings_file(repo):
    return f"{repo}/.claude/cfq/settings.json"


def lockfile(repo):
    return f"{repo}/.claude/cfq/.lock"


def maintenance_marker(repo):
    return f"{repo}/.claude/cfq/.maintenance"


def telemetry_log(repo):
    return f"{repo}/.claude/cfq/telemetry.jsonl"
