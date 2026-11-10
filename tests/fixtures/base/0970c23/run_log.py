"""Run log shared by handoff_state.py and check_handoff.py (spec section 12, A12).

Each script run appends one JSON line to <home>/.claude/handoffs/.state/runs.jsonl
so a later session can reconstruct what happened. HANDOFF_LOG overrides the path;
the value "off" disables logging. A handoff file body is never recorded, only its path;
indented stdout lines are dropped and only counted (omitted_lines). The only environment
variables read are CLAUDE_CODE_SESSION_ID (kept only when it is a UUID) and HANDOFF_CALLER (kept
only when it is in CALLERS, and only on a check_handoff.py record); nothing else from the environment
is recorded.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2026.11.09-3"
MAX_BYTES = 5 * 1024 * 1024
SESSION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")  # UUID only
CALLERS = ("handoff_state.py",)


def log_path():
    """Path of the run log, or None when logging is off."""
    value = os.environ.get("HANDOFF_LOG")
    if value == "off":
        return None
    if value:
        return Path(value)
    return Path.home() / ".claude" / "handoffs" / ".state" / "runs.jsonl"


def build_record(*, script, mode, cwd, toplevel, args, exit_code, stdout_lines,
                 handoff=None, cont=None, checker=None, project=None, fail_codes=None,
                 handoff_bytes=None):
    """Record fields of spec section 12 except ts and version, which append_run fills.

    Indented stdout lines (checker findings, item excerpts) can quote the handoff body, so they are
    counted in omitted_lines and never recorded.
    """
    every = list(stdout_lines)
    lines = [line for line in every if not line[:1].isspace()]
    return {
        "script": script,
        "mode": mode,
        "cwd": cwd,
        "toplevel": toplevel,
        "args": list(args),
        "exit": exit_code,
        "stdout": lines,
        "omitted_lines": len(every) - len(lines),
        "warnings": [line for line in lines if "WARNING:" in line],
        "handoff": handoff,
        "continue": cont,
        "checker": checker,
        "project": project,
        "fail_codes": fail_codes,
        "handoff_bytes": handoff_bytes,
    }


def env_signals(script):
    """session_id and caller from the two named environment variables, null when absent or invalid.
    caller marks the checker run handoff_state.py started, so any other script's record gets null."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    caller = os.environ.get("HANDOFF_CALLER")
    return {
        "session_id": session if session and SESSION_ID.fullmatch(session) else None,
        "caller": caller if script == "check_handoff.py" and caller in CALLERS else None,
    }


def append_run(record):
    """Append one record. Return None on success, or the reason when it was not written."""
    path = log_path()
    if path is None:
        return None
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rest = {k: v for k, v in record.items()
                if k not in ("ts", "version", "session_id", "caller")}
        full = {"ts": ts, "version": VERSION, **rest, **env_signals(rest.get("script"))}
        line = json.dumps(full, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None
