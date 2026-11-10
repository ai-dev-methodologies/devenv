"""Tests for scripts/handoff_state.py (new state-block, resume and warning behavior).

Item ids in test names (H1, G1, A4, ...) refer to the review verdict and to
scratch/new-format-spec.md. Every run uses a fake HOME so the real ~/.claude is
never read or written.

Assumed interface (not in the current code):
  - HANDOFF_CHECKER: environment variable holding the path of the checker script.
    The script runs it as `<python> $HANDOFF_CHECKER <handoff path>`, like the
    default scripts/check_handoff.py.
  - Warnings and the checker stderr excerpt may go to stdout or stderr; tests read
    both streams together.
"""
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "handoff_state.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "state"
TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "check" / "template.md"
SYSTEM_PY = Path("/usr/bin/python3")
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
LOCK = "package-lock.json"


class StateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.handoffs = self.home / ".claude" / "handoffs"
        self.handoffs.mkdir(parents=True)
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        self.script = SCRIPT

    def use_scripts_without_budget(self):
        """Run a copy of scripts/ that has no context_budget*.py, so a budget script planted under
        HOME/.claude/scripts/ is the one found (the engine's own copy sits next to handoff_state.py and wins)."""
        folder = self.tmp / "engine-scripts"
        folder.mkdir()
        for path in (ROOT / "scripts").glob("*.py"):
            if not path.name.startswith("context_budget"):
                (folder / path.name).write_bytes(path.read_bytes())
        self.script = folder / "handoff_state.py"

    # --- helpers -----------------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in (
            "HANDOFF_CHECKER", "HANDOFF_LOG", "CLAUDE_CODE_SESSION_ID", "HANDOFF_CALLER")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8",
                   HANDOFF_LOG=str(self.tmp / "runs.jsonl"))
        env.update(extra)
        return env

    def git(self, *args, repo=None):
        return subprocess.run(
            ["git", *GIT_ID, "-C", str(repo or self.repo), *args],
            capture_output=True, text=True, encoding="utf-8", env=self.env(), check=True,
        ).stdout.strip()

    def init_repo(self, files):
        """git init, then commit `files` (name -> text). Branch is main."""
        self.git("init", "-q", "-b", "main")
        for name, text in files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def write(self, name, text):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def run_state(self, *args, extra_env=None, cwd=None):
        return subprocess.run(
            [sys.executable, str(self.script), str(cwd or self.repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.env(**(extra_env or {})), cwd=str(self.tmp),
        )

    def write_handoff(self, name, text, mtime=None):
        p = self.handoffs / name
        p.write_text(text, encoding="utf-8", newline="\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def fixture_text(self, name, toplevel=None):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        return text.replace("@@TOPLEVEL@@", toplevel or self.repo.as_posix())

    def valid_handoff_text(self, branch="main", toplevel=None, head="none"):
        text = self.fixture_text("handoff_valid.md", toplevel)
        return text.replace("@@BRANCH@@", branch).replace("@@HEAD@@", head)

    def project_status_name(self, folder, key):
        return f"{folder}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}.status.txt"

    @staticmethod
    def lines_with(text, prefix):
        return [ln for ln in text.splitlines() if ln.startswith(prefix)]

    @staticmethod
    def combined(proc):
        return proc.stdout + "\n" + proc.stderr


# --- H1: porcelain parsing and lockfile detection ----------------------------
class LockfileDetectionTests(StateTestCase):
    def test_H1_leading_space_status_lockfile_is_detected(self):
        self.init_repo({LOCK: "{}\n", "a.txt": "a\n"})
        self.write(LOCK, '{"changed": true}\n')  # ` M package-lock.json`, the only status line
        out = self.run_state().stdout
        found = self.lines_with(out, "lockfile changed:")
        self.assertEqual(len(found), 1, out)
        self.assertIn(LOCK, found[0], out)

    def test_H1_lockfile_in_subfolder_with_leading_space_is_detected(self):
        self.init_repo({"web/yarn.lock": "x\n"})
        self.write("web/yarn.lock", "y\n")
        found = self.lines_with(self.run_state().stdout, "lockfile changed:")
        self.assertEqual(len(found), 1)
        self.assertIn("yarn.lock", found[0])

    def test_H1_rename_checks_original_path(self):
        self.init_repo({LOCK: "{}\n", "a.txt": "a\n"})
        self.git("mv", LOCK, "moved.json")
        out = self.run_state().stdout
        found = self.lines_with(out, "lockfile changed:")
        self.assertEqual(len(found), 1, out)
        self.assertIn(LOCK, found[0], f"rename source not checked: {out}")

    def test_H1_no_lockfile_change_reports_no(self):
        self.init_repo({"a.txt": "a\n"})
        self.write("a.txt", "b\n")
        self.assertEqual(self.lines_with(self.run_state().stdout, "lockfile changed:"), ["lockfile changed: no"])


# --- G1, A5, G2: bounded git state block ---------------------------------------
class StatusBlockTests(StateTestCase):
    def build_dirty_repo(self):
        tracked = [f"alpha_tracked_{i}.txt" for i in range(3)]
        self.init_repo({n: "v1\n" for n in tracked})
        for n in tracked:
            self.write(n, "v2\n")
        untracked = [f"scratch_untracked_{i:02d}.dat" for i in range(60)]
        for n in untracked:
            self.write(n, "u\n")
        return tracked + untracked

    def check_bounded_block(self, proc, names):
        out = proc.stdout
        for n in names:
            self.assertNotIn(n, out, f"changed file name leaked into stdout: {n}")
        self.assertLessEqual(len(out.splitlines()), 15, out)
        status = self.lines_with(out, "status:")
        self.assertEqual(status, ["status: 63 changed (?? 60 · M 3)"], out)
        files = self.lines_with(out, "status file: ")
        self.assertEqual(len(files), 1, out)
        path = Path(files[0][len("status file: "):].strip())
        self.assertTrue(path.is_file(), f"status file missing: {path}")
        self.assertFalse(fnmatch(path.name, "*-session.tmp"), path.name)
        expected = self.project_status_name("proj", self.repo.as_posix())
        self.assertEqual(path.resolve(), (self.home / ".claude" / "handoffs" / ".state" / expected).resolve())
        body = path.read_text(encoding="utf-8")
        missing = [n for n in names if n not in body]
        self.assertEqual(missing, [], "paths missing from status file")
        self.assertEqual(list(self.handoffs.glob("*-session.tmp")), [])

    def test_G1_A5_default_mode_block_is_bounded(self):
        names = self.build_dirty_repo()
        self.check_bounded_block(self.run_state(), names)

    def test_G1_A5_resume_mode_block_is_bounded(self):
        names = self.build_dirty_repo()
        self.check_bounded_block(self.run_state("--resume"), names)

    def test_G1_status_clean(self):
        self.init_repo({"a.txt": "a\n"})
        self.assertEqual(self.lines_with(self.run_state().stdout, "status:"), ["status: clean"])

    def test_G1_status_codes_are_xy_trimmed_and_counted(self):
        self.init_repo({n: "v1\n" for n in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt")})
        self.write("a.txt", "v2\n")
        self.git("add", "a.txt")
        self.write("a.txt", "v3\n")            # MM
        self.write("new.txt", "n\n")
        self.git("add", "new.txt")             # A
        (self.repo / "d.txt").unlink()         # ` D` -> D
        self.git("mv", "e.txt", "e2.txt")      # R
        out = self.run_state().stdout
        status = self.lines_with(out, "status:")
        self.assertEqual(len(status), 1, out)
        m = re.fullmatch(r"status: (\d+) changed \((.+)\)", status[0])
        self.assertIsNotNone(m, status[0])
        self.assertEqual(m.group(1), "4")
        counts = dict(part.rsplit(" ", 1) for part in m.group(2).split(" · "))
        self.assertEqual(counts, {"MM": "1", "A": "1", "D": "1", "R": "1"})

    def test_G2_default_mode_prints_diffstat(self):
        self.init_repo({"a.txt": "1\n2\n3\n", "b.txt": "x\n"})
        self.write("a.txt", "1\n2\n3\n4\n5\n")
        self.write("b.txt", "y\n")
        expected = self.git("diff", "--shortstat", "HEAD")
        self.assertTrue(expected)
        self.assertEqual(self.lines_with(self.run_state().stdout, "diffstat:"), [f"diffstat: {expected}"])

    def test_G2_diffstat_none_when_unchanged(self):
        self.init_repo({"a.txt": "a\n"})
        self.assertEqual(self.lines_with(self.run_state().stdout, "diffstat:"), ["diffstat: none"])


# --- A4, M4: continue judgment ------------------------------------------------
class ContinueTests(StateTestCase):
    """The handoff file body is never evidence; only the argument tokens are."""

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())

    def continue_lines(self, *args):
        proc = self.run_state("--resume", *args)
        return proc, self.lines_with(proc.stdout, "continue:")

    def test_A4_continue_token_table(self):
        yes = {"--c": "--c", "--continue": "--continue", "계속": "계속",
               "「계속」": "계속", "work --c": "--c",
               "계속.": "계속", "\"계속\"": "계속"}
        # bare `continue` is not a continue word: it stays a slug selector (`/resume-handoff continue-x`)
        no = ["계속하지 마", "계속해", "yesterday", "continued", "work", "continue", "--cont"]
        for arg, token in yes.items():
            with self.subTest(arg=arg):
                proc, found = self.continue_lines(arg)
                self.assertEqual(len(found), 1, proc.stdout)
                self.assertRegex(found[0], r"^continue: yes \(.*\)$")
                self.assertIn(token, found[0])
        for arg in no:
            with self.subTest(arg=arg):
                proc, found = self.continue_lines(arg)
                self.assertEqual(found, ["continue: no"], proc.stdout)

    def test_A4_no_argument_is_no(self):
        proc, found = self.continue_lines()
        self.assertEqual(found, ["continue: no"], proc.stdout)

    def test_M4_continue_word_only_in_handoff_body_is_no(self):
        text = self.valid_handoff_text().replace(
            "- none\n", "- 「계속」 · --continue · keep going\n")
        self.write_handoff("2026-11-02-newer-session.tmp", text, mtime=2_000_000_000)
        proc, found = self.continue_lines()
        self.assertEqual(found, ["continue: no"], proc.stdout)

    def test_M4_auto_resume_line_is_gone(self):
        text = "**Auto-resume:** yes\n" + self.valid_handoff_text()
        marked = self.write_handoff("2026-11-02-marked-session.tmp", text, mtime=2_000_000_000)
        for args in ((), (str(marked),)):  # a resolved handoff, by newest and by path
            with self.subTest(args=args):
                proc = self.run_state("--resume", *args)
                self.assertTrue(self.lines_with(proc.stdout, "handoff: ")[0].endswith(marked.name), proc.stdout)
                self.assertEqual(self.lines_with(proc.stdout, "handoff auto-resume:"), [], proc.stdout)


# --- M6: candidate content check without a line-count threshold ------------------
class CandidateContentTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})

    def handoff_lines(self, proc):
        return self.lines_with(proc.stdout, "handoff: ")

    def test_M6_short_valid_handoff_is_accepted(self):
        short = self.fixture_text("handoff_short_valid.md")
        self.write_handoff("2026-11-01-short-session.tmp", short)
        found = self.handoff_lines(self.run_state("--resume"))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("2026-11-01-short-session.tmp"), found[0])

    def test_M6_placeholder_only_handoff_is_rejected(self):
        text = self.fixture_text("handoff_placeholder_only.md")
        self.write_handoff("2026-11-01-blank-session.tmp", text)
        proc = self.run_state("--resume")
        found = self.handoff_lines(proc)
        self.assertEqual(len(found), 1, proc.stdout)
        self.assertIn("none found", found[0])

    def test_M6_placeholder_only_newer_file_loses_to_older_valid_file(self):
        short = self.fixture_text("handoff_short_valid.md")
        blank = self.fixture_text("handoff_placeholder_only.md")
        self.write_handoff("2026-11-01-short-session.tmp", short, mtime=1_700_000_000)
        self.write_handoff("2026-11-02-blank-session.tmp", blank, mtime=1_800_000_000)
        found = self.handoff_lines(self.run_state("--resume"))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("2026-11-01-short-session.tmp"), found[0])


# --- M7: undecodable candidate ---------------------------------------------------
class UnreadableCandidateTests(StateTestCase):
    def test_M7_unreadable_candidate_is_skipped_with_warning(self):
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-good-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        bad = self.handoffs / "2026-11-02-bad-session.tmp"
        bad.write_bytes(b"\xff\xfe\xfd not utf-8\n" * 20)
        os.utime(bad, (1_800_000_000, 1_800_000_000))
        proc = self.run_state("--resume")
        self.assertNotIn("Traceback", proc.stderr)
        found = self.lines_with(proc.stdout, "handoff: ")
        self.assertEqual(len(found), 1, proc.stdout)
        self.assertTrue(found[0].endswith("2026-11-01-good-session.tmp"), found[0])
        warns = self.lines_with(self.combined(proc), "WARNING: unreadable handoff skipped:")
        self.assertEqual(len(warns), 1, self.combined(proc))
        self.assertIn("2026-11-02-bad-session.tmp", warns[0])


# --- M8: detached HEAD branch comparison -----------------------------------------
class DetachedBranchTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.write("a.txt", "b\n")
        self.git("commit", "-q", "-am", "second")

    def printed_branch(self):
        found = self.lines_with(self.run_state().stdout, "branch: ")
        self.assertEqual(len(found), 1)
        return found[0][len("branch: "):]

    def branch_line(self, proc):
        found = self.lines_with(proc.stdout, "handoff branch:")
        self.assertEqual(len(found), 1, proc.stdout)
        return found[0]

    def test_M8_same_detached_commit_is_not_branch_changed(self):
        self.git("checkout", "-q", "--detach", "HEAD~1")
        recorded = self.printed_branch()
        self.assertTrue(recorded.startswith("detached "), recorded)
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(recorded))
        proc = self.run_state("--resume")
        line = self.branch_line(proc)
        self.assertIn("same", line)
        self.assertNotIn("WARNING: branch changed", self.combined(proc))

    def test_M8_different_detached_commit_is_branch_changed(self):
        self.git("checkout", "-q", "--detach", "HEAD~1")
        recorded = self.printed_branch()
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(recorded))
        self.git("checkout", "-q", "--detach", "main")
        proc = self.run_state("--resume")
        self.assertIn("WARNING: branch changed", self.branch_line(proc))

    def test_M8_named_branch_with_note_is_same(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text("main (2 ahead)"))
        self.assertIn("same", self.branch_line(self.run_state("--resume")))


