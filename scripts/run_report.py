"""Summarize the run log (runs.jsonl) in one command: what happened and what to fix.

This file owns the report format; the record format is owned by run_log.py. Read-only: it never
writes the log. Standard library only, Python 3.9+.

Usage: run_report.py [--log PATH] [--since YYYY-MM-DD|YYYY-MM-DDTHH:MM:SSZ] [--project PATH]
                     [--version V] [--json]

--log      default: the path run_log.log_path() gives (HANDOFF_LOG respected); PATH.1 is read too
--since    records at or after that time (a date means 00:00:00Z)
--project  records of that project (check records of a /resume-handoff run take the project of the
           handoff_state record with the same session_id and handoff)
--version  records whose version is V, or starts with `V-` or `V.`
--json     the same content as JSON with the fixed keys meta, runs, versions, warnings,
           check_failures, resume, handoff_size, projects, sessions, coverage

Text output has a run report line, a filters line, then 9 `## ` sections in fixed order; a section
without data prints `(none)`. Paths under the home folder print as `~/...`. A record's raw `stdout`
and `args` are never printed. Do not paste a report into a public repository.

A run that only printed the usage (handoff_state.py with no argument or `-h`/`--help`, check_handoff.py with
`-h`/`--help`) is counted as `<script>/usage` in Runs, and a check usage run as the code `usage`. Check failures
also gives the attempts to pass per version (`attempts_by_version`, a line per version, at most 10). Handoff size
counts the last direct check of each file directly under ~/.claude/handoffs/ (a resume's own check, a fixture or
scratch path stays out); its limit is 24 KiB (`over_24_kib`).

Exit codes: 0 ok (no records or no log file included) · 2 usage error, unreadable log, or an internal
error (one stderr line, never a traceback).
"""
import argparse
import importlib.util
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SESSION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")  # = run_log.SESSION_ID
AGE_RE = re.compile(r"handoff age: (\d+(?:\.\d+)?) days")
SKIP_RE = re.compile(r"newer handoff skipped \(([^)]*)\)")
HELP_ARGS = ("-h", "--help")
SKIP_REASONS = ("empty file", "project unknown", "not valid UTF-8", "no usable Exact Next Step")
CHECK_SCRIPT = "check_handoff.py"
STATE_SCRIPT = "handoff_state.py"
TOP = 10
KIB_LIMIT = 24 * 1024
SECTIONS = ("Runs", "Versions", "Warnings", "Check failures", "Resume", "Handoff size", "Projects",
            "Sessions", "Coverage")
NEW_KEYS = ("session_id", "project", "fail_codes", "caller")
# Every `WARNING:` line check_handoff.py and handoff_state.py print on stdout, as (pattern, kind). A line
# counts as a warning only when it starts with `WARNING:` or ends with one of the two mid-line forms;
# the pattern runs on the text from `WARNING:` on, digits as N. A line no pattern takes is `other`, so
# the text after a prefix (a path, a name, a branch) never becomes a kind. Add a row with a new format.
MID_LINE_WARNINGS = (re.compile(r"^handoff age: .*  (WARNING: handoff is older than \d+ days)$"),
                     re.compile(r"^handoff branch: .* -> changed  (WARNING: branch changed)$"))
