#!/usr/bin/env bash
# One lock per repo: only one implement-for-queue session may work a repo at a time.
# Liveness comes from the holder's transcript mtime, not from a fixed expiry.
# Usage: cfq-lock.sh acquire <repo-root> <batch> | release <repo-root> | status <repo-root>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-lock.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"

mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }

# Prints "alive" or "dead" for the lock content on stdin. $1: epoch fallback, $2: stale seconds.
liveness() {
  local t at age now stale_s="$2"
  now=$(date +%s)
  t=$(jq -r '.transcript // ""')
  if [ -n "$t" ] && [ -f "$t" ]; then
    age=$(( now - $(mtime "$t") ))
  else
    at=$(printf '%s' "$1")
    age=$(( now - at ))
  fi
  [ "$age" -lt "$stale_s" ] && echo alive || echo dead
}

cmd="${1:-}"
case "$cmd" in
  acquire)
    repo="${2:?usage: cfq-lock.sh acquire <repo-root> <batch>}"
    batch="${3:?usage: cfq-lock.sh acquire <repo-root> <batch>}"
    "$cfq" layout ensure "$repo" >/dev/null
    stale_s=$("$cfq" settings get --repo "$repo" sessionStaleSeconds)
    f="$(lockfile "$repo")"
    mkdir -p "$(dirname "$f")"
    sid="${CLAUDE_CODE_SESSION_ID:-unknown}"
    tpath=$("$cfq" runtime transcript-path --repo "$repo" --exact)

    if [ -f "$f" ]; then
      holder=$(jq -r '.session_id // ""' "$f" 2>/dev/null || true)
      hbatch=$(jq -r '.batch // ""' "$f" 2>/dev/null || true)
      hepoch=$(jq -r '.epoch // 0' "$f" 2>/dev/null || echo 0)
      if [ "$holder" = "$sid" ]; then
        echo "OK $hbatch (already held by this session)"; exit 0
      fi
      state=$(jq -c . "$f" | liveness "$hepoch" "$stale_s")
      if [ "$state" = "alive" ]; then
        echo "LOCKED $holder $hbatch $(jq -r '.at // "?"' "$f")" >&2
        exit 1
      fi
      echo "TAKEOVER $holder $hbatch (stale)" >&2
    fi

    jq -n --arg s "$sid" --arg b "$batch" --arg t "$tpath" \
          --arg at "$(date -Iseconds)" --argjson e "$(date +%s)" \
          '{session_id: $s, batch: $b, transcript: $t, at: $at, epoch: $e}' >"$f.tmp"
    mv "$f.tmp" "$f"
    echo "OK $batch"
    ;;

  release)
    repo="${2:?usage: cfq-lock.sh release <repo-root>}"
    f="$(lockfile "$repo")"
    [ -f "$f" ] || { echo "FREE"; exit 0; }
    holder=$(jq -r '.session_id // ""' "$f" 2>/dev/null || true)
    sid="${CLAUDE_CODE_SESSION_ID:-unknown}"
    if [ "$holder" != "$sid" ]; then
      echo "cfq-lock.sh: lock held by $holder, not releasing" >&2
      exit 1
    fi
    rm -f "$f"
    echo "FREE"
    ;;

  status)
    repo="${2:?usage: cfq-lock.sh status <repo-root>}"
    f="$(lockfile "$repo")"
    [ -f "$f" ] || { echo "FREE"; exit 0; }
    stale_s=$("$cfq" settings get --repo "$repo" sessionStaleSeconds)
    hepoch=$(jq -r '.epoch // 0' "$f")
    state=$(jq -c . "$f" | liveness "$hepoch" "$stale_s")
    jq -r --arg st "$state" '"\($st | ascii_upcase) \(.session_id) \(.batch) \(.at)"' "$f"
    ;;

  *)
    echo "usage: cfq-lock.sh acquire <repo-root> <batch> | release <repo-root> | status <repo-root>" >&2
    exit 1
    ;;
esac
