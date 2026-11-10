#!/usr/bin/env python3
"""One command shared by the /handoff · /resume-handoff pair.

Both commands start with the same bounded state block, so what /handoff writes into
`Session State` and what /resume-handoff compares against come from one source.

Usage:
  python3 ~/.claude/scripts/handoff_state.py <cwd>
      /handoff Step 0+2: state block (user rules file, toplevel, linked worktree, project key,
      previous handoff, history file, project rules file, branch, head, upstream ahead/behind,
      status summary + status file, lockfile-changed, diffstat).
  python3 ~/.claude/scripts/handoff_state.py <cwd> --resume [<arg> ...]
      /resume-handoff Step 0+1: the same state block (without previous handoff, history file and
      diffstat), then a `continue:` line, the handoff file resolved from <arg> (path | YYYY-MM-DD |
      slug | nothing = newest), its age, whether its recorded branch matches the current branch,
      how many commits and files changed since its recorded HEAD, one `live subagent:` line per
      Live subagents item, and the checker result.

The project key is the main worktree of a linked worktree, else the toplevel, else (outside
git) the cwd. Without a path argument, only handoffs whose `**Toplevel:**` line (or, in older
files, the first path of `**Project:**`) equals the project key are candidates. A path argument
is used as given; when its project differs, `continue:` is forced to no.

An argument that is an existing file path is the selector as written, spaces included. The
other arguments are split on whitespace. `--continue`, `--c`, or a token that is exactly
`계속` (surrounding punctuation and quotes removed) sets `continue: yes`. Of the remaining
words, the first date, path, or slug that names a handoff file is the selector; the rest are
reported on one `WARNING: ignored argument words:` line. The handoff body is never evidence
for `continue:`.

In the default mode, when CLAUDE_CODE_SESSION_ID is a UUID, `session id: <id>` and
`transcript: <path | none (not found)>` follow the state block (also outside git); the path is the
newest ~/.claude/projects/*/<id>.jsonl. Without the variable there are no such lines.

In resume mode, when the chosen handoff's head carries `**Session:** <UUID>`, that session's transcript
is read again: the messages the user gave it after its last /handoff input that the file's
`## After Handoff` section does not quote make `continue: no (unrecorded messages after handoff)`, even
with `--c`. A `project mismatch` or `newer handoff skipped` reason wins over it, but either way
`WARNING: N user message(s) after the last /handoff are not in the handoff file` and one indented
`「<message>」 (HH:MM:SSZ)` line per message (at most 5, then `… and N more`) follow the `handoff:` line.
When the transcript cannot be judged, `WARNING: after-handoff messages not checked: <reason>` takes that
place and `continue:` is unchanged. A file without the line, or with `none`, prints neither.

In the default mode, when ~/.claude/scripts/context_budget.py exists, its `--line` output is
printed as `context-budget: <first line, cut to 200 chars>`. Exit 0 and 2 both count as output
(2 means over budget); any other exit code or empty output prints a `WARNING: context-budget ...`
line instead. The exit code of this script never changes. No file, no line, no warning.

`previous handoff:` is the newest handoff of the project key (a candidate for the new file's
`**Continues:**`). `history file:` is the project's append-only record of closed items,
~/.claude/handoffs/history/<project folder>-<sha1(project key)[:8]>.md, printed with `(exists)` or
`(new)` so the model knows whether to create it or append to it.

`head:` is the short sha of HEAD, or none. In resume mode, when the chosen handoff records a
`- **HEAD:**` commit that exists in this repository, `since handoff: <N> commits · <M> files changed`
follows (N of 10 or more adds a WARNING); an unknown commit prints `since handoff: unknown (commit not found)`.

The changed-file list is written to
~/.claude/handoffs/.state/<toplevel folder>-<sha1(toplevel)[:8]>.status.txt and never printed,
so a resume does not start by paying for a long listing.

`live subagent: <name> -> <state> (<reason>)` judges each Live subagents item from the newest copy of
~/.claude/projects/*/*/subagents/agent-<id>.jsonl (or */subagents/, or a `result` that is a .jsonl file),
reading only its last 256 KiB: a final stop_reason end_turn, stop_sequence, max_tokens or refusal is
`returned`; otherwise `running?` while the copy changed in the last 15 minutes, `lost` once `expected-by`
(an ISO time) has passed or it has been still for over 60 minutes, else `running? (idle)`. No copy is
`lost (no transcript)`; an id outside [A-Za-z0-9_.:-] is `unknown (id not usable)`. At most 5 lines.

In resume mode, a file as new as or newer than the chosen one (or, when none is chosen, any file) that
is empty, names no project (Toplevel and Project both missing or placeholders), is not valid UTF-8
while its head (read with errors="replace") names this project or none, or is this project's but has
no usable Exact Next Step is named on a `WARNING: newer handoff skipped (<reason>): <file name>` line
(reason `empty file` · `project unknown` · `not valid UTF-8` · `no usable Exact Next Step`), and
`continue:` becomes `no (newer handoff skipped)` even with `--c`. An undecodable file keeps its
`WARNING: unreadable handoff skipped:` line too. A path argument and files confirmed to belong to
another project are not affected. The checker's `WARNING: handoff file is … KiB` line is left out of
the resume excerpt (a resume cannot shrink the file); the checker's own run record keeps it.

Each kind of WARNING line is capped at 3 lines; the third then says how many more there were.
Every run appends one record to the run log (run_log.py next to this file).

Exit codes: 0 ok · 1 resume found no handoff for this project · 2 usage, or Python older
than 3.9 · 3 checker missing or crashed (a `WARNING: checker ...` line says which) · 4 internal
error (traceback tail on stderr).
Set HANDOFF_CHECKER to use a checker other than check_handoff.py next to this file.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

CHECK_CAP = 8
WARN_CAP = 3
STDERR_LINES = 20
STDERR_CHARS = 2000
BUDGET_TIMEOUT = 10
BUDGET_CHARS = 200
OLD_DAYS = 7
HOME = Path.home()
HANDOFF_DIR = HOME / ".claude" / "handoffs"
STATE_DIR = HANDOFF_DIR / ".state"
USER_RULES = HOME / ".claude" / "handoff-rules.local.md"
CHECKER = Path(__file__).with_name("check_handoff.py")
BUDGET_SCRIPT = HOME / ".claude" / "scripts" / "context_budget.py"
LOCKFILES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb",
    "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock", "Gemfile.lock", "go.sum",
    "composer.lock", "gradle.lockfile",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")
SINCE_WARN = 10
SUBAGENT_CAP = 5
TAIL_BYTES = 256 * 1024
ACTIVE_SECONDS = 15 * 60
STALE_SECONDS = 60 * 60
RETURNED_STOPS = ("end_turn", "stop_sequence", "max_tokens", "refusal")
NAME_ID_RE = re.compile(r"\(([^()]*)\)\s*$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
PROJECTS_DIR = HOME / ".claude" / "projects"
INTERNAL_ERROR = 4
OVERRIDE_POINTS = ("runner-completion", "state-files", "forbidden-writes", "values")
OVERRIDE_RE = re.compile(r"^(?:" + "|".join(OVERRIDE_POINTS) + r")\s*(?::|$)")
CONTINUE_FLAGS = ("--continue", "--c")
CONTINUE_WORD = "계속"
TOKEN_PUNCT = ".,!?\"'「」"
DETACHED_RE = re.compile(r"^detached\s+([0-9a-f]{7,40})\b")
HEAD_FIELD_RE = re.compile(r"^\*\*([^*]+):\*\*\s*(.*)$")
OUTSIDE_WARNING = "WARNING: rules file section outside override points:"
UNREADABLE_WARNING = "WARNING: unreadable handoff skipped:"
SKIPPED_WARNING = "WARNING: newer handoff skipped"
SIZE_WARNING = "WARNING: handoff file is "
SESSION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")  # = run_log.SESSION_ID
UNRECORDED = "no (unrecorded messages after handoff)"
NOT_CHECKED_WARNING = "WARNING: after-handoff messages not checked:"
RUN: dict = {}  # facts for the run log, filled while the script runs


def git(cwd: Path, *args: str, strip: bool = True) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = proc.stdout or ""
    return proc.returncode, out.strip() if strip else out


def branch_key(value: str) -> str:
    """Shared by writing (state block) and comparing (resume): `detached <sha>` keeps its sha."""
    value = value.strip()
    m = DETACHED_RE.match(value)
    if m:
        return f"detached {m.group(1)}"
    return re.split(r"[\s(（·]", value, maxsplit=1)[0]


def same_branch(recorded: str, current: str) -> bool:
    if recorded == current:
        return True
    if recorded.startswith("detached ") and current.startswith("detached "):
        a, b = recorded.split(" ", 1)[1], current.split(" ", 1)[1]
        return a.startswith(b) or b.startswith(a)  # short sha length can differ between runs
    return False


def parse_porcelain(raw: str) -> list[tuple[str, str, str | None]]:
    """`status --porcelain=v1 -z` -> [(XY, path, original path or None)]."""
    parts = raw.split("\0")
    entries, i = [], 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path, orig = rec[:2], rec[3:], None
        if "R" in xy or "C" in xy:
            orig = parts[i] if i < len(parts) else None
            i += 1
        entries.append((xy, path, orig))
    return entries


def capped(lines: list[str], kind: str) -> list[str]:
    """At most WARN_CAP lines of one warning kind; the last one then counts the rest."""
    if len(lines) <= WARN_CAP:
        return lines
    return lines[:WARN_CAP - 1] + [f"{kind} … and {len(lines) - WARN_CAP + 1} more"]


def rules_warnings(path: Path) -> list[str]:
    """One warning per `## ` section whose title is not an override point."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [f"WARNING: rules file unreadable: {path}"]
    warns, fenced = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and ln.startswith("## ") and not OVERRIDE_RE.match(ln[3:].strip()):
            warns.append(f"{OUTSIDE_WARNING} {ln[3:].strip()}")
    return capped(warns, OUTSIDE_WARNING)


