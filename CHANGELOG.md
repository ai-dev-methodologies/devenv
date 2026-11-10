# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are dates (`YYYY.MM.DD-N`), the
same value that every run writes to the run log as `version`.

## [2026.11.10-2] - 2026-11-10

First release as the devenv Claude Code plugin. The handoff pair moves into it; entries below this one
describe the earlier standalone handoff commands.

### Added
- Plugin and marketplace in one repository (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
  install id `devenv@devenv`). The userConfig option `python` (required, no default) names the interpreter
  every hook and skill runs; `bin/devenv-py` runs a script with the interpreter given after `--py`.
- Skills: `handoff`, `resume-handoff`, `lean`, `lean-off` (typed by the user only), `cycle` and `doctor`
  (user-invoked only), called as `/devenv:<name>`.
- Hooks (`hooks/hooks.json`), active only where lean mode is on for the project or session: a large Read
  on the main thread is refused (`hook_read.py`), large Bash output is replaced by its head and a saved full
  output (`hook_bash.py`), a subagent return over 30 lines is sent back once (`hook_return.py`, SubagentStop
  and the SubagentHandback tool), and SessionStart records the start load and reminds a lean session
  (`hook_session_start.py`). The model cannot turn lean mode off.
- State files `~/.claude/state/lean-mode.json` (per project key, or per session) and
  `~/.claude/state/devenv-dev.json` (developer mode, off by default), written by `devenv_state.py` only.
- Rule layers: engine defaults, `~/.claude/devenv.local.md`, `<toplevel>/.claude/devenv.local.md`
  (the older name `handoff-rules.local.md` is read when the new one is missing). New sections
  `lean-limits`, `doctor-settings` and 14 cycle override points.
- Doctor: static checks, a replay of every registered hook in a temporary folder, and live firing read from
  `~/.claude/handoffs/.state/events.jsonl` (developer mode only). `dev on|off|status|report` and
  `init-project`.
- `context_budget.py` and `context_budget_check.py` ship with the engine; `handoff_state.py` uses the copy
  next to it first.
- Release test: the top CHANGELOG version, `plugin.json` version and `run_log.VERSION` must be equal.
- `LICENSE` (MIT) and a README in English and Korean.

### Changed
- The run log default path is written only while developer mode is on. `HANDOFF_LOG` still wins.
- Skill bodies call scripts as `devenv-py ${CLAUDE_PLUGIN_ROOT}/scripts/<script> … --py ${user_config.python}`.
- In the `Why` cell of Decisions Made and the why line of an open item, the user's words are quoted and the
  agent's own judgment starts with `agent:`. The checker does not change.

### Removed
- `install.py` and its tests. The plugin is installed with `claude plugin install`.

### Known limits
- Large output of tools whose output format is not documented (an agent listing, for example) is not
  stopped, and a shell redirection that writes the lean state file directly is not refused.
- The release test does not catch a release whose content changed while its version did not.

## [2026.11.10-1] - 2026-11-10

### Added
- Before Handoff gate. The user messages right before the last `/handoff` input (the last 5, counted
  past earlier `/handoff` inputs) and that input's arguments are recorded in a new optional section
  `## Before Handoff`, right before `## After Handoff` (last in the file when there is none). One line
  per message: `- 「<the user's own sentence>」 → 반영: <section title>` or `- 「…」 → 무관`. `반영:` must
  name a `## ` section of the file exactly (not Before Handoff or After Handoff). A message that opens
  with a quote of the assistant's output (`'…'`, `「…」`, or `>` lines of 40 characters or more, with 40
  characters or more of text after it) is recorded from the user's own sentence on; copying the quote
  along on one line also passes.
  `check_handoff.py` exits 1 with the new failure code `before-handoff` (one per missing message) and
  prints the lines to copy, only when `CLAUDE_CODE_SESSION_ID` is a UUID.
