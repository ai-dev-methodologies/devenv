"""Tests for scripts/run_report.py (run log analyzer, plan runlog-plan-v3 section 3.4, tests R8).

Every test points HOME, USERPROFILE and the log at a temporary folder so the real ~/.claude
is never read or written. Records are built here with all 19 keys (old ones with the 14 old keys).
"""
import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_report.py"
sys.path.insert(0, str(ROOT / "scripts"))

import check_handoff  # noqa: E402
import run_report  # noqa: E402

SECTIONS = ["## Runs", "## Versions", "## Warnings", "## Check failures", "## Resume",
            "## Handoff size", "## Projects", "## Sessions", "## Coverage"]
JSON_KEYS = ["meta", "runs", "versions", "warnings", "check_failures", "resume", "handoff_size",
             "projects", "sessions", "coverage"]
NEW_KEYS = ("session_id", "caller", "project", "fail_codes", "handoff_bytes")
HEADER_RE = re.compile(r"records (\d+) · skipped (\d+) malformed")
AFTER_KIND = "user messages after the last /handoff not in the handoff file"
NOT_CHECKED_KIND = "after-handoff messages not checked"
BEFORE_KIND = "user messages before the last /handoff not in the handoff file"
REVIEWS_KIND = "reviews not checked"
S1, S2, S3 = (f"{n}{n}{n}{n}{n}{n}{n}{n}-aaaa-4bbb-8ccc-{n * 12}" for n in "123")  # UUID-shaped session ids


def rec(**over):
    """A 19-key record (a check run by default)."""
    base = {
        "ts": "2026-11-09T10:00:00Z", "version": "2026.11.09-2", "script": "check_handoff.py",
        "mode": "check", "cwd": "/work/p", "toplevel": None, "args": [], "exit": 0, "stdout": [],
        "omitted_lines": 0, "warnings": [], "handoff": None, "continue": None, "checker": None,
        "session_id": None, "caller": None, "project": None, "fail_codes": None,
        "handoff_bytes": None,
    }
    base.update(over)
    return base


def old(**over):
    """The same record without the 5 keys added by the run-log signals work."""
    return {k: v for k, v in rec(**over).items() if k not in NEW_KEYS}


def resume(**over):
    over.setdefault("script", "handoff_state.py")
    over.setdefault("mode", "resume")
    return rec(**over)


class ReportCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HANDOFF_LOG", "CLAUDE_CODE_SESSION_ID", "HANDOFF_CALLER"):
            os.environ.pop(name, None)
        self.log = self.tmp / "runs.jsonl"

    def write(self, records, path=None):
        lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
        self._write(path or self.log, "\n".join(lines) + "\n")

    @staticmethod
    def _write(path, text):
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def report(self, *args, log=True):
        argv = (["--log", str(self.log)] if log else []) + list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_report.main(argv)
        return code, out.getvalue(), err.getvalue()

    def data(self, *args):
        code, out, err = self.report("--json", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def text(self, *args):
        code, out, err = self.report(*args)
        self.assertEqual(code, 0, err)
        return out


def section(out, name):
    """Lines of one `## name` section."""
    lines = out.splitlines()
    start = lines.index(f"## {name}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


class EmptyAndErrorTest(ReportCase):
    def heads(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("run report:") or ln.startswith("## ")]

    def test_R8_empty_or_missing_log_prints_all_sections(self):
        self._write(self.log, "")
        missing = self.tmp / "nothing.jsonl"
        for label, argv in (("empty", ["--log", str(self.log)]), ("missing", ["--log", str(missing)])):
            with self.subTest(case=label):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = run_report.main(argv)
                self.assertEqual(code, 0, err.getvalue())
                heads = self.heads(out.getvalue())
                self.assertEqual(len(heads), 10)
                self.assertTrue(heads[0].startswith("run report:"))
                self.assertEqual(heads[1:], SECTIONS)
                self.assertIn("records 0 · skipped 0 malformed", heads[0])
                self.assertEqual(out.getvalue().splitlines()[1], "filters: since=- project=- version=-")
                for name in SECTIONS:
                    self.assertEqual(section(out.getvalue(), name[3:]), ["(none)"], name)

    def test_R8_unreadable_log_exit2(self):
        code, out, err = self.report("--log", str(self.tmp), log=False)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_R8_bad_since_exit2(self):
        self.write([rec()])
        for bad in ("2026-13-40", "2026-11-09T10:00:00+09:00", "yesterday", "2026-11-09 10:00:00"):
            with self.subTest(since=bad):
                code, out, err = self.report("--since", bad)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")

    def test_R8_empty_option_value_exit2(self):
        self.write([rec()])
        for option in ("--since", "--version", "--project", "--log"):
            with self.subTest(option=option):
                code, out, err = self.report(option, "", log=option != "--log")
                self.assertEqual(code, 2, out)
                self.assertEqual(out, "")
                self.assertEqual(len(err.splitlines()), 1, err)

    def test_R8_unresolvable_home_in_log_exit2(self):
        code, out, err = self.report("--log", "~nosuchuser_zz/x", log=False)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertNotIn("Traceback", err)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--log", "~nosuchuser_zz/x"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_R8_unexpected_error_exit2_one_line(self):
        self.write([rec()])
        for name in ("build", "render"):
            with self.subTest(fails=name), mock.patch.object(run_report, name, side_effect=ValueError("boom")):
                code, out, err = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertNotIn("Traceback", err)

    def test_R8_unknown_option_exit2(self):
        code, _, _ = self.report("--no-such-option")
        self.assertEqual(code, 2)

    def test_R8_log_off_without_path_exit2(self):
        os.environ["HANDOFF_LOG"] = "off"
        code, out, err = self.report(log=False)
        self.assertEqual(code, 2)
        self.assertIn("off", err)

    def test_R8_default_log_follows_env_and_home(self):
        self.write([rec(), rec()])
        os.environ["HANDOFF_LOG"] = str(self.log)
        code, out, _ = self.report("--json", log=False)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["meta"]["records"], 2)
        del os.environ["HANDOFF_LOG"]
        default = self.home / ".claude" / "handoffs" / ".state" / "runs.jsonl"
        default.parent.mkdir(parents=True)
        self._write(default, json.dumps(rec()) + "\n")
        (self.home / ".claude" / "state").mkdir(parents=True)  # the default path is used only in developer mode
        self._write(self.home / ".claude" / "state" / "devenv-dev.json", '{"on": true}\n')
        code, out, _ = self.report("--json", log=False)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["meta"]["records"], 1)