def project_digest(project: str) -> str:
    return hashlib.sha1(project.encode("utf-8")).hexdigest()[:8]


def write_status_file(top: Path, entries: list[tuple[str, str, str | None]]) -> str:
    """Keyed by the toplevel, so two linked worktrees with one folder name do not share a file."""
    lines = [f"{xy} {orig} -> {path}" if orig else f"{xy} {path}" for xy, path, orig in entries]
    target = STATE_DIR / f"{top.name}-{project_digest(top.as_posix())}.status.txt"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as f:
            f.write("".join(f"{ln}\n" for ln in lines))
    except OSError as exc:
        return f"not written ({exc})"
    return str(target)


def chain_lines() -> None:
    """`previous handoff:` and `history file:` for the project key (default mode)."""
    previous, warns, _, _ = resolve_handoff(None, RUN["project"])
    for warn in warns:
        print(warn)
    print(f"previous handoff: {previous if previous else 'none'}")
    history = HANDOFF_DIR / "history" / f"{Path(RUN['project']).name}-{project_digest(RUN['project'])}.md"
    print(f"history file: {history} ({'exists' if history.is_file() else 'new'})")


def state_block(cwd: Path, with_diffstat: bool) -> tuple[bool, str | None]:
    """Print the state block. Returns (inside a git repository, current branch or None)."""
    print(f"user rules file: {USER_RULES if USER_RULES.is_file() else 'none'}")
    code, toplevel = git(cwd, "rev-parse", "--show-toplevel")
    if code != 0 or not toplevel:
        print(f"toplevel: not a git repository ({cwd})")
        RUN["project"] = cwd.as_posix()
        print(f"project key: {RUN['project']}")
        if with_diffstat:
            chain_lines()
        print("head: none")
        return False, None
    top = Path(toplevel)
    RUN["toplevel"] = str(top)
    print(f"toplevel: {top}")

    _, git_dir = git(cwd, "rev-parse", "--path-format=absolute", "--git-dir")
    _, common_dir = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    linked = bool(git_dir and common_dir and Path(git_dir) != Path(common_dir))
    main_tree = Path(common_dir).parent if linked and common_dir else top
    print(f"linked worktree: {'yes' if linked else 'no'}" + (f" (main: {main_tree})" if linked else ""))
    RUN["project"] = main_tree.as_posix()
    print(f"project key: {RUN['project']}")
    if with_diffstat:
        chain_lines()

    candidates = [top / ".claude" / "handoff-rules.local.md"]
    if linked:
        candidates.append(main_tree / ".claude" / "handoff-rules.local.md")
    rules = next((p for p in candidates if p.is_file()), None)
    print(f"rules file: {rules if rules else 'none'}")
    if rules:
        for warn in rules_warnings(rules):
            print(warn)

    code, branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or not branch:
        code, branch = git(cwd, "symbolic-ref", "--short", "-q", "HEAD")  # a branch with no commits yet
        branch = branch if code == 0 and branch else "unknown"
    if branch == "HEAD":
        _, sha = git(cwd, "rev-parse", "--short", "HEAD")
        branch = branch_key(f"detached {sha}")
    print(f"branch: {branch}")
    code, sha = git(cwd, "rev-parse", "--short", "HEAD")
    print(f"head: {sha if code == 0 and sha else 'none'}")

    code, upstream = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code == 0 and upstream:
        _, counts = git(cwd, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        behind, ahead = (counts.split() + ["?", "?"])[:2]
        print(f"upstream: {upstream} ahead {ahead} behind {behind}")
    else:
        print("upstream: none")

    code, raw = git(cwd, "status", "--porcelain=v1", "-z", strip=False)
    if code != 0:
        print("status: unavailable (git status failed)")
        entries = []
    else:
        entries = parse_porcelain(raw)
        if not entries:
            print("status: clean")
        else:
            counts = Counter(xy.strip() for xy, _, _ in entries)
            summary = " · ".join(f"{c} {n}" for c, n in counts.most_common())
            print(f"status: {len(entries)} changed ({summary})")
        print(f"status file: {write_status_file(top, entries)}")

    names = {Path(p).name for _, path, orig in entries for p in (path, orig) if p}
    changed = sorted(names & LOCKFILES)
    print("lockfile changed: " + (", ".join(changed) if changed else "no"))

    if with_diffstat:
        code, stat = git(cwd, "diff", "--shortstat", "HEAD")
        print(f"diffstat: {stat if stat else 'none'}" if code == 0 else "diffstat: unavailable")
    return True, branch


def budget_line(cwd: Path) -> None:
    """Print the optional `context-budget:` line from the user's own script, if there is one."""
    if not BUDGET_SCRIPT.is_file():
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(BUDGET_SCRIPT), "--line"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=BUDGET_TIMEOUT, cwd=str(cwd),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"WARNING: context-budget not run ({type(exc).__name__})")
        return
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode not in (0, 2) or not lines:
        print(f"WARNING: context-budget gave no line (exit {proc.returncode})")
        return
    print(f"context-budget: {lines[0][:BUDGET_CHARS]}")


