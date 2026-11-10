---
name: handoff
description: Write a session handoff file to ~/.claude/handoffs/ without stopping any running work, so a future session can resume with full context. Read back with /resume-handoff.
allowed-tools: Bash(devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/handoff_state.py *), Bash(devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/check_handoff.py *), Bash(env -u CLAUDE_CODE_SESSION_ID devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/check_handoff.py *), Bash(devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/run_report.py *)
---

# Handoff Command

Capture what happened in this session and write it to a dated file that `/resume-handoff` can load.

`/handoff` and `/resume-handoff` are one pair. They do not call or depend on any other session
command.

## Ground rules

- `/handoff` writes one handoff file plus lines appended to one history file (Step 5), and nothing
  else, apart from durable copies of live results that would otherwise sit under `~/.claude/jobs/`
  (Step 2) and the design file of Step 1 (a location the project decides, outside its
  `forbidden-writes`). The history file is appended to, never written over: the command never rewrites,
  deletes, or edits existing lines in it.
- It never stops subagents, background jobs, monitors, or scheduled tasks, and never tells them
  to stop.
- It never runs `/compact`, opens or suggests a new session, or ends the session.
- It never asks a confirmation question. After writing, it reports the path, the live work, and
  whether the user can switch sessions now (`Resume: ready` or `not ready — <reason>`); the
  current task continues in the same turn.

## When to Use

- End of a work session before closing Claude Code
- Before hitting context limits, or when a context-usage check blocks the end of a response. Write
  the file and keep working; a new session or `/compact` happens only when the user asks for it
- After solving a problem whose reasoning would be expensive to rediscover
- Any time context must survive into a future session

## Process

### Step 0: Load state and rules

Run exactly one shell command:

```
devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/handoff_state.py <cwd> --py ${user_config.python}
```

It prints toplevel, the `project key:` (the main worktree's top level for a linked worktree, the
top level for a git repository, the cwd outside git), whether the cwd is a linked worktree, the two
rules file paths, branch, upstream ahead/behind, a one-line git status summary, the
lockfile-changed flag, a `diffstat:` line, a `head:` line (the short commit id, or `none`), a
`previous handoff:` line (the most recent handoff of the same project key, or `none`; a candidate
for the `**Continues:**` header), a `history file:` line (the path of this project's history file
followed by `(exists)` or `(new)`, printed even when the file does not exist yet), and optionally a
`context-budget:` line. The status block never lists file names: the full list goes to the file
named on the `status file:` line.

When `CLAUDE_CODE_SESSION_ID` is set to a UUID, it also prints two lines after the state block, git
or not: `session id: <uuid>` and `transcript: <absolute path | none (not found)>`. The `session id:`
value goes into the `**Session:**` header (Step 5). The `transcript:` path is this session's
transcript file; Step 2 derives the subagent result paths from it. Without the variable neither line
is printed, and the header reads `**Session:** none`.

When the lean skill's state file `~/.claude/state/lean-mode.json` exists, the script also prints one
`lean-mode:` line (`on (since <UTC time>)`, `on` (when `since` is not a strict UTC time), `off`, `off (set for another project)`, or `unknown`) before the `context-budget:` line; the
resumed session reads the same file again, so the file does not record it.

Read the rules files it names, in this order, with the `Read` tool before anything else:

1. `user rules file: <path|none>`: `~/.claude/devenv.local.md`, falling back to
   `~/.claude/handoff-rules.local.md`, the user's own operating rules. It has no section restriction.
