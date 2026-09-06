---
name: implement-for-queue
description: >
  Work off parked phase plans from the repo-local queue (<repo>/.claude/cfq/) one
  batch per session, phase by phase, stopping when the context window gets too full. Use for
  "/ifq", "/implement-for-queue", "implement the queue", "work off the plans", "continue with
  the plans", "next phase".
argument-hint: <batch or phase>
---

# Implement-for-Queue: Work Off a Batch Phase by Phase

Always answer in the user's language.

## Output Format

Plugin root: `${CLAUDE_PLUGIN_ROOT}` — every `<plugin-root>/…` path in a reference file below
resolves against it.

Status lines, not prose — read `${CLAUDE_PLUGIN_ROOT}/references/output-format.md` and follow it.

## Section Map

| Section | Steps | Contents |
|---|---|---|
| PRECHECKS | 1-7 | Preflight, Model Check, Batch, Size Gate |
| IMPLEMENTATION | 8-9 | per phase: announcement, result, Commit |
| POSTCHECKS | 10-12 | Context Check, Telemetry, Lock, Batch Done |

Not strictly sequential: a `WARN` at Step 10 loops back to Step 5 for the next phase instead of
opening `POSTCHECKS`. `POSTCHECKS` opens only on a `STOP`, a red phase, or a finished batch.

## Step 1 — Arguments

Text passed with the invocation narrows batch selection in Step 3 — it never replaces the briefing, the go-ahead, or the per-phase Go question.

## Step 2 — Plan-Mode Gate

Before Step 3, check for Plan Mode — read
`${CLAUDE_PLUGIN_ROOT}/references/interaction-policy.md`'s **Plan-Mode Gate** section and follow it.

## Step 3 — Preflight: Policy, Batch Selection

Print the `PRECHECKS` header, then one call:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cfq" preflight-impl "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```
`status: "NO_REPO"` → abort, report, end. Otherwise this one call already resolved Steps 1
(model gate), 2 (plugin gate) and 3a (batch selection) together — read its fields below, no
further calls needed for those three steps.

**Model Gate.** The running model's name is in your system prompt's environment block;
`policy.allowAnyModel: true` → skip this check, otherwise it must match one of
`policy.implModels` (substring match: `sonnet` matches `claude-sonnet-5`). No match → **stop
immediately**, touch nothing, report the allowed models, that `/model <x>` then `/ifq` is the way
forward, and that `CFQ_IMPL_MODELS`/`cfq` changes the list. Print the `Model Gate` status line
either way.

**Plugin Boundaries.** `policy.implBlockedPlugins` — those plugins/skills aren't called for the
rest of the session, not even indirectly — per-phase skill recommendations on this list are
ignored. Print the `Plugin Boundaries` status line.

**Batch Selection.** `status` already reflects the filtered outcome — `NO_BATCH` → report "No open
plans for this repo in the queue.", end. `BLOCKED`/`MULTIPLE_IN_PROGRESS`/`selection.planning`
entries each need specific wording (wait list, stop-immediately rule, the still-planning notice) —
cold-path detail: read `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Batch Selection Rules** section on first use each
session and apply it here.

`selection.inProgress` non-null → that batch was auto-selected already (`batch`/`nextPhase`/
`branch`/`resume`/`contextGate` are already resolved for it) — skip the `AskUserQuestion`, print
`Batch` as `resumed <name> · <done>/<done+open> phases done` (prefix `high · ` if flagged),
straight to Step 4. `selection.inProgress` null and `selection.selectable` has **exactly one**
entry → same pre-resolved fields, no question either — print `Batch` noting it was the only
selectable batch (e.g. `2026-08-18-example · only open batch · 3 phases`), straight to Step 4.
`selection.selectable` has **zero** entries and `status` isn't `NO_BATCH`/`BLOCKED` → treat as
`NO_BATCH`. **More than one** → `batch`/`nextPhase`/`branch`/`resume`/`contextGate` are `null`; ask
one `AskUserQuestion`, "There are N open plans for this repo. How do you want to proceed?": **Work
through them in order** (show `selection.selectable`, already sorted flagged-first-then-name) or
**Choose a specific plan** (a second `AskUserQuestion`, batches as options, label = topic slug,
description = open phase count + date, prefixed with `high · ` only when flagged). Set the chosen
batch (or the first, for "in order"), re-run the preflight call with `--select <chosen>` to resolve
its fields — **do not acquire the lock yet**. Print the `Batch` status line once chosen; both
questions stay prose. **Never two batches in the same session**, not even once the first finishes
and context is still free — different plans belong in separate context windows.

