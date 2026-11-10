#!/usr/bin/env python3
"""Lean mode and developer mode state files for devenv.

Files (in the state folder, default `~/.claude/state/`):
  lean-mode.json   {"on", "since", "project", "projects": {key: {"on", "since"}},
                    "sessions": {session id: {"on", "since", "project"}}}
  devenv-dev.json  {"on", "since"}

`on`, `since` and `project` at the top level mirror the last value written, so a reader that knows only
those three keys keeps working (the resume script's `lean-mode:` line reads lean_status). A file without
`projects`, `sessions` and `session` is the older format: only the top-level keys are read, and the first
write moves its `on` entry into `projects`. An older single `session` slot ({"id", "on", "since",
"project"}) is read as a one-entry `sessions` map and moved into `sessions` on the next write.

Lean is set and judged per project key (`git_project`, the same key the resume script prints): the main
worktree of a linked worktree, else the git toplevel, else the cwd. `lean_set` stores the key of its cwd.
A stored path counts for a query when it is the query's project key or a folder inside it (or inside a
linked worktree's own toplevel, where older files stored the cwd); a folder above the key is another
project. Resolution for (cwd, session id): the `sessions` entry of that id when its project
counts; else the `projects` entry of the key itself; else any counting entry that is on, then any
counting entry (older format: the top-level `project` is the only entry); else off. Paths are compared in
`path_key` form; a path with a `..` part never matches. A file that cannot be read or does not have this
shape is `unknown`, never silently off.

Strict reading (`strict=True`, used by the enforcing hooks and by `lean_set`'s answer): an entry counts only
when it is the key itself or an existing folder whose own project key is the key (`same_project`). A
session opened in a folder above a project (a home folder, `/`, an outer repository) is therefore not on;
an older file's subfolder cwd counts while that folder exists and is inside the same project. The resume
script's line keeps the inside-counts reading above.

The state folder can be replaced only with `root=` / `--root` (tests and replay). No environment variable
changes it, so a leaked variable cannot point a hook at an empty state.

CLI (prints one JSON line; exit 0, 2 usage error, 3 state file unknown, 1 write failure):
  devenv_state.py lean on|off|status [--session ID] [--cwd PATH] [--root DIR]
  devenv_state.py dev on|off|status [--root DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LEAN_NAME = "lean-mode.json"
DEV_NAME = "devenv-dev.json"
DRIVE_RE = re.compile(r"^(?:/mnt)?/([A-Za-z])(?=/|$)")  # /c/... and /mnt/c/... -> c:/...
SINCE_RE = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
EXIT_WRITE, EXIT_USAGE, EXIT_UNKNOWN = 1, 2, 3
# Session entries are never removed by the session that ends, so they pile up. Deliberate shortcut: keep the
# latest MAX_SESSIONS by `since` on each write; a busy machine with more parallel sessions than this loses the
# oldest one's lean mode (raise the cap, or prune by the transcript's existence, if that is ever reported).
MAX_SESSIONS = 32


class StateError(Exception):
    """The state file exists but cannot be read or does not have the expected shape."""


def default_root() -> Path:
    return Path.home() / ".claude" / "state"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_key(text: str) -> str:
    """Comparable form of a path (same rule as check_handoff.path_key): `/` separators, no trailing slash,
    Windows forms lower-cased as `c:/...`."""
    s = text.strip().replace("\\", "/")
    s = DRIVE_RE.sub(lambda m: f"{m.group(1)}:", s, count=1)
    if len(s) > 1:
        s = s.rstrip("/") or "/"
    if re.match(r"^[A-Za-z]:(?:/|$)", s):
        s = s.lower()
    return s


def usable_key(path) -> str | None:
    """`path_key` of an absolute path without `..` parts, else None."""
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        key = path_key(str(Path(path).expanduser()))
    except (RuntimeError, ValueError):
        return None
    if ".." in key.split("/"):
        return None
    if not (key.startswith("/") or re.match(r"^[a-z]:(?:/|$)", key)):
        return None
    return key


def contains(outer: str, inner: str) -> bool:
    """True when `inner` is `outer` or a folder inside it (both in `usable_key` form)."""
    if outer == inner:
        return True
    return inner.startswith(outer if outer.endswith("/") else outer + "/")


def git_project(cwd) -> dict:
    """{"toplevel": str|None, "linked": bool, "project": str} for `cwd`. The project key is the main worktree
    of a linked worktree, else the git toplevel, else (outside git, or git cannot run) `cwd` as given. The
    resume script (handoff_state.py) and lean_status / lean_set share this one function."""
    def rev_parse(*args) -> str:
        try:
            proc = subprocess.run(["git", "-C", str(cwd), "rev-parse", *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    top = rev_parse("--show-toplevel")
    if not top:
        return {"toplevel": None, "linked": False, "project": Path(cwd).as_posix()}
    dirs = rev_parse("--path-format=absolute", "--git-dir", "--git-common-dir").splitlines()
    git_dir, common = dirs if len(dirs) == 2 else ("", "")
    linked = bool(git_dir and common and Path(git_dir) != Path(common))
    main = Path(common).parent if linked else Path(top)
    return {"toplevel": str(Path(top)), "linked": linked, "project": main.as_posix()}


def project_roots(cwd):
    """(project key, [keys a stored path may sit in]) for `cwd`, or None when `cwd` is not usable."""
    usable = usable_key(cwd)
    if usable is None:
        return None
    gp = git_project(usable)
    key = usable_key(gp["project"])
    if key is None:
        return None
    top = usable_key(gp["toplevel"]) if gp["toplevel"] else None
    return key, [key] + ([top] if top and top != key else [])


def counts(roots, stored) -> bool:
    """True when the stored path (in `usable_key` form, or None) is a root or a folder inside one."""
    return stored is not None and any(contains(r, stored) for r in roots)


def own_key(stored) -> str | None:
    """Project key of a stored path that is an existing folder (`git_project` of it), else None. Older files
    stored the cwd, which may be a subfolder of the project or a folder that is gone."""
    if stored is None or not os.path.isdir(stored):
        return None
    return usable_key(git_project(stored)["project"])


def same_project(key, roots, stored) -> bool:
    """Strict match: the stored path is the key itself, or a counting folder whose own project key is the key.
    A folder above a project (a home folder, `/`, an outer repository) is never the inner project."""
    return stored is not None and (stored == key or (counts(roots, stored) and own_key(stored) == key))


def off_removes(key, roots, stored) -> bool:
    """`lean_set(False)` removes the key's own entries and counting paths that are not another project's:
    an existing folder with a different project key (a nested repository, a non-git subfolder that was set
    on its own) is left alone; a counting path that no longer exists is removed with the key's entry."""
    return stored is not None and (stored == key or (counts(roots, stored) and own_key(stored) in (key, None)))