class ReadingTest(ReportCase):
    def test_R8_malformed_lines_counted(self):
        self.write([rec(), "this is not json", rec(ts="yesterday")])
        out = self.text()
        self.assertIn("records 1 · skipped 2 malformed", out.splitlines()[0])

    def test_R8_non_object_and_missing_ts_are_malformed(self):
        no_ts = rec()
        del no_ts["ts"]
        self.write([rec(), "[1, 2]", '"text"', json.dumps(no_ts), "", rec(ts=5)])
        meta = self.data()["meta"]
        self.assertEqual((meta["records"], meta["skipped"]), (1, 4))

    def test_R8_reads_rotated_file(self):
        self.write([rec(ts="2026-11-09T10:00:00Z")])
        self.write([rec(ts="2026-11-01T10:00:00Z"), rec(ts="2026-11-02T10:00:00Z")],
                   path=Path(str(self.log) + ".1"))
        out = self.text()
        head = out.splitlines()[0]
        self.assertIn("records 3", head)
        self.assertIn(f"+ {str(self.log) + '.1'}".replace(str(self.home), "~"), head)
        self.assertIn("2026-11-01T10:00:00Z .. 2026-11-09T10:00:00Z", head)

    def test_R8_does_not_append_to_log(self):
        self.write([rec(), rec()])
        before = self.log.read_bytes()
        self.text()
        self.data()
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(len(before.splitlines()), 2)

    def test_R8_old_records_tolerated(self):
        self.write([
            old(ts="2026-11-08T10:00:00Z", exit=1, handoff="/h/a.md"),
            old(ts="2026-11-08T10:01:00Z", exit=0, handoff="/h/a.md"),
            old(ts="2026-11-08T10:02:00Z", script="handoff_state.py", mode="resume", **{"continue": "yes (x)"}),
            rec(ts="2026-11-09T10:00:00Z"),
        ])
        data = self.data()
        self.assertEqual(data["meta"]["records"], 4)
        self.assertEqual(data["coverage"]["missing_session_id"], 3)
        self.assertEqual(data["coverage"]["missing_caller"], 3)
        self.assertEqual(data["coverage"]["missing_fail_codes"], 3)
        self.assertEqual(data["coverage"]["missing_project"], 3)
        self.assertEqual(data["check_failures"]["attempts"]["groups"], 0)
        self.assertEqual(data["runs"]["check_callers"]["unknown"], 2)
        self.assertEqual(data["resume"]["continue"], {"yes": 1})
        text = "\n".join(section(self.text(), "Coverage"))
        self.assertIn("session_id 3", text)
        self.assertIn("caller 3", text)


