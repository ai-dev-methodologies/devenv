# Field Cross-Reference Table

This document owns where each field of the current handoff format goes in the new format. It is a record of the cross-check made at the time of the migration. After the migration is done, "current" means the format before the migration.

It is the basis table for judging whether the number of current-format fields with no place in the new format is 0. The table classifies every ambiguous item as "keep". The result is on the "Cross-check result:" line at the end of the document.

## How to Read

| Abbreviation | Meaning |
|---|---|
| core format | The file format section of `skills/handoff/SKILL.md` in this repository |
| core write | Steps 0 to 6, Ground rules, and Safety rules of the same file |
| core resume | `skills/resume-handoff/SKILL.md` |
| core checker | `scripts/check_handoff.py` |
| core state script | `scripts/handoff_state.py` |
| user layer | `~/.claude/handoff-rules.local.md`. The order of application is core, user layer, project layer |
| project layer | `<toplevel>/.claude/handoff-rules.local.md`. The points it can override are a closed list of 4 |
| four override points | `runner-completion` (how to judge runner completion) · `state-files` (extra state files to read) · `forbidden-writes` (paths that must not be written) · `values` (value settings) |

- `file:line` in the current location refers to the installed copy under `~/.claude/` before the migration. The short names are:
  - `handoff.md`: the write command
  - `resume-handoff.md`: the resume command
  - `check_handoff.py`
  - `handoff_state.py`
- There are seven handling values:
  - keep
  - rename
  - split
  - optional line
  - project override point
  - replaced by script output
  - removed (user decision)
- A cell that exists only in the new format is not put in the table. Such a cell is not a current field. Example: the decision rationale · what to do after return · `**Toplevel:**` · `fires-at` · `diffstat:`.

## A. Header Lines and Separators

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 1 | Title line `# Session: YYYY-MM-DD` | handoff.md:173 | core format · title | keep | The general rule is that the title holds only the date. The current format also uses only the date |
| 2 | `**Started:**` | handoff.md:175 | core format · header lines | keep | No decision |
| 3 | `**Last Updated:**` | handoff.md:176 | core format · header lines | keep | No decision |
| 4 | `**Project:**` | handoff.md:177 | core format · header lines | keep | No decision. A new cell `**Toplevel:**` (the `project key:` value that identifies the project) is added on the next line |
| 5 | `**Topic:**` | handoff.md:178 | core format · header lines | keep | No decision. With the Auto-resume line gone, the use of this line as a position reference is gone |
| 6 | `**Auto-resume:**` header line | handoff.md:179 | none | removed (user decision) | The user decided to remove the auto-resume watcher |
| 7 | Auto-resume write condition (write only when a continue instruction was recorded, otherwise omit the line) | handoff.md:166-169 | none | removed (user decision) | With no marker, there is no write condition |
| 8 | Auto-resume position (right under `Topic`) · within 40 lines · format `yes · cwd=<absolute path>` | handoff.md:166, check_handoff.py:35-36, 108-111 | none | removed (user decision) | The marker check is taken out of the checker. A handoff with no marker does not fail because of the marker |
| 9 | Auto-resume grounds directive check (find 4 directive kinds in Decisions Made · Requirement Changes) | check_handoff.py:37-38, 112-114 | none | removed (user decision) | The marker grounds directive check is taken out of the checker |
| 10 | Auto-resume `yes` verdict output (`handoff auto-resume:`) | handoff_state.py:162-163 | none | removed (user decision) | The `handoff auto-resume:` line is no longer printed. The `continue:` line takes its place |
| 11 | First-line comment `<!-- context-budget: … -->` | handoff.md:118-119 | core format · first line (optional) | optional line | Put on the first line only when the script printed a `context-budget:` line. It is shown as an optional line in the format code block |
| 12 | Rule that the line is written and work continues even if the comment tool exits with code 2 | handoff.md:119 | core write · optional line | optional line | `handoff_state.py` calls `context_budget.py --line` and prints the result as a `context-budget:` line, and prints the line even if the exit code is 2 |
| 13 | Exclude comment lines · `#` lines · `---` · `[…]` placeholder lines from the content line count | handoff_state.py:105-111 | `is_placeholder` common check (line count criterion dropped) | replaced by script output | The line count criterion, which is not in the documents, is replaced by the common placeholder check |
| 14 | Section separator `---` | handoff.md:181, 191 and others | core format | keep | No decision |

