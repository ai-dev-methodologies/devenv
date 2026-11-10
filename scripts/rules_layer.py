#!/usr/bin/env python3
"""Rule layers for devenv: engine defaults (constants here), then the user layer, then the project layer.

Files:
  user layer     ~/.claude/devenv.local.md, else ~/.claude/handoff-rules.local.md (the older name)
  project layer  <toplevel>/.claude/devenv.local.md, then the main worktree's when the toplevel is a linked
                 worktree; when neither exists, .claude/handoff-rules.local.md in the same order
When both names exist in one layer, the new one is used and a `WARNING: both ...` line says so.

A `## <id>` section is an override point. A project file named devenv.local.md may hold any id in
KNOWN_SECTIONS; a file with the older name keeps the four handoff ids only (so its output does not change).
Any other `## ` section prints `WARNING: rules file section outside override points: <title>` and is not
applied. The user layer has no section restriction. Sections inside ``` or ~~~ fences are not sections.

`## lean-limits` holds `key: positive integer` lines (keys in DEFAULT_LIMITS). A later layer wins; a bad
value keeps the value from the layer below it (the engine default when no layer set it) and warns. A value
over LIMIT_CAP_FACTOR times the engine default is cut to that cap and warns: the model can write these
files while lean mode is on, so without a cap one large value would turn the limits off.

CLI (exit 0 done, 1 write failure or the line was appended but the file is still not ignored, 2 usage):
  rules_layer.py init-project <top> [--append-gitignore]
    Creates <toplevel>/.claude/devenv.local.md as a sample with no live section, never over an existing
    file. When git does not ignore the file, prints the .gitignore line and a `git check-ignore -v`
    command to confirm it; `--append-gitignore` appends the line instead.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

NEW_NAME = "devenv.local.md"
LEGACY_NAME = "handoff-rules.local.md"
HANDOFF_IDS = ("runner-completion", "state-files", "forbidden-writes", "values")
CYCLE_IDS = ("plan-lane", "approval-points", "critic-lanes", "external-critic", "judge-conditions",
             "review-rounds", "grading", "ledger", "dispatch-check", "priority-queue", "lane-ledger",
             "limits", "naming", "adapters")
ENGINE_IDS = ("lean-limits", "doctor-settings")
KNOWN_SECTIONS = HANDOFF_IDS + CYCLE_IDS + ENGINE_IDS
DEFAULT_LIMITS = {"read_bytes": 32768, "read_lines": 400, "bash_bytes": 16384, "bash_lines": 200,
                  "bash_head_lines": 40, "return_lines": 30}
# A layer may raise a limit to at most 4 x its default (read_lines 1600, return_lines 120, ...). The rules files
# are not guarded while lean mode is on, so this cap is what keeps a written value from switching enforcement
# off; 4 x leaves room for a project with long files while one read still stays a bounded part of the context.
LIMIT_CAP_FACTOR = 4
OUTSIDE_WARNING = "WARNING: rules file section outside override points:"
GITIGNORE_LINE = ".claude/" + NEW_NAME
IGNORED, NOT_IGNORED, NOT_APPLICABLE = "ignored", "not ignored", "not applicable"
LIMIT_LINE = re.compile(r"^(?:[-*]\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
EXIT_FAIL, EXIT_USAGE = 1, 2

SAMPLE = """# devenv project rules

Project layer of the devenv rules. Each override is a `## <id>` section; a section whose title is not
an override point is reported and not applied. This file is local: keep it out of git with the line
`.claude/devenv.local.md` in `.gitignore`.

Override points (copy a heading out of the block to use it):