class FilterTest(ReportCase):
    def count(self, *args):
        return self.data(*args)["meta"]["records"]

    def test_R8_filters(self):
        self.write([
            rec(ts="2026-11-08T23:59:59Z", version="2026.11.09-1", project="/a/p1"),
            rec(ts="2026-11-09T00:00:00Z", version="2026.11.09-10", project="/b/p2"),
            rec(ts="2026-11-09T12:00:00Z", version="2026.11.09-2", project="/a/p1"),
        ])
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count("--since", "2026-11-09"), 2)
        self.assertEqual(self.count("--since", "2026-11-09T12:00:00Z"), 1)
        self.assertEqual(self.count("--project", "/a/p1/"), 2)
        self.assertEqual(self.count("--project", "/nowhere"), 0)
        self.assertEqual(self.count("--version", "2026.11.09-1"), 1)
        self.assertEqual(self.count("--version", "2026.11.09-10"), 1)
        self.assertEqual(self.count("--version", "2026.11.09"), 3)
        self.assertEqual(self.count("--version", "2026.11"), 3)  # the `V.` prefix
        self.assertEqual(self.count("--since", "2026-11-09", "--version", "2026.11.09-2",
                                    "--project", "/a/p1"), 1)

    def test_R8_relative_project_is_made_absolute(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        self.write([rec(project=str(proj)), rec(project="/elsewhere")])
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(proj)
        self.assertEqual(self.count("--project", "."), 1)
        os.chdir(self.tmp)
        self.assertEqual(self.count("--project", "proj"), 1)

    def test_R8_filters_line_echoes_values(self):
        self.write([rec()])
        out = self.text("--since", "2026-11-01", "--version", "2026.11.09")
        self.assertEqual(out.splitlines()[1], "filters: since=2026-11-01 project=- version=2026.11.09")

    def test_R8_filter_keeps_malformed_count(self):
        self.write([rec(), "garbage"])
        self.assertEqual(self.data("--version", "none")["meta"]["skipped"], 1)


class WarningsTest(ReportCase):
    def kinds(self):
        return {w["kind"]: w["count"] for w in self.data()["warnings"]}

    def test_R8_warning_kinds_normalized(self):
        self.write([
            rec(warnings=["WARNING: handoff file is 17 KiB (over 24 KiB)"]),
            rec(warnings=["WARNING: handoff file is 18 KiB (over 24 KiB)"]),
            resume(warnings=["WARNING: 3 commits since the handoff"]),
            resume(warnings=["WARNING: 12 commits since the handoff"]),
            resume(warnings=["handoff age: 9.0 days  WARNING: handoff is older than 7 days"]),
            resume(warnings=["WARNING: ignored argument words: foo bar"]),
            resume(warnings=["WARNING: handoff is older than 9 days"]),
        ])
        self.assertEqual(self.kinds(), {
            "handoff file is N KiB": 2,
            "N commits since the handoff": 2,
            "handoff is older than N days": 2,
            "ignored argument words": 1,
        })

    def test_R8_warning_kinds_are_a_closed_vocabulary(self):
        sentinel = "body" + "sentinel" + "-5150"
        self.write([resume(warnings=[
            f"x WARNING: {sentinel}(b WARNING: c",  # mid-line, not a known form: not a warning
            f"live subagent: notes WARNING: {sentinel} (ab12) -> lost (no transcript)",
            "WARNING: " + "w" * 90,  # starts with WARNING: but is no format the scripts print
            f"WARNING: {sentinel}",
            f"handoff branch: WARNING:{sentinel} -> changed  WARNING: branch changed",
            f"handoff age: 9.0 days  WARNING: handoff is older than 7 days {sentinel}",  # not at the line end
            "WARNING: 3 user message(s) after the last /handoff are not in the handoff file",
            f"WARNING: 3 user message(s) after the last /handoff are not in the handoff file {sentinel}",
            f"WARNING: after-handoff messages not checked: {sentinel}",
        ])])
        self.assertEqual(self.kinds(), {"other": 3, "branch changed": 1, AFTER_KIND: 1, NOT_CHECKED_KIND: 1})
        self.assertNotIn(sentinel, self.text())

    def test_R8_every_printed_warning_has_a_kind(self):
        lines = {  # one line per WARNING: format of check_handoff.py and handoff_state.py stdout
            "WARNING: handoff file is 17 KiB (over 24 KiB)": "handoff file is N KiB",
            "WARNING: rules file unreadable: /h/r.md": "rules file unreadable",
            "WARNING: rules file section outside override points: X": "rules file section outside override points",
            "WARNING: rules file section outside override points: … and 2 more": "rules file section outside override points",
            "WARNING: context-budget not run (OSError)": "context-budget not run",
            "WARNING: context-budget gave no line (exit 3)": "context-budget gave no line",
            "WARNING: placeholder check unavailable, only empty text counts as placeholder (x)": "placeholder check unavailable",
            "WARNING: unreadable handoff skipped: a.md": "unreadable handoff skipped",
            "WARNING: unreadable handoff skipped: … and 4 more": "unreadable handoff skipped",
            "WARNING: newer handoff skipped (empty file): a.md": "newer handoff skipped",
            "WARNING: 12 commits since the handoff": "N commits since the handoff",
            "WARNING: checker missing (/x/check_handoff.py); the handoff was not validated": "checker missing",
            "WARNING: checker exited 1 without a [FAIL] line; the handoff was not validated": "checker exited N",
            "WARNING: checker exited -11; the handoff was not validated": "checker exited N",
            "WARNING: ignored argument words: foo": "ignored argument words",
            "WARNING: newest handoff belongs to another project: /p": "newest handoff belongs to another project",
            "WARNING: handoff project differs: /p": "handoff project differs",
            "handoff age: 9.0 days  WARNING: handoff is older than 7 days": "handoff is older than N days",
            "handoff branch: main -> changed  WARNING: branch changed": "branch changed",
            "WARNING: 2 user message(s) after the last /handoff are not in the handoff file": AFTER_KIND,
            "WARNING: after-handoff messages not checked: transcript not found": NOT_CHECKED_KIND,
            "WARNING: after-handoff messages not checked: checker unavailable": NOT_CHECKED_KIND,
            "WARNING: 3 user message(s) before the last /handoff are not in the handoff file": BEFORE_KIND,
            "WARNING: reviews not checked: transcript not found": REVIEWS_KIND,
        }
        for line, kind in lines.items():
            with self.subTest(line=line):
                self.assertEqual(run_report.warning_kind(line), kind)
        for tail in ("handoff branch: main -> changed  WARNING: branch changed extra",
                     "handoff age: 9.0 days  WARNING: handoff is older than 7 days extra"):
            with self.subTest(trailing=tail):
                self.assertIsNone(run_report.warning_kind(tail))  # the mid-line forms end the line

    def test_R8_warnings_top_10(self):
        kinds = ["WARNING: handoff file is 17 KiB (over 24 KiB)", "WARNING: rules file unreadable: x",
                 "WARNING: context-budget not run (E)", "WARNING: context-budget gave no line (exit 1)",
                 "WARNING: unreadable handoff skipped: a", "WARNING: newer handoff skipped (empty file): a",
                 "WARNING: 12 commits since the handoff", "WARNING: checker missing (x); y",
                 "WARNING: ignored argument words: a", "WARNING: newest handoff belongs to another project: p",
                 "WARNING: handoff project differs: p", "handoff branch: a -> changed  WARNING: branch changed"]
        self.write([rec(warnings=[line] * (i + 1)) for i, line in enumerate(kinds)])
        listed = self.data()["warnings"]
        self.assertEqual(len(listed), 10)
        self.assertEqual(listed[0], {"kind": "branch changed", "count": 12})
        self.assertEqual(len(section(self.text(), "Warnings")), 10)

    def test_R8_warning_kinds_after_handoff(self):
        self.write([
            resume(warnings=["WARNING: 2 user message(s) after the last /handoff are not in the handoff file"]),
            resume(warnings=["WARNING: 7 user message(s) after the last /handoff are not in the handoff file"]),
            resume(warnings=["WARNING: after-handoff messages not checked: transcript not found"]),
            rec(warnings=["WARNING: after-handoff messages not checked: no /handoff input in transcript"]),
            resume(warnings=["WARNING: after-handoff messages not checked: checker unavailable"]),
        ])
        self.assertEqual(self.kinds(), {AFTER_KIND: 2, NOT_CHECKED_KIND: 3})

    def test_R8_usage_and_help_runs_add_no_warnings(self):
        env = {**os.environ, "HANDOFF_LOG": str(self.log)}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("HANDOFF_CALLER", None)
        for argv in ([str(ROOT / "scripts" / "check_handoff.py")],
                     [str(ROOT / "scripts" / "handoff_state.py"), "--help"]):
            subprocess.run([sys.executable, *argv], capture_output=True, env=env)
        recs = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(recs), 2)
        self.assertTrue(any(r["warnings"] for r in recs), "the docstrings quote WARNING: formats")
        self.assertEqual(self.data()["warnings"], [])

    def test_R8_newer_skipped_by_reason(self):
        self.write([
            resume(warnings=["WARNING: newer handoff skipped (empty file): a.md"]),
            resume(warnings=["WARNING: newer handoff skipped (project unknown): b.md"]),
            resume(warnings=["WARNING: newer handoff skipped (not valid UTF-8): c.md"]),
            resume(warnings=["WARNING: newer handoff skipped (no usable Exact Next Step): d.md"]),
            resume(warnings=["WARNING: newer handoff skipped (empty file, project unknown): … and 3 more"]),
            resume(warnings=["WARNING: newer handoff skipped (something else): e.md"]),
        ])
        self.assertEqual(self.data()["resume"]["newer_skipped"], {
            "empty file": 2, "project unknown": 2, "not valid UTF-8": 1,
            "no usable Exact Next Step": 1, "other": 1,
        })
        self.assertEqual(self.kinds(), {"newer handoff skipped": 6})

    def test_R8_continue_counts(self):
        self.write([
            resume(**{"continue": "yes (abc)"}), resume(**{"continue": "yes (def)"}),
            resume(**{"continue": "no"}), resume(**{"continue": "no (project mismatch)"}),
            resume(**{"continue": "no (newer handoff skipped)"}),
        ])
        self.assertEqual(self.data()["resume"]["continue"], {
            "yes": 2, "no": 1, "no (project mismatch)": 1, "no (newer handoff skipped)": 1})
        text = "\n".join(section(self.text(), "Resume"))
        self.assertIn("yes 2", text)

    def test_R8_counts_after_handoff_gate(self):
        self.write([
            rec(exit=1, fail_codes={"after-handoff": 2, "contract-session": 1}),
            rec(exit=1, fail_codes={"after-handoff": 1}),
            resume(**{"continue": "no (unrecorded messages after handoff)"}),
            resume(**{"continue": "no (unrecorded messages after handoff)"}),
            resume(**{"continue": "yes (--c)"}),
        ])
        data = self.data()
        codes = data["check_failures"]["codes"]
        self.assertEqual(codes["after-handoff"], {"runs": 2, "messages": 3})
        self.assertEqual(codes["contract-session"], {"runs": 1, "messages": 1})
        self.assertNotIn("other", codes)
        self.assertEqual(data["resume"]["continue"], {"no (unrecorded messages after handoff)": 2, "yes": 1})
        text = self.text()
        self.assertIn("after-handoff", "\n".join(section(text, "Check failures")))
        self.assertIn("no (unrecorded messages after handoff) 2", "\n".join(section(text, "Resume")))

    def test_R8_resume_checker_exit_counts(self):
        self.write([resume(checker=0), resume(checker=0), resume(checker=1), resume(checker=None)])
        self.assertEqual(self.data()["resume"]["checker_exit"], {"0": 2, "1": 1, "none": 1})