## B. General Rules for Section Structure

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 15 | Core sections are always present, and an empty one holds `None` | handoff.md:112-113 | core write · Step 5 | keep | An empty section is the signal "this was considered" |
| 16 | A conditional section with no content is omitted entirely | handoff.md:115-116 | core write · Step 5 | keep | No decision |
| 17 | `## Confidence` and the values high · medium · low | handoff.md:183-185 | core format | keep | The resume briefing reads it (resume-handoff.md:95) |
| 18 | Confidence value criteria (compaction 0 to 1 times · 2 times · 3 or more) | handoff.md:187-189 | core format | keep | No decision |
| 19 | With 3 or more compactions, read the files and verify before asserting a state | handoff.md:68-69 | core write · Step 1 | keep | No decision |

## C. Session State

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 20 | The `## Session State` section, omitted entirely outside a git repository | handoff.md:193-195, 77 | core format | keep | No decision |
| 21 | Step 0 has exactly 1 shell command | handoff.md:34-38 | core write · Step 0 | keep | No decision |
| 22 | Session State is written from the Step 0 output only, with no added commands | handoff.md:71-73 | core write · Step 2 | keep | No decision |
| 23 | `toplevel:` output line | handoff_state.py:51-56 | core state script output | keep | No decision |
| 24 | `linked worktree:` output line | handoff_state.py:58-62 | core state script output | keep | No decision |
| 25 | `rules file:` output line (the project rules file path, replaced by the main worktree for a linked worktree) | handoff.md:40-41, handoff_state.py:64-68 | core state script output | keep | The project layer path stays as is, and the user layer is added as the second layer |
| 26 | If a rules file exists, read it first, and where it differs from the current file, the rules file wins | handoff.md:41-44, resume-handoff.md:35-36 | project layer (closed list of 4) | project override point | The current format can override anywhere, and the new format overrides only 4 points |
| 27 | The `- **Branch:**` field and the `branch:` output line | handoff.md:197, 70-76, handoff_state.py:70-76 | core format · Session State | keep | Resume reads it by the `- **Branch:**` prefix and the first word (handoff_state.py:136-141) |
| 28 | Detached HEAD written as `detached <sha>` | handoff_state.py:73-75 | core state script | keep | No decision |
| 29 | The `- **Uncommitted changes:**` field (file list or `none`) | handoff.md:198, 80 | core format · Session State | split | Only files this session changed are named, and the rest are given as a count. The state is split into one `status:` line and `status file:` |
| 30 | `status --short: N lines` and the file list (up to 40 lines, `... +N more`) | handoff_state.py:27, 86-92 | core state script output | replaced by script output | Replaced by one `status: N changed (M a · ?? b)` line plus `status file: <path>`. Names are not listed in the block |
| 31 | Collecting the files this session changed (`git diff --stat HEAD` or conversation memory) | handoff.md:52-53 | Conversation memory goes to the Uncommitted field, and statistics go to one `diffstat:` line | split | The conversation memory part is kept as is |
| 32 | The `- **Unpushed commits:**` field (count · `none` · `no upstream configured`) and the `upstream:` output | handoff.md:199, 79, handoff_state.py:78-84 | core format · Session State | keep | No decision |
| 33 | The `- **Lockfile changed:**` field (yes, install … · no) and the lockfile name list | handoff.md:200, handoff_state.py:33-37, 94-95 | core format · Session State | keep | No decision |
| 34 | The single field `- **Live background work:**` | handoff.md:201 | core format · Session State | split | Split into three fields, `Live subagents` · `Live runners` · `Live scheduled`, which are always present. `none` if there are none. Waiting work with no result path, such as a monitor, cron, or wakeup, goes to `Live scheduled` |
| 35 | The name and id of a Live item | handoff.md:201, 83 | The `name` cell of a `Live subagents` · `Live runners` item | rename | A new item has no id cell. In both fields, `name` holds `<name> (<id>)` together. The parenthesis holds one of agent id · task id · shell id · PID, and the checker checks the trailing `(…)` |
| 36 | `what it does` of a Live item | handoff.md:201, 83 | The `does` cell of the item | rename | Only the key name changes. The meaning is the same |
| 37 | `result path` of a Live item | handoff.md:201, 83-84 | The `result` cell of the item | rename | For a subagent it is the absolute transcript path, and for a runner it is the absolute log path. The checker rejects anything under `.claude/jobs/`. A `Live scheduled` item with no result path uses `fires-at` (a time or a trigger) |
| 38 | `returned when: <observation>` of a Live item | handoff.md:201, 85-86 | The `done-when` cell of the item | rename | Only the key name changes. The meaning is the same. For work still running on a jobs basis, the jobs path is written in this cell |
| 39 | What decides runner completion (one observation written per item) | handoff.md:83-86 | project layer · how to judge runner completion | project override point | The completion marker differs for each project runner |
| 40 | Live items come from the conversation, not from a script. Item kinds are subagent · job · monitor · runner | handoff.md:83-84 | core write · Step 1 · Step 2 | keep | Taking items from the conversation stays as is. The kinds are split into `Live subagents` · `Live runners` · `Live scheduled` (monitor · cron · wakeup), so every kind has a place to be recorded |
| 41 | Do not make up values. Do not collect port · Docker · migration · CI results | handoff.md:88-90 | core write · Step 2 | keep | No decision |
| 42 | Three Live field names allowed (`Live background work` · `Live subagents` · `Live runners`) | check_handoff.py:40, 52-54 | core checker | split | The new format always requires the three lines `Live subagents` · `Live runners` · `Live scheduled` and does not accept `Live background work` |
| 43 | Live field value rule: an empty value fails, and a value other than `none` · `없음` must be a result path containing `/` | check_handoff.py:41, 55-60 | core checker | split | The value is `none` or a list of items. Each item needs 6 keys (`Live scheduled` has `fires-at` instead of `result`) and a `(<id>)` in `name`, `result` is an absolute path, and it fails if it is under `.claude/jobs/` |
| 44 | The 4 required Session State fields (Branch · Uncommitted changes · Unpushed commits · Lockfile changed) | check_handoff.py:39, 49-51 | core checker | keep | The 4 stay as they are, and 3 Live fields are added (row 42). If the checker gets `--git yes`, it also fails when the Session State section is missing |

