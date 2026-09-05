#!/usr/bin/env bash
# Security snapshot for a target repo. Never fails: always prints one JSON object on stdout and
# exits 0, even when a forge is unreachable or unauthenticated. GitHub gets real findings via
# `gh` (Dependabot + code-scanning); Gitea has no such API (checked against its Swagger
# definition), so only reachability/login are confirmed there and the local npm audit is the
# actual source. A local audit runs in addition on every repo with a package.json, never only
# as a substitute.
# Detection-only mode (CFQ_SECURITY_DETECT_ONLY=1) prints "<forge> <host> <owner/name>" for the
# origin remote and exits — for testing the string parsing without network or credentials.
# Usage: cfq-security.sh <repo-root>
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-security.sh: jq is required" >&2; exit 1; }

repo="${1:?usage: cfq-security.sh <repo-root>}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

# timeout/gtimeout is an optional capability (per config/dependencies.txt), not a global cfq hard
# dependency — its absence degrades this script's network-bound checks instead of failing them.
sec_timeout_bin=""
if command -v timeout >/dev/null 2>&1; then
  sec_timeout_bin="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  sec_timeout_bin="gtimeout"
fi
sec_timeout=$("$cfq" settings get securityTimeoutSeconds)
sec_cap=$("$cfq" settings get securityFindingsCap)

run_timeout() {
  [ -n "$sec_timeout_bin" ] || return 1
  "$sec_timeout_bin" "$sec_timeout" "$@"
}

# Prints "<forge> <host> <owner/name>" for the repo's origin remote. forge is one of
# github|gitea|unknown|none. The gitea branch never guesses: only a host that appears in the tea
# login list (CFQ_TEA_LOGIN_HOSTS overrides it for tests) is trusted — otherwise tea would
# silently query the wrong instance ("falling back to login ...").
detect_forge() {
  local repo="$1" url host owner_name rest hosts h forge

  url=$(git -C "$repo" remote get-url origin 2>/dev/null || true)
  if [ -z "$url" ]; then
    printf 'none - -\n'
    return
  fi

  case "$url" in
    git@*:*)
      host="${url#git@}"; host="${host%%:*}"
      owner_name="${url#*:}"
      ;;
    ssh://*)
      rest="${url#ssh://}"
      rest="${rest#*@}"
      host="${rest%%[:/]*}"
      owner_name="${rest#*/}"
      ;;
    http://*)
      rest="${url#http://}"
      host="${rest%%/*}"
      owner_name="${rest#*/}"
      ;;
    https://*)
      rest="${url#https://}"
      host="${rest%%/*}"
      owner_name="${rest#*/}"
      ;;
    *)
      printf 'unknown - -\n'
      return
      ;;
  esac
  owner_name="${owner_name%.git}"

  if [ "$host" = "github.com" ]; then
    forge="github"
  else
    if [ -n "${CFQ_TEA_LOGIN_HOSTS:-}" ]; then
      hosts="$CFQ_TEA_LOGIN_HOSTS"
    elif command -v tea >/dev/null 2>&1; then
      hosts=$(run_timeout tea login list --output simple 2>/dev/null \
        | awk '{print $2}' | sed -E 's#^[a-zA-Z]+://##; s#[:/].*$##')
    else
      hosts=""
    fi
    forge="unknown"
    for h in $hosts; do
      [ "$h" = "$host" ] && forge="gitea" && break
    done
  fi

  printf '%s %s %s\n' "$forge" "$host" "$owner_name"
}

if [ "${CFQ_SECURITY_DETECT_ONLY:-}" = "1" ]; then
  detect_forge "$repo"
  exit 0
fi

read -r forge host slug <<<"$(detect_forge "$repo")"

findings='[]'
sources='[]'
hint=""

add_findings() { findings=$(jq -c -n --argjson a "$findings" --argjson b "$1" '$a + $b'); }
add_source()   { sources=$(jq -c -n --argjson a "$sources" --arg s "$1" '$a + [$s]'); }