class WarningBodyTest(ReportCase):
    """H1: the unindented handoff_state lines that quote the handoff (`live subagent:`, `handoff branch:`)."""

    def test_R8_body_text_in_warnings_never_reaches_the_report(self):
        name_sentinel, branch_sentinel = "body" + "sentinel", "branch" + "body" + "sentinel"
        repo = self.tmp / "proj"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
               "-C", str(repo)]
        subprocess.run(git + ["init", "-q", "-b", "main"], check=True, capture_output=True)
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(git + ["add", "-A"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True, capture_output=True)
        item = "\n".join([f"  - name: notes WARNING: {name_sentinel} leaked (ab12)", "    does: reviews the diff",
                          "    decision: Use the sample approach", "    result: /home/u/out/review.md",
                          "    done-when: the transcript ends with end_turn", "    after-return: fold the findings in"])
        text = (ROOT / "tests" / "fixtures" / "state" / "handoff_valid.md").read_text(encoding="utf-8")
        text = (text.replace("@@TOPLEVEL@@", repo.as_posix()).replace("@@BRANCH@@", f"WARNING:{branch_sentinel}")
                .replace("@@HEAD@@", "none").replace("- **Live subagents:** none", "- **Live subagents:**\n" + item))
        handoff = self.tmp / "2026-11-01-work-session.tmp"
        handoff.write_text(text, encoding="utf-8", newline="\n")
        env = {**os.environ, "HANDOFF_LOG": str(self.log), "PYTHONIOENCODING": "utf-8"}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("HANDOFF_CALLER", None)
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "handoff_state.py"), str(repo),
                               "--resume", str(handoff)], capture_output=True, text=True, encoding="utf-8", env=env)
        warnings = [w for ln in self.log.read_text(encoding="utf-8").splitlines() for w in json.loads(ln)["warnings"]]
        for sentinel in (name_sentinel, branch_sentinel):
            self.assertIn(sentinel, proc.stdout, "the briefing prints both values")
            self.assertTrue(any(sentinel in w for w in warnings), "both lines reach the record's warnings")
        text_out, json_out = self.text(), self.report("--json")[1]
        for sentinel in (name_sentinel, branch_sentinel):
            self.assertEqual(text_out.count(sentinel), 0)
            self.assertEqual(json_out.count(sentinel), 0)
        self.assertIn({"kind": "branch changed", "count": 1}, json.loads(json_out)["warnings"])


