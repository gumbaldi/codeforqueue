#!/usr/bin/env bash
# Telemetry for cfq. Aggregates the running session's transcript into the batch report and a
# repo-local JSONL. Numbers, timestamps and names only — never prompt text, never tool arguments.
# Usage: cfq-telemetry.sh record <batch-dir> planning|phase [<phase-slug>]
#        cfq-telemetry.sh record <batch-dir> bootstrap <skill-name> <callCount> <durationMs>
#        cfq-telemetry.sh sync [<repo-root>]
set -eu

command -v jq >/dev/null 2>&1 || { echo "cfq-telemetry.sh: jq is required" >&2; exit 1; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"
# shellcheck source=cfq-paths.sh
. "$script_dir/cfq-paths.sh"

# pwd-based resolution (matches ctx-usage.sh), shared via the runtime adapter.
transcript_path() {
  "$cfq" runtime transcript-path
}

cmd="${1:-}"
case "$cmd" in
  record)
    dir="${2:?usage: cfq-telemetry.sh record <batch-dir> planning|phase [<phase-slug>]}"
    kind="${3:?usage: cfq-telemetry.sh record <batch-dir> planning|phase [<phase-slug>]}"
    [ -d "$dir" ] || { echo "cfq-telemetry.sh: no such batch directory: $dir" >&2; exit 1; }

    if [ "$kind" = "bootstrap" ]; then
      skill="${4:?usage: cfq-telemetry.sh record <batch-dir> bootstrap <skill-name> <callCount> <durationMs>}"
      call_count="${5:?usage: cfq-telemetry.sh record <batch-dir> bootstrap <skill-name> <callCount> <durationMs>}"
      duration_ms="${6:?usage: cfq-telemetry.sh record <batch-dir> bootstrap <skill-name> <callCount> <durationMs>}"
      case "$call_count" in ''|*[!0-9]*) echo "cfq-telemetry.sh: callCount must be a non-negative integer" >&2; exit 1 ;; esac
      case "$duration_ms" in ''|*[!0-9]*) echo "cfq-telemetry.sh: durationMs must be a non-negative integer" >&2; exit 1 ;; esac

      repo="$(cd "$dir" && git rev-parse --show-toplevel 2>/dev/null || echo '')"
      jsonl="$(telemetry_log "$repo")"
      rec=$(jq -n -c \
          --arg batch "$(basename "$dir")" --arg repo "$repo" --arg skill "$skill" \
          --argjson call_count "$call_count" --argjson duration_ms "$duration_ms" \
          --arg ts "$(date -Iseconds)" '
        {schema: 1, kind: "bootstrap", repo: $repo, batch: $batch, skill: $skill,
         call_count: $call_count, duration_ms: $duration_ms, timestamp: $ts}
      ')
      mkdir -p "$(dirname "$jsonl")"
      printf '%s\n' "$rec" >> "$jsonl"
      printf '%s\n' "$rec" | jq -r '"telemetry: bootstrap \(.skill) \(.call_count) calls / \(.duration_ms) ms"'
      exit 0
    fi

    phase="${4:-}"

    tf="$(transcript_path)"
    [ -n "$tf" ] && [ -f "$tf" ] || { echo "cfq-telemetry.sh: no transcript found — skipped" >&2; exit 0; }

    repo="$(cd "$dir" && git rev-parse --show-toplevel 2>/dev/null || echo '')"
    jsonl="$(telemetry_log "$repo")"
    sid="${CLAUDE_CODE_SESSION_ID:-}"

    # Window start: end of the last record of this same session, empty on the first one.
    since=""
    if [ -f "$jsonl" ] && [ -n "$sid" ]; then
      since=$(jq -r --arg s "$sid" 'select(.session_id == $s) | .until' "$jsonl" 2>/dev/null | tail -1 || true)
      [ "$since" = "null" ] && since=""
    fi

    # Skills the plan recommended for this phase — stored next to the ones actually used.
    recommended='[]'
    if [ -n "$phase" ]; then
      pf="$dir/$phase.md"; [ -f "$pf" ] || pf="$dir/done/$phase.md"
      if [ -f "$pf" ]; then
        recommended=$(sed -n '/^## Empfohlene Skills/,/^## /p' "$pf" \
          | sed -n 's/^- \([A-Za-z0-9:._-]\{1,\}\).*/\1/p' \
          | jq -R -s 'split("\n") | map(select(length > 0))')
      fi
    fi

    rec=$(jq -s -c \
        --arg since "$since" --arg kind "$kind" --arg phase "$phase" \
        --arg batch "$(basename "$dir")" --arg repo "$repo" \
        --argjson recommended "$recommended" '
      def n: . // 0;
      def sums: {
        turns: length,
        input:          ([.[].message.usage.input_tokens | n]                | add // 0),
        output:         ([.[].message.usage.output_tokens | n]               | add // 0),
        cache_read:     ([.[].message.usage.cache_read_input_tokens | n]     | add // 0),
        cache_creation: ([.[].message.usage.cache_creation_input_tokens | n] | add // 0)
      } | . + {
        # cache_read repeats the same prefix on every turn — summing it across turns counts the
        # same tokens dozens of times. billable_in is the part that is actually new per turn and
        # is the number to compare phases by; cache_read stays available as a raw metric.
        billable_in: (.input + .cache_creation)
      };
      def bucket(f): group_by(f) | map({ key: (.[0] | f | tostring), value: sums }) | from_entries;
      def ts: try (sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch 0;

      map(select(.type == "assistant"
                 and ((.timestamp // "") != "")
                 and ($since == "" or .timestamp > $since))) as $t
      | ($t | map(select(.isSidechain == true))) as $sub
      | {
          schema: 1,
          kind:   $kind,
          repo:   $repo,
          batch:  $batch,
          phase:  $phase,
          session_id: ($t[-1].sessionId // ""),
          branch:     ($t[-1].gitBranch // ""),
          cc_version: ($t[-1].version // ""),
          from:  ($t[0].timestamp  // ""),
          until: ($t[-1].timestamp // ""),
          wallclock_s: (if ($t | length) > 0
                        then (($t[-1].timestamp | ts) - ($t[0].timestamp | ts))
                        else 0 end),
          totals:    ($t | sums),
          by_model:  ($t | bucket(.message.model // "?")),
          by_effort: ($t | bucket(.effort // "?")),
          by_skill:  ($t | bucket(.attributionSkill // "-")),
          by_plugin: ($t | bucket(.attributionPlugin // "-")),
          tools: ([$t[].message.content[]? | select(.type == "tool_use") | .name]
                  | group_by(.) | map({key: .[0], value: length}) | from_entries),
          subagent: ($sub | sums),
          skills_recommended: $recommended
        }
    ' "$tf")

    mkdir -p "$(dirname "$jsonl")"
    printf '%s\n' "$rec" >> "$jsonl"

    f="$dir/report.json"
    if [ ! -f "$f" ]; then
      jq -n --arg repo "$repo" --arg batch "$(basename "$dir")" --arg started "$(date -Iseconds)" \
            '{repo: $repo, batch: $batch, started: $started, phases: []}' >"$f"
    fi
    if [ "$kind" = "planning" ]; then
      jq --argjson t "$rec" '.planning = $t' "$f" >"$f.tmp" && mv "$f.tmp" "$f"
    else
      jq --argjson t "$rec" 'if (.phases | length) > 0 then .phases[-1].telemetry = $t else . end' \
        "$f" >"$f.tmp" && mv "$f.tmp" "$f"
    fi
    printf '%s\n' "$rec" | jq -r '"telemetry: \(.totals.turns) turns, \(.totals.output) out / \(.totals.input + .totals.cache_read + .totals.cache_creation) in"'
    ;;

  sync)
    repo="${2:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
    [ -n "$repo" ] || exit 0
    target=$("$cfq" settings get telemetrySyncRepo 2>/dev/null || true)
    case "$target" in ''|null) exit 0 ;; esac
    src="$(telemetry_log "$repo")"
    [ -f "$src" ] || exit 0
    [ -d "$target/.git" ] || { echo "cfq-telemetry.sh: $target is no git repo — sync skipped" >&2; exit 0; }

    marker="$(cfq_repo_dir "$repo")/.telemetry-synced"
    n=0; [ -f "$marker" ] && n=$(cat "$marker")
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    total=$(wc -l <"$src")
    [ "$total" -gt "$n" ] || { echo "telemetry sync: nothing new"; exit 0; }

    name="$(basename "$repo").jsonl"
    tail -n +$((n + 1)) "$src" >>"$target/$name"
    printf '%s\n' "$total" >"$marker"

    # Never fatal: a broken sync repo must not abort an implementation session.
    if git -C "$target" add -- "$name" \
       && git -C "$target" commit -q -m "telemetry: $(basename "$repo") +$((total - n))" \
       && git -C "$target" push -q; then
      echo "telemetry sync: +$((total - n)) -> $target/$name (committed, pushed)"
    else
      echo "telemetry sync: +$((total - n)) written to $target/$name, git step failed (non-fatal)" >&2
    fi
    ;;

  *)
    echo "usage: cfq-telemetry.sh record <batch-dir> planning|phase [<phase-slug>] | sync [<repo-root>]" >&2
    exit 1
    ;;
esac