def session_lines() -> None:
    """`session id:` and `transcript:` for /handoff Step 0, only when CLAUDE_CODE_SESSION_ID is a UUID."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not SESSION_ID.fullmatch(sid):
        return
    print(f"session id: {sid}")
    find = getattr(checker_module(), "find_transcript", None)
    found = find(sid) if find else None
    print(f"transcript: {found if found else 'none (not found)'}")


def continue_hit(tok: str) -> str | None:
    """The continue token `tok` stands for, or None."""
    if tok in CONTINUE_FLAGS:
        return tok
    return CONTINUE_WORD if tok.strip(TOKEN_PUNCT) == CONTINUE_WORD else None


def is_selector_word(word: str) -> bool:
    """A date, a path, or a slug that names at least one handoff file."""
    if DATE_RE.match(word) or "/" in word or "\\" in word or word.endswith(".tmp"):
        return True
    return any(HANDOFF_DIR.glob(f"????-??-??-{word}*-session.tmp"))


def parse_args(args: list[str]) -> tuple[str | None, str | None, list[str]]:
    """(continue token, handoff selector, ignored words). Only argument tokens count."""
    token, selector, words, ignored = None, None, [], []
    for arg in args:
        if selector is None and arg.strip() and not continue_hit(arg.strip()) and Path(arg).expanduser().is_file():
            selector = arg  # an existing file keeps its exact spelling, spaces included
        else:
            words.extend(arg.split())
    for word in words:
        hit = continue_hit(word)
        if hit:
            token = token or hit
        elif selector is None and is_selector_word(word):
            selector = word
        else:
            ignored.append(word)
    return token, selector, ignored


_checker_mod = False  # False = not loaded yet, None = could not be loaded


def checker_module():
    """check_handoff.py next to this script, loaded once, or None when it cannot be loaded."""
    global _checker_mod
    if _checker_mod is False:
        try:
            spec = importlib.util.spec_from_file_location("check_handoff", CHECKER)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _checker_mod = mod
        except Exception as exc:  # keep resume usable; say that the check is weaker
            print(f"WARNING: placeholder check unavailable, only empty text counts as placeholder ({exc})")
            _checker_mod = None
    return _checker_mod


def is_placeholder(text: str) -> bool:
    """Same rule as check_handoff.is_placeholder."""
    mod = checker_module()
    return mod.is_placeholder(text) if mod else not text.strip()


def path_key(text: str) -> str:
    """Same comparison form as check_handoff.path_key."""
    mod = checker_module()
    return mod.path_key(text) if mod else text.strip().replace("\\", "/").rstrip("/")


def handoff_project(text: str) -> tuple[str | None, str | None]:
    """(comparable project key, value as written) from the head's Toplevel line, else the first path of Project."""
    head: dict = {}
    for ln in text.splitlines():
        if ln.startswith("## "):
            break
        m = HEAD_FIELD_RE.match(ln.strip())
        if m:
            head.setdefault(m.group(1).strip(), m.group(2).strip())
    top = head.get("Toplevel", "").strip("`")
    if top and not is_placeholder(top):
        return path_key(Path(top).expanduser().as_posix()), top
    project = head.get("Project", "")
    if project and not is_placeholder(project):
        first = project.split()[0].strip("`,;")
        return path_key(Path(first).expanduser().as_posix()), project
    return None, None