class CheckFailuresTest(ReportCase):
    def test_R8_fail_codes_counted(self):
        self.write([
            rec(exit=1, fail_codes={"contract-toplevel": 1, "secret:github-token": 2}),
            rec(exit=1, fail_codes={"contract-toplevel": 3}),
            rec(exit=0, fail_codes={}),
            rec(exit=1, fail_codes={"strange code with spaces": 1, "usage": True}),
            rec(exit=1, fail_codes={"contract-made-up": 2, "secret:not-a-kind": 1}),
        ])
        failures = self.data()["check_failures"]
        self.assertEqual(failures["check_runs"], 5)
        self.assertEqual(failures["failed_runs"], 4)
        self.assertEqual(failures["codes"]["contract-toplevel"], {"runs": 2, "messages": 4})
        self.assertEqual(failures["codes"]["secret:github-token"], {"runs": 1, "messages": 2})
        self.assertEqual(failures["codes"]["other"], {"runs": 2, "messages": 4})
        self.assertEqual(set(failures["codes"]), {"contract-toplevel", "secret:github-token", "other"})
        self.assertNotIn("strange code with spaces", json.dumps(failures))
        self.assertNotIn("usage", failures["codes"])

    def test_R8_attempts_to_pass(self):
        def check(sid, minute, code, **over):
            return rec(ts=f"2026-11-09T10:{minute:02d}:00Z", exit=code, session_id=sid,
                       handoff=f"/h/{sid[:1]}.md", **over)
        self.write([
            check(S1, 0, 1), check(S1, 1, 1),
            check(S1, 2, 1, caller="handoff_state.py"),  # resume's own check: not an attempt
            old(ts="2026-11-09T10:02:30Z", exit=1, handoff="/h/1.md"),  # no caller key: not an attempt
            check(S1, 3, 0), check(S1, 4, 1),  # after the pass: ignored
            check(S2, 5, 0),
            check(S3, 6, 1),
            rec(ts="2026-11-09T10:07:00Z", exit=0, handoff="/h/x.md"),  # no session id: unattributed
        ])
        attempts = self.data()["check_failures"]["attempts"]
        self.assertEqual(attempts["groups"], 3)
        self.assertEqual(attempts["first_try_pass_rate"], round(1 / 3, 3))
        self.assertEqual(attempts["median"], 2)
        self.assertEqual(attempts["max"], 3)
        self.assertEqual(attempts["never_passed"], 1)
        self.assertEqual(attempts["unattributed"], 1)

    def test_R8_attempts_first_try_zero(self):
        self.write([rec(ts=f"2026-11-09T10:0{i}:00Z", exit=code, session_id=S1, handoff="/h/a.md")
                    for i, code in enumerate((1, 1, 0))])
        attempts = self.data()["check_failures"]["attempts"]
        self.assertEqual((attempts["groups"], attempts["first_try_pass_rate"], attempts["median"],
                          attempts["max"], attempts["never_passed"]), (1, 0.0, 3, 3, 0))
        text = "\n".join(section(self.text(), "Check failures"))
        self.assertIn("first-try pass 0%", text)


class UsageAndVersionAttemptsTest(ReportCase):
    def test_BH40_help_is_usage(self):
        self.write([
            resume(mode="handoff", args=["--help"], exit=2), resume(mode="handoff", args=["-h"], exit=2),
            resume(mode="handoff", args=[], exit=2),
            resume(mode="handoff", args=["/p"], exit=0), resume(mode="resume", args=["/p", "--resume"], exit=0),
            rec(args=["--help"], exit=2, fail_codes={"unreadable": 1}), rec(args=["-h"], exit=2, fail_codes=None),
            rec(args=["--help", "x"], exit=2), rec(args=["/h/a.md", "--git", "yes"], exit=0),
        ])
        data = self.data()
        table = data["runs"]["by_script_mode"]
        self.assertEqual(table["handoff_state.py/usage"], {"records": 3, "exit": {"2": 3}})
        self.assertEqual(table["handoff_state.py/handoff"], {"records": 1, "exit": {"0": 1}})
        self.assertEqual(table["handoff_state.py/resume"], {"records": 1, "exit": {"0": 1}})
        self.assertEqual(table["check_handoff.py/usage"], {"records": 2, "exit": {"2": 2}})
        self.assertEqual(table["check_handoff.py/check"]["records"], 2)
        self.assertEqual(data["check_failures"]["codes"]["usage"], {"runs": 2, "messages": 2})
        self.assertNotIn("unreadable", data["check_failures"]["codes"])  # the checker records --help as unreadable
        self.assertIn("usage: runs 2 · messages 2", section(self.text(), "Check failures"))
        self.assertFalse([ln for ln in section(self.text(), "Check failures") if ln.startswith("unreadable")])
        runs = "\n".join(section(self.text(), "Runs"))
        self.assertIn("handoff_state.py/usage: 3", runs)
        self.assertIn("check_handoff.py/usage: 2", runs)
        self.assertNotIn("--help", self.text())

    def test_BH41_attempts_by_version(self):
        def check(version, minute, code):
            return rec(ts=f"2026-11-09T10:{minute:02d}:00Z", version=version, exit=code, session_id=S1, handoff="/h/a.md")
        self.write([check("v1", 0, 1), check("v1", 1, 0), check("v2", 2, 0), check("v3", 3, 1),
                    rec(ts="2026-11-09T10:04:00Z", version=None, exit=0, session_id=S2, handoff="/h/b.md")])
        failures = self.data()["check_failures"]
        self.assertEqual(failures["attempts"]["groups"], 2)  # the version-blind count is unchanged
        self.assertEqual(failures["attempts"]["median"], 1.5)
        by = {e["version"]: e for e in failures["attempts_by_version"]}
        self.assertEqual(list(by), ["(none)", "v1", "v2", "v3"])
        self.assertEqual(by["v1"], {"version": "v1", "groups": 1, "first_try_pass_rate": 0.0, "median": 2, "max": 2,
                                    "never_passed": 0})
        self.assertEqual(by["v2"], {"version": "v2", "groups": 1, "first_try_pass_rate": 1.0, "median": 1, "max": 1,
                                    "never_passed": 0})
        self.assertEqual(by["v3"], {"version": "v3", "groups": 1, "first_try_pass_rate": 0.0, "median": None, "max": None,
                                    "never_passed": 1})
        lines = [ln for ln in section(self.text(), "Check failures") if ln.startswith("attempts v")]
        self.assertEqual(lines[0], "attempts v1: groups 1 · first-try pass 0% · median 2 · max 2 · never passed 0")
        self.assertEqual(len(lines), 3)

    def test_BH41b_attempts_by_version_text_is_capped_at_10(self):
        self.write([rec(version=f"v{i:02d}", session_id=S1, handoff="/h/a.md") for i in range(12)])
        self.assertEqual(len(self.data()["check_failures"]["attempts_by_version"]), 12)
        self.assertEqual(len([ln for ln in section(self.text(), "Check failures") if ln.startswith("attempts v")]), 10)

    def test_BH44_warning_kinds(self):
        self.write([
            resume(warnings=["WARNING: 2 user message(s) before the last /handoff are not in the handoff file"]),
            resume(warnings=["WARNING: 6 user message(s) before the last /handoff are not in the handoff file"]),
            rec(warnings=["WARNING: reviews not checked: transcript not found"]),
            rec(warnings=["WARNING: reviews not checked: transcript format unknown"]),
            rec(warnings=["WARNING: reviews not checked: transcript not found extra text"]),
            rec(warnings=["WARNING: 2 user message(s) before the last /handoff are not in the handoff file with a tail"]),
        ])
        kinds = {w["kind"]: w["count"] for w in self.data()["warnings"]}
        self.assertEqual(kinds, {BEFORE_KIND: 2, REVIEWS_KIND: 3, "other": 1})