# --- M10: checker failures are loud ----------------------------------------------
class CheckerFailureTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.fake = FIXTURES / "fake_checker.py"

    def run_with_checker(self, checker, mode="exit0"):
        return self.run_state("--resume", extra_env={"HANDOFF_CHECKER": str(checker), "FAKE_CHECKER_MODE": mode})

    def test_M10_missing_checker_warns_and_fails(self):
        proc = self.run_with_checker(self.tmp / "no-such-checker.py")
        self.assertTrue(self.lines_with(self.combined(proc), "WARNING: checker"), self.combined(proc))
        self.assertNotEqual(proc.returncode, 0)

    def test_M10_checker_exit_2_warns_and_fails(self):
        proc = self.run_with_checker(self.fake, "exit2")
        self.assertTrue(self.lines_with(self.combined(proc), "WARNING: checker"), self.combined(proc))
        self.assertNotEqual(proc.returncode, 0)

    def test_M10_checker_exit_0_is_quiet_and_succeeds(self):
        proc = self.run_with_checker(self.fake, "exit0")
        self.assertEqual(self.lines_with(self.combined(proc), "WARNING: checker"), [])
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_M10_checker_exit_1_is_a_finding_not_a_warning(self):
        proc = self.run_with_checker(self.fake, "exit1")
        self.assertEqual(self.lines_with(self.combined(proc), "WARNING: checker"), [])

    def test_M10_checker_stderr_is_cut_to_20_lines(self):
        proc = self.run_with_checker(self.fake, "lines")
        shown = [ln for ln in self.combined(proc).splitlines() if "LINE-" in ln]
        self.assertGreaterEqual(len(shown), 1, self.combined(proc))
        self.assertLessEqual(len(shown), 20)

    def test_M10_checker_stderr_is_cut_to_2000_chars(self):
        proc = self.run_with_checker(self.fake, "chars")
        shown = self.combined(proc).count("ζ")
        self.assertGreaterEqual(shown, 1, self.combined(proc))
        self.assertLessEqual(shown, 2000)


# --- A11: rules file layers -------------------------------------------------------
class RulesFileTests(StateTestCase):
    OUTSIDE = "WARNING: rules file section outside override points:"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})

    def project_rules(self, fixture):
        self.write(".claude/handoff-rules.local.md", (FIXTURES / fixture).read_text(encoding="utf-8"))

    def user_rules(self, text):
        p = self.home / ".claude" / "handoff-rules.local.md"
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def test_A11_user_rules_file_none_when_absent(self):
        self.assertEqual(self.lines_with(self.run_state().stdout, "user rules file:"), ["user rules file: none"])

    def test_A11_user_rules_file_path_when_present(self):
        p = self.user_rules("# mine\n")
        found = self.lines_with(self.run_state().stdout, "user rules file:")
        self.assertEqual(len(found), 1)
        self.assertEqual(Path(found[0][len("user rules file:"):].strip()).resolve(), p.resolve())

    def test_A11_project_rules_file_line_is_kept(self):
        self.project_rules("rules_allowed.md")
        found = self.lines_with(self.run_state().stdout, "rules file:")
        self.assertEqual(len(found), 1, "`user rules file:` must not match the project line")
        self.assertEqual(Path(found[0][len("rules file:"):].strip()).resolve(),
                         (self.repo / ".claude" / "handoff-rules.local.md").resolve())

    def test_A11_outside_sections_each_warn_once(self):
        self.project_rules("rules_outside.md")
        proc = self.run_state()
        warns = self.lines_with(self.combined(proc), self.OUTSIDE)
        self.assertEqual(len(warns), 2, self.combined(proc))
        self.assertTrue(any("random-notes" in w for w in warns))
        self.assertTrue(any("other-topic" in w for w in warns))
        self.assertEqual(proc.returncode, 0, "a warning must not change the exit code")

    def test_A11_allowed_sections_do_not_warn(self):
        self.project_rules("rules_allowed.md")
        proc = self.run_state()
        self.assertEqual(self.lines_with(self.combined(proc), self.OUTSIDE), [])
        self.assertEqual(proc.returncode, 0)

    def test_A11_user_layer_has_no_section_limit(self):
        self.user_rules("# mine\n\n## anything-goes\ntext\n")
        self.assertEqual(self.lines_with(self.combined(self.run_state()), self.OUTSIDE), [])


# --- context-budget: optional line from ~/.claude/scripts/context_budget.py --------
class ContextBudgetTests(StateTestCase):
    PREFIX = "context-budget:"
    WARN = "WARNING: context-budget"

    def setUp(self):
        super().setUp()
        self.use_scripts_without_budget()
        self.init_repo({"a.txt": "a\n"})
        self.scripts = self.home / ".claude" / "scripts"
        self.scripts.mkdir(parents=True)
        self.budget = self.scripts / "context_budget.py"

    def fake_budget(self, stdout, code=0):
        """Fake context_budget.py: prints `stdout` and exits `code` only when called as `--line`."""
        self.budget.write_text(
            "import sys\n"
            "if sys.argv[1:] != ['--line']:\n"
            "    sys.exit(9)\n"
            f"sys.stdout.write({stdout!r})\n"
            f"sys.exit({code})\n",
            encoding="utf-8", newline="\n",
        )

    def test_budget_line_is_printed_in_default_mode(self):
        self.fake_budget("context-budget 2026-11-08 total=1 cap=2 status=ok\n")
        proc = self.run_state()
        self.assertEqual(self.lines_with(proc.stdout, self.PREFIX),
                         [f"{self.PREFIX} context-budget 2026-11-08 total=1 cap=2 status=ok"], proc.stdout)
        self.assertEqual(self.lines_with(self.combined(proc), self.WARN), [])
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_budget_exit_2_still_prints_line_and_does_not_fail(self):
        self.fake_budget("context-budget total=99 status=over\n", code=2)
        proc = self.run_state()
        self.assertEqual(self.lines_with(proc.stdout, self.PREFIX),
                         [f"{self.PREFIX} context-budget total=99 status=over"], proc.stdout)
        self.assertEqual(self.lines_with(self.combined(proc), self.WARN), [])
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_budget_other_exit_code_warns_without_line(self):
        self.fake_budget("context-budget total=1\n", code=3)
        proc = self.run_state()
        self.assertEqual(self.lines_with(proc.stdout, self.PREFIX), [], proc.stdout)
        self.assertEqual(len(self.lines_with(self.combined(proc), self.WARN)), 1, self.combined(proc))
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_budget_empty_output_warns_without_line(self):
        self.fake_budget("", code=0)
        proc = self.run_state()
        self.assertEqual(self.lines_with(proc.stdout, self.PREFIX), [], proc.stdout)
        self.assertEqual(len(self.lines_with(self.combined(proc), self.WARN)), 1, self.combined(proc))
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_budget_script_absent_prints_neither_line_nor_warning(self):
        proc = self.run_state()
        self.assertEqual(self.lines_with(proc.stdout, self.PREFIX), [], proc.stdout)
        self.assertEqual(self.lines_with(self.combined(proc), self.WARN), [])

    def test_budget_not_printed_in_resume_mode(self):
        self.fake_budget("context-budget total=1 status=ok\n")
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_state("--resume")
        self.assertEqual(self.lines_with(proc.stdout, self.PREFIX), [], proc.stdout)
        self.assertEqual(self.lines_with(self.combined(proc), self.WARN), [])

    def test_budget_multiline_output_keeps_first_line_cut_to_200_chars(self):
        first = "context-budget " + "x" * 300
        self.fake_budget(first + "\nsecond line\n")
        proc = self.run_state()
        found = self.lines_with(proc.stdout, self.PREFIX)
        self.assertEqual(found, [f"{self.PREFIX} {first[:200]}"], proc.stdout)
        self.assertNotIn("second line", proc.stdout)


# --- where the budget script is found: next to handoff_state.py first, else ~/.claude/scripts/ ------
class BudgetLocationTests(StateTestCase):
    REAL_LINE = re.compile(r"^context-budget: context-budget \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \S+ total=\d+ "
                           r"cap=\d+ status=(ok|over) files=\d+ missing=\d+$")

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        budget = self.home / ".claude" / "scripts" / "context_budget.py"
        budget.parent.mkdir(parents=True)
        budget.write_text("print('older install')\n", encoding="utf-8")

    def test_budget_sibling_preferred(self):
        proc = self.run_state()  # the engine's scripts/ as shipped: its own context_budget.py is the sibling
        found = self.lines_with(proc.stdout, "context-budget:")
        self.assertEqual(len(found), 1, proc.stdout)
        self.assertRegex(found[0], self.REAL_LINE)
        self.assertNotIn("older install", proc.stdout)

    def test_budget_home_fallback_without_sibling(self):
        self.use_scripts_without_budget()
        proc = self.run_state()
        self.assertEqual(self.lines_with(proc.stdout, "context-budget:"), ["context-budget: older install"],
                         proc.stdout)



def system_python_version():
    """(major, minor) of /usr/bin/python3, or None when it is absent or does not run."""
    if not SYSTEM_PY.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(SYSTEM_PY), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = proc.stdout.split()
    return (int(parts[0]), int(parts[1])) if proc.returncode == 0 and len(parts) == 2 else None


SYSTEM_PY_VERSION = system_python_version()


# --- H-1: Python 3.9 ----------------------------------------------------------------
class PythonVersionTests(StateTestCase):
    @unittest.skipUnless(SYSTEM_PY_VERSION and SYSTEM_PY_VERSION < (3, 10),
                         "needs /usr/bin/python3 older than 3.10")
    def test_H1_both_modes_run_on_system_python(self):
        self.init_repo({"a.txt": "a\n"})
        self.write("a.txt", "b\n")
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [str(SYSTEM_PY), str(SCRIPT), str(self.repo), *args],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env=self.env(), cwd=str(self.tmp),
                )
                both = self.combined(proc)
                self.assertNotIn("Traceback", both)
                if SYSTEM_PY_VERSION < (3, 9):
                    self.assertEqual(proc.returncode, 2, both)
                    continue
                self.assertEqual(proc.returncode, 0, both)
                files = self.lines_with(proc.stdout, "status file: ")
                self.assertEqual(len(files), 1, both)
                self.assertTrue(Path(files[0][len("status file: "):]).is_file(), both)
                if args:
                    self.assertEqual(len(self.lines_with(proc.stdout, "handoff check: exit 0")), 1, both)

    def test_H1_python_older_than_3_9_exits_2_with_message(self):
        err = StringIO()
        with mock.patch.object(sys, "argv", [str(SCRIPT), str(self.repo)]), \
                mock.patch.object(sys, "version_info", (3, 8, 0)), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("3.9", err.getvalue())