def has_next_step(text: str) -> bool:
    """A candidate carries a real Exact Next Step (check_handoff.next_step_missing): a template whose head
    or other sections were filled in is still not a handoff to resume."""
    lines = text.split("\n")
    mod = checker_module()
    if mod:
        return not mod.next_step_missing(lines)
    return any(ln.strip() == "## Exact Next Step" for ln in lines)


def resolve_handoff(selector: str | None, project: str) -> tuple[Path | None, list[str], str | None, list[str]]:
    """Implements /resume-handoff Step 1.

    Returns (handoff path or None, warnings, project of the newest other-project file when none matched,
    warnings for files that may be this project's, could not be used, and are as new as or newer than
    the chosen one).
    """
    if selector:
        direct = Path(selector).expanduser()
        if direct.is_file():
            return direct, [], None, []
        if "/" in selector or "\\" in selector or selector.endswith(".tmp"):
            return None, [], None, []
        if DATE_RE.match(selector):
            pattern = f"{selector}-*-session.tmp"
        else:
            pattern = f"????-??-??-{selector}*-session.tmp"
    else:
        pattern = "*-session.tmp"
    key = path_key(project)
    found, skipped, unreadable = [], [], []
    for p in HANDOFF_DIR.glob(pattern):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # often a write cut inside a multibyte character: the head survives
            unreadable.append(f"{UNREADABLE_WARNING} {p.name}")
            try:
                owner_key = handoff_project(p.read_text(encoding="utf-8", errors="replace"))[0]
            except OSError:
                owner_key = None
            if owner_key in (key, None):
                skipped.append((p, "not valid UTF-8"))
            continue
        except OSError:
            unreadable.append(f"{UNREADABLE_WARNING} {p.name}")
            continue
        if not text:
            skipped.append((p, "empty file"))
            continue
        owner, usable = handoff_project(text), has_next_step(text)
        if usable:
            found.append((p, owner))
        if owner[0] is None:
            skipped.append((p, "project unknown"))
        elif owner[0] == key and not usable:
            skipped.append((p, "no usable Exact Next Step"))
    warns = capped(sorted(unreadable), UNREADABLE_WARNING)
    found.sort(key=lambda f: (-f[0].stat().st_mtime, -f[0].stat().st_size, str(f[0])))
    mine = [p for p, (k, _) in found if k == key]
    chosen = mine[0] if mine else None
    floor = chosen.stat().st_mtime if chosen else float("-inf")
    newer = sorted(((p, why) for p, why in skipped if p.stat().st_mtime >= floor),
                   key=lambda s: (-s[0].stat().st_mtime, s[0].name))
    rest = ", ".join(dict.fromkeys(why for _, why in newer[WARN_CAP - 1:]))  # reasons behind "… and N more"
    skip_warns = capped([f"{SKIPPED_WARNING} ({why}): {p.name}" for p, why in newer], f"{SKIPPED_WARNING} ({rest}):")
    if chosen:
        return chosen, warns, None, skip_warns
    return None, warns, (found[0][1][1] or "unknown") if found else None, skip_warns