## Step 4 — Batch Briefing and Go-Ahead

Nothing is touched, no lock taken, until the user has seen what the batch contains — never read
phase files in full here, that's Step 8's job. `contextGate.verdict` is `WARN` → print one warning
line *above* the briefing, naming the reason in the user's language and the concrete numbers from
`contextGate.note` (e.g. the five-hour budget is at 89% against a 70% threshold); state plainly
that this is a budget warning, not a blocker, and that the phase runs normally if started — wording
per `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Phase Announcement and Go Gate**. Present
`batch.briefText` compactly (already the full per-phase listing — name/priority/phase
count/`dependsOn`/one line per phase with size and context excerpt), then ask exactly one
`AskUserQuestion`, "Start implementing this batch?" — no extra question for the warning, it only
adds a line above the existing one:
- **Start** → acquire the repo lock (`bin/cfq lock acquire "<repo-root>" "<batch>"`). Exit ≠ 0 (`LOCKED`) →
  **end immediately**, touch nothing, name holder/batch/time, note the 30-minute stale takeover;
  `TAKEOVER` → proceed, `Lock` carries that warning; else `Lock` is just acquired. `branch.mode`
  (from the preflight — already computed, no new call) decides the checkout — full behavior (`off`/`continue`/`new`,
  base-branch question, checkout, changelog init, post-checkout reconfirm) — based on `origin`'s
  current state — in `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Branch and Changelog on Go-Ahead**. `Branch` renders whichever
  happened. `resume` (same preflight
  result) already carries done/open phases, last commit, deviations, red-phase history,
  `.batch-context.md`'s path — no new `bin/cfq resume` call; if `resume.batchContext.exists`, `Read`
  it now. Print `Resume` — phases done/open, `.batch-context.md` present or not. Then Step 5.
- **A different batch** → back to Step 3's question with the remaining batches; the declined one
  isn't offered again. Nothing left → report and end. Not offered at all when Step 3 didn't run a
  picker (auto-resumed in-progress batch, or only one selectable batch) — offering one here would
  restart a different batch mid-work; the go-ahead then has only **Start** / **Cancel**.
- **Cancel** → report "aborted, nothing touched" and end. No lock was ever held.

## Step 5 — Earlier Failed Attempt

Entering this step closes `PRECHECKS` and opens `IMPLEMENTATION`. Before reading the phase file,
two checks — both already resolved by Step 3's preflight call for the phase it was run against;
**only re-run the preflight here** (same `--select <batch>`) if
a phase other than the one it resolved is about to start (e.g. the second and later phases of a
batch, since the preflight only ever resolves `nextPhase` for the lowest-numbered open phase at
call time). `nextPhase.failedAttempt` — `.found: true` → read its
`.note`/`.at`, check whether the cause still holds before repeating, and mention it in the new
entry ("second attempt after …"); `.found: false` → skip silently. Print `Failed Attempt` either
way.

## Step 6 — Size Gate

`contextGate` — deterministic projection, already computed by the
preflight from the phase's `## Size` heading, never prose arithmetic. `contextGate.verdict`, three
branches:

- `START` → Step 8, as normal.
- `WARN` → **Step 7, same as `START`** — the phase is not blocked. The warning carries into Step 7's
  Go question as a third option; nothing is skipped and nothing ends here.
- `HANDOFF` → no phase ran, hand off cleanly (Step 10) instead.

Print the `Size Gate` status line as `USED=<contextGate.used|?> SIZE=<contextGate.size>
LIMIT=<contextGate.limit> <contextGate.verdict> <contextGate.reason> (<contextGate.note>)`, icon
`✅` for `START`, `⚠️` for `WARN`, `❌` for `HANDOFF` — `contextGate.reason` names which threshold
fired structurally, the report repeats that token rather than a paraphrase of the note; this closes
`PRECHECKS`.

