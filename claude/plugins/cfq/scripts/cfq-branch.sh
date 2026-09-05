#!/usr/bin/env bash
# Computes the branch/batch-identity decision for a batch go-ahead as one JSON object. Read-only —
# never creates or checks out a branch itself; the caller acts on `mode`. New branches use the
# batch's own stable directory name directly (`cfq/<batch-directory-name>`) — no version scanning,
# no pseudo-version increment, no identity derived from Git branch history.
# Usage: cfq-branch.sh plan <repo-root> <batch-dir-name>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-branch.sh: jq is required" >&2; exit 1; }

cmd="${1:?usage: cfq-branch.sh plan <repo-root> <batch-dir-name>}"
repo_root="${2:?usage: cfq-branch.sh plan <repo-root> <batch-dir-name>}"
batch_name="${3:?usage: cfq-branch.sh plan <repo-root> <batch-dir-name>}"
[ "$cmd" = "plan" ] || { echo "cfq-branch.sh: unknown command '$cmd'" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

# New-format batch directory names are <digits>-<YYYY-MM-DD>-<slug> (the number precedes the
# date); legacy names start directly with the date. Prints the plain integer (no leading zeros) on
# a match, nothing on no match — mirrors cfq-changelog.sh's own parse_batch_number.
parse_batch_number() {
  if [[ "$1" =~ ^([0-9]+)-[0-9]{4}-[0-9]{2}-[0-9]{2}- ]]; then
    printf '%d\n' "$((10#${BASH_REMATCH[1]}))"
  fi
}
number="$(parse_batch_number "$batch_name")"
number_json='null'; [ -n "$number" ] && number_json="$number"

slug=$(printf '%s' "$batch_name" | sed -E 's/^[0-9]+-[0-9]{4}-[0-9]{2}-[0-9]{2}-//; s/^[0-9]{4}-[0-9]{2}-[0-9]{2}-//')

branch_per_batch=$("$cfq" settings get branchPerBatch)
if [ "$branch_per_batch" = "false" ]; then
  jq -n --arg batch "$batch_name" --argjson num "$number_json" \
    '{mode: "off", batch: $batch, batchNumber: $num, branch: null, base: null, candidates: [], remoteChecked: false, remoteWarning: null}'
  exit 0
fi

# Best-effort remote check: no origin, or fetch fails (offline/sandboxed) -> remote_checked stays
# false and every path below behaves exactly as before this was added.
remote_checked=false
if git -C "$repo_root" remote get-url origin >/dev/null 2>&1 \
  && git -C "$repo_root" fetch -q origin >/dev/null 2>&1; then
  remote_checked=true
fi

# Prefer the branch already persisted in the CFQ changelog for this exact batch — authoritative,
# since it is the branch that was actually checked out at init time — but only once it's confirmed
# to still exist; a deleted branch falls through to the suffix match below rather than being
# handed to the caller as an unresolvable "continue". Also falls through when the changelog
# doesn't know this batch yet, e.g. a batch parked before the changelog existed.
existing="$("$cfq" changelog branch-for "$repo_root" "$batch_name" 2>/dev/null || true)"
if [ -n "$existing" ] && ! git -C "$repo_root" rev-parse --verify -q "refs/heads/$existing" >/dev/null 2>&1 \
  && ! git -C "$repo_root" rev-parse --verify -q "refs/remotes/origin/$existing" >/dev/null 2>&1; then
  existing=""
fi
if [ -z "$existing" ]; then
  existing=$(git -C "$repo_root" branch -a --format='%(refname:short)' | sed 's#^origin/##' | sort -u \
    | grep -E -- "-${slug}\$" || true)
  existing=$(printf '%s\n' "$existing" | head -1)
fi

if [ -n "$existing" ]; then
  continue_warning_json='null'
  if [ "$remote_checked" = true ] \
    && git -C "$repo_root" rev-parse --verify -q "refs/heads/$existing" >/dev/null 2>&1 \
    && git -C "$repo_root" rev-parse --verify -q "refs/remotes/origin/$existing" >/dev/null 2>&1; then
    if git -C "$repo_root" merge-base --is-ancestor "refs/heads/$existing" "refs/remotes/origin/$existing"; then
      if [ "$(git -C "$repo_root" symbolic-ref -q --short HEAD 2>/dev/null)" != "$existing" ]; then
        git -C "$repo_root" update-ref "refs/heads/$existing" "refs/remotes/origin/$existing"
      fi
    else
      ahead_count=$(git -C "$repo_root" rev-list --count "refs/remotes/origin/$existing..refs/heads/$existing")
      continue_warning_json=$(jq -n --arg msg \
        "local $existing is $ahead_count commit(s) ahead of/diverged from origin/$existing — resolve before continuing" \
        '$msg')
    fi
  fi
  jq -n --arg batch "$batch_name" --argjson num "$number_json" --arg branch "$existing" \
    --argjson remoteChecked "$remote_checked" --argjson remoteWarning "$continue_warning_json" \
    '{mode: "continue", batch: $batch, batchNumber: $num, branch: $branch, base: null, candidates: [], remoteChecked: $remoteChecked, remoteWarning: $remoteWarning}'
  exit 0
fi

branch="cfq/${batch_name}"

candidates=()
while IFS= read -r b; do
  [ -n "$b" ] && [ "$b" != main ] || continue
  cnt=$(git -C "$repo_root" rev-list --count main.."$b")
  [ "$cnt" -gt 0 ] && candidates+=("$b")
done < <(git -C "$repo_root" branch --format='%(refname:short)')

new_warning_json='null'
if [ "$remote_checked" = true ] && git -C "$repo_root" rev-parse --verify -q refs/remotes/origin/main >/dev/null 2>&1; then
  if git -C "$repo_root" merge-base --is-ancestor refs/heads/main refs/remotes/origin/main; then
    if [ "$(git -C "$repo_root" symbolic-ref -q --short HEAD 2>/dev/null)" != main ]; then
      git -C "$repo_root" update-ref refs/heads/main refs/remotes/origin/main
    fi
  else
    ahead_count=$(git -C "$repo_root" rev-list --count refs/remotes/origin/main..refs/heads/main)
    candidates+=(main)
    new_warning_json=$(jq -n --arg msg \
      "local main is $ahead_count commit(s) ahead of origin/main — resolve before basing new work on it" \
      '$msg')
  fi
fi

if [ "${#candidates[@]}" -eq 0 ]; then
  base_json='"main"'
else
  base_json='null'
fi
cand_json=$(printf '%s\n' "${candidates[@]:-}" | sed '/^$/d' | jq -R . | jq -s .)

jq -n --arg batch "$batch_name" --argjson num "$number_json" --arg branch "$branch" \
  --argjson base "$base_json" --argjson candidates "$cand_json" \
  --argjson remoteChecked "$remote_checked" --argjson remoteWarning "$new_warning_json" \
  '{mode: "new", batch: $batch, batchNumber: $num, branch: $branch, base: $base, candidates: $candidates, remoteChecked: $remoteChecked, remoteWarning: $remoteWarning}'