def handoff_session(text: str) -> str | None:
    """The head's `**Session:**` value when it is a UUID, else None (no line, `none`, anything else)."""
    for ln in text.splitlines():
        if ln.startswith("## "):
            break
        m = HEAD_FIELD_RE.match(ln.strip())
        if m and m.group(1).strip() == "Session":
            value = m.group(2).strip().strip("`")
            return value if SESSION_ID.fullmatch(value) else None
    return None


def after_handoff_lines(path: Path) -> tuple[bool, list[str]]:
    """Resume gate: (some message the writing session got after its last /handoff is not quoted in the file,
    the WARNING line and its indented quotes). Nothing for a file whose head names no session."""
    text = path.read_text(encoding="utf-8", errors="replace")
    sid = handoff_session(text)
    if sid is None:
        return False, []
    mod = checker_module()
    try:
        missing, reason = mod.after_handoff_check(sid, text.split("\n"))
        shown = mod.shown_quotes(missing)
    except Exception:  # no checker, or one without the transcript functions (a mixed install)
        missing, reason = [], "checker unavailable"
    if reason:
        return False, [f"{NOT_CHECKED_WARNING} {reason}"]
    if not missing:
        return False, []
    head = f"WARNING: {len(missing)} user message(s) after the last /handoff are not in the handoff file"
    return True, [head] + [f"  {ln}" for ln in shown]