## D. Body Sections and Item Formats

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 45 | `## What We Are Building` (1 to 3 paragraphs, enough for someone with no memory of it to understand) | handoff.md:205-208 | core format | keep | No decision |
| 46 | `## What WORKED (with evidence)` and the rule to move an item with no evidence to Not Tried | handoff.md:212-215 | core format | keep | No decision |
| 47 | WORKED item format `— confirmed by:` | handoff.md:217 | core format | keep | No decision |
| 48 | WORKED empty phrase `Nothing confirmed working yet.` | handoff.md:219 | core format | keep | No decision |
| 49 | `## What Did NOT Work (and why)` and the rule to give the exact reason | handoff.md:223-226 | core format | keep | No decision |
| 50 | Failure item format `— failed because:` | handoff.md:228 | core format | keep | No decision |
| 51 | Failure empty phrase `No failed approaches yet.` | handoff.md:230 | core format | keep | No decision |
| 52 | `## Discarded Hypotheses` (section omitted if empty) | handoff.md:234-238 | core format (conditional section) | keep | No decision |
| 53 | Discarded item format `— rejected because:` | handoff.md:240 | core format | keep | No decision |
| 54 | `## What Has NOT Been Tried Yet` and the three lines of a decided item | handoff.md:244-248 | core format | keep | No decision |
| 55 | Untried item format `— what: · why: · how:` | handoff.md:250 | core format | keep | No decision |
| 56 | Untried empty phrase `No specific untried approaches identified.` | handoff.md:252 | core format | keep | No decision |
| 57 | `## Current State of Files` and the table column names `File` · `Status` · `Notes` | handoff.md:256-258 | core format | keep | No decision |
| 58 | The 4 Status values (Complete · In progress · Broken · Not started) | handoff.md:260-263 | core format | keep | No decision |
| 59 | File notation rule (concrete paths, no globs, ranges as `:L10-L45`) | handoff.md:265 | core format | keep | No decision |
| 60 | Files empty phrase `No files modified this session.` | handoff.md:267 | core format | keep | No decision |
| 61 | `## Decisions Made` | handoff.md:271-273 | core format | keep | No decision |
| 62 | Decisions item format `— reason:` | handoff.md:275 | core format · three cells `What` · `Why` · `How` | rename | `reason` goes to the `Why` cell, and the `What` and `How` cells are added. `How` may be replaced by a design document path |
| 63 | Decisions empty phrase `No major decisions made this session.` | handoff.md:277 | core format | keep | No decision |
| 64 | `## User Assumptions` (section omitted if empty) | handoff.md:281-284 | core format (conditional section) | keep | No decision |
| 65 | User-claim marker `[user-stated — not in code]` | handoff.md:286 | core format | keep | The resume briefing reads it (resume-handoff.md:126) |
| 66 | `## Requirement Changes` (section omitted if the scope did not move) | handoff.md:290-292 | core format (conditional section) | keep | No decision |
| 67 | Requirement change item format `State 1` · `State 2` · `State N (current)` | handoff.md:294-296 | core format | keep | No decision |
| 68 | `## Blockers & Open Questions` | handoff.md:300-303 | core format | keep | No decision |
| 69 | Blockers items (what is blocked · unanswered questions · external dependencies being waited on) | handoff.md:305 | core format | keep | No decision |
| 70 | Blockers empty phrase `No active blockers.` | handoff.md:307 | core format | keep | No decision |
| 71 | `## Exact Next Step` (the one thing to do right after resume; if unknown, say so and point to the section that decides the direction) | handoff.md:311-314 | core format | keep | A general rule is added: if there are Live items, the first step is written as "resume verdict" |
| 72 | Exact Next Step is written as an instruction | handoff.md:332-334 | core format · write notes | keep | No decision |
| 73 | `## Environment & Setup Notes` (only when needed, environment variables by name only) | handoff.md:318-321 | core format (conditional section) | keep | No decision |
| 74 | The note that `What Did NOT Work` and `Discarded Hypotheses` are the core value of this command | handoff.md:330-331 | core write · Notes | keep | No decision |