- Reviews gate. Every review run of the session (an Agent whose `subagent_type`, after its last `:`,
  contains `review`, `critic`, or `judge` or equals `architect` or `verifier`, and every Workflow run)
  needs a row in a new optional section `## Reviews`, right after `## Decisions Made`:
  `- <agent id | runId> · <type> · <final verdict | running> · <adopted or rejected, one line>`.
  A missing row, or a row whose verdict or last cell is empty or a placeholder, fails with the new
  failure code `reviews`; the printed row is not enough as it stands, so it fails until both cells are
  filled. When the transcript cannot be read, `WARNING: reviews not checked: <reason>` follows the
  result block and the exit code does not change. A review launch whose result carries no `agentId`
  (`runId` for a Workflow) prints the same WARNING; the runs that carry one are still checked. Accepted limit: a review done by an agent whose type
  matches none of these words is not counted.
- Resume briefing. `handoff_state.py --resume` prints `before handoff: N user message(s)` with the
  messages (at most 6) after the after-handoff lines and before `handoff age:`, and
  `WARNING: N user message(s) before the last /handoff are not in the handoff file` when
  `## Before Handoff` does not record them all. It is display only: `continue:` does not change. The
  briefing of `/resume-handoff` gets a `BEFORE HANDOFF:` block (after `AFTER HANDOFF:`) and a `REVIEWS:`
  block (after `DECIDED`). The Steps 0-3 call list is unchanged.
- Next-step order. When a numbered step of `## Exact Next Step` is a 🔴 user step, every later numbered
  step ends with `(독립)`, `(independent)`, `(← n)`, or `(<- n)`; otherwise the new failure code
  `contract-next-step-deps` fires. It is checked on the writing side only, so older files still resume.
- Run log: 7 new integer keys, always present (`null` when there is no value), so a line has 26 keys:
  `after_handoff_lines`, `before_handoff_lines`, `before_unrelated`, `attempt_n` (the try number at one
  handoff file in one session; 1 when the log file does not exist yet), `elapsed_ms` (since the last `/handoff` input), `reviews_found`,
  `reviews_recorded`. In its default mode `handoff_state.py` records the `previous handoff:` path in
  the `handoff` field. A check record's `handoff` is now the absolute path after `expanduser`; a `~user`
  that does not resolve is kept unexpanded instead of dropping the record.
- `run_report.py`: a run that only printed the usage is counted as `handoff_state.py/usage` or
  `check_handoff.py/usage` (and the failure code `usage`); `attempts_by_version` in Check failures gives
  the attempts to pass per version; two new warning kinds, `user messages before the last /handoff not in
  the handoff file` and `reviews not checked`.