```
## runner-completion
## state-files
## forbidden-writes
## values
## plan-lane
## approval-points
## critic-lanes
## external-critic
## judge-conditions
## review-rounds
## grading
## ledger
## dispatch-check
## priority-queue
## lane-ledger
## limits
## naming
## adapters
## doctor-settings
## lean-limits
read_bytes: 32768
read_lines: 400
bash_bytes: 16384
bash_lines: 200
bash_head_lines: 40
return_lines: 30
```
"""


# --- finding the files ---------------------------------------------------------------------------------
def pick(new: list[Path], legacy: list[Path], label_new: str, label_legacy: str) -> tuple[Path | None, list[str]]:
    """The first existing new-name file, else the first existing old-name file; warn when both exist."""
    found_new = next((p for p in new if p.is_file()), None)
    found_legacy = next((p for p in legacy if p.is_file()), None)
    if found_new is None:
        return found_legacy, []
    warns = [f"WARNING: both {label_new} and {label_legacy} exist; using {NEW_NAME}"] if found_legacy else []
    return found_new, warns


def user_file(home: Path | None = None) -> tuple[Path | None, list[str]]:
    base = (Path(home) if home is not None else Path.home()) / ".claude"
    return pick([base / NEW_NAME], [base / LEGACY_NAME], f"~/.claude/{NEW_NAME}", f"~/.claude/{LEGACY_NAME}")


def project_file(top: Path, main_tree: Path | None = None) -> tuple[Path | None, list[str]]:
    """`top` is the toplevel; `main_tree` is the main worktree when `top` is a linked worktree."""
    tops = [Path(top)]
    if main_tree is not None and Path(main_tree) != Path(top):
        tops.append(Path(main_tree))
    return pick([t / ".claude" / NEW_NAME for t in tops], [t / ".claude" / LEGACY_NAME for t in tops],
                f".claude/{NEW_NAME}", f".claude/{LEGACY_NAME}")


# --- reading sections ----------------------------------------------------------------------------------
def known_ids(path: Path) -> tuple:
    return HANDOFF_IDS if Path(path).name == LEGACY_NAME else KNOWN_SECTIONS


def section_id(title: str, ids: tuple) -> str | None:
    """The id a `## ` title names (`id` or `id: free text`), or None."""
    m = re.match(r"^(" + "|".join(re.escape(i) for i in ids) + r")\s*(?::|$)", title)
    return m.group(1) if m else None


def sections(path: Path) -> list[tuple[str, list[str]]]:
    """[(title, body lines)] for every `## ` section outside fences. Raises OSError or UnicodeDecodeError."""
    with Path(path).open("r", encoding="utf-8") as fh:
        text = fh.read()
    out, fenced = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            if out:
                out[-1][1].append(ln)
        elif not fenced and ln.startswith("## "):
            out.append((ln[3:].strip(), []))
        elif out:
            out[-1][1].append(ln)
    return out


def section_warnings(path: Path) -> list[str]:
    """One warning per `## ` section that is not an override point of this file (not capped)."""
    try:
        found = sections(path)
    except (OSError, UnicodeDecodeError):
        return [f"WARNING: rules file unreadable: {path}"]
    ids = known_ids(path)
    return [f"{OUTSIDE_WARNING} {title}" for title, _ in found if section_id(title, ids) is None]


def limits(paths) -> tuple[dict, list[str]]:
    """Lean limits from the given layer files in order (None and missing files are skipped). Only a file
    named devenv.local.md can set them."""
    values, warns = dict(DEFAULT_LIMITS), []
    for path in paths:
        if path is None or not Path(path).is_file() or Path(path).name != NEW_NAME:
            continue
        try:
            found = sections(path)
        except (OSError, UnicodeDecodeError):
            warns.append(f"WARNING: rules file unreadable: {path}")
            continue
        for title, body in found:
            if section_id(title, ("lean-limits",)) is None:
                continue
            fenced = False
            for ln in body:
                if ln.lstrip().startswith(("```", "~~~")):
                    fenced = not fenced
                    continue
                m = None if fenced else LIMIT_LINE.match(ln.strip())
                if not m:
                    continue
                key, raw = m.group(1), m.group(2)
                if key not in DEFAULT_LIMITS:
                    warns.append(f"WARNING: lean-limits unknown key in {path}: {key}")
                elif re.fullmatch(r"[0-9]+", raw) and int(raw) > 0:
                    cap = LIMIT_CAP_FACTOR * DEFAULT_LIMITS[key]
                    if int(raw) > cap:
                        warns.append(f"WARNING: lean-limits {key} in {path}: {int(raw)} is over the cap {cap} "
                                     f"({LIMIT_CAP_FACTOR} x the default); using {cap}")
                    values[key] = min(int(raw), cap)
                else:
                    warns.append(f"WARNING: lean-limits {key} in {path}: not a positive integer ({raw!r}); "
                                 f"keeping {values[key]}")
    return values, warns


# --- git -----------------------------------------------------------------------------------------------
def git(cwd: Path, *args: str) -> tuple[int, str]:
    """(exit code, stripped stdout); exit 127 when git cannot be run."""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return 127, ""
    return proc.returncode, (proc.stdout or "").strip()


def ignore_state(path: Path | None) -> tuple[str, str]:
    """(IGNORED | NOT_IGNORED | NOT_APPLICABLE, evidence) for a project rules file. Not applicable when
    there is no file or it is not inside a git work tree. A tracked file is not ignored (git publishes it
    whatever .gitignore says)."""
    if path is None or not Path(path).is_file():
        return NOT_APPLICABLE, "no project rules file"
    path = Path(path)
    code, inside = git(path.parent, "rev-parse", "--is-inside-work-tree")
    if code != 0 or inside != "true":
        return NOT_APPLICABLE, f"not a git repository ({path.parent})"
    code, out = git(path.parent, "check-ignore", "-v", "--", path.name)
    if code == 0:
        return IGNORED, out
    if code == 1:
        return NOT_IGNORED, f"{path} is not ignored"
    return NOT_APPLICABLE, f"git check-ignore exited {code}"


# --- init-project --------------------------------------------------------------------------------------
def append_line(gitignore: Path, line: str) -> None:
    try:
        with gitignore.open("rb") as fh:
            old = fh.read()
    except FileNotFoundError:
        old = b""
    with gitignore.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(("\n" if old and not old.endswith(b"\n") else "") + line + "\n")


def init_project(top: Path, append_gitignore: bool = False) -> tuple[int, list[str]]:
    top = Path(top).expanduser()
    if not top.is_dir():
        return EXIT_USAGE, [f"rules_layer: not a folder: {top}"]
    top = top.resolve()
    code, toplevel = git(top, "rev-parse", "--show-toplevel")
    in_git = code == 0 and bool(toplevel)
    if in_git:
        top = Path(toplevel)
    target = top / ".claude" / NEW_NAME
    lines = []
    if target.exists():
        lines.append(f"exists: {target} (not overwritten)")
    else:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(SAMPLE)
        except OSError as exc:
            return EXIT_FAIL, lines + [f"rules_layer: cannot create {target}: {exc}"]
        lines.append(f"created: {target}")
        if (top / ".claude" / LEGACY_NAME).is_file():
            lines.append(f"WARNING: .claude/{LEGACY_NAME} exists; the new file now takes its place, "
                         f"so move its sections into .claude/{NEW_NAME}")
    if not in_git:
        lines.append("gitignore: not a git repository")
        return 0, lines
    state, evidence = ignore_state(target)
    if state == IGNORED:
        lines.append(f"gitignore: ignored ({evidence})")
        return 0, lines
    if state != NOT_IGNORED:
        lines.append(f"gitignore: unknown ({evidence})")
        return 0, lines
    check = f"check: git -C {top} check-ignore -v {GITIGNORE_LINE}"
    if not append_gitignore:
        lines += ["gitignore: not ignored", f"suggest: add this line to {top / '.gitignore'}:",
                  f"  {GITIGNORE_LINE}", check]
        return 0, lines
    try:
        append_line(top / ".gitignore", GITIGNORE_LINE)
    except OSError as exc:
        return EXIT_FAIL, lines + [f"rules_layer: cannot append to {top / '.gitignore'}: {exc}"]
    lines.append(f"gitignore: appended {GITIGNORE_LINE} to {top / '.gitignore'}")
    state, evidence = ignore_state(target)
    if state != IGNORED:
        lines += [f"WARNING: still not ignored ({evidence}); a tracked file or a later rule keeps it in git",
                  check]
        return EXIT_FAIL, lines
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    append = "--append-gitignore" in args
    args = [a for a in args if a != "--append-gitignore"]
    if len(args) != 2 or args[0] != "init-project":
        sys.stderr.write("usage: rules_layer.py init-project <top> [--append-gitignore]\n")
        return EXIT_USAGE
    code, lines = init_project(Path(args[1]), append_gitignore=append)
    for ln in lines:
        (sys.stderr if code == EXIT_USAGE else sys.stdout).write(ln + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
