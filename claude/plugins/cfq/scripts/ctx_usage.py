#!/usr/bin/env python3
# Usage: ctx_usage.py              # post-phase check
#        ctx_usage.py gate <SIZE>  # pre-phase gate; SIZE kept for call-site compatibility with an
#                                   # existing consumer, no longer affects the decision
"""Gate policy: three verdicts, five reasons, three independent off switches.
  - START (gate) / OK (post-phase), reason=none: nothing fired, continue normally.
  - WARN, reason=fiveHour|sevenDay|unknown: advisory only -- the caller decides whether to
    continue. Rate limits (stopFiveHourPct/stopSevenDayPct against the statusline payload's
    five-hour/seven-day usage percentages) and an unresolved context reading (no statusline
    payload, so `used` is empty and nothing is known either way) are budget/visibility concerns,
    not blockers -- continuing works fine.
  - HANDOFF (gate) / STOP (post-phase), reason=capacity: stopUsed against the absolute
    context-token count (input + cache_read + cache_creation). The window fills up whether or
    not the tokens were cheap, so `used` is never weighted by cache share. This is the one real
    blocker -- continuing genuinely does not work.
Precedence: rate limit is checked first, since it is the constraint no later session can work
around -- the context window resets on /clear, the five-hour budget does not -- but a later
capacity hit always wins the reason and escalates the verdict to HANDOFF/STOP: a full window
must never be downgraded to a warning just because a rate-limit reason happened to be evaluated
first. Both reasons still appear in the note when that happens.
The cache share (fresh/creation/read split) is display only and never enters a verdict --
current_usage is a snapshot of the last API call, so the ratio swings after a /clear or a cache
miss without anything being wrong; it is shown so a human can weigh whether attaching another
phase to a warm session is worth it.
Runtime discovery (statusline payload, transcript parsing) lives in cfq_runtime.py; this script
only consumes its `context` subcommand's `used`/`cache`/`rateLimits` fields.
Used by implement-for-queue after each finished phase, and in `gate` mode before a phase starts.

Ported from ctx-usage.sh -- a port, not a redesign: the CLI contract (verbs, argument order, text
output, exit codes) is the invariant this file preserves.
"""

import json
import pathlib
import subprocess
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CFQ_BIN = SCRIPT_DIR.parent / "bin" / "cfq"


def cfq_run(*args):
    return subprocess.run([str(CFQ_BIN), *args], capture_output=True, text=True)


def resolved_setting(key, default):
    value = cfq_run("settings", "get", key).stdout.strip()
    if value == "-1":
        return -1
    if value.isdigit():
        return int(value)
    return default


def main(argv):
    stop_used = resolved_setting("stopUsed", 100000)
    stop_5h = resolved_setting("stopFiveHourPct", 70)
    stop_7d = resolved_setting("stopSevenDayPct", 95)

    gate = bool(argv) and argv[0] == "gate"
    size = ""
    if gate:
        size = argv[1] if len(argv) > 1 and argv[1] in ("S", "M", "L") else "M"

    # All three switches off: no reason can ever fire, nothing to resolve.
    if stop_used == -1 and stop_5h == -1 and stop_7d == -1:
        if gate:
            print(f"USED=? SIZE={size} LIMIT=-1 START REASON=none (all stop reasons disabled)")
        else:
            print("USED=? OK REASON=none (all stop reasons disabled)")
        return

    ctx = json.loads(cfq_run("runtime", "context").stdout or "{}")
    used = ctx.get("used")
    note = ctx.get("note")
    if note is None:
        note = "null"
    cache = ctx.get("cache") or {}
    cache_read = cache.get("read")
    rate_limits = ctx.get("rateLimits") or {}
    five_hour_pct = rate_limits.get("fiveHourPct")
    seven_day_pct = rate_limits.get("sevenDayPct")

    # Cache share and rate-limit percentages are appended to the note as display info regardless
    # of the decision below; the note must stay parenthesis-free (cfq_ifq_preflight.py's capture
    # is greedy to the final `)`).
    if cache_read is not None and used is not None and used > 0:
        note = f"{note}, cache {cache_read * 100 // used}%"
    if five_hour_pct is not None:
        note = f"{note}, 5h {five_hour_pct}%"
    if seven_day_pct is not None:
        note = f"{note}, 7d {seven_day_pct}%"

    reason = "none"
    rate_note = ""
    cap_note = ""

    # 1. Rate limit -- checked first; a rate-limit WARN is emitted regardless of the stopUsed=0
    #    bypass below, since that bypass only suppresses the capacity reason.
    if five_hour_pct is not None and stop_5h != -1 and five_hour_pct >= stop_5h:
        reason = "fiveHour"
        rate_note = f"5h {five_hour_pct}% >= {stop_5h}"
    elif seven_day_pct is not None and stop_7d != -1 and seven_day_pct >= stop_7d:
        reason = "sevenDay"
        rate_note = f"7d {seven_day_pct}% >= {stop_7d}"

    # 2. stopUsed=0, gate mode only: bypasses only the capacity reason below, never the
    #    rate-limit WARN.
    bypass_capacity = gate and stop_used == 0

    if used is None:
        if reason == "none":
            reason = "unknown"
        if rate_note:
            note = f"{note}, {rate_note}"
        if gate:
            verdict = "START" if reason == "none" else "WARN"
            print(f"USED=? SIZE={size} LIMIT={stop_used} {verdict} REASON={reason} ({note})")
        else:
            verdict = "OK" if reason == "none" else "WARN"
            print(f"USED=? {verdict} REASON={reason} ({note})")
        return

    # 3. Capacity -- always evaluated, even when a rate-limit reason already fired above: a full
    #    window is a real blocker and must not be downgraded to a warning by a rate-limit hit
    #    that happened to be evaluated first.
    if not bypass_capacity and stop_used != -1 and used >= stop_used:
        reason = "capacity"
        cap_note = f"used {used} >= {stop_used}"

    if rate_note:
        note = f"{note}, {rate_note}"
    if cap_note:
        note = f"{note}, {cap_note}"

    if gate:
        verdict = {"none": "START", "capacity": "HANDOFF"}.get(reason, "WARN")
        print(f"USED={used} SIZE={size} LIMIT={stop_used} {verdict} REASON={reason} ({note})")
    else:
        verdict = {"none": "OK", "capacity": "STOP"}.get(reason, "WARN")
        print(f"USED={used} {verdict} REASON={reason} ({note})")


if __name__ == "__main__":
    main(sys.argv[1:])
