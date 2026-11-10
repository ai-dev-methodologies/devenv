[한국어](article.ko.md)

# When a written rule is not enough: enforcing context discipline in Claude Code with hooks

This article covers devenv, a Claude Code plugin that enforces context-saving rules with hooks and replays those hooks so the enforcement cannot switch itself off unnoticed.

## The problem: one large output undoes a rule

Long Claude Code sessions run into two problems. The first is the main session's context. A rule such as "read only the lines you need" sits in an instruction file and is mostly followed. Then one agent listing or one unbounded file read puts thousands of lines into the conversation, and they stay there for the rest of the session. A rule kept only by convention broke at the moments that cost the most. The instruction files add to the load too: on the machine this plugin was built on, the files loaded at session start came to 55,382 bytes against a budget of 49,152 bytes.

The second is the move to a new session, which starts from whatever was written down. What went missing was the decisions and their reasons, rejected approaches, and the state of running work.

devenv handles both in one plugin: a lean mode enforced by hooks for the first, and a handoff and resume pair checked by scripts for the second.

## Structure: a public engine and a private layer

What a plugin can carry decides the split.

| | Items |
|---|---|
| A plugin can carry | skills, commands, agents, hooks, MCP and LSP servers, output styles, `bin/`, `userConfig`, and a `settings.json` limited to `agent` and `subagentStatusLine` |
| It cannot carry | rule files, `CLAUDE.md` (one at the plugin root is not loaded), permissions, environment variables, a status line |

So devenv has two layers.

| Layer | Holds | Distribution |
|---|---|---|
| Engine (public plugin) | skills, hooks, scripts, defaults | installed from a marketplace, no authentication |
| Personal layer (private repository) | rule files, `CLAUDE.md`, agent definitions | placed by a copy script. Settings values are not copied; the doctor reports the names of missing settings |

Per-person and per-project behavior comes from rule layers, applied in order:

1. Engine defaults: constants in `scripts/rules_layer.py`
2. User layer: `~/.claude/devenv.local.md`
3. Project layer: `<toplevel>/.claude/devenv.local.md`

Each `## <id>` section is one override point; in the project layer, a section outside the list is not applied and prints a warning. They are read when a command runs, so they add nothing to the start load. The enforcement limits live here too; the defaults, from `scripts/rules_layer.py`:

```python
DEFAULT_LIMITS = {"read_bytes": 32768, "read_lines": 400, "bash_bytes": 16384, "bash_lines": 200,
                  "bash_head_lines": 40, "return_lines": 30}
```

The project layer file is kept out of git. It records how one person runs work in that repository (approval points, critic lanes, a ledger command), not facts about the code, and it can carry names that do not belong in a shared history. The command that creates the file prints the `.gitignore` line, and the doctor warns when the file is not ignored.

## What the hooks do, and what we measured first

Before writing the hooks we ran 16 headless sessions (`claude -p`) against a throwaway plugin whose hooks logged their input, and two more while writing the Read hook. Some results changed the design.

| Script | Event | Effect while lean mode is on |
|---|---|---|
| `hook_read.py` | PreToolUse (Read, Write, Edit, Bash) | refuses a large Read without a line range and any model attempt to turn lean mode off |
| `hook_bash.py` | PostToolUse (Bash) | replaces a large output with its head and the path of the full output |
| `hook_return.py` | SubagentStop, PreToolUse (SubagentHandback) | sends a subagent reply over 30 lines back once to be shortened |
| `hook_session_start.py` | SessionStart | records the start load and reminds a lean session of its rules |

**Hooks fire on subagent tool calls too.** A PreToolUse input from inside a subagent has `agent_id` and `agent_type`; one from the main thread has neither. The Read and Bash hooks act only when `agent_id` is absent. Without that check, the subagent handed a large read would be refused as well.

**Bash output size is unknown until the command has run.** A command string does not say how much it will print, so the Bash hook acts after the run, in PostToolUse, by returning `updatedToolOutput`. The replacement must keep the output object's keys. With all four required keys (`stdout`, `stderr`, `interrupted`, `isImage`) the model saw the replacement. With only `stdout`, or with a plain string, the model saw the original output and the stream reported the hook as successful. The only trace was one line in the debug log:

```
[ERROR] PostToolUse hook returned updatedToolOutput that does not match Bash's output shape: reason=schema_invalid issues=invalid_type
```

The hook now copies the original `tool_response`, replaces only `stdout` and `stderr`, and checks the shape before printing.

**In auto permission mode, a SubagentStop block is ignored.** In default mode, SubagentStop carries the reply in `last_assistant_message`; a block makes the subagent rewrite it, and the hook lets the second call (`stop_hook_active: true`) through. In auto mode the subagent reports through a `SubagentHandback` tool call instead. SubagentStop then has no reply field, and the debug log shows `Stop hook block discarded (turn ended by tool result, no model re-invoke)`. So the gate also sits on that tool call, registered in `hooks/hooks.json`:

```json
{
  "matcher": "SubagentHandback",
  "hooks": [
    {
      "type": "command",
      "command": "${user_config.python}",
      "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/hook_return.py", "handback", "--data", "${CLAUDE_PLUGIN_DATA}"]
    }
  ]
}
```

