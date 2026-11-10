#!/usr/bin/env python3
"""Check a /handoff session file: open items, Decisions table, and the resume contract.

Usage: check_handoff.py <session-file> [--git yes|no]

Open items (handoff.md Step 1): every bullet under "What Has NOT Been Tried Yet" and "Decisions Made"
must carry what/why/how, or point at a design file. An item passes when its text (the bullet plus
its indented continuation lines) contains either
  - all three labels  what:/무엇을  why:/왜  how:/어떻게   (case-insensitive, Korean or English), or
  - a path that looks like a design file (…-design.md / …-plan.md / plans/…).
Items shorter than MIN_ITEM_CHARS are treated as status lines, not work items, and skipped.

Decisions table: when "Decisions Made" holds a table, it has a header row, a separator row, and at least
one data row. Every data row has three filled cells (What | Why | How), none a placeholder. A section with
no table at all passes only when its body is the single sentence NO_DECISIONS, or one line `None`.

Resume contract (what /resume-handoff needs without running anything but handoff_state.py):
  - The head carries `**Toplevel:** <absolute path>` (the project key handoff_state.py printed) and
    `**Continues:** none | <absolute path of an existing previous handoff>`: not this file itself, and
    of the same Toplevel when both files carry one.
  - "Goal (carried)" carries Origin, Goal, Done-when, Constraints and Changes with real values (Constraints
    may be none); Origin starts with an absolute path. When Continues names a file that has the same
    section, only the keys that file carries are compared: Origin is byte-identical to it, its Changes
    entries are the first entries here unchanged, Goal and Done-when are byte-identical unless Changes
    gained an entry, and its Constraints entries (split at `; `; none or 없음 is no entry) are the first
    entries here, whole and unchanged, unless a new Changes entry names `Constraints:`; a new entry
    carries a quoted user sentence. Both sides are compared with secret-like values replaced by
    REDACTED, so masking a carried token is not a change.
  - Every live item of the Continues file (key: the id in its name, else the whole name) appears in one
    of the live fields here, or among the keys of "Closed (pointers)".
  - "Closed (pointers)", when present, is `- <N> items closed -> <history file> (keys: a, b)`: the file is
    an existing absolute path and has a ` · <key> · ` line part for every listed key.
  - "Session State", when present, is not empty and carries the five state fields STATE_FIELDS with real
    values, plus the three live fields of LIVE_KEYS. Each live field is "none" or a list of items; every
    item has its field's six keys with non-placeholder values. Subagent and runner names end with
    `(<id>)`, the id of letters, digits and `_ . : -` and not a status word (NOT_IDS). Their `result` is
    an absolute path that is not under .claude/jobs/ or /tmp/claude-* (both are deleted). Scheduled items
    carry `fires-at` instead of `result`.
  - With `--git yes`, a missing "Session State" section fails. With `--git no` (outside git) it is required
    too, with the three live fields; the git fields may be n/a or absent. Without `--git`, a missing
    section is not checked.
  - "Exact Next Step" exists and is neither empty, only placeholder lines, nor one [...] or <...> block.

Secret-like values: a line holding a provider token form of SECRET_PATTERNS fails as
`secret-like value (<kind>) on line <N>; replace it with REDACTED`, unless the line holds EXAMPLE. A
private key is a block: from its BEGIN line to its END line (or the end of the file), an END line with
the base64 lines just above it, or KEY_BODY_MIN_LINES base64 lines of KEY_BODY_MIN_CHARS or more in a
row; it fails as `secret-like value (private key block) on lines <A>-<B>; replace the whole block with
REDACTED`. These failures print first. No printed line carries the value: every other check reads this
file and the Continues file with those forms and every line of a key block replaced by REDACTED.

A file over SIZE_LIMIT_KIB prints one `WARNING: handoff file is <N> KiB (over 24 KiB)` line after the result
block; the exit code is unchanged.
Every run appends one record to the run log (run_log.py next to this file).

Exit 0 = every check passes. Exit 1 = at least one check failed (listed). Exit 2 = usage error, file missing
or unreadable, or an unexpected error (traceback tail on stderr).
"""
import importlib.util
import json
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SECTIONS = ("What Has NOT Been Tried Yet", "Decisions Made")  # open designs and decisions; blockers and next-step lines are navigation
LABELS = (("what:", "무엇을"), ("why:", "왜"), ("how:", "어떻게"))
DESIGN_PATH_RE = re.compile(r"(?:[\w./-]*-design\.md|[\w./-]*-plan\.md|\bplans/[\w./-]+)")
MIN_ITEM_CHARS = 40
STATE_FIELDS = ("Branch", "HEAD", "Uncommitted changes", "Unpushed commits", "Lockfile changed")
RETIRED_LIVE_FIELD = "Live background work"
ITEM_KEYS = ("name", "does", "decision", "result", "done-when", "after-return")
SCHEDULED_KEYS = ("name", "does", "decision", "fires-at", "done-when", "after-return")
OPTIONAL_KEYS = ("started", "expected-by")
LIVE_KEYS = {"Live subagents": ITEM_KEYS, "Live runners": ITEM_KEYS, "Live scheduled": SCHEDULED_KEYS}
ID_FIELDS = ("Live subagents", "Live runners")  # their `name` is `<name> (<id>)`
LIVE_FIELDS = tuple(LIVE_KEYS)
NONE_WORDS = ("none", "없음")
NO_DECISIONS = "No major decisions made this session."  # byte-identical to the sentence in commands/handoff.md
NO_DECISIONS_WORDS = ("none", "none.")
PLACEHOLDER_WORDS = ("...", "…", "tbd", "todo", "fixme", "placeholder", "n/a")
FIELD_RE = re.compile(r"^[-*]\s+\*\*(.+?):\*\*\s*(.*)$")
ABS_PATH_RE = re.compile(r"^(?:/|~/|[A-Za-z]:[\\/])\S")  # POSIX, ~/, C:\ and C:/ ; /c/ and /mnt/c/ are POSIX
SEPARATOR_RE = re.compile(r"^\|?[\s:|-]*-[\s:|-]*\|?$")
TOPLEVEL_RE = re.compile(r"^\*\*Toplevel:\*\*\s*(.*)$")
CONTINUES_RE = re.compile(r"^\*\*Continues:\*\*\s*(.*)$")
GOAL_SECTION = "Goal (carried)"
GOAL_KEYS = ("Origin", "Goal", "Done-when", "Constraints", "Changes")
GOAL_SAME_KEYS = ("Origin", "Goal", "Done-when")  # carried verbatim unless Changes gained an entry
CONSTRAINT_SPLIT_RE = re.compile(r"\s*;\s+")
CONSTRAINTS_CHANGE = "Constraints:"  # a new Changes entry naming this may drop or rewrite a constraint
CHANGE_SPLIT_RE = re.compile(r"\s*\|\s+")
QUOTE_RE = re.compile(r"\"[^\"]+\"|“[^”]+”|「[^」]+」")
CLOSED_SECTION = "Closed (pointers)"
CLOSED_RE = re.compile(r"^\s*[-*]\s+.*?(?:→|->)\s*(.+?)\s*\(keys:\s*(.*?)\)\s*$")
SIZE_LIMIT_KIB = 24
SECRET_PATTERNS = (  # provider token forms only; the left guard keeps `task-…` and `disk-…` slugs out
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}")),
    ("GitHub fine-grained token", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}")),
    # a real key tail mixes digits and upper case; a kebab name (`sk-telecom-billing-…`) has neither
    ("sk- API key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}")),
    ("Google API key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
)
SECRET_SKIP_WORD = "EXAMPLE"  # documented sample keys (AWS docs) carry it; it never exempts a private key block
KEY_KIND = "private key block"  # the value of a private key is its body, so the whole block is the secret
KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
KEY_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
KEY_BODY_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
KEY_BODY_MIN_CHARS = 64
KEY_BODY_MIN_LINES = 3  # a body left behind without its BEGIN and END lines
REDACTED = "REDACTED"  # no brackets: a bracketed value would read as a placeholder
NAME_ID_RE = re.compile(r"\(([^()]*)\)\s*$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
NOT_IDS = ("running", "done", "pending", "none", "tbd", "todo")
NA_WORD = "n/a"  # the value of a git field outside git (--git no)
VOLATILE_PREFIXES = ("/tmp/claude-", "/private/tmp/claude-")  # background task output, cleared with /tmp
DRIVE_RE = re.compile(r"^(?:/mnt)?/([A-Za-z])(?=/|$)")  # /c/... and /mnt/c/... -> c:/...
JOBS_PART = "/.claude/jobs/"
SESSION_RE = re.compile(r"^\*\*Session:\*\*\s*(.*)$")
SESSION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")  # = run_log.SESSION_ID
RESUME_CALLER = "handoff_state.py"  # a resume checks a file another session wrote: no writing-side checks
AFTER_SECTION = "After Handoff"
QUOTE_PREFIX_CHARS = 40  # a message is recorded when 「 plus its first 40 characters (a shorter one 「whole」) is quoted
QUOTE_MAX_CHARS = 300  # match_quotes: a message up to this long is quoted whole
QUOTE_EDGE_CHARS = 40  # a longer one by an item holding its first QUOTE_HEAD_CHARS and its last this many characters
QUOTE_HEAD_CHARS = QUOTE_MAX_CHARS - QUOTE_EDGE_CHARS - 1  # 259: quote_line shows head, `…`, edge in QUOTE_MAX_CHARS
QUOTE_STRIP_MIN = 40  # a leading quote this long (marks included) is the assistant's output, not the user's text
QUOTE_OWN_MIN = 40  # ... cut only when the user's text after it is this long; a shorter one keeps the whole message
SHOWN_MAX = 5
BEFORE_SECTION = "Before Handoff"
BEFORE_MAX = 5  # the messages right before the last /handoff input that Before Handoff records, plus its arguments
UNRELATED, REFLECTED = "무관", "반영"  # a Before Handoff item ends `→ 무관` or `→ 반영: <section title>`
REVIEWS_SECTION = "Reviews"
REVIEW_WORDS = ("review", "critic", "judge")  # in the last `:` token of a subagent type
REVIEW_TYPES = ("architect", "verifier")  # equal to the last `:` token
WORKFLOW_TYPE = "workflow"  # every Workflow run is a review run
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
TRANSCRIPT_MAX_BYTES = 512 * 1024 * 1024
TRANSCRIPT_KEYS = ('"user"', '"assistant"', "queued_command")  # only lines holding one are parsed
COMMAND_TAGS = ("<command-", "<bash-", "<local-command-")  # a typed slash, ! or local command, not a message
PLUGIN_NAME = "devenv"  # = the `name` of .claude-plugin/plugin.json (pinned by a test)
# a typed /handoff: the bare name, or the plugin-qualified one; another plugin's `<x>:handoff` is not ours
HANDOFF_COMMANDS = ("<command-name>/handoff</command-name>", f"<command-name>/{PLUGIN_NAME}:handoff</command-name>")
TIME_RE = re.compile(r"T(\d{2}:\d{2}:\d{2})")


def code_slug(text: str) -> str:
    """Lower-case `text` with every run of non-alphanumerics as one `-`, no `-` at either end."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


FAIL_CODES = (  # the closed vocabulary of the run log's fail_codes: constants only, never message text
    ("decisions-table", "contract-toplevel", "contract-continues", "contract-closed", "contract-live-chain",
     "contract-goal-missing", "contract-goal-drift", "contract-session-state", "contract-next-step",
     "usage", "unreadable", "internal", "secret:other", "after-handoff", "contract-session",
     "contract-next-step-deps", "before-handoff", "reviews")
    + tuple(f"secret:{code_slug(label)}" for label, _ in SECRET_PATTERNS) + (f"secret:{code_slug(KEY_KIND)}",)
    + tuple(f"item-incomplete:{code_slug(title)}" for title in SECTIONS)
)
RUN: dict = {}  # what main() hands to log_run: lines, parts, size, fail; reset at the start of run()


def is_placeholder(text: str) -> bool:
    """True when `text` is an unfilled template value. `none` is a real value, not a placeholder."""
    s = text.strip()
    if not s or set(s) <= set("-—="):
        return True
    if s.lower() in PLACEHOLDER_WORDS:
        return True
    return (s[0], s[-1]) in (("[", "]"), ("<", ">"))


def is_key_body(line: str) -> bool:
    s = line.strip()
    return len(s) >= KEY_BODY_MIN_CHARS and bool(KEY_BODY_RE.match(s))


def key_blocks(lines: "list[str]") -> "list[tuple[int, int]]":
    """(first, last) 0-based line indexes of every private key block, in file order."""
    blocks, i = [], 0
    while i < len(lines):
        if KEY_BEGIN_RE.search(lines[i]):
            end = next((j for j in range(i, len(lines)) if KEY_END_RE.search(lines[j])), None)
            if end is None:  # no END line: the block runs to the last non-empty line of the file
                end = len(lines) - 1
                while end > i and not lines[end].strip():
                    end -= 1
            blocks.append((i, end))
            i = end + 1
            continue
        j = i
        while j < len(lines) and is_key_body(lines[j]):
            j += 1
        if j < len(lines) and KEY_END_RE.search(lines[j]):  # an END line and the body lines just above it
            blocks.append((i, j))
            i = j + 1
        elif j - i >= KEY_BODY_MIN_LINES:
            blocks.append((i, j - 1))
            i = j
        else:
            i = max(j, i + 1)
    return blocks


def redact_key_line(line: str) -> str:
    """A line of a key block as REDACTED, keeping its indentation, the text before BEGIN and after END."""
    begin, end = KEY_BEGIN_RE.search(line), KEY_END_RE.search(line)
    head = line[:begin.start()] if begin else line[:len(line) - len(line.lstrip())]
    return head + REDACTED + (line[end.end():] if end else "")


def redact(text: str) -> str:
    """`text` with every secret-like value, and every line of a private key block, replaced by REDACTED."""
    lines = text.split("\n")
    for first, last in key_blocks(lines):
        for i in range(first, last + 1):
            lines[i] = redact_key_line(lines[i])
    text = "\n".join(lines)
    for _, pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def secret_parts(lines: "list[str]") -> "list[tuple[str, str]]":
    """(kind label, message) per private key block, and per line and kind of token outside a block; a token
    line holding EXAMPLE is skipped. In line order. No value is kept."""
    blocks = key_blocks(lines)
    inside = {i for first, last in blocks for i in range(first, last + 1)}
    found = [(first, KEY_KIND, f"secret-like value ({KEY_KIND}) on lines {first + 1}-{last + 1}; "
                               f"replace the whole block with {REDACTED}") for first, last in blocks]
    found += [(n, kind, f"secret-like value ({kind}) on line {n + 1}; replace it with {REDACTED}")
              for n, line in enumerate(lines) if n not in inside and SECRET_SKIP_WORD not in line
              for kind, pattern in SECRET_PATTERNS if pattern.search(line)]
    return [(kind, msg) for _, kind, msg in sorted(found, key=lambda f: f[0])]


def secret_failures(lines: "list[str]") -> "list[str]":
    """The messages of secret_parts, in the same order."""
    return [msg for _, msg in secret_parts(lines)]


def secret_code(kind: str) -> str:
    code = f"secret:{code_slug(kind)}"
    return code if code in FAIL_CODES else "secret:other"


def is_absolute_path(text: str) -> bool:
    """True for POSIX, ~/ and Windows (C:\\, C:/, /c/, /mnt/c/) absolute paths; a sentence is not a path."""
    return bool(ABS_PATH_RE.match(text.strip()))


def path_key(text: str) -> str:
    """Comparable form of a path: `/` separators, no trailing slash; Windows forms lower-cased as `c:/...`."""
    s = text.strip().replace("\\", "/")
    s = DRIVE_RE.sub(lambda m: f"{m.group(1)}:", s, count=1)
    if len(s) > 1:
        s = s.rstrip("/") or "/"
    if re.match(r"^[A-Za-z]:(?:/|$)", s):
        s = s.lower()
    return s


def clean_path(text: str) -> str:
    """`/` separators with `/./` and repeated slashes collapsed, so a spelled-out path cannot dodge a check."""
    s = text.strip().replace("\\", "/")
    while "/./" in s:
        s = s.replace("/./", "/")
    return re.sub(r"/{2,}", "/", s)


def is_under_jobs(path: str) -> bool:
    """True when `path` has a /.claude/jobs/ part (that folder is deleted by cleanup)."""
    return JOBS_PART in path_key(clean_path(path)) + "/"


def is_volatile(path: str) -> bool:
    """True for background-task output under /tmp/claude-* (cleared with /tmp)."""
    return clean_path(path).startswith(VOLATILE_PREFIXES)


def item_id(name: str) -> "str | None":
    """The id in a `<name> (<id>)` value, or None when there is none or it cannot be an id."""
    m = NAME_ID_RE.search(name)
    value = m.group(1).strip() if m else ""
    if not ID_RE.match(value) or value.lower() in NOT_IDS:
        return None
    return value


def section_body(lines: "list[str]", title: str) -> "str | None":
    """Body of `## <title>`, or None when the section is absent."""
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == f"## {title}")
    except StopIteration:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end])


def session_fields(body: str) -> "dict[str, tuple[str, list[str]]]":
    """`- **Label:** value` lines of Session State -> label: (inline value, indented continuation lines)."""
    fields, current = {}, None
    for line in body.split("\n"):
        m = FIELD_RE.match(line)
        if m:
            current = m.group(1).strip()
            fields[current] = (m.group(2).strip(), [])
        elif current and (line.startswith((" ", "\t")) or not line.strip()):
            fields[current][1].append(line)
        else:
            current = None
    return fields


def live_items(block: "list[str]") -> "list[dict[str, str]]":
    """Parse `- name: ...` / `  key: value` lines into one dict per item.

    Only a bullet at the first bullet's indentation starts an item. A deeper bullet continues the last key,
    and an unknown `key:` line ends it, so neither creates a fake item nor changes a known value.
    """
    known = set(ITEM_KEYS) | set(SCHEDULED_KEYS) | set(OPTIONAL_KEYS)
    items, current, last, indent = [], None, None, None
    for line in block:
        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet and indent is None:
            indent = len(bullet.group(1))
        if bullet and len(bullet.group(1)) <= indent:
            current, last = {}, None
            items.append(current)
            rest = bullet.group(2).strip()
        elif bullet:
            if last:
                current[last] = f"{current[last]} {bullet.group(2).strip()}".strip()
            continue
        else:
            rest = line.strip()
        if not rest or current is None:
            continue
        m = re.match(r"^([a-z][a-z-]*):\s*(.*)$", rest)
        if m and m.group(1) in known:
            last = m.group(1)
            current[last] = m.group(2).strip()
        elif m:
            last = None
        elif last:
            current[last] = f"{current[last]} {rest}".strip()
    return items


def live_item_failures(label: str, items: "list[dict[str, str]]") -> "list[str]":
    problems, keys = [], LIVE_KEYS[label]
    for n, item in enumerate(items, 1):
        name = item.get("name", "").strip()
        who = f"{label} item '{name}'" if name and not is_placeholder(name) else f"{label} item #{n}"
        missing = [k for k in keys if k not in item]
        if missing:
            problems.append(f"{who} lacks key(s): {', '.join(missing)}")
        empty = [k for k in keys if k in item and is_placeholder(item[k])]
        if empty:
            problems.append(f"{who} has empty or placeholder value(s): {', '.join(empty)}")
        if label in ID_FIELDS and name and not is_placeholder(name) and item_id(name) is None:
            problems.append(f"{who} name lacks a usable id: write <name> (<id>), id of letters, digits, _ . : -")
        if "result" not in keys:
            continue
        result = item.get("result", "").strip("` ")
        if result and not is_placeholder(result):
            if not is_absolute_path(result):
                problems.append(f"{who} result is not an absolute path: {result[:60]}")
            elif is_under_jobs(result):
                problems.append(f"{who} result is under .claude/jobs/ (deleted on cleanup): copy it to a durable path")
            elif is_volatile(result):
                problems.append(f"{who} result is in a volatile location (/tmp/claude-*): copy it to a durable path")
    return problems


def session_state_failures(body: str, git: "bool | None" = None) -> "list[str]":
    """With git False (outside git) the git fields may be n/a or absent; the live fields are always required."""
    problems = []
    if is_placeholder(body):
        return ["Session State is empty"]
    fields = session_fields(body)
    for label in STATE_FIELDS:
        if label not in fields:
            if git is not False:
                problems.append(f"Session State lacks {label}")
            continue
        inline, rest = fields[label]
        value = " ".join([inline] + [ln.strip() for ln in rest])
        if is_placeholder(value) and not (git is False and value.strip().lower() == NA_WORD):
            problems.append(f"Session State field {label} has no value or a placeholder")
    if RETIRED_LIVE_FIELD in fields:
        problems.append(f"Session State uses the retired field {RETIRED_LIVE_FIELD}; use {', '.join(LIVE_FIELDS)}")
    for label in LIVE_FIELDS:
        if label not in fields:
            problems.append(f"Session State lacks {label}")
            continue
        inline, rest = fields[label]
        items = live_items(rest)
        if items:
            problems.extend(live_item_failures(label, items))
        elif not inline:
            problems.append(f"{label} has no value: write none or a list of items")
        elif inline.lower().split("(")[0].strip() not in NONE_WORDS:
            problems.append(f"{label} must be none or a list of items, got: {inline[:60]}")
    return problems


def head_value(lines: "list[str]", pattern: "re.Pattern[str]") -> "str | None":
    """First value of a `**Key:** value` line in the head (before the first `## `), or None when absent."""
    head = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    values = [m.group(1).strip().strip("`") for m in (pattern.match(l) for l in lines[:head]) if m]
    return values[0] if values else None


def toplevel_failures(lines: "list[str]") -> "list[str]":
    """The head carries `**Toplevel:** <absolute path>`."""
    value = head_value(lines, TOPLEVEL_RE)
    if value is None:
        return ["head line **Toplevel:** is missing (the project key handoff_state.py printed)"]
    if is_placeholder(value) or not is_absolute_path(value):
        return [f"**Toplevel:** is not an absolute path: {value[:60]}"]
    return []


def continues_path(lines: "list[str]") -> "Path | None":
    """The previous handoff `**Continues:**` names, when it is an existing file; None otherwise."""
    value = head_value(lines, CONTINUES_RE)
    if value is None or is_placeholder(value) or value.lower() == "none" or not is_absolute_path(value):
        return None
    path = Path(value).expanduser()
    return path if path.is_file() else None


def continues_failures(lines: "list[str]", self_path: "Path | None" = None) -> "list[str]":
    """The head carries `**Continues:** none` or the absolute path of an existing previous handoff:
    not this file itself, and of the same `**Toplevel:**` when both files carry one."""
    value = head_value(lines, CONTINUES_RE)
    if value is None:
        return ["head line **Continues:** is missing (none, or the previous handoff's absolute path)"]
    if value.lower() == "none":
        return []
    if is_placeholder(value) or not is_absolute_path(value):
        return [f"**Continues:** is neither none nor an absolute path: {value[:60]}"]
    prev = Path(value).expanduser()
    if not prev.is_file():
        return [f"**Continues:** names a file that does not exist: {value[:100]}"]
    if self_path is not None and prev.resolve() == self_path.resolve():
        return ["**Continues:** names this file itself; name the handoff this session started from, or none"]
    prev_lines = read_lines(prev)
    mine, theirs = head_value(lines, TOPLEVEL_RE), head_value(prev_lines or [], TOPLEVEL_RE)
    if mine and theirs and path_key(mine) != path_key(theirs):
        return [f"**Continues:** names a handoff of another **Toplevel:** ({theirs[:100]})"]
    return []


def read_lines(path: Path) -> "list[str] | None":
    """Lines of a file with secret-like values as REDACTED, or None when it cannot be read as UTF-8."""
    try:
        return redact(path.read_text(encoding="utf-8")).split("\n")
    except (OSError, UnicodeDecodeError):
        return None


def goal_fields(lines: "list[str]") -> "dict[str, str] | None":
    """`Goal (carried)` as key -> value (inline plus continuation lines), or None when the section is absent."""
    body = section_body(lines, GOAL_SECTION)
    if body is None:
        return None
    return {k: " ".join([inline] + [ln.strip() for ln in rest if ln.strip()]).strip()
            for k, (inline, rest) in session_fields(body).items()}


def constraint_entries(value: str) -> "list[str]":
    """Entries of a `Constraints` value: `none` or `없음` is no entry; `; ` separates several."""
    if value.strip().lower() in NONE_WORDS:
        return []
    return [e for e in CONSTRAINT_SPLIT_RE.split(value.strip()) if e.strip()]


def change_entries(value: str) -> "list[str]":
    """Entries of a `Changes` value: `none` is no entry; `| ` joins several."""
    if value.strip().lower() == "none":
        return []
    return [e for e in CHANGE_SPLIT_RE.split(value.strip()) if e.strip()]


def goal_parts(lines: "list[str]") -> "tuple[list[str], list[str]]":
    """Goal (carried) as (missing, drift): the five real values, then no silent drift from the previous handoff."""
    goal = goal_fields(lines)
    if goal is None:
        return [f"{GOAL_SECTION} section is missing ({', '.join(GOAL_KEYS)})"], []
    problems = []
    for key in GOAL_KEYS:
        if key not in goal:
            problems.append(f"{GOAL_SECTION} lacks {key}")
        elif is_placeholder(goal[key]):
            problems.append(f"{GOAL_SECTION} {key} has no value or a placeholder")
    if not problems and not is_absolute_path(goal["Origin"]):
        problems.append(f"{GOAL_SECTION} Origin does not start with an absolute path: {goal['Origin'][:60]}")
    if problems:
        return problems, []
    prev_path = continues_path(lines)
    prev_lines = read_lines(prev_path) if prev_path else None
    prev = goal_fields(prev_lines) if prev_lines else None
    if prev is None:
        return [], []
    # Only the keys the previous file carries are compared (an older file has no Constraints), and a
    # secret-like value masked as REDACTED on either side is not a change.
    goal = {k: redact(v) for k, v in goal.items()}
    prev = {k: redact(v) for k, v in prev.items() if k in GOAL_KEYS}
    before, now = change_entries(prev.get("Changes", "none")), change_entries(goal["Changes"])
    if len(now) < len(before):
        return [], [f"{GOAL_SECTION} Changes lost {len(before) - len(now)} entr(ies) of the previous handoff; entries are never removed"]
    if now[:len(before)] != before:
        problems.append(f"{GOAL_SECTION} Changes rewrote an entry of {prev_path.name}; earlier entries are copied unchanged, new ones appended")
    added = now[len(before):]
    if "Constraints" in prev:
        kept = constraint_entries(prev["Constraints"])
        if constraint_entries(goal["Constraints"])[:len(kept)] != kept and not any(CONSTRAINTS_CHANGE in e for e in added):
            problems.append(f"{GOAL_SECTION} Constraints dropped or rewrote an entry of {prev_path.name}; append new ones "
                            f"after it, or name {CONSTRAINTS_CHANGE} in a new Changes entry")
    if added:
        if "Origin" in prev and goal["Origin"] != prev["Origin"]:
            problems.append(f"{GOAL_SECTION} Origin differs from {prev_path.name}; Origin is copied unchanged")
        unquoted = [e[:60] for e in added if not QUOTE_RE.search(e)]
        return [], problems + [f"{GOAL_SECTION} Changes entry has no quoted user sentence: {e}" for e in unquoted]
    changed = [k for k in GOAL_SAME_KEYS if k in prev and goal[k] != prev[k]]
    if changed:
        problems.append(f"{GOAL_SECTION} changed without a Changes entry ({', '.join(changed)} differ from {prev_path.name})")
    return [], problems


def goal_failures(lines: "list[str]") -> "list[str]":
    """Goal (carried): five real values, and no silent drift from the previous handoff of the chain."""
    missing, drift = goal_parts(lines)
    return missing + drift


def live_keys(lines: "list[str]") -> "list[tuple[str, str]]":
    """(name, key) of every item in the three live fields; the key is the id, or the whole name without one."""
    body = section_body(lines, "Session State")
    fields = session_fields(body) if body else {}
    out = []
    for label in LIVE_FIELDS:
        for item in live_items(fields.get(label, ("", []))[1]):
            name = item.get("name", "").strip()
            if name and not is_placeholder(name):
                out.append((name, item_id(name) or name))
    return out


def closed_keys(lines: "list[str]") -> "set[str]":
    body = section_body(lines, CLOSED_SECTION) or ""
    pointers = [m for m in (CLOSED_RE.match(ln) for ln in body.split("\n")) if m]
    return {k.strip() for m in pointers for k in m.group(2).split(",") if k.strip()}


def live_chain_failures(lines: "list[str]") -> "list[str]":
    """Every live item of the previous handoff is carried (same id) or closed (its id among the Closed keys)."""
    prev_path = continues_path(lines)
    prev_lines = read_lines(prev_path) if prev_path else None
    if not prev_lines:
        return []
    kept = {key for _, key in live_keys(lines)} | closed_keys(lines)
    return [f"Live item {name} from the previous handoff is missing: keep an item with id {key} in a Live field, "
            f"or list {key} in a Closed (pointers) keys list"
            for name, key in live_keys(prev_lines) if key not in kept]


def closed_failures(lines: "list[str]") -> "list[str]":
    """Closed (pointers), when present: each pointer names an existing absolute history file holding its keys."""
    body = section_body(lines, CLOSED_SECTION)
    if body is None:
        return []
    pointers = [m for m in (CLOSED_RE.match(ln) for ln in body.split("\n")) if m]
    if not pointers:
        return [f"{CLOSED_SECTION} has no `- <N> items closed → <history file> (keys: a, b)` line; omit the section when nothing was closed"]
    problems = []
    for m in pointers:
        target, keys = m.group(1).strip("` "), [k.strip() for k in m.group(2).split(",") if k.strip()]
        if not is_absolute_path(target):
            problems.append(f"{CLOSED_SECTION} history path is not absolute: {target[:60]}")
            continue
        path = Path(target).expanduser()
        if not path.is_file():
            problems.append(f"{CLOSED_SECTION} history file does not exist: {target[:100]}")
            continue
        if not keys:
            problems.append(f"{CLOSED_SECTION} lists no keys for {path.name}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            problems.append(f"{CLOSED_SECTION} history file is unreadable: {target[:100]}")
            continue
        missing = [k for k in keys if f" · {k} · " not in text]
        if missing:
            problems.append(f"{CLOSED_SECTION} keys not found in {path.name}: {', '.join(missing)}")
    return problems


def next_step_missing(lines: "list[str]") -> bool:
    """True when Exact Next Step is absent, empty, only placeholder lines, or one [...] or <...> block."""
    nxt = section_body(lines, "Exact Next Step") or ""
    real = [ln for ln in nxt.split("\n") if not is_placeholder(ln)]
    block = "\n".join(real).strip()
    return not real or (block[:1], block[-1:]) in (("[", "]"), ("<", ">"))


STEP_RE = re.compile(r"^(\d+)\.\s")  # an unindented numbered line; bullets and nested lines are not steps
USER_STEP = "\U0001F534"  # 🔴
DEP_RE = re.compile(r"\((?:독립|independent|(?:←|<-)\s*(\d+))\)", re.I)


def next_step_dep_failures(lines: "list[str]") -> "list[str]":
    """Every numbered step after the first 🔴 user step carries (독립), (independent), or (← n) of a smaller step n."""
    steps: "list[tuple[int, str]]" = []
    for line in (section_body(lines, "Exact Next Step") or "").split("\n"):
        m = STEP_RE.match(line)
        if m:
            steps.append((int(m.group(1)), line))
        elif steps and line.startswith((" ", "\t")):
            steps[-1] = (steps[-1][0], f"{steps[-1][1]} {line.strip()}")
    user = next((n for n, text in steps if USER_STEP in text), None)
    if user is None:
        return []
    return [f"Exact Next Step step {n} comes after the {USER_STEP} user step {user} and lacks (독립) or (← n)"
            for n, text in steps
            if n > user and not any(m.group(1) is None or int(m.group(1)) < n for m in DEP_RE.finditer(text))]


def resume_contract_parts(lines: "list[str]", git: "bool | None" = None,
                          path: "Path | None" = None) -> "list[tuple[str, list[str]]]":
    """The resume-contract checks as (code, messages), in the order resume_contract_failures lists them."""
    goal_missing, goal_drift = goal_parts(lines)
    parts = [("contract-toplevel", toplevel_failures(lines)), ("contract-continues", continues_failures(lines, path)),
             ("contract-goal-missing", goal_missing), ("contract-goal-drift", goal_drift),
             ("contract-closed", closed_failures(lines)), ("contract-live-chain", live_chain_failures(lines))]
    problems = []
    state = section_body(lines, "Session State")
    if state is not None:
        problems.extend(session_state_failures(state, git))
    elif git:
        problems.append("Session State section is missing inside a git repository")
    elif git is False:
        problems.append("Session State section is missing: outside git it still carries the three Live fields")
    parts.append(("contract-session-state", problems))
    if next_step_missing(lines):
        parts.append(("contract-next-step", ["Exact Next Step is missing, empty, or only placeholders"]))
    return parts


def resume_contract_failures(lines: "list[str]", git: "bool | None" = None, path: "Path | None" = None) -> "list[str]":
    """What /resume-handoff needs from the file itself. Empty list when satisfied."""
    return [msg for _, msgs in resume_contract_parts(lines, git, path) for msg in msgs]


def find_transcript(session_id: str) -> "Path | None":
    """~/.claude/projects/*/<id>.jsonl for a UUID: the most recently modified copy, on a tie the first path."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    try:
        found = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    except (OSError, RuntimeError, KeyError):  # no home
        return None
    best, newest = None, 0.0
    for p in found:
        try:
            mtime = p.stat().st_mtime
        except (OSError, RuntimeError):  # a copy gone between glob and stat, or a symlink loop: skip that one
            continue
        if best is None or mtime > newest:
            best, newest = p, mtime
    return best


def content_text(content) -> str:
    """A message content as text: the string itself, or its text blocks joined; anything else is no text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def answers_text(rec: dict) -> str:
    """A dialog answer (toolUseResult.answers) as its values joined by ` / `, a list of strings by `, `."""
    result = rec.get("toolUseResult")
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        return ""
    parts = []
    for value in answers.values():
        if isinstance(value, list):
            value = ", ".join(v for v in value if isinstance(v, str))
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " / ".join(parts)


def record_text(rec: dict) -> str:
    """The text a record carries: a queued message's prompt, a dialog answer, else the message content."""
    if rec.get("type") == "attachment":
        att = rec.get("attachment")
        prompt = att.get("prompt") if isinstance(att, dict) else None
        return content_text(prompt)
    message = rec.get("message")
    return answers_text(rec) or content_text(message.get("content") if isinstance(message, dict) else None)


def handoff_command(text: str) -> "str | None":
    """The HANDOFF_COMMANDS entry that occurs first in `text`, else None."""
    found = [(text.find(cmd), cmd) for cmd in HANDOFF_COMMANDS if cmd in text]
    return min(found)[1] if found else None


def classify_record(rec: dict) -> "str | None":
    """"baseline" for a /handoff input, "message" for a message the user gave the session, else None.
    The first matching row decides (new-format-spec section 20)."""
    kind = rec.get("type")
    if kind == "assistant":
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                given = block.get("input")
                skill = given.get("skill") if isinstance(given, dict) else None
                if isinstance(skill, str) and (skill == "handoff" or skill.endswith(":handoff")):
                    return "baseline"
        return None
    if kind == "attachment":
        att = rec.get("attachment")
        if not isinstance(att, dict) or att.get("type") != "queued_command":
            return None
        origin, prompt = att.get("origin"), att.get("prompt")
        human = isinstance(origin, dict) and origin.get("kind") == "human"
        return "message" if human and content_text(prompt).strip() else None
    if kind != "user":
        return None
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = content_text(content).lstrip()
    if not rec.get("isMeta") and text.startswith("<command-") and handoff_command(text):
        return "baseline"
    if rec.get("isMeta") or rec.get("promptSource") == "system":
        return None
    if answers_text(rec):
        return "message"
    origin = rec.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    if text.startswith(COMMAND_TAGS) or not text.strip():
        return None
    return "message"


def normalize(text: str) -> str:
    """Secret-like values as REDACTED, NFC, every run of whitespace as one space."""
    return " ".join(unicodedata.normalize("NFC", redact(text)).split())


def message_time(rec: dict) -> str:
    """A queued message's attachment timestamp, else the record timestamp; "" when absent."""
    att = rec.get("attachment")
    ts = att.get("timestamp") if rec.get("type") == "attachment" and isinstance(att, dict) else rec.get("timestamp")
    return ts if isinstance(ts, str) else ""


def is_review_type(subagent_type) -> bool:
    """True when the last `:` token of an Agent's subagent_type holds a REVIEW_WORDS word or is one of REVIEW_TYPES."""
    if not isinstance(subagent_type, str):
        return False
    last = subagent_type.rsplit(":", 1)[-1].lower()
    return any(word in last for word in REVIEW_WORDS) or last in REVIEW_TYPES


def fold_review_run(rec: dict, n: int, launched: dict, runs: dict) -> "str | None":
    """Fold record number n into the review runs. A Workflow launch, or an Agent launch of a review type (assistant
    tool_use), waits in `launched` (tool_use id -> (n, type)) until the tool_result record carries its run id in
    toolUseResult: runId for a Workflow, agentId for an Agent. A string result (a failed launch) counts nothing.
    `runs` maps a run id to (launch number, type), the first launch of an id only. Returns why a dict result went
    uncounted (it carries no run id), else None."""
    lost = None
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if rec.get("type") == "assistant" and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
            given = block.get("input")
            if block.get("name") == "Workflow":
                launched[block["id"]] = (n, WORKFLOW_TYPE)
            elif block.get("name") == "Agent" and isinstance(given, dict) and is_review_type(given.get("subagent_type")):
                launched[block["id"]] = (n, given["subagent_type"])
        elif rec.get("type") == "user" and block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
            start, kind = launched.pop(block["tool_use_id"], (None, None))
            result = rec.get("toolUseResult")
            rid = result.get("runId" if kind == WORKFLOW_TYPE else "agentId") if kind and isinstance(result, dict) else None
            if isinstance(rid, str) and rid.split() == [rid]:
                runs.setdefault(rid, (start, kind))
            elif kind and isinstance(result, dict):
                lost = "runId missing from a Workflow result" if kind == WORKFLOW_TYPE else "agentId missing from an Agent result"
    return lost


def handoff_args(rec: dict) -> "list[str]":
    """[the non-empty `<command-args>` after the handoff command name (HANDOFF_COMMANDS) of a typed /handoff input],
    else []."""
    message = rec.get("message") if rec.get("type") == "user" else None
    text = content_text(message.get("content")) if isinstance(message, dict) else ""
    command = handoff_command(text)
    m = ARGS_RE.search(text.split(command, 1)[1]) if command else None
    return [m.group(1)] if m and m.group(1).strip() else []


def scan_transcript(path: Path) -> dict:
    """One pass over a session transcript (new-format-spec section 21). Raw (text, timestamp) of each message after
    the last /handoff input (`after`) and of the BEFORE_MAX messages right before it, counted past earlier /handoff
    inputs (`before`); that input's arguments as `(text, timestamp)`, none or one (`args`), and its timestamp
    (`baseline_ts`); the review runs of the whole transcript as (id, type) in launch order (`reviews`); why the
    messages (`msg_reason`) or the review runs (`review_reason`) cannot be judged, else None; and why a review launch
    went uncounted (`review_note`, fold_review_run), else None. Never raises."""
    found = {"after": [], "before": [], "args": [], "baseline_ts": None, "reviews": [], "review_note": None}
    try:
        if path.stat().st_size > TRANSCRIPT_MAX_BYTES:
            return dict(found, msg_reason="transcript too large", review_reason="transcript too large")
        after, recent, before, args, stamp = [], [], [], [], None
        human, baseline, launched, runs, note = False, False, {}, {}, None
        with path.open(encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if not any(key in line for key in TRANSCRIPT_KEYS):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                human = human or (rec.get("type") == "user" and isinstance(rec.get("origin"), dict))
                note = fold_review_run(rec, n, launched, runs) or note
                label = classify_record(rec)
                if label == "baseline":
                    after, before, baseline = [], recent[-BEFORE_MAX:], True
                    stamp = message_time(rec) or None
                    args = [(text, stamp or "") for text in handoff_args(rec)]
                elif label == "message":
                    msg = (record_text(rec), message_time(rec))
                    after.append(msg)
                    recent = (recent + [msg])[-BEFORE_MAX:]
        if not human:
            return dict(found, msg_reason="transcript format unknown", review_reason="transcript format unknown")
        reviews = [(rid, kind) for rid, (_, kind) in sorted(runs.items(), key=lambda run: run[1][0])]
        if not baseline:
            return dict(found, reviews=reviews, review_note=note, msg_reason="no /handoff input in transcript",
                        review_reason=None)
        return {"after": after, "before": before, "args": args, "baseline_ts": stamp, "reviews": reviews,
                "review_note": note, "msg_reason": None, "review_reason": None}
    except OSError:
        return dict(found, msg_reason="transcript unreadable", review_reason="transcript unreadable")
    except Exception:  # a record shape this code does not know: the gates step aside, the run goes on
        return dict(found, msg_reason="transcript format unknown", review_reason="transcript format unknown")


def split_quote(raw: str) -> "tuple[str, bool]":
    """(the user's own text, whether a leading quote was cut). After leading whitespace, a quote of the assistant's
    output opens the message: `'` to the next `'`, `「` to the first `」`, or a run of lines opening with `>`. It is cut
    when it is QUOTE_STRIP_MIN characters or more (marks included, normalized) and QUOTE_OWN_MIN characters or more
    (normalized) follow it. Any other message, one opening with `<pasted_content` (it has no end mark) included, is all
    the user's own text."""
    text = raw.lstrip()
    if text[:1] in ("'", "「"):
        end = text.find("'" if text[0] == "'" else "」", 1)
        if end < 0:
            return raw, False
        quote, rest = text[:end + 1], text[end + 1:]
    elif text.startswith(">"):
        lines = text.split("\n")
        k = next((i for i, line in enumerate(lines) if not line.startswith(">")), len(lines))
        quote, rest = "\n".join(lines[:k]), "\n".join(lines[k:])
    else:
        return raw, False
    if len(normalize(quote)) < QUOTE_STRIP_MIN or len(normalize(rest)) < QUOTE_OWN_MIN:
        return raw, False
    return rest, True


def own_text(raw: str) -> str:
    """The user's own text of a message, before normalizing: split_quote without the flag."""
    return split_quote(raw)[0]


def own_messages(pairs: "list[tuple[str, str]]") -> "list[tuple[str, bool, str]]":
    """(normalized own text, whether a leading quote was cut, timestamp) per raw (text, timestamp); empty ones dropped."""
    out = []
    for text, ts in pairs:
        own, cut = split_quote(text)
        n = normalize(own)
        if n:
            out.append((n, cut, ts))
    return out


def read_session(path: "Path | None") -> dict:
    """scan_transcript with every message as own_messages gives it and the arguments as (normalized text, False,
    timestamp). Both reasons are "transcript not found" when path is None. Never raises."""
    if path is None:
        return {"after": [], "before": [], "args": [], "baseline_ts": None, "reviews": [], "review_note": None,
                "msg_reason": "transcript not found", "review_reason": "transcript not found"}
    got = scan_transcript(path)
    try:
        args = [(normalize(text), False, ts) for text, ts in got["args"]]
        return dict(got, after=own_messages(got["after"]), before=own_messages(got["before"]),
                    args=[arg for arg in args if arg[0]])
    except Exception:
        return {"after": [], "before": [], "args": [], "baseline_ts": None, "reviews": [], "review_note": None,
                "msg_reason": "transcript format unknown", "review_reason": "transcript format unknown"}


def messages_after_handoff(path: Path) -> "tuple[list[tuple[str, str]], str | None]":
    """(normalized text, timestamp) of each message after the last /handoff input, by position in the file,
    a text given twice listed twice; or ([], reason) when the transcript cannot be judged. Never raises.
    The 2026.11.09-3 view: the whole text, a leading quote kept; the gates use read_session."""
    got = scan_transcript(path)
    if got["msg_reason"]:
        return [], got["msg_reason"]
    msgs = [(normalize(text), ts) for text, ts in got["after"]]
    return [(text, ts) for text, ts in msgs if text], None


def unrecorded(msgs: "list[tuple[str, str]]", section_text: str) -> "list[tuple[str, str]]":
    """The messages the section does not quote. A message is quoted by a list item opening with `- 「` plus its
    first QUOTE_PREFIX_CHARS characters (a shorter one `- 「whole」`); k messages with the same quote need k items,
    the first ones count. A quote nested inside another item's text does not count."""
    body = normalize(section_text)
    used, out = {}, []
    for text, ts in msgs:
        n = normalize(text)
        needle = "- 「" + (n[:QUOTE_PREFIX_CHARS] if len(n) >= QUOTE_PREFIX_CHARS else n + "」")
        used[needle] = used.get(needle, 0) + 1
        if used[needle] > body.count(needle):
            out.append((text, ts))
    return out


def disposition(item: str, titles: "set[str]") -> "str | None":
    """UNRELATED or REFLECTED by the text after the item's last `→ `: `무관` (text may follow after a space) or
    `반영: <a title in titles>` exactly; None for anything else."""
    tail = item.rsplit("→ ", 1)[1] if "→ " in item else ""
    if tail == UNRELATED or tail.startswith(UNRELATED + " "):
        return UNRELATED
    if tail.startswith(REFLECTED + ": ") and tail[len(REFLECTED) + 2:] in titles:
        return REFLECTED
    return None


def quotes_message(item: str, text: str, cut: bool) -> bool:
    """A normalized `- 「` item quotes a message's own text, as quote_line shows it. Over QUOTE_MAX_CHARS: its first
    QUOTE_HEAD_CHARS characters and its last QUOTE_EDGE_CHARS right before 」. Else whole: `「text」`, opening the item
    when shorter than QUOTE_EDGE_CHARS, or `text」` when a leading quote was cut (the quote copied along)."""
    if len(text) > QUOTE_MAX_CHARS:
        return text[:QUOTE_HEAD_CHARS] in item and text[-QUOTE_EDGE_CHARS:] + "」" in item
    if len(text) < QUOTE_EDGE_CHARS:
        return item.startswith("- 「" + text + "」")
    return (text if cut else "「" + text) + "」" in item


def match_quotes(msgs: "list[tuple[str, bool, str]]", section_text: str,
                 titles: "set[str] | None" = None) -> "tuple[list[tuple[str, str]], int]":
    """(the messages the section does not quote as (text, timestamp), how many matched items end in `→ 무관`).
    msgs are read_session's (own text, quote cut, timestamp). Each `- 「` list item quotes one message at most: a
    message takes the first unused item that quotes it (quotes_message) and, with titles (Before Handoff), carries a
    disposition naming one of them (new-format-spec section 21)."""
    free = [item for item in (normalize(i) for i in items_of(section_text.split("\n"))) if item.startswith("- 「")]
    missing, unrelated = [], 0
    for text, cut, ts in msgs:
        n = normalize(text)
        hit = next((i for i, item in enumerate(free) if quotes_message(item, n, cut)
                    and (titles is None or disposition(item, titles))), None)
        if hit is None:
            missing.append((text, ts))
            continue
        item = free.pop(hit)
        if titles is not None and disposition(item, titles) == UNRELATED:
            unrelated += 1
    return missing, unrelated


def reflect_titles(lines: "list[str]") -> "set[str]":
    """The `## ` section titles a Before Handoff item may name after `→ 반영: `: all but Before and After Handoff."""
    titles = {normalize(l.strip()[3:]) for l in lines if l.strip().startswith("## ")}
    return titles - {BEFORE_SECTION, AFTER_SECTION}


def unrecorded_reviews(reviews: "list[tuple[str, str]]", section_text: str) -> "list[tuple[str, str]]":
    """The review runs (id, type) without an item `- <id> · <type> · <verdict> · <adopted/rejected>` in the section:
    a normalized list item opening with `- <id> · `, four or more ` · ` cells, the third and fourth not placeholders."""
    items = [normalize(item) for item in items_of(section_text.split("\n"))]
    def recorded(rid):
        cells = [item[2:].split(" · ") for item in items if item.startswith(f"- {rid} · ")]
        return any(len(c) >= 4 and not is_placeholder(c[2]) and not is_placeholder(c[3]) for c in cells)
    return [(rid, kind) for rid, kind in reviews if not recorded(rid)]


def quote_line(text: str, ts: str) -> str:
    """`「<text>」 (HH:MM:SSZ)`; past QUOTE_MAX_CHARS the text is its first QUOTE_HEAD_CHARS characters, `…` and its
    last QUOTE_EDGE_CHARS, QUOTE_MAX_CHARS in all, so the line as shown still quotes the message."""
    n = normalize(text)
    shown = n if len(n) <= QUOTE_MAX_CHARS else n[:QUOTE_HEAD_CHARS] + "…" + n[-QUOTE_EDGE_CHARS:]
    m = TIME_RE.search(ts)
    return f"「{shown}」" + (f" ({m.group(1)}Z)" if m else "")


def shown_quotes(msgs: "list[tuple[str, str]]") -> "list[str]":
    """quote_line for the first SHOWN_MAX messages, then `… and N more`."""
    out = [quote_line(text, ts) for text, ts in msgs[:SHOWN_MAX]]
    if len(msgs) > SHOWN_MAX:
        out.append(f"… and {len(msgs) - SHOWN_MAX} more")
    return out


def elapsed_ms(ts: "str | None") -> "int | None":
    """Milliseconds from an ISO timestamp to now; None when it does not parse, has no zone, or lies ahead."""
    try:
        ms = int((datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() * 1000)
    except (AttributeError, TypeError, ValueError):
        return None
    return ms if ms >= 0 else None


def writer_session() -> "str | None":
    """This session's id when the checker runs on the writing side: CLAUDE_CODE_SESSION_ID is a UUID and the
    caller is not handoff_state.py."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not SESSION_ID.fullmatch(sid) or os.environ.get("HANDOFF_CALLER") == RESUME_CALLER:
        return None
    return sid


def session_failures(lines: "list[str]", session_id: str) -> "list[str]":
    """The head carries `**Session:** <this session's id>`."""
    if head_value(lines, SESSION_RE) != session_id:
        return [f"**Session:** must be this session's id {session_id}"]
    return []


def handoff_gates(session_id: str, lines: "list[str]") -> dict:
    """The transcript gates of a handoff file on one read of the session's transcript (new-format-spec section 21):
    `after` and `before`, the messages After Handoff and Before Handoff do not quote, as (text, timestamp); `reviews`,
    the review runs Reviews does not record, as (id, type); `msg_reason` and `review_reason`, why either side was not
    checked, else None; `review_note`, why a review launch went uncounted, else None; and the run-log counts
    `after_lines`, `before_lines`, `before_unrelated`, `reviews_found`, `reviews_recorded` (None for a side not
    checked) with `baseline_ts`. Never raises."""
    empty = {"after": [], "before": [], "reviews": [], "after_lines": None, "before_lines": None,
             "before_unrelated": None, "reviews_found": None, "reviews_recorded": None, "baseline_ts": None,
             "review_note": None}
    try:
        got = read_session(find_transcript(session_id))
        out = dict(empty, msg_reason=got["msg_reason"], review_reason=got["review_reason"], baseline_ts=got["baseline_ts"],
                   review_note=got["review_note"])
        if not got["msg_reason"]:
            targets = got["before"] + got["args"]
            out["after"] = match_quotes(got["after"], section_body(lines, AFTER_SECTION) or "")[0]
            out["before"], out["before_unrelated"] = match_quotes(
                targets, section_body(lines, BEFORE_SECTION) or "", reflect_titles(lines))
            out["after_lines"], out["before_lines"] = len(got["after"]), len(targets)
        if not got["review_reason"]:
            out["reviews"] = unrecorded_reviews(got["reviews"], section_body(lines, REVIEWS_SECTION) or "")
            out["reviews_found"] = len(got["reviews"])
            out["reviews_recorded"] = len(got["reviews"]) - len(out["reviews"])
        return out
    except Exception:
        return dict(empty, msg_reason="transcript format unknown", review_reason="transcript format unknown")


def after_handoff_check(session_id: str, lines: "list[str]") -> "tuple[list[tuple[str, str]], str | None]":
    """(messages after the last /handoff missing from After Handoff, None), or ([], why they were not checked)."""
    gates = handoff_gates(session_id, lines)
    return ([], gates["msg_reason"]) if gates["msg_reason"] else (gates["after"], None)


def split_cells(line: str) -> "list[str]":
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [c.strip() for c in re.split(r"(?<!\\)\|", inner)]


def decisions_failures(lines: "list[str]") -> "list[str]":
    """Decisions Made table: header, separator, and data rows with three filled cells. Empty when absent or the no-decisions sentence alone."""
    body = section_body(lines, "Decisions Made")
    if body is None:
        return []
    rows = [ln for ln in body.split("\n") if ln.strip().startswith("|")]
    if not rows:
        text = [ln.strip() for ln in body.split("\n") if ln.strip() and not set(ln.strip()) <= set("-—=")]
        if len(text) == 1 and (text[0].rstrip(".").strip() == NO_DECISIONS.rstrip(".")
                               or text[0].lower() in NO_DECISIONS_WORDS):
            return []
        if not text:
            return ["Decisions Made has no table rows"]
        return [f'Decisions Made has prose but no table: add a | What | Why | How | table, or write only "{NO_DECISIONS}"']
    if len(rows) < 2 or not SEPARATOR_RE.match(rows[1].strip()):
        return ["Decisions Made table lacks its header and separator rows (| What | Why | How |)"]
    if len(rows) == 2:
        return ["Decisions Made table has no data rows"]
    problems = []
    for n, row in enumerate(rows[2:], 1):
        cells = split_cells(row)
        if len(cells) != 3:
            problems.append(f"Decisions Made row {n} has {len(cells)} cell(s), expected 3 (What | Why | How)")
            continue
        bad = [name for name, cell in zip(("What", "Why", "How"), cells) if is_placeholder(cell)]
        if bad:
            problems.append(f"Decisions Made row {n} has empty or placeholder cell(s): {', '.join(bad)}")
    return problems


def items_of(section_lines: "list[str]") -> "list[str]":
    items, current = [], []
    for line in section_lines:
        if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line):
            if current:
                items.append("\n".join(current))
            current = [line]
        elif current and (line.startswith((" ", "\t")) or line.strip() == ""):
            current.append(line)
        else:
            if current:
                items.append("\n".join(current))
                current = []
    if current:
        items.append("\n".join(current))
    return items


def missing_labels(item: str) -> "list[str]":
    """The what/why/how labels the item lacks; empty when it names a design file."""
    low = item.lower()
    if DESIGN_PATH_RE.search(item):
        return []
    return [pair[0].rstrip(":") for pair in LABELS if not any(label in low for label in pair)]


def passes(item: str) -> bool:
    return not missing_labels(item)


def parse_argv(argv: "list[str]") -> "tuple[str, bool | None] | None":
    """(session file, git flag or None), or None on a usage error."""
    files, git, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--git" and i + 1 < len(argv) and argv[i + 1] in ("yes", "no"):
            git, i = argv[i + 1] == "yes", i + 2
        else:
            files.append(argv[i])
            i += 1
    return (files[0], git) if len(files) == 1 else None


def main(argv: "list[str]") -> int:
    parsed = parse_argv(argv)
    if parsed is None:
        RUN["fail"] = "usage"
        print(__doc__)
        return 2
    path, git = Path(parsed[0]).expanduser(), parsed[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        RUN["fail"] = "unreadable"
        print(f"cannot read {path}: {exc}")
        return 2
    secret_pairs = secret_parts(text.split("\n"))
    secrets = [msg for _, msg in secret_pairs]
    lines = redact(text).split("\n")  # no message below can print a value, whole or cut
    RUN["lines"] = lines
    failures = []
    for title in SECTIONS:
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == f"## {title}")
        except StopIteration:
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        for item in items_of(lines[start + 1:end]):
            flat = " ".join(item.split())
            if len(flat) < MIN_ITEM_CHARS or flat.lower().startswith(("- none", "- 없음", "- no ")):
                continue
            if not passes(item):
                failures.append((title, flat[:120] + f" (missing: {', '.join(missing_labels(item))})"))
    decisions = decisions_failures(lines)
    contract_pairs = resume_contract_parts(lines, git, path)
    session, after, after_reason, before, reviews, review_reason = writer_session(), [], None, [], [], None
    if session:  # the writing side: the head names this session, and its messages and review runs are recorded
        contract_pairs.insert(2, ("contract-session", session_failures(lines, session)))
        contract_pairs.insert(3, ("contract-next-step-deps", next_step_dep_failures(lines)))
        gates = handoff_gates(session, lines)
        after, after_reason, before = gates["after"], gates["msg_reason"], gates["before"]
        reviews, review_reason = gates["reviews"], gates["review_reason"] or gates["review_note"]
        RUN["session"] = session
        RUN["counts"] = {"after_handoff_lines": gates["after_lines"], "before_handoff_lines": gates["before_lines"],
                         "before_unrelated": gates["before_unrelated"], "reviews_found": gates["reviews_found"],
                         "reviews_recorded": gates["reviews_recorded"], "elapsed_ms": elapsed_ms(gates["baseline_ts"])}
    contract = [msg for _, msgs in contract_pairs for msg in msgs]
    RUN["parts"] = ([(secret_code(kind), [msg]) for kind, msg in secret_pairs]
                    + [(f"item-incomplete:{code_slug(title)}", [head]) for title, head in failures]
                    + [("decisions-table", decisions)] + contract_pairs + [("after-handoff", [t for t, _ in after])]
                    + [("before-handoff", [t for t, _ in before]), ("reviews", [rid for rid, _ in reviews])])
    if secrets:  # first: the resume excerpt keeps only the first lines of this output
        print(f"[FAIL] {len(secrets)} secret-like value(s):")
        for msg in secrets:
            print(f"  - {msg}")
    if failures:
        print(f"[FAIL] {len(failures)} item(s) lack what/why/how or a design-file path:")
        for title, head in failures:
            print(f"  - [{title}] {head}")
    if decisions:
        print(f"[FAIL] {len(decisions)} Decisions table problem(s):")
        for msg in decisions:
            print(f"  - {msg}")
    if contract:
        print(f"[FAIL] {len(contract)} resume-contract problem(s):")
        for msg in contract:
            print(f"  - {msg}")
    if after:
        print(f'[FAIL] {len(after)} user message(s) after the last /handoff are not in "{AFTER_SECTION}":')
        for line in shown_quotes(after):
            print(f"  - {line}")
    if before:
        print(f'[FAIL] {len(before)} user message(s) before the last /handoff are not in "{BEFORE_SECTION}" '
              f"(write - 「…」 → {REFLECTED}: <section> | {UNRELATED}):")
        for text, ts in before:
            print(f"  - {quote_line(text, ts)}")
    if reviews:
        print(f'[FAIL] {len(reviews)} review run(s) of this session are not in "{REVIEWS_SECTION}" '
              "(fill <verdict> and <채택/기각>):")
        for rid, kind in reviews:
            print(f"  - {rid} · {normalize(kind)} · <verdict> · <채택/기각>")
    code = 1 if failures or decisions or contract or secrets or after or before or reviews else 0
    if not code:
        print("[PASS] open items carry what/why/how or a design path; Decisions table and resume contract satisfied; "
              "no secret-like values")
    size = len(text.encode("utf-8"))
    RUN["size"] = size
    if size > SIZE_LIMIT_KIB * 1024:
        print(f"WARNING: handoff file is {-(-size // 1024)} KiB (over {SIZE_LIMIT_KIB} KiB)")
    if after_reason:
        print(f"WARNING: after-handoff messages not checked: {after_reason}")
    if review_reason:
        print(f"WARNING: reviews not checked: {review_reason}")
    return code


class _Tee:
    """Writes through to a stream and keeps a copy, so the run log gets the exact stdout lines."""

    def __init__(self, stream):
        self.stream, self.parts = stream, []

    def write(self, s):
        self.parts.append(s)
        return self.stream.write(s)

    def flush(self):
        self.stream.flush()

    def lines(self) -> "list[str]":
        """Split on "\\n" only: splitlines() would also split on U+2028 and the like inside an indented line."""
        lines = "".join(self.parts).split("\n")
        return lines[:-1] if lines[-1] == "" else lines


def count_fail_codes(parts: "list[tuple[str, list[str]]]") -> "dict[str, int]":
    """code -> number of messages, in the order the codes first appear; a code without messages is left out."""
    counts: dict = {}
    for code, messages in parts:
        if messages:
            counts[code] = counts.get(code, 0) + len(messages)
    return counts


def project_of(lines: "list[str] | None") -> "str | None":
    """The head `**Toplevel:**` value when it is an absolute path to a directory that exists here, else None."""
    value = head_value(lines, TOPLEVEL_RE) if lines else None
    if not value or is_placeholder(value) or not is_absolute_path(value):
        return None
    try:
        return value if Path(value).expanduser().is_dir() else None
    except OSError:  # EACCES, ENAMETOOLONG and the like: not a folder here
        return None


def log_run(argv: "list[str]", code: int, stdout_lines: "list[str]") -> None:
    """Append one run record; a failure only prints a WARNING line on stderr. Every field is computed in the try."""
    try:
        parsed = parse_argv(argv)
        fail = RUN.get("fail")
        fail_codes = {fail: 1} if fail else count_fail_codes(RUN.get("parts", []))
        spec = importlib.util.spec_from_file_location("run_log", Path(__file__).with_name("run_log.py"))
        run_log = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_log)
        record_args = dict(
            script="check_handoff.py", mode="check", cwd=str(Path.cwd()), toplevel=None, args=argv,
            exit_code=code, stdout_lines=stdout_lines,
            handoff=run_log.handoff_path(parsed[0]) if parsed else None,
            project=project_of(RUN.get("lines")), fail_codes=fail_codes, handoff_bytes=RUN.get("size"),
            **RUN.get("counts", {}),
        )
        if RUN.get("session"):  # the writing side: which try at this file this run is
            record_args["attempt_n"] = run_log.attempt_number(RUN["session"], record_args["handoff"])
        reason = run_log.append_run(run_log.build_record(**record_args))
    except Exception as exc:  # a missing or broken run_log.py never changes the result
        reason = f"{type(exc).__name__}: {exc}"
    if reason:
        sys.stderr.write(f"WARNING: run log not written: {reason}\n")


def run(argv: "list[str]") -> int:
    """main() plus the run log; an unexpected exception prints its traceback tail and exits 2."""
    RUN.clear()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    tee = _Tee(sys.stdout)
    sys.stdout = tee
    try:
        code = main(argv)
    except Exception:
        tail = traceback.format_exc().rstrip("\n").splitlines()[-5:]
        sys.stderr.write(redact("\n".join(tail)) + "\n")
        RUN["fail"] = "internal"
        code = 2
    finally:
        sys.stdout = tee.stream
    log_run(argv, code, tee.lines())
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
