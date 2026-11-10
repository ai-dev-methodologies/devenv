#!/usr/bin/env python3
"""PreToolUse hook for Read, Write, Edit and Bash: large Read denial and the lean-off guard.

Reads one PreToolUse input (JSON) on stdin. It acts only in a project or session where lean mode is on
(devenv_state.lean_status, strict, for the input's `cwd` and `session_id`); every other call passes with no output.

- Read (main thread only: a subagent call, with `agent_id` in the input, passes): a text file over
  `read_bytes` or `read_lines` (rules_layer.limits) is denied when `allow_large` is true, `limit` is missing,
  or `limit` is over `read_lines`. Files under the handoffs folder and binary files (a NUL byte in the first
  8 KiB) pass. The reason names the size, the offset/limit way to read a part and delegation; it never says
  how to turn enforcement off.
- Lean-off guard (main thread and subagents): a Write or Edit of the lean state file is denied, and so is a
  Bash command whose normalized text names the state tool (the stem `devenv_stat`, so `devenv_stat?.py`,
  `DEVENV_STATE.PY` and `python -m devenv_state` count) and has both `lean` and `off` as words anywhere.
  Normalizing joins line continuations, lower-cases, expands brace lists (`of{f,}`) and drops quotes, `$`,
  backslashes, backticks, braces and commas, so a line continuation between `lean` and `off`, `$'off'` and
  `"LEAN" 'Off'` count. The words need not belong to the same command, so a harmless command that has all
  three (a grep of the state tool for the text `lean off`) is denied too, with a reason that says it was
  judged a lean-mode end.
  `dev off`, `lean status`, `lean on` and words that merely contain `off` pass. Not caught (accepted
  limit): a shell that changes the state file directly (redirection, rm, mv, chmod) and names or words
  built at run time (variables, globs other than the stem, another file name). The user's own lean-off
  skill line runs without this hook.

Lean state that cannot be read (`unknown`) never blocks: the call passes with a visible `systemMessage`
and an event. Each denial and each unknown state is recorded with events.record("read_deny", ...)
(developer mode only); counters `guard` (1 for the lean-off guard) and `unknown` (1 for an unreadable
state) tell the rows apart.

Paths: `--root DIR` (tests and replay only; no environment variable changes it) is passed to
devenv_state and events.record, and also replaces ~/.claude for the user rules layer
(DIR/devenv.local.md) and the handoffs folder (DIR/handoffs/). Without it: ~/.claude/state/lean-mode.json,
~/.claude/devenv.local.md, ~/.claude/handoffs/.

Exit codes: 0 with no output (pass) or one JSON object (deny or warning). Input that is not a JSON object,
an unknown argument, or an internal error exits 1: the call goes ahead (non-blocking) and the harness
shows the error. Exit 2 is never used, because it would block the tool call.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

STATE_STEM = "devenv_stat"  # the state tool's name without its last letter, so `devenv_stat?.py` matches too
BRACE_LIST = re.compile(r"(\S*?)\{([^{}\s]*,[^{}\s]*)\}(\S*)")  # an innermost `{a,b}` list and its word
BRACE_ROUNDS = 64  # expansions per command; a command with more lists keeps the rest unexpanded
DROPPED = re.compile(r"[\"'$\\`{},]")
WORD_BREAK = re.compile(r"[\s;&|()<>]+")
SNIFF_BYTES = 8192
CHUNK = 1 << 20
TOOLS = ("Read", "Write", "Edit", "Bash")


class ArgumentError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 by default, which would block the tool call
        raise ArgumentError(message)


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(f"devenv_{name}", Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def claude_dir(root):
    return Path(root) if root is not None else Path.home() / ".claude"


def state_file(root):
    state_root = Path(root) if root is not None else Path.home() / ".claude" / "state"
    return state_root / "lean-mode.json"


def handoffs_dir(root):
    return claude_dir(root) / "handoffs"


def user_layer(root):
    return claude_dir(root) / "devenv.local.md"


def same_path(a, b):
    """True when two absolute paths name the same file (existing files by identity, else by text)."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    if sys.platform in ("darwin", "win32"):  # case-insensitive by default
        return os.path.normcase(ra).lower() == os.path.normcase(rb).lower()
    return ra == rb


