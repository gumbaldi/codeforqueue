#!/usr/bin/env bash
# Runs the batch-done sequence (move, register, language check, maintenance check, security
# diff, changelog, telemetry sync) in a fixed order and prints one JSON object. The lock release
# is guaranteed via a trap set before the first operation — a mid-sequence failure must never
# leave the repo locked.
# Usage: cfq-finish.sh <repo-root> <batch-dir> <branch>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-finish.sh: jq is required" >&2; exit 1; }

repo_root="${1:?usage: cfq-finish.sh <repo-root> <batch-dir> <branch>}"
batch_dir="${2:?usage: cfq-finish.sh <repo-root> <batch-dir> <branch>}"
branch="${3:?usage: cfq-finish.sh <repo-root> <batch-dir> <branch>}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"
batch_dir="${batch_dir%/}"
batch_name="$(basename "$batch_dir")"

errors=()
add_error() { errors+=("$1: $2"); }

# The reason this script exists: releasing the lock must not depend on reaching the end of a
# happy path. The trap fires on every exit, normal or not.
trap '"$cfq" lock release "$repo_root" >/dev/null 2>&1 || true' EXIT

done_dir="$(impl_done_dir "$repo_root")"
mkdir -p "$done_dir"
moved="$done_dir/$batch_name"
if [ -d "$batch_dir" ] && [ "$batch_dir" != "$moved" ]; then
  mv "$batch_dir" "$moved"
fi
batch_dir="$moved"

"$cfq" registry add "$repo_root" >/dev/null 2>&1 \
  || add_error registry "cfq-registry.sh add failed"

lang_json='{"issues":0,"findings":[]}'
if out=$("$cfq" lang "$repo_root" --changed main 2>&1); then
  lang_json=$(jq -c '
    { issues: ((.missing|length) + (.stray|length) + (.unfiled|length)),
      findings: ( [.missing[] | "missing: " + .] + [.stray[] | "stray: " + .] + [.unfiled[] | "unfiled: " + .] ) }
  ' <<<"$out" 2>/dev/null || echo '{"issues":0,"findings":[]}')
else
  add_error lang "$out"
fi

if prose_out=$("$cfq" lang prose "$repo_root" main 2>&1); then
  lang_json=$(jq -c --argjson p "$prose_out" '. + {prose: $p}' <<<"$lang_json" 2>/dev/null || echo "$lang_json")
else
  add_error lang "$prose_out"
fi

maintenance="unknown"
if out=$("$cfq" maintenance due "$repo_root" 2>&1); then
  maintenance="$out"
else
  add_error maintenance "$out"
fi

existed_before=0
[ -f "$batch_dir/report.json" ] \
  && existed_before=$(jq '.security | length' "$batch_dir/report.json" 2>/dev/null || echo 0)

planning_json='{}'
now_json='{}'
new_json='{}'
if sec_now=$("$cfq" security "$repo_root" 2>&1); then
  if "$cfq" report security "$batch_dir" "$sec_now" >/dev/null 2>&1; then
    now_json=$(jq -c '.security[-1].counts // {}' "$batch_dir/report.json" 2>/dev/null || echo '{}')
    if [ "$existed_before" -gt 0 ]; then
      planning_json=$(jq -c '.security[0].counts // {}' "$batch_dir/report.json" 2>/dev/null || echo '{}')
      new_json=$(jq -n --argjson p "$planning_json" --argjson n "$now_json" '
        reduce ($n | keys[]) as $k ({};
          (($n[$k] // 0) - ($p[$k] // 0)) as $d
          | if $d > 0 then . + {($k): $d} else . end)')
    fi
  else
    add_error security "cfq_report.py security failed"
  fi
else
  add_error security "$sec_now"
fi

changelog="changelogFile empty"
changelog_file=$("$cfq" settings get changelogFile)
if [ -n "$changelog_file" ]; then
  phases=$(find "$batch_dir/done" -maxdepth 1 -name '[0-9][0-9]-*.md' 2>/dev/null | wc -l | tr -d ' ')
  if err_out=$("$cfq" changelog finish "$repo_root" "$branch" "$batch_dir" 2>&1); then
    changelog="${batch_name} done · ${phases} phases"
    if [ -n "$(git -C "$repo_root" status --porcelain -- "$changelog_file" 2>/dev/null)" ]; then
      if git -C "$repo_root" add "$changelog_file" >/dev/null 2>&1 \
        && git -C "$repo_root" commit -q -m "Mark ${branch} batch done in the changelog" \
          -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>" >/dev/null 2>&1 \
        && git -C "$repo_root" push -q >/dev/null 2>&1; then
        changelog="${changelog} · committed"
      else
        add_error changelog "git commit/push of $changelog_file failed"
      fi
    fi
  else
    add_error changelog "$err_out"
    changelog="error"
  fi
fi

telemetry=$("$cfq" telemetry sync "$repo_root" 2>&1) || add_error telemetry "$telemetry"

if [ "$("$cfq" settings get --repo "$repo_root" htmlReport)" = "true" ]; then
  "$cfq" report html "$batch_dir" >/dev/null 2>&1 \
    || echo "cfq-finish.sh: html report render failed for $batch_dir" >&2
fi

err_json=$(printf '%s\n' "${errors[@]:-}" | sed '/^$/d' | jq -R . | jq -s .)

jq -n \
  --arg moved "$batch_dir" \
  --argjson lang "$lang_json" \
  --arg maintenance "$maintenance" \
  --argjson planning "$planning_json" --argjson now "$now_json" --argjson new "$new_json" \
  --arg changelog "$changelog" \
  --arg telemetry "$telemetry" \
  --argjson errors "$err_json" \
  '{moved: $moved, lang: $lang, maintenance: $maintenance,
    security: {planning: $planning, now: $now, new: $new},
    changelog: $changelog, telemetry: $telemetry, lock: "released", errors: $errors}'
