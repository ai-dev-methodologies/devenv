#!/usr/bin/env python3
"""devenv doctor: checks of this install, a replay of every registered hook, live hook firing, developer mode.

Usage (skills call it through bin/devenv-py, which runs it with the interpreter after the trailing --py):
  doctor.py [--cwd DIR] [--python PATH] [--home DIR] [--verbose]
  doctor.py dev on|off|status|report [--home DIR]
  doctor.py init-project [--cwd DIR] [--append-gitignore]

A check run prints one `DOCTOR <id> PASS|WARN|FAIL|SKIP <evidence>` line per check and a last line
`DOCTOR summary pass=N warn=N fail=N skip=N`. PASS lines are folded into the summary unless --verbose.
FAIL is used only for what a machine check confirmed; SKIP always says why; nothing is fixed
automatically. Evidence names keys, paths and counts only, never a settings value.

Exit: 0 no FAIL and no replay SKIP · 1 a FAIL or a replay SKIP (a live SKIP does not count) · 2 the doctor
itself failed (a usage error, or a check that raised: its line is SKIP with `internal error`).

Checks:
  static   python-version (the --python interpreter is 3.10 or later), plugin-version (CHANGELOG top,
           plugin.json and run_log.VERSION are equal), hook-scripts (every registered script exists),
           skill-description (300 characters at most), lean-state and dev-state (the state files can be
           read), rules-both-files, rules-sections, rules-limits, rules-ignored (rules_layer), paths
           (`~/.claude/` paths in the rules and instruction files exist; fenced code and `<…>`, `{…}`, `$`
           placeholders are left out; globs must match), settings-names (names listed under
           `## doctor-settings` in the user rules layer exist in settings.json or settings.local.json),
           legacy-install (the older scripts in ~/.claude/scripts/), legacy-budget-hook (an older
           SessionStart context_budget.py entry in settings next to the engine's SessionStart hook writes
           the start load twice), start-load (over 48 KiB fails, with the three largest files), hook-errors
           (devenv `hook_non_blocking_error` lines in the newest three transcripts of this project).
  replay   One line per registered hook. The command line from hooks/hooks.json runs as registered, with
           only `${user_config.python}` (the --python value, the interpreter the hooks get),
           `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` (a temporary folder) filled in, plus the
           isolation arguments `--root <temporary folder>` (and `--log` for SessionStart). Violating inputs
           must be denied or replaced, clean and lean-off inputs must pass, SubagentStop must pass
           `stop_hook_active: true`, and the Bash replacement object is checked key by key here. A replay
           line proves the script, the interpreter and the registered command line only.
  live     One line per hook: a firing of the current version in the developer mode record
           (events.jsonl), else `SKIP (no live firing recorded)`.

--python  the interpreter the hooks run with (the userConfig `python` value); default this interpreter.
--home    replaces the home folder for every file the doctor reads (tests). The replay writes only inside
          its own temporary folder; isolation goes through command line arguments only, never through an
          environment variable.
`dev report` summarizes events.jsonl (counts per version and kind, returned bytes) and the run log
(run_report.py). Turning developer mode off is not guarded by the lean-off guard.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS.parent
Result = namedtuple("Result", "id status evidence")
STATUSES = ("PASS", "WARN", "FAIL", "SKIP")
SCOPE = "(proves: script, interpreter, registered command line)"
NO_LIVE = "(no live firing recorded)"
MIN_PYTHON = (3, 10)
DESCRIPTION_MAX = 300
EVIDENCE_CHARS = 600
TIMEOUT = 30
LEGACY_SCRIPTS = ("handoff_state.py", "check_handoff.py", "run_log.py", "run_report.py")
HOOK_SCRIPTS = ("hook_session_start.py", "hook_read.py", "hook_return.py", "hook_bash.py")
HANDBACK_TOOL = "SubagentHandback"
READ_TOOLS = ("Read", "Write", "Edit", "Bash")
# The Bash output shape the harness accepts (measured): these keys with these types, every original key kept.
BASH_KEYS = {"stdout": str, "stderr": str, "interrupted": bool, "isImage": bool}
REPLAYS = ("replay-session-start", "replay-read", "replay-handback", "replay-bash", "replay-stop")
LIVE = (("live-session-start", "session_start"), ("live-read", "read_deny"), ("live-handback", "handback_deny"),
        ("live-bash", "bash_replace"), ("live-stop", "return_block"))
PATH_RE = re.compile(r"~/\.claude(?:/[A-Za-z0-9._\-*?<>{}$@+=%~/]*)?")
PLACEHOLDER = set("<>{}$")
GLOB = set("*?[")
SESSION = "00000000-0000-4000-8000-00000000d0c7"


class DoctorError(Exception):
    """A check input that cannot be used (the message becomes the evidence)."""


class Context:
    def __init__(self, home, cwd, python, plugin_root=PLUGIN_ROOT):
        self.home = Path(home)
        self.claude = self.home / ".claude"
        self.cwd = Path(cwd).resolve()
        self.python = str(python)
        self.plugin_root = Path(plugin_root)

    @property
    def state_root(self):
        return self.claude / "state"

    @property
    def events_file(self):
        return self.claude / "handoffs" / ".state" / "events.jsonl"

    def shown(self, path) -> str:
        text = str(path)
        home = str(self.home)
        return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def sibling(name):
    spec = importlib.util.spec_from_file_location(f"devenv_doctor_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one_line(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= EVIDENCE_CHARS else text[:EVIDENCE_CHARS - 1] + "…"


def guarded(check_id, fn):
    """(results, internal error) of fn(), which returns a Result list or one (status, evidence) pair. A check
    that raises becomes one SKIP line with `internal error` (and the run exits 2)."""
    try:
        out = fn()
    except Exception as exc:  # a doctor bug must not hide the other checks
        return [Result(check_id, "SKIP", one_line(f"internal error: {type(exc).__name__}: {exc}"))], True
    if isinstance(out, tuple):
        out = [Result(check_id, out[0], one_line(out[1]))]
    return out, False


# --- shared inputs ---------------------------------------------------------------------------------------
def registrations(plugin_root: Path):
    """[(event, matcher or None, handler dict)] from hooks/hooks.json. Raises DoctorError."""
    path = Path(plugin_root) / "hooks" / "hooks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        found = []
        for event, groups in data["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    if not isinstance(handler, dict):
                        raise TypeError("a handler is not an object")
                    found.append((event, group.get("matcher"), handler))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DoctorError(f"hooks/hooks.json unusable ({type(exc).__name__}: {exc})")
    return found


def fill(text: str, ctx, data_dir) -> str:
    out = (text.replace("${user_config.python}", ctx.python)
           .replace("${CLAUDE_PLUGIN_ROOT}", str(ctx.plugin_root))
           .replace("${CLAUDE_PLUGIN_DATA}", str(data_dir)))
    if "${" in out:
        raise DoctorError(f"unknown placeholder in registration: {text}")
    return out


def command_line(handler, ctx, data_dir) -> list:
    """The registered command line with the three placeholders filled in. Raises DoctorError."""
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        raise DoctorError("handler is not a command hook")
    args = handler.get("args")
    if args is None:
        return shlex.split(fill(handler["command"], ctx, data_dir))
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise DoctorError("handler args is not a list of strings")
    return [fill(handler["command"], ctx, data_dir)] + [fill(a, ctx, data_dir) for a in args]


def script_of(handler, ctx) -> Path | None:
    """The first argument that names a .py file, with the plugin root filled in."""
    argv = command_line(handler, ctx, ctx.claude / "plugin-data")
    return next((Path(a) for a in argv[1:] if a.endswith(".py")), None)


def git_layers(cwd: Path):
    """(toplevel, main worktree or None) for cwd, or (None, None) outside git."""
    rl = sibling("rules_layer")
    code, out = rl.git(cwd, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir",
                       "--git-common-dir")
    parts = out.splitlines()
    if code != 0 or len(parts) != 3:
        return None, None
    top, git_dir, common = (Path(p) for p in parts)
    return top, (common.parent if git_dir != common else None)


def rule_files(ctx, user_home=None):
    """(user file, project file, warnings) of the rules layers for ctx.cwd."""
    rl = sibling("rules_layer")
    user, uw = rl.user_file(ctx.home if user_home is None else user_home)
    top, main_tree = git_layers(ctx.cwd)
    project, pw = rl.project_file(top, main_tree) if top else (None, [])
    return user, project, uw + pw


def read_json_file(path: Path):
    """Parsed JSON, None when missing. Raises DoctorError naming the file only (never its content)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DoctorError(f"{path.name} unreadable ({type(exc).__name__})")