## Step 7 — Phase Announcement and Go Gate

Print the phase announcement —
`"${CLAUDE_PLUGIN_ROOT}/bin/cfq" brief "<batch-dir>" --phase <NN>`, rendered as returned, no
rewording. `contextGate.verdict` was `WARN` → the announcement is followed by the same warning line
as Step 4, and the `AskUserQuestion` gains a third option: **Go** (proceed to Step 8, description names
the budget state, never claims the attempt will fail) / **Handoff** (end the session cleanly
instead of implementing — Step 10's `STOP` sequence: telemetry sync, lock release, short handoff
report) / **Cancel** (release the lock, end the session, nothing touched). Otherwise just **Go** /
**Cancel** as before. The warning re-appears at every phase because Step 7 runs per phase — intended,
not a repetition bug: nothing advances automatically while the budget is over threshold. Rendering
example and option copy in `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Phase Announcement
and Go Gate**.

## Step 8 — Implementation

Read the lowest-numbered open `NN-*.md` in full — multi-file or
unclear-scope phases may delegate that research to an `implExploreModel` subagent first;
implementation itself never runs on one. Implement it completely, run the plan's verification with
output filtered — a green run may delegate the filtering to the same subagent, a red run never does
(full unfiltered failure back either way), per `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Research and Verification
Delegation**. A phase touching `docs/<codeLanguage>/…` →
write the counterparts in every `docLanguages` entry before it goes green, per
`${CLAUDE_PLUGIN_ROOT}/references/doc-style.md` or `<repo>/docs/STYLE.md` if present. Work found
beyond this phase's scope → one `AskUserQuestion` on parking it: yes writes
`plan/<YYYY-MM-DD>-<slug>.md` per `${CLAUDE_PLUGIN_ROOT}/references/queues.md`, no stays a sentence in the report, no
second attempt. Green → move the file to `<batch>/done/` (`mkdir -p` first), register the repo
(`bin/cfq registry add "<repo-root>"`), print the **Summary** (`${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Phase
Summary** — its `Deviation` lines double as this call's `deviations` array, one source, two
renderings); red → **stop**, print `❌ red` with each trimmed error as `   └ ` lines, the file stays
open, don't move on. Record the phase either way, before anything else, with
`"${CLAUDE_PLUGIN_ROOT}/bin/cfq" report append "<batch-dir>" '<phase-json>'` — this captures
telemetry automatically. `phase` carries the full phase slug — the plan file's name without `.md`,
e.g. `02-gate-rate-limits-and-cache-display`, never the bare number. It is the same value Step 5
passes to `report set-commit` and to `changelog commit-message`, and `report append` rejects
anything else. `deviations` is not optional padding — name what the plan said, what was
built, and why; an honest empty array is fine, a glossed-over deviation is not. On red, `errors`
carries the actual failure output, trimmed to what identifies it.

**Stop rule**, before the next phase in the same session: (a) files beyond `## Affected Files`, (b)
verification red or skipped, (c) a planned change omitted, (d) an unnamed new dependency/script —
mechanics in `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Stop Rule**. Any firing → ask once before continuing; none
→ continue as today, no question.

## Step 9 — Commit & Push (on green, every phase)

Automatically, right after moving the file to `done/`, even if more phases follow — never
collected until batch end or a `/clear`. The branch already exists (Step 4 created it or checked
an existing one out) — commit, message in `codeLanguage`, composed via `bin/cfq changelog
commit-message` (`${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Phase Commit Trailers**), and push: `-u origin
<branch>` on this session's first push, a plain `git push` after that. Then backfill the commit
hash via
`git rev-parse HEAD` and `bin/cfq report set-commit "<batch-dir>" "<phase-slug>" "<sha>"` — without
it, `bin/cfq resume`'s commit fields stay empty for every phase from here on. Print the `Commit`
status line — branch and commits pushed.

## Step 10 — Context Check After Every Phase

Run `"${CLAUDE_PLUGIN_ROOT}/bin/cfq" ctx`, now returning `OK` / `WARN` / `STOP`.
`policy.onePhasePerSession` (Step 3's preflight, no new call) `true` → treat exactly like `STOP`
below, regardless of the context gate's own verdict; `false` → the context gate alone decides.

- `STOP` → print `POSTCHECKS` (this closes `IMPLEMENTATION`), sync telemetry and release the lock
  (`bin/cfq telemetry sync "<repo-root>"`, `bin/cfq lock release "<repo-root>"`), printing
  `Telemetry`/`Lock`, then end — the follow-up session acquires the lock fresh, a half-finished
  batch must not stay locked. Print the `HANDOFF · implement-for-queue` short format from Step 12.
- `OK` → next phase, same batch.
- `WARN` → **do not end, do not advance silently.** Go to Step 5 for the next phase (re-running the
  preflight with `--select <batch>` as Step 5 already requires for any phase past the first), so
  the announcement and the Go gate run. The gate there resolves `WARN` again and Step 7 carries the
  warning and its three options. If there is no next open phase, Step 11 (Batch Done) runs
  normally — a finished batch is not held back by a budget warning. An unresolvable context reading
  arrives as `WARN REASON=unknown` and follows this same path — the user decides, rather than the
  session ending on a missing measurement.

`stopUsed: 0` is deliberate, not a misconfiguration — `STOP` fires after every phase for the
capacity reason, one context window each. A rate limit produces a `WARN`, which never overrides a
capacity `STOP` and never ends a session on its own — the old assumption that a rate-limit stop
wins over the `stopUsed: 0` bypass no longer holds. `stopUsed: -1` is equally deliberate — `STOP`
never fires **for the capacity reason**; the rate-limit reason has its own switches.
`stopFiveHourPct: -1` and `stopSevenDayPct: -1` are each just as deliberate — warns for nothing for
that reason either; a payload without `rate_limits` (API-level billing) means the check simply
doesn't apply, which isn't worth a comment. `onePhasePerSession: true` (the default) is the finer
of two gates: the batch-level Step 4 go-ahead is coarse, this and Step 7's per-phase Go question
are fine — together nothing is ever implemented without an explicit confirmation naming what's
about to change; it outranks `WARN` — with one-phase-per-session on, the session ends after a phase
either way, and the budget warning changes nothing.

## Step 11 — Batch Done

No open `NN-*.md` left → print the `POSTCHECKS` header (this closes `IMPLEMENTATION`), then run
`"${CLAUDE_PLUGIN_ROOT}/bin/cfq" finish "<repo-root>" "<batch-dir>" "<branch>"`, which moves
the batch into `impl/done/`, registers the repo, runs the language/maintenance/security/changelog/
telemetry sequence and releases the lock unconditionally (a `trap`, so a mid-sequence failure can
never leave the repo locked), and prints one JSON object. Render its fields — `Language`/
`Maintenance`/`Security Diff`/`Changelog`/`Telemetry`/`Lock`/`.errors` — field-by-field detail in
`${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Batch-Done Report Fields**.

Render the HTML report only when `htmlReport` is `true` (`bin/cfq report html
"<repo-root>/.claude/cfq/impl/done/<batch>"`), printing `Report` as `rendered`; else `➖ off ·
/rfq renders on demand` and no `file://` line in Step 12. Hand the batch to Step 12 for the closing report.

## Step 12 — Closing Reports

One format, two lengths, both end the session, both a label/value list under the `Output Format`
padding rule — `RESULT · implement-for-queue` (full) or `HANDOFF · implement-for-queue` (short).
**Full format** — `RESULT · implement-for-queue` header, fields `Batch`/`Cost`/`Skills`/
`Security`/`Merge`/`Report` — field-by-field detail (which script call, what each renders, the
`todo/` entry for a forgotten merge) in `${CLAUDE_PLUGIN_ROOT}/references/queues.md`'s **Closing Report Fields**.

**Short format** — `HANDOFF · implement-for-queue` header, three to four lines: phases done, phases open, the `USED`
value, `/clear` → `/ifq`. No cost breakdown, no merge hint. **Red case:** still the full format,
naming the red phase; its `❌` line already appeared in `IMPLEMENTATION` (Step 8), so Step 12 only
repeats the `5 green, 1 red` split in `Batch`, not the error text.
