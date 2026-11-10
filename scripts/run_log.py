"""Run log shared by handoff_state.py and check_handoff.py (spec section 12, A12).

Each script run appends one JSON line to <home>/.claude/handoffs/.state/runs.jsonl
so a later session can reconstruct what happened. HANDOFF_LOG overrides the path;
the value "off" disables logging. Without HANDOFF_LOG the default path is written only while developer
mode is on (devenv_state.py next to this file); when that state cannot be read nothing is written and
append_run returns the reason. A handoff file body is never recorded, only its path;
indented stdout lines are dropped and only counted (omitted_lines). The only environment
variables read are CLAUDE_CODE_SESSION_ID (kept only when it is a UUID) and HANDOFF_CALLER (kept
only when it is in CALLERS, and only on a check_handoff.py record); nothing else from the environment
is recorded.
The gate counters (after_handoff_lines, before_handoff_lines, before_unrelated, attempt_n, elapsed_ms,
reviews_found, reviews_recorded) hold integers only: any other value is stored as null, so no
message or review text can reach the log through them.
"""
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2026.11.10-2"
MAX_BYTES = 5 * 1024 * 1024
SESSION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")  # UUID only
CALLERS = ("handoff_state.py",)
INT_FIELDS = ("after_handoff_lines", "before_handoff_lines", "before_unrelated", "attempt_n",
              "elapsed_ms", "reviews_found", "reviews_recorded")


def dev_status():
    """devenv_state.dev_status() of the sibling module; a module that cannot be loaded counts as unknown."""
    try:
        spec = importlib.util.spec_from_file_location("devenv_state", Path(__file__).with_name("devenv_state.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.dev_status()
    except Exception as exc:
        return {"state": "unknown", "since": None, "error": f"cannot load devenv_state.py ({type(exc).__name__})"}


def log_target():
    """(path, reason). path is where append_run writes, or None when logging is off; reason is set only when
    HANDOFF_LOG is unset and developer mode cannot be read (nothing is written then)."""
    value = os.environ.get("HANDOFF_LOG")
    if value == "off":
        return None, None
    if value:
        return Path(value), None
    state = dev_status()
    if state.get("state") == "on":
        return Path.home() / ".claude" / "handoffs" / ".state" / "runs.jsonl", None
    if state.get("state") == "off":
        return None, None
    return None, f"developer mode unknown: {state.get('error')}"


def log_path():
    """Path of the run log, or None when logging is off (HANDOFF_LOG=off, or developer mode not on)."""
    return log_target()[0]


def build_record(*, script, mode, cwd, toplevel, args, exit_code, stdout_lines,
                 handoff=None, cont=None, checker=None, project=None, fail_codes=None,
                 handoff_bytes=None, after_handoff_lines=None, before_handoff_lines=None,
                 before_unrelated=None, attempt_n=None, elapsed_ms=None, reviews_found=None,
                 reviews_recorded=None):
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
        "after_handoff_lines": integer_or_none(after_handoff_lines),
        "before_handoff_lines": integer_or_none(before_handoff_lines),
        "before_unrelated": integer_or_none(before_unrelated),
        "attempt_n": integer_or_none(attempt_n),
        "elapsed_ms": integer_or_none(elapsed_ms),
        "reviews_found": integer_or_none(reviews_found),
        "reviews_recorded": integer_or_none(reviews_recorded),
    }


def integer_or_none(value):
    """The value when it is an int (bool excluded), else None, so text never lands in an INT_FIELDS key."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def handoff_path(raw):
    """The handoff path as a run record keeps it: expanduser, then absolute against this process's cwd.

    A `~user` that does not resolve (RuntimeError) is kept unexpanded, so the record is still written.
    """
    try:
        return str(Path(raw).expanduser().absolute())
    except RuntimeError:
        return str(Path(raw).absolute())


def attempt_number(session_id, handoff):
    """1 + the earlier direct check_handoff.py runs of this session on this handoff file, or None.

    Paths compare as handoff_path gives them on both sides. 1 when the log file does not exist yet.
    None when logging is off, the log cannot be read, or session_id or handoff is empty. Lines that do
    not parse are skipped.
    """
    try:
        path = log_path()
        if path is None or not session_id or handoff is None:
            return None
        if not path.exists():
            return 1
        key = handoff_path(handoff)
        earlier = 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                if session_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                    if (rec["script"] == "check_handoff.py" and rec["caller"] is None
                            and rec["session_id"] == session_id
                            and handoff_path(rec["handoff"]) == key):
                        earlier += 1
                except (ValueError, KeyError, TypeError):
                    continue
        return earlier + 1
    except (OSError, ValueError, TypeError):
        return None


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
    """Append one record. Return None on success or when logging is off, else the reason it was not written."""
    path, reason = log_target()
    if reason:
        return reason
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