## E. Rules the Checker Enforces

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 75 | Three-cell label pairs (`what:`/무엇을 · `why:`/왜 · `how:`/어떻게, case-insensitive) | check_handoff.py:31, 85-89 | core checker | keep | No decision |
| 76 | List of sections that get the three-cell check (What Has NOT Been Tried Yet · Decisions Made) | check_handoff.py:30 | core checker | split | `What Has NOT Been Tried Yet` still gets the label pair check. Decisions Made is table rows, not bullets, so it is not subject to the label pair check, and each row is checked separately for having all three cells and for not being a placeholder |
| 77 | A design document path pattern (`…-design.md` · `…-plan.md` · `plans/…`) stands in for the three cells | check_handoff.py:32, 87-88 | core checker | keep | Project-specific plan folder patterns are taken by the project layer point "extra state files to read" |
| 78 | Items under 40 characters and items starting with `- none` · `- 없음` · `- no ` are treated as status lines and skipped | check_handoff.py:33, 139-140 | core checker | keep | No decision |
| 79 | Item splitting rule (starts with a bullet or number, an indented line continues the item) | check_handoff.py:67-82 | core checker | keep | No decision |
| 80 | Exact Next Step must exist and be non-empty | check_handoff.py:61-63 | core checker | keep | Besides being empty, a section holding only placeholders (including a multi-line bracket block) also fails |
| 81 | Exit codes 0 (pass) · 1 (item · marker · resume contract failure) · 2 (file missing) | check_handoff.py:23-24, 123-128, 157-160 | core checker | split | A marker failure is removed (row 8). 0 and 1 stay as they are, and 2 is used for an unexpected exception as well as a missing file |
| 82 | Three failure messages (item / marker / resume contract) and the pass message | check_handoff.py:145-159 | core checker | split | The marker message is removed (row 8). The item and resume contract messages and the pass message remain |
| 83 | After writing, fix and rerun until the checker exits 0 | handoff.md:123-128 | core write · Step 6 | keep | No decision |
| 84 | Checker default constants (minimum item length 40 · header 40 lines · directive list) | check_handoff.py:33, 36, 38 | core checker | split | The minimum item length 40 stays as a core default. The header 40 lines and the directive list are removed together with the marker check (rows 8 and 9) |