2. `rules file: <path|none>`: `<toplevel>/.claude/devenv.local.md` (then the main worktree's),
   falling back to `.claude/handoff-rules.local.md` in the same order, the project's rules.

When both names exist in one layer the script uses `devenv.local.md` and prints
`WARNING: both .claude/devenv.local.md and .claude/handoff-rules.local.md exist; using devenv.local.md`
(`~/.claude/` paths for the user layer). The project file is local: put the line
`.claude/devenv.local.md` in `.gitignore` (`/devenv:doctor init-project` creates the file and shows
the line). In `devenv.local.md`, sections that belong to other devenv parts (the cycle override ids,
`lean-limits`, `doctor-settings`) print no warning; this command does not apply them.

**Rules apply in the order core, then user, then project.** A later layer wins wherever it
differs from an earlier one; everything it does not change still follows this file. The project
layer may change only the four override points below, each as a `## <id>` section. For any other
`## ` section the script prints `WARNING: rules file section outside override points: <title>`;
that section is not applied.

| Override point | What the project layer decides |
|---|---|
| `runner-completion` | How a running runner's completion is judged (read by `/resume-handoff`) |
| `state-files` | Extra state or design files to read |
| `forbidden-writes` | Paths this command must never write |
| `values` | Item-unit name, session file size cap, short-id format |

`/resume-handoff` runs the same script with `--resume`. Whatever this step prints is what the next
session will compare against, so `Session State` is written from this output and nothing else.

### Step 1: Gather what only this session knows

Before writing, collect:

- Files modified this session (recall from the conversation; the `status:` and `diffstat:` lines
  from Step 0 give the totals; when the names are needed, Read the file named on `status file:`)
- What was attempted, what failed, and the exact reason each failure happened
- Approaches that were consciously rejected, and why they were rejected
- Claims the user made about the system that are not visible in the code
- The goal chain. When this session started with `/resume-handoff`, take `Goal (carried)` from
  that handoff's section and copy it verbatim; Read that file again if it is no longer in context.
  Do not re-summarize it. Change `Goal` or `Done-when` only when the user said to change them, and
  keep the user's quote. Copy `Constraints` verbatim too; new items may be added at the end, separated by `; `, and existing
  items stay exactly as they are. Delete
  or edit existing `Constraints` text only when the user said to, and then keep the quote and add
  a `Changes` entry that contains `Constraints:`. When the session did not start from a handoff,
  the user's own words in this session become `Goal` (quote them), `Done-when`, and `Constraints`
- Whether the requirement changed during the session, and in what order
- The unresolved `Live` items of the previous handoff (the file `**Continues:**` will name; Read it
  again if needed). Each one is copied into this handoff or closed into the history file (key = its
  id), so it never drops out of the chain silently
- Work still running in the background: call `ListAgents` to see which subagents are running now
  (skip the call when the tool is not available), then collect each subagent, runner, and scheduled
  wait (monitor, cron, wakeup): its id, what it is doing, when it was spawned and when it should
  return (`started`, `expected-by`), and where its result will land (transcript, output file, or
  log path). Record only; do not stop or restart anything
- For every open work item and every decision, three lines: **what** (the end state), **why**
  (the user's own words, quoted), **how** (the mechanism and files). A title or action list alone
  is incomplete; the next session will shrink it into a smaller task than the one decided. In the
  `Why` cell of Decisions Made and the why line of an open item, quote the user's words as they were
  said; a reason that is the agent's own judgment starts with `agent:`
- A design the user settled in conversation that is not yet in a durable project file (plan or
  design note): write it to that file first, then point the handoff at the path. Designs that live
  only in the conversation are what a handoff loses

**Which items go in the file (every section, not only live work):** leave an item out only when all
three hold: it has finished, its result is not used by any later step, and no decision depends on
it. When in doubt, include it. A closed item stays as one line with its result and evidence path
only when an open item still expects something from it.

An item that meets all three conditions is forgotten with a record: take it out of the handoff body
and append one line to the history file (Step 5). `What Did NOT Work` and `Discarded Hypotheses`
are never moved to the history file; they stay in the handoff because they still prevent retries.

Read a file before asserting its state when the session has been compacted three or more times
(for the Uncommitted field, that file is the one named on `status file:`). Degraded memory is the
main source of wrong handoffs.

### Step 2: Fill Session State from the Step 0 output

No further shell command. Map the Step 0 lines onto the fields:

| Step 0 line | Session State field |
|---|---|
| `toplevel: not a git repository` | keep Session State; write `n/a` in **Branch**, **Uncommitted changes**, **Unpushed commits**, **Lockfile changed**, and **HEAD** |
| `branch:` | **Branch** |
| `upstream: … ahead N behind M` / `upstream: none` | **Unpushed commits** (`N` · "none" · "no upstream configured") |
| `status:` + `status file:` + `diffstat:` | **Uncommitted changes**: names of the files this session changed, then the count of the rest (`status:` gives the total; the full list is in `status file:`) |
| `lockfile changed:` | **Lockfile changed** |
| `head:` | **HEAD** (`none` when it prints `none`) |

Live work comes from the conversation, not from the script, and the three Live fields are always
written, inside a git repository or not. Write `Live subagents`, `Live
runners`, and `Live scheduled` (monitor, cron, and wakeup waits that have no result path) as three
separate fields, each `none` or a list of items. Subagent and runner items have six keys; a
scheduled item has the same keys with `fires-at` in place of `result`:

| Key | Value |
|---|---|
| `name` | Subagent and runner: `<name> (<id>)`, the id in parentheses being the agent id, task id, shell id, or PID (required; the checker looks for it, and the next session finds the subagent transcript `agent-<id>.jsonl` by it). Scheduled: the name of the wait |
| `does` | What it is doing |
| `decision` | The decision it serves: a `What` value from the Decisions table, or a design file path |
| `result` | Absolute path where the result lands (subagent: its transcript `subagents/agent-<id>.jsonl` inside the folder named by the `transcript:` path without `.jsonl`, or the file it writes its result to; runner: log file) |
| `fires-at` | Scheduled only: the time or trigger at which it fires |
| `done-when` | The one observation that means it has returned (a log's last line, a marker file, a transcript stop reason) |
| `after-return` | What to do once it has returned |
| `started`, `expected-by` | Optional: spawn time and expected return time. Not checked |

`result` must be an absolute path (`/…`, `~/…`, or a Windows `C:\…`, `C:/…`, `/c/…`, `/mnt/c/…`
path), never a sentence. A `result` under `.claude/jobs/` fails the checker, because that location
is cleaned up automatically. A `result` under `/tmp/claude-` or `/private/tmp/claude-` (background
task output) fails the same way. For a finished item whose result sits there, copy the result to a
durable location (for example `~/.claude/handoffs/results/<short-id>-<name>.md`, written with Write)
and put the copy's path in `result`. For an item still running whose output will land there, put
the jobs path in `done-when` (where to watch), put the durable destination in `result`, and make
the first step of `after-return` "copy the jobs result to `result`". The next session judges live
work from those paths alone.

A subagent transcript path is built from the `transcript:` line of Step 0: the folder is that path
without `.jsonl`, and the file is `subagents/agent-<id>.jsonl` inside it. Never guess the folder. When
the line reads `none (not found)` or is absent, have the subagent write its result to an absolute
path, and record that path as `result`.

Never invent a value. Do not collect listening ports, Docker state, migration status, or CI
results; they cost network and database calls, and several of the usual commands are Linux-only.

### Step 3: Locate the folder

The handoff folder is `~/.claude/handoffs/`. Write creates the parent folder when the file is
written, so no separate folder-creation command is needed. Never write into
`~/.claude/session-data/` or any other folder owned by a plugin, and never into a path the project
layer lists under `forbidden-writes`.

### Step 4: Choose the filename

Write to `~/.claude/handoffs/YYYY-MM-DD-<short-id>-session.tmp` using today's actual date.

The short-id rules:

- Allowed: letters `a-zA-Z`, digits `0-9`, hyphens `-`, underscores `_`
- First character must be a letter, digit, or underscore
- Use 8 or more lowercase characters to avoid same-day collisions
- The project layer may replace this format through its `values` section

Valid example: `2026-09-16-a1b2c3d4-session.tmp`

Getting this wrong means `/resume-handoff` will not find the file.

### Step 5: Write the file

Use the format below. Core sections are always present; write "None" rather than deleting them,
because an honest empty section tells the next session that the question was considered.

Put the `project key:` value from Step 0 in the `**Toplevel:**` header line, right after
`**Project:**`. `/resume-handoff` uses it to pick only this project's files.

Put the handoff this session started from in the `**Continues:**` header line, right after
`**Toplevel:**`: the file `/resume-handoff` loaded at the start of this session, or `none` when the
session did not start that way. The `previous handoff:` line from Step 0 is a candidate; confirm it
against how this session actually started.

Put the `session id:` value from Step 0 in the `**Session:**` header line, right after
`**Continues:**`; write `none` when Step 0 printed no `session id:` line. The checker compares it with
this session's id.

Write `## Goal (carried)` before `## What We Are Building`, with its five keys:

- `Origin`: the absolute path of the first handoff of the chain, and its date. The first handoff of
  a chain writes its own absolute path, so every later copy stays byte-identical and still correct.
- `Goal` and `Done-when`: copy the previous file's values verbatim. Do not re-summarize them. Change
  them only when the user said to change the goal or the done condition in this session; then add
  the date, the user's quote, and `<before> → <now>` to `Changes`, and keep the earlier `Changes`
  entries and `Origin` as they are. Also record the same event in `Requirement Changes`, and have
  each of the two sections point at the other.
- `Constraints`: what was decided not to do, and what is out of scope. Copy the previous file's
  value verbatim; new items may be added at the end, separated by `; `, and existing items stay exactly as
  they are. `none` means zero items. To delete or edit existing `Constraints` items, the
  user must have said so in this session; keep the user's quote and add a `Changes` entry that
  contains `Constraints:`, the date, and `<before> → <now>`.
- `Changes`: `none` until the user changes the goal.

When the previous file has no `Goal (carried)` (it was written in an older format), build the
section from its `What We Are Building` and the user's own words, and set `Origin` to that file.

**Size cap.** The cap is the project `values` size cap, or 24 KiB when `values` sets none. When the
file would exceed it, shorten in this order and stop as soon as it fits:

1. Closed items that can move to the history file.
2. `What WORKED` rows that no open item leans on.
3. `Current State of Files` rows for files that are already committed.
4. Long quotes in the `Why` cells of Decisions Made, shortened.

Never trim these: `Goal (carried)`, `Live` items, `What Did NOT Work`, `Discarded Hypotheses`,
`Exact Next Step`, and the what, why and how of an open item.

**History file.** Append one line per forgotten item to the file named on the `history file:` line
of Step 0, in this form, and skip an item whose key is already in the history file:

```
<YYYY-MM-DD> · <key> · <what ended> · <result or evidence path>
```

If the `history file:` line ends in `(new)`, create the file with Write (the parent folder is
created with it). If it ends in `(exists)`, append with a quoted heredoc (`cat >> "<history file>" <<'EOF'`) so the existing
lines stay as they are and the shell does not expand anything in the new lines. Then add
`## Closed (pointers)` to the handoff with one line (`- <N> items closed → <history file path>
(keys: a, b, c)`). When nothing was closed, omit the section.

The four added sections (Discarded Hypotheses, User Assumptions, Requirement Changes,
Environment & Setup Notes) are conditional. Omit an added section entirely when it has no content,
since an empty one carries no signal.

If Step 0 printed a `context-budget:` line, put it as the first line of the file as
`<!-- context-budget: … -->`. If it printed none, write no such line.

**After Handoff.** `/handoff` starts the baseline: only user messages that arrive after it count.
Whenever a user message arrives after `/handoff` started, whether typed, queued while a tool ran, or
the answer to a question dialog (for a question-dialog answer, quote the chosen answers: answers to
several questions joined by ` / `, several choices for one question by `, `), append one line to the
`## After Handoff` section at the very end of the file (create the section when it is missing):

```
- 「<user message verbatim>」 → 결정: <what changed and how>
```

Write the line even when nothing changed: `→ 결정: 변경 없음`. Copy the message as typed, whole; cut one
over 300 characters to its first 259 characters, `…`, and its last 40. Write `REDACTED` only where the checker's printed line
shows `REDACTED`. If a message holds a secret the checker does not mask, never write the value:
report `not ready — unmaskable secret in a message` and tell the user. This is the only exception
to the "exits 0" rule; never run `/handoff` again to clear it (the baseline would move past that
message). Never rewrite
the body to absorb the decision: when a decision contradicts the body, this section is later and wins.
Append only; do not touch existing lines. When `/handoff` runs again (typed by the user or re-run
through the Skill tool), first add a line for every user message since the previous run that has
none, and carry every existing line over (as is, or folded into the body with its quote); the
baseline then moves.

**Before Handoff.** When `CLAUDE_CODE_SESSION_ID` is a UUID, the checker lists the user messages that
came right before the last `/handoff` input (the last 5, counted past earlier `/handoff` inputs) and
that input's arguments, at most 6. Add one line for each to `## Before Handoff`, right before
`## After Handoff` (last in the file when there is no such section):

```
- 「<the user's own sentence>」 → 반영: <section title>
- 「<the user's own sentence>」 → 무관
```

Copy the lines the checker shows. A message that opens with a quote of the assistant's output (a
`'…'` span, a `「…」` span, or lines opening with `>`) is written from the user's own sentence on
when that sentence is 40 characters or more (a shorter one keeps the quote); copying the quote along
on one line also passes. A message of several lines goes on one line. A line quotes the message
whole; one over 300 characters is its first 259 characters, `…`, and its last 40. Everything after the last
`→ ` is the disposition. `반영: <section title>` names a `## ` section of this file exactly as it is
titled (not Before Handoff or After Handoff). `무관` means the message changed nothing in this file;
a few words may follow it. A message that gave an instruction or a decision is never `무관`: record
it in Decisions Made, Goal (carried), or Exact Next Step, and name that section. When `/handoff`
runs again, write the section again from the checker's lines for the new `/handoff` input.

**Reviews.** When the checker finds review runs in this session's transcript, `## Reviews` (right
after `## Decisions Made`) holds one row per run, in launch order:

```
- <agent id | runId> · <type> · <final verdict | running> · <adopted or rejected, one line>
```

A review run is an Agent whose `subagent_type`, after its last `:`, contains `review`, `critic`, or
`judge` or equals `architect` or `verifier`, and every Workflow run. The id is the `agentId` or
`runId` of the run's result. The checker shows each missing row as
`- <id> · <type> · <verdict> · <채택/기각>`: replace the last two cells. The verdict is the run's
last return, and `running` when it has not returned by the time `/handoff` runs. Never write a
transcript path in a row. An agent's transcript is `subagents/agent-<id>.jsonl` and a Workflow's is
`subagents/workflows/<runId>/`, both under the folder of the `transcript:` path without `.jsonl`.
Rows of earlier sessions are not carried over: keep their conclusions in Decisions Made or
Discarded Hypotheses.

**Next-step order.** When a numbered step of `## Exact Next Step` is a 🔴 user step (the user acts
first), end every later numbered step with `(독립)` (or `(independent)`) when it can start now, or
with `(← n)` (or `(<- n)`) naming the earlier step n it waits for.

### Step 6: Check, report and continue

Run `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/check_handoff.py <file> --git yes|no --py ${user_config.python}` (`no` only when Step 0
printed `toplevel: not a git repository`). It exits 1 and lists every open item that lacks the
what/why/how lines or a design-file path, every Decisions row with an empty `What`, `Why`, or `How`
cell, and every break of the resume contract (a missing or relative `**Toplevel:**` line, Session
State without its eight fields (the seven earlier ones and `**HEAD:**`; the five git fields may be
`n/a` outside git), a live item missing one of
its six keys, a `name` without its `(<id>)`, or a `result` that is not an absolute path, a missing
or placeholder-only `Exact Next Step`), and every break of the goal chain: a `Goal (carried)`
without its five keys, a `Goal`, `Done-when`, or `Origin` that differs from the `**Continues:**`
file without a matching `Changes` entry, a `Constraints` whose items do not start with all of the previous items unchanged
(new items are added at the end, separated by `; `) and has no `Changes` entry that contains `Constraints:`, a `**Continues:**` that is
neither `none` nor an existing absolute path, a `Closed (pointers)` path that does not exist or
lacks a listed key, and a Live item of the previous handoff that is neither carried nor closed
(`Live item <name> from the previous handoff is missing`). An open item that fails ends with
`(missing: what, why, how)`, naming the lines it lacks: write it as
`- … — what: … · why: "…" · how: …` (or give a design-file path). A missing Live item is fixed by
keeping an item with that id in a Live field, or by closing it with one `Closed (pointers)` line such as
`- 1 items closed → <history file> (keys: <id>)`. It also fails on a secret value in a
provider-specific format: a token, or a private key block (from the `BEGIN … PRIVATE KEY` line to
its `END` line, or a run of three or more long base64 lines). It prints the kind and the line or
lines and never prints the value. The fix is to replace the token, or the whole key block, with
`REDACTED` (no brackets); replacing only the header line still fails. These failures are printed
before every other failure. The
`Goal (carried)` comparison does not count that `REDACTED` replacement as a change. Fix those
items and run it again; do not report until it exits 0 (the one exception: an unmaskable secret in
a user message, Step 5). The checker also prints
`WARNING: handoff file is <N> KiB (over 24 KiB)` for a large file, after the result block
(`[PASS]` or `[FAIL]` and its list); the exit code does not change. Shorten the file as Step 5
describes. Keep the never-trim list; a file that still exceeds the cap after that is left as it
is. The checker's limit is fixed at 24 KiB. The project `values` size cap is used only for the
Step 5 shortening order, and the checker never reads it.
`/resume-handoff` runs the same checker on load, so a file that passes here is a file the next
session can resume with one command.

When `CLAUDE_CODE_SESSION_ID` is a UUID, the checker also reads this session's transcript, and these
failures can appear. `contract-session` fires when the `**Session:**` line is missing or is not
this session's id (message `**Session:** must be this session's id <id>`). `after-handoff` fires
when a user message after the last `/handoff` is not quoted in `## After Handoff` (a message under 40
characters must open its own `- 「` line; a longer one counts when its whole quote, or for one over 300
characters its first 259 characters and its last 40, appears inside one line): it exits 1 and
prints each missing message verbatim, at most 5 lines. Fix it by appending the missing lines to
`## After Handoff` (`→ 결정:` as in Step 5); do not rewrite the body.
`before-handoff` fires when a message before the last `/handoff`, or an argument of it, is not in
`## Before Handoff` with a valid disposition (Step 5). `reviews` fires when a review run of this
session has no valid row in `## Reviews`: a row copied as the checker shows it still fails until the
verdict and the last cell are filled. `contract-next-step-deps` fires when a numbered step after the
first 🔴 step of `## Exact Next Step` lacks `(독립)` or `(← n)`. When the transcript cannot be
read, it prints `WARNING: after-handoff messages not checked: <reason>` and
`WARNING: reviews not checked: <reason>` after the result block and the exit code does not change.
A review launch whose result carries no `agentId` (`runId` for a Workflow) prints the same reviews
WARNING; the runs that carry one are still checked. To run the checker by hand from another session on an older file, drop
the session variable so the writing-side checks are skipped:
`env -u CLAUDE_CODE_SESSION_ID devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/check_handoff.py <file> --git yes|no --py ${user_config.python}`
(on Windows, clear the variable first with `set CLAUDE_CODE_SESSION_ID=`).

