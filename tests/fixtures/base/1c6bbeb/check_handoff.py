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

A file over SIZE_LIMIT_KIB prints one `WARNING: handoff file is <N> KiB (over 16 KiB)` line after the result
block; the exit code is unchanged.
Every run appends one record to the run log (run_log.py next to this file).

Exit 0 = every check passes. Exit 1 = at least one check failed (listed). Exit 2 = usage error, file missing
or unreadable, or an unexpected error (traceback tail on stderr).
"""
import importlib.util
import re
import sys
import traceback
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
SIZE_LIMIT_KIB = 16
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


def code_slug(text: str) -> str:
    """Lower-case `text` with every run of non-alphanumerics as one `-`, no `-` at either end."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


FAIL_CODES = (  # the closed vocabulary of the run log's fail_codes: constants only, never message text
    ("decisions-table", "contract-toplevel", "contract-continues", "contract-closed", "contract-live-chain",
     "contract-goal-missing", "contract-goal-drift", "contract-session-state", "contract-next-step",
     "usage", "unreadable", "internal", "secret:other")
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
    return [f"Live item {name} from the previous handoff is missing (carry it or close it)"
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


def passes(item: str) -> bool:
    low = item.lower()
    if DESIGN_PATH_RE.search(item):
        return True
    return all(any(label in low for label in pair) for pair in LABELS)


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
                failures.append((title, flat[:120]))
    decisions = decisions_failures(lines)
    contract_pairs = resume_contract_parts(lines, git, path)
    contract = [msg for _, msgs in contract_pairs for msg in msgs]
    RUN["parts"] = ([(secret_code(kind), [msg]) for kind, msg in secret_pairs]
                    + [(f"item-incomplete:{code_slug(title)}", [head]) for title, head in failures]
                    + [("decisions-table", decisions)] + contract_pairs)
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
    code = 1 if failures or decisions or contract or secrets else 0
    if not code:
        print("[PASS] open items carry what/why/how or a design path; Decisions table and resume contract satisfied; "
              "no secret-like values")
    size = len(text.encode("utf-8"))
    RUN["size"] = size
    if size > SIZE_LIMIT_KIB * 1024:
        print(f"WARNING: handoff file is {-(-size // 1024)} KiB (over {SIZE_LIMIT_KIB} KiB)")
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
        record_args = dict(
            script="check_handoff.py", mode="check", cwd=str(Path.cwd()), toplevel=None, args=argv,
            exit_code=code, stdout_lines=stdout_lines,
            handoff=str(Path(parsed[0]).expanduser()) if parsed else None,
            project=project_of(RUN.get("lines")), fail_codes=fail_codes, handoff_bytes=RUN.get("size"),
        )
        spec = importlib.util.spec_from_file_location("run_log", Path(__file__).with_name("run_log.py"))
        run_log = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_log)
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
