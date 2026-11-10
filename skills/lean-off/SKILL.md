---
name: lean-off
description: Turn devenv lean mode off for this project, including the this-session-only entries set in it. Only the user can run it, by typing /devenv:lean-off; the model cannot invoke it.
disable-model-invocation: true
allowed-tools: Bash(devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/devenv_state.py *)
---

# Lean Mode Off

The line below has already turned lean mode off for this project and removed the this-session-only
entries set in it. A project inside it with its own entry, such as a nested repository, keeps lean mode
on:

!`devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/devenv_state.py lean off --cwd ${CLAUDE_PROJECT_DIR} --py ${user_config.python}`

Report the result in one line: `lean off (<scope>)` from the printed JSON line, or the printed
error text as it is. Run nothing else. When a folder above this project still has lean mode on, the
script keeps it on there and records this project as off.
