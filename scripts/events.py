"""Developer mode enforcement record: one JSON line per hook decision.

record() appends {"ts", "version", "kind", "lean_on", <counters>} to
<home>/.claude/handoffs/.state/events.jsonl (next to runs.jsonl, a separate file, so the run log format
does not change). It writes only while developer mode is on (devenv_state.py next to this file).
Counters hold integers only (bool excluded): any other value is stored as null, and a counter name that
is not a short lower-case identifier is dropped, so no file body, command text or secret can reach the
record. `version` is run_log.VERSION.

`root=` (tests and replay only; no environment variable changes it) replaces both the state folder that
holds the developer mode file and the folder of events.jsonl, so a replay never reads or writes the real
files. The report over this file belongs to the doctor, not here.

Hooks call record(), so it never raises: it returns None when the line was written or developer mode is
off, and the reason (a short string) when developer mode cannot be read or the write failed.
Deliberate shortcut: the file has no size limit or rotation (developer mode only, a few hundred bytes per
event); add rotation like run_log.MAX_BYTES if a developer mode file ever grows past a few MiB.
"""
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

FILE_NAME = "events.jsonl"
KINDS = ("read_deny", "bash_replace", "return_block", "handback_deny", "return_pass", "session_start")
COUNTER = re.compile(r"[a-z][a-z0-9_]{0,31}")
RESERVED = ("ts", "version", "kind", "lean_on")


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(f"devenv_{name}", Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events_path(root=None):
    """events.jsonl under `root`, or the default under the home folder."""
    if root is not None:
        return Path(root) / FILE_NAME
    return Path.home() / ".claude" / "handoffs" / ".state" / FILE_NAME


def integer_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_line(kind, lean_on, version, counters):
    """The JSON line (with its newline) for one event."""
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": version,
        "kind": kind,
        "lean_on": lean_on if isinstance(lean_on, bool) else None,
    }
    for name, value in counters.items():
        if isinstance(name, str) and COUNTER.fullmatch(name) and name not in RESERVED:
            rec[name] = integer_or_none(value)
    return json.dumps(rec, ensure_ascii=False) + "\n"


def record(kind, *, lean_on, root=None, **counters):
    """Append one event line when developer mode is on. Never raises (see the module docstring)."""
    try:
        if kind not in KINDS:
            return "unknown event kind"
        state = load_sibling("devenv_state").dev_status(root=root)
        if state.get("state") == "off":
            return None
        if state.get("state") != "on":
            return f"developer mode unknown: {state.get('error')}"
        data = build_line(kind, lean_on, load_sibling("run_log").VERSION, counters).encode("utf-8")
        path = events_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # one write call with O_APPEND, so lines from hooks running at the same time do not interleave
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        if written != len(data):
            return f"short write: {written} of {len(data)} bytes"
    except Exception as exc:  # a hook must never die on the record
        return f"{type(exc).__name__}: {exc}"
    return None