def settings_files(ctx):
    return [ctx.claude / "settings.json", ctx.claude / "settings.local.json"]


# --- static checks ---------------------------------------------------------------------------------------
def check_python(ctx):
    code = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    try:
        proc = subprocess.run([ctx.python, "-c", code], capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return "FAIL", f"interpreter not found: {ctx.python}"
    except (OSError, subprocess.SubprocessError) as exc:
        return "FAIL", f"interpreter cannot run: {ctx.python} ({type(exc).__name__})"
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", proc.stdout.strip())
    if proc.returncode != 0 or not m:
        return "FAIL", f"{ctx.python} printed no version (exit {proc.returncode})"
    version = tuple(int(g) for g in m.groups())
    shown = ".".join(m.groups())
    if version[:2] < MIN_PYTHON:
        return "FAIL", f"{ctx.python} is {shown}; the hooks need 3.10 or later"
    return "PASS", f"{ctx.python} {shown}"


def check_plugin_version(ctx):
    root = ctx.plugin_root
    values = {}
    try:
        m = re.search(r"^## \[([^\]]+)\]", (root / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
        values["CHANGELOG.md"] = m.group(1) if m else None
    except OSError:
        values["CHANGELOG.md"] = None
    try:
        manifest = (root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        values["plugin.json"] = json.loads(manifest).get("version")
    except (OSError, ValueError, AttributeError):
        values["plugin.json"] = None
    try:
        m = re.search(r'^VERSION = "([^"]+)"', (root / "scripts" / "run_log.py").read_text(encoding="utf-8"), re.M)
        values["run_log.VERSION"] = m.group(1) if m else None
    except OSError:
        values["run_log.VERSION"] = None
    shown = " ".join(f"{k}={v if v is not None else '(none)'}" for k, v in values.items())
    if None in values.values() or len(set(values.values())) != 1:
        return "FAIL", f"versions differ or are missing: {shown}"
    return "PASS", shown


def check_hook_scripts(ctx):
    regs = registrations(ctx.plugin_root)
    missing, bad, n = [], [], 0
    for event, _, handler in regs:
        try:
            script = script_of(handler, ctx)
        except DoctorError as exc:
            bad.append(f"{event}: {exc}")
            continue
        if script is None:
            bad.append(f"{event}: no script argument")
        elif not script.is_file():
            missing.append(f"{event}: {ctx.shown(script)}")
        n += 1
    if missing or bad:
        return "FAIL", "; ".join(bad + [f"missing {m}" for m in missing])
    return "PASS", f"{n} registered scripts exist"


def frontmatter(text: str):
    m = re.match(r"---\n(.*?)\n---\n", text.replace("\r\n", "\n"), re.S)
    if m is None:
        return None
    fields = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def check_skill_descriptions(ctx):
    paths = sorted((ctx.plugin_root / "skills").glob("*/SKILL.md"))
    if not paths:
        return "FAIL", "no skills/*/SKILL.md"
    bad = []
    for path in paths:
        fields = frontmatter(path.read_text(encoding="utf-8"))
        desc = (fields or {}).get("description")
        if not desc:
            bad.append(f"{path.parent.name} (no description)")
        elif len(desc) > DESCRIPTION_MAX:
            bad.append(f"{path.parent.name} ({len(desc)} characters)")
    if bad:
        return "FAIL", f"over {DESCRIPTION_MAX} characters or missing: " + ", ".join(bad)
    return "PASS", f"{len(paths)} skills, {DESCRIPTION_MAX} characters or fewer"


def check_lean_state(ctx):
    ds = sibling("devenv_state")
    path = ctx.state_root / ds.LEAN_NAME
    try:
        data = ds.load_lean(ctx.state_root)
    except ds.StateError as exc:
        return "FAIL", f"{ctx.shown(path)}: {exc}"
    return "PASS", f"{ctx.shown(path)} " + ("not present (lean off)" if data is None else "readable")


def check_dev_state(ctx):
    ds = sibling("devenv_state")
    path = ctx.state_root / ds.DEV_NAME
    st = ds.dev_status(root=ctx.state_root)
    if st["state"] == "unknown":
        return "FAIL", f"{ctx.shown(path)}: {st['error']}"
    return "PASS", f"{ctx.shown(path)}: developer mode {st['state']}"


def check_both_files(ctx):
    user, project, warns = rule_files(ctx)
    if warns:
        return "WARN", "; ".join(warns)
    names = [f"user {ctx.shown(user) if user else '(none)'}", f"project {ctx.shown(project) if project else '(none)'}"]
    return "PASS", ", ".join(names)


def check_sections(ctx):
    _, project, _ = rule_files(ctx)
    if project is None:
        return "PASS", "no project rules file"
    warns = sibling("rules_layer").section_warnings(project)
    if warns:
        return "WARN", f"{ctx.shown(project)}: " + "; ".join(warns)
    return "PASS", f"{ctx.shown(project)}: every section is an override point"


def check_limits(ctx):
    user, project, _ = rule_files(ctx)
    _, warns = sibling("rules_layer").limits([user, project])
    if warns:
        return "WARN", "; ".join(ctx.shown(w) for w in warns)
    return "PASS", "lean-limits values are positive integers within the cap (or not set)"


def check_ignored(ctx):
    _, project, _ = rule_files(ctx)
    if project is None:
        return "SKIP", "no project rules file"
    rl = sibling("rules_layer")
    state, evidence = rl.ignore_state(project)
    rel = f".claude/{project.name}"
    if state == rl.IGNORED:
        return "PASS", f"{rel} ignored ({evidence})"
    if state == rl.NOT_IGNORED:
        return "WARN", f"{rel} is not ignored by git: add `{rel}` to .gitignore"
    return "SKIP", evidence


def instruction_files(ctx):
    budget = sibling("context_budget")
    user, project, _ = rule_files(ctx)
    seen, out = set(), []
    for path in [p for p, _ in budget.measure(ctx.cwd, home=ctx.home)] + [user, project]:
        if path is not None and Path(path).is_file() and str(path) not in seen:
            seen.add(str(path))
            out.append(Path(path))
    return out


def path_exists(ctx, text: str) -> bool:
    rel = text[2:].rstrip("/") if text != "~/.claude" else ".claude"
    if set(rel) & GLOB:
        return any(ctx.home.glob(rel))
    return (ctx.home / rel).exists()


def check_paths(ctx):
    files = instruction_files(ctx)
    missing, total = [], 0
    for path in files:
        fenced = False
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
            for m in PATH_RE.finditer(line):
                text = m.group(0).rstrip(".,:;")
                if set(text) & PLACEHOLDER:
                    continue
                total += 1
                if not path_exists(ctx, text):
                    missing.append(f"{ctx.shown(path)}:{n} {text}")
    if missing:
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        return "WARN", f"{len(missing)} missing: " + ", ".join(missing[:5]) + more
    return "PASS", f"{total} paths in {len(files)} files exist"


def doctor_settings_names(user: Path | None):
    """Names listed under `## doctor-settings` of the user layer, or None when there is no such section."""
    if user is None:
        return None
    rl = sibling("rules_layer")
    found = None
    for title, body in rl.sections(user):
        if rl.section_id(title, ("doctor-settings",)) is None:
            continue
        found = [] if found is None else found
        fenced = False
        for line in body:
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            m = None if fenced else re.fullmatch(r"(?:[-*]\s+)?`?([A-Za-z0-9_.\-]+)`?", line.strip())
            if m:
                found.append(m.group(1))
    return found


def has_name(data, dotted: str) -> bool:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return False
        data = data[part]
    return True


def check_settings_names(ctx):
    user, _, _ = rule_files(ctx)
    names = doctor_settings_names(user)
    if not names:
        return "SKIP", "no names listed under ## doctor-settings in the user rules layer"
    loaded = []
    for path in settings_files(ctx):
        data = read_json_file(path)
        if isinstance(data, dict):
            loaded.append(data)
    missing = [n for n in names if not any(has_name(d, n) for d in loaded)]
    if missing:
        return "WARN", f"{len(missing)} of {len(names)} expected names missing from settings: " + ", ".join(missing)
    return "PASS", f"{len(names)} expected names present"


def check_legacy_install(ctx):
    left = [n for n in LEGACY_SCRIPTS if (ctx.claude / "scripts" / n).is_file()]
    if left:
        return "WARN", "older install still in ~/.claude/scripts/: " + ", ".join(left)
    return "PASS", "no older scripts in ~/.claude/scripts/"


def check_legacy_budget_hook(ctx):
    found = []
    for path in settings_files(ctx):
        data = read_json_file(path)
        groups = ((data or {}).get("hooks") or {}).get("SessionStart") if isinstance(data, dict) else None
        n = 0
        for group in groups if isinstance(groups, list) else []:
            for handler in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(handler, dict) and "context_budget.py" in str(handler.get("command", "")):
                    n += 1
        if n:
            found.append(f"{path.name} {n}")
    engine = any(event == "SessionStart" and "hook_session_start.py" in json.dumps(handler)
                 for event, _, handler in registrations(ctx.plugin_root))
    if found and engine:
        return "WARN", (f"duplicate start load record: SessionStart context_budget.py entries ({', '.join(found)}) "
                        f"and the engine's hook_session_start.py both write the context-budget log")
    if found:
        return "PASS", f"older SessionStart context_budget.py entries only ({', '.join(found)})"
    return "PASS", "no older SessionStart context_budget.py entry"


def check_start_load(ctx):
    rows = sibling("context_budget").measure(ctx.cwd, home=ctx.home)
    verdict = sibling("hook_session_start").start_verdict(rows)
    head = f"total={verdict['total']} cap={verdict['cap']}"
    if verdict["status"] == "FAIL":
        big = ", ".join(f"{ctx.shown(p)} {s}" for p, s in verdict["largest"])
        return "FAIL", f"{head}; largest: {big}"
    return "PASS", f"{head} files={sum(1 for _, s in rows if s is not None)}"


def check_hook_errors(ctx):
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(ctx.cwd))
    folder = ctx.claude / "projects" / slug
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3] if folder.is_dir() else []
    if not files:
        return "SKIP", f"no transcripts in {ctx.shown(folder)}"
    try:
        names = sorted({p.name for p in (script_of(h, ctx) for _, _, h in registrations(ctx.plugin_root)) if p})
    except DoctorError:
        names = list(HOOK_SCRIPTS)
    needles = ["/scripts/" + n for n in names] or ["/scripts/" + n for n in HOOK_SCRIPTS]
    count = 0
    for path in files:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "hook_non_blocking_error" not in line:
                    continue
                try:
                    att = json.loads(line).get("attachment")
                except (ValueError, AttributeError):
                    continue
                if isinstance(att, dict) and att.get("type") == "hook_non_blocking_error":
                    blob = f"{att.get('command', '')}\n{att.get('stderr', '')}"
                    if any(n in blob for n in needles):
                        count += 1
    if count:
        return "WARN", f"{count} devenv hook errors (hook_non_blocking_error) in the newest {len(files)} transcripts"
    return "PASS", f"no devenv hook errors in the newest {len(files)} transcripts"


STATIC = (("python-version", check_python), ("plugin-version", check_plugin_version),
          ("hook-scripts", check_hook_scripts), ("skill-description", check_skill_descriptions),
          ("lean-state", check_lean_state), ("dev-state", check_dev_state),
          ("rules-both-files", check_both_files), ("rules-sections", check_sections),
          ("rules-limits", check_limits), ("rules-ignored", check_ignored), ("paths", check_paths),
          ("settings-names", check_settings_names), ("legacy-install", check_legacy_install),
          ("legacy-budget-hook", check_legacy_budget_hook), ("start-load", check_start_load),
          ("hook-errors", check_hook_errors))


def run_group(pairs, ctx):
    results, internal = [], False
    for check_id, fn in pairs:
        def call(fn=fn):
            try:
                return fn(ctx)
            except DoctorError as exc:  # an unusable input is a finding, not a doctor failure
                return "FAIL", str(exc)
        out, err = guarded(check_id, call)
        results += out
        internal = internal or err
    return results, internal


def static_checks(ctx):
    return run_group(STATIC, ctx)[0]


# --- replay ----------------------------------------------------------------------------------------------
class ReplayError(Exception):
    pass


def matches(matcher, tool: str) -> bool:
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def replay_of(event, matcher):
    if event == "SessionStart":
        return "replay-session-start"
    if event == "SubagentStop":
        return "replay-stop"
    if event == "PostToolUse" and matches(matcher, "Bash"):
        return "replay-bash"
    if event == "PreToolUse" and matches(matcher, "Read"):
        return "replay-read"
    if event == "PreToolUse" and matches(matcher, HANDBACK_TOOL):
        return "replay-handback"
    return None


def over(lines: int, size: int) -> str:
    """The shorter of two texts: one over `lines` lines, one over `size` bytes."""
    by_lines = "x\n" * (lines + 5)
    by_bytes = "x" * (size + 5) + "\n"
    return by_lines if len(by_lines) <= len(by_bytes) else by_bytes


def text_lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


def run_hook(argv, payload, cwd):
    try:
        proc = subprocess.run(argv, input=json.dumps(payload).encode("utf-8"), capture_output=True,
                              timeout=TIMEOUT, cwd=str(cwd))
    except FileNotFoundError:
        raise ReplayError(f"interpreter not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise ReplayError(f"timed out after {TIMEOUT} s")
    except OSError as exc:
        raise ReplayError(f"cannot run {argv[0]} ({type(exc).__name__})")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ReplayError(f"exit {proc.returncode}: {err[-1] if err else '(no stderr)'}")
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        raise ReplayError("stdout is not one JSON object")
    if not isinstance(data, dict):
        raise ReplayError("stdout is not one JSON object")
    return data


def denied(out) -> bool:
    spec = (out or {}).get("hookSpecificOutput") or {}
    return spec.get("hookEventName") == "PreToolUse" and spec.get("permissionDecision") == "deny"


def blocked(out) -> bool:
    return (out or {}).get("decision") == "block" or denied(out)


def bash_shape_problems(updated, original) -> list:
    """The doctor's own check of a Bash replacement object (independent of the hook's code)."""
    if not isinstance(updated, dict):
        return ["replacement is not an object"]
    problems = []
    for key, kind in BASH_KEYS.items():
        if key not in updated:
            problems.append(f"missing key {key}")
        elif type(updated[key]) is not kind:
            problems.append(f"{key} is {type(updated[key]).__name__}, not {kind.__name__}")
    problems += [f"original key dropped: {k}" for k in original if k not in updated]
    return problems


def limits_for(user_file, cwd: Path):
    rl = sibling("rules_layer")
    top, main_tree = git_layers(cwd)
    project = rl.project_file(top, main_tree)[0] if top else None
    return rl.limits([user_file, project])[0]


def event_rows(root: Path):
    path = root / "events.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


class Replay:
    """One registration replayed in its own temporary folder."""

    def __init__(self, ctx, handler, base: Path):
        self.ctx = ctx
        self.base = base
        self.root = base / "state"
        self.data = base / "data"
        self.proj = base / "project"
        for folder in (self.root, self.data, self.proj):
            folder.mkdir(parents=True)
        self.argv = command_line(handler, ctx, self.data) + ["--root", str(self.root)]
        self.ds = sibling("devenv_state")
        self.ds.dev_set(True, root=self.root)  # the replay checks that its own events are recorded
        self.lean(True)
        self.cases = []

    def lean(self, on: bool):
        self.ds.lean_set(on, str(self.proj), root=self.root)

    def payload(self, event, **fields):
        base = {"session_id": SESSION, "transcript_path": str(self.base / "transcript.jsonl"),
                "cwd": str(self.proj), "permission_mode": "default", "hook_event_name": event}
        base.update(fields)
        return base

    def case(self, name, payload, expect, extra=()):
        """Run one input; `expect(out)` returns None when right, else the problem."""
        try:
            out = run_hook(self.argv + list(extra), payload, self.proj)
            problem = expect(out)
        except ReplayError as exc:
            problem = str(exc)
        self.cases.append((name, problem))

    def events(self, name, test):
        if not any(test(r) for r in event_rows(self.root)):
            self.cases.append((name, "no matching event in events.jsonl"))
        else:
            self.cases.append((name, None))


def expect_deny(out):
    return None if denied(out) else f"not denied ({json.dumps(out)[:120] if out else 'no output'})"


def expect_pass(out):
    return f"blocked: {json.dumps(out)[:120]}" if blocked(out) else None


def replay_read(ctx, handler, base, matcher):
    uncovered = [t for t in READ_TOOLS if not matches(matcher, t)]
    if uncovered:
        return [("matcher covers Read, Write, Edit, Bash", f"matcher {matcher!r} misses " + ", ".join(uncovered))]
    r = Replay(ctx, handler, base)
    limits = limits_for(r.root / "devenv.local.md", r.proj)  # hook_read reads the user layer under --root
    big = r.proj / "big.txt"
    big.write_text(over(limits["read_lines"], limits["read_bytes"]), encoding="utf-8")
    small = r.proj / "small.txt"
    small.write_text("ok\n", encoding="utf-8")

    def tool(name, **tool_input):
        return r.payload("PreToolUse", tool_name=name, tool_input=tool_input, tool_use_id="toolu_doctor")

    state_tool = f"devenv-py {ctx.plugin_root}/scripts/devenv_state.py"
    r.case("deny large Read", tool("Read", file_path=str(big)), expect_deny)
    r.case("pass small Read", tool("Read", file_path=str(small)), expect_pass)
    r.case("deny lean off command", tool("Bash", command=f"{state_tool} lean off --cwd {r.proj} --py {ctx.python}"),
           expect_deny)
    r.case("deny Write of the lean state file",
           tool("Write", file_path=str(r.root / "lean-mode.json"), content="{}"), expect_deny)
    dev_off = f"devenv-py {ctx.plugin_root}/scripts/doctor.py dev off --py {ctx.python}"
    r.case("pass dev off", tool("Bash", command=dev_off),
           expect_pass)
    r.lean(False)
    r.case("pass large Read with lean off", tool("Read", file_path=str(big)), expect_pass)
    r.events("record read_deny", lambda e: e.get("kind") == "read_deny" and e.get("guard") == 0)
    return r.cases


def replay_bash(ctx, handler, base, matcher):
    if not matches(matcher, "Bash"):
        return [("matcher covers Bash", f"matcher {matcher!r} misses Bash")]
    r = Replay(ctx, handler, base)
    limits = limits_for(sibling("rules_layer").user_file()[0], r.proj)  # hook_bash reads the home's user layer
    big = over(limits["bash_lines"], limits["bash_bytes"])

    def post(stdout):
        response = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False,
                    "noOutputExpected": False}
        return r.payload("PostToolUse", tool_name="Bash", tool_input={"command": "doctor replay"},
                         tool_response=response, tool_use_id="toolu_doctor"), response

    payload, original = post(big)

    def replaced(out):
        spec = (out or {}).get("hookSpecificOutput") or {}
        if spec.get("hookEventName") != "PostToolUse" or "updatedToolOutput" not in spec:
            return f"not replaced ({json.dumps(out)[:120] if out else 'no output'})"
        problems = bash_shape_problems(spec["updatedToolOutput"], original)
        if problems:
            return "bad replacement shape: " + "; ".join(problems)
        if spec["updatedToolOutput"]["stdout"] == big:
            return "stdout not shortened"
        return None

    def untouched(out):
        spec = (out or {}).get("hookSpecificOutput") or {}
        return "replaced" if "updatedToolOutput" in spec else None

    r.case("replace large output", payload, replaced)
    r.case("pass small output", post("ok\n")[0], untouched)
    r.lean(False)
    r.case("pass large output with lean off", payload, untouched)
    r.events("record bash_replace", lambda e: e.get("kind") == "bash_replace" and e.get("replaced") == 1)
    return r.cases


def replay_stop(ctx, handler, base, matcher):
    r = Replay(ctx, handler, base)
    limit = limits_for(sibling("rules_layer").user_file()[0], r.proj)["return_lines"]
    long_text = text_lines(limit + 1)

    def stop(text, active=False):
        return r.payload("SubagentStop", stop_hook_active=active, agent_id="adoctorreplay",
                         agent_type="general-purpose", last_assistant_message=text,
                         agent_transcript_path=str(r.base / "agent.jsonl"))

    def expect_block(out):
        return None if (out or {}).get("decision") == "block" else "not blocked"

    r.case(f"block {limit + 1}-line return", stop(long_text), expect_block)
    r.case("pass short return", stop("done"), expect_pass)
    r.case("pass stop_hook_active", stop(long_text, active=True), expect_pass)
    r.lean(False)
    r.case("pass long return with lean off", stop(long_text), expect_pass)
    r.events("record return_block", lambda e: e.get("kind") == "return_block")
    return r.cases


def replay_handback(ctx, handler, base, matcher):
    if not matches(matcher, HANDBACK_TOOL):
        return [(f"matcher covers {HANDBACK_TOOL}", f"matcher {matcher!r} misses {HANDBACK_TOOL}")]
    r = Replay(ctx, handler, base)
    limit = limits_for(sibling("rules_layer").user_file()[0], r.proj)["return_lines"]
    long_text = text_lines(limit + 1)

    def handback(agent, text):
        return r.payload("PreToolUse", tool_name=HANDBACK_TOOL, tool_input={"message": text}, agent_id=agent,
                         agent_type="general-purpose", tool_use_id="toolu_doctor")

    r.case(f"deny {limit + 1}-line report", handback("adoctor1", long_text), expect_deny)
    r.case("pass the retry", handback("adoctor1", long_text), expect_pass)
    r.case("pass short report", handback("adoctor2", "done"), expect_pass)
    r.lean(False)
    r.case("pass long report with lean off", handback("adoctor3", long_text), expect_pass)
    r.events("record handback_deny", lambda e: e.get("kind") == "handback_deny")
    return r.cases


def replay_session_start(ctx, handler, base, matcher):
    r = Replay(ctx, handler, base)
    extra = ["--log", str(r.base / "context-budget.log")]
    payload = r.payload("SessionStart", source="startup")

    def context(out):
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext")

    def notice(out):
        text = context(out)
        return None if isinstance(text, str) and text.startswith("lean-mode: on") else "no lean-mode notice"

    def silent(out):
        return f"notice with lean off: {context(out)[:80]}" if context(out) else None

    r.case("lean notice", payload, notice, extra)
    r.lean(False)
    r.case("no notice with lean off", payload, silent, extra)
    r.events("record session_start", lambda e: e.get("kind") == "session_start")
    return r.cases


REPLAY_FN = {"replay-session-start": replay_session_start, "replay-read": replay_read,
             "replay-handback": replay_handback, "replay-bash": replay_bash, "replay-stop": replay_stop}


def replay_checks(ctx, only=None):
    wanted = [cid for cid in REPLAYS if only is None or cid in only]
    try:
        regs = registrations(ctx.plugin_root)
    except DoctorError as exc:
        return [Result(cid, "FAIL", one_line(str(exc)) + " " + SCOPE) for cid in wanted]
    try:
        tmp = Path(tempfile.mkdtemp(prefix="devenv-doctor-")).resolve()  # hooks may resolve cwd symlinks
    except OSError as exc:
        return [Result(cid, "SKIP", f"no temporary folder ({type(exc).__name__}) {SCOPE}")
                for cid in wanted]
    results = []
    try:
        for cid in wanted:
            mine = [(m, h) for e, m, h in regs if replay_of(e, m) == cid]
            if not mine:
                results.append(Result(cid, "FAIL", f"not registered in hooks/hooks.json {SCOPE}"))
                continue
            cases = []
            for i, (matcher, handler) in enumerate(mine):
                try:
                    cases += REPLAY_FN[cid](ctx, handler, tmp / f"{cid}-{i}", matcher)
                except DoctorError as exc:
                    cases.append(("registration", str(exc)))
            failed = [f"{name}: {problem}" for name, problem in cases if problem]
            if failed:
                results.append(Result(cid, "FAIL", one_line("; ".join(failed)) + " " + SCOPE))
            else:
                results.append(Result(cid, "PASS", one_line(", ".join(n for n, _ in cases)) + " " + SCOPE))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# --- live ------------------------------------------------------------------------------------------------
def current_version(ctx):
    m = re.search(r'^VERSION = "([^"]+)"', (ctx.plugin_root / "scripts" / "run_log.py").read_text(encoding="utf-8"),
                  re.M)
    return m.group(1) if m else None


def load_events(path: Path):
    """(rows, unreadable line count)."""
    rows, bad = [], 0
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def live_checks(ctx):
    version = current_version(ctx)
    rows, _ = load_events(ctx.events_file)
    rows = [r for r in rows if r.get("version") == version]
    mode = sibling("devenv_state").dev_status(root=ctx.state_root)["state"]
    results = []
    for cid, kind in LIVE:
        mine = [r for r in rows if r.get("kind") == kind]
        note = ""
        if kind == "read_deny":
            guard = sum(1 for r in mine if r.get("guard") == 1)
            unknown = sum(1 for r in mine if r.get("unknown") == 1)
            fired = sum(1 for r in mine if r.get("guard") in (0, None) and r.get("unknown") in (0, None))
            note = f"; guard={guard} unknown={unknown} (counted apart)"
        elif kind == "bash_replace":
            fired = sum(1 for r in mine if r.get("replaced") == 1)
            note = (f"; not replaced={len(mine) - fired}; a replacement of the wrong shape is logged only in the "
                    "harness debug file, which live cannot read")
        else:
            fired = len(mine)
        if fired:
            results.append(Result(cid, "PASS", one_line(f"{kind}={fired} in version {version}{note}")))
        else:
            results.append(Result(cid, "SKIP", one_line(f"{NO_LIVE} {kind} in version {version}; "
                                                        f"developer mode {mode}{note}")))
    return results


# --- run, render, exit -----------------------------------------------------------------------------------
def run_all(ctx):
    results, internal = run_group(STATIC, ctx)
    for check_id, fn in (("replay", lambda: replay_checks(ctx)), ("live", lambda: live_checks(ctx))):
        out, err = guarded(check_id, fn)
        results += out
        internal = internal or err
    return results, internal


def render(results, verbose=False):
    lines = [f"DOCTOR {r.id} {r.status} {r.evidence}" for r in results if verbose or r.status != "PASS"]
    counts = {s: sum(1 for r in results if r.status == s) for s in STATUSES}
    lines.append("DOCTOR summary " + " ".join(f"{s.lower()}={counts[s]}" for s in STATUSES))
    return lines


def exit_code(results, internal):
    if internal:
        return 2
    if any(r.status == "FAIL" or (r.status == "SKIP" and r.id.startswith("replay")) for r in results):
        return 1
    return 0


# --- dev and init-project --------------------------------------------------------------------------------
def dev_line(st) -> str:
    since = f" (since {st['since']})" if st.get("since") else ""
    error = f" ({st['error']})" if st.get("error") else ""
    return f"developer mode: {st['state']}{since}{error}"


def dev_report(ctx) -> int:
    st = sibling("devenv_state").dev_status(root=ctx.state_root)
    if st["state"] == "off":
        print("developer mode is off, so no runs or hook events are being recorded "
              "(turn it on with /devenv:doctor dev on); older records follow if any")
    elif st["state"] == "unknown":
        print(f"developer mode is unknown ({st['error']}), so nothing is being recorded")
    else:
        print(dev_line(st))
    rows, bad = load_events(ctx.events_file)
    if not rows and not bad:
        print(f"events: none at {ctx.shown(ctx.events_file)}")
    else:
        print(f"events: {ctx.shown(ctx.events_file)} ({len(rows)} lines, {bad} unreadable)")
        for version in sorted({str(r.get("version")) for r in rows}):
            mine = [r for r in rows if str(r.get("version")) == version]
            parts = []
            for kind in sorted({str(r.get("kind")) for r in mine}):
                of_kind = [r for r in mine if str(r.get("kind")) == kind]
                part = f"{kind}={len(of_kind)}"
                if kind == "read_deny":
                    part += (f" (guard={sum(1 for r in of_kind if r.get('guard') == 1)} "
                             f"unknown={sum(1 for r in of_kind if r.get('unknown') == 1)})")
                if kind == "bash_replace":
                    part += f" (replaced={sum(1 for r in of_kind if r.get('replaced') == 1)})"
                parts.append(part)
            returned = sum(r.get("bytes") for r in mine if r.get("kind") in ("return_block", "handback_deny",
                                                                             "return_pass")
                           and isinstance(r.get("bytes"), int))
            print(f"version {version}: {' '.join(parts)} return_bytes={returned}")
    value = os.environ.get("HANDOFF_LOG")
    if value == "off":
        print("run log: off (HANDOFF_LOG=off)")
        return 0
    log = Path(value) if value else ctx.claude / "handoffs" / ".state" / "runs.jsonl"
    if not log.is_file() and not Path(str(log) + ".1").is_file():
        print(f"run log: none at {ctx.shown(log)}")
        return 0
    print(f"run log: {ctx.shown(log)}")
    sys.stdout.flush()
    return sibling("run_report").main(["--log", str(log)])


def dev_command(action, ctx) -> int:
    if action == "report":
        return dev_report(ctx)
    ds = sibling("devenv_state")
    try:
        st = ds.dev_status(root=ctx.state_root) if action == "status" else ds.dev_set(action == "on",
                                                                                       root=ctx.state_root)
    except OSError as exc:
        print(f"doctor: developer mode not changed: {exc}", file=sys.stderr)
        return 1
    print(dev_line(st))
    return 1 if st["state"] == "unknown" else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="doctor.py", description="devenv doctor")
    parser.add_argument("command", nargs="*", help="nothing (checks) | dev on|off|status|report | init-project")
    parser.add_argument("--cwd", default=None, help="project folder (default: the current folder)")
    parser.add_argument("--python", default=None, help="interpreter the hooks run with (default: this one)")
    parser.add_argument("--home", default=None, help="home folder stand-in for tests")
    parser.add_argument("--verbose", action="store_true", help="print PASS lines too")
    parser.add_argument("--append-gitignore", action="store_true", help="init-project: append the line")
    args = parser.parse_intermixed_args(argv)
    try:
        ctx = Context(home=args.home or Path.home(), cwd=args.cwd or os.getcwd(),
                      python=args.python or sys.executable)
        cmd = args.command
        if cmd[:1] == ["dev"] and len(cmd) == 2 and cmd[1] in ("on", "off", "status", "report"):
            return dev_command(cmd[1], ctx)
        if cmd == ["init-project"]:
            code, lines = sibling("rules_layer").init_project(ctx.cwd, append_gitignore=args.append_gitignore)
            for line in lines:
                print(line)
            return code
        if cmd:
            parser.print_usage(sys.stderr)
            print(f"doctor.py: error: unknown command: {' '.join(cmd)}", file=sys.stderr)
            return 2
        results, internal = run_all(ctx)
        for line in render(results, verbose=args.verbose):
            print(line)
        return exit_code(results, internal)
    except Exception as exc:  # the doctor's own failure is exit 2, with one line
        print(f"doctor.py: internal error ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