WARNING_KINDS = (
    (re.compile(r"WARNING: handoff file is N KiB \(over N KiB\)$"), "handoff file is N KiB"),
    (re.compile(r"WARNING: rules file unreadable: "), "rules file unreadable"),
    (re.compile(r"WARNING: rules file section outside override points: "), "rules file section outside override points"),
    (re.compile(r"WARNING: context-budget not run \("), "context-budget not run"),
    (re.compile(r"WARNING: context-budget gave no line \(exit -?N\)$"), "context-budget gave no line"),
    (re.compile(r"WARNING: placeholder check unavailable, only empty text counts as placeholder \("),
     "placeholder check unavailable"),
    (re.compile(r"WARNING: unreadable handoff skipped: "), "unreadable handoff skipped"),
    (re.compile(r"WARNING: newer handoff skipped \("), "newer handoff skipped"),
    (re.compile(r"WARNING: N commits since the handoff$"), "N commits since the handoff"),
    (re.compile(r"WARNING: checker missing \(.*\); the handoff was not validated$"), "checker missing"),
    (re.compile(r"WARNING: checker exited -?N(?: without a \[FAIL\] line)?; the handoff was not validated$"),
     "checker exited N"),
    (re.compile(r"WARNING: ignored argument words: "), "ignored argument words"),
    (re.compile(r"WARNING: newest handoff belongs to another project: "), "newest handoff belongs to another project"),
    (re.compile(r"WARNING: handoff project differs: "), "handoff project differs"),
    (re.compile(r"WARNING: handoff is older than N days$"), "handoff is older than N days"),
    (re.compile(r"WARNING: branch changed$"), "branch changed"),
    (re.compile(r"WARNING: after-handoff messages not checked: "), "after-handoff messages not checked"),
    (re.compile(r"WARNING: N user message\(s\) after the last /handoff are not in the handoff file$"),
     "user messages after the last /handoff not in the handoff file"),
    (re.compile(r"WARNING: N user message\(s\) before the last /handoff are not in the handoff file$"),
     "user messages before the last /handoff not in the handoff file"),
    (re.compile(r"WARNING: reviews not checked: "), "reviews not checked"),
)


class ReportError(Exception):
    """A usage error or an unreadable log: exit 2."""


def load_sibling(name: "str"):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checker_module():
    """check_handoff.py next to this script: path_key and FAIL_CODES are its rules, not copies."""
    try:
        return load_sibling("check_handoff")
    except Exception as exc:
        raise ReportError(f"cannot load check_handoff.py next to this script ({type(exc).__name__})")


def checker_path_key():
    return checker_module().path_key


def default_log() -> "Path":
    try:
        found, reason = load_sibling("run_log").log_target()
    except Exception:
        return Path.home() / ".claude" / "handoffs" / ".state" / "runs.jsonl"
    if found is None:
        if os.environ.get("HANDOFF_LOG") == "off":
            raise ReportError("the run log is off (HANDOFF_LOG=off); pass --log PATH")
        if reason:
            raise ReportError(f"no run log is being written ({reason}); pass --log PATH")
        raise ReportError("developer mode is off, so no run log is being written (turn it on with "
                          "/devenv:doctor dev on); pass --log PATH to read an older log")
    return found


# ---- small value helpers ------------------------------------------------------------------------

def clean(value, limit: "int" = 60) -> "str":
    """A record string with whitespace collapsed and cut, so a odd value cannot flood the report."""
    return " ".join(str(value).split())[:limit]


def text_of(rec, key):
    value = rec.get(key)
    return value if isinstance(value, str) and value.strip() else None


def int_of(rec, key):
    value = rec.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    low = ordered[mid - 1]  # low + half the gap: a sum of two huge floats would overflow to inf
    return ordered[mid] if len(ordered) % 2 else low + (ordered[mid] - low) / 2


def tidy(number):
    """An int when whole, else one decimal."""
    return int(number) if float(number).is_integer() else round(number, 1)


def rate(part: "int", whole: "int"):
    return round(part / whole, 3) if whole else None


def percent(value) -> "str":
    return "-" if value is None else f"{value * 100:.0f}%"


def counts(items):
    out = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def by_count(mapping):
    """(key, count) pairs, highest count first, then key."""
    return sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0]))


# ---- reading ------------------------------------------------------------------------------------

class Row:
    __slots__ = ("rec", "dt", "group")

    def __init__(self, rec, dt):
        self.rec, self.dt, self.group = rec, dt, None