if [ "$forge" = "github" ] && command -v gh >/dev/null 2>&1 && run_timeout gh api user >/dev/null 2>&1; then
  hint="Dependabot and code scanning return nothing for this repo (disabled or never configured) — local audit is the only source."
  dep=$(run_timeout gh api --paginate "repos/$slug/dependabot/alerts?state=open" 2>/dev/null || true)
  if [ -n "$dep" ] && printf '%s' "$dep" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
    add_source dependabot
    add_findings "$(printf '%s' "$dep" | jq -c '[.[] | {
      id: (.dependency.package.name // "?"),
      severity: (.security_advisory.severity // "unknown"),
      source: "dependabot",
      fix: ((.security_vulnerability.first_patched_version // null) != null)
    }]')"
  fi

  cs=$(run_timeout gh api --paginate "repos/$slug/code-scanning/alerts?state=open" 2>/dev/null || true)
  if [ -n "$cs" ] && printf '%s' "$cs" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
    add_source code-scanning
    add_findings "$(printf '%s' "$cs" | jq -c '[.[] | {
      id: (.rule.id // "?"),
      severity: (.rule.security_severity_level // .rule.severity // "unknown"),
      source: "code-scanning",
      fix: false
    }]')"
  fi
  [ "$(printf '%s' "$sources" | jq 'length')" != "0" ] && hint=""
elif [ "$forge" = "gitea" ] && command -v tea >/dev/null 2>&1; then
  if run_timeout tea api "repos/$slug" >/dev/null 2>&1; then
    add_source "gitea:none"
    hint="This forge offers no security-alert API — local audit is the source."
  else
    hint="tea login add --url $host"
  fi
fi

# Local audit: runs on every repo with a package.json, in addition to any forge source above.
if [ -f "$repo/package.json" ] && command -v npm >/dev/null 2>&1; then
  npm_out=$(cd "$repo" && run_timeout npm audit --json 2>/dev/null || true)
  if [ -n "$npm_out" ]; then
    npm_findings=$(printf '%s' "$npm_out" | jq -c '
      [ (.vulnerabilities // {}) | to_entries[] | {
          id: .key,
          severity: ((.value.severity // "unknown") | if . == "moderate" then "medium" else . end),
          source: "npm-audit",
          fix: ((.value.fixAvailable // false) != false)
        } ]
    ' 2>/dev/null || echo '[]')
    npm_findings=$(jq -c -n --argjson existing "$findings" --argjson n "$npm_findings" '
      $n | map(select(. as $f | ($existing | any(.id == $f.id and .severity == $f.severity)) | not))
    ')
    if [ "$(printf '%s' "$npm_findings" | jq 'length')" != "0" ]; then
      add_source npm-audit
      add_findings "$npm_findings"
    fi
  fi
fi

if [ "$(printf '%s' "$sources" | jq 'length')" = "0" ]; then
  if [ -z "$hint" ]; then
    if [ -z "$sec_timeout_bin" ]; then
      hint="timeout/gtimeout not found on this host — security checks are skipped until one is installed"
    else
      case "$forge" in
        github) hint="gh auth login" ;;
        gitea)  hint="tea login add --url $host" ;;
        *)      hint="no supported manifest found" ;;
      esac
    fi
  fi
  jq -n --arg forge "$forge" --arg hint "$hint" \
    '{available: false, forge: $forge, sources: [], counts: {}, findings: [], hint: $hint}'
  exit 0
fi

counts=$(printf '%s' "$findings" | jq -c 'group_by(.severity) | map({key: .[0].severity, value: length}) | from_entries')
fixable=$(printf '%s' "$findings" | jq -c 'map(select(.fix == true)) | group_by(.severity) | map({key: .[0].severity, value: length}) | from_entries')
top20=$(printf '%s' "$findings" | jq -c --argjson cap "$sec_cap" '
  def rank: if .severity == "critical" then 0 elif .severity == "high" then 1
            elif .severity == "medium" then 2 elif .severity == "low" then 3 else 4 end;
  sort_by(rank) | .[0:$cap] | map({id, severity, source, fix})
')

jq -n --arg forge "$forge" --argjson sources "$sources" --argjson counts "$counts" \
      --argjson fixable "$fixable" --argjson findings "$top20" --arg hint "$hint" '
  {available: true, forge: $forge, sources: $sources, counts: $counts, fixable: $fixable, findings: $findings}
  + (if $hint != "" then {hint: $hint} else {} end)
'
