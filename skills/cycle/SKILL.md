---
name: cycle
description: Run the default work cycle on a task - one planner run, one critic lane, a revision with no re-review, execution, then one adversarial verify pass that fixes only H findings. Roles map to agent type, model and effort; rules layers may override. User-invoked only.
argument-hint: "<task>"
disable-model-invocation: true
---

# Default Work Cycle

The light default way to carry a task (`$ARGUMENTS`) from plan to verified change. The session that
runs this skill is the main session: it plans the order of work, writes the orders, spawns the roles,
reads their returns and judges. The roles do the planning, review and editing.

## Steps

1. **PLAN** - one planner run. The plan names the goal, the acceptance criteria, how each criterion is
   verified (a command, a file or an observation), and the work units with the files each one touches.
2. **CRITIC** - one critic lane reviews the plan and returns findings in the finding format below.
3. **Revise** - the main session revises the plan for the H findings. There is no re-review after the
   revision. The user approves the revised plan (the default approval point) before any execution.
4. **EXECUTE** - one executor per work unit, tests first: the executor writes or names the test that
   specifies the behavior, sees it fail for the right reason, then implements to green.
5. **VERIFY** - one adversarial verify pass over the whole change, whose job is to break it. Only H
   findings become fix work units. M and L findings are recorded and asked about once, together, at
   the end.

## Roles

Engine defaults. Every agent call names model and effort explicitly; none inherits the session's.

| Role | Agent type | Model | Effort | Writes |
|---|---|---|---|---|
| planner | `general-purpose` | `opus` | `high` | the plan file only |
| critic | `general-purpose` | `opus` | `high` | nothing, or the report file the order names |
| executor | `general-purpose` | `sonnet` | `high` | the files of its work unit |
| verifier | `general-purpose` | `opus` | `high` | nothing, or the report file the order names |

A rules layer may replace the agent type, model or effort of any role: `plan-lane` for the planner,
`critic-lanes` for the critic, `grading` for the executor and verifier lanes. A user layer that keeps
its own agent definitions names them there.

## Finding format

Every critic and verifier finding is `{severity, claim, evidence, location}`:

- `severity` is H (blocks the next step), M or L.
- `claim` is one sentence that can be checked.
- `evidence` is the command that was run and its output, or the quoted line and its call path.
- `location` is `path:line` or the plan section.

An H without evidence counts as M and is not fixed. The report keeps it in an `Unsupported H` list,
word for word. The main session applies this when it reads the verdict. A security or data-loss H is
placed at the top of the report, even when it is downgraded.

## Role return rules

Every order carries these rules; the engine default text is below.

All roles:
- The last message is the deliverable.
- Never end on a message without content.
- Stay within the line limit the order sets.
- Do not run anything in the background; return as soon as the work is done.

Critic and verifier:
- Line 2 is `H n · M n · L n`.
- Every finding has the four fields.
- A return carries at most 10 findings.
- When the order names a report file, write the details only to that file and return a summary of 20 lines or fewer with its path. Create or change no other file.

Executor:
- Return the list of changed files and each verification command with its result.

The return gate hook limits the length of returns only while lean mode is on, and never checks their
format; these rules cover both in every project.

## Override points

Rules apply in the order engine, then user, then project; a later layer wins wherever it differs. The
user layer is `~/.claude/devenv.local.md` and has no section restriction. The project layer is
`<toplevel>/.claude/devenv.local.md` (then the main worktree's) and may change only the points below,
each as a `## <id>` section. Read both files with the `Read` tool before PLAN. Any other `## `
section is reported as `WARNING: rules file section outside override points: <title>` and is not
applied. The older file name `handoff-rules.local.md` carries the handoff points only; cycle sections
there are not applied.

`/devenv:doctor init-project` creates the project file with every point as a sample heading and shows
the line that keeps it out of git: put `.claude/devenv.local.md` in `.gitignore`.

| Id | What it sets | Engine default |
|---|---|---|
| `plan-lane` | which planner runs PLAN: the planner role or an adapter from `adapters` | the planner role, one run |
| `approval-points` | where the user approves | plan approval only |
| `critic-lanes` | number of critic lanes, their model and effort | 1 lane, `opus`, `high` |
| `external-critic` | a critic outside this tool and the command that must pass before calling it | off |
| `judge-conditions` | when a second, independent verify lane runs | never |
| `review-rounds` | rounds per review target, the blocking severity, handling of M and L | 1 round, H blocks, M and L asked once at the end |
| `grading` | risk grades, the VERIFY lanes per grade, when EXECUTE moves to a stronger model | one grade, one verifier lane, no move |
| `ledger` | a work ledger and its commands | none |
| `dispatch-check` | a command that checks an order before it is dispatched | none |
| `priority-queue` | the values of the priority column in the remaining-work table | none |
| `lane-ledger` | a lane ledger and the command that watches for returns | none |
| `limits` | concurrent agents, and iterations without improvement before stopping | 10, 2 |
| `naming` | the agent naming rule and whether it is checked | not checked |
| `adapters` | optional tools the project plugs into the cycle | none |

What no layer changes: model and effort are named on every agent call.