def clean_since(value):
    return value if isinstance(value, str) and SINCE_RE.fullmatch(value) else None


# --- reading -------------------------------------------------------------------------------------------
def read_json(path: Path):
    """Parsed content, or None when the file does not exist. Raises StateError for anything else."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(f"cannot read {path.name}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise StateError(f"{path.name} is not valid JSON: {exc}") from exc


def check_entry(entry, where: str, need_id: bool = False) -> None:
    if not isinstance(entry, dict) or not isinstance(entry.get("on"), bool):
        raise StateError(f"{where} needs a boolean `on`")
    if need_id and not (isinstance(entry.get("id"), str) and entry["id"]):
        raise StateError(f"{where} needs a non-empty string `id`")


def load_lean(root: Path):
    """Validated lean state (dict) or None when the file is missing. Raises StateError."""
    data = read_json(root / LEAN_NAME)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise StateError(f"{LEAN_NAME} is not a JSON object")
    check_entry(data, "top level")
    if "projects" in data:
        if not isinstance(data["projects"], dict):
            raise StateError("`projects` is not an object")
        for entry in data["projects"].values():
            check_entry(entry, "a `projects` entry")
    if "sessions" in data:
        if not isinstance(data["sessions"], dict):
            raise StateError("`sessions` is not an object")
        for sid, entry in data["sessions"].items():
            if not sid:
                raise StateError("a `sessions` entry has an empty session id")
            check_entry(entry, "a `sessions` entry")
    if "session" in data:
        check_entry(data["session"], "`session`", need_id=True)
    return data


def is_legacy(data: dict) -> bool:
    return "projects" not in data and "sessions" not in data and "session" not in data


def session_entries(data: dict) -> dict:
    """{session id: entry}: the `sessions` map, with an older single `session` slot read as one entry."""
    out = {}
    old = data.get("session")
    if old:
        out[old["id"]] = {k: v for k, v in old.items() if k != "id"}
    out.update(data.get("sessions", {}))
    return out


def project_entries(data: dict) -> dict:
    """{key: entry} for the project entries this file holds (the older format's top level as one entry)."""
    if is_legacy(data):
        key = usable_key(data.get("project"))
        if data["on"] and key:
            return {key: {"on": True, "since": data.get("since")}}
        return {}
    out = {}
    for raw_key, entry in data["projects"].items():
        key = usable_key(raw_key)
        if key:
            out[key] = entry
    return out


def result(on, scope, since=None, error=None) -> dict:
    return {"on": on, "since": clean_since(since) if on is not None else None, "scope": scope,
            "error": error}


def lean_status(cwd, session_id, *, root=None, strict=False) -> dict:
    """{"on": bool|None, "since": str|None, "scope": "session"|"project"|"none"|"unknown", "error": str|None}.
    `on` is None only with scope "unknown" (file unreadable or of the wrong shape).
    strict=True (the enforcing hooks): an entry counts only when `same_project` holds, so a session in a
    folder above a project, or in an outer repository, is not on. strict=False (the resume script's line)
    keeps the inside-counts reading of the module docstring."""
    root = Path(root) if root is not None else default_root()
    try:
        data = load_lean(root)
    except StateError as exc:
        return result(None, "unknown", error=str(exc))
    if data is None:
        return result(False, "none")
    query = project_roots(cwd)
    if query is None:
        return result(False, "none")
    key, roots = query

    def match(stored):
        return same_project(key, roots, stored) if strict else counts(roots, stored)

    session = session_entries(data).get(session_id) if session_id else None
    if session and match(usable_key(session.get("project"))):
        return result(session["on"], "session", session.get("since"))
    entries = project_entries(data)
    hits = sorted(k for k in entries if match(k))
    if not hits:
        return result(False, "none")
    entry = entries[key] if key in entries else next(
        (entries[k] for k in hits if entries[k]["on"]), entries[hits[0]])
    return result(entry["on"], "project", entry.get("since"))


def lean_elsewhere(data: dict) -> bool:
    """True when a loaded file turns lean on for some project (older format: its top-level `on`). The resume
    script prints `off (set for another project)` when this holds and no entry counts for its cwd."""
    if is_legacy(data):
        return data["on"]
    return any(entry["on"] for entry in data.get("projects", {}).values())


# --- writing -------------------------------------------------------------------------------------------
def write_json(path: Path, data: dict) -> None:
    """Write to a temporary file in the same folder, then replace, so readers never see a partial file.
    Read-modify-write is not locked: two writers at the same moment keep the later one (the files change
    only on explicit user commands; add a lock if hooks ever start writing them)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lean_set(on: bool, cwd, *, session_id=None, root=None) -> dict:
    """Turn lean mode on or off for the project key of `cwd` (an absolute path) or, with `session_id`, for that
    session only (its own `sessions` entry; other sessions' entries are not touched). Project off removes the key's own entries and session entry (`off_removes`); entries of
    other projects inside the key (a nested repository, a non-git subfolder set on its own) stay. Returns
    the new strict lean_status.
    Raises ValueError for an unusable `cwd` and StateError when the existing file is unknown (it is left
    as it is; fix or remove it first)."""
    query = project_roots(cwd)
    if query is None:
        raise ValueError(f"not an absolute path without `..`: {cwd!r}")
    key, roots = query
    root = Path(root) if root is not None else default_root()
    data = load_lean(root) or {"on": False}
    projects = project_entries(data)  # also moves an older-format entry into `projects`
    sessions = session_entries(data)  # also moves an older single `session` slot into `sessions`
    since = now_utc()
    if session_id:
        sessions[session_id] = {"on": bool(on), "since": since, "project": key}
        latest = sorted(sessions, key=lambda sid: str(sessions[sid].get("since") or ""), reverse=True)
        sessions = {sid: sessions[sid] for sid in latest[:MAX_SESSIONS]}
    elif on:
        projects[key] = {"on": True, "since": since}
    else:
        projects = {k: v for k, v in projects.items() if not off_removes(key, roots, k)}
        sessions = {sid: entry for sid, entry in sessions.items()
                    if not off_removes(key, roots, usable_key(entry.get("project")))}
    new = {"on": bool(on), "since": since, "project": key, "projects": projects}
    if sessions:
        new["sessions"] = sessions
    write_json(root / LEAN_NAME, new)
    return lean_status(cwd, session_id, root=root, strict=True)


# --- developer mode ------------------------------------------------------------------------------------
def dev_status(*, root=None) -> dict:
    """{"state": "on"|"off"|"unknown", "since": str|None, "error": str|None}; a missing file is off."""
    root = Path(root) if root is not None else default_root()
    try:
        data = read_json(root / DEV_NAME)
        if data is None:
            return {"state": "off", "since": None, "error": None}
        check_entry(data, DEV_NAME)
    except StateError as exc:
        return {"state": "unknown", "since": None, "error": str(exc)}
    return {"state": "on" if data["on"] else "off", "since": clean_since(data.get("since")), "error": None}


def dev_mode(*, root=None) -> bool:
    """True only when developer mode is on; off and unknown both mean "do not record" (see dev_status)."""
    return dev_status(root=root)["state"] == "on"


def dev_set(on: bool, *, root=None) -> dict:
    root = Path(root) if root is not None else default_root()
    write_json(root / DEV_NAME, {"on": bool(on), "since": now_utc()})
    return dev_status(root=root)


# --- CLI -----------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="devenv_state.py", description="devenv lean and developer mode state")
    sub = parser.add_subparsers(dest="kind", required=True)
    lean = sub.add_parser("lean")
    lean.add_argument("action", choices=("on", "off", "status"))
    lean.add_argument("--session", default=None)
    lean.add_argument("--cwd", default=None)
    lean.add_argument("--root", default=None, help="state folder for tests and replay only")
    dev = sub.add_parser("dev")
    dev.add_argument("action", choices=("on", "off", "status"))
    dev.add_argument("--root", default=None, help="state folder for tests and replay only")
    args = parser.parse_args(argv)

    def emit(obj) -> None:
        print(json.dumps(obj, ensure_ascii=False, sort_keys=True))

    try:
        if args.kind == "dev":
            st = dev_status(root=args.root) if args.action == "status" else dev_set(args.action == "on",
                                                                                   root=args.root)
            emit(st)
            return EXIT_UNKNOWN if st["state"] == "unknown" else 0
        cwd = args.cwd if args.cwd is not None else os.getcwd()
        if args.action == "status":
            st = lean_status(cwd, args.session, root=args.root)
        else:
            st = lean_set(args.action == "on", cwd, session_id=args.session, root=args.root)
        emit(st)
        return EXIT_UNKNOWN if st["scope"] == "unknown" else 0
    except ValueError as exc:
        print(f"devenv_state: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except StateError as exc:
        emit({"on": None, "since": None, "scope": "unknown", "error": str(exc)})
        print(f"devenv_state: state file left unchanged: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    except OSError as exc:
        print(f"devenv_state: write failed: {exc}", file=sys.stderr)
        return EXIT_WRITE


if __name__ == "__main__":
    sys.exit(main())
