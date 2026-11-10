#!/usr/bin/env python3
"""Return length gate for subagents: a reply longer than `return_lines` goes back once to be rewritten.

Two hook points, one rule (count_lines over the reply text, blank lines included):
  hook_return.py stop [--root DIR]
      SubagentStop. Reads `last_assistant_message`. Blocks with {"decision": "block", "reason": ...} when
      the reply is too long. Passes when `stop_hook_active` is true (so it blocks at most once), when
      `agent_type` is empty (internal agents), and when the reply field is missing: in auto permission
      mode the report goes through the SubagentHandback tool, SubagentStop carries no reply and a block
      there is discarded, so the `handback` point below does the work.
  hook_return.py handback --data DIR [--root DIR]
      PreToolUse on SubagentHandback. Reads `tool_input.message` and denies a too long report once per
      `agent_id`; the subagent then calls the tool again. The once-only marker is DIR/handback/<agent_id>
      (DIR is the plugin data folder). When the marker cannot be written the call is not denied, so a
      broken data folder can never deny every retry.

Both points act only while lean mode is on for the input `cwd` and `session_id` (devenv_state.lean_status, strict).
An unreadable lean state does not block: the hook shows a `systemMessage` warning and records the pass.
The limit comes from rules_layer.limits (user layer, then project layer; default 30).

`--root` (tests and replay only) replaces the state folder for devenv_state and events.record; no
environment variable changes it. A broken stdin JSON passes. Usage errors and engine errors exit 1, never
0 (that would hide a broken registration) and never 2 (that would block the subagent or the tool).
Deliberate shortcut: markers are never pruned (one empty file per subagent that was denied); prune by age
if the handback folder ever grows past a few thousand files.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXIT_ERROR = 1
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
HANDBACK_TOOL = "SubagentHandback"


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(f"devenv_{name}", Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}.py next to {Path(__file__).name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


devenv_state = load_sibling("devenv_state")
events = load_sibling("events")
rules_layer = load_sibling("rules_layer")


def count_lines(text: str) -> int:
    """Lines in `text` as a reader sees them: blank lines count, a final newline does not add one."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def reason_text(lines: int, limit: int, retry: str) -> str:
    return (f"{lines}-line return; the limit is {limit} lines. Rewrite it as a summary of at most {limit} "
            f"lines and put the full detail in a file, naming its path in the summary. {retry}")


# --- input ---------------------------------------------------------------------------------------------
def git_out(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def limit_for(cwd) -> int:
    """`return_lines` from the user layer, then the project layer of `cwd` (none outside git)."""
    files = [rules_layer.user_file()[0]]
    if isinstance(cwd, str) and cwd and Path(cwd).is_dir():
        top = git_out(Path(cwd), "rev-parse", "--show-toplevel")
        if top:
            git_dir = git_out(Path(cwd), "rev-parse", "--path-format=absolute", "--git-dir")
            common = git_out(Path(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir")
            linked = bool(git_dir and common and Path(git_dir) != Path(common))
            files.append(rules_layer.project_file(Path(top), Path(common).parent if linked else None)[0])
    values, warns = rules_layer.limits(files)
    for warn in warns:
        print(f"hook_return: {warn}", file=sys.stderr)
    return values["return_lines"]


def text_or_none(value):
    return value if isinstance(value, str) else None


# --- decisions -----------------------------------------------------------------------------------------
def record(kind, lean_on, root, **counters):
    problem = events.record(kind, lean_on=lean_on, root=root, **counters)
    if problem:
        print(f"hook_return: event not recorded: {problem}", file=sys.stderr)


def lean_gate(payload, root):
    """(lean_on, output) where output is the warning JSON when the lean state is unknown, else None."""
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    status = devenv_state.lean_status(payload.get("cwd"), session_id, root=root, strict=True)
    if status["scope"] == "unknown":
        return None, {"systemMessage": f"devenv: lean state unreadable ({status['error']}); "
                                       "the return length gate let this reply through"}
    return bool(status["on"]), None


def decide_stop(payload, root):
    text = text_or_none(payload.get("last_assistant_message"))
    if text is None:
        return None  # auto mode: the report went through SubagentHandback
    size = len(text.encode("utf-8"))
    lean_on, warning = lean_gate(payload, root)
    if warning:
        record("return_pass", None, root, bytes=size, lean_unknown=1)
        return warning
    agent_type = payload.get("agent_type")
    if (not lean_on or payload.get("stop_hook_active") is True
            or not (isinstance(agent_type, str) and agent_type.strip())):
        record("return_pass", lean_on, root, bytes=size)
        return None
    lines, limit = count_lines(text), limit_for(payload.get("cwd"))
    if lines <= limit:
        record("return_pass", lean_on, root, bytes=size)
        return None
    record("return_block", lean_on, root, lines=lines, bytes=size)
    return {"decision": "block", "reason": reason_text(lines, limit, "Then finish again.")}


def mark_once(data, agent_id) -> bool:
    """True when this call created the marker (deny now); False when it existed or cannot be written."""
    if not (isinstance(agent_id, str) and SAFE_ID.fullmatch(agent_id)):
        print("hook_return: no usable agent_id; not denying", file=sys.stderr)
        return False
    if not data or not Path(data).is_absolute():
        print(f"hook_return: --data is not an absolute path ({data!r}); not denying", file=sys.stderr)
        return False
    folder = Path(data) / "handback"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(folder / agent_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        return False
    except OSError as exc:
        print(f"hook_return: cannot write the marker ({exc}); not denying", file=sys.stderr)
        return False
    os.close(fd)
    return True


def decide_handback(payload, root, data):
    if payload.get("tool_name") != HANDBACK_TOOL:
        return None
    tool_input = payload.get("tool_input")
    text = text_or_none(tool_input.get("message")) if isinstance(tool_input, dict) else None
    if text is None:
        return None
    size = len(text.encode("utf-8"))
    lean_on, warning = lean_gate(payload, root)
    if warning:
        record("return_pass", None, root, bytes=size, lean_unknown=1)
        return warning
    if not lean_on:
        record("return_pass", lean_on, root, bytes=size)
        return None
    lines, limit = count_lines(text), limit_for(payload.get("cwd"))
    if lines <= limit or not mark_once(data, payload.get("agent_id")):
        record("return_pass", lean_on, root, bytes=size)
        return None
    record("handback_deny", lean_on, root, lines=lines, bytes=size)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason_text(lines, limit, f"Then call {HANDBACK_TOOL} again."),
    }}


# --- CLI -----------------------------------------------------------------------------------------------
class Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 by default, which blocks on these events
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


def main(argv=None) -> int:
    parser = Parser(prog="hook_return.py", description="devenv return length gate")
    sub = parser.add_subparsers(dest="point", required=True, parser_class=Parser)
    stop = sub.add_parser("stop", help="SubagentStop")
    stop.add_argument("--root", default=None, help="state folder for tests and replay only")
    handback = sub.add_parser("handback", help=f"PreToolUse on {HANDBACK_TOOL}")
    handback.add_argument("--data", required=True, help="plugin data folder (holds the once-only markers)")
    handback.add_argument("--root", default=None, help="state folder for tests and replay only")
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        print(f"hook_return: input is not JSON ({exc}); passing", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("hook_return: input is not a JSON object; passing", file=sys.stderr)
        return 0
    if args.point == "stop":
        out = decide_stop(payload, args.root)
    else:
        out = decide_handback(payload, args.root, args.data)
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