def recorded_field(path: Path, label: str) -> str | None:
    """Value of the first `- **<label>:**` line in the file, or None when absent or empty."""
    prefix = f"- **{label}:**"
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.startswith(prefix):
            return ln.split(prefix, 1)[1].strip() or None
    return None


def recorded_branch(path: Path) -> str | None:
    value = recorded_field(path, "Branch")
    return branch_key(value) if value and not is_placeholder(value) else None  # n/a outside git


def since_handoff(path: Path) -> None:
    """Print how far HEAD moved since the handoff's recorded HEAD (nothing when it recorded none)."""
    value = recorded_field(path, "HEAD")
    words = value.split() if value else []
    sha = words[0].strip("`") if words else ""
    if not sha or is_placeholder(sha) or sha.lower() == "none":
        return
    cwd = Path(RUN["cwd"])
    if SHA_RE.match(sha):  # a hex word only: anything else could be read as a git option
        verify_code, _ = git(cwd, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        count_code, count = git(cwd, "rev-list", "--count", f"{sha}..HEAD")
        files_code, files = git(cwd, "diff", "--name-only", sha, "HEAD", "--")
        if verify_code == 0 and count_code == 0 and files_code == 0 and count.isdigit():
            changed = len(files.splitlines())
            print(f"since handoff: {count} commits · {changed} files changed")
            if int(count) >= SINCE_WARN:
                print(f"WARNING: {count} commits since the handoff")
            return
    print("since handoff: unknown (commit not found)")


def last_stop_reason(path: Path) -> str | None:
    """stop_reason of the last assistant record in the final TAIL_BYTES of a transcript, or None."""
    try:
        with path.open("rb") as f:
            start = max(0, f.seek(0, 2) - TAIL_BYTES)
            f.seek(start)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines[1:] if start else lines):  # a cut first line is not a record
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "assistant":
            msg = rec.get("message")
            return msg.get("stop_reason") if isinstance(msg, dict) else None
    return None


def is_past(value: str) -> bool:
    """True when `value` reads as an ISO time that has passed; anything else is not a time."""
    text = value.strip()
    try:
        when = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return False
    return when < datetime.now(when.tzinfo)