class ResumeIntervalTest(ReportCase):
    def test_R8_handoff_to_resume_interval(self):
        self.write([
            rec(ts="2026-11-09T10:00:00Z", exit=0, handoff="/h/a.md", session_id=S1),
            rec(ts="2026-11-09T11:00:00Z", exit=0, handoff="/h/a.md", session_id=S1),
            rec(ts="2026-11-09T11:30:00Z", exit=1, handoff="/h/a.md", session_id=S1),
            rec(ts="2026-11-09T11:40:00Z", exit=0, handoff="/h/a.md", caller="handoff_state.py"),
            resume(ts="2026-11-09T12:00:00Z", handoff="/h/a.md", session_id=S2),
            resume(ts="2026-11-09T12:00:00Z", handoff="/h/b.md",
                   stdout=["continue: no", "handoff age: 2.0 days  WARNING: handoff is older than 7 days"]),
            resume(ts="2026-11-09T12:00:00Z", handoff="/h/c.md", stdout=["continue: no"]),
            resume(ts="2026-11-09T12:00:00Z", handoff=None, exit=1),
        ])
        interval = self.data()["resume"]["interval"]
        self.assertEqual(interval, {"n": 2, "median_hours": 24.5, "max_hours": 48.0, "unknown": 2})

    def test_R8_json_has_no_non_finite_numbers(self):
        def strict(text):
            return json.loads(text, parse_constant=lambda c: self.fail(f"non-standard JSON constant {c}"))
        huge, big = "9" * 400, "5" + "0" * 306  # float("9"*400) is inf; two 1.2e308 hours overflow a sum
        for ages, want in (([huge], (0, 1)), ([big, big], (2, 0))):
            with self.subTest(ages=len(ages)):
                self.write([resume(handoff=f"/h/{i}.md", stdout=[f"handoff age: {a} days"]) for i, a in enumerate(ages)])
                code, out, err = self.report("--json")
                self.assertEqual(code, 0, err)
                interval = strict(out)["resume"]["interval"]
                self.assertEqual((interval["n"], interval["unknown"]), want)
                self.assertNotIn("Infinity", out)
                self.assertNotIn("inf", self.text())

    def test_R8_interval_lookup_ignores_later_checks(self):
        self.write([
            resume(ts="2026-11-09T09:00:00Z", handoff="/h/a.md"),
            rec(ts="2026-11-09T10:00:00Z", exit=0, handoff="/h/a.md"),
        ])
        self.assertEqual(self.data()["resume"]["interval"]["unknown"], 1)

    def test_R8_interval_pairs_across_filters(self):
        self.write([
            rec(ts="2026-11-08T10:00:00Z", exit=0, handoff="/h/a.md"),
            resume(ts="2026-11-09T10:00:00Z", handoff="/h/a.md"),
        ])
        interval = self.data("--since", "2026-11-09")["resume"]["interval"]
        self.assertEqual((interval["n"], interval["max_hours"]), (1, 24.0))


