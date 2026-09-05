#!/usr/bin/env bash
# One-time, isolated upgrade utility from the old repo-local layout (<repo>/.claude/code-for-queue/)
# to the canonical one (<repo>/.claude/cfq/). This is the ONLY production file allowed to know the
# old path. Never sourced or called by normal skills/preflights/runtime helpers after a fleet is
# fully migrated — it is upgrade/recovery tooling, not a second runtime compatibility path.
#
# Usage: cfq-layout-v1.sh discover [<current-repo>]
#        cfq-layout-v1.sh plan --all-known [<current-repo>]
#        cfq-layout-v1.sh apply --all-known [<current-repo>]
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-layout-v1.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(dirname "$script_dir")"

old_root() { printf '%s/.claude/code-for-queue' "$1"; }
new_root() { printf '%s/.claude/cfq' "$1"; }

# Every repo the fleet could possibly need migrating: the global registry, the current repo (if
# given), and configured scanRoots — searching only for the exact old marker directory, not a
# broad `.claude/` parse.
discover() {
  local current="${1:-}" repos=""
  repos=$("$scripts_dir/cfq-registry.sh" list 2>/dev/null || true)
  [ -n "$current" ] && repos="$repos
$current"

  local roots root
  roots=$(python3 "$scripts_dir/cfq_settings.py" get scanRoots 2>/dev/null || true)
  local old_ifs=$IFS; IFS=','
  for root in $roots; do
    IFS=$old_ifs
    root="${root/#\~/$HOME}"
    [ -d "$root" ] || continue
    while IFS= read -r d; do
      repos="$repos
$(dirname "$(dirname "$d")")"
    done < <(find "$root" -mindepth 2 -maxdepth 4 -type d -path '*/.claude/code-for-queue' 2>/dev/null)
    IFS=','
  done
  IFS=$old_ifs

  printf '%s\n' "$repos" | sed '/^$/d' | sort -u
}

# Merges one file: moves if absent at target, drops the old copy if byte-identical, otherwise
# records a conflict. In dry-run mode nothing is written; conflicts/moves are only recorded.
merge_file() {
  local src="$1" dst="$2" dry_run="$3" rel="$4"
  if [ ! -e "$dst" ]; then
    [ "$dry_run" = "1" ] || { mkdir -p "$(dirname "$dst")"; mv "$src" "$dst"; }
    return 0
  fi
  if cmp -s "$src" "$dst"; then
    [ "$dry_run" = "1" ] || rm -f "$src"
    return 0
  fi
  conflicts+=("$rel")
}

# telemetry.jsonl is an additive log — concatenation is always safe, never a conflict.
merge_telemetry() {
  local src="$1" dst="$2" dry_run="$3"
  [ -f "$src" ] || return 0
  [ "$dry_run" = "1" ] && return 0
  if [ ! -e "$dst" ]; then
    mv "$src" "$dst"
  else
    cat "$src" >>"$dst"
    rm -f "$src"
  fi
}

# Classifies/migrates one repo. $2: 1 = dry run (plan), 0 = apply for real.
# Sets globals: result, conflicts (array).
migrate_repo() {
  local repo="$1" dry_run="$2" old new
  conflicts=()
  result=""

  if [ ! -d "$repo" ]; then
    result="stale"; return 0
  fi
  old="$(old_root "$repo")"
  new="$(new_root "$repo")"

  if [ ! -d "$old" ]; then
    if [ -d "$new" ]; then result="already-canonical"; else result="stale"; fi
    return 0
  fi

  [ "$dry_run" = "1" ] || mkdir -p "$new/plan" "$new/impl/done" "$new/todo"

  # Every top-level entry under the old root is merged generically, not just the documented
  # plan/impl/todo trio — a repo may carry pre-v0.2 debris (e.g. a stray top-level `done/`) that
  # predates the current three-queue model. Nothing under the old root is ever silently discarded:
  # unknown content still moves across, it just isn't special-cased.
  local entry name f d rel
  while IFS= read -r -d '' entry; do
    name="$(basename "$entry")"
    if [ "$name" = "telemetry.jsonl" ]; then
      merge_telemetry "$entry" "$new/$name" "$dry_run"
      continue
    fi
    if [ -f "$entry" ]; then
      merge_file "$entry" "$new/$name" "$dry_run" "$name"
      continue
    fi
    if [ "$dry_run" != "1" ]; then
      while IFS= read -r -d '' d; do
        mkdir -p "$new/${d#"$old"/}"
      done < <(find "$entry" -type d -print0)
    fi
    while IFS= read -r -d '' f; do
      rel="${f#"$old"/}"
      merge_file "$f" "$new/$rel" "$dry_run" "$rel"
    done < <(find "$entry" -type f -print0)
  done < <(find "$old" -mindepth 1 -maxdepth 1 -print0)

  if [ "${#conflicts[@]}" -gt 0 ]; then
    result="conflict"
    return 0
  fi

  if [ "$dry_run" = "1" ]; then
    result="migrated"
    return 0
  fi

  # Drained means no files left at all. Anything unrecognized left behind blocks removal instead
  # of silently discarding it.
  local leftover
  leftover=$(find "$old" -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$leftover" = "0" ]; then
    rm -rf "$old"
    result="migrated"
  else
    result="conflict"
    conflicts=("unrecognized files remain under $old")
  fi
}

emit_repo_json() {
  local repo="$1" code="null"
  [ "$result" = "conflict" ] && code='"CFQ_LAYOUT_MIGRATION_CONFLICT"'
  jq -n --arg path "$repo" --arg result "$result" --argjson code "$code" \
    --argjson conflicts "$(printf '%s\n' "${conflicts[@]:-}" | sed '/^$/d' | jq -R . | jq -s .)" \
    '{path: $path, result: $result, code: $code, conflicts: $conflicts}'
}

cmd="${1:-}"
case "$cmd" in
  discover)
    discover "${2:-}"
    ;;
  plan|apply)
    [ "${2:-}" = "--all-known" ] || { echo "usage: cfq-layout-v1.sh $cmd --all-known [<current-repo>]" >&2; exit 1; }
    current="${3:-}"
    dry_run=1; [ "$cmd" = "apply" ] && dry_run=0
    results="[]"
    while IFS= read -r repo; do
      [ -n "$repo" ] || continue
      migrate_repo "$repo" "$dry_run"
      results=$(jq --argjson r "$(emit_repo_json "$repo")" '. + [$r]' <<<"$results")
    done < <(discover "$current")
    if [ "$cmd" = "apply" ]; then
      "$scripts_dir/cfq-registry.sh" prune >/dev/null 2>&1 || true
    fi
    jq -n --argjson repos "$results" '{repos: $repos}'
    ;;
  *)
    echo "usage: cfq-layout-v1.sh discover [<current-repo>] | plan --all-known [<current-repo>] | apply --all-known [<current-repo>]" >&2
    exit 1
    ;;
esac