def subagent_state(item: dict) -> str:
    """`<state> (<reason>)` of one Live subagents item, judged from the newest copy of its transcript."""
    m = NAME_ID_RE.search(item.get("name", ""))
    sid = m.group(1).strip() if m else ""
    if not ID_RE.match(sid):
        return "unknown (id not usable)"
    copies = [*PROJECTS_DIR.glob(f"*/*/subagents/agent-{sid}.jsonl"), *PROJECTS_DIR.glob(f"*/subagents/agent-{sid}.jsonl")]
    result = item.get("result", "").strip("` ")
    if result.endswith(".jsonl"):
        copies.append(Path(result).expanduser())
    dated = []
    for copy in copies:
        try:
            dated.append((copy.stat().st_mtime, copy))
        except OSError:
            continue
    if not dated:
        return "lost (no transcript)"
    mtime, newest = max(dated)
    stop = last_stop_reason(newest)
    if stop in RETURNED_STOPS:
        return f"returned ({stop})"
    age = time.time() - mtime
    if age <= ACTIVE_SECONDS:
        return f"running? (active {int(age // 60)}m ago)"
    if is_past(item.get("expected-by", "")):
        return "lost (past expected-by)"
    if age > STALE_SECONDS:
        return f"lost (stale {int(age // 60)}m)"
    return f"running? (idle {int(age // 60)}m)"


def live_subagent_lines(path: Path) -> None:
    """One `live subagent:` line per Live subagents item of the handoff (at most SUBAGENT_CAP lines)."""
    mod = checker_module()
    if mod is None:
        return
    body = mod.section_body(path.read_text(encoding="utf-8", errors="replace").split("\n"), "Session State")
    field = mod.session_fields(body).get("Live subagents") if body else None
    items = mod.live_items(field[1]) if field else []
    out = [f"live subagent: {item.get('name', '').strip()} -> {subagent_state(item)}" for item in items]
    if len(out) > SUBAGENT_CAP:
        out = out[:SUBAGENT_CAP - 1] + [f"live subagent: … and {len(out) - SUBAGENT_CAP + 1} more"]
    for ln in out:
        print(ln)


def cut_stderr(text: str) -> list[str]:
    lines = text[:STDERR_CHARS].splitlines()[:STDERR_LINES]
    if len(text) > STDERR_CHARS or len(text.splitlines()) > STDERR_LINES:
        lines.append("... stderr truncated")
    return lines


def run_checker(path: Path, in_git: bool) -> int:
    """Print the checker result. Returns 0, or 3 when the checker is missing or crashed."""
    checker = Path(os.environ.get("HANDOFF_CHECKER") or CHECKER)
    if not checker.is_file():
        print(f"WARNING: checker missing ({checker}); the handoff was not validated")
        return 3
    proc = subprocess.run(
        [sys.executable, str(checker), str(path), "--git", "yes" if in_git else "no"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "HANDOFF_CALLER": "handoff_state.py"},  # marks the checker's run log record only
    )
    RUN["checker"] = proc.returncode
    out = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    out = [ln for ln in out if not ln.lstrip().startswith(SIZE_WARNING)]  # a resume cannot shrink the file
    crashed_1 = proc.returncode == 1 and not any(ln.startswith("[FAIL]") for ln in out)
    if proc.returncode not in (0, 1) or crashed_1:
        why = " without a [FAIL] line" if crashed_1 else ""
        print(f"WARNING: checker exited {proc.returncode}{why}; the handoff was not validated")
        for ln in cut_stderr(proc.stderr or ""):
            print(f"  {ln}")
        return 3
    print(f"handoff check: exit {proc.returncode}")
    for ln in out[:CHECK_CAP]:
        print(f"  {ln}")
    if len(out) > CHECK_CAP:
        print(f"  ... +{len(out) - CHECK_CAP} more")
    return 0