class ProjectTest(ReportCase):
    def projects(self, *args):
        return {p["project"]: p["runs"] for p in self.data(*args)["projects"]}

    def test_R8_check_project_from_state_record(self):
        self.write([
            rec(ts="2026-11-09T10:00:00Z", script="handoff_state.py", mode="resume", project="/work/proj",
                session_id=S1, handoff="/h/a.md"),
            rec(ts="2026-11-09T10:00:01Z", caller="handoff_state.py", session_id=S1,
                handoff="/h/a.md", cwd="/elsewhere"),
            rec(ts="2026-11-09T10:00:02Z", caller="handoff_state.py", session_id=S2,
                handoff="/h/a.md", cwd="/elsewhere"),
            rec(ts="2026-11-09T10:00:03Z", project="/work/other", cwd="/elsewhere"),
            rec(ts="2026-11-09T10:00:04Z", toplevel="/work/top", cwd="/elsewhere"),
            rec(ts="2026-11-09T10:00:05Z", cwd="/work/cwd/"),
        ])
        self.assertEqual(self.projects(), {
            "/work/proj": 2, "/elsewhere": 1, "/work/other": 1, "/work/top": 1, "/work/cwd": 1})
        self.assertEqual(self.data("--project", "/work/proj")["meta"]["records"], 2)

    def test_R8_tilde_project_expanded_before_comparison(self):
        home_proj = str(self.home / "proj")
        self.write([
            resume(ts="2026-11-09T10:00:00Z", project=home_proj, session_id=S1, handoff="/h/a.md"),
            rec(ts="2026-11-09T10:00:01Z", project="~/proj"),
            rec(ts="2026-11-09T10:00:02Z", caller="handoff_state.py", session_id=S1,
                handoff="/h/a.md", project="~/ignored"),
        ])
        key = run_report.checker_path_key()(home_proj)
        self.assertEqual(self.projects(), {key: 3})
        self.assertEqual(self.data("--project", "~/proj")["meta"]["records"], 3)
        self.assertEqual(self.data("--project", home_proj)["meta"]["records"], 3)

    def test_R8_projects_top_10_and_unknown(self):
        self.write([rec(project=f"/p/{i:02d}") for i in range(12)] + [rec(cwd=None)])
        listed = self.data()["projects"]
        self.assertEqual(len(listed), 10)
        self.assertEqual(len(section(self.text(), "Projects")), 10)

    def test_R8_path_key_matches_checker(self):
        samples = ["/a/b", "/a/b/", "  /x/y  ", "/", "C:\\work\\X\\p", "c:/work/X/p/", "D:/", "/c/work/X",
                   "/mnt/c/work/X/", "~/proj", "relative/dir", ""]
        shared = run_report.checker_path_key()
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(shared(sample), check_handoff.path_key(sample))

    def test_R8_path_key_is_reused_not_copied(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn("path_key", names)


class SizeAndVersionTest(ReportCase):
    def handoff_path(self, name):
        return str(self.home / ".claude" / "handoffs" / name)

    def test_R8_size_distribution(self):
        sizes = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 16384, 25000]
        self.write([rec(handoff_bytes=n, handoff=self.handoff_path(f"{i}.md")) for i, n in enumerate(sizes)] +
                   [rec(handoff_bytes=None, handoff=self.handoff_path("n.md")),
                    rec(handoff_bytes=True, handoff=self.handoff_path("t.md")),
                    rec(handoff_bytes="9", handoff=self.handoff_path("s.md")),
                    resume(handoff_bytes=500, handoff=self.handoff_path("r.md"))])
        size = self.data()["handoff_size"]
        self.assertEqual(size, {"n": 10, "min": 1000, "median": 5500, "p90": 16384, "max": 25000,
                                "over_24_kib": 1})
        self.assertNotEqual(size["p90"], size["max"])
        line = " ".join(section(self.text(), "Handoff size"))
        self.assertIn("n 10", line)
        self.assertIn("over 24 KiB 1", line)

    def test_BH42_size_scope(self):
        self.write([
            rec(ts="2026-11-09T10:00:00Z", handoff_bytes=1000, handoff=self.handoff_path("a.md")),
            rec(ts="2026-11-09T10:01:00Z", handoff_bytes=2000, handoff=self.handoff_path("a.md")),  # a recheck: the last counts
            rec(handoff_bytes=3000, handoff=self.handoff_path("b.md")),
            rec(handoff_bytes=4000, handoff=self.handoff_path("c.md"), caller="handoff_state.py"),  # a resume's own check
            rec(handoff_bytes=5000, handoff="/tmp/demo/h.md"),  # a fixture or scratch path
            rec(handoff_bytes=5500, handoff=self.handoff_path("sub/x.md")),  # not directly below handoffs/
            rec(handoff_bytes=5600, handoff=None),
            rec(handoff_bytes=6000, handoff="~/.claude/handoffs/e.md"),  # ~ is the home folder
            old(handoff_bytes=7000, handoff=self.handoff_path("o.md")),  # no caller key: not a direct check
        ])
        size = self.data()["handoff_size"]
        self.assertEqual((size["n"], size["min"], size["max"]), (3, 2000, 6000))
        self.assertEqual(size["median"], 3000)

    def test_BH43_size_limit_24(self):
        self.write([rec(handoff_bytes=20 * 1024, handoff=self.handoff_path("a.md")),
                    rec(handoff_bytes=25 * 1024, handoff=self.handoff_path("b.md"))])
        size = self.data()["handoff_size"]
        self.assertEqual(size["over_24_kib"], 1)
        self.assertNotIn("over_16_kib", size)
        self.assertIn("over 24 KiB 1", " ".join(section(self.text(), "Handoff size")))
        self.assertNotIn("16 KiB", self.text())

    def test_R8_versions_breakdown(self):
        self.write([
            rec(version="v1", exit=0), rec(version="v1", exit=1),
            resume(version="v1", **{"continue": "yes (x)"}), resume(version="v1", **{"continue": "no"}),
            rec(version="v2", exit=0),
            rec(version=None, exit=0),
        ])
        versions = {v["version"]: v for v in self.data()["versions"]}
        self.assertEqual(set(versions), {"v1", "v2", "(none)"})
        self.assertEqual(versions["v1"]["records"], 4)
        self.assertEqual((versions["v1"]["check_runs"], versions["v1"]["check_failed"]), (2, 1))
        self.assertEqual(versions["v1"]["check_fail_rate"], 0.5)
        self.assertEqual((versions["v1"]["resume_runs"], versions["v1"]["resume_continue_yes"]), (2, 1))
        self.assertEqual(versions["v1"]["continue_yes_rate"], 0.5)
        self.assertEqual(versions["v2"]["check_fail_rate"], 0.0)
        self.assertIsNone(versions["v2"]["continue_yes_rate"])
        text = "\n".join(section(self.text(), "Versions"))
        self.assertIn("v1: 4 records", text)

    def test_R8_runs_breakdown(self):
        self.write([
            rec(exit=0), rec(exit=1), rec(exit=1, caller="handoff_state.py"), old(exit=0),
            resume(exit=0), resume(exit=1), resume(mode="handoff", exit=0, args=["/p"]),
        ])
        runs = self.data()["runs"]
        self.assertEqual(runs["by_script_mode"]["check_handoff.py/check"],
                         {"records": 4, "exit": {"0": 2, "1": 2}})
        self.assertEqual(runs["by_script_mode"]["handoff_state.py/resume"],
                         {"records": 2, "exit": {"0": 1, "1": 1}})
        self.assertEqual(runs["by_script_mode"]["handoff_state.py/handoff"],
                         {"records": 1, "exit": {"0": 1}})
        self.assertEqual(runs["check_callers"], {"direct": 2, "handoff_state.py": 1, "unknown": 1})


