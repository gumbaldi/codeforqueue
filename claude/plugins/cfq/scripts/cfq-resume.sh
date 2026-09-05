#!/usr/bin/env bash
# Deterministic batch-state reconstruction for a fresh /ifq session — no LLM summarization, only
# facts read from disk, report.json, and git. Called unconditionally at Step 3b, fresh batch or
# resumed alike — same call, same shape either way.
# Usage: cfq-resume.sh <repo-root> <batch-dir>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-resume.sh: jq is required" >&2; exit 1; }

repo_root="${1:?usage: cfq-resume.sh <repo-root> <batch-dir>}"
batch_dir="${2:?usage: cfq-resume.sh <repo-root> <batch-dir>}"
batch_dir="${batch_dir%/}"
[ -d "$batch_dir" ] || { echo "cfq-resume.sh: no such batch directory: $batch_dir" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
batch_name="$(basename "$batch_dir")"
report="$batch_dir/report.json"

branch_json=$("$cfq" branch plan "$repo_root" "$batch_name" 2>/dev/null \
  || echo '{"mode":null,"batch":null,"batchNumber":null,"branch":null,"base":null,"candidates":[]}')
branch_name=$(jq -r '.branch // empty' <<<"$branch_json")

open_recs=$(mktemp)
done_recs=$(mktemp)
trap 'rm -f "$open_recs" "$done_recs"' EXIT

shopt -s nullglob
for f in "$batch_dir"/[0-9][0-9]-*.md; do
  num=$(basename "$f" | sed -n 's/^\([0-9][0-9]\)-.*/\1/p')
  slug=$(basename "$f" .md)
  size=$(awk '/^## Size/{g=1;next} g && NF{print $1; exit}' "$f")
  printf '%s\t%s\t%s\n' "$num" "$slug" "${size:-M}" >>"$open_recs"
done
for f in "$batch_dir/done"/[0-9][0-9]-*.md; do
  num=$(basename "$f" | sed -n 's/^\([0-9][0-9]\)-.*/\1/p')
  slug=$(basename "$f" .md)
  commit=""
  [ -f "$report" ] && commit=$(jq -r --arg s "$slug" \
    '[.phases[] | select(.phase == $s and .status == "green")] | last.commit // empty' "$report")
  printf '%s\t%s\t%s\n' "$num" "$slug" "$commit" >>"$done_recs"
done
shopt -u nullglob

deviations_json='[]'
red_json='[]'
last_commit=""
last_commit_source="null"
if [ -f "$report" ]; then
  deviations_json=$(jq -c '
    [.phases[] | select(.status == "green") | . as $p
     | (($p.deviations // []) | map({phase: $p.phase, text: .}))] | flatten' "$report")
  red_json=$(jq -c '[.phases[] | select(.status == "red") | .phase] | unique' "$report")
  last_commit=$(jq -r '
    [.phases[] | select(.status == "green" and ((.commit // "") != ""))] | last.commit // empty' "$report")
fi
[ -n "$last_commit" ] && last_commit_source='"report"'
if [ -z "$last_commit" ] && [ -n "$branch_name" ]; then
  # --verify: without it, rev-parse echoes the literal unresolved arg to stdout on failure
  # instead of nothing, which would make an unresolvable branch look like a valid commit.
  last_commit=$(git -C "$repo_root" rev-parse --verify -q "$branch_name" 2>/dev/null \
    || git -C "$repo_root" rev-parse --verify -q "origin/$branch_name" 2>/dev/null || true)
  [ -n "$last_commit" ] && last_commit_source='"branch-tip"'
fi
last_commit_json='null'
[ -n "$last_commit" ] && last_commit_json=$(jq -n --arg c "$last_commit" '$c')

ctx_exists="false"; ctx_path='null'
if [ -f "$batch_dir/.batch-context.md" ]; then
  ctx_exists="true"
  ctx_path=$(jq -n --arg p "$batch_dir/.batch-context.md" '$p')
fi

jq -n --rawfile open "$open_recs" --rawfile doneRecs "$done_recs" \
  --arg batch "$batch_name" --arg batchDir "$batch_dir" --argjson branch "$branch_json" \
  --argjson lastCommit "$last_commit_json" --argjson lastCommitSource "$last_commit_source" \
  --argjson deviations "$deviations_json" --argjson redPhases "$red_json" \
  --argjson ctxExists "$ctx_exists" --argjson ctxPath "$ctx_path" '
  def parse_tsv: split("\n") | map(select(length > 0)) | map(split("\t"));
  {
    batch: $batch, batchDir: $batchDir, branch: $branch,
    phasesOpen: ($open | parse_tsv | map({num: .[0], slug: .[1], size: .[2]})),
    phasesDone: ($doneRecs | parse_tsv | map({num: .[0], slug: .[1],
      commit: (if .[2] == "" then null else .[2] end)})),
    lastCommit: $lastCommit, lastCommitSource: $lastCommitSource,
    deviations: $deviations, redPhases: $redPhases,
    batchContext: {exists: $ctxExists, path: $ctxPath}
  }'
