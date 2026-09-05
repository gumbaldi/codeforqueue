#!/usr/bin/env bash
# Gate policy: three verdicts, five reasons, three independent off switches.
#   - START (gate) / OK (post-phase), reason=none: nothing fired, continue normally.
#   - WARN, reason=fiveHour|sevenDay|unknown: advisory only -- the caller decides whether to
#     continue. Rate limits (stopFiveHourPct/stopSevenDayPct against the statusline payload's
#     five-hour/seven-day usage percentages) and an unresolved context reading (no statusline
#     payload, so `used` is empty and nothing is known either way) are budget/visibility concerns,
#     not blockers -- continuing works fine.
#   - HANDOFF (gate) / STOP (post-phase), reason=capacity: stopUsed against the absolute
#     context-token count (input + cache_read + cache_creation). The window fills up whether or
#     not the tokens were cheap, so `used` is never weighted by cache share. This is the one real
#     blocker -- continuing genuinely does not work.
# Precedence: rate limit is checked first, since it is the constraint no later session can work
# around -- the context window resets on /clear, the five-hour budget does not -- but a later
# capacity hit always wins the reason and escalates the verdict to HANDOFF/STOP: a full window
# must never be downgraded to a warning just because a rate-limit reason happened to be evaluated
# first. Both reasons still appear in the note when that happens.
# The cache share (fresh/creation/read split) is display only and never enters a verdict --
# current_usage is a snapshot of the last API call, so the ratio swings after a /clear or a cache
# miss without anything being wrong; it is shown so a human can weigh whether attaching another
# phase to a warm session is worth it.
# Runtime discovery (statusline payload, transcript parsing) lives in cfq-runtime.sh; this script
# only consumes its `context` subcommand's `used`/`cache`/`rateLimits` fields.
# Used by implement-for-queue after each finished phase, and in `gate` mode before a phase starts.
# Usage: ctx-usage.sh              # post-phase check
#        ctx-usage.sh gate <SIZE>  # pre-phase gate; SIZE kept for call-site compatibility with an
#                                  # existing consumer, no longer affects the decision
set -u
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cfq="$script_dir/../bin/cfq"

stop_used=$("$cfq" settings get stopUsed 2>/dev/null || echo 100000)
case "$stop_used" in
  -1) ;;
  ''|*[!0-9]*) stop_used=100000 ;;
esac

stop_5h=$("$cfq" settings get stopFiveHourPct 2>/dev/null || echo 70)
case "$stop_5h" in
  -1) ;;
  ''|*[!0-9]*) stop_5h=70 ;;
esac

stop_7d=$("$cfq" settings get stopSevenDayPct 2>/dev/null || echo 95)
case "$stop_7d" in
  -1) ;;
  ''|*[!0-9]*) stop_7d=95 ;;
esac

cmd="${1:-}"; size=""
if [ "$cmd" = "gate" ]; then
  case "${2:-}" in S|M|L) size="${2}" ;; *) size="M" ;; esac
fi

# All three switches off: no reason can ever fire, nothing to resolve.
if [ "$stop_used" = "-1" ] && [ "$stop_5h" = "-1" ] && [ "$stop_7d" = "-1" ]; then
  if [ "$cmd" = "gate" ]; then
    echo "USED=? SIZE=$size LIMIT=-1 START REASON=none (all stop reasons disabled)"
  else
    echo "USED=? OK REASON=none (all stop reasons disabled)"
  fi
  exit 0
fi

ctx=$("$cfq" runtime context)
used=$(jq -r '.used // empty' <<<"$ctx")
note=$(jq -r '.note' <<<"$ctx")
cache_read=$(jq -r '.cache.read // empty' <<<"$ctx")
five_hour_pct=$(jq -r '.rateLimits.fiveHourPct // empty' <<<"$ctx")
seven_day_pct=$(jq -r '.rateLimits.sevenDayPct // empty' <<<"$ctx")

# Cache share and rate-limit percentages are appended to the note as display info regardless of
# the decision below; the note must stay parenthesis-free (cfq-ifq-preflight.sh's capture is
# greedy to the final `)`).
if [ -n "$cache_read" ] && [ -n "$used" ] && [ "$used" -gt 0 ] 2>/dev/null; then
  note="$note, cache $((cache_read * 100 / used))%"
fi
[ -n "$five_hour_pct" ] && note="$note, 5h ${five_hour_pct}%"
[ -n "$seven_day_pct" ] && note="$note, 7d ${seven_day_pct}%"

reason="none"
rate_note="" cap_note=""

# 1. Rate limit -- checked first; a rate-limit WARN is emitted regardless of the stopUsed=0
#    bypass below, since that bypass only suppresses the capacity reason.
if [ -n "$five_hour_pct" ] && [ "$stop_5h" != "-1" ] && [ "$five_hour_pct" -ge "$stop_5h" ] 2>/dev/null; then
  reason=fiveHour; rate_note="5h ${five_hour_pct}% >= $stop_5h"
elif [ -n "$seven_day_pct" ] && [ "$stop_7d" != "-1" ] && [ "$seven_day_pct" -ge "$stop_7d" ] 2>/dev/null; then
  reason=sevenDay; rate_note="7d ${seven_day_pct}% >= $stop_7d"
fi

# 2. stopUsed=0, gate mode only: bypasses only the capacity reason below, never the rate-limit WARN.
bypass_capacity=""
[ "$cmd" = "gate" ] && [ "$stop_used" -eq 0 ] 2>/dev/null && bypass_capacity=1

if [ -z "$used" ]; then
  [ "$reason" = "none" ] && reason=unknown
  [ -n "$rate_note" ] && note="$note, $rate_note"
  if [ "$cmd" = "gate" ]; then
    case "$reason" in none) verdict=START ;; *) verdict=WARN ;; esac
    echo "USED=? SIZE=$size LIMIT=$stop_used $verdict REASON=$reason ($note)"
  else
    case "$reason" in none) verdict=OK ;; *) verdict=WARN ;; esac
    echo "USED=? $verdict REASON=$reason ($note)"
  fi
  exit 0
fi

# 3. Capacity -- always evaluated, even when a rate-limit reason already fired above: a full
#    window is a real blocker and must not be downgraded to a warning by a rate-limit hit that
#    happened to be evaluated first.
if [ -z "$bypass_capacity" ] && [ "$stop_used" != "-1" ] && [ "$used" -ge "$stop_used" ] 2>/dev/null; then
  reason=capacity; cap_note="used $used >= $stop_used"
fi

[ -n "$rate_note" ] && note="$note, $rate_note"
[ -n "$cap_note" ] && note="$note, $cap_note"

if [ "$cmd" = "gate" ]; then
  case "$reason" in
    none) verdict=START ;;
    capacity) verdict=HANDOFF ;;
    *) verdict=WARN ;;
  esac
  echo "USED=${used:-?} SIZE=$size LIMIT=$stop_used $verdict REASON=$reason ($note)"
  exit 0
fi

case "$reason" in
  none) verdict=OK ;;
  capacity) verdict=STOP ;;
  *) verdict=WARN ;;
esac
echo "USED=${used:-?} $verdict REASON=$reason ($note)"
