#!/usr/bin/env python3
"""PostToolUse Bash hook: replace a large output with its head and the path of the full output.

Registration (matcher `Bash`): hook_bash.py --out-dir <plugin data>/bash-out
stdin is the PostToolUse input. stdout is empty (output unchanged) or one JSON object.

It acts only on the main thread (no `agent_id` in the input) of a session or project with lean mode on
(devenv_state.lean_status, strict, for the input `cwd` and `session_id`). It leaves the output alone when
`tool_response` lacks any of the four required keys (`stdout`, `stderr` strings, `interrupted`, `isImage`
booleans), when the output is an image, when the run was interrupted, and when stdout and stderr are both
empty. Otherwise, when stdout plus stderr is over `bash_bytes` bytes or `bash_lines` lines (rules_layer
limits; a bad value falls back to the default), it:
  1. writes the full output (stdout, then `[devenv] stderr:` and stderr when stderr is not empty) to
     `<out-dir>/<UTC>-<8 hex>.txt`, readable by the owner only, and keeps the newest 50 such files;
  2. prints `updatedToolOutput`: the original `tool_response` with every key kept, `stdout` replaced by its
     first `bash_head_lines` lines plus a size line with the file path and a line on narrowing the
     command, and `stderr` replaced by "" when it was empty, else its first 10 lines.
Each head is also cut to a byte budget (stdout half of `bash_bytes`, stderr a quarter), so one very long
line cannot keep the replacement large. The replacement goes through output_shape_problems() before it is
printed; the harness ignores a replacement of the wrong shape and logs that only to its debug file.

Nothing is replaced, and a `systemMessage` warning is printed, when the lean state file cannot be read
(the warning appears only for an output over the limits), when the full output cannot be saved, or when the
replacement fails the shape check. The output is never lost. Each decision over the limits is recorded with
events.record("bash_replace", replaced=1|0, ...) (developer mode only).

`--root DIR` (tests and doctor replay only) replaces the lean and developer mode state folder and the
events.jsonl folder. No environment variable changes either path.
Exit: 0 decided (output replaced or not), 1 stdin is not a JSON object or an internal error (shown as a
non-blocking hook error; the output stays unchanged), 2 usage error.
"""
import argparse
import importlib.util
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_KEYS = {"stdout": str, "stderr": str, "interrupted": bool, "isImage": bool}
TEXT_KEYS = ("stdout", "stderr")
KEEP_FILES = 50
STDERR_HEAD_LINES = 10
SAVED_NAME = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}\.txt$")
ENCODING = ("utf-8", "surrogatepass")  # lone surrogates from JSON escapes survive the round trip


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(f"devenv_{name}", Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- pure helpers --------------------------------------------------------------------------------------
def output_shape_problems(updated, original=None) -> list:
    """Problems that make the harness ignore `updated` as a Bash output (empty list = good shape).
    The four required keys must exist with their types; with `original`, every key of it must remain and
    any key other than stdout and stderr must keep its value."""
    if not isinstance(updated, dict):
        return [f"not an object: {type(updated).__name__}"]
    problems = []
    for key, kind in REQUIRED_KEYS.items():
        if key not in updated:
            problems.append(f"missing key: {key}")
        elif type(updated[key]) is not kind:
            problems.append(f"{key} is {type(updated[key]).__name__}, not {kind.__name__}")
    if isinstance(original, dict):
        for key, value in original.items():
            if key not in updated:
                problems.append(f"original key dropped: {key}")
            elif key not in TEXT_KEYS and updated[key] != value:
                problems.append(f"original value changed: {key}")
    return problems


def size_of(text: str) -> tuple:
    """(lines, bytes); a last line without a newline counts as a line."""
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return lines, len(text.encode(*ENCODING))


def head(text: str, max_lines: int, max_bytes: int) -> str:
    """The first `max_lines` lines of `text`, cut to `max_bytes` bytes at a character boundary."""
    out = "".join(text.splitlines(keepends=True)[:max_lines])
    data = out.encode(*ENCODING)
    if len(data) > max_bytes:
        out = data[:max_bytes].decode("utf-8", "ignore")
    return out


def build_replacement(response: dict, saved_path: str, head_lines: int, limit_bytes: int, lines: int,
                      size: int) -> dict:
    """`response` with every key kept and stdout and stderr replaced (see the module docstring)."""
    top = head(response["stdout"], head_lines, max(1, limit_bytes // 2))
    if top and not top.endswith("\n"):
        top += "\n"
    stdout = (f"{top}[devenv] output {lines} lines/{size} bytes replaced; full output: {saved_path} "
              f"(read it with offset/limit)\n"
              "[devenv] to see less, run the command again with its output narrowed "
              "(for example `| head -n 50`, `| tail -n 50` or `| grep <pattern>`)")
    stderr = response["stderr"]
    new = dict(response)
    new["stdout"] = stdout
    new["stderr"] = head(stderr, STDERR_HEAD_LINES, max(1, limit_bytes // 4)) if stderr else ""
    return new


# --- files ---------------------------------------------------------------------------------------------
def save_full(out_dir: Path, stdout: str, stderr: str) -> Path:
    """Write the full output to a new owner-only file in `out_dir`. Raises OSError."""
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"{stamp}-{secrets.token_hex(4)}.txt"
    body = stdout + (("" if not stdout or stdout.endswith("\n") else "\n") + "[devenv] stderr:\n" + stderr
                     if stderr else "")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body.encode(*ENCODING))
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def prune(out_dir: Path, keep: Path) -> None:
    """Keep the newest KEEP_FILES saved outputs (by name, which starts with the UTC time); only files named
    like a saved output are removed, never `keep`. A file that cannot be removed is left for the next run."""
    names = sorted(p.name for p in out_dir.iterdir() if SAVED_NAME.match(p.name) and p.is_file())
    for name in names[:max(0, len(names) - KEEP_FILES)]:
        if name != keep.name:
            try:
                (out_dir / name).unlink()
            except OSError:
                pass


def rule_paths(rules_layer, cwd) -> list:
    """[user layer file, project layer file] for `cwd` (None where there is none)."""
    user, _ = rules_layer.user_file()
    project = None
    if isinstance(cwd, str) and cwd and Path(cwd).is_dir():
        code, out = rules_layer.git(Path(cwd), "rev-parse", "--path-format=absolute", "--show-toplevel",
                                    "--git-dir", "--git-common-dir")
        parts = out.splitlines()
        if code == 0 and len(parts) == 3:
            top, git_dir, common = (Path(p) for p in parts)
            main_tree = common.parent if git_dir != common else None
            project, _ = rules_layer.project_file(top, main_tree)
    return [user, project]


# --- hook ----------------------------------------------------------------------------------------------
def decide(data: dict, out_dir: Path, root) -> dict | None:
    """The hook's JSON answer for one PostToolUse input, or None to leave the output unchanged."""
    if data.get("tool_name") != "Bash" or "agent_id" in data:
        return None
    response = data.get("tool_response")
    if output_shape_problems(response):
        return None
    if response["isImage"] or response["interrupted"] or not (response["stdout"] or response["stderr"]):
        return None
    status = load_sibling("devenv_state").lean_status(data.get("cwd"), data.get("session_id"), root=root,
                                                        strict=True)
    unknown = status.get("scope") == "unknown"
    if not unknown and status.get("on") is not True:
        return None
    rules_layer = load_sibling("rules_layer")
    limits, _ = rules_layer.limits(rule_paths(rules_layer, data.get("cwd")))
    out_lines, out_bytes = size_of(response["stdout"])
    err_lines, err_bytes = size_of(response["stderr"])
    lines, size = out_lines + err_lines, out_bytes + err_bytes
    if lines <= limits["bash_lines"] and size <= limits["bash_bytes"]:
        return None

    def record(**flags):
        load_sibling("events").record("bash_replace", lean_on=status.get("on"), root=root, lines=lines,
                                      bytes=size, **flags)

    if unknown:
        record(replaced=0, state_unknown=1)
        return {"systemMessage": f"[devenv] lean state unknown ({status.get('error')}); Bash output of "
                                 f"{lines} lines/{size} bytes left unchanged"}
    try:
        saved = save_full(out_dir, response["stdout"], response["stderr"])
    except OSError as exc:
        record(replaced=0, save_failed=1)
        return {"systemMessage": f"[devenv] full output could not be saved ({exc}); Bash output of "
                                 f"{lines} lines/{size} bytes left unchanged"}
    try:
        prune(out_dir, saved)
    except OSError:
        pass  # the new file is saved; old ones are removed on a later run
    new = build_replacement(response, str(saved), limits["bash_head_lines"], limits["bash_bytes"], lines,
                            size)
    problems = output_shape_problems(new, response)
    if problems:
        record(replaced=0, shape_failed=1)
        return {"systemMessage": f"[devenv] replacement failed the shape check ({'; '.join(problems)}); "
                                 f"Bash output left unchanged, full output: {saved}"}
    record(replaced=1)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hook_bash.py", description="devenv PostToolUse Bash output hook")
    parser.add_argument("--out-dir", required=True, help="folder for full outputs (<plugin data>/bash-out)")
    parser.add_argument("--root", default=None, help="state and events folder for tests and replay only")
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        print(f"hook_bash: stdin is not JSON ({exc}); output left unchanged", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("hook_bash: stdin is not a JSON object; output left unchanged", file=sys.stderr)
        return 1
    answer = decide(data, Path(args.out_dir), args.root)
    if answer is not None:
        sys.stdout.write(json.dumps(answer) + "\n")  # ASCII escapes: a lone surrogate cannot break the write
    return 0


if __name__ == "__main__":
    sys.exit(main())
