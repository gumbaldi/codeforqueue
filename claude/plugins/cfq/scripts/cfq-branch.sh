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

# Short names of every `origin/*` branch, `origin/HEAD` dropped, `origin/` prefix stripped.
# Shared by `check`'s newer-candidate scan and `plan`'s new-mode candidate collection so the
# "newest commit among these refs" enumeration is never done twice.
list_remote_branch_names() {
  git -C "$repo_root" branch -r --format='%(refname:short)' 2>/dev/null \
    | sed -n 's#^origin/##p' | grep -v '^HEAD$' || true
}

# Classifies a bidirectional ahead/behind count pair into the shared remoteState vocabulary --
# used by both `continue` mode and `new` mode so a caller never has to branch on `mode` to read it.
classify_remote_state() {
  local ahead="$1" behind="$2"
  if [ "$ahead" -gt 0 ] && [ "$behind" -gt 0 ]; then
    echo diverged
  elif [ "$ahead" -gt 0 ]; then
    echo ahead
  elif [ "$behind" -gt 0 ]; then
    echo behind
  else
    echo synced
  fi
}

# "<hash> <subject>" lines for commits reachable from $2 but not $1, as a JSON array -- the shape
# the push offer in references/queues.md names commits from.
list_unpushed_commits() {
  git -C "$repo_root" log --format='%h %s' "$1..$2" 2>/dev/null \
    | jq -R -s 'split("\n") | map(select(length > 0))'
}

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
    [ -n "$rb" ] || continue
    epoch=$(git -C "$repo_root" log -1 --format=%ct "refs/remotes/origin/$rb" 2>/dev/null) || continue
    if [ "$epoch" -gt "$ref_epoch" ] && [ "$epoch" -gt "$newer_epoch" ]; then
      newer_epoch="$epoch"
      newer_name="$rb"
    fi
  done < <(list_remote_branch_names)

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
  remote_state="unknown"
  pushable=false
  unpushed_json='[]'

  if [ "$remote_checked" = true ] \
    && git -C "$repo_root" rev-parse --verify -q "refs/heads/$existing" >/dev/null 2>&1 \
    && git -C "$repo_root" rev-parse --verify -q "refs/remotes/origin/$existing" >/dev/null 2>&1; then
    counts=$(git -C "$repo_root" rev-list --left-right --count "refs/remotes/origin/$existing...refs/heads/$existing")
    behind=$(printf '%s' "$counts" | awk '{print $1}')
    ahead=$(printf '%s' "$counts" | awk '{print $2}')
    remote_state=$(classify_remote_state "$ahead" "$behind")

    case "$remote_state" in
      behind)
        # `update-ref` can't move the ref of the branch that is currently checked out -- fall back
        # to a fast-forward merge there, and never mutate anything against a dirty tree.
        checked_out="$(git -C "$repo_root" symbolic-ref -q --short HEAD 2>/dev/null || true)"
        if [ "$checked_out" != "$existing" ]; then
          git -C "$repo_root" update-ref "refs/heads/$existing" "refs/remotes/origin/$existing"
        elif [ -z "$(git -C "$repo_root" status --porcelain)" ]; then
          git -C "$repo_root" merge -q --ff-only "refs/remotes/origin/$existing"
        else
          continue_warning_json=$(jq -n --arg msg \
            "local $existing is behind origin/$existing but the working tree is dirty — resolve before continuing" \
            '$msg')
        fi
        ;;
      ahead)
        pushable=true
        unpushed_json=$(list_unpushed_commits "refs/remotes/origin/$existing" "refs/heads/$existing")
        ;;
      diverged)
        continue_warning_json=$(jq -n --arg msg \
          "local $existing is $ahead commit(s) ahead of and $behind commit(s) behind origin/$existing — a push would be rejected" \
          '$msg')
        ;;
    esac
  fi

  jq -n --arg batch "$batch_name" --argjson num "$number_json" --arg branch "$existing" \
    --argjson remoteChecked "$remote_checked" --argjson remoteWarning "$continue_warning_json" \
    --arg remoteState "$remote_state" --argjson pushable "$pushable" --argjson unpushed "$unpushed_json" \
    '{mode: "continue", batch: $batch, batchNumber: $num, branch: $branch, base: null, candidates: [],
      remoteChecked: $remoteChecked, remoteWarning: $remoteWarning, remoteState: $remoteState,
      pushable: $pushable, unpushed: $unpushed}'
  exit 0
