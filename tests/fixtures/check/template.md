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
