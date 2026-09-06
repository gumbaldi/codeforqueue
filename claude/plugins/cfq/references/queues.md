# Batch Briefing and Queue Entry Formats

## Batch Selection Rules (Step 3)

`bin/cfq preflight-impl`'s `selection` object carries `blocked`, `planning`, `inProgress`, and
`multipleInProgress` — the filters and stop conditions Step 3 applies before any picker runs.
The preflight itself already excludes blocked/planning/other-in-progress batches from
`selection.selectable`; this section only covers the wording each case needs.

**Blocked** batches (`selection.blocked`, each `{name, dependsOn, unknownDeps}`) are never offered.
If `status` is `BLOCKED`, print the wait list (batch → waiting on `dependsOn`) and end — never fall
back to a blocked one. `unknownDeps` (an unresolvable `.dependsOn` name) is shown at selection time
with `⚠️` and the unresolvable name but doesn't block (`/cfq` fixes it) — one sentence, no more.

**Planning** — a batch `/pfq` is still writing (`.planning` marker not yet cleared by its lint
step) — is never offered either, separately from the `dependsOn` wait list: for every name in
`selection.planning`, "Batch `<name>` is still being planned — try again once `/pfq` finishes."
One line per such batch, no more.

**In-progress invariant.** `status: "MULTIPLE_IN_PROGRESS"` (`selection.multipleInProgress`
non-empty) means more than one batch violates the one-in-progress-batch-per-repo invariant: **stop
immediately**, touch nothing, name every batch in the list, and say this must be resolved via
`/cfq` Step C (archive or reprioritize one) before `/ifq` can proceed — never silently pick one,
never fall through to the picker. A batch that is both blocked and in-progress is excluded from
this check by the blocked filter (it doesn't reach `selection.selectable`/`.inProgress` at all) and
surfaces only through the wait-list path — auto-resuming it would restart work whose dependency
reappeared after the batch was started, so it waits like any other blocked batch.

## Batch Briefing (Step 4)

`batch.briefText` in the preflight result already holds `bin/cfq brief`'s output for the resolved
batch — batch name, priority, phase count, `.dependsOn` if present, then one line per phase (number
and title, size in brackets, context excerpt). Present it as-is, compactly: no prose around it, no
repetition of the plan, no commentary on the phases. A phase file without `## Size` counts as `M`;
one without `## Context` shows its title alone — an incomplete plan is worth showing, not worth
aborting over.

## Branch and Changelog on Go-Ahead (Step 4)

The preflight's `branch` field (already computed pre-mutation — see **Resume Snapshot** above) is
the same JSON `bin/cfq branch plan "<repo-root>" "<batch>"` itself returns: `batch`/`batchNumber`
echo the batch's own stable identity (`batchNumber` is `null` for a legacy unnumbered batch);
`mode` says what to do. Always use the `branch` field directly for checkout — never reconstruct a
branch string from a number/slug:

`bin/cfq branch plan` is remote-aware: it fetches `origin` once (best-effort — no `origin`, or the
fetch fails offline/sandboxed, and everything falls back to local-only behavior unchanged) before
deciding, so a stale local `main`/branch never gets silently proposed as a base. On the `new` path,
`origin/*` is the source of truth for `candidates` — each is an object (`name`, `ref`,
`aheadOfMain`, `behindRemote`, `aheadRemote`, `localOnly`, `highestBatch`, `mergedIntoOriginMain`,
`lastCommit`), ranked by `lastCommit` descending, and `base`/`baseRef` already name the
recommended one (`base: "main"` / `baseRef` pointing at `origin/main` when `candidates` is empty —
never `null` waiting on a question). Two additive top-level fields ride along with every response:
`remoteChecked` (bool) and `remoteWarning` (string or `null`, set only when local `main` — on the
`new` path — or the persisted branch — on `continue` — has commits `origin` doesn't and that gap
can't be auto-resolved; it no longer affects candidate selection, it is purely a heads-up).

- **`off`** (`branchPerBatch` is `false`) → `Branch: ➖ branchPerBatch off`, skip everything below.
- **`continue`** (a branch for this batch already exists) → `git checkout "<branch>"`, don't write
  a changelog entry (the batch is already recorded). Local purely behind its own
  `origin/<branch>` is fast-forwarded automatically before checkout. `remoteWarning` non-null here
  (local ahead of/diverged from `origin/<branch>`) doesn't block — `git checkout "<branch>"` still
  runs, but the `Branch` status line surfaces the warning as a `⚠️` note.
- **`new`** → a genuinely empty `candidates` list resolves silently to `base`/`baseRef` (`main`/
  `origin/main`), no question. Otherwise one `AskUserQuestion` listing every entry in `candidates`
  (already ranked), asking which one the new branch builds on. Recommended (first, labelled
  `(Recommended)`): `base` — the newest by `lastCommit`, never the checked-out branch. Each other
  option's description names its `aheadOfMain`, plus `local only` / `already contained in
  origin/main` / `behind origin by <behindRemote>` where applicable. The free-text answer
  (`AskUserQuestion`'s built-in "Other") is resolved with `bin/cfq branch check "<repo-root>"
  "<name>"`: `UNRESOLVED` → ask once more naming the unresolvable input; a second miss ends the
  session without touching anything, exactly like the dirty-tree rule below. On `OK`, surface both
  its warnings separately when they apply — "`<name>` is `<behind>` commit(s) behind
  `origin/<name>`" and "`<newerCandidate.name>` has a newer commit (`<newerCandidate.lastCommit>`)"
  — before the checkout runs, and use its `ref` as `<baseRef>` and `<name>` as `<base>` below. Then:

```bash
git checkout -b "<branch>" "<baseRef>"
"<plugin-root>/bin/cfq" changelog init "<repo-root>" "<branch>" "<base>" "<batch>"
"<plugin-root>/bin/cfq" branch plan "<repo-root>" "<batch>"
```

`changelog init` keeps receiving `<base>` (the plain branch name), not `<baseRef>` — the changelog
records which branch the work builds on, not which ref was used to cut it.

The `new`-mode `bin/cfq branch plan` re-run above is the one and only place this batch's mutation
step calls it directly — solely to reconfirm the branch now exists post-checkout; `continue`/`off`
never call it again, the preflight's answer already stands. A dirty working tree at this point is
an error, not something to work around: report it and end without touching anything.

## Batch Allocation Errors (`cfq batch allocate` — pfq Step 14)

`BATCH_LEDGER_MISMATCH` means a numbered queue directory exists with no matching ledger entry.
`allocate` cannot itself produce this state — it reserves the ledger entry before creating the
directory and never rolls the reservation back on a later failure, so the recoverable half (a
ledger entry with no directory) is the only one `allocate` can ever leave behind. The error's
`action` field already names the resolved `changelogFile` path and the repair, computed, never
hardcoded:

```bash
"<plugin-root>/bin/cfq" batch reconcile "<repo-root>"          # read-only, exits non-zero on a gap
"<plugin-root>/bin/cfq" batch reconcile "<repo-root>" --fix    # reserves every orphaned directory
```

`reconcile` never deletes anything and never touches a ledger entry with no directory — a reserved
number whose batch never got parked is a legitimate abandoned reservation, not a gap to close.

## Phase Announcement and Go Gate (Step 7)

Runs after the size gate, before any code is written, every phase — the fine-grained counterpart
to Step 4's one coarse per-batch go-ahead. The announcement is
`bin/cfq brief "<batch-dir>" --phase <NN>`'s output, rendered as returned, no rewording —
deterministic, extracted from the phase file, so it cannot drift in wording between phases:

```
PHASE 02 · ifq-per-phase-go-gate · Size L
  Goal     <first two non-empty lines of ## Context>
  Files    bin/cfq, implement-for-queue/SKILL.md, queues.md, test-settings.sh
  Check    <first command line from ## Verification>
```

Then one `AskUserQuestion`, two options: **Go** — "proceed, implement this phase now" — and
**Cancel** — "release the lock and end the session, nothing touched". `Cancel` runs
`bin/cfq lock release "<repo-root>"`, reports "cancelled before implementation, nothing touched",
and ends; it never leaves the lock held. `Go` proceeds straight to Step 8.

**`WARN` variant.** When `contextGate.verdict` is `WARN`, one warning line precedes the
announcement, naming the reason and the concrete numbers from `contextGate.note` in the user's
language — e.g.:

```
⚠️ Five-hour budget at 89% (threshold 70%) — this is a warning, not a blocker; the phase runs
normally if you start it.

PHASE 02 · ifq-per-phase-go-gate · Size L
  Goal     <first two non-empty lines of ## Context>
  Files    bin/cfq, implement-for-queue/SKILL.md, queues.md, test-settings.sh
  Check    <first command line from ## Verification>
```

The `AskUserQuestion` then carries a third option: **Go** — "proceed, implement this phase now",
its description naming the budget state so the user sees what they are accepting — and **must
not** claim the attempt will fail. **Handoff** — "end the session cleanly instead of implementing",
reusing Step 10's `STOP` sequence (telemetry sync, lock release, the `HANDOFF ·
implement-for-queue` short report) — this is the option that used to be forced on the user; it is
now the one they choose. **Cancel** stays as above. No option may be phrased as futile — the
observed bug produced a choice between "start anyway, but it will hand off immediately without
implementing" and "cancel"; every option offered here must actually do what it says. This same
warning line is reused verbatim at Step 4, above the batch briefing — one wording, two call
sites, never two drifting variants.

## Phase Summary (Step 8)

Printed after Step 8, before the `bin/cfq report append` call:

```
PHASE 02 DONE
✅ Implemented     <one clause: what was built>
✅ Verification    <command, and its result>
⚠️ Deviation       <one line per deviation>
```

`Implemented` and `Verification` are the model's own prose — no script can know what actually
happened. `Deviation` repeats, verbatim, whatever goes into that phase's `report.json`
`deviations` entry: one source, two renderings, never worded differently for the two audiences. No
deviations → the `Deviation` line is omitted entirely, not printed empty.

## Stop Rule (Step 8, before the next phase)

A gate, not a status line — checked after a phase goes green, before auto-advancing to the next
open phase in the same session. Exactly four triggers, nothing else:

- (a) files were changed that `## Affected Files` does not list — checked mechanically, never by
  judgement: `git diff --name-only HEAD~1..HEAD` against that phase's `## Affected Files`.
- (b) verification came back red, or was not run
- (c) a change the plan specifies was deliberately left out
- (d) a new dependency or a new script was introduced that the plan does not name

Any of the four firing → do not auto-advance; state which trigger fired and ask once
(`AskUserQuestion`) whether to continue anyway. None firing → continue as today, no question.
Everything that is not one of the four is a note in the report, never a stop — say so explicitly so
a future reader does not add a fifth trigger by interpretation.

## Phase Commit Trailers (Step 9)

Write the human-written subject/body (plus `Co-Authored-By`, as before) to a temp message file,
then run it through:

```bash
"<plugin-root>/bin/cfq" changelog commit-message "<repo-root>" "<batch>" "<phase-slug>" green "<message-file>"
```

Commit with `git commit -F -` on its output. For a numbered batch (`batchNumber` from Step 4's
`Branch`/`Resume` data is non-null) this appends `CFQ-Batch-Number`, `CFQ-Batch`, `CFQ-Phase`,
`CFQ-Phase-Status` to the existing trailer block via `git interpret-trailers`, leaving the
human-written subject/body untouched. A legacy (unnumbered) batch passes the message through
unchanged — never invent a `CFQ-Batch-Number` for one. Claude never hand-writes or hand-formats a
`CFQ-*` trailer.

`<phase-slug>` is the full phase slug — the same value the phase's `report.json` entry carries in
its `phase` field and the same one Step 5 hands to `report set-commit` — one identifier per phase,
in the trailer, the report and the lookup alike.

## Batch-Done Report Fields (Step 11)

`bin/cfq finish`'s one JSON object, rendered field by field:

- `Language`: `.lang.issues` is the structural count (`missing`/`stray`/`unfiled`); judge
  `.lang.prose.sample` for prose, comments, identifiers and commit messages not in `codeLanguage` —
  any language, never hardcode one to look for. `i18nExcludePatterns` keeps locale/translation
  resources out of the sample by default; a line that lands anyway (custom naming the patterns
  miss) from an evident translation resource isn't a `codeLanguage` violation — expected
  multi-language content, not a policy breach. `.lang.prose.truncated: true` means the sample is
  exactly that, a sample, so the status line says so (`⚠️ 2 issues · sampled`); an empty sample (no
  git repo, unknown ref) is `➖`, not a finding. Either source finding → `⚠️` with the combined
  count, details as `   └ ` lines; nothing found → `✅ no issues`. No repair here — every finding
  becomes a `todo/` entry per **Follow-Up** below.
- `Maintenance` from `.maintenance`: `➖ off` · `➖ not due (<n> commits)` · `⚠️ due (<n> commits) ·
  run /pfq`.
- `Security Diff` from `.security.new` — the difference only, no repeat of the overall count, no
  new planning, no automatic fix; an empty `.security` block (older batch, no planning snapshot)
  → skip without comment.
- `Changelog` from `.changelog` as-is.
- `Telemetry` from `.telemetry`, `Lock` from `.lock`.
- Any `.errors` entries → `⚠️` lines naming the failed step; the sequence still completed.

## Closing Report Fields (Step 12, full format)

`RESULT · implement-for-queue` header, one label/value line per field:

- `Batch` — batch and repo, phases total, green/red split.
- `Cost` — run `"<plugin-root>/bin/cfq" report summary "<batch-dir>"` (same call
  `report-for-queue` already uses for its table) and render fields 9/7/8/10/11 as turns, output
  tokens total (planning's share named separately), models, efforts.
- `Skills` — recommended vs. used, query in **Skills Recommended vs. Used** below.
- `Security` — the difference only, one line.
- `Merge` — current branch, commits ahead of `main`, a ready-to-run command as an indented
  `   └ ` line, printed not run; also a `todo/` entry per **Follow-Up** below without asking, so a
  forgotten merge is never lost.
- `Report` — `file://` path, only when Step 11 rendered one, else the line is omitted.

## Skills Recommended vs. Used (Step 12)

```bash
jq -c '{recommended: [.phases[].telemetry.skills_recommended // []] | flatten | unique,
        used:        [.phases[].telemetry.by_skill // {} | keys[]] | unique | map(select(. != "-"))}' \
  "<batch-dir>/report.json"
```

## Plan Entry (`plan/<YYYY-MM-DD>-<slug>.md`)

H1 title, then:

- `## Finding` — what was noticed
- `## Location` — files and locations, absolute paths
- `## Why Not Here` — why it's out of scope for the current phase
- `## Origin` — batch and phase it came from

## Follow-Up (`todo/<YYYY-MM-DD>-<slug>.md`)

H1 title, one or two sentences describing what to do, optionally a `check: <shell-command>` line
(exit `0` means done). For the merge case: `check: git branch --merged main | grep -q <branch>`.
Plus `## Origin`, same as above.

Both formats: filename `<YYYY-MM-DD>-<slug>.md`. The headings are always English; only the prose
inside them follows `codeLanguage`.

## Resume Snapshot (Step 3 preflight)

`bin/cfq preflight-impl`'s `resume` field carries what `bin/cfq resume` itself returns for the
resolved batch, minus its own `branch` sub-object (redundant with the preflight's top-level
`branch`, computed by that same underlying call — see the `resume` noun's own script header
comment for the full, still-current field-by-field contract: `phasesOpen`/`phasesDone`, `lastCommit`/
`lastCommitSource`, `deviations`, `redPhases`, `batchContext.exists`/`.path`). One JSON object,
deterministic — no summarization, only facts read from disk, `report.json`, and git. The script
itself is unchanged; only who calls it and what's kept from its output moved.

## Research and Verification Delegation (Step 8)

Two, and only two, places in Step 8 may run on an Explore subagent instead of the implementing
session's own model — never a blanket "delegate whatever seems slow". Model choice is a rule, not
a mood — cheap model to locate, expensive to judge; read
`<plugin-root>/references/explore-escalation.md` and follow it. Both keys
(`implExploreModel`/`.implExploreModelComplex`) come from the preflight's `policy` object, no
`bin/cfq settings get` call here:

- **Pre-implementation research.** Before writing any code, a phase that touches several files or
  whose scope isn't fully clear from the phase file alone may send an Explore subagent to locate
  the relevant code, existing patterns, and callers, and return a distilled summary. This mirrors
  `plan-for-queue` Step 5 exactly — same reasoning, same escalation rule. A narrow, single-file
  phase needs no subagent; reading the phase file is enough.
- **Verification output filtering, green case only.** Running the phase's verification command
  (tests, build, lint) can itself be delegated to an `implExploreModel` subagent when the raw output
  would be long — it runs the command and reports back pass/fail plus which command ran, nothing
  more. This is a locate-shaped task (pass/fail, which command), so it always stays on the cheap
  model even where the pre-implementation research above escalated.

**Never delegate a red result.** The moment verification fails, the subagent (if one was used for
that run) returns the complete, unfiltered failure output — full stack trace, full diff, everything
— because the implementing model needs the whole picture to fix the bug. A red phase is exactly the
case where summarizing loses the detail that matters; this is the one asymmetry in the rule, not an
oversight. If verification wasn't delegated at all, this doesn't apply — the failure is already in
context.

**Never delegate implementation, test writing, or documentation.** Writing or editing any file
under the phase's scope always happens in the implementing session itself — a subagent producing
code the parent must then read back in full to verify or commit is strictly more expensive than the
parent writing it directly. See the plugin `CLAUDE.md`'s "Subagents are for exploration and
mechanical test execution" section for the full reasoning and the measurement anyone changing this
should re-run first.

## Reusing a Warm Explore Agent (Step 8)

Only applies when a second phase runs in the same session, i.e. when `onePhasePerSession` is
`false`. Under the default (`true`) this never applies and nothing below happens.

When the next phase's `## Affected Files` overlaps the files an Explore agent of this session has
already read, continue that agent with `SendMessage` instead of starting a new one — its context
still holds those files. Continue it only if nothing has changed underneath it:

```bash
git diff --name-only <sha-when-the-agent-started>..HEAD
```

If that output intersects the files the agent read, its knowledge is stale — start a fresh agent.
One `git` call decides this; do not reason about what an agent "probably still knows".

This never applies to implementation, test writing or documentation, which stay off subagents
entirely.