fi

branch="cfq/${batch_name}"

# --- Candidate collection: `origin/*` is the source of truth once remote-checked; local-only
# branches (no remote counterpart) are still offered, explicitly marked. Offline falls back to
# local branches only, every one implicitly local-only.
candidate_names=()
declare -A cand_ref=()
declare -A cand_local_only=()

if [ "$remote_checked" = true ]; then
  while IFS= read -r rb; do
    [ -n "$rb" ] || continue
    candidate_names+=("$rb")
    cand_ref["$rb"]="refs/remotes/origin/$rb"
    cand_local_only["$rb"]=false
  done < <(list_remote_branch_names)

  while IFS= read -r lb; do
    [ -n "$lb" ] && [ "$lb" != main ] || continue
    if [ -z "${cand_ref[$lb]+x}" ]; then
      candidate_names+=("$lb")
      cand_ref["$lb"]="refs/heads/$lb"
      cand_local_only["$lb"]=true
    fi
  done < <(git -C "$repo_root" branch --format='%(refname:short)')

  main_ref="refs/remotes/origin/main"
else
  while IFS= read -r lb; do
    [ -n "$lb" ] && [ "$lb" != main ] || continue
    candidate_names+=("$lb")
    cand_ref["$lb"]="refs/heads/$lb"
    cand_local_only["$lb"]=true
  done < <(git -C "$repo_root" branch --format='%(refname:short)')

  main_ref="refs/heads/main"
fi

