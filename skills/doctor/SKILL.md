---
name: doctor
description: Check the devenv install (interpreter, versions, rule layers, start load, a replay of every registered hook, live hook firing), switch developer mode and its report, or create the project rules file. Only the user can run it, by typing /devenv:doctor.
argument-hint: "[verbose] | dev on|off|status|report | init-project"
disable-model-invocation: true
allowed-tools: Bash(devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py *)
---

# Doctor

Run the one command for the argument, then stop:

| Argument | Command |
|---|---|
| nothing | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --cwd ${CLAUDE_PROJECT_DIR} --python ${user_config.python} --py ${user_config.python}` |
| `verbose` | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --cwd ${CLAUDE_PROJECT_DIR} --python ${user_config.python} --verbose --py ${user_config.python}` |
| `dev on` | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py dev on --py ${user_config.python}` |
| `dev off` | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py dev off --py ${user_config.python}` |
| `dev status` | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py dev status --py ${user_config.python}` |
| `dev report` | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py dev report --py ${user_config.python}` |
| `init-project` | `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py init-project --cwd ${CLAUDE_PROJECT_DIR} --py ${user_config.python}` |

Any other argument: say which arguments exist and run nothing.

## Reporting

Pass the script output on unchanged: every line as printed, in the printed order, with nothing added
between the lines, nothing summarized and nothing reworded. Then give the exit code. Do not fix anything
the output reports; the doctor never changes a file, and a fix is the user's call.

A check run prints `DOCTOR <id> PASS|WARN|FAIL|SKIP <evidence>` lines and ends with
`DOCTOR summary pass=N warn=N fail=N skip=N`. PASS lines are folded into that summary; `verbose` prints
them too. Exit 0 means no FAIL and no replay SKIP, 1 means a FAIL or a replay SKIP, 2 means the doctor
itself failed. A live SKIP does not change the exit code.

## What each line proves

- Static lines (`python-version`, `plugin-version`, `hook-scripts`, `skill-description`, `lean-state`,
  `dev-state`, `rules-*`, `paths`, `settings-names`, `legacy-*`, `start-load`, `hook-errors`): the files
  and values on this machine as the doctor read them. Evidence names keys, paths and counts, never a
  settings value.
- `settings-names`: the `## doctor-settings` section of the user rules file lists one dotted settings path
  per list line, with no file name in front (`- env.API_TOKEN`). Each name is looked up in settings.json and
  settings.local.json together, and a key that holds a dot itself is found too. A list line in another
  format is skipped and warned about with its count and the first 40 characters of the first such line.
- `replay-*` lines: each registered hook command ran in a temporary folder with violating, clean and
  lean-off inputs, and was denied, replaced or let through as it should be. A replay line proves the
  script, interpreter, registered command line only; it does not prove that Claude Code calls the hook.
- `live-*` lines: Claude Code really fired that hook in this version, as recorded while developer mode
  was on. Without such a record the line is `SKIP (no live firing recorded)`. A Bash replacement of the
  wrong shape is logged only in the harness debug file, which the doctor cannot read.

`dev on` and `dev off` switch developer mode, which records runs and hook decisions as counts; `dev report`
summarizes those records. `init-project` creates `.claude/devenv.local.md` in this project when it is
missing and prints the `.gitignore` line to add.
