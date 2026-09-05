# Canonical repo-local CFQ path helpers. Meant to be sourced, not run standalone. Pure string
# work only — no mutation, no I/O. The old `.claude/code-for-queue` root is known nowhere in this
# file; that's the isolated migration utility's job (scripts/migrations/cfq-layout-v1.sh).

cfq_repo_dir() { printf '%s/.claude/cfq' "$1"; }
plan_dir() { printf '%s/.claude/cfq/plan' "$1"; }
impl_dir() { printf '%s/.claude/cfq/impl' "$1"; }
impl_done_dir() { printf '%s/.claude/cfq/impl/done' "$1"; }
todo_dir() { printf '%s/.claude/cfq/todo' "$1"; }
repo_settings_file() { printf '%s/.claude/cfq/settings.json' "$1"; }
lockfile() { printf '%s/.claude/cfq/.lock' "$1"; }
maintenance_marker() { printf '%s/.claude/cfq/.maintenance' "$1"; }
telemetry_log() { printf '%s/.claude/cfq/telemetry.jsonl' "$1"; }
