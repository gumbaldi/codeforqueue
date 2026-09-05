"""Canonical repo-local CFQ path helpers — Python mirror of scripts/cfq-paths.sh.

Pure string work only, no mutation, no I/O. This is a deliberate, tested duplication of the
shell file (tests/test_layout.py's consistency test runs both against the same cases and asserts
byte-identical output): the shell file stays in place for the 22 scripts that still source it,
and the duplication disappears when the last of them is ported.
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
