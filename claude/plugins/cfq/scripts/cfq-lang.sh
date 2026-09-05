#!/usr/bin/env bash
# Language/doc-tree drift reporter for a target repo. Never writes, never fails: always prints one
# JSON object on stdout and exits 0 — the caller (a skill) decides what the numbers mean.
# Usage: cfq-lang.sh <repo-root> [--changed <ref>]      (docs-tree structure, unchanged)
#        cfq-lang.sh prose <repo-root> <ref>            (bounded prose sample for the drift guard)
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-lang.sh: jq is required" >&2; exit 1; }

mode="tree"
if [ "${1:-}" = "prose" ]; then
  mode="prose"
  shift
  repo="${1:?usage: cfq-lang.sh prose <repo-root> <ref>}"
  prose_ref="${2:?usage: cfq-lang.sh prose <repo-root> <ref>}"
else
  repo="${1:?usage: cfq-lang.sh <repo-root> [--changed <ref>]}"
  shift || true
  changed_ref=""
  if [ "${1:-}" = "--changed" ]; then
    changed_ref="${2:?usage: cfq-lang.sh <repo-root> [--changed <ref>]}"
  fi
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

# Settings always via cfq-settings.sh, never from the environment directly — that's the only
# place the env > file > default precedence chain is implemented.
code_language=$("$cfq" settings get codeLanguage)
doc_languages_csv=$("$cfq" settings get docLanguages 2>/dev/null || true)
doc_level=$("$cfq" settings get docLevel)
i18n_exclude_csv=$("$cfq" settings get i18nExcludePatterns 2>/dev/null || true)

if [ -z "$doc_languages_csv" ]; then
  doc_languages_json='[]'
else
  doc_languages_json=$(jq -Rc 'split(",")' <<<"$doc_languages_csv")
fi

if [ "$mode" = "prose" ]; then
  # Caps kept as named variables so the sample size is tunable in one place.
  prose_max_lines=200
  prose_max_bytes=8192

  sample=""
  truncated=false
  note=""

  # i18n/locale resource directories intentionally hold multiple languages by design — excluded
  # from the diff pathspec so their content never lands in the drift sample in the first place.
  # A leading "*/" in a stored pattern only matches nested paths under git's glob pathspec magic
  # (single "*" doesn't cross "/"), so it's normalized to "**/" here to also match a top-level
  # directory of the same name (e.g. "*/locales/*" -> "**/locales/*", matches both locales/de.json
  # and app/locales/de.json).
  i18n_exclude_json=$(jq -Rc 'split(",") | map(select(length > 0))' <<<"$i18n_exclude_csv")
  mapfile -t i18n_pathspecs < <(jq -r '
    .[] | (if startswith("*/") then .[2:] elif startswith("/") then .[1:] else . end) as $p
        | ":(exclude,glob)**/" + $p' <<<"$i18n_exclude_json")

  if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 \
     && git -C "$repo" rev-parse --verify "$prose_ref" >/dev/null 2>&1; then
    commits=$(git -C "$repo" log --format=%B "$prose_ref"..HEAD 2>/dev/null || true)
    added=$(git -C "$repo" diff "$prose_ref"...HEAD -- . "${i18n_pathspecs[@]}" 2>/dev/null \
      | grep '^+' | grep -v '^+++' | sed 's/^+//' || true)
    combined=$(printf '%s\n%s' "$commits" "$added")

    if [ -z "$(printf '%s' "$combined" | tr -d '[:space:]')" ]; then
      note="no changes between $prose_ref and HEAD"
    else
      by_lines=$(printf '%s\n' "$combined" | head -n "$prose_max_lines")
      total_lines=$(printf '%s\n' "$combined" | grep -c '^' || true)
      by_lines_bytes=$(printf '%s' "$by_lines" | wc -c | tr -d ' ')
      if [ "$by_lines_bytes" -gt "$prose_max_bytes" ]; then
        sample=$(printf '%s' "$by_lines" | head -c "$prose_max_bytes")
        truncated=true
      else
        sample="$by_lines"
        [ "$total_lines" -gt "$prose_max_lines" ] && truncated=true
      fi
    fi
  else
    note="no git repo or unknown ref '$prose_ref'"
  fi

  lines_count=$(printf '%s' "$sample" | grep -c '^' || true)

  jq -n \
    --arg codeLanguage "$code_language" \
    --arg ref "$prose_ref" \
    --argjson truncated "$truncated" \
    --argjson lines "$lines_count" \
    --arg sample "$sample" \
    --arg note "$note" \
    '{mode: "prose", codeLanguage: $codeLanguage, ref: $ref, truncated: $truncated, lines: $lines,
      sample: $sample, note: $note}'
  exit 0
fi

scope="repo"
note=""
changed_set=""

if [ -n "$changed_ref" ]; then
  if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 \
     && git -C "$repo" rev-parse --verify "$changed_ref" >/dev/null 2>&1; then
    changed_set=$(git -C "$repo" diff --name-only "$changed_ref"...HEAD -- . 2>/dev/null || true)
    scope="changed"
  else
    note="no git repo or unknown ref '$changed_ref', falling back to repo scope"
  fi
fi

# Restricts a newline list of repo-relative paths to the changed set when scoped; passthrough
# otherwise.
scoped() {
  if [ "$scope" != "changed" ]; then
    cat
  else
    comm -12 <(sort -u) <(printf '%s\n' "$changed_set" | sort -u)
  fi
}

to_json_arr() {
  jq -R -s 'split("\n") | map(select(length > 0))'
}

missing_list=""
stray_list=""
unfiled_list=""

docs_dir="$repo/docs"

if [ "$doc_level" = "minimal" ] && [ ! -d "$docs_dir" ]; then
  note="docLevel=minimal, no docs tree expected"
elif [ -d "$docs_dir" ]; then
  all_md=$(cd "$repo" && find docs -type f -name '*.md' | sed 's#^\./##' | sort)

  unfiled_list=$( (printf '%s\n' "$all_md" | grep -E '^docs/[^/]+\.md$' || true) | scoped)

  code_files=$( (printf '%s\n' "$all_md" | grep -E "^docs/$code_language/" || true) | scoped)
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    rel="${f#docs/"$code_language"/}"
    for lang in $(jq -r '.[]' <<<"$doc_languages_json"); do
      counterpart="docs/$lang/$rel"
      [ -f "$repo/$counterpart" ] || missing_list="$missing_list
$counterpart"
    done
  done <<<"$code_files"

  for lang in $(jq -r '.[]' <<<"$doc_languages_json"); do
    lang_files=$( (printf '%s\n' "$all_md" | grep -E "^docs/$lang/" || true) | scoped)
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      rel="${f#docs/"$lang"/}"
      original="docs/$code_language/$rel"
      [ -f "$repo/$original" ] || stray_list="$stray_list
$f"
    done <<<"$lang_files"
  done
fi

missing_arr=$(to_json_arr <<<"$missing_list")
stray_arr=$(to_json_arr <<<"$stray_list")
unfiled_arr=$(to_json_arr <<<"$unfiled_list")

jq -n \
  --arg codeLanguage "$code_language" \
  --argjson docLanguages "$doc_languages_json" \
  --arg docLevel "$doc_level" \
  --arg scope "$scope" \
  --argjson missing "$missing_arr" \
  --argjson stray "$stray_arr" \
  --argjson unfiled "$unfiled_arr" \
  --arg note "$note" \
  '{codeLanguage: $codeLanguage, docLanguages: $docLanguages, docLevel: $docLevel, scope: $scope,
    missing: $missing, stray: $stray, unfiled: $unfiled, note: $note}'