# --- H-5: project key and project-filtered candidates --------------------------------
class ProjectKeyTests(StateTestCase):
    OTHER = "/elsewhere/other-proj"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})

    def key_lines(self, proc):
        return self.lines_with(proc.stdout, "project key:")

    def test_H5_project_key_is_toplevel_in_both_modes(self):
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                self.assertEqual(self.key_lines(self.run_state(*args)), [f"project key: {self.repo.as_posix()}"])

    def test_H5_project_key_of_linked_worktree_is_main_worktree(self):
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(wt))
        proc = self.run_state(cwd=wt)
        self.assertEqual(self.key_lines(proc), [f"project key: {self.repo.as_posix()}"], proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "toplevel:"), [f"toplevel: {wt}"], proc.stdout)

    def test_H5_project_key_outside_git_is_cwd(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                proc = self.run_state(*args, cwd=plain)
                self.assertEqual(self.key_lines(proc), [f"project key: {plain.as_posix()}"], proc.stdout)

    def test_H5_newest_other_project_handoff_is_not_chosen(self):
        self.write_handoff("2026-11-01-mine-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        self.write_handoff("2026-11-02-theirs-session.tmp", self.valid_handoff_text(toplevel=self.OTHER),
                           mtime=1_800_000_000)
        proc = self.run_state("--resume")
        found = self.lines_with(proc.stdout, "handoff: ")
        self.assertEqual(len(found), 1, proc.stdout)
        self.assertTrue(found[0].endswith("2026-11-01-mine-session.tmp"), proc.stdout)

    def test_H5_slug_selector_is_filtered_by_project(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        self.write_handoff("2026-11-02-work-session.tmp", self.valid_handoff_text(toplevel=self.OTHER),
                           mtime=1_800_000_000)
        found = self.lines_with(self.run_state("--resume", "work").stdout, "handoff: ")
        self.assertTrue(found[0].endswith("2026-11-01-work-session.tmp"), found)

    def test_H5_only_other_project_handoffs_is_none_found_with_warning(self):
        self.write_handoff("2026-11-02-theirs-session.tmp", self.valid_handoff_text(toplevel=self.OTHER))
        proc = self.run_state("--resume", "--c")
        self.assertEqual(proc.returncode, 1, self.combined(proc))
        found = self.lines_with(proc.stdout, "handoff: ")
        self.assertEqual(len(found), 1, proc.stdout)
        self.assertTrue(found[0].startswith("handoff: none found for this project"), proc.stdout)
        warns = self.lines_with(self.combined(proc), "WARNING: newest handoff belongs to another project:")
        self.assertEqual(len(warns), 1, self.combined(proc))
        self.assertIn(self.OTHER, warns[0])
        self.assertEqual(self.lines_with(proc.stdout, "handoff check:"), [], proc.stdout)

    def test_H5_legacy_project_line_with_matching_path_is_accepted(self):
        text = self.valid_handoff_text().replace(
            f"**Project:** sample\n**Toplevel:** {self.repo.as_posix()}\n",
            f"**Project:** {self.repo.as_posix()} (legacy note)\n")
        self.assertNotIn("**Toplevel:**", text)
        self.write_handoff("2026-11-01-legacy-session.tmp", text)
        found = self.lines_with(self.run_state("--resume").stdout, "handoff: ")
        self.assertTrue(found[0].endswith("2026-11-01-legacy-session.tmp"), found)

    def test_H5_legacy_project_line_with_tilde_is_expanded(self):
        repo2 = self.home / "work"
        repo2.mkdir()
        self.git("init", "-q", "-b", "main", repo=repo2)
        text = self.valid_handoff_text().replace(
            f"**Project:** sample\n**Toplevel:** {self.repo.as_posix()}\n", "**Project:** ~/work + notes\n")
        self.write_handoff("2026-11-01-tilde-session.tmp", text)
        found = self.lines_with(self.run_state("--resume", cwd=repo2).stdout, "handoff: ")
        self.assertTrue(found[0].endswith("2026-11-01-tilde-session.tmp"), found)

    def test_H5_explicit_path_of_other_project_forces_continue_no(self):
        theirs = self.write_handoff("2026-11-02-theirs-session.tmp", self.valid_handoff_text(toplevel=self.OTHER))
        proc = self.run_state("--resume", str(theirs), "--c")
        self.assertTrue(self.lines_with(proc.stdout, "handoff: ")[0].endswith(theirs.name), proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: no (project mismatch)"], proc.stdout)
        warns = self.lines_with(self.combined(proc), "WARNING: handoff project differs:")
        self.assertEqual(len(warns), 1, self.combined(proc))
        self.assertIn(self.OTHER, warns[0])

    def test_H5_explicit_path_of_this_project_keeps_continue_yes(self):
        mine = self.write_handoff("2026-11-02-mine-session.tmp", self.valid_handoff_text())
        proc = self.run_state("--resume", str(mine), "--c")
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: yes (--c)"], proc.stdout)
        self.assertEqual(self.lines_with(self.combined(proc), "WARNING: handoff project differs:"), [])


# --- M-3: user rules layer outside git --------------------------------------------
class UserRulesOutsideGitTests(StateTestCase):
    def test_M3_user_rules_line_outside_git_in_both_modes(self):
        rules = self.home / ".claude" / "handoff-rules.local.md"
        rules.write_text("# mine\n", encoding="utf-8", newline="\n")
        plain = self.tmp / "plain"
        plain.mkdir()
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                out = self.run_state(*args, cwd=plain).stdout.splitlines()
                found = [ln for ln in out if ln.startswith("user rules file:")]
                self.assertEqual(len(found), 1, out)
                self.assertEqual(Path(found[0][len("user rules file:"):].strip()).resolve(), rules.resolve())
                top = next(i for i, ln in enumerate(out) if ln.startswith("toplevel:"))
                self.assertLess(out.index(found[0]), top, "user rules line must come before the git check")


# --- M-4: status file name carries a project hash ------------------------------------
class StatusFileNameTests(StateTestCase):
    def test_M4_same_folder_name_in_two_places_gets_two_status_files(self):
        paths = []
        for parent in ("a", "b"):
            repo = self.tmp / parent / "api"
            repo.mkdir(parents=True)
            self.git("init", "-q", "-b", "main", repo=repo)
            (repo / f"only_in_{parent}.txt").write_text("x\n", encoding="utf-8")
            out = self.run_state(cwd=repo).stdout
            found = self.lines_with(out, "status file: ")
            self.assertEqual(len(found), 1, out)
            path = Path(found[0][len("status file: "):])
            self.assertEqual(path.name, self.project_status_name("api", repo.as_posix()))
            paths.append(path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertIn("only_in_a.txt", paths[0].read_text(encoding="utf-8"))
        self.assertIn("only_in_b.txt", paths[1].read_text(encoding="utf-8"))


    def test_L15_two_linked_worktrees_with_one_folder_name_get_two_status_files(self):
        self.init_repo({"a.txt": "a\n"})
        paths = []
        for parent in ("x", "y"):
            wt = self.tmp / parent / "wt"
            wt.parent.mkdir()
            self.git("worktree", "add", "-q", "-b", f"feat-{parent}", str(wt))
            (wt / f"only_in_{parent}.txt").write_text("x\n", encoding="utf-8")
            found = self.lines_with(self.run_state(cwd=wt).stdout, "status file: ")
            self.assertEqual(len(found), 1)
            path = Path(found[0][len("status file: "):])
            self.assertEqual(path.name, self.project_status_name("wt", wt.as_posix()))
            paths.append(path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertIn("only_in_x.txt", paths[0].read_text(encoding="utf-8"))
        self.assertIn("only_in_y.txt", paths[1].read_text(encoding="utf-8"))


# --- M-5: checker crash with exit 1, and the --git argument ---------------------------
class CheckerContractTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FIXTURES / "fake_checker.py"

    def run_fake(self, mode, cwd=None):
        return self.run_state("--resume", cwd=cwd,
                              extra_env={"HANDOFF_CHECKER": str(self.fake), "FAKE_CHECKER_MODE": mode})

    def test_M5_checker_exit_1_without_fail_line_is_a_checker_error(self):
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_fake("crash1")
        both = self.combined(proc)
        self.assertEqual(len(self.lines_with(both, "WARNING: checker")), 1, both)
        self.assertIn("fake checker bug", both)
        self.assertEqual(proc.returncode, 3, both)

    def test_M5_checker_exit_1_with_fail_line_is_a_finding(self):
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_fake("exit1")
        self.assertEqual(self.lines_with(self.combined(proc), "WARNING: checker"), [])
        self.assertEqual(self.lines_with(proc.stdout, "handoff check:"), ["handoff check: exit 1"], proc.stdout)
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_M2_checker_gets_git_yes_inside_a_repository(self):
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.assertIn("--git yes", self.run_fake("argv").stdout)

    def test_M2_checker_gets_git_no_outside_a_repository(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(toplevel=plain.as_posix()))
        self.assertIn("--git no", self.run_fake("argv", cwd=plain).stdout)


# --- M-11, L-7: argument words ----------------------------------------------------------
class ArgumentWordTests(StateTestCase):
    IGNORED = "WARNING: ignored argument words:"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())

    def test_M11_continue_with_extra_words_resumes_newest(self):
        for args in (("계속 진행",), ("계속", "진행"), ("--c", "please")):
            with self.subTest(args=args):
                proc = self.run_state("--resume", *args)
                self.assertRegex(self.lines_with(proc.stdout, "continue:")[0], r"^continue: yes \(")
                found = self.lines_with(proc.stdout, "handoff: ")
                self.assertTrue(found[0].endswith("2026-11-01-work-session.tmp"), proc.stdout)
                warns = self.lines_with(self.combined(proc), self.IGNORED)
                self.assertEqual(len(warns), 1, self.combined(proc))
                self.assertIn(args[-1].split()[-1], warns[0])
                self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_M11_matching_slug_is_kept_as_selector(self):
        proc = self.run_state("--resume", "work", "--c")
        self.assertTrue(self.lines_with(proc.stdout, "handoff: ")[0].endswith("2026-11-01-work-session.tmp"))
        self.assertEqual(self.lines_with(self.combined(proc), self.IGNORED), [])

    def test_L7_path_argument_keeps_its_spaces(self):
        folder = self.tmp / "picked"
        folder.mkdir()
        exact = folder / "my  handoff.tmp"
        exact.write_text(self.valid_handoff_text(), encoding="utf-8", newline="\n")
        (folder / "my handoff.tmp").write_text(self.valid_handoff_text(), encoding="utf-8", newline="\n")
        proc = self.run_state("--resume", str(exact))
        self.assertEqual(self.lines_with(proc.stdout, "handoff: "), [f"handoff: {exact}"], proc.stdout)


# --- L-2: unfilled template --------------------------------------------------------------
class TemplateCandidateTests(StateTestCase):
    def test_L2_unfilled_template_is_not_a_candidate(self):
        self.init_repo({"a.txt": "a\n"})
        template = TEMPLATE.read_text(encoding="utf-8")
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        self.write_handoff("2026-11-02-template-session.tmp", template, mtime=1_800_000_000)
        found = self.lines_with(self.run_state("--resume").stdout, "handoff: ")
        self.assertTrue(found[0].endswith("2026-11-01-work-session.tmp"), found)

    def test_L2_template_with_a_filled_toplevel_is_not_a_candidate(self):
        self.init_repo({"a.txt": "a\n"})
        template = TEMPLATE.read_text(encoding="utf-8")
        filled = re.sub(r"^\*\*Toplevel:\*\* .*$", f"**Toplevel:** {self.repo.as_posix()}", template, flags=re.M)
        self.assertNotEqual(filled, template)
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        self.write_handoff("2026-11-02-template-session.tmp", filled, mtime=1_800_000_000)
        proc = self.run_state("--resume")
        found = self.lines_with(proc.stdout, "handoff: ")
        self.assertTrue(found[0].endswith("2026-11-01-work-session.tmp"), proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "handoff branch:"), ["handoff branch: main -> same"], proc.stdout)

    def test_L2_filled_sections_without_a_next_step_are_not_a_candidate(self):
        self.init_repo({"a.txt": "a\n"})
        text = self.valid_handoff_text().replace("Run the unit tests and read the first failure.", "<the next step>")
        self.write_handoff("2026-11-01-work-session.tmp", text)
        self.assertIn("none found", self.lines_with(self.run_state("--resume").stdout, "handoff: ")[0])

    def test_L2_placeholder_head_lines_are_not_content(self):
        self.init_repo({"a.txt": "a\n"})
        text = (f"# Session: 2026-11-01\n\n**Started:** [time]\n**Toplevel:** {self.repo.as_posix()}\n"
                "**Topic:** [one-line summary]\n\n## Exact Next Step\n\n[the next step]\n")
        self.write_handoff("2026-11-01-heads-session.tmp", text)
        proc = self.run_state("--resume")
        self.assertIn("none found", self.lines_with(proc.stdout, "handoff: ")[0], proc.stdout)


# --- F3: a newer handoff of this project without a usable Exact Next Step is reported ---------------
class NewerSkippedTests(StateTestCase):
    WARN = "WARNING: newer handoff skipped (no usable Exact Next Step):"
    SKIP = "WARNING: newer handoff skipped "
    OLD, NEW = 1_700_000_000, 1_800_000_000
    NEW_NAME = "2026-11-02-new-session.tmp"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})

    def broken_text(self, toplevel=None):
        return self.valid_handoff_text(toplevel=toplevel).replace(
            "Run the unit tests and read the first failure.", "[next step]")

    def pair(self, old="2026-11-01-old-session.tmp", new="2026-11-02-new-session.tmp"):
        old_path = self.write_handoff(old, self.valid_handoff_text(), mtime=self.OLD)
        self.write_handoff(new, self.broken_text(), mtime=self.NEW)
        return old_path

    def test_F3_broken_newer_file_is_reported_and_continue_is_no(self):
        old = self.pair()
        proc = self.run_state("--resume", "--c")
        out = self.combined(proc)
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, self.WARN), [f"{self.WARN} 2026-11-02-new-session.tmp"], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)
        self.assertEqual(proc.returncode, 0, out)

    def test_F3_continue_is_no_with_the_reason_even_without_a_continue_token(self):
        self.pair()
        out = self.combined(self.run_state("--resume"))
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)

    def test_F3_run_log_records_the_continue_reason(self):
        self.pair()
        self.run_state("--resume", "--c")
        recs = [json.loads(ln) for ln in (self.tmp / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        resume = [r for r in recs if r["mode"] == "resume"]
        self.assertEqual([r["continue"] for r in resume], ["no (newer handoff skipped)"])
        self.assertTrue(any(w.startswith(self.WARN) for w in resume[0]["warnings"]), resume[0])

    def test_F3_broken_file_of_another_project_is_not_reported(self):
        old = self.write_handoff("2026-11-01-old-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        other = (self.tmp / "other").as_posix()
        self.write_handoff("2026-11-02-new-session.tmp", self.broken_text(toplevel=other), mtime=self.NEW)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: yes (--c)"], out)

    def test_F3_explicit_path_is_not_affected(self):
        old = self.pair()
        out = self.combined(self.run_state("--resume", "--c", str(old)))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: yes (--c)"], out)

    def test_F3_only_a_broken_file_is_none_found_with_the_warning(self):
        self.write_handoff("2026-11-02-new-session.tmp", self.broken_text(), mtime=self.NEW)
        proc = self.run_state("--resume")
        out = self.combined(proc)
        self.assertEqual(self.lines_with(out, "handoff: "), ["handoff: none found for this project"], out)
        self.assertEqual(self.lines_with(out, self.WARN), [f"{self.WARN} 2026-11-02-new-session.tmp"], out)
        self.assertEqual(proc.returncode, 1, out)

    def test_F3_warnings_are_capped_at_3_lines(self):
        self.write_handoff("2026-11-01-old-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        for i in range(5):
            self.write_handoff(f"2026-11-0{i + 2}-broken{i}-session.tmp", self.broken_text(), mtime=self.NEW + i)
        out = self.combined(self.run_state("--resume"))
        warns = self.lines_with(out, self.WARN)
        self.assertEqual(len(warns), 3, out)
        self.assertEqual(warns[0], f"{self.WARN} 2026-11-06-broken4-session.tmp", "newest first")
        self.assertEqual(warns[-1], f"{self.WARN} … and 3 more")

    def test_F3_broken_file_older_than_the_chosen_one_is_not_reported(self):
        self.write_handoff("2026-11-01-old-session.tmp", self.broken_text(), mtime=self.OLD)
        new = self.write_handoff("2026-11-02-new-session.tmp", self.valid_handoff_text(), mtime=self.NEW)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {new}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: yes (--c)"], out)

    def test_F3_slug_selector_reports_a_newer_broken_match(self):
        old = self.write_handoff("2026-11-01-alpha-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        self.write_handoff("2026-11-02-alpha2-session.tmp", self.broken_text(), mtime=self.NEW)
        out = self.combined(self.run_state("--resume", "--c", "alpha"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, self.WARN), [f"{self.WARN} 2026-11-02-alpha2-session.tmp"], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)

    def test_F3_date_selector_reports_a_newer_broken_match(self):
        old = self.write_handoff("2026-11-02-a-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        self.write_handoff("2026-11-02-b-session.tmp", self.broken_text(), mtime=self.NEW)
        out = self.combined(self.run_state("--resume", "--c", "2026-11-02"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, self.WARN), [f"{self.WARN} 2026-11-02-b-session.tmp"], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)

    def test_F3_undecodable_file_without_a_head_is_also_skipped_as_not_valid_utf8(self):
        # 18.3 ④: undecodable and its head (read with errors="replace") names no project
        old = self.write_handoff("2026-11-01-old-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        self.write_raw("2026-11-02-bad-session.tmp", b"\xff\xfe\xfd not utf-8\n" * 20, self.NEW)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: unreadable handoff skipped:"),
                         ["WARNING: unreadable handoff skipped: 2026-11-02-bad-session.tmp"], out)
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"),
                         [f"{self.SKIP}(not valid UTF-8): 2026-11-02-bad-session.tmp"], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)

    # --- 18.3 (M2): residual paths. Each case is checked newer than and as new as the chosen file. ---
    def write_raw(self, name, data, mtime):
        p = self.handoffs / name
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))
        return p

    def unknown_project_text(self):
        return self.valid_handoff_text().replace("**Project:** sample", "**Project:** [project name]").replace(
            f"**Toplevel:** {self.repo.as_posix()}", "**Toplevel:** [absolute path]")

    def truncated_bytes(self, toplevel=None):
        """A write cut in the middle of a Korean character: the head survives, the tail is not UTF-8."""
        data = (self.valid_handoff_text(toplevel=toplevel) + "\n## Background\n한국어 본문\n").encode("utf-8")
        cut = data.index("한".encode("utf-8")) + 2
        return data[:cut]

    def assert_skipped_both_ways(self, reason, data, extra=()):
        """`data` (bytes) as the newer file: newer, then the same mtime as the chosen file."""
        for when in (self.NEW, self.OLD):
            with self.subTest(mtime="newer" if when == self.NEW else "same"):
                for p in self.handoffs.glob("*-session.tmp"):
                    p.unlink()
                old = self.write_handoff("2026-11-01-old-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
                self.write_raw(self.NEW_NAME, data, when)
                proc = self.run_state("--resume", "--c")
                out = self.combined(proc)
                self.assertNotIn("Traceback", out)
                self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
                self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"),
                                 [f"{self.SKIP}({reason}): {self.NEW_NAME}"], out)
                self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)
                for line in extra:
                    self.assertIn(line, out.splitlines(), out)
                self.assertEqual(proc.returncode, 0, out)

    def test_F3_empty_newer_file_is_skipped(self):
        self.assert_skipped_both_ways("empty file", b"")

    def test_F3_newer_file_with_placeholder_toplevel_and_project_is_skipped(self):
        text = self.unknown_project_text()
        self.assertIsNone(re.search(r"^\*\*(Project|Toplevel):\*\* [^\[]", text, re.M), text)
        broken = text.replace("Run the unit tests and read the first failure.", "[next step]")
        self.assert_skipped_both_ways("project unknown", broken.encode("utf-8"))

    def test_F3_newer_file_of_unknown_project_is_skipped_even_with_a_next_step(self):
        self.assert_skipped_both_ways("project unknown", self.unknown_project_text().encode("utf-8"))

    def test_F3_newer_file_cut_inside_a_utf8_character_is_skipped(self):
        data = self.truncated_bytes()
        with self.assertRaises(UnicodeDecodeError):
            data.decode("utf-8")
        self.assert_skipped_both_ways("not valid UTF-8", data,
                                      extra=[f"WARNING: unreadable handoff skipped: {self.NEW_NAME}"])

    def test_F3_file_without_a_next_step_as_new_as_the_chosen_one_is_skipped(self):
        # catches `>=` written back as `>`: the old suite had no equal-mtime case
        self.assert_skipped_both_ways("no usable Exact Next Step", self.broken_text().encode("utf-8"))

    def test_F3_undecodable_file_of_another_project_keeps_only_its_unreadable_warning(self):
        old = self.write_handoff("2026-11-01-old-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        self.write_raw(self.NEW_NAME, self.truncated_bytes(toplevel=(self.tmp / "other").as_posix()), self.NEW)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {old}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: unreadable handoff skipped:"),
                         [f"WARNING: unreadable handoff skipped: {self.NEW_NAME}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: yes (--c)"], out)

    def test_F3_empty_file_older_than_the_chosen_one_is_not_reported(self):
        self.write_raw("2026-11-01-empty-session.tmp", b"", self.OLD - 1)
        new = self.write_handoff("2026-11-02-new-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "handoff: "), [f"handoff: {new}"], out)
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: yes (--c)"], out)

    def test_F3_valid_file_as_new_as_the_chosen_one_is_not_reported(self):
        self.write_handoff("2026-11-01-a-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        self.write_handoff("2026-11-01-b-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: yes (--c)"], out)

    def test_F3_mixed_reasons_count_as_one_kind_capped_at_3_lines(self):
        self.write_handoff("2026-11-01-old-session.tmp", self.valid_handoff_text(), mtime=self.OLD)
        self.write_raw("2026-11-02-a-session.tmp", b"", self.NEW + 3)
        self.write_raw("2026-11-02-b-session.tmp", self.unknown_project_text().encode("utf-8"), self.NEW + 2)
        self.write_raw("2026-11-02-c-session.tmp", self.truncated_bytes(), self.NEW + 1)
        self.write_raw("2026-11-02-d-session.tmp", self.broken_text().encode("utf-8"), self.NEW)
        out = self.combined(self.run_state("--resume", "--c"))
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [
            f"{self.SKIP}(empty file): 2026-11-02-a-session.tmp",
            f"{self.SKIP}(project unknown): 2026-11-02-b-session.tmp",
            f"{self.SKIP}(not valid UTF-8, no usable Exact Next Step): … and 2 more",
        ], out)
        self.assertEqual(self.lines_with(out, "continue:"), ["continue: no (newer handoff skipped)"], out)

    def test_F3_default_mode_prints_no_skipped_warning(self):
        old = self.pair()
        out = self.combined(self.run_state())
        self.assertEqual(self.lines_with(out, "WARNING: newer handoff skipped"), [], out)
        self.assertEqual(self.lines_with(out, "previous handoff:"), [f"previous handoff: {old}"], out)


# --- F2/17.3: resume mode hides the checker's size warning (a resume cannot shrink the file) ----------
class SizeWarningHiddenTests(StateTestCase):
    SIZE = "WARNING: handoff file is"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.fake = FIXTURES / "fake_checker.py"

    def resume_with_fake(self, mode):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        return self.run_state("--resume", extra_env={"HANDOFF_CHECKER": str(self.fake), "FAKE_CHECKER_MODE": mode})

    def test_F2_size_warning_is_not_shown_in_resume_output(self):
        out = self.combined(self.resume_with_fake("sizewarn"))
        self.assertNotIn(self.SIZE, out)
        self.assertIn("  [PASS] all checks passed", out)
        self.assertEqual(self.lines_with(out, "handoff check:"), ["handoff check: exit 0"], out)

    def test_F2_size_warning_does_not_use_up_the_excerpt_cap(self):
        out = self.combined(self.resume_with_fake("sizewarn_cap"))
        self.assertNotIn(self.SIZE, out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("  ")]), 8, out)
        self.assertNotIn("more", out)

    def test_F2_checker_run_record_keeps_the_size_warning(self):
        text = self.valid_handoff_text() + "\n" + "padding line that only adds size\n" * 800
        self.assertGreater(len(text.encode("utf-8")), 24 * 1024)
        self.write_handoff("2026-11-01-work-session.tmp", text)
        proc = self.run_state("--resume")
        self.assertNotIn(self.SIZE, self.combined(proc))
        recs = [json.loads(ln) for ln in (self.tmp / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        check = [r for r in recs if r["mode"] == "check"]
        self.assertEqual(len(check), 1, recs)
        self.assertTrue(any(ln.startswith(self.SIZE) for ln in check[0]["stdout"]), check[0])
        resume = [r for r in recs if r["mode"] == "resume"]
        self.assertFalse(any(self.SIZE in ln for ln in resume[0]["stdout"]), resume[0])


# --- L-9: exit codes and branch name ----------------------------------------------------
class ExitCodeTests(StateTestCase):
    NON_GIT_HANDOFF = (
        "# Session: 2026-11-01\n\n**Project:** plain\n**Toplevel:** @@TOPLEVEL@@\n**Continues:** none\n\n"
        "## Session State\n\n- **Branch:** n/a\n- **HEAD:** n/a\n- **Uncommitted changes:** n/a\n"
        "- **Unpushed commits:** n/a\n- **Lockfile changed:** n/a\n- **Live subagents:** none\n"
        "- **Live runners:** none\n- **Live scheduled:** none\n\n"
        "## Goal (carried)\n\n- **Origin:** /home/u/.claude/handoffs/2026-11-01-plain-session.tmp · 2026-11-01\n"
        "- **Goal:** Check the plain folder. User: \"check it\"\n"
        "- **Done-when:** the check passes\n- **Constraints:** none\n- **Changes:** none\n\n"
        "## Decisions Made\n\nNo major decisions made this session.\n\n"
        "## Exact Next Step\n\nRun the unit tests.\n"
    )

    def test_L9_outside_git_resume_found_and_passing_exits_0(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.write_handoff("2026-11-01-plain-session.tmp",
                           self.NON_GIT_HANDOFF.replace("@@TOPLEVEL@@", plain.as_posix()))
        proc = self.run_state("--resume", cwd=plain)
        self.assertEqual(self.lines_with(proc.stdout, "handoff check:"), ["handoff check: exit 0"], proc.stdout)
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_HC_outside_git_handoff_without_session_state_fails_the_check(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        text = self.NON_GIT_HANDOFF.replace("@@TOPLEVEL@@", plain.as_posix())
        start, end = text.index("## Session State"), text.index("## Goal (carried)")
        self.write_handoff("2026-11-01-plain-session.tmp", text[:start] + text[end:])
        proc = self.run_state("--resume", cwd=plain)
        self.assertEqual(self.lines_with(proc.stdout, "handoff check:"), ["handoff check: exit 1"], proc.stdout)
        self.assertIn("Session State", proc.stdout)

    def test_HC_na_branch_is_not_recorded(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.write_handoff("2026-11-01-plain-session.tmp",
                           self.NON_GIT_HANDOFF.replace("@@TOPLEVEL@@", plain.as_posix()))
        proc = self.run_state("--resume", cwd=plain)
        self.assertEqual(self.lines_with(proc.stdout, "handoff branch:"), ["handoff branch: not recorded"], proc.stdout)

    def test_L9_outside_git_default_mode_exits_0(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        proc = self.run_state(cwd=plain)
        self.assertTrue(self.lines_with(proc.stdout, "toplevel: not a git repository"), proc.stdout)
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_L9_branch_without_commits_is_named(self):
        self.git("init", "-q", "-b", "main")
        self.assertEqual(self.lines_with(self.run_state().stdout, "branch: "), ["branch: main"])


# --- L-10: warnings capped per kind -------------------------------------------------------
class WarningCapTests(StateTestCase):
    def test_L10_rules_section_warnings_capped_at_3_lines(self):
        self.init_repo({"a.txt": "a\n"})
        body = "# rules\n\n" + "".join(f"## note-{i}\ntext\n\n" for i in range(6))
        self.write(".claude/handoff-rules.local.md", body)
        proc = self.run_state()
        warns = self.lines_with(proc.stdout, "WARNING: rules file section outside override points:")
        self.assertEqual(len(warns), 3, proc.stdout)
        self.assertIn("and 4 more", warns[-1])

    def test_L10_unreadable_handoff_warnings_capped_at_3_lines(self):
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-good-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        for i in range(5):
            (self.handoffs / f"2026-11-0{i + 2}-bad{i}-session.tmp").write_bytes(b"\xff\xfe not utf-8\n")
        proc = self.run_state("--resume")
        warns = self.lines_with(self.combined(proc), "WARNING: unreadable handoff skipped:")
        self.assertEqual(len(warns), 3, self.combined(proc))
        self.assertIn("and 3 more", warns[-1])


# --- A12: run log --------------------------------------------------------------------------
class RunLogTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.log = self.tmp / "runs.jsonl"

    def records(self):
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]

    def test_A12_default_mode_appends_one_record(self):
        proc = self.run_state()
        recs = self.records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual((rec["script"], rec["mode"], rec["exit"]), ("handoff_state.py", "handoff", 0))
        self.assertEqual(rec["stdout"], proc.stdout.splitlines())
        self.assertEqual(rec["args"], [str(self.repo)])
        self.assertEqual(Path(rec["toplevel"]).resolve(), self.repo)
        self.assertIsNone(rec["handoff"])
        self.assertIsNone(rec["continue"])
        self.assertIsNone(rec["checker"])

    def test_A12_resume_records_handoff_continue_and_checker(self):
        path = self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_state("--resume", "--c")
        recs = self.records()
        self.assertEqual([r["mode"] for r in recs], ["check", "resume"], recs)
        check, rec = recs
        self.assertEqual(rec["handoff"], str(path))
        self.assertEqual(rec["continue"], "yes (--c)")
        self.assertEqual(rec["checker"], 0)
        self.assertEqual(rec["exit"], proc.returncode)
        lines = proc.stdout.splitlines()
        self.assertEqual(rec["stdout"], [ln for ln in lines if not ln.startswith(" ")])
        self.assertEqual(rec["omitted_lines"], sum(ln.startswith(" ") for ln in lines))
        self.assertGreater(rec["omitted_lines"], 0, "the checker result line is indented")
        self.assertEqual(check["handoff"], str(path))

    def test_M3_handoff_body_excerpts_stay_out_of_the_log(self):
        items = "".join(f"- SECRET-ITEM-{i} body text that only the handoff may hold, nothing else\n" for i in range(5))
        text = self.valid_handoff_text().replace("## Requirement Changes", f"## What Has NOT Been Tried Yet\n{items}\n## Requirement Changes")
        self.write_handoff("2026-11-01-work-session.tmp", text)
        proc = self.run_state("--resume")
        self.assertIn("SECRET-ITEM-0", proc.stdout, "the checker reports the failing items on screen")
        log = self.log.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-ITEM", log)
        self.assertEqual([r["checker"] for r in self.records() if r["mode"] == "resume"], [1])

    def test_A12_unwritable_log_warns_on_stderr_and_keeps_exit(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("file", encoding="utf-8")
        proc = self.run_state(extra_env={"HANDOFF_LOG": str(blocker / "x" / "runs.jsonl")})
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(len(self.lines_with(proc.stderr, "WARNING: run log not written:")), 1, proc.stderr)
        self.assertNotIn("run log", proc.stdout)

    def test_M3_token_in_live_item_name_stays_out_of_the_log(self):
        token = "gh" + "p_" + "Ab3" * 12  # built here, so no complete token sits in any file
        item = "\n".join([f"  - name: worker-{token} (h7)", "    does: reviews the diff",
                          "    decision: Use the sample approach", "    result: /home/u/out/review.md",
                          "    done-when: the transcript ends with end_turn", "    after-return: fold the findings in"])
        text = self.valid_handoff_text().replace("- **Live subagents:** none", "- **Live subagents:**\n" + item)
        self.write_handoff("2026-11-01-work-session.tmp", text)
        proc = self.run_state("--resume")
        self.assertIn(f"live subagent: worker-{token} (h7)", proc.stdout, "the briefing is not redacted")
        self.assertEqual(self.log.read_text(encoding="utf-8").count(token), 0)
        (rec,) = [r for r in self.records() if r["mode"] == "resume"]
        self.assertIn("live subagent: worker-REDACTED (h7) -> lost (no transcript)", rec["stdout"])

    def test_M4_tee_splits_on_newline_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("handoff_state_tee", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tee = mod._Tee(StringIO())
        self.assertEqual(tee.lines(), [])
        tee.write("  a\u2028b\x85c\x0bd\x0ce\x1cf\x1dg\x1eh\n")
        tee.write("next")
        self.assertEqual(tee.lines(), ["  a\u2028b\x85c\x0bd\x0ce\x1cf\x1dg\x1eh", "next"])

    def test_A12_off_writes_nothing(self):
        self.run_state(extra_env={"HANDOFF_LOG": "off"})
        self.assertFalse(self.log.exists())
        self.assertFalse((self.home / ".claude" / "handoffs" / ".state" / "runs.jsonl").exists())


class BrokenCheckerRecordTests(StateTestCase):
    """N2: a check_handoff.py that cannot be imported still leaves a record, with no stdout line in it."""
    BASE_COMMIT = "c34a100e08ab4c4fc09f7ab3abaa31ae36281fad"

    def sources(self):
        """{label: {name: bytes}} of the base-commit and current handoff_state.py + run_log.py."""
        out = {"base": {}, "new": {}}
        for name in ("handoff_state.py", "run_log.py"):
            out["new"][name] = (ROOT / "scripts" / name).read_bytes()
            out["base"][name] = (BASE_SNAPSHOTS / self.BASE_COMMIT[:7] / name).read_bytes()
        out["new"]["rules_layer.py"] = (ROOT / "scripts" / "rules_layer.py").read_bytes()  # imported since WU6
        out["new"]["devenv_state.py"] = (ROOT / "scripts" / "devenv_state.py").read_bytes()  # project key, lean line
        return out

    def test_N2_unloadable_checker_still_writes_an_empty_record(self):
        token = "gh" + "p_" + "Ab3" * 12  # built here, so no complete token sits in any file
        self.init_repo({"a.txt": "a\n"})
        item = "\n".join([f"  - name: worker-{token} (h7)", "    does: reviews the diff",
                          "    decision: Use the sample approach", "    result: /home/u/out/review.md",
                          "    done-when: the transcript ends with end_turn", "    after-return: fold the findings in"])
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text().replace(
            "- **Live subagents:** none", "- **Live subagents:**\n" + item))
        sources = self.sources()
        folder = self.tmp / "scripts"  # one folder for both versions: the checker's traceback names its path
        folder.mkdir()
        (folder / "check_handoff.py").write_text('raise ImportError("broken install")\n', encoding="utf-8")
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                runs, logs = {}, {}
                for label, files in sources.items():
                    for name, data in files.items():
                        (folder / name).write_bytes(data)
                    logs[label] = self.tmp / f"{label}-{len(args)}.jsonl"
                    runs[label] = subprocess.run(
                        [sys.executable, str(folder / "handoff_state.py"), str(self.repo), *args],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env=self.env(HANDOFF_LOG=str(logs[label])), cwd=str(self.tmp))
                new, base = runs["new"], runs["base"]
                self.assertEqual((new.returncode, new.stdout), (base.returncode, base.stdout))
                self.assertIn("WARNING: placeholder check unavailable", new.stdout)
                recs = [json.loads(ln) for ln in logs["new"].read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(recs), 1, new.stderr)
                rec = recs[0]
                self.assertEqual((rec["script"], rec["exit"]), ("handoff_state.py", new.returncode))
                self.assertEqual((rec["stdout"], rec["warnings"], rec["omitted_lines"]), ([], [], 0))
                self.assertEqual(logs["new"].read_text(encoding="utf-8").count(token), 0)
                self.assertNotIn("run log not written", new.stderr)


# --- R7: run log signals (project key, caller marker on the checker subprocess) ---------------
class RunSignalTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.log = self.tmp / "runs.jsonl"

    def own_records(self):
        recs = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]
        return [r for r in recs if r["script"] == "handoff_state.py"]

    def key_of(self, proc):
        (line,) = self.lines_with(proc.stdout, "project key: ")
        return line[len("project key: "):]

    def test_R7_record_project_is_project_key(self):
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(wt))
        plain = self.tmp / "plain"
        plain.mkdir()
        for label, cwd in (("git", self.repo), ("linked worktree", wt), ("outside git", plain)):
            for args in ((), ("--resume",)):
                with self.subTest(where=label, args=args):
                    self.log.unlink(missing_ok=True)
                    proc = self.run_state(*args, cwd=cwd)
                    (rec,) = self.own_records()
                    self.assertEqual(rec["project"], self.key_of(proc), proc.stdout)

    def test_R7_checker_gets_caller_env(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_state("--resume", extra_env={
            "HANDOFF_CHECKER": str(FIXTURES / "fake_checker.py"), "FAKE_CHECKER_MODE": "env"})
        self.assertIn("[PASS] caller=handoff_state.py", proc.stdout, self.combined(proc))

    def test_R7_caller_marker_overrides_a_user_value_on_the_subprocess_only(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_state("--resume", extra_env={
            "HANDOFF_CHECKER": str(FIXTURES / "fake_checker.py"), "FAKE_CHECKER_MODE": "env",
            "HANDOFF_CALLER": "something-else"})
        self.assertIn("[PASS] caller=handoff_state.py", proc.stdout, self.combined(proc))
        (rec,) = [r for r in self.own_records() if r["mode"] == "resume"]
        self.assertIsNone(rec["caller"], "an unlisted value is never recorded")

    def test_R7_marker_is_not_set_on_the_current_process(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("handoff_state_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        handoff = self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        env = {k: v for k, v in os.environ.items() if k != "HANDOFF_CALLER"}
        env.update(HANDOFF_CHECKER=str(FIXTURES / "fake_checker.py"), FAKE_CHECKER_MODE="env")
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(StringIO()) as out:
            mod.run_checker(handoff, False)
            self.assertNotIn("HANDOFF_CALLER", os.environ)
        self.assertIn("[PASS] caller=handoff_state.py", out.getvalue())

    def test_R7_own_record_caller_null(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.run_state("--resume")
        recs = self.own_records()
        self.assertEqual([r["mode"] for r in recs], ["resume"])
        self.assertTrue(all("caller" in r and r["caller"] is None for r in recs), recs)

    def test_R7_real_checker_record_carries_the_caller(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        sid = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
        self.run_state("--resume", extra_env={"CLAUDE_CODE_SESSION_ID": sid})
        recs = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]
        check = [r for r in recs if r["script"] == "check_handoff.py"]
        self.assertEqual([r["caller"] for r in check], ["handoff_state.py"])
        self.assertEqual([r["session_id"] for r in recs], [sid] * len(recs))

    def test_R7_exported_caller_never_stamps_the_own_record(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                self.run_state(*args, extra_env={"HANDOFF_CALLER": "handoff_state.py"})
                (rec,) = self.own_records()
                self.assertIsNone(rec["caller"])

    def test_R7_fail_codes_and_bytes_null(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.run_state()
        self.run_state("--resume")
        recs = self.own_records()
        self.assertEqual(len(recs), 2)
        for rec in recs:
            with self.subTest(mode=rec["mode"]):
                self.assertIn("fail_codes", rec)
                self.assertIn("handoff_bytes", rec)
                self.assertIsNone(rec["fail_codes"])
                self.assertIsNone(rec["handoff_bytes"])


# --- 14.2: previous handoff and history file (default mode) ------------------------------------
class ChainLinesTests(StateTestCase):
    def history_name(self, folder, key):
        return f"{folder}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}.md"

    def one(self, proc, prefix):
        found = self.lines_with(proc.stdout, prefix)
        self.assertEqual(len(found), 1, proc.stdout)
        value = found[0][len(prefix):].strip()
        if prefix == "history file:":
            m = re.fullmatch(r"(.+) \((exists|new)\)", value)
            self.assertIsNotNone(m, f"history file line lacks (exists|new): {found[0]}")
            value = m.group(1)
        return value

    def test_L13_history_file_says_new_then_exists(self):
        self.init_repo({"a.txt": "a\n"})
        line = self.lines_with(self.run_state().stdout, "history file:")[0]
        self.assertTrue(line.endswith(" (new)"), line)
        path = Path(line[len("history file:"):-len(" (new)")].strip())
        path.parent.mkdir(parents=True)
        path.write_text("2026-11-01 · k · done · /x\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.lines_with(self.run_state().stdout, "history file:"), [f"history file: {path} (exists)"])

    def test_C2_no_handoff_prints_previous_none(self):
        self.init_repo({"a.txt": "a\n"})
        self.assertEqual(self.one(self.run_state(), "previous handoff:"), "none")

    def test_C2_previous_is_newest_handoff_of_this_project(self):
        self.init_repo({"a.txt": "a\n"})
        mine_old = self.write_handoff("2026-11-01-mine-session.tmp", self.valid_handoff_text(), mtime=1_700_000_000)
        mine_new = self.write_handoff("2026-11-02-mine2-session.tmp", self.valid_handoff_text(), mtime=1_750_000_000)
        self.write_handoff("2026-11-03-theirs-session.tmp", self.valid_handoff_text(toplevel="/elsewhere/other"),
                           mtime=1_800_000_000)
        self.assertEqual(Path(self.one(self.run_state(), "previous handoff:")).resolve(), mine_new.resolve())
        self.assertNotEqual(mine_old, mine_new)

    def test_C2_previous_outside_git_uses_cwd_as_project(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        mine = self.write_handoff("2026-11-01-plain-session.tmp", self.valid_handoff_text(toplevel=plain.as_posix()))
        self.assertEqual(Path(self.one(self.run_state(cwd=plain), "previous handoff:")).resolve(), mine.resolve())

    def test_C2_previous_is_not_printed_in_resume_mode(self):
        self.init_repo({"a.txt": "a\n"})
        self.write_handoff("2026-11-01-mine-session.tmp", self.valid_handoff_text())
        self.assertEqual(self.lines_with(self.run_state("--resume").stdout, "previous handoff:"), [])

    def test_C3_history_file_path_has_folder_and_project_hash(self):
        self.init_repo({"a.txt": "a\n"})
        expected = self.home / ".claude" / "handoffs" / "history" / self.history_name("proj", self.repo.as_posix())
        got = Path(self.one(self.run_state(), "history file:"))
        self.assertEqual(got.resolve(), expected.resolve())
        self.assertFalse(got.exists(), "the script prints the path only; the model appends to it")
        self.assertFalse(fnmatch(got.name, "*-session.tmp"))
        self.assertEqual(self.lines_with(self.run_state("--resume").stdout, "history file:"), [])

    def test_C3_history_file_outside_git_is_keyed_by_cwd(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        expected = self.home / ".claude" / "handoffs" / "history" / self.history_name("plain", plain.as_posix())
        self.assertEqual(Path(self.one(self.run_state(cwd=plain), "history file:")).resolve(), expected.resolve())

    def test_C3_history_file_of_linked_worktree_is_the_main_worktrees(self):
        self.init_repo({"a.txt": "a\n"})
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(wt))
        expected = self.home / ".claude" / "handoffs" / "history" / self.history_name("proj", self.repo.as_posix())
        self.assertEqual(Path(self.one(self.run_state(cwd=wt), "history file:")).resolve(), expected.resolve())

    def test_C2_default_mode_block_stays_within_15_lines_with_a_previous_handoff(self):
        self.init_repo({"a.txt": "a\n"})
        self.write("a.txt", "b\n")
        self.write("new_untracked_file.txt", "x\n")
        self.write_handoff("2026-11-01-mine-session.tmp", self.valid_handoff_text())
        out = self.run_state().stdout
        self.assertLessEqual(len(out.splitlines()), 15, out)
        for name in ("a.txt", "new_untracked_file.txt"):
            self.assertNotIn(name, out)


# --- 14.4: head line and commits since the handoff ------------------------------------------------
class HeadAndSinceTests(StateTestCase):
    SINCE = "since handoff:"

    def commit_file(self, name):
        self.write(name, f"{name}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"add {name}")

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.base_sha = self.git("rev-parse", "HEAD")

    def resume(self, head):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text(head=head))
        return self.run_state("--resume")

    def test_C4_head_line_in_both_modes_is_the_short_sha(self):
        short = self.git("rev-parse", "--short", "HEAD")
        for args in ((), ("--resume",)):
            with self.subTest(args=args):
                self.assertEqual(self.lines_with(self.run_state(*args).stdout, "head:"), [f"head: {short}"])

    def test_C4_head_is_none_without_commits_and_outside_git(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        self.git("init", "-q", "-b", "main", repo=bare)
        plain = self.tmp / "plain"
        plain.mkdir()
        for where in (bare, plain):
            for args in ((), ("--resume",)):
                with self.subTest(where=where.name, args=args):
                    self.assertEqual(self.lines_with(self.run_state(*args, cwd=where).stdout, "head:"), ["head: none"])

    def test_C4_since_line_counts_commits_and_files(self):
        self.commit_file("b.txt")
        self.commit_file("c.txt")
        self.write("c.txt", "uncommitted edit\n")  # not counted: only commits since the handoff
        proc = self.resume(self.base_sha[:7])
        self.assertEqual(self.lines_with(proc.stdout, self.SINCE), ["since handoff: 2 commits · 2 files changed"], proc.stdout)
        self.assertEqual(self.lines_with(self.combined(proc), "WARNING: 2 commits since"), [])

    def test_C4_since_line_with_no_new_commit(self):
        proc = self.resume(self.base_sha[:7])
        self.assertEqual(self.lines_with(proc.stdout, self.SINCE), ["since handoff: 0 commits · 0 files changed"], proc.stdout)

    def test_C4_nine_commits_do_not_warn_ten_do(self):
        for i in range(9):
            self.commit_file(f"f{i}.txt")
        nine = self.resume(self.base_sha)
        self.assertEqual(self.lines_with(nine.stdout, self.SINCE), ["since handoff: 9 commits · 9 files changed"], nine.stdout)
        self.assertEqual(self.lines_with(self.combined(nine), "WARNING: 9 commits since"), [])
        self.commit_file("f9.txt")
        ten = self.resume(self.base_sha)
        self.assertEqual(self.lines_with(ten.stdout, self.SINCE), ["since handoff: 10 commits · 10 files changed"], ten.stdout)
        self.assertEqual(self.lines_with(self.combined(ten), "WARNING: 10 commits since the handoff"),
                         ["WARNING: 10 commits since the handoff"], ten.stdout)
        self.assertEqual(ten.returncode, 0, self.combined(ten))

    def test_C4_unknown_commit_is_reported(self):
        proc = self.resume("deadbeefdeadbeef")
        self.assertEqual(self.lines_with(proc.stdout, self.SINCE), ["since handoff: unknown (commit not found)"], proc.stdout)

    def test_C4_unsafe_head_value_is_unknown_not_a_git_option(self):
        proc = self.resume("--output=pwned.txt")
        self.assertEqual(self.lines_with(proc.stdout, self.SINCE), ["since handoff: unknown (commit not found)"], proc.stdout)
        self.assertFalse((self.repo / "pwned.txt").exists())
        self.assertFalse((self.tmp / "pwned.txt").exists())

    def test_C4_head_none_or_missing_prints_no_since_line(self):
        self.assertEqual(self.lines_with(self.resume("none").stdout, self.SINCE), [])
        text = self.valid_handoff_text().replace("- **HEAD:** none\n", "")
        self.assertNotIn("**HEAD:**", text)
        self.write_handoff("2026-11-01-work-session.tmp", text)
        self.assertEqual(self.lines_with(self.run_state("--resume").stdout, self.SINCE), [])

    def test_C4_head_with_trailing_note_uses_its_first_word(self):
        self.commit_file("b.txt")
        proc = self.resume(f"{self.base_sha[:7]} (before the refactor)")
        self.assertEqual(self.lines_with(proc.stdout, self.SINCE), ["since handoff: 1 commits · 1 files changed"], proc.stdout)

    def test_C4_since_is_not_printed_in_default_mode_or_outside_git(self):
        self.assertEqual(self.lines_with(self.run_state().stdout, self.SINCE), [])
        plain = self.tmp / "plain"
        plain.mkdir()
        self.write_handoff("2026-11-01-plain-session.tmp", self.valid_handoff_text(toplevel=plain.as_posix(), head="abcdef1"))
        self.assertEqual(self.lines_with(self.run_state("--resume", cwd=plain).stdout, self.SINCE), [])

    def test_C4_resume_block_without_a_handoff_stays_within_15_lines(self):
        for i in range(30):
            self.write(f"untracked_{i}.txt", "u\n")
        out = self.run_state("--resume").stdout
        self.assertLessEqual(len(out.splitlines()), 15, out)


# --- 16.1: subagent state judged from the newest transcript copy ------------------------------------
class LiveSubagentTests(StateTestCase):
    PREFIX = "live subagent:"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.now = time.time()

    @staticmethod
    def item(name, item_id, result="/home/u/out/review.md", expected_by=None):
        lines = [f"  - name: {name} ({item_id})" if item_id is not None else f"  - name: {name}",
                 "    does: reviews the diff", "    decision: Use the sample approach", f"    result: {result}",
                 "    done-when: the transcript ends with end_turn", "    after-return: fold the findings in"]
        if expected_by:
            lines.append(f"    expected-by: {expected_by}")
        return "\n".join(lines)

    def resume_with(self, *items):
        text = self.valid_handoff_text().replace("- **Live subagents:** none",
                                                 "- **Live subagents:**\n" + "\n".join(items))
        self.write_handoff("2026-11-01-work-session.tmp", text)
        return self.run_state("--resume")

    def transcript(self, item_id, stops, age_min, project="p1", session="s1", flat=False, path=None, pad=0):
        """A transcript copy whose assistant records end with `stops`, last modified `age_min` minutes ago."""
        if path is None:
            folder = self.home / ".claude" / "projects" / project
            path = (folder if flat else folder / session) / "subagents" / f"agent-{item_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        recs = [{"type": "user", "message": {"role": "user", "content": "BODY-TEXT-MARKER " + "y" * pad}}]
        for stop in stops:
            recs.append({"type": "assistant", "message": {"role": "assistant", "stop_reason": stop,
                                                          "content": [{"type": "text", "text": "BODY-TEXT-MARKER"}]}})
        path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8", newline="\n")
        mtime = self.now - age_min * 60 - 5
        os.utime(path, (mtime, mtime))
        return path

    def states(self, proc):
        return self.lines_with(proc.stdout, self.PREFIX)

    def test_HA_newest_copy_returned_wins_over_an_old_stuck_copy(self):
        self.transcript("a13b28", ["tool_use"], age_min=120, session="s1")
        self.transcript("a13b28", ["tool_use", "end_turn"], age_min=5, session="s2")
        proc = self.resume_with(self.item("review-high", "a13b28"))
        self.assertEqual(self.states(proc), ["live subagent: review-high (a13b28) -> returned (end_turn)"], proc.stdout)

    def test_HA_newest_copy_still_working_is_running_active(self):
        self.transcript("a13b28", ["end_turn"], age_min=120, session="s1")
        self.transcript("a13b28", ["end_turn", "tool_use"], age_min=2, session="s2")
        proc = self.resume_with(self.item("review-high", "a13b28"))
        self.assertEqual(self.states(proc), ["live subagent: review-high (a13b28) -> running? (active 2m ago)"], proc.stdout)

    def test_L6_other_final_stop_reasons_are_returned(self):
        for stop in ("stop_sequence", "max_tokens", "refusal"):
            with self.subTest(stop=stop):
                self.transcript("b1", ["tool_use", stop], age_min=200)
                proc = self.resume_with(self.item("worker", "b1"))
                self.assertEqual(self.states(proc), [f"live subagent: worker (b1) -> returned ({stop})"], proc.stdout)

    def test_HA_idle_stale_and_missing(self):
        for age, stops, expected in ((30, ["tool_use"], "running? (idle 30m)"),
                                     (90, ["tool_use"], "lost (stale 90m)"),
                                     (30, [], "running? (idle 30m)")):
            with self.subTest(age=age, stops=stops):
                self.transcript("c2", stops, age_min=age)
                proc = self.resume_with(self.item("worker", "c2"))
                self.assertEqual(self.states(proc), [f"live subagent: worker (c2) -> {expected}"], proc.stdout)
        proc = self.resume_with(self.item("ghost", "d3"))
        self.assertEqual(self.states(proc), ["live subagent: ghost (d3) -> lost (no transcript)"], proc.stdout)

    def test_HA_expected_by(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
        for age, when, expected in ((30, past, "lost (past expected-by)"),
                                    (30, future, "running? (idle 30m)"),
                                    (3, past, "running? (active 3m ago)"),
                                    (30, "after lunch", "running? (idle 30m)")):
            with self.subTest(age=age, when=when):
                self.transcript("e4", ["tool_use"], age_min=age)
                proc = self.resume_with(self.item("worker", "e4", expected_by=when))
                self.assertEqual(self.states(proc), [f"live subagent: worker (e4) -> {expected}"], proc.stdout)

    def test_HA_unusable_id_is_unknown(self):
        for name, item_id in (("worker", None), ("worker", "has space"), ("worker", "../x"), ("worker", "a*b")):
            with self.subTest(item_id=item_id):
                proc = self.resume_with(self.item(name, item_id))
                shown = f"{name} ({item_id})" if item_id is not None else name
                self.assertEqual(self.states(proc), [f"live subagent: {shown} -> unknown (id not usable)"], proc.stdout)

    def test_HA_result_jsonl_and_flat_layout_are_searched(self):
        elsewhere = self.transcript("f5", ["end_turn"], age_min=50, path=self.tmp / "kept" / "agent-f5.jsonl")
        proc = self.resume_with(self.item("worker", "f5", result=str(elsewhere)))
        self.assertEqual(self.states(proc), ["live subagent: worker (f5) -> returned (end_turn)"], proc.stdout)
        self.transcript("f5", ["tool_use"], age_min=1, flat=True)
        proc = self.resume_with(self.item("worker", "f5", result=str(elsewhere)))
        self.assertEqual(self.states(proc), ["live subagent: worker (f5) -> running? (active 1m ago)"], proc.stdout)

    def test_HA_only_the_file_tail_is_read_and_no_body_is_printed(self):
        self.transcript("g6", ["tool_use", "end_turn"], age_min=200, pad=400 * 1024)
        proc = self.resume_with(self.item("worker", "g6"))
        self.assertEqual(self.states(proc), ["live subagent: worker (g6) -> returned (end_turn)"], proc.stdout)
        self.assertNotIn("BODY-TEXT-MARKER", self.combined(proc))

    def test_HA_more_than_five_items_are_capped(self):
        items = [self.item(f"w{i}", f"id{i}") for i in range(7)]
        lines = self.states(self.resume_with(*items))
        self.assertEqual(len(lines), 5, lines)
        self.assertEqual(lines[0], "live subagent: w0 (id0) -> lost (no transcript)")
        self.assertEqual(lines[-1], "live subagent: … and 3 more")

    def test_HA_lines_sit_before_the_check_and_only_in_resume_mode(self):
        self.transcript("h7", ["end_turn"], age_min=5)
        proc = self.resume_with(self.item("worker", "h7"))
        out = proc.stdout.splitlines()
        live = next(i for i, ln in enumerate(out) if ln.startswith(self.PREFIX))
        check = next(i for i, ln in enumerate(out) if ln.startswith("handoff check:"))
        self.assertLess(live, check, proc.stdout)
        self.assertEqual(self.states(self.run_state()), [])
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.assertEqual(self.states(self.run_state("--resume")), [])


# --- 16.6: A5 with warnings present ---------------------------------------------------------------
class A5WithWarningsTests(StateTestCase):
    GIT_LINES = ("branch:", "upstream:", "status:", "status file:", "lockfile changed:", "diffstat:", "head:")

    def setUp(self):
        super().setUp()
        names = [f"changed_name_{i:02d}.txt" for i in range(40)]
        self.init_repo({"a.txt": "a\n"})
        for n in names:
            self.write(n, "x\n")
        self.names = names + ["a.txt"]
        self.write("a.txt", "b\n")
        self.write(".claude/handoff-rules.local.md", "".join(f"## note-{i}\ntext\n" for i in range(4)))
        for i in range(2):
            (self.handoffs / f"2026-11-0{i + 1}-bad{i}-session.tmp").write_bytes(b"\xff\xfe not utf-8\n")
        self.use_scripts_without_budget()
        budget = self.home / ".claude" / "scripts" / "context_budget.py"
        budget.parent.mkdir(parents=True)
        budget.write_text("print('budget 42%')\n", encoding="utf-8")

    def check(self, proc, whole_cap):
        out = proc.stdout.splitlines()
        git_lines = [ln for ln in out if ln.startswith(self.GIT_LINES)]
        self.assertLessEqual(len(git_lines), 15, proc.stdout)
        self.assertTrue(any(ln.startswith("WARNING:") for ln in out), proc.stdout)
        for n in self.names:
            self.assertNotIn(n, proc.stdout)
        if whole_cap:
            self.assertLessEqual(len(out), whole_cap, proc.stdout)

    def test_M4_default_mode_with_warnings(self):
        self.check(self.run_state(), whole_cap=25)

    def test_M4_resume_mode_with_warnings(self):
        self.check(self.run_state("--resume"), whole_cap=None)

    def test_AH27_default_cap_with_env(self):
        put_transcript(self.home, FULL)
        proc = self.run_state(extra_env={"CLAUDE_CODE_SESSION_ID": SID})
        self.assertEqual(len(self.lines_with(proc.stdout, "session id: ")), 1, proc.stdout)
        self.check(proc, whole_cap=27)  # spec 16.6: 25, plus the two session lines


# --- L-12: an unexpected error is exit 4 ------------------------------------------------------------
class InternalErrorTests(StateTestCase):
    def test_L12_unexpected_exception_exits_4_with_traceback_tail(self):
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('hs', {str(SCRIPT)!r})\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "def boom(*a, **k): raise RuntimeError('state bug here')\n"
            "mod.state_block = boom\n"
            f"sys.exit(mod.run([{str(self.repo)!r}]))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", env=self.env())
        self.assertEqual(proc.returncode, 4, self.combined(proc))
        tail = proc.stderr.strip().splitlines()
        self.assertLessEqual(len(tail), 5, proc.stderr)
        self.assertIn("RuntimeError: state bug here", tail[-1])
        rec = json.loads((self.tmp / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["exit"], 4)


# --- AH: session lines and the resume gate on messages after the last /handoff ----------------------
TRANSCRIPT = Path(__file__).resolve().parent / "fixtures" / "transcript" / "incident.jsonl"
SID = "00000000-0000-4000-8000-000000000001"  # the fixture's session id
M1 = "Move part C to the second round and ship A and B first."
M2 = "검토자에게는 A와 B 문서만 이슈 목록 없이 먼저 보낸다."
A1 = "A and B now, C later"  # the dialog answer of fixture line 33
M3 = "Also keep part D out of this round."  # typed after the handoff was checked
CUT_BEFORE, FULL = 25, 35  # fixture line counts: before M1·M2 are delivered, everything
GATE_BASE = "1c6bbeb"  # handoff_state.py and check_handoff.py before the after-handoff gate
UNRECORDED = "no (unrecorded messages after handoff)"
NOT_CHECKED = "WARNING: after-handoff messages not checked: "
BEFORE_TRANSCRIPT = Path(__file__).resolve().parent / "fixtures" / "transcript" / "before.jsonl"
BEFORE_OK = Path(__file__).resolve().parent / "fixtures" / "transcript" / "before_ok.md"
SID2 = "00000000-0000-4000-8000-000000000002"  # before.jsonl's session id
RELEASE_BASE = "0970c23"  # the release before the Before Handoff gate
BASE_SNAPSHOTS = ROOT / "tests" / "fixtures" / "base"  # scripts/ files of older commits, copied from the original repository


def put_transcript(home, count, extra=()):
    """The first `count` fixture lines plus `extra` lines as ~/.claude/projects/-tmp-demo/<SID>.jsonl."""
    lines = TRANSCRIPT.read_text(encoding="utf-8").splitlines()[:count] + list(extra)
    folder = home / ".claude" / "projects" / "-tmp-demo"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{SID}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def m3_line():
    return json.dumps({"type": "user", "timestamp": "2026-11-09T07:00:00.000Z", "cwd": "/tmp/demo", "gitBranch": "main",
                       "sessionId": SID, "origin": {"kind": "human"}, "message": {"role": "user", "content": M3}},
                      ensure_ascii=False)


def image_queued_line(text):
    """A queued message delivered as content blocks: the text, then a pasted image."""
    prompt = [{"type": "text", "text": text},
              {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}]
    return json.dumps({"type": "attachment", "timestamp": "2026-11-09T06:55:42.000Z", "sessionId": SID,
                       "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
                                      "origin": {"kind": "human"}, "timestamp": "2026-11-09T06:55:42.000Z"}},
                      ensure_ascii=False)


def base_scripts(folder, names=("handoff_state.py", "check_handoff.py", "run_log.py"), commit=GATE_BASE):
    """Write the `commit` copies of `names` from tests/fixtures/base into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes((BASE_SNAPSHOTS / commit / name).read_bytes())
    return folder


class SessionLineTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.use_scripts_without_budget()
        budget =self.home / ".claude" / "scripts" / "context_budget.py"
        budget.parent.mkdir(parents=True)
        budget.write_text("print('budget 42%')\n", encoding="utf-8")

    def assert_session_lines(self, proc, last_state_line, transcript):
        out = proc.stdout.splitlines()
        i = next(n for n, ln in enumerate(out) if ln.startswith(last_state_line))
        self.assertEqual(out[i + 1:i + 4], [f"session id: {SID}", f"transcript: {transcript}", "context-budget: budget 42%"],
                         proc.stdout)

    def test_AH20_default_session_lines(self):
        path = put_transcript(self.home, FULL)
        env = {"CLAUDE_CODE_SESSION_ID": SID}
        self.assert_session_lines(self.run_state(extra_env=env), "diffstat:", path)
        plain = self.tmp / "plain"
        plain.mkdir()
        proc = self.run_state(extra_env=env, cwd=plain)
        self.assertIn("toplevel: not a git repository", proc.stdout)
        self.assert_session_lines(proc, "head: none", path)
        path.unlink()
        self.assert_session_lines(self.run_state(extra_env=env), "diffstat:", "none (not found)")
        for value in (None, "not-a-uuid"):
            with self.subTest(env=value):
                proc = self.run_state(extra_env={"CLAUDE_CODE_SESSION_ID": value} if value else None)
                self.assertEqual(self.lines_with(proc.stdout, "session id:") + self.lines_with(proc.stdout, "transcript:"),
                                 [], proc.stdout)
        resume = self.run_state("--resume", extra_env=env)
        self.assertNotIn("session id:", resume.stdout, "the lines are /handoff Step 0 only")

    def test_AH20b_session_rule_is_run_log_rule(self):
        import importlib.util
        mods = {}
        for name, path in (("hs", SCRIPT), ("rl", ROOT / "scripts" / "run_log.py")):
            spec = importlib.util.spec_from_file_location(f"{name}_session_rule", path)
            mods[name] = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mods[name])
        self.assertEqual(mods["hs"].SESSION_ID.pattern, mods["rl"].SESSION_ID.pattern)


class ResumeGateTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.log = self.tmp / "runs.jsonl"

    def text(self, *quotes, session=SID, toplevel=None):
        text = self.valid_handoff_text(toplevel=toplevel).replace(
            "**Continues:** none", f"**Continues:** none\n**Session:** {session}", 1)
        if quotes:
            text += "\n## After Handoff\n\n" + "".join(f"- 「{q}」 → decision: unchanged\n" for q in quotes)
        return text

    def resume(self, *args, **kw):
        proc = self.run_state("--resume", *args, **kw)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertNotIn("Traceback", proc.stderr)
        return proc

    def continue_line(self, proc):
        (line,) = self.lines_with(proc.stdout, "continue:")
        return line

    def gate_lines(self, proc):
        """The after-handoff WARNING line, the indented lines right after it, and their place after `handoff:`."""
        out = proc.stdout.splitlines()
        i = next((n for n, ln in enumerate(out) if "after the last /handoff" in ln or ln.startswith(NOT_CHECKED)), None)
        self.assertIsNotNone(i, f"no after-handoff WARNING line:\n{proc.stdout}")
        j = i + 1
        while j < len(out) and out[j].startswith(("  「", "  … and ")):
            j += 1
        return out[i:j], out[:i]

    def test_AH21_resume_gate_overrides_continue(self):
        put_transcript(self.home, FULL, [m3_line()])
        self.write_handoff("2026-11-01-work-session.tmp", self.text(M1, M2, A1))
        proc = self.resume("--c")
        self.assertEqual(self.continue_line(proc), f"continue: {UNRECORDED}")
        block, before = self.gate_lines(proc)
        self.assertEqual(block, ["WARNING: 1 user message(s) after the last /handoff are not in the handoff file",
                                 f"  「{M3}」 (07:00:00Z)"], proc.stdout)
        self.assertTrue(before[-1].startswith("handoff: "), proc.stdout)
        self.assertIn("handoff check: exit 0", proc.stdout)

    def test_AH21b_resume_dialog_answer(self):
        put_transcript(self.home, FULL, [m3_line()])
        self.write_handoff("2026-11-01-work-session.tmp", self.text(M1, M2, M3))
        proc = self.resume("--c")
        self.assertEqual(self.continue_line(proc), f"continue: {UNRECORDED}")
        self.assertEqual(self.gate_lines(proc)[0][1:], [f"  「{A1}」 (06:56:30Z)"], proc.stdout)

    def test_AH21c_resume_queued_message_with_image(self):
        said = "Ship the screenshot fix first."
        put_transcript(self.home, CUT_BEFORE, [image_queued_line(said)])
        self.write_handoff("2026-11-01-work-session.tmp", self.text())
        proc = self.resume("--c")
        self.assertEqual(self.continue_line(proc), f"continue: {UNRECORDED}")
        self.assertEqual(self.gate_lines(proc)[0][1:], [f"  「{said}」 (06:55:42Z)"], proc.stdout)

    def test_AH22_resume_all_quoted(self):
        put_transcript(self.home, FULL, [m3_line()])
        self.write_handoff("2026-11-01-work-session.tmp", self.text(M1, M2, A1, M3))
        proc = self.resume("--c")
        self.assertEqual(self.continue_line(proc), "continue: yes (--c)")
        self.assertNotIn("after the last /handoff", proc.stdout)
        self.assertNotIn("after-handoff", proc.stdout)

    def test_AH23_resume_degrade(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.text(M1, M2, A1))
        proc = self.resume("--c")  # no transcript at all
        self.assertEqual(self.continue_line(proc), "continue: yes (--c)")
        self.assertEqual(self.gate_lines(proc)[0], [NOT_CHECKED + "transcript not found"], proc.stdout)
        put_transcript(self.home, FULL, [m3_line()])
        folder = base_scripts(self.tmp / "mixed", names=("check_handoff.py",))
        for name in ("handoff_state.py", "run_log.py", "rules_layer.py", "devenv_state.py"):
            (folder / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        proc = subprocess.run([sys.executable, str(folder / "handoff_state.py"), str(self.repo), "--resume", "--c"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.env(), cwd=str(self.tmp))
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.continue_line(proc), "continue: yes (--c)")
        self.assertEqual(self.gate_lines(proc)[0], [NOT_CHECKED + "checker unavailable"], proc.stdout)
        self.assertNotIn(M3, proc.stdout)

    def test_AH24_precedence_overlaps(self):
        put_transcript(self.home, FULL, [m3_line()])
        other = self.tmp / "other"
        other.mkdir()
        explicit = self.tmp / "2026-11-01-other-session.tmp"
        explicit.write_text(self.text(M1, M2, A1, toplevel=other.as_posix()), encoding="utf-8", newline="\n")
        proc = self.resume(str(explicit), "--c")
        self.assertEqual(self.continue_line(proc), "continue: no (project mismatch)")
        block, before = self.gate_lines(proc)
        self.assertEqual(block[1:], [f"  「{M3}」 (07:00:00Z)"], proc.stdout)
        self.assertTrue(before[-1].startswith("WARNING: handoff project differs: "), proc.stdout)
        self.assertTrue(before[-2].startswith("handoff: "), proc.stdout)

        self.write_handoff("2026-11-01-old-session.tmp", self.text(M1, M2, A1), mtime=1_700_000_000)
        self.write_handoff("2026-11-02-new-session.tmp", self.text(M1, M2, A1).replace(
            "Run the unit tests and read the first failure.", "[next step]"), mtime=1_800_000_000)
        proc = self.resume("--c")
        self.assertEqual(self.continue_line(proc), "continue: no (newer handoff skipped)")
        self.assertEqual(self.gate_lines(proc)[0][1:], [f"  「{M3}」 (07:00:00Z)"], proc.stdout)

        (self.handoffs / "2026-11-02-new-session.tmp").unlink()
        self.assertEqual(self.continue_line(self.resume("--c")), f"continue: {UNRECORDED}")
        self.assertEqual(self.continue_line(self.resume()), f"continue: {UNRECORDED}")

    def test_AH25_old_handoff_unchanged(self):
        self.use_scripts_without_budget()
        old = base_scripts(self.tmp / "old")
        put_transcript(self.home, FULL, [m3_line()])
        plain = self.valid_handoff_text()
        cases = {"no Session line": plain, "Session none": self.text(session="none"),
                 "Session not a UUID": self.text(session="abc")}
        for label, text in cases.items():
            for env in ({}, {"CLAUDE_CODE_SESSION_ID": SID}):
                with self.subTest(case=label, env=bool(env)):
                    self.write_handoff("2026-11-01-work-session.tmp", text)
                    runs = [subprocess.run([sys.executable, str(script), str(self.repo), "--resume", "--c"],
                                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                                           env=self.env(HANDOFF_LOG="off", **env), cwd=str(self.tmp))
                            for script in (old / "handoff_state.py", self.script)]
                    self.assertEqual((runs[1].returncode, runs[1].stdout), (runs[0].returncode, runs[0].stdout))
        default = [subprocess.run([sys.executable, str(script), str(self.repo)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=self.env(HANDOFF_LOG="off"), cwd=str(self.tmp))
                   for script in (old / "handoff_state.py", self.script)]
        self.assertEqual((default[1].returncode, default[1].stdout), (default[0].returncode, default[0].stdout))

    def test_AH25b_session_line_in_body_is_not_the_head(self):
        put_transcript(self.home, FULL, [m3_line()])
        self.write_handoff("2026-11-01-work-session.tmp",
                           self.valid_handoff_text() + f"\n## Notes\n\n**Session:** {SID}\n")
        proc = self.resume("--c")
        self.assertEqual(self.continue_line(proc), "continue: yes (--c)")
        self.assertNotIn("after the last /handoff", proc.stdout)
        self.assertNotIn("after-handoff", proc.stdout)

    def test_AH26_privacy(self):
        put_transcript(self.home, FULL, [m3_line()])
        self.write_handoff("2026-11-01-work-session.tmp", self.text(M1, M2, A1))
        proc = self.resume("--c", extra_env={"CLAUDE_CODE_SESSION_ID": "11111111-1111-4111-8111-111111111111"})
        self.assertIn(M3, proc.stdout)
        log = self.log.read_text(encoding="utf-8")
        self.assertEqual(log.count("part D out"), 0)
        recs = [json.loads(ln) for ln in log.splitlines()]
        (rec,) = [r for r in recs if r["script"] == "handoff_state.py"]
        self.assertEqual(rec["continue"], UNRECORDED)
        self.assertIn("WARNING: 1 user message(s) after the last /handoff are not in the handoff file", rec["warnings"])
        self.assertEqual({len(r) for r in recs}, {26})


class ResumeBeforeTests(StateTestCase):
    """The resume briefing's BEFORE HANDOFF block (new-format-spec section 21). Display only: `continue:` is
    decided by the after gate alone."""

    BEFORE_FIRST = "  「「Plan B」 is the one we keep.」 (07:00:30Z)"
    BEFORE_LAST = "  「record the pilot decision」 (07:05:00Z)"
    MISSING = "WARNING: {n} user message(s) before the last /handoff are not in the handoff file"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.log = self.tmp / "runs.jsonl"

    def put(self, count=15, extra=()):
        lines = BEFORE_TRANSCRIPT.read_text(encoding="utf-8").splitlines()[:count] + list(extra)
        folder = self.home / ".claude" / "projects" / "-tmp-demo"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{SID2}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def handoff(self, section=True, session=SID2):
        text = self.valid_handoff_text().replace("**Continues:** none", f"**Continues:** none\n**Session:** {session}", 1)
        if section:
            text += BEFORE_OK.read_text(encoding="utf-8")
        self.write_handoff("2026-11-01-work-session.tmp", text)

    def resume(self, *args):
        proc = self.run_state("--resume", *args)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        return proc

    def block(self, proc):
        """The `before handoff:` line and the indented lines right after it."""
        out = proc.stdout.splitlines()
        i = next((n for n, ln in enumerate(out) if ln.startswith("before handoff:")), None)
        self.assertIsNotNone(i, f"no before handoff line:\n{proc.stdout}")
        j = i + 1
        while j < len(out) and out[j].startswith("  「"):
            j += 1
        return out[i:j]

    def test_BH30_resume_before_block(self):
        self.put()
        self.handoff()
        proc = self.resume("--c")
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: yes (--c)"], proc.stdout)
        block = self.block(proc)
        self.assertEqual(block[0], "before handoff: 6 user message(s)")
        self.assertEqual(len(block), 7, proc.stdout)
        self.assertEqual((block[1], block[6]), (self.BEFORE_FIRST, self.BEFORE_LAST), proc.stdout)
        for ln in block[1:]:
            self.assertFalse(ln.startswith(("  「'", "  「>", "  「<pasted")), ln)
        self.assertNotIn("before the last /handoff", proc.stdout)
        self.assertNotIn("not checked", proc.stdout)

    def test_BH31_resume_before_missing(self):
        self.put()
        self.handoff(section=False)
        proc = self.resume("--c")
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: yes (--c)"], proc.stdout)
        self.assertEqual(len(self.block(proc)), 7, proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "WARNING: 6 user message(s) before"),
                         [self.MISSING.format(n=6)], proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: yes (--c)"])

    def test_BH32_order(self):
        extra = [json.dumps({"type": "user", "timestamp": "2026-11-09T07:09:00.000Z", "cwd": "/tmp/demo", "gitBranch": "main",
                             "sessionId": SID2, "origin": {"kind": "human"},
                             "message": {"role": "user", "content": M3}}, ensure_ascii=False)]
        self.put(extra=extra)
        self.handoff()
        proc = self.resume("--c")
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), [f"continue: {UNRECORDED}"], proc.stdout)
        out = proc.stdout.splitlines()
        where = [next(n for n, ln in enumerate(out) if ln.startswith(prefix)) for prefix in
                 ("handoff: ", "WARNING: 1 user message(s) after the last /handoff", "before handoff:", "handoff age:")]
        self.assertEqual(where, sorted(where), proc.stdout)
        self.assertEqual(out[where[1] + 1], f"  「{M3}」 (07:09:00Z)", proc.stdout)

    def test_BH33_degrade(self):
        self.handoff()
        proc = self.resume("--c")  # no transcript
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: yes (--c)"])
        self.assertEqual(self.lines_with(proc.stdout, NOT_CHECKED), [NOT_CHECKED + "transcript not found"], proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "before handoff:"), [], proc.stdout)
        self.assertNotIn("before the last /handoff", proc.stdout)
        self.put()
        folder = base_scripts(self.tmp / "mixed", names=("check_handoff.py",), commit=RELEASE_BASE)
        for name in ("handoff_state.py", "run_log.py", "rules_layer.py", "devenv_state.py"):
            (folder / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        proc = subprocess.run([sys.executable, str(folder / "handoff_state.py"), str(self.repo), "--resume", "--c"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.env(), cwd=str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.lines_with(proc.stdout, "continue:"), ["continue: yes (--c)"], proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, NOT_CHECKED), [NOT_CHECKED + "checker unavailable"], proc.stdout)
        self.assertEqual(self.lines_with(proc.stdout, "before handoff:"), [], proc.stdout)

    def test_BH34_old_handoff_unchanged(self):
        old = base_scripts(self.tmp / "old", commit=RELEASE_BASE)
        self.put()
        cases = {"no Session line": self.valid_handoff_text(),
                 "Session none": self.valid_handoff_text().replace("**Continues:** none", "**Continues:** none\n**Session:** none", 1)}
        for label, text in cases.items():
            for env in ({}, {"CLAUDE_CODE_SESSION_ID": SID2}):
                with self.subTest(case=label, env=bool(env)):
                    self.write_handoff("2026-11-01-work-session.tmp", text)
                    runs = [subprocess.run([sys.executable, str(script), str(self.repo), "--resume", "--c"],
                                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                                           env=self.env(HANDOFF_LOG="off", **env), cwd=str(self.tmp))
                            for script in (old / "handoff_state.py", SCRIPT)]
                    self.assertEqual((runs[1].returncode, runs[1].stdout), (runs[0].returncode, runs[0].stdout))

    def test_BH35_privacy(self):
        self.put()
        self.handoff(section=False)
        self.resume("--c")
        log = self.log.read_text(encoding="utf-8")
        for sentinel in ("regression pilot", "full suite run", "parser check", "Plan B", "pilot decision"):
            self.assertNotIn(sentinel, log)
        recs = [json.loads(ln) for ln in log.splitlines()]
        (rec,) = [r for r in recs if r["script"] == "handoff_state.py"]
        self.assertIn(self.MISSING.format(n=6), rec["warnings"])
        self.assertEqual(rec["continue"], "yes (--c)")
        self.assertEqual({len(r) for r in recs}, {26})

    def test_BH36_default_handoff_field(self):
        self.use_scripts_without_budget()
        old = base_scripts(self.tmp / "old", commit=RELEASE_BASE)
        path = self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        proc = self.run_state()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (shown,) = self.lines_with(proc.stdout, "previous handoff: ")
        self.assertEqual(shown, f"previous handoff: {path}")
        recs = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["handoff"] for r in recs], [str(path)])
        before = subprocess.run([sys.executable, str(old / "handoff_state.py"), str(self.repo)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", env=self.env(HANDOFF_LOG="off"),
                                cwd=str(self.tmp))
        self.assertEqual((proc.returncode, proc.stdout), (before.returncode, before.stdout))
        path.unlink()
        self.run_state()
        recs = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertIsNone(recs[-1]["handoff"])
        self.assertIn("previous handoff: none", self.run_state().stdout)


# --- lean-mode: one line from ~/.claude/state/lean-mode.json ------------------------------------------
class LeanModeTests(StateTestCase):
    PREFIX = "lean-mode:"
    SINCE = "2026-11-10T01:02:03Z"
    SECRET = "/synthetic/private/project-dir"

    def setUp(self):
        super().setUp()
        self.init_repo({"a.txt": "a\n"})
        self.log = self.tmp / "runs.jsonl"
        self.state_dir = self.home / ".claude" / "state"
        self.lean = self.state_dir / "lean-mode.json"

    def put(self, data):
        """Write the state file; `data` is a dict (dumped as JSON), str, or bytes (written as is)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(data, dict):
            data = json.dumps(data)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.lean.write_bytes(data)

    def on(self, **extra):
        """`on: true` for this test's own project unless `project=` says otherwise."""
        self.put({"on": True, "since": self.SINCE, "project": self.repo.as_posix(), **extra})

    def lean_lines(self, proc):
        return self.lines_with(proc.stdout, self.PREFIX)

    def test_on_prints_since_in_default_mode(self):
        self.on()
        proc = self.run_state()
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.lean_lines(proc), [f"lean-mode: on (since {self.SINCE})"], proc.stdout)

    def test_off_prints_off(self):
        self.put({"on": False})
        proc = self.run_state()
        self.assertEqual(self.lean_lines(proc), ["lean-mode: off"], proc.stdout)
        self.put({"on": False, "since": self.SINCE, "project": self.SECRET})
        self.assertEqual(self.lean_lines(self.run_state()), ["lean-mode: off"])

    def test_line_sits_between_session_lines_and_budget(self):
        self.use_scripts_without_budget()
        budget =self.home / ".claude" / "scripts" / "context_budget.py"
        budget.parent.mkdir(parents=True)
        budget.write_text("print('budget 42%')\n", encoding="utf-8")
        self.on()
        proc = self.run_state(extra_env={"CLAUDE_CODE_SESSION_ID": SID})
        out = proc.stdout.splitlines()
        i = next(n for n, ln in enumerate(out) if ln.startswith("session id:"))
        self.assertEqual([ln.split(":")[0] for ln in out[i:i + 4]],
                         ["session id", "transcript", "lean-mode", "context-budget"], proc.stdout)

    def test_project_is_never_printed_or_recorded(self):
        self.on(project=self.SECRET)
        for args in ((), ("--resume",)):
            proc = self.run_state(*args)
            self.assertEqual(self.lean_lines(proc), ["lean-mode: off (set for another project)"], proc.stdout)
            self.assertNotIn(self.SECRET, self.combined(proc))
        self.assertNotIn(self.SECRET, self.log.read_text(encoding="utf-8"))

    def test_missing_file_prints_nothing_and_changes_nothing(self):
        self.use_scripts_without_budget()
        plain = self.run_state()
        self.assertEqual(plain.returncode, 0, self.combined(plain))
        self.assertEqual(self.lean_lines(plain), [])
        self.state_dir.mkdir(parents=True)  # the directory alone changes nothing
        self.assertEqual(self.run_state().stdout, plain.stdout)
        self.on()
        with_line = self.run_state()
        rest = [ln for ln in with_line.stdout.splitlines(keepends=True) if not ln.startswith(self.PREFIX)]
        self.assertEqual("".join(rest), plain.stdout, "the lean line is the only difference")

    def test_malformed_file_prints_unknown_and_keeps_exit(self):
        cases = {
            "not json": "{nope",
            "empty": "",
            "list": "[true]",
            "string": '"on"',
            "on missing": '{"since": "%s"}' % self.SINCE,
            "on not bool": '{"on": "yes", "since": "%s"}' % self.SINCE,
            "on is 1": '{"on": 1}',
            "on null": '{"on": null}',
            "not utf-8": b"\xff\xfe{\x00",
            "too deep": "[" * 200000,
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.put(data)
                proc = self.run_state()
                self.assertEqual(proc.returncode, 0, self.combined(proc))
                self.assertEqual(self.lean_lines(proc), ["lean-mode: unknown"], proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)

    def test_unreadable_file_prints_unknown(self):
        self.lean.mkdir(parents=True)  # a directory where the file should be
        proc = self.run_state()
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.lean_lines(proc), ["lean-mode: unknown"], proc.stdout)

    def test_bad_since_prints_on_without_it(self):
        bad = ["", "yesterday", "2026-11-10 01:02:03", "2026-11-10T01:02:03", "2026-11-10T01:02:03+09:00",
               "2026-11-10T01:02:03Z\nlean-mode: off", self.SINCE + "\n", "/etc/passwd", "x" * 500,
               " " + self.SINCE, "2026-13-10T01:02:03Z", 20261110, None, ["a"]]
        for value in bad:
            with self.subTest(since=value):
                self.put({"on": True, "since": value, "project": self.repo.as_posix()})
                proc = self.run_state()
                self.assertEqual(proc.returncode, 0, self.combined(proc))
                self.assertEqual(proc.stdout.splitlines().count("lean-mode: on"), 1, proc.stdout)
                self.assertEqual(self.lean_lines(proc), ["lean-mode: on"], proc.stdout)
        self.put({"on": True, "project": self.repo.as_posix()})
        self.assertEqual(self.lean_lines(self.run_state()), ["lean-mode: on"], "no since key")
        for good in ("2026-11-10T01:02:03Z", "2026-11-10T01:02:03.456Z", "2026-11-10T01:02:03+00:00"):
            with self.subTest(since=good):
                self.put({"on": True, "since": good, "project": self.repo.as_posix()})
                self.assertEqual(self.lean_lines(self.run_state()), [f"lean-mode: on (since {good})"])

    def test_matching_project_is_on_in_both_modes(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.on(project=self.repo.as_posix() + "/")  # a trailing slash is the same project
        for args in ((), ("--resume",)):
            proc = self.run_state(*args)
            self.assertEqual(self.lean_lines(proc), [f"lean-mode: on (since {self.SINCE})"], proc.stdout)

    def test_project_with_tilde_matches_after_expanduser(self):
        work = self.home / "work"
        work.mkdir()
        self.git("init", "-q", "-b", "main", repo=work)
        self.put({"on": True, "since": self.SINCE, "project": "~/work"})
        self.assertEqual(self.lean_lines(self.run_state(cwd=work)), [f"lean-mode: on (since {self.SINCE})"])
        self.assertEqual(self.lean_lines(self.run_state()), ["lean-mode: off (set for another project)"])

    def test_linked_worktree_uses_the_main_tree_as_project(self):
        tree = self.tmp / "linked"
        self.git("worktree", "add", "-q", "-b", "side", str(tree))
        self.on()  # the main tree
        self.assertEqual(self.lean_lines(self.run_state(cwd=tree)), [f"lean-mode: on (since {self.SINCE})"])
        self.on(project=tree.as_posix())  # the lean skill writes its cwd: the linked tree it was turned on in
        self.assertEqual(self.lean_lines(self.run_state(cwd=tree)), [f"lean-mode: on (since {self.SINCE})"])
        self.on(project=(tree / "sub").as_posix())
        self.assertEqual(self.lean_lines(self.run_state(cwd=tree)), [f"lean-mode: on (since {self.SINCE})"])
        # known limit (spec 21.9): another linked tree of the same repository is not recognised
        other = self.tmp / "linked-other"
        self.git("worktree", "add", "-q", "-b", "side2", str(other))
        self.assertEqual(self.lean_lines(self.run_state(cwd=other)), ["lean-mode: off (set for another project)"])
        self.on(project=str(self.tmp / "linked-x"))
        self.assertEqual(self.lean_lines(self.run_state(cwd=tree)), ["lean-mode: off (set for another project)"])

    def test_project_inside_the_project_key_matches(self):
        for sub in ("sub", "a/b/c", "sub/"):
            with self.subTest(sub=sub):
                self.on(project=f"{self.repo.as_posix()}/{sub}")  # the cwd the lean skill ran in
                for args in ((), ("--resume",)):
                    proc = self.run_state(*args)
                    self.assertEqual(self.lean_lines(proc), [f"lean-mode: on (since {self.SINCE})"], proc.stdout)
        subdir = self.repo / "sub"
        subdir.mkdir()
        self.assertEqual(self.lean_lines(self.run_state(cwd=subdir)), [f"lean-mode: on (since {self.SINCE})"])

    def test_project_prefix_lookalikes_and_dotdot_do_not_match(self):
        repo = self.repo.as_posix()
        for value in (repo + "-other/sub", repo + "x", repo + "//..", repo + "/../elsewhere", repo + "/sub/../../x"):
            with self.subTest(project=value):
                self.on(project=value)
                self.assertEqual(self.lean_lines(self.run_state()), ["lean-mode: off (set for another project)"])

    def test_non_git_cwd_subfolder_matches(self):
        plain = self.tmp / "plain"
        (plain / "deep").mkdir(parents=True)
        self.put({"on": True, "since": self.SINCE, "project": (plain / "deep").as_posix()})
        self.assertEqual(self.lean_lines(self.run_state(cwd=plain)), [f"lean-mode: on (since {self.SINCE})"])

    def test_other_project_is_off_for_another_project(self):
        repo = self.repo.as_posix()
        others = [self.SECRET, repo + "-other", str(self.repo.parent), "", "  ", 7, None, ["x"], {"a": 1}]
        for value in others:
            with self.subTest(project=value):
                self.on(project=value)
                proc = self.run_state()
                self.assertEqual(proc.returncode, 0, self.combined(proc))
                self.assertEqual(self.lean_lines(proc), ["lean-mode: off (set for another project)"], proc.stdout)
        self.put({"on": True, "since": self.SINCE})
        self.assertEqual(self.lean_lines(self.run_state()), ["lean-mode: off (set for another project)"],
                         "no project key")
        self.put({"on": False, "since": self.SINCE, "project": self.SECRET})
        self.assertEqual(self.lean_lines(self.run_state()), ["lean-mode: off"], "off stays plain off")

    def test_resume_mode_prints_the_line_too(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        self.on()
        proc = self.run_state("--resume", "--c")
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.lean_lines(proc), [f"lean-mode: on (since {self.SINCE})"], proc.stdout)
        out = proc.stdout.splitlines()
        self.assertLess(out.index(f"lean-mode: on (since {self.SINCE})"), next(
            n for n, ln in enumerate(out) if ln.startswith("continue:")))
        self.on(project=self.SECRET)
        self.assertEqual(self.lean_lines(self.run_state("--resume")), ["lean-mode: off (set for another project)"])
        self.put({"on": False})
        self.assertEqual(self.lean_lines(self.run_state("--resume")), ["lean-mode: off"])
        self.put("{nope")
        self.assertEqual(self.lean_lines(self.run_state("--resume")), ["lean-mode: unknown"])

    def test_resume_without_handoff_still_prints_it_and_keeps_exit(self):
        self.on()
        proc = self.run_state("--resume")
        self.assertEqual(proc.returncode, 1, self.combined(proc))
        self.assertEqual(self.lean_lines(proc), [f"lean-mode: on (since {self.SINCE})"], proc.stdout)

    def test_resume_without_file_is_unchanged(self):
        self.write_handoff("2026-11-01-work-session.tmp", self.valid_handoff_text())
        plain = self.run_state("--resume")
        self.on()
        with_line = self.run_state("--resume")
        rest = [ln for ln in with_line.stdout.splitlines(keepends=True) if not ln.startswith(self.PREFIX)]
        self.assertEqual("".join(rest), plain.stdout)
        self.assertEqual(with_line.returncode, plain.returncode)

    def test_run_log_gains_no_key(self):
        self.run_state()
        self.on()
        self.run_state()
        plain, with_line = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(set(plain), set(with_line))
        self.assertEqual(len(plain), 26, sorted(plain))
        self.assertIn(f"lean-mode: on (since {self.SINCE})", with_line["stdout"])
        self.assertNotIn("lean-mode: on (since", "".join(plain["stdout"]))

    def test_state_file_is_never_written(self):
        self.on()
        before = (self.lean.read_bytes(), self.lean.stat().st_mtime_ns)
        self.run_state()
        self.run_state("--resume")
        self.assertEqual((self.lean.read_bytes(), self.lean.stat().st_mtime_ns), before)
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), ["lean-mode.json"])


if __name__ == "__main__":
    unittest.main()