# Highest-numbered cfq/<NNN>-... branch among all candidates, found before the aheadOfMain filter
# below so a fully-merged batch branch is never silently dropped — it's always offered as an
# alternative, per the batch context's "last batch is always offered" rule.
highest_name=""
highest_num=-1
for name in "${candidate_names[@]:-}"; do
  [ -n "$name" ] || continue
  case "$name" in cfq/*) ;; *) continue ;; esac
  num="$(parse_batch_number "${name#cfq/}")"
  [ -n "$num" ] || continue
  if [ "$num" -gt "$highest_num" ]; then
    highest_num="$num"
    highest_name="$name"
  fi
done

cand_jsons=()
for name in "${candidate_names[@]:-}"; do
  [ -n "$name" ] || continue
  ref="${cand_ref[$name]}"
  local_only="${cand_local_only[$name]}"

  ahead_of_main=0
  if git -C "$repo_root" rev-parse --verify -q "$ref" >/dev/null 2>&1 \
    && git -C "$repo_root" rev-parse --verify -q "$main_ref" >/dev/null 2>&1; then
    ahead_of_main=$(git -C "$repo_root" rev-list --count "$main_ref..$ref" 2>/dev/null || echo 0)
  fi

  is_highest=false
  [ "$name" = "$highest_name" ] && is_highest=true

  [ "$ahead_of_main" -gt 0 ] || [ "$is_highest" = true ] || continue

  merged=false
  if [ "$ahead_of_main" -eq 0 ] \
    && git -C "$repo_root" rev-parse --verify -q "$main_ref" >/dev/null 2>&1 \
    && git -C "$repo_root" merge-base --is-ancestor "$ref" "$main_ref" 2>/dev/null; then
    merged=true
  fi

  behind_remote=0
  ahead_remote=0
  if [ "$local_only" = false ] \
    && git -C "$repo_root" rev-parse --verify -q "refs/heads/$name" >/dev/null 2>&1; then
    counts=$(git -C "$repo_root" rev-list --left-right --count "$ref...refs/heads/$name")
    behind_remote=$(printf '%s' "$counts" | awk '{print $1}')
    ahead_remote=$(printf '%s' "$counts" | awk '{print $2}')
  fi

  last_commit=$(git -C "$repo_root" log -1 --format=%cI "$ref")
  last_epoch=$(git -C "$repo_root" log -1 --format=%ct "$ref")

  cand_jsons+=("$(jq -nc --arg name "$name" --arg ref "$ref" --argjson aheadOfMain "$ahead_of_main" \
    --argjson behindRemote "$behind_remote" --argjson aheadRemote "$ahead_remote" \
    --argjson localOnly "$local_only" --argjson highestBatch "$is_highest" \
    --argjson mergedIntoOriginMain "$merged" --arg lastCommit "$last_commit" --argjson lastEpoch "$last_epoch" \
    '{name: $name, ref: $ref, aheadOfMain: $aheadOfMain, behindRemote: $behindRemote,
      aheadRemote: $aheadRemote, localOnly: $localOnly, highestBatch: $highestBatch,
      mergedIntoOriginMain: $mergedIntoOriginMain, lastCommit: $lastCommit, lastEpoch: $lastEpoch}')")
done

if [ "${#cand_jsons[@]}" -eq 0 ]; then
  cand_json='[]'
else
  cand_json=$(printf '%s\n' "${cand_jsons[@]}" | jq -s 'sort_by(.lastEpoch) | reverse | map(del(.lastEpoch))')
fi

if [ "$(printf '%s' "$cand_json" | jq 'length')" -eq 0 ]; then
  base_json='"main"'
  base_ref_json=$(jq -n --arg r "$main_ref" '$r')
  base_name="main"
  base_local_only=false
else
  base_json=$(printf '%s' "$cand_json" | jq '.[0].name')
  base_ref_json=$(printf '%s' "$cand_json" | jq '.[0].ref')
  base_name=$(printf '%s' "$cand_json" | jq -r '.[0].name')
  base_local_only=$(printf '%s' "$cand_json" | jq -r '.[0].localOnly')
fi
base_local_ref="refs/heads/$base_name"
base_origin_ref="refs/remotes/origin/$base_name"

# remoteState/pushable/unpushed for the *chosen* base -- same vocabulary and helpers as `continue`
# mode, so a caller never has to branch on `mode` to read them. Local main ahead of/diverged from
# origin/main (or a candidate ahead of/diverged from its own origin counterpart) no longer
# influences candidate selection -- `baseRef` already points into `refs/remotes/` -- but is still a
# real state worth a push offer, which `references/queues.md` turns `remoteWarning` into.
remote_state="unknown"
pushable=false
unpushed_json='[]'
new_warning_json='null'
if [ "$remote_checked" = true ]; then
  if [ "$base_local_only" = true ]; then
    if git -C "$repo_root" rev-parse --verify -q "$base_local_ref" >/dev/null 2>&1; then
      remote_state="ahead"
      pushable=true
      unpushed_json=$(git -C "$repo_root" log --format='%h %s' "$base_local_ref" 2>/dev/null \
        | jq -R -s 'split("\n") | map(select(length > 0))')
    fi
  elif git -C "$repo_root" rev-parse --verify -q "$base_local_ref" >/dev/null 2>&1 \
    && git -C "$repo_root" rev-parse --verify -q "$base_origin_ref" >/dev/null 2>&1; then
    counts=$(git -C "$repo_root" rev-list --left-right --count "$base_origin_ref...$base_local_ref")
    behind=$(printf '%s' "$counts" | awk '{print $1}')
    ahead=$(printf '%s' "$counts" | awk '{print $2}')
    remote_state=$(classify_remote_state "$ahead" "$behind")
    case "$remote_state" in
      ahead)
        pushable=true
        unpushed_json=$(list_unpushed_commits "$base_origin_ref" "$base_local_ref")
        new_warning_json=$(jq -n --arg msg \
          "local $base_name is $ahead commit(s) ahead of origin/$base_name — a push before continuing would include them" \
          '$msg')
        ;;
      diverged)
        new_warning_json=$(jq -n --arg msg \
          "local $base_name has diverged from origin/$base_name — resolve before basing new work on it" \
          '$msg')
        ;;
    esac
  fi
fi

jq -n --arg batch "$batch_name" --argjson num "$number_json" --arg branch "$branch" \
  --argjson base "$base_json" --argjson baseRef "$base_ref_json" --argjson candidates "$cand_json" \
  --argjson remoteChecked "$remote_checked" --argjson remoteWarning "$new_warning_json" \
  --arg remoteState "$remote_state" --argjson pushable "$pushable" --argjson unpushed "$unpushed_json" \
  '{mode: "new", batch: $batch, batchNumber: $num, branch: $branch, base: $base, baseRef: $baseRef,
    candidates: $candidates, remoteChecked: $remoteChecked, remoteWarning: $remoteWarning,
    remoteState: $remoteState, pushable: $pushable, unpushed: $unpushed}'