Both scripts append one JSON line per run to `~/.claude/handoffs/.state/runs.jsonl` (the handoff
file's path, never its body). When the user later asks what happened, read that file together with
the handoff file it names. Run `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/run_report.py --py ${user_config.python}` only when the user asks what
happened: it summarizes that file.

Report three lines, without printing the file or asking anything:

```
Handoff saved to [resolved path]
Live work: [n] subagent(s), [m] runner(s), [k] scheduled, not yet finished
Resume: ready — run `/clear`, then `/resume-handoff <short-id>`
```

The third line is the user's cue to switch sessions. Write `ready` only when all of these hold;
otherwise write `not ready — <which condition failed>`, fix it, and run `/handoff` again (never to
clear an after-handoff failure: append the lines instead):

- The checker above exited 0.
- Every live item in the file names where its result lands (an absolute path that is not under
  `~/.claude/jobs/`) or, for a scheduled item, when it fires, so the next session can judge it
  without this one. In-process subagents and background jobs survive `/clear`; their transcripts
  continue under the new session's directory.
- Nothing decided or designed in this conversation is missing from the file or from a durable
  file the handoff points at.
- Every user message after the last `/handoff` is quoted in `## After Handoff` (the checker tests
  this).
- Every message and argument the checker lists for `## Before Handoff` is there with its
  disposition (the checker tests this).
- Every review run of this session has a row in `## Reviews` (the checker tests this).
- No write to the handoff file is still pending (a lane whose result you are about to fold in
  within the next minute: fold it in first, then report).

`/handoff` never runs `/clear` itself. The user decides when to switch.

The final reply (the three-line report and the rest of that reply) restates the file only and never
states a plan, a decision, or a next step that differs from it. A plan or decision the file lacks
goes into its body section first (Edit), then the checker runs again; `## After Handoff` holds user
messages only.

Then continue the task that was in progress. A user message that arrives after this report gets one
line in `## After Handoff` (Edit, append only), and the checker runs again. If the user later points
out a mistake in the file, append its line to `## After Handoff` with the correction after
`→ 결정:`; Edit the body only for a wrong fact (path, id, command), never Write the whole file, and
run the checker again.

## Safety rules

- Record environment variable names only, never their values.
- Do not paste diffs into the file. Reference `path:L10-L45` and say what changed and why.
- If an error message or command contains a credential, redact it with `REDACTED` (no brackets) and
  say that it was redacted. The Step 6 checker scans the file for provider-specific secret formats
  mechanically and fails on a match.
- The file lives in the home directory, never inside the repository.

---

## Session File Format

```markdown
<!-- context-budget: … -->   (optional first line: only when Step 0 printed a context-budget: line)
# Session: YYYY-MM-DD

**Started:** [approximate time if known]
**Last Updated:** [current time]
**Project:** [project name or path]
**Toplevel:** [the `project key:` value from Step 0, an absolute path]
**Continues:** [absolute path of the handoff this session started from | none]
**Session:** [the `session id:` value from Step 0 | none]
**Topic:** [one-line summary of what this session was about]

---

## Confidence

[high | medium | low]

- `high`: 0 or 1 compactions, nothing important inferred
- `medium`: exactly 2 compactions, or some context reconstructed rather than recalled
- `low`: 3 or more compactions. The next session verifies against the code before acting

---

## Session State

[Outside a git repository, write `n/a` in the five git fields (Branch, HEAD, Uncommitted changes,
Unpushed commits, Lockfile changed); the three Live fields stay required.]

- **Branch:** [branch name]
- **HEAD:** [short commit id | none]
- **Uncommitted changes:** [names of files this session changed · count of the rest | "none"]
- **Unpushed commits:** [list | "none" | "no upstream configured"]
- **Lockfile changed:** [yes, install before running anything | no]
- **Live subagents:** none | list of items, each:
  - name: [<name> (<id>)]
    does: [what it is doing]
    decision: [a `What` value from Decisions Made, or a design file path]
    result: [absolute path]
    done-when: [observation that means it has returned]
    after-return: [what to do then]
    started: [optional]
    expected-by: [optional]
- **Live runners:** none | list of items, same keys (`name`: `<runner name> (<id>)`, `result`: log path)
- **Live scheduled:** none | list of items, same keys with `fires-at` in place of `result`

---

## Goal (carried)

- **Origin:** [absolute path of the first handoff of the chain] · [YYYY-MM-DD]
- **Goal:** [the original goal; on the first handoff, include the user's own words quoted]
- **Done-when:** [the done condition; several conditions separated by `; `]
- **Constraints:** [what was decided not to do, and what is out of scope; `none` when there is nothing; several separated by `; `]
- **Changes:** [none | YYYY-MM-DD "[user's quote]" : [before → now]; several entries joined by `| `]

---

## What We Are Building

[1-3 paragraphs. Enough that someone with zero memory of this session understands the goal:
what it does, why it is needed, how it fits the larger system.]

---

## What WORKED (with evidence)

[Only confirmed items. Each needs the evidence that confirms it. Without evidence, move the item
to "What Has NOT Been Tried Yet" instead.]

- **[thing that works]** — confirmed by: [test name, HTTP status, observed output]

If nothing is confirmed: "Nothing confirmed working yet."

---

## What Did NOT Work (and why)

[Technical failures. The exact reason, so the next session does not retry them.
"Threw X because Y" is useful. "Did not work" is not.]

- **[approach]** — failed because: [exact error or reason]

If nothing failed: "No failed approaches yet."

---

## Discarded Hypotheses

[Omit when empty. Approaches rejected by choice rather than by failure. These are different from
the section above: nothing broke, the direction was reasoned away. Record the reasoning, or it
will be proposed again next session.]

- **[approach]** — rejected because: [reasoning]

---

## What Has NOT Been Tried Yet

[Promising directions nobody attempted. Specific enough to act on. Each item that was decided
with the user carries its three lines: what (end state), why (the user's words), how (mechanism
and files), or a path to the design file that holds them.]

- [approach] — what: [end state] · why: "[user's words]" · how: [mechanism, files]

If nothing is queued: "No specific untried approaches identified."

---

## Current State of Files

| File | Status | Notes |
| --- | --- | --- |
| `path/to/file.ts` | Complete | [what it does] |
| `path/to/file.ts` | In progress | [what is done, what is left] |
| `path/to/file.ts` | Broken | [what is wrong] |
| `path/to/file.ts` | Not started | [planned, untouched] |

Use concrete paths, never globs. Add `:L10-L45` when a specific range matters.

If no files were touched: "No files modified this session."

---

## Decisions Made

[Choices and their reasons, so the next session does not relitigate them. Every row has all three
cells filled; `How` may be a design file path.]

| What | Why | How |
|---|---|---|
| [the end state decided] | [why this over the alternatives, the user's words quoted] | [mechanism and files, or a design file path] |

If none: "No major decisions made this session."

---

## Reviews

[Optional. Omit when the checker found no review run in this session. One row per review run, in
launch order; the verdict is the run's last return, `running` while it has not returned.]

- [agent id | runId] · [type] · [final verdict | running] · [adopted or rejected, one line]

---

## User Assumptions

[Omit when empty. Statements the user made about the system that are not verifiable in the code.
Mark every one, because the next session will otherwise treat them as established fact.]

- [statement] `[user-stated — not in code]`

---

## Requirement Changes

[Omit when the scope never moved. List every state in order, not just the first and last.]

- State 1: [initial requirement]
- State 2: [what changed and why]
- State N (current): [where it stands now]

---

## Blockers & Open Questions

[Unresolved items the next session must address. Questions raised and not answered.
External dependencies being waited on.]

- [blocker or question]

If none: "No active blockers."

---

## Closed (pointers)

[Optional. Omit when nothing was closed.]

- [N] items closed → [absolute path of the history file] (keys: [a, b, c])

---

## Exact Next Step

[The single most important thing to do on resuming, precise enough that no thinking is needed
to start. If any live item exists, step 1 is to judge it as `/resume-handoff` Step 3 describes
(returned, running, or lost) before the rest. If genuinely unknown, say so and point at the sections that
decide the direction. When a numbered step is a 🔴 user step, every later numbered step ends with
`(독립)` or `(← n)`, as in:
1. 🔴 [what the user must do]
2. [a step that waits for step 1] (← 1)
3. [a step that does not wait] (독립)]

---

## Environment & Setup Notes

[Omit unless the project needs something non-obvious: commands to run it, services that must be
up, environment variable names that must be set. Names only, never values.]

---

## Before Handoff

[Optional. Omit when the checker lists no message. Right before `## After Handoff`, or last when that
section is absent. One line per message the user gave right before `/handoff` and per argument of
`/handoff`, as the checker shows it:]

- 「[the user's own sentence]」 → 반영: [the title of a `## ` section of this file]
- 「[the user's own sentence]」 → 무관

---

## After Handoff

[Optional, always the last section. User messages only: omit it when no user message arrived after
`/handoff` started. One line per message, no message skipped:]

- 「[user message verbatim]」 → 결정: [what changed and how | 변경 없음]
```

---

## Notes

- Every session gets its own handoff file. Never append to a previous handoff file. Running
  `/handoff` again in the same session rewrites that session's file as a whole. The history file is
  the one exception: it only grows by appended lines.
- "What Did NOT Work" and "Discarded Hypotheses" are the sections that pay for this command.
  Everything else can be rediscovered by reading the code; those two cannot.
- "Exact Next Step" is written as an instruction on purpose. `/resume-handoff` briefs the user and
  waits before acting unless it is run with the continue argument (`--c` or `계속`), so a direct next
  step does not cause the next session to run off on its own. Whether to continue is decided by
  that argument alone, never by anything written in this file.
- Read the file back with `/resume-handoff`. It reads the whole file, so every section carries over.