def resume_block(args: list[str], branch: str | None, in_git: bool) -> int:
    token, selector, ignored = parse_args(args)
    explicit = bool(selector) and Path(selector).expanduser().is_file()
    path, warns, other, skipped = resolve_handoff(selector, RUN["project"])
    mismatch = None
    if path is not None and explicit:  # a path argument is not filtered, only compared
        key, shown = handoff_project(path.read_text(encoding="utf-8", errors="replace"))
        if key != path_key(RUN["project"]):
            mismatch = shown or "unknown"
    unrecorded, gate = after_handoff_lines(path) if path is not None else (False, [])
    if mismatch:
        RUN["continue"] = "no (project mismatch)"
    elif skipped:
        RUN["continue"] = "no (newer handoff skipped)"
    elif unrecorded:
        RUN["continue"] = UNRECORDED
    else:
        RUN["continue"] = f"yes ({token})" if token else "no"
    print(f"continue: {RUN['continue']}")
    if ignored:
        print(f"WARNING: ignored argument words: {' '.join(ignored)}")
    for warn in warns + skipped:
        print(warn)
    if path is None:
        print("handoff: none found for this project" + (f" matching {selector!r}" if selector else ""))
        if other:
            print(f"WARNING: newest handoff belongs to another project: {other}")
        return 1
    RUN["handoff"] = str(path)
    print(f"handoff: {path}")
    if mismatch:
        print(f"WARNING: handoff project differs: {mismatch}")
    for ln in gate:
        print(ln)
    age_days = (time.time() - path.stat().st_mtime) / 86400
    warn = "  WARNING: handoff is older than 7 days" if age_days > OLD_DAYS else ""
    print(f"handoff age: {age_days:.1f} days{warn}")
    rec = recorded_branch(path)
    if rec is None:
        print("handoff branch: not recorded")
    elif branch is None:
        print(f"handoff branch: {rec} (current unknown)")
    else:
        same = same_branch(rec, branch)
        print(f"handoff branch: {rec} -> {'same' if same else 'changed  WARNING: branch changed'}")
    if in_git:
        since_handoff(path)
    live_subagent_lines(path)
    return run_checker(path, in_git)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    cwd = Path(argv[0]).expanduser().resolve()
    RUN["cwd"] = str(cwd)
    rest = argv[1:]
    resume = bool(rest) and rest[0] == "--resume"
    in_git, branch = state_block(cwd, with_diffstat=not resume)
    if not resume:
        session_lines()
        budget_line(cwd)
        return 0
    return resume_block(rest[1:], branch, in_git)


class _Tee:
    """Writes through to a stream and keeps a copy, so the run log gets the exact stdout lines."""

    def __init__(self, stream):
        self.stream, self.parts = stream, []

    def write(self, s):
        self.parts.append(s)
        return self.stream.write(s)

    def flush(self):
        self.stream.flush()

    def lines(self) -> list[str]:
        """Split on "\\n" only: splitlines() would also split on U+2028 and the like inside an indented line."""
        lines = "".join(self.parts).split("\n")
        return lines[:-1] if lines[-1] == "" else lines


def log_run(argv: list[str], code: int, lines: list[str]) -> None:
    """Append one run record; a failure only prints a WARNING line on stderr. The recorded lines go through the
    checker's redact (a Live subagents name can hold a token); without the checker the record keeps no line."""
    try:
        with contextlib.redirect_stdout(sys.stderr):  # stdout is already final; a load WARNING goes to stderr
            mod = checker_module()
        lines = mod.redact("\n".join(lines)).split("\n") if mod and lines else []
        spec = importlib.util.spec_from_file_location("run_log", Path(__file__).with_name("run_log.py"))
        run_log = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_log)
        reason = run_log.append_run(run_log.build_record(
            script="handoff_state.py", mode="resume" if argv[1:2] == ["--resume"] else "handoff",
            cwd=RUN.get("cwd", str(Path.cwd())), toplevel=RUN.get("toplevel"), args=argv, exit_code=code,
            stdout_lines=lines, handoff=RUN.get("handoff"), cont=RUN.get("continue"), checker=RUN.get("checker"),
            project=RUN.get("project"),
        ))
    except Exception as exc:  # a missing or broken run_log.py never changes the result
        reason = f"{type(exc).__name__}: {exc}"
    if reason:
        sys.stderr.write(f"WARNING: run log not written: {reason}\n")


def run(argv: list[str]) -> int:
    """main() plus the run log. An unexpected exception prints its traceback tail on stderr and exits 4."""
    tee = _Tee(sys.stdout)
    sys.stdout, code = tee, INTERNAL_ERROR
    try:
        code = main(argv)
    except Exception:
        tail = traceback.format_exc().rstrip("\n").splitlines()[-5:]
        sys.stderr.write("\n".join(tail) + "\n")
        code = INTERNAL_ERROR
    finally:
        sys.stdout = tee.stream
        log_run(argv, code, tee.lines())
    return code


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        sys.stderr.write("handoff_state.py needs Python 3.9 or newer\n")
        raise SystemExit(2)
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(run(sys.argv[1:]))