When PreToolUse denied that tool call, the subagent rewrote its report and called the tool again. The hook denies once per `agent_id`.

**A skill with model invocation turned off cannot be reached through a command file.** A personal command file that called the engine's lean-off skill got `cannot be used with Skill tool due to disable-model-invocation`, the same refusal as asking the model to call it. Only the user typing the name ran the skill, so turning lean mode off is the user's own `/devenv:lean-off`.

**Shell lines in a skill body do not pass through tool-call hooks.** In one run, a skill line written as `` !`command` `` ran its script and the PreToolUse Bash hook recorded nothing for it. An ordinary Bash call in the same setup was recorded. This is why the lean-off skill can run the state tool's `lean off` command while the same command from the model is refused. Two more findings shaped those lines:

- A line that called the interpreter directly needed approval, and the whole skill failed.
- `allowed-tools` does not substitute `${user_config.*}`, so skills call a `bin/devenv-py` wrapper that takes the interpreter as an argument.

**A missing interpreter does not block, and one of its notices is invisible.** With the interpreter path missing, all three ways of writing the hook command ended in a non-blocking error (exit 1, 127 or 126 depending on the form), and the tool call and the subagent return went ahead. The PreToolUse notice appeared in the main transcript. The SubagentStop notice appeared only in the subagent's transcript, where the main session never sees it.

## Keeping enforcement from switching off quietly

The last finding means an enforcement hook can stop working with no visible sign. Three rules guard against it.

**Errors are not reported as success.**
- On bad input or an internal error the hooks exit 1, shown as a hook error. Never 0, which hides a broken registration, and never 2, which blocks the tool call.
- A lean state file that cannot be read is `unknown`, never off. The call passes with a visible warning.
- `onFailure: "block"` is not used. With it, a missing interpreter would block calls in projects where lean mode is off.

**The model cannot turn enforcement off.**
- The only way off is `/devenv:lean-off`, typed by the user.
- The Read hook refuses a Write or Edit of the state file and a Bash call that runs the state tool with `lean` and `off`, in subagents as well. Its refusal messages never say how to turn enforcement off.
- Test paths for the state folder come only from a `--root` argument. A leaked environment variable could point a live hook at an empty state, which reads as off.

**The doctor replays the hooks.**
- For each enforcement hook it replays, with temporary state and data paths, a violating input that must be refused and a clean input and a lean-off input that must pass.
- It replays `stop_hook_active: true` for the SubagentStop gate and checks the four output keys of the Bash replacement directly.
- Each result line states what it proves. A `replay` line covers only the script, the interpreter and the registration line.
- A separate `live` line, read from the developer mode event log, reports whether the harness actually calls the hook. It is `SKIP` when nothing has been recorded since the current version was installed.
- Lines read `DOCTOR <id> PASS|WARN|FAIL|SKIP <evidence>`, then a summary. FAIL marks only what a machine checked, and nothing is fixed automatically.

## Handing a session over

`/devenv:handoff` writes a dated file, and `/devenv:resume-handoff` loads it in a new session. The work is split by what a script can check.

| Scripts decide | The model writes |
|---|---|
| git state, project key, previous handoff, transcript path (`handoff_state.py`) | what was tried, what failed and why, rejected approaches |
| whether the file is complete (`check_handoff.py`): open items with what, why and how, filled Decisions rows, live items with six keys, the goal carried unchanged unless a change is recorded | the decisions and their reasons |
| secrets in known token formats, reported by kind and line, never by value | the exact next step |
| user messages after the last `/handoff` that the file does not quote | |
| whether a resume continues: only the argument `--c`, `--continue` or `계속`, never text in the file | |

The handoff skill never stops running work and does not report until the checker exits 0. Before its briefing, a resume makes at most:

- one script call
- three reads: the handoff file and up to two rule files
- one agent listing
- one read-only agent returning at most 40 lines, which reads the history file only when an open item points at it

## Limits

These are not covered, and the skills say so.

- Large output from tools whose output format is not documented, such as an agent listing. The incident that started this work is still not prevented.
- A shell redirection that writes the lean state file directly.
- The length of the main session's own reports, which stays a convention.
- Items only an interactive session can show, such as whether a skill's shell line opens an approval prompt. Internal agents with an empty `agent_type` did not fire in headless runs either; the hook lets an empty type through.

We have not measured how much context the enforcement saves. Developer mode records how many times enforcement acted, not what it saved.

## Installing

The engine repository is also its own marketplace (`https://github.com/ai-dev-methodologies/devenv`). Install the `devenv` plugin from it and give the absolute path of a Python 3.10 or later interpreter when asked; every hook and skill runs its script with it. From `.claude-plugin/plugin.json`:

```json
"userConfig": {
  "python": {
    "type": "string",
    "title": "Python interpreter",
    "description": "Absolute path to a Python 3.10 or later interpreter used to run the plugin scripts.",
    "required": true
  }
}
```

Turn lean mode on with `/devenv:lean on` for the project or `/devenv:lean on session` for this session only. The repository README is the canonical guide to installation and use.