- Lean mode line. When `~/.claude/state/lean-mode.json` (the lean skill's state file) exists,
  `handoff_state.py` prints one `lean-mode:` line in both modes: `on (since <UTC time>)`, `off`,
  `off (set for another project)`, or `unknown` (unreadable or malformed; the exit code does not
  change). The mode is project-scoped: `on` counts only when the file's `project` is the current
  project key, `since` is printed only when it is a strict UTC timestamp, and the path is never
  printed. Without the file the stdout is unchanged. The run log gets no new key (26).

### Changed
- Stdout contract exceptions. Without `CLAUDE_CODE_SESSION_ID`, or for a file without `**Session:**`,
  every output is byte-identical to 2026.11.09-3 except these:
  - The module docstring of `check_handoff.py` changes in one place, `(over 16 KiB)` to
    `(over 24 KiB)`, so the usage output differs by exactly one line. It is the only exception to the
    rule that the docstring stays as it is.
  - A failing open item ends with ` (missing: what, why, how)`, naming the lines it lacks, and the
    `contract-live-chain` line reads `Live item <name> from the previous handoff is missing: keep an item
    with id <key> in a Live field, or list <key> in a Closed (pointers) keys list`. Both apply with or
    without the session variable.
  - The size limit is 24 KiB: a file between 16 and 24 KiB no longer prints
    `WARNING: handoff file is <N> KiB`, and a larger one prints `(over 24 KiB)`. The Step 5 size cap
    default is 24 KiB too.
  - The 🔴 check of `contract-next-step-deps` runs on the writing side only.
- After Handoff matching is stricter and shared with Before Handoff. A message that opens with a quote of
  the assistant's output is matched by its own sentence when 40 characters or more of it follow the
  quote, a message of 300 characters or less must be quoted whole, and a longer one is shown and
  matched as its first 259 characters, `…`, and its last 40 (it was the first 300 and `…`). A file written
  with the old matching may need `계속` on resume.
- `run_report.py`: the size statistics count the last direct check of each file directly under
  `~/.claude/handoffs/` (a resume's own check, a fixture, and a scratch path are left out); the limit is
  24 KiB, so the JSON key `over_16_kib` is now `over_24_kib` and the text reads `over 24 KiB`.
- Run log version is `2026.11.10-1`.

### Known limits
- A `무관` line is not gated; the briefing shows the original message so a misjudged one can be seen.
- Reviews of earlier sessions are not carried into `## Reviews`; their conclusions belong in
  `Decisions Made` or `Discarded Hypotheses`.

## [2026.11.09-3] - 2026-11-09

### Added
- After-handoff gate. A decision the user gives after `/handoff` starts is recorded in the handoff
  file, and both gates compare against the session transcript. The handoff file has a header line
  `**Session:** <id>` (right after `**Continues:**`; `none` when there is no session id) and an optional
  last section `## After Handoff` with one line per user message:
  `- 「<user message verbatim>」 → 결정: <what changed and how>` (`변경 없음` is also a line).
  Typed messages, queued messages, and question-dialog answers all count.
- Writing gate: when `CLAUDE_CODE_SESSION_ID` is a UUID, `check_handoff.py` reads this session's
  transcript and exits 1 with the new failure code `after-handoff` for each user message after the
  last `/handoff` that the section does not quote, printing those messages verbatim (at most 5). The
  new failure code `contract-session` fires when `**Session:**` is missing or is not this session's id.
  To check an older file by hand from another session, run
  `env -u CLAUDE_CODE_SESSION_ID python3 ~/.claude/scripts/check_handoff.py <file> --git yes|no`.
- Resume gate: when the file's `**Session:**` is a UUID, `handoff_state.py --resume` prints
  `WARNING: N user message(s) after the last /handoff are not in the handoff file` with one indented
  line per message, and `continue: no (unrecorded messages after handoff)`, even with `--c`. The
  reason order is `project mismatch`, then `newer handoff skipped`, then this one; the WARNING is
  printed whichever wins. The briefing starts with an `AFTER HANDOFF:` block.
- Degrade: when the transcript cannot be judged (missing, unreadable, too large, unknown format, no
  `/handoff` input), both gates print `WARNING: after-handoff messages not checked: <reason>`. Exit
  codes and `continue:` do not change.
- `run_report.py` counts two new warning kinds: `after-handoff messages not checked` and
  `user messages after the last /handoff not in the handoff file`.

### Changed
- Stdout contract exception: in its default mode `handoff_state.py` prints `session id: <uuid>` and
  `transcript: <path | none (not found)>` after the state block when `CLAUDE_CODE_SESSION_ID` is a
  UUID, so the default output is up to 27 lines instead of 25. Without the variable the output is
  byte-identical to before. `/handoff` uses the printed `transcript:` folder for subagent result paths.
- `run_report.py` finds a transcript through `check_handoff.find_transcript`. With more than one copy
  of the session, the most recent copy is chosen; a copy whose status cannot be read is skipped.
- `handoff_state.py --help` documents the two new lines and the resume gate.
- The Steps 0-3 call list of `/resume-handoff` is unchanged.

### Known limits
- A decision that was never typed, or one the session omitted from the file before `/handoff` ran,
  is not caught. The gates compare user messages only.

## [2026.11.09-2] - 2026-11-09

### Added
- Each run-log line has five new keys, always present (`null` when there is no value), so a line has
  19 keys: `session_id` (from `CLAUDE_CODE_SESSION_ID` when it is a UUID, 8-4-4-4-12 hex digits;
  otherwise `null`), `caller` (from `HANDOFF_CALLER`, `handoff_state.py` when it ran the checker),
  `project`, `fail_codes` and `handoff_bytes`. `fail_codes` is set by `check_handoff.py` only: an
  object of fixed failure codes with counts, `{}` on a pass. No other environment variable is read.
  Indented output lines are still dropped and only counted, but a `handoff_state.py` line keeps its
  unindented `live subagent:` and `handoff branch:` lines, which quote Live subagents item names and
  the recorded branch; secret-like values in them are written as `REDACTED`. When `check_handoff.py`
  cannot be loaded, the `handoff_state.py` line is still written but keeps no output line.
- `scripts/run_report.py` summarizes `runs.jsonl` in one command: runs, versions, warnings, check
  failures and attempts until a pass, resume results, handoff size, projects, sessions, and old lines
  without the new keys. Options: `--log` · `--since` · `--project` · `--version` · `--json`. It never
  writes the log and never prints a record's `stdout` or `args`. Exit codes: 0 done, 2 usage error
  (including `HANDOFF_LOG=off` without `--log`), unreadable log, or internal error. `install.py`
  installs it with the other scripts (six files).
- `handoff_state.py` passes `HANDOFF_CALLER=handoff_state.py` to the checker it runs.
- `/handoff` and `/resume-handoff` may run `run_report.py`, but only when the user asks what happened.
  The Steps 0-3 call list of `/resume-handoff` is unchanged.

### Changed
- `check_handoff.py` records the `project` of a checked file from its `**Toplevel:**` header when that
  is an existing absolute folder. Its stdout and its exit codes are unchanged, with one exception: a
  `~user` path that cannot be resolved now ends with exit code 2, where it used to end with a full
  traceback and exit code 1. stderr then shows the last 5 lines of the traceback, followed by one
  `WARNING: run log not written: ...` line.

## [2026.11.09-1] - 2026-11-09

### Added
- `Goal (carried)` has a fifth key, `Constraints`: what was decided not to do and what is out of
  scope. It is copied along the chain like the goal. New items may be added at the end, separated
  by `; `; existing items stay as they are unless a `Changes` entry with a user quote names
  `Constraints:`. `none` means no items.
- `check_handoff.py` fails on provider-specific secret values (GitHub, OpenAI-style `sk-`, Slack,
  AWS, Google API keys, and whole private key blocks). The message names the kind and the line or
  lines, never the value. Replace the token, or the whole key block, with `REDACTED` (no brackets).
- The resume briefing shows `Constraints` verbatim in the GOAL block.
- `README.ko.md` (Korean). `README.md` is now the English default, with a language switch at the
  top of both files.
- This changelog.

### Changed
- When `/resume-handoff` picks a handoff but a file of the same project that is as new or newer was
  passed over, it prints `WARNING: newer handoff skipped (<reason>): <file name>` and
  `continue: no (newer handoff skipped)`, even with `--c`. Reasons: `no usable Exact Next Step`,
  `empty file`, `project unknown`, `not valid UTF-8`.
- The size warning (`WARNING: handoff file is <N> KiB (over 16 KiB)`) now comes after the check
  result and is left out of the resume output. `/handoff` keeps an over-limit file once the
  never-trim list is respected.
- The secret failure block is printed before other failures, so the resume excerpt keeps it.
- `docs/field-map.md` is in English.

### Fixed
- A newer handoff with an empty or placeholder `Exact Next Step` was dropped silently and an older
  file was resumed with `--c`.
- A secret copied into `Goal (carried)` was locked along the chain: masking it counted as a goal
  change. Masked values now compare equal.
- A truncated failure message could print the start of a secret value.
- Adding a fifth `Goal (carried)` key no longer switches the chain comparison off when the previous
  handoff has only four keys.
- A kebab-case name that starts with `sk-` in a path or branch is no longer reported as an API key.
- Masking only the header line of a private key left its body in the file and passed; the whole
  block is now checked and masked.

## [2026.11.08-1] - 2026-11-08

### Added
- First release of `/handoff` and `/resume-handoff` as a canonical, cross-platform repository with
  `install.py` (dry-run by default, `--yes` to apply, backups, rollback, `--python` to choose the
  interpreter written into the command files).
- The continue decision comes only from the resume argument (`--c`, `--continue`, or the word `계속`).
- Live work is carried in three fields (`Live subagents`, `Live runners`, `Live scheduled`), each item
  with `name (id)`, `does`, `decision`, `result`, `done-when`, `after-return`. The resume script
  judges each subagent from its newest transcript copy.
- `Goal (carried)` is copied verbatim along the `**Continues:**` chain; closed items go to a history
  file and the handoff keeps only a pointer.
- Resume picks only handoffs of the current project (`**Toplevel:**`).
- Rule layers: core, then a user layer, then a project layer limited to four override points.
- Every run appends one JSON line to `~/.claude/handoffs/.state/runs.jsonl`.