def inside(path, folder):
    real, base = os.path.realpath(path), os.path.realpath(folder)
    if sys.platform in ("darwin", "win32"):
        real, base = real.lower(), base.lower()
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def project_layer(cwd):
    """Project rules file for `cwd` (rules_layer.project_file over the git toplevel), or None."""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return None
    rules = load_sibling("rules_layer")
    code, top = rules.git(Path(cwd), "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return None
    _, git_dir = rules.git(Path(cwd), "rev-parse", "--path-format=absolute", "--git-dir")
    _, common = rules.git(Path(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir")
    linked = bool(git_dir and common and Path(git_dir) != Path(common))
    found, _ = rules.project_file(Path(top), Path(common).parent if linked else None)
    return found


def measure(path):
    """(bytes, lines, binary) of a regular file, or None when it cannot be read.
    Deliberate shortcut: counts every line of a large file (about 0.1 s per 100 MiB); cap the scan if
    hooks ever time out on huge files."""
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        lines, last = 0, b""
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
            if b"\x00" in head:
                return size, 0, True
            chunk = head
            while chunk:
                lines += chunk.count(b"\n")
                last = chunk[-1:]
                chunk = fh.read(CHUNK)
        if size and last != b"\n":
            lines += 1
        return size, lines, False
    except OSError:
        return None


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def read_decision(tool_input, cwd, root):
    """(deny output, counters) for a Read, or (None, None) when it passes."""
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        raise ValueError("Read input has no file_path string")
    if inside(path, handoffs_dir(root)):
        return None, None
    size = measure(path)
    if size is None or size[2]:
        return None, None
    nbytes, nlines, _ = size
    limits, _ = load_sibling("rules_layer").limits([user_layer(root), project_layer(cwd)])
    max_bytes, max_lines = limits["read_bytes"], limits["read_lines"]
    if nbytes <= max_bytes and nlines <= max_lines:
        return None, None
    limit = tool_input.get("limit")
    has_limit = isinstance(limit, (int, float)) and not isinstance(limit, bool)
    if tool_input.get("allow_large") is not True and has_limit and limit <= max_lines:
        return None, None
    reason = (f"[devenv] Read denied: {path} is {nlines} lines / {nbytes} bytes, over the lean limit of "
              f"{max_lines} lines / {max_bytes} bytes for one read. Read it in parts with offset and limit "
              f"(limit at most {max_lines}), e.g. offset=1 limit={min(200, max_lines)}, then the next range; "
              f"or delegate reading and summarizing it to a subagent and keep only the summary here.")
    return deny(reason), {"lines": nlines, "bytes": nbytes, "guard": 0, "unknown": 0}


GUARD_REASON = ("[devenv] Denied: lean mode state belongs to the user and this call would change it. "
                "Keep working within the lean limits.")
BASH_GUARD_REASON = (GUARD_REASON + " This command was judged to end lean mode: it names the state tool "
                     "together with the words for that.")


def expand_braces(text):
    """`pre{a,b}post` -> `preapost prebpost`, innermost lists first, at most BRACE_ROUNDS lists."""
    for _ in range(BRACE_ROUNDS):
        m = BRACE_LIST.search(text)
        if m is None:
            break
        pre, alts, post = m.groups()
        text = text[:m.start()] + " ".join(pre + alt + post for alt in alts.split(",")) + text[m.end():]
    return text


def normalized(command):
    """Command text with line continuations joined, lower case, brace lists expanded, and quotes, `$`,
    backslashes, backticks, braces and commas dropped."""
    text = command.replace("\\\r\n", "").replace("\\\n", "").lower()
    return DROPPED.sub("", expand_braces(text))


def lean_off_command(command):
    """True when the normalized command names the state tool and has the words `lean` and `off`. Deliberately
    coarse: the words may sit in another command of the same line (accepted false positive)."""
    text = normalized(command)
    words = set(WORD_BREAK.split(text))
    return STATE_STEM in text and "lean" in words and "off" in words


def guard_hit(tool, tool_input, root):
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise ValueError("Bash input has no command string")
        # known limit: a shell that changes the state file directly is not recognized here (docstring)
        return lean_off_command(command)
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        raise ValueError(f"{tool} input has no file_path string")
    return same_path(path, str(state_file(root)))


def decide(data, root):
    """(output dict or None, event counters or None, lean_on) for one PreToolUse input."""
    tool = data.get("tool_name")
    if tool not in TOOLS:
        return None, None, None
    agent = data.get("agent_id")
    if tool == "Read" and isinstance(agent, str) and agent:  # the lean-off guard covers subagents too
        return None, None, None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input is not an object")
    if tool != "Read" and not guard_hit(tool, tool_input, root):
        return None, None, None
    cwd, session = data.get("cwd"), data.get("session_id")
    status = load_sibling("devenv_state").lean_status(cwd, session if isinstance(session, str) else None,
                                                      root=root, strict=True)
    if status.get("scope") == "unknown":
        message = (f"[devenv] lean mode state unreadable ({status.get('error')}); lean enforcement skipped "
                   f"for this {tool} call.")
        return {"systemMessage": message}, {"guard": 0, "unknown": 1}, None
    if status.get("on") is not True:
        return None, None, False
    if tool == "Read":
        out, counters = read_decision(tool_input, cwd, root)
        return out, counters, True
    return deny(BASH_GUARD_REASON if tool == "Bash" else GUARD_REASON), {"guard": 1, "unknown": 0}, True


def main(argv=None):
    try:
        parser = Parser(prog="hook_read.py", description="devenv PreToolUse hook (Read, Write, Edit, Bash)")
        parser.add_argument("--root", default=None, help="state and ~/.claude stand-in for tests and replay")
        args = parser.parse_args(argv)
    except ArgumentError as exc:
        print(f"hook_read: {exc}", file=sys.stderr)
        return 1
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        print(f"hook_read: stdin is not JSON ({exc}); call passed unchecked", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("hook_read: stdin is not a JSON object; call passed unchecked", file=sys.stderr)
        return 1
    try:
        out, counters, lean_on = decide(data, args.root)
    except ValueError as exc:
        print(f"hook_read: unexpected input ({exc}); call passed unchecked", file=sys.stderr)
        return 1
    if counters is not None:
        load_sibling("events").record("read_deny", lean_on=lean_on, root=args.root, **counters)
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