class SessionsTest(ReportCase):
    def test_R8_transcript_resolution(self):
        sid = "11111111-2222-3333-4444-555555555555"
        projects = self.home / ".claude" / "projects" / "x"
        projects.mkdir(parents=True)
        self._write(projects / f"{sid}.jsonl", "{}\n")
        self.write([
            rec(ts="2026-11-09T10:00:00Z", session_id=sid, project="/work/p"),
            rec(ts="2026-11-09T10:05:00Z", session_id=sid),
            rec(ts="2026-11-09T11:00:00Z", session_id="other-session-1"),
            rec(ts="2026-11-09T12:00:00Z", session_id="../../etc/passwd"),
        ])
        sessions = {s["session_id"]: s for s in self.data()["sessions"]}
        self.assertEqual(sessions[sid]["transcript"], str(projects / f"{sid}.jsonl"))
        self.assertEqual(sessions[sid]["runs"], 2)
        self.assertEqual(sessions[sid]["first_ts"], "2026-11-09T10:00:00Z")
        self.assertEqual(sessions[sid]["project"], "/work/p")
        self.assertIsNone(sessions["(invalid)"]["transcript"])
        text = "\n".join(section(self.text(), "Sessions"))
        self.assertIn("not found", text)
        self.assertIn(f"~/.claude/projects/x/{sid}.jsonl", text)

    def test_R8_transcript_uses_checker_lookup(self):
        sid = "11111111-2222-3333-4444-555555555555"
        first, recent = (self.home / ".claude" / "projects" / name / f"{sid}.jsonl" for name in ("a", "b"))
        for path, mtime in ((first, 1_700_000_000), (recent, 1_800_000_000)):
            path.parent.mkdir(parents=True)
            self._write(path, "{}\n")
            os.utime(path, (mtime, mtime))
        self.write([rec(session_id=sid)])
        (session,) = self.data()["sessions"]
        self.assertEqual(session["transcript"], str(recent))  # the most recently modified copy, not the first path
        self.assertFalse(hasattr(run_report, "transcript_of"), "the lookup is check_handoff.find_transcript")
        with mock.patch.object(check_handoff, "find_transcript", return_value=None) as found, \
                mock.patch.object(run_report, "checker_module", return_value=check_handoff):
            self.assertIsNone(self.data()["sessions"][0]["transcript"])
        found.assert_called_once_with(sid)

    def test_R8_transcript_old_checker(self):
        sid = "11111111-2222-3333-4444-555555555555"
        projects = self.home / ".claude" / "projects" / "x"
        projects.mkdir(parents=True)
        self._write(projects / f"{sid}.jsonl", "{}\n")
        self.write([rec(session_id=sid)])
        folder = self.tmp / "mixed"
        folder.mkdir()
        (folder / "check_handoff.py").write_bytes(
            (ROOT / "tests" / "fixtures" / "base" / "1c6bbeb" / "check_handoff.py").read_bytes())
        for name in ("run_report.py", "run_log.py"):
            (folder / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        proc = subprocess.run([sys.executable, str(folder / "run_report.py"), "--log", str(self.log), "--json"],
                              capture_output=True, text=True, encoding="utf-8", env={**os.environ})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        (session,) = json.loads(proc.stdout)["sessions"]
        self.assertEqual(session["session_id"], sid)
        self.assertIsNone(session["transcript"])

    def test_R8_invalid_session_id_never_globbed(self):
        token = "gh" + "p_" + "Ab3" * 12  # built here, so no complete token sits in any file
        self.write([rec(session_id="../../etc/*"), rec(session_id="short"), rec(session_id=token),
                    rec(session_id="session-" + "x" * 120)])
        with mock.patch("pathlib.Path.glob", side_effect=AssertionError("glob called")):
            sessions = self.data()["sessions"]
            text = self.text()
        self.assertEqual(sessions, [{"session_id": "(invalid)", "project": "/work/p",
                                     "first_ts": "2026-11-09T10:00:00Z", "runs": 4, "transcript": None}])
        for output in (json.dumps(sessions), text):
            self.assertNotIn(token, output)
            self.assertNotIn("xxxxxxxx", output)
            self.assertNotIn("etc", output)

    def test_R8_session_rule_is_run_log_rule(self):
        import run_log
        self.assertEqual(run_report.SESSION_ID.pattern, run_log.SESSION_ID.pattern)

    def test_R8_sessions_most_recent_10(self):
        ids = [f"00000000-0000-4000-8000-0000000000{i:02d}" for i in range(12)]
        self.write([rec(ts=f"2026-11-09T10:{i:02d}:00Z", session_id=sid) for i, sid in enumerate(ids)])
        listed = self.data()["sessions"]
        self.assertEqual(len(listed), 10)
        self.assertEqual(listed[0]["session_id"], ids[11])


class OutputContractTest(ReportCase):
    def test_R8_text_paths_use_tilde(self):
        proj = self.home / "dev" / "proj"
        sid = "11111111-2222-3333-4444-555555555555"
        transcripts = self.home / ".claude" / "projects" / "y"
        transcripts.mkdir(parents=True)
        self._write(transcripts / f"{sid}.jsonl", "{}\n")
        log = self.home / ".claude" / "handoffs" / ".state" / "runs.jsonl"
        log.parent.mkdir(parents=True)
        self.write([rec(project=str(proj), cwd=str(proj), session_id=sid)], path=log)
        code, out, err = self.report("--log", str(log), "--project", str(proj), log=False)
        self.assertEqual(code, 0, err)
        self.assertNotIn(str(self.home), out)
        self.assertIn("~/dev/proj", out)
        self.assertIn("~/.claude/handoffs/.state/runs.jsonl", out.splitlines()[0])
        self.assertIn("project=~/dev/proj", out.splitlines()[1])
        self.assertIn(f"~/.claude/projects/y/{sid}.jsonl", out)

    def test_R8_tilde_not_applied_to_lookalike_prefix(self):
        lookalike = str(self.home) + "x/proj"
        self.write([rec(project=lookalike)])
        self.assertIn(lookalike, self.text())

    def test_R8_json_fixed_keys(self):
        for records in ([], [rec(), resume()]):
            self.write(records)
            self.assertEqual(list(self.data()), JSON_KEYS)

    def test_R8_never_echoes_stdout_or_args(self):
        a, b = "SENTINEL" + "_STDOUT_8841", "SENTINEL" + "_ARGS_1127"
        self.write([
            rec(stdout=[a, f"  indented {a}"], args=[b, "--git", "yes"], exit=1,
                fail_codes={"contract-toplevel": 1}, handoff="/h/a.md", session_id=S1),
            resume(stdout=[a, "continue: yes (x)"], args=["--resume", b], handoff="/h/a.md"),
        ])
        code, text, _ = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.report("--json")[1])["meta"]["records"], 2)
        for output in (text, self.report("--json")[1]):
            self.assertNotIn(a, output)
            self.assertNotIn(b, output)

    def test_R8_runs_as_script_under_current_python(self):
        self.write([rec()])
        env = {**os.environ}
        proc = subprocess.run([sys.executable, str(SCRIPT), "--log", str(self.log)],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("run report:"))
        self.assertNotIn("Traceback", proc.stderr)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--log", str(self.tmp)],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