def read_rows(path: "Path", rows: "list", skipped: "list") -> "bool":
    """Append the records of one file. False when the file does not exist; ReportError when unreadable."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    dt = datetime.strptime(rec["ts"], TS_FORMAT)
                except (ValueError, RecursionError, KeyError, TypeError):
                    skipped.append(1)
                    continue
                rows.append(Row(rec, dt))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReportError(f"cannot read {path}: {type(exc).__name__}")
    return True


def load(log: "Path"):
    rows, skipped = [], []
    rotated = Path(str(log) + ".1")
    used = read_rows(rotated, rows, skipped)  # older records first
    read_rows(log, rows, skipped)
    rows.sort(key=lambda r: r.dt)  # stable: file order breaks ties
    return rows, len(skipped), used


# ---- record classification ----------------------------------------------------------------------

def is_check(rec) -> "bool":
    return rec.get("script") == CHECK_SCRIPT


def is_resume(rec) -> "bool":
    return rec.get("script") == STATE_SCRIPT and rec.get("mode") == "resume"


def is_usage(rec) -> "bool":
    """A run that only printed the usage: handoff_state.py with no argument or `-h`/`--help` first (never a
    resume, whose args start with the cwd), or check_handoff.py with exactly `-h` or `--help`."""
    args = rec.get("args")
    if not isinstance(args, list):
        return False
    if rec.get("script") == STATE_SCRIPT:
        return rec.get("mode") != "resume" and (not args or args[0] in HELP_ARGS)
    return is_check(rec) and len(args) == 1 and args[0] in HELP_ARGS


def caller_kind(rec) -> "str":
    if "caller" not in rec:
        return "unknown"
    return "direct" if rec["caller"] is None else clean(rec["caller"])


def is_direct_check(rec) -> "bool":
    return is_check(rec) and "caller" in rec and rec["caller"] is None


def session_of(rec):
    value = rec.get("session_id")
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else None


def make_norm(path_key):
    def norm(value):
        """path_key of a path with a leading ~ expanded; None for an empty or non-string value."""
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.startswith("~"):
            try:
                text = str(Path(text).expanduser())
            except Exception:  # unknown user or no home: compare the text as written
                pass
        return path_key(text)
    return norm


def assign_groups(rows, norm):
    """Project group key per row. A check run started by handoff_state.py takes the project of the
    handoff_state record with the same session_id and handoff; the rest use project, toplevel, cwd."""
    owner = {}
    for row in rows:
        rec = row.rec
        if rec.get("script") == STATE_SCRIPT and session_of(rec) and norm(rec.get("handoff")):
            project = norm(rec.get("project"))
            if project:
                owner[(session_of(rec), norm(rec["handoff"]))] = project
    for row in rows:
        rec, key = row.rec, None
        if is_check(rec) and rec.get("caller") == STATE_SCRIPT and session_of(rec) and norm(rec.get("handoff")):
            key = owner.get((session_of(rec), norm(rec["handoff"])))
        for field in ("project", "toplevel", "cwd"):
            if key:
                break
            key = norm(rec.get(field))
        row.group = key


def version_matches(rec, wanted: "str") -> "bool":
    version = rec.get("version")
    return isinstance(version, str) and (version == wanted or version.startswith(wanted + "-")
                                         or version.startswith(wanted + "."))


# ---- warnings -----------------------------------------------------------------------------------

def warning_kind(line: "str"):
    """The WARNING_KINDS kind of a warning line, `other` for an unknown one, None when it is no warning."""
    if not isinstance(line, str):
        return None
    if not line.startswith("WARNING:"):
        found = next((m for m in (r.match(line) for r in MID_LINE_WARNINGS) if m), None)
        if found is None:
            return None
        line = found.group(1)
    text = re.sub(r"\d+", "N", line)
    return next((kind for pattern, kind in WARNING_KINDS if pattern.match(text)), "other")


def warning_lines(rec):
    items = rec.get("warnings")
    return [w for w in items if isinstance(w, str)] if isinstance(items, list) else []


def skip_reasons(line: "str"):
    match = SKIP_RE.search(line)
    if not match:
        return []
    reasons = [r.strip() for r in match.group(1).split(", ")]
    return [r if r in SKIP_REASONS else "other" for r in reasons]


# ---- the report ---------------------------------------------------------------------------------

def runs_section(rows):
    table = {}
    for row in rows:
        key = f"{clean(row.rec.get('script'))}/{'usage' if is_usage(row.rec) else clean(row.rec.get('mode'))}"
        entry = table.setdefault(key, {"records": 0, "exit": {}})
        entry["records"] += 1
        code = int_of(row.rec, "exit")
        label = str(code) if code is not None else "unknown"
        entry["exit"][label] = entry["exit"].get(label, 0) + 1
    callers = counts(caller_kind(r.rec) for r in rows if is_check(r.rec))
    return {
        "by_script_mode": {k: table[k] for k in sorted(table)},
        "check_callers": {"direct": callers.pop("direct", 0), "handoff_state.py": callers.pop("handoff_state.py", 0),
                          "unknown": callers.pop("unknown", 0), **callers},
    }


def versions_section(rows):
    table = {}
    for row in rows:
        rec = row.rec
        name = version_name(rec)
        entry = table.setdefault(name, {"version": name, "records": 0, "check_runs": 0, "check_failed": 0,
                                        "resume_runs": 0, "resume_continue_yes": 0})
        entry["records"] += 1
        if is_check(rec):
            entry["check_runs"] += 1
            entry["check_failed"] += 1 if int_of(rec, "exit") != 0 else 0
        elif is_resume(rec):
            entry["resume_runs"] += 1
            entry["resume_continue_yes"] += 1 if continue_value(rec) == "yes" else 0
    out = []
    for name in sorted(table):
        e = table[name]
        e["check_fail_rate"] = rate(e["check_failed"], e["check_runs"])
        e["continue_yes_rate"] = rate(e["resume_continue_yes"], e["resume_runs"])
        out.append(e)
    return out


def continue_value(rec) -> "str":
    value = rec.get("continue")
    if not isinstance(value, str):
        return "none"
    return "yes" if value.startswith("yes (") or value == "yes" else clean(value)


def warnings_section(rows):
    kinds = counts(k for r in rows for k in (warning_kind(w) for w in warning_lines(r.rec)) if k)
    return [{"kind": k, "count": n} for k, n in by_count(kinds)[:TOP]]


def code_of(raw, vocabulary) -> "str":
    return raw if isinstance(raw, str) and raw in vocabulary else "other"


def version_name(rec) -> "str":
    return clean(rec.get("version")) if isinstance(rec.get("version"), str) and rec["version"].strip() else "(none)"


def attempt_stats(groups) -> "dict":
    """groups: per handoff file, the exit codes of its direct checks, oldest first."""
    groups = list(groups)
    passed = [exits.index(0) + 1 for exits in groups if 0 in exits]
    return {
        "groups": len(groups),
        "first_try_pass_rate": rate(sum(1 for n in passed if n == 1), len(groups)),
        "median": tidy(median(passed)) if passed else None,
        "max": max(passed) if passed else None,
        "never_passed": len(groups) - len(passed),
    }


def check_failures_section(rows, norm, vocabulary):
    checks = [r for r in rows if is_check(r.rec)]
    codes = {}
    for row in checks:
        fail_codes = row.rec.get("fail_codes")
        seen = {}
        for raw, number in (fail_codes.items() if isinstance(fail_codes, dict) else ()):
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                code = code_of(raw, vocabulary)
                seen[code] = seen.get(code, 0) + number
        if is_usage(row.rec):
            seen = {"usage": 1}  # the checker records --help as {"unreadable": 1}: one run, counted once
        for code, number in seen.items():
            entry = codes.setdefault(code, {"runs": 0, "messages": 0})
            entry["runs"] += 1
            entry["messages"] += number
    groups, by_version, unattributed = {}, {}, 0
    for row in checks:
        if not is_direct_check(row.rec):
            continue
        sid, handoff = session_of(row.rec), norm(row.rec.get("handoff"))
        if sid and handoff:
            groups.setdefault((sid, handoff), []).append(int_of(row.rec, "exit"))
            by_version.setdefault((version_name(row.rec), sid, handoff), []).append(int_of(row.rec, "exit"))
        else:
            unattributed += 1
    attempts = {**attempt_stats(groups.values()), "unattributed": unattributed}
    versions = {}
    for (version, _, _), exits in by_version.items():
        versions.setdefault(version, []).append(exits)
    attempts_by_version = [{"version": v, **attempt_stats(versions[v])} for v in sorted(versions)]
    return {
        "check_runs": len(checks),
        "failed_runs": sum(1 for r in checks if int_of(r.rec, "exit") != 0),
        "codes": {k: codes[k] for k, _ in sorted(codes.items(), key=lambda kv: (-kv[1]["runs"], kv[0]))},
        "attempts": attempts,
        "attempts_by_version": attempts_by_version,
    }


def resume_section(rows, all_rows, norm):
    resumes = [r for r in rows if is_resume(r.rec)]
    passes = {}
    for row in all_rows:  # lookups use every record, so a filter does not break a handoff/resume pair
        rec = row.rec
        key = norm(rec.get("handoff"))
        if is_direct_check(rec) and int_of(rec, "exit") == 0 and key:
            passes.setdefault(key, []).append(row.dt)
    hours, unknown = [], 0
    for row in resumes:
        key, found = norm(row.rec.get("handoff")), None
        if key:
            before = [dt for dt in passes.get(key, []) if dt <= row.dt]
            if before:
                found = (row.dt - before[-1]).total_seconds() / 3600
            else:
                stdout = row.rec.get("stdout")
                for line in stdout if isinstance(stdout, list) else []:
                    match = AGE_RE.match(line) if isinstance(line, str) else None
                    if match:
                        found = float(match.group(1)) * 24
                        break
        if found is None or not math.isfinite(found):  # an absurd age is unknown, never Infinity in JSON
            unknown += 1
        else:
            hours.append(found)
    reasons = counts(reason for r in rows for w in warning_lines(r.rec) for reason in skip_reasons(w))
    checker = counts(str(int_of(r.rec, "checker")) if int_of(r.rec, "checker") is not None else "none"
                     for r in resumes)
    return {
        "runs": len(resumes),
        "continue": dict(by_count(counts(continue_value(r.rec) for r in resumes))),
        "newer_skipped": dict(by_count(reasons)),
        "checker_exit": dict(sorted(checker.items())),
        "interval": {
            "n": len(hours),
            "median_hours": tidy(median(hours)) if hours else None,
            "max_hours": round(max(hours), 1) if hours else None,
            "unknown": unknown,
        },
    }


def size_section(rows, norm):
    """Sizes of the handoff files a person wrote: direct checks (no resume's own, no old record) of a file
    directly under <home>/.claude/handoffs/, the last record of each file. Fixtures and scratch paths stay out."""
    try:
        folder = norm(str(Path.home() / ".claude" / "handoffs"))
    except RuntimeError:  # no home folder: no handoff file can be told from a fixture
        folder = None
    last = {}
    for row in rows:
        n, key = int_of(row.rec, "handoff_bytes"), norm(row.rec.get("handoff"))
        if folder and key and n is not None and n >= 0 and is_direct_check(row.rec) and key.rsplit("/", 1)[0] == folder:
            last[key] = n
    sizes = sorted(last.values())
    if not sizes:
        return {"n": 0, "min": None, "median": None, "p90": None, "max": None, "over_24_kib": 0}
    p90 = sizes[-(-9 * len(sizes) // 10) - 1]  # nearest rank
    return {"n": len(sizes), "min": sizes[0], "median": tidy(median(sizes)), "p90": p90, "max": sizes[-1],
            "over_24_kib": sum(1 for n in sizes if n > KIB_LIMIT)}


def projects_section(rows):
    table = counts(r.group or "(unknown)" for r in rows)
    return [{"project": k, "runs": n} for k, n in by_count(table)[:TOP]]


def transcript(sid: "str", find):
    if not find or not SESSION_ID.fullmatch(sid):  # never glob a value that is not an id
        return None
    found = find(sid)
    return str(found) if found else None


def sessions_section(rows, find):
    """`find` is check_handoff.find_transcript, or None with an older checker (every transcript is then None)."""
    table = {}
    for row in rows:
        raw = row.rec.get("session_id")
        if not isinstance(raw, str) or not raw.strip():
            continue
        sid = raw if SESSION_ID.fullmatch(raw) else "(invalid)"  # never print a value that is not an id
        entry = table.setdefault(sid, {"session_id": sid, "project": None, "first_ts": row.rec["ts"],
                                       "last": row.dt, "runs": 0, "raw": sid})
        entry["runs"] += 1
        entry["last"] = row.dt
        entry["project"] = entry["project"] or row.group
    recent = sorted(table.values(), key=lambda e: e["last"], reverse=True)[:TOP]
    return [{"session_id": e["session_id"], "project": e["project"], "first_ts": e["first_ts"],
             "runs": e["runs"], "transcript": transcript(e["raw"], find)} for e in recent]


def coverage_section(rows):
    out = {"records": len(rows)}
    for key in NEW_KEYS:
        out[f"missing_{key}"] = sum(1 for r in rows if key not in r.rec)
    return out


def build(log, used_rotated, rows, all_rows, skipped, filters, norm, vocabulary, find):
    stamps = [r.rec["ts"] for r in rows]
    return {
        "meta": {"log": str(log), "rotated": str(log) + ".1" if used_rotated else None, "records": len(rows),
                 "skipped": skipped, "first_ts": stamps[0] if stamps else None,
                 "last_ts": stamps[-1] if stamps else None, "filters": filters},
        "runs": runs_section(rows),
        "versions": versions_section(rows),
        "warnings": warnings_section(rows),
        "check_failures": check_failures_section(rows, norm, vocabulary),
        "resume": resume_section(rows, all_rows, norm),
        "handoff_size": size_section(rows, norm),
        "projects": projects_section(rows),
        "sessions": sessions_section(rows, find),
        "coverage": coverage_section(rows),
    }


# ---- text ---------------------------------------------------------------------------------------

def pairs(mapping, sep=" "):
    return " · ".join(f"{k}{sep}{v}" for k, v in mapping.items())


def render(report) -> "list":
    meta = report["meta"]
    head = f"run report: {meta['log']}" + (f" + {meta['rotated']}" if meta["rotated"] else "")
    out = [f"{head} · records {meta['records']} · skipped {meta['skipped']} malformed · "
           f"{meta['first_ts'] or '-'} .. {meta['last_ts'] or '-'}",
           "filters: " + " ".join(f"{k}={v or '-'}" for k, v in meta["filters"].items())]
    body = {
        "Runs": runs_lines(report["runs"]),
        "Versions": [f"{v['version']}: {v['records']} records · check fail {v['check_failed']}/{v['check_runs']} "
                     f"({percent(v['check_fail_rate'])}) · resume continue=yes {v['resume_continue_yes']}/"
                     f"{v['resume_runs']} ({percent(v['continue_yes_rate'])})" for v in report["versions"]],
        "Warnings": [f"{w['count']}  {w['kind']}" for w in report["warnings"]],
        "Check failures": check_lines(report["check_failures"]),
        "Resume": resume_lines(report["resume"]),
        "Handoff size": size_lines(report["handoff_size"]),
        "Projects": [f"{p['runs']}  {p['project']}" for p in report["projects"]],
        "Sessions": [f"{s['session_id']}  {s['project'] or '-'}  {s['first_ts']}  runs {s['runs']}  "
                     f"{s['transcript'] or 'not found'}" for s in report["sessions"]],
        "Coverage": coverage_lines(report["coverage"]),
    }
    for name in SECTIONS:
        out.append(f"## {name}")
        out.extend(body[name] or ["(none)"])
    return out


def runs_lines(runs):
    lines = []
    for key, entry in runs["by_script_mode"].items():
        line = f"{key}: {entry['records']} · exit {pairs(entry['exit'], ':')}"
        if key == f"{CHECK_SCRIPT}/check":
            line += f" · caller {pairs(runs['check_callers'])}"
        lines.append(line)
    return lines


def check_lines(failures):
    attempts = failures["attempts"]
    if not failures["check_runs"]:
        return []
    lines = [f"check runs {failures['check_runs']} · failed {failures['failed_runs']}"]
    lines += [f"{code}: runs {e['runs']} · messages {e['messages']}" for code, e in failures["codes"].items()]
    lines.append(f"attempts to pass (direct checks): groups {attempts['groups']} · first-try pass "
                 f"{percent(attempts['first_try_pass_rate'])} · median {attempts['median'] if attempts['median'] is not None else '-'}"
                 f" · max {attempts['max'] if attempts['max'] is not None else '-'}"
                 f" · never passed {attempts['never_passed']} · unattributed {attempts['unattributed']}")
    for entry in failures["attempts_by_version"][:TOP]:
        lines.append(f"attempts {entry['version']}: groups {entry['groups']} · first-try pass "
                     f"{percent(entry['first_try_pass_rate'])} · median {entry['median'] if entry['median'] is not None else '-'}"
                     f" · max {entry['max'] if entry['max'] is not None else '-'} · never passed {entry['never_passed']}")
    return lines


def resume_lines(resume):
    if not resume["runs"]:
        return []
    interval = resume["interval"]
    fmt = lambda v: "-" if v is None else f"{v}h"  # noqa: E731
    return [
        f"resume runs {resume['runs']}",
        f"continue: {pairs(resume['continue']) or '-'}",
        f"newer handoff skipped: {pairs(resume['newer_skipped']) or '-'}",
        f"checker exit: {pairs(resume['checker_exit'], ':') or '-'}",
        f"handoff to resume: n {interval['n']} · median {fmt(interval['median_hours'])} · "
        f"max {fmt(interval['max_hours'])} · unknown {interval['unknown']}",
    ]


def size_lines(size):
    if not size["n"]:
        return []
    return [f"n {size['n']} · min {size['min']} · median {size['median']} · p90 {size['p90']} · "
            f"max {size['max']} · over 24 KiB {size['over_24_kib']}"]


def coverage_lines(cov):
    if not cov["records"]:
        return []
    missing = " · ".join(f"{k} {cov['missing_' + k]}" for k in NEW_KEYS)
    return [f"records {cov['records']} · keys absent (old records): {missing}"]


def home_forms(path_key):
    try:
        home = str(Path.home())
    except Exception:
        return []
    forms = {home, home.replace("\\", "/"), path_key(home)}
    return sorted((f for f in forms if len(f) > 1 and path_key(f) not in ("", "/")), key=len, reverse=True)


def tildify(text: "str", path_key) -> "str":
    """The home folder prefix as `~`, only at path boundaries (`/home/ab` does not touch `/home/abc`)."""
    for form in home_forms(path_key):
        text = re.sub(r"(?<![A-Za-z0-9_.~/\\-])" + re.escape(form) + r"(?![A-Za-z0-9_.-])", "~", text)
    return text


# ---- entry --------------------------------------------------------------------------------------

def parse_since(value: "str"):
    try:
        if DATE_RE.fullmatch(value):
            return datetime.strptime(value, "%Y-%m-%d")
        if TS_RE.fullmatch(value):
            return datetime.strptime(value, TS_FORMAT)
    except ValueError:
        pass
    raise ReportError(f"--since must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, got {clean(value)!r}")


def expand(value: "str", option: "str") -> "str":
    try:
        return str(Path(value).expanduser())
    except RuntimeError:  # an unknown ~user, or no home folder
        raise ReportError(f"{option}: cannot resolve {clean(value)!r}")


def report_for(ns) -> "tuple":
    for option in ("since", "project", "version", "log"):
        if getattr(ns, option) is not None and not getattr(ns, option).strip():
            raise ReportError(f"--{option} needs a value")
    since = parse_since(ns.since) if ns.since is not None else None
    checker = checker_module()
    path_key = checker.path_key
    norm = make_norm(path_key)
    log = Path(expand(ns.log, "--log")) if ns.log is not None else default_log()
    all_rows, skipped, used_rotated = load(log)
    assign_groups(all_rows, norm)
    wanted = None
    if ns.project is not None:
        project = expand(ns.project.strip(), "--project")
        wanted = norm(project if checker.is_absolute_path(project) else str(Path(project).resolve()))
    rows = [r for r in all_rows
            if (ns.version is None or version_matches(r.rec, ns.version))
            and (since is None or r.dt >= since)
            and (wanted is None or r.group == wanted)]
    filters = {"since": ns.since, "project": ns.project, "version": ns.version}
    find = getattr(checker, "find_transcript", None)
    return build(log, used_rotated, rows, all_rows, skipped, filters, norm, checker.FAIL_CODES, find), path_key


def main(argv: "list") -> "int":
    parser = argparse.ArgumentParser(prog="run_report.py", description="Summarize the handoff run log.")
    parser.add_argument("--log")
    parser.add_argument("--since")
    parser.add_argument("--project")
    parser.add_argument("--version")
    parser.add_argument("--json", action="store_true")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed its usage message on stderr
        return exc.code if isinstance(exc.code, int) else 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        report, path_key = report_for(ns)
        if ns.json:
            out = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        else:
            out = tildify("\n".join(render(report)), path_key)
        print(out)
    except ReportError as exc:
        sys.stderr.write(f"run_report.py: error: {exc}\n")
        return 2
    except Exception as exc:  # the only exit codes are 0 and 2; no traceback, no record text
        sys.stderr.write(f"run_report.py: error: internal error ({type(exc).__name__})\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
