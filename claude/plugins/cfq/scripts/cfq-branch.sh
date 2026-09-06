#!/usr/bin/env bash
# Computes the branch/batch-identity decision for a batch go-ahead as one JSON object. Read-only —
# never creates or checks out a branch itself; the caller acts on `mode`. New branches use the
# batch's own stable directory name directly (`cfq/<batch-directory-name>`) — no version scanning,
# no pseudo-version increment, no identity derived from Git branch history.
# `check` resolves and judges one named branch (for a free-text base-branch answer) — also
# read-only, no dispatcher entry of its own since `branch` is already the noun.
# Usage: cfq-branch.sh plan <repo-root> <batch-dir-name>
#        cfq-branch.sh check <repo-root> <branch-name>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-branch.sh: jq is required" >&2; exit 1; }

usage="usage: cfq-branch.sh plan <repo-root> <batch-dir-name> | cfq-branch.sh check <repo-root> <branch-name>"
cmd="${1:?$usage}"
repo_root="${2:?$usage}"
case "$cmd" in
  plan|check) ;;
  *) echo "cfq-branch.sh: unknown command '$cmd'" >&2; exit 1 ;;
esac

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

# Best-effort remote check: no origin, or fetch fails (offline/sandboxed) -> remote_checked stays
# false and every path below behaves exactly as before this was added. Shared by both verbs.
remote_checked=false
if git -C "$repo_root" remote get-url origin >/dev/null 2>&1 \
  && git -C "$repo_root" fetch -q origin >/dev/null 2>&1; then
  remote_checked=true
fi

if [ "$cmd" = "check" ]; then
  branch_name="${3:?$usage}"

  origin_ref="refs/remotes/origin/$branch_name"
  local_ref="refs/heads/$branch_name"
  origin_exists=false
  local_exists=false
  git -C "$repo_root" rev-parse --verify -q "$origin_ref" >/dev/null 2>&1 && origin_exists=true
  git -C "$repo_root" rev-parse --verify -q "$local_ref" >/dev/null 2>&1 && local_exists=true

  if [ "$origin_exists" = false ] && [ "$local_exists" = false ]; then
    jq -n --arg name "$branch_name" --argjson remoteChecked "$remote_checked" \
      '{status: "UNRESOLVED", name: $name, ref: null, remoteChecked: $remoteChecked}'
    exit 0
  fi

  if [ "$origin_exists" = true ]; then
    ref="$origin_ref"
    local_only=false
  else
    ref="$local_ref"
    local_only=true
  fi

  ahead=0
  behind=0
  if [ "$origin_exists" = true ] && [ "$local_exists" = true ]; then
    counts=$(git -C "$repo_root" rev-list --left-right --count "$origin_ref...$local_ref")
    behind=$(printf '%s' "$counts" | awk '{print $1}')
    ahead=$(printf '%s' "$counts" | awk '{print $2}')
  fi

  last_commit=$(git -C "$repo_root" log -1 --format=%cI "$ref")
  ref_epoch=$(git -C "$repo_root" log -1 --format=%ct "$ref")

  # Same "newest commit wins" rule the `new`-mode candidate list below applies — phase 02 extracts
  # this into a shared function both paths call.
  newer_name=""
  newer_epoch=0
  while IFS= read -r rb; do
    [ -n "$rb" ] && [ "$rb" != "origin/HEAD" ] || continue
    epoch=$(git -C "$repo_root" log -1 --format=%ct "refs/remotes/$rb" 2>/dev/null) || continue
    if [ "$epoch" -gt "$ref_epoch" ] && [ "$epoch" -gt "$newer_epoch" ]; then
      newer_epoch="$epoch"
      newer_name="${rb#origin/}"
    fi
  done < <(git -C "$repo_root" branch -r --format='%(refname:short)' 2>/dev/null)

  newer_candidate_json='null'
  if [ -n "$newer_name" ]; then
    newer_iso=$(git -C "$repo_root" log -1 --format=%cI "refs/remotes/origin/$newer_name")
    newer_candidate_json=$(jq -n --arg name "$newer_name" --arg lastCommit "$newer_iso" \
      '{name: $name, lastCommit: $lastCommit}')
  fi

  jq -n --arg name "$branch_name" --arg ref "$ref" --argjson localOnly "$local_only" \
    --argjson ahead "$ahead" --argjson behind "$behind" --arg lastCommit "$last_commit" \
    --argjson newerCandidate "$newer_candidate_json" --argjson remoteChecked "$remote_checked" \
    '{status: "OK", name: $name, ref: $ref, localOnly: $localOnly, ahead: $ahead, behind: $behind,
      lastCommit: $lastCommit, newerCandidate: $newerCandidate, remoteChecked: $remoteChecked}'
  exit 0
fi

# --- plan ---
batch_name="${3:?$usage}"

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