## F. File Name, Location, and Safety

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 85 | Create the save folder `~/.claude/handoffs/` only when it is missing | handoff.md:91-92 | core write · Step 3 | keep | No decision |
| 86 | Do not write to plugin-owned folders or `session-data` | handoff.md:93-94 | core write · Step 3 | keep | No decision |
| 87 | Do not write to the project state folder | handoff.md:94 | project layer · paths that must not be written | project override point | The state folder name differs for each project |
| 88 | File name `YYYY-MM-DD-<short-id>-session.tmp` (today's date) | handoff.md:98 | core write · Step 4 | keep | No decision |
| 89 | short-id rules (allowed characters · first character · at least 8 lowercase characters) | handoff.md:100-104 | core write · Step 4 | keep | The short-id rules move up to the core as a general rule |
| 90 | A valid example and "if it is wrong, resume cannot find it" | handoff.md:106-108 | core write · Step 4 | keep | No decision |
| 91 | Each session has its own file, never appended to, and a rerun rewrites it whole | handoff.md:328-329 | core write · Notes | keep | No decision |
| 92 | Record environment variables by name only | handoff.md:157 | core write · Safety rules | keep | General rule against sensitive values |
| 93 | Do not paste a diff, point to it as `path:L10-L45` | handoff.md:158 | core write · Safety rules | keep | No decision |
| 94 | Mask credentials and say they were masked | handoff.md:159 | core write · Safety rules | keep | General rule against sensitive values |
| 95 | The file stays in the home folder and not inside the repository | handoff.md:160 | core write · Safety rules | keep | No decision |

## G. Behavior Rules of the Write Command

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 96 | The two commands are a pair and do not call or depend on any other command | handoff.md:9-10, resume-handoff.md:9-10 | core write · core resume | keep | No decision |
| 97 | Write one file only | handoff.md:14 | core write · Ground rules | keep | No decision |
| 98 | Do not stop subagents · background jobs · monitors · scheduled work, and do not tell anyone to stop them | handoff.md:15-16 | core write · Ground rules | keep | No decision |
| 99 | Do not run `/compact`, open a new session, or end the session | handoff.md:17 | core write · Ground rules | keep | No decision |
| 100 | Do not ask confirmation questions | handoff.md:18-20 | core write · Ground rules | keep | No decision |
| 101 | 4 times to use it (end of session · before the context limit · right after costly reasoning · any time) | handoff.md:22-28 | core write · When to Use | keep | No decision |
| 102 | Items to collect (changed files · attempts and why they failed · rejected approaches · user claims not visible in code · order of requirement changes · background work) | handoff.md:53-60 | core write · Step 1 | keep | Background work is checked with `ListAgents`, and the spawn time and expected return (`started` · `expected-by`) are collected together |
| 103 | For each open item and decision, three lines: what · why (quoting the user) · how | handoff.md:61-64 | core write · Step 1 | keep | The same rule as the mandatory three-cell Decisions |
| 104 | A design decided only in conversation is written to a durable file first, and the handoff points to that path | handoff.md:64-66 | core write · Step 1 | keep | The project layer "extra state files to read" can take that path |
| 105 | First report line `Handoff saved to [resolved path]` | handoff.md:131-132 | core write · Step 6 | keep | No decision |
| 106 | Second report line `Live background work: [n] item(s), still running` | handoff.md:134 | core write · Step 6 | split | Reported split into the counts of `Live subagents` · `Live runners` · `Live scheduled` |
| 107 | Third report line `Resume: ready — …` or `not ready — <reason>` | handoff.md:135, 138-139 | core write · Step 6 | keep | No decision |
| 108 | The 4 ready conditions (checker exit 0 · every Live item has a result location · decisions and design are in the file · no pending writes) | handoff.md:141-148 | core write · Step 6 | keep | A `Live scheduled` item has `fires-at` instead of a result location |
| 109 | `/clear` is not done by the write command and is decided by the user | handoff.md:150 | core write · Step 6 | keep | No decision |
| 110 | After the report, continue the work that was in progress, and rewrite the file if something is pointed out | handoff.md:152-153 | core write · Step 6 | keep | No decision |
| 111 | The note that resume reads the whole file, so every section carries over | handoff.md:335 | core write · Notes | keep | No decision |

## H. Fields and Output Read by the Resume Command

| # | Current field | Current location (file:line) | New location | Handling | Reason |
|---|---|---|---|---|---|
| 112 | 4 argument forms (none · date · slug · path) | resume-handoff.md:14-19 | core resume · Usage | keep | No decision |
| 113 | Step 0 command `handoff_state.py <cwd> --resume [<arg>]`, once | resume-handoff.md:25-36 | core resume · Step 0 | keep | No decision |
| 114 | The table of 5 calls in Steps 0 to 3 and the `TOOLS USED (Steps 0–3)` report | resume-handoff.md:38-53, 102 | core resume · Step 0 | keep | The 5 calls stay as they are. Read is 1 handoff + at most 2 rules files = at most 3, the result path · `status file:` · `state-files` paths are added to the read-only agent input, and it is written as 0 times when `ListAgents` cannot be used |
| 115 | Candidate file rules (a path is used as that file · a date · a slug right after the date · no argument means all) | resume-handoff.md:57-63, handoff_state.py:115-133 | core resume · Step 1 | keep | Date · slug · no-argument candidates are filtered to files whose `project key:` and `**Toplevel:**` match. A path argument is not filtered, and if the project differs it is `continue: no (project mismatch)` |
| 116 | Candidate filter rules (exclude empty files · unreadable files · files with only a title and placeholders; latest modification time; the larger file on a tie; then path order) | resume-handoff.md:65-66, handoff_state.py:129-133 | core resume · Step 1 | keep | No decision |
| 117 | Minimum content line count 10 (only in the script, not in the resume command document) | handoff_state.py:29, 129 | `is_placeholder` common check (line count criterion dropped) | replaced by script output | The line count criterion, which is not in the documents, is replaced by the common placeholder check |
| 118 | Stop after reporting `handoff: none found` | resume-handoff.md:68, handoff_state.py:146-148 | core resume · Step 1 | keep | The output becomes `handoff: none found for this project`. If the most recent file belongs to another project, a warning is added |
| 119 | Summarize after reading the whole file | resume-handoff.md:72 | core resume · Step 2 | keep | If the project layer names extra state files, they are read too |
| 120 | The 3 live state values `running` · `returned` · `lost`, and only read-only checks allowed | resume-handoff.md:76-85 | core resume · Step 3 | keep | A general rule is added that a subagent is judged by the last `stop_reason` in its transcript, along with the unconfirmed in-progress state `running?` |
| 121 | `handoff age:` output and the warning for over 7 days | handoff_state.py:30, 150-152, resume-handoff.md:153 | core state script output | keep | No decision |
| 122 | `handoff branch:` output and the branch change warning | handoff_state.py:153-160, resume-handoff.md:153 | core state script output | keep | No decision |
| 123 | `handoff check: exit N` and at most 8 lines of check output | handoff_state.py:28, 164-176 | core state script output | keep | No decision |
| 124 | Briefing header and footer line `HANDOFF LOADED: [path]` | resume-handoff.md:92 | core resume · Step 4 | keep | No decision |
| 125 | Briefing `PROJECT:` (reads the Project · Topic header lines) | resume-handoff.md:94 | core resume · Step 4 | keep | No decision |
| 126 | Briefing `CONFIDENCE:` (reads the Confidence section; if low, verify against the code) | resume-handoff.md:95 | core resume · Step 4 | keep | No decision |
| 127 | Briefing `GOAL:` (summarizes What We Are Building) | resume-handoff.md:97-98 | core resume · Step 4 | keep | No decision |
| 128 | Briefing `STATE:` (reads Branch · Uncommitted · Unpushed · Lockfile) | resume-handoff.md:100-101 | core resume · Step 4 | keep | No decision |
| 129 | Briefing `LIVE WORK:` (reads the Live fields and reports the state and result location of each item) | resume-handoff.md:104-105 | core resume · Step 4 | keep | Reads all three split Live fields and also shows `decision` and `after-return` |
| 130 | Briefing `OPEN WORK:` (reads In progress · Not started in Current State of Files) | resume-handoff.md:107-108 | core resume · Step 4 | keep | No decision |
| 131 | Briefing `CONFIRMED WORKING:` (the count and a one-line summary of What WORKED) | resume-handoff.md:110-111 | core resume · Step 4 | keep | No decision |
| 132 | Briefing `DECIDED (do not relitigate):` (all of Decisions Made) | resume-handoff.md:113-114 | core resume · Step 4 | keep | No decision |
| 133 | Briefing `REQUIREMENT NOW:` (the current state in Requirement Changes) | resume-handoff.md:116-117 | core resume · Step 4 | keep | No decision |
| 134 | Briefing `SETUP:` (a one-line summary of Environment & Setup Notes) | resume-handoff.md:119-120 | core resume · Step 4 | keep | No decision |
| 135 | Briefing `DO NOT RETRY:` (all of What Did NOT Work + Discarded Hypotheses) | resume-handoff.md:122-124 | core resume · Step 4 | keep | No decision |
| 136 | `DO NOT RETRY` is always shown, even when it is `none` | resume-handoff.md:138 | core resume · Step 4 | keep | No decision |
| 137 | Briefing `UNVERIFIED USER STATEMENTS:` (all of User Assumptions) | resume-handoff.md:126-127 | core resume · Step 4 | keep | No decision |
| 138 | Briefing `BLOCKERS / OPEN QUESTIONS:` | resume-handoff.md:129-130 | core resume · Step 4 | keep | No decision |
| 139 | Briefing `NEXT STEP:` (Exact Next Step) | resume-handoff.md:132-133 | core resume · Step 4 | keep | No decision |
| 140 | The text for when there is no Exact Next Step: `No next step recorded — review 'What Has NOT Been Tried Yet' together first` | resume-handoff.md:134 | core resume · Step 4 | keep | No decision |
| 141 | If the user already said to continue in this session, act without waiting and do not open a question window | resume-handoff.md:143-146 | None. Whether to continue is decided by the `continue:` line in core resume · Step 5 | removed (user decision) | The paths that continued because of a standing continue instruction in the body or a continue instruction in this session are removed. The rule on whether to continue is owned by core resume Step 5 |
| 142 | When waiting, do not touch files, and when told to continue, run Exact Next Step as written, handling `returned` · `lost` Live items first | resume-handoff.md:148-149 | core resume · Step 5 | keep | No decision |
| 143 | Warnings to put in the briefing (`WARNING:` suffix · check exit 1 and the problem list · file missing warning) | resume-handoff.md:153-155 | core resume · Warnings | keep | A warning for ignored argument words and a warning for another project's handoff are added |
| 144 | The handoff file is a read-only record and is not modified | resume-handoff.md:159 | core resume · Notes | keep | No decision |
| 145 | To save a new session, run `/handoff` again | resume-handoff.md:160 | core resume · Notes | keep | No decision |

Cross-check result: current 145 · located in the new format 139 · removed by user decision 6 · missing 0
