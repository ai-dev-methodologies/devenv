#!/usr/bin/env python3
"""SessionStart hook: records the start load of instruction files and tells a lean session about it.

  hook_session_start.py [--log PATH] [--root DIR]

Reads one SessionStart input (JSON object) on stdin; `cwd` (else the process folder) is the project and
`session_id` picks a session-scoped lean state.

1. Measures the files a session loads at start (context_budget.measure, next to this file) and appends the
   same line context_budget.py --record writes:
   `context-budget <UTC> <cwd> total=<bytes> cap=49152 status=ok|over files=<n> missing=<n>`
   to PATH (default ~/.claude/logs/context-budget.log; no environment variable changes it).
2. Lean mode on (devenv_state.lean_status, strict): prints one SessionStart `additionalContext` line, the
   `lean-mode: on (since …)` notice with the instruction to follow the lean skill's operating rules, plus
   the total and the three largest files when the total is over the cap. Lean off prints nothing; an
   unreadable lean state prints a `lean-mode: unknown` notice. The line is capped at CONTEXT_CHARS, far
   under the harness's 10,000-character field limit.
3. events.record("session_start", lean_on, start_bytes=<total>) (developer mode only). A record that
   failed is shown to the user as a `systemMessage`.

The cap is CAP (48 KiB, the design's start budget); no rules layer section sets it. start_verdict() is the
pure check the doctor shares: total over the cap is FAIL.

`--root DIR` (tests and replay only) replaces the state folder for devenv_state and events.record.
Exit codes: 0 done (with or without output). A usage error, input that is not a JSON object, a log line
that cannot be written, or an internal error prints `hook_session_start: <reason>` on stderr and exits 1:
the session still starts (SessionStart cannot be blocked) and the harness shows the hook error. Exit 2 is
never used.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path

CAP = 49152  # 48 * 1024, the same default as context_budget.DEFAULT_CAP
LARGEST = 3
PATH_CHARS = 160
CONTEXT_CHARS = 2000
LEAN_RULE = "follow the lean skill's operating rules (devenv:lean) for this session"


class ArgumentError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(f"devenv_{name}", Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_verdict(rows, cap=CAP) -> dict:
    """Pure: rows are (path, size or None) as context_budget.measure returns them.
    {"status": "PASS"|"FAIL", "total", "cap", "largest": [(path, size)] (at most 3, largest first)}."""
    sized = [(p, s) for p, s in rows if s is not None]
    total = sum(s for _, s in sized)
    largest = sorted(sized, key=lambda row: (-row[1], str(row[0])))[:LARGEST]
    return {"status": "FAIL" if total > cap else "PASS", "total": total, "cap": cap, "largest": largest}


def one_line(text: str) -> str:
    """Control characters (a newline in a file name, for one) become `?`, so the notice stays one line."""
    return "".join("?" if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)


def shown_path(path: Path, home: Path) -> str:
    text = str(path)
    if text.startswith(str(home) + os.sep):
        text = "~" + text[len(str(home)):]
    text = one_line(text)
    return text if len(text) <= PATH_CHARS else "…" + text[-(PATH_CHARS - 1):]


def notice(lean: dict, verdict: dict, home: Path) -> str | None:
    """The additionalContext line, or None when lean mode is off."""
    if lean.get("on") is None:
        return "lean-mode: unknown (state file unreadable); run /devenv:lean status to check it"
    if not lean.get("on"):
        return None
    since = f" (since {lean['since']})" if lean.get("since") else ""
    parts = [f"lean-mode: on{since}; {LEAN_RULE}"]
    if verdict["status"] == "FAIL":
        big = ", ".join(f"{shown_path(p, home)} {s}" for p, s in verdict["largest"])
        parts.append(f"context-budget over: total={verdict['total']} cap={verdict['cap']}; largest: {big}")
    line = one_line(" · ".join(parts))
    return line if len(line) <= CONTEXT_CHARS else line[:CONTEXT_CHARS - 1] + "…"


def run(args, data) -> int:
    budget = load_sibling("context_budget")
    raw_cwd = data.get("cwd")
    given = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else os.getcwd()
    # The budget measures the resolved folder; lean mode is judged on the cwd as given, like the other hooks,
    # so devenv_state.git_project alone decides the project key.
    cwd = Path(given).resolve()
    rows = budget.measure(cwd)
    line, total = budget.build_line(cwd, CAP, rows)
    log = Path(args.log) if args.log else budget.home_dir() / ".claude" / "logs" / "context-budget.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"hook_session_start: context-budget line not written to {log} ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 1
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    lean = load_sibling("devenv_state").lean_status(given, session_id, root=args.root, strict=True)
    failed = load_sibling("events").record("session_start", lean_on=lean.get("on"), root=args.root,
                                           start_bytes=total)
    out = {}
    text = notice(lean, start_verdict(rows), budget.home_dir())
    if text is not None:
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": text}
    if failed:
        out["systemMessage"] = f"devenv: session_start event not recorded ({one_line(failed)[:200]})"
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    try:
        parser = Parser(prog="hook_session_start.py", description="devenv SessionStart hook")
        parser.add_argument("--log", default=None, help="context-budget log file (default under ~/.claude/logs)")
        parser.add_argument("--root", default=None, help="state folder for tests and replay only")
        args = parser.parse_args(argv)
    except ArgumentError as exc:
        print(f"hook_session_start: {exc}", file=sys.stderr)
        return 1
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        print(f"hook_session_start: stdin is not JSON ({exc})", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("hook_session_start: stdin is not a JSON object", file=sys.stderr)
        return 1
    try:
        return run(args, data)
    except Exception as exc:  # report every engine error; never exit 0 over it, never 2
        tail = traceback.format_exc().strip().splitlines()[-3:]
        print(f"hook_session_start: internal error ({type(exc).__name__}: {exc})", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
