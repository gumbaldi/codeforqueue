---
name: code-for-queue
description: >
  Dashboard and settings for the code-for-queue workflow: show every parked queue across all
  repositories — what is still open, how much is done — manage the current repository's queue,
  and change cfq's configuration. Use for "/cfq", "/code-for-queue", "show my queues", "queue
  status", "cfq settings".
argument-hint: <settings|repo>
---

# Code-for-Queue: Dashboard, Settings, First-Time Setup

Always answer in the user's language.

## Output Format

Plugin root: `${CLAUDE_PLUGIN_ROOT}` — every `<plugin-root>/…` path in a reference file below
resolves against it.

Status lines, not prose — read `${CLAUDE_PLUGIN_ROOT}/references/output-format.md` and follow it.

## Section Map

| Section | Step | Label | Example detail |
|---|---|---|---|
| PRECHECKS | A | `Setup` | `➖ already done` / `⚠️ first run · 2 questions follow` |
| PRECHECKS | B | `Dash` | `2 repos · 1 with open work` |
| PRECHECKS | B | `Plugins` | `✅ mattpocock-skills and ponytail installed · classic grill and audit on` |

## Step 0 — Aggregate

One call, reused by Steps A through D — never re-derive any of the following by hand:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cfq" dash
```

`status: "NO_REPO"` means no repo anywhere has a queue yet — say so, Steps C/D still work once
this repo has one. `RUNTIME_DEGRADED` → surface `.runtimeDiagnostic` verbatim, treat both plugins
as not installed for this run. `MULTIPLE_IN_PROGRESS` → this repo has more than one batch
locked/in-progress at once, an invariant violation — surface it, never silently pick one.

## Plugin Status Line (Step A)

`cfq dash render` (Step B) computes this identically inside the script — this section is Step A's
own copy, needed there because Step A's offer flow reacts to `.plugins` directly rather than
printing the rendered block.

From `.plugins`: `.mattpocock`/`.ponytail` (installed), `.useMattpocockGrilling`/
`.usePonytailAudit` (the switches), and `.ponytailMode` (`off`/`lite`/`full`/`ultra`/`unknown`) —
computed once by the aggregator, no separate call.

- Neither installed → `➖ mattpocock-skills/ponytail not installed`.
- Both installed, at least one switch off → `➖ installed · <off list>`, naming only the switch(es)
  that are actually off (`grill: classic off`, `audit off`).
- Both installed, both on → `✅ mattpocock-skills and ponytail installed · classic grill and audit
  on`.
- One installed, one missing → name the missing one and the installed one's switch state, e.g.
  `➖ ponytail not installed · classic grill on`.
- Ponytail installed and `.ponytailMode` is not `off` → append `· mode: <mode> · cfq expects off`
  and force the icon to `⚠️`, regardless of which of the four cases above applies — cfq expects
  ponytail dormant outside the maintenance audit. `.ponytailMode == "off"` → append `· mode: off`
  with no icon change, no warning.

## Step A — First-Time Setup (only if `setupDone` is `false`)

Print the `PRECHECKS` header on entering this step.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cfq" settings state get setupDone
```

If it hasn't run yet, clarify two things **before** anything else — each its own
`AskUserQuestion`:

1. *Grill procedure*: step-by-step (one question per round, recommended) vs. classic (all
   questions of a round at once, saves context — needs `mattpocock-skills`, falls back to
   step-by-step without it). Write the result to `grillMode`.
2. *Optional third-party plugins*: for each plugin `.plugins` reports not installed, **offer** it,
   never install it unasked — what it does, what cfq uses it for, install command and docs are in
   `${CLAUDE_PLUGIN_ROOT}/references/dashboard.md`. Agreement → hand the user the `/plugin` command
   to run and set the matching switch to `true`. Decline → the switch stays `false`; cfq must work
   fully without either plugin, no path may run into a dead end without them.
3. *Ponytail dormant by default* — only when `.plugins.ponytail` is `true` and `.plugins.ponytailMode`
   is not `off`: offer to set `~/.config/ponytail/config.json`'s `defaultMode` to `off`, copy in
   `${CLAUDE_PLUGIN_ROOT}/references/dashboard.md`. Agreement → merge the key into the existing
   config (create the file/directory if absent, never overwrite unrelated keys). Decline → write
   nothing; don't ask again this session. This question runs once, here — a session where ponytail
   gets installed or its mode changes after `setupDone` is already `true` relies on
   `cfq doctor check`'s advisory line (which already carries the fix command) instead of a second
   interactive ask.

Afterward set `setupDone` to `true`. Both switches stay changeable later via Step C or the env
vars, even with a plugin installed. Print the `Setup` status line — `➖ already done` when this
step didn't run at all.

## Step B — Dashboard (default behavior with no argument)

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/cfq" dash render
```

Print its output exactly as returned — the `PRECHECKS` header, `Dash`/`Plugins` status lines,
`QUEUES`, `THIS REPO · <name>` (when applicable, open batches only, plus the expanded next batch),
the copyable `cd`/`/model`/`/ifq` sequence, and `CONFIG · <name>` are all already rendered. No
reformatting, no rebuilding a table from `.repos`/
`.thisRepo`/`.settings` by hand — this is the same aggregation Step 0 fetches as JSON, formatted by
the script instead of the model. (The bare `/cfq` slash command already prints this block via its
own injection before the model runs at all; this step exists for every other way the skill gets
invoked — natural language, or as part of Step A's flow.)

The dashboard never executes `todo/` `check:` commands — that stays Step C's job, on request.

## Step C — Management (on request, always confirm before writing)

Six actions, exclusively in the current repository: flag/unflag priority, delete a batch, archive
a batch, clean the registry, set/remove a dependency, work off `todo/` entries. Each follows the
same shared flow — a deterministic check (already available from `.repos`/`.thisRepo`) → present →
confirm → mutate → report under an `ACTION` header. Full per-action detail in
`${CLAUDE_PLUGIN_ROOT}/references/dashboard.md`.

## Step D — Settings

Change requests go through `bin/cfq settings set [--repo <path>] <key> <value>` (or `unset`) — read
each key's value/source straight from `.settings` (Step 0's call), never re-list. Scope inference,
the global-only rejection, and the `env:repo-legacy` migration note are in
`${CLAUDE_PLUGIN_ROOT}/references/dashboard.md`. After a change, print one status line:
`✅ Setting  maintenanceEvery: 50 → 40 (global)`, or `⚠️ Setting  stopUsed set, but
CFQ_STOP_USED overrides` when an env var shadows the key.
