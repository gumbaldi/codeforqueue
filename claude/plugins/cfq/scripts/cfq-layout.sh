#!/usr/bin/env bash
# Owns the canonical `<repo>/.claude/cfq/` layout and its local Git-state policy. Knows nothing
# about the old `.claude/code-for-queue` layout — that is the isolated migration utility's job
# (scripts/migrations/cfq-layout-v1.sh). No `migrate` subcommand here on purpose.
# Usage: cfq-layout.sh ensure <repo-root>
#        cfq-layout.sh status <repo-root>
#        cfq-layout.sh sync-git-policy <repo-root>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-layout.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"

BLOCK_BEGIN="# BEGIN cfq-managed (do not edit this block by hand)"
BLOCK_END="# END cfq-managed"
# Paths excluded when gitStatePolicy=local. changelog.yml is CFQ's numbered-batch allocation
# ledger as well as workflow history, so it follows the same local/trackable policy as the rest
# of the queue -- no second Git-state mechanism.
BLOCK_ENTRIES=(
  ".claude/cfq/plan/"
  ".claude/cfq/impl/"
  ".claude/cfq/todo/"
  ".claude/cfq/changelog.yml"
  ".claude/cfq/.lock"
  ".claude/cfq/.maintenance"
  ".claude/cfq/telemetry.jsonl"
)

exclude_file() {
  local gd
  gd=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 0
  printf '%s/info/exclude' "$gd"
}

# Strips any existing cfq-managed block from stdin, prints the rest.
strip_block() {
  awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip { print }
  '
}

do_ensure_dirs() {
  local repo="$1"
  mkdir -p "$(plan_dir "$repo")" "$(impl_done_dir "$repo")" "$(todo_dir "$repo")"
}

do_sync_git_policy() {
  local repo="$1" policy ef stripped
  policy=$("$cfq" settings get --repo "$repo" gitStatePolicy)
  ef=$(exclude_file "$repo")
  [ -n "$ef" ] || return 0

  stripped=""
  [ -f "$ef" ] && stripped=$(strip_block <"$ef")

  mkdir -p "$(dirname "$ef")"
  case "$policy" in
    local)
      { [ -n "$stripped" ] && printf '%s\n' "$stripped"
        printf '%s\n' "$BLOCK_BEGIN"
        printf '%s\n' "${BLOCK_ENTRIES[@]}"
        printf '%s\n' "$BLOCK_END"
      } >"$ef.tmp"
      mv "$ef.tmp" "$ef"
      ;;
    trackable)
      if [ -f "$ef" ]; then
        printf '%s\n' "$stripped" >"$ef.tmp"
        mv "$ef.tmp" "$ef"
      fi
      ;;
  esac
}

cmd="${1:-}"
repo="${2:?usage: cfq-layout.sh ensure|status|sync-git-policy <repo-root>}"

case "$cmd" in
  ensure)
    do_ensure_dirs "$repo"
    do_sync_git_policy "$repo"
    echo "OK"
    ;;
  status)
    ef=$(exclude_file "$repo")
    block="absent"
    if [ -n "$ef" ] && [ -f "$ef" ] && grep -qxF "$BLOCK_BEGIN" "$ef" 2>/dev/null; then
      block="present"
    fi
    policy=$("$cfq" settings get --repo "$repo" gitStatePolicy)
    canonical="false"; [ -d "$(cfq_repo_dir "$repo")" ] && canonical="true"
    jq -n --arg p "$policy" --arg b "$block" --argjson c "$canonical" \
      '{gitStatePolicy: $p, excludeBlock: $b, canonical: $c}'
    ;;
  sync-git-policy)
    do_sync_git_policy "$repo"
    echo "OK"
    ;;
  *)
    echo "usage: cfq-layout.sh ensure|status|sync-git-policy <repo-root>" >&2
    exit 1
    ;;
esac
