---
name: lean
description: Lean mode keeps the main context small: short reports, short returns from delegated work, hooks that refuse large reads and outputs. With no argument it turns lean mode on for this project; `session` limits it to this session, `status` only reports. Use when the user asks for lean mode.
argument-hint: "[on | session | status]"
allowed-tools: Bash(devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/devenv_state.py *)
---

# Lean Mode

Lean mode keeps the main session's context small: short reports, short returns from delegated work,
and hooks that stop large outputs before they land in the conversation. It applies to one project
(the default) or to this session only, and it stays on across `/devenv:handoff`, `/devenv:resume-handoff`
and compaction until it is turned off.

## Turning it on and reading its state

With no argument, this skill turns lean mode on for this project. Run the command for the argument:

| Argument | Effect | Command |
|---|---|---|
| nothing, or `on` | turns lean mode on for this project | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/devenv_state.py lean on --cwd ${CLAUDE_PROJECT_DIR} --py ${user_config.python}` |
| `session` or `on session` | turns lean mode on for this session only | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/devenv_state.py lean on --cwd ${CLAUDE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID} --py ${user_config.python}` |
| `status` | reports the state and turns nothing on | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/devenv_state.py lean status --cwd ${CLAUDE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID} --py ${user_config.python}` |
| `off`, or any argument that asks to turn it off | answers with one line, "To turn lean mode off, type `/devenv:lean-off` yourself.", and changes nothing | none |

- The project setting covers this project folder and every folder inside it, in every session. Its key
  is the git project of `${CLAUDE_PROJECT_DIR}`: the main worktree of a linked worktree, else the git
  toplevel, else the folder itself.
- The session setting covers this session only; other sessions in the project keep their own state.
- Any other argument: answer with the four usages in one line (`/devenv:lean`, `/devenv:lean session`,
  `/devenv:lean status`, `/devenv:lean-off`) and run nothing.
- The script prints one JSON line (`on`, `since`, `scope`). Exit 3 means the state file is unreadable:
  report the `error` value as printed and change nothing.
- After turning it on, answer in one line with the scope the script printed and the project folder
  `${CLAUDE_PROJECT_DIR}`, for example `lean on (project: /work/app)`, and say once that the user turns
  it off by typing `/devenv:lean-off`. For `status`, answer in one line with the printed state.
- The script is the only writer of the state file. Never write the state file with the Write or Edit
  tool.

Turning lean off is the user's own step: the user types `/devenv:lean-off` themselves. This skill
never turns it off.

## While lean mode is on

These rules come before any other report format rule. Their priority order is: keep going, then find
problems by idempotent repetition, then save context.

1. **Short reports.** A progress report is at most 5 lines: one line per lane whose state changed
   (lane, result, next action) and one line with the expected finish time. Give the full table of
   remaining work only when the user asks or a batch ends. Read times from the system clock (for
   example `date +%H:%M`); never estimate them.
   - Answers to the user's questions have no line cap. Keep them short but complete, conclusion
     first. The 5-line cap applies to progress reports and notification replies only.
2. **Delegated work returns its core only.** Ask every agent for a return of 30 lines or fewer with
   verdict, result and next action, and the full text as a file path. Do not ask for copies of the
   return by message. Judge from the return. When a fact needed for the verdict is missing or unclear,
   do not read the full file: ask the same agent for that item only, or extract only the needed part
   with a script. Ask as many times as needed, narrowing each time (item, then evidence line, then the
   source range).
3. **No large output.** Run long jobs so that only a summary line comes back. Read only the line range
   you need. Call tools with large output (an agent listing, for example) only when the verdict needs
   them.
4. **Keep going.** After handling a return, dispatch the next work in the same turn. Resume an
   interrupted agent with the same order. Ask the user only about what really blocks progress
   (a blocking finding, security, a push, a done condition, plan approval), all in one batch, and
   keep unblocked lanes running while waiting. A context-usage warning is not a reason to stop. When
   the session has to stop, write a handoff with `/devenv:handoff`; the next session finds lean mode
   still on.
5. **Idempotent repetition.** Running the same input again gives the same result. A rerun never
   overwrites results already made. Reviews block only on what the rules layer says blocks
   (`review-rounds`); record every problem found as one line so the next round handles it.
6. **Records go to files.** Decisions, history and long tables go to files; the conversation holds
   their paths.

Code, commands, paths, error messages, numbers and negations are never shortened or reworded.

Only two things are exempt from the short report cap: security warnings and the confirmation block
before an irreversible action. Write both in full sentences. The 30-line return gate for subagents has
no exceptions.

## What the hooks enforce

While lean mode is on for the project or session, the plugin hooks enforce part of the rules above.
The Read and Bash hooks act on the main thread only, so subagents can still read what their task
needs.

- A Read of a large text file (over 32 KiB or 400 lines by default) is refused unless it asks for a
  line range within the limit. The refusal names offset, limit and delegation as the ways forward.
  Binary files and `~/.claude/handoffs/` are not refused.
- Bash output over the limit is replaced (over 16 KiB or 200 lines by default): the conversation gets
  the first 40 lines and the path of a file holding the full output, to read with offset and limit.
- A subagent return longer than 30 lines is blocked and the subagent is asked to rewrite it shorter.
- At session start, a notice says lean mode is on and points back to these rules.
- Writing the state file with Write or Edit and running the state tool's off command are refused, in
  subagents too.

The limits come from the rules layer: a `## lean-limits` section in `~/.claude/devenv.local.md` or
`<toplevel>/.claude/devenv.local.md` holds `key: number` lines (`read_bytes`, `read_lines`,
`bash_bytes`, `bash_lines`, `bash_head_lines`, `return_lines`). A value can raise a limit to at most 4
times its default; a larger value is cut to that cap with a warning.

What the hooks do not cover: tools whose output format is not documented (an agent listing, for
example) and the length of the main session's own reports. Those follow the rules above by
convention only. A shell command that changes the state file directly is not refused either: a
redirection, `rm`, `mv`, `chmod`, or a run through one more shell or interpreter that builds the command
at run time.
