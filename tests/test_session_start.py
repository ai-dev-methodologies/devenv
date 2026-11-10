"""SessionStart hook: the context-budget log line, the lean notice, the over-cap report and the doctor verdict."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "hook_session_start.py"
STATE = ROOT / "scripts" / "devenv_state.py"
CAP = 49152
SID = "00000000-0000-4000-8000-0000000000aa"
LINE_RE = re.compile(r"^context-budget \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \S+ total=\d+ cap=\d+ "
                     r"status=(ok|over) files=\d+ missing=\d+$")


def load_hook():
    spec = importlib.util.spec_from_file_location("hook_session_start_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionStartTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.root = self.tmp / "state"
        self.log = self.tmp / "logs" / "budget.log"

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("HANDOFF_LOG",)}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8")
        return env

    def user_rules(self, size):
        (self.home / ".claude" / "CLAUDE.md").write_bytes(b"x" * size)

    def state(self, *args):
        proc = subprocess.run([sys.executable, str(STATE), *args, "--root", str(self.root)], capture_output=True,
                              text=True, encoding="utf-8", env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def lean_on(self):
        self.state("lean", "on", "--cwd", str(self.proj))

    def run_hook(self, *args, payload=None, root=True, log=True):
        if payload is None:
            payload = {"session_id": SID, "transcript_path": str(self.tmp / "t.jsonl"), "cwd": str(self.proj),
                       "hook_event_name": "SessionStart", "source": "startup"}
        argv = [sys.executable, str(HOOK), *args]
        if log:
            argv += ["--log", str(self.log)]
        if root:
            argv += ["--root", str(self.root)]
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(), cwd=str(self.tmp), timeout=60)

    def context(self, proc):
        """additionalContext of the hook output, or None when the hook printed nothing."""
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        if not proc.stdout.strip():
            return None
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return out["hookSpecificOutput"]["additionalContext"]

    def log_lines(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


class SessionStartTests(SessionStartTestCase):
    def test_over_cap_lean_on_reports(self):
        self.user_rules(CAP + 1)
        self.lean_on()
        self.state("dev", "on")
        ctx = self.context(self.run_hook())
        self.assertIn("lean-mode: on (since ", ctx)
        self.assertIn(f"total={CAP + 1} cap={CAP}", ctx)
        self.assertIn("CLAUDE.md", ctx)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], LINE_RE)
        self.assertIn(f" {self.proj} total={CAP + 1} cap={CAP} status=over ", lines[0])
        events = [json.loads(ln) for ln in (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(e["kind"], e["lean_on"], e["start_bytes"]) for e in events],
                         [("session_start", True, CAP + 1)])

    def test_over_cap_lean_off_silent_but_logged(self):
        self.user_rules(CAP + 1)
        proc = self.run_hook()
        self.assertIsNone(self.context(proc))
        self.assertEqual(proc.stderr, "")
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("status=over", lines[0])
        self.assertFalse((self.root / "events.jsonl").exists(), "developer mode is off: no event")

    def test_under_cap_lean_on_only_notice(self):
        self.user_rules(CAP)  # exactly the cap is still ok
        self.lean_on()
        ctx = self.context(self.run_hook())
        self.assertTrue(ctx.startswith("lean-mode: on (since "), ctx)
        self.assertNotIn("total=", ctx)
        self.assertNotIn("over", ctx)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"total={CAP} cap={CAP} status=ok", lines[0])

    def test_folder_above_the_project_has_no_lean_notice(self):
        self.lean_on()
        payload = {"session_id": SID, "transcript_path": str(self.tmp / "t.jsonl"), "cwd": str(self.tmp),
                   "hook_event_name": "SessionStart", "source": "startup"}
        self.assertIsNone(self.context(self.run_hook(payload=payload)))

    def test_additional_context_under_10000_chars(self):
        deep = self.proj
        for i in range(4):
            deep = deep / (f"{i:02d}" + "d" * 200)
        deep.mkdir(parents=True)
        (deep / "CLAUDE.md").write_bytes(b"x" * CAP)
        (deep / "CLAUDE.local.md").write_bytes(b"y" * CAP)
        rules = self.home / ".claude" / "rules"
        rules.mkdir()
        for i in range(30):
            (rules / (f"{i:02d}" + "r\n" * 50 + ".md")).write_bytes(b"z" * 1000)
        # outside git the project key is the cwd itself, so lean is set for the folder the session starts in
        self.state("lean", "on", "--cwd", str(deep))
        ctx = self.context(self.run_hook(payload={"session_id": SID, "cwd": str(deep),
                                                  "hook_event_name": "SessionStart", "source": "startup"}))
        self.assertIn("status=over", self.log_lines()[0])
        self.assertIn("total=", ctx)
        self.assertLess(len(ctx), 10000)
        self.assertEqual(ctx.splitlines(), [ctx], "one line, even with a newline in a file name")

    def test_symlinked_cwd_outside_git_uses_the_same_project_key_as_the_other_hooks(self):
        # Outside git the project key is the cwd as given (devenv_state.git_project). The other hooks pass the
        # input cwd unchanged, so this hook must not resolve the symbolic link before judging lean mode.
        if not hasattr(os, "symlink"):
            self.fail("os.symlink is not available")
        link = self.tmp / "link"
        link.symlink_to(self.proj, target_is_directory=True)
        self.state("lean", "on", "--cwd", str(link))
        spec = importlib.util.spec_from_file_location("devenv_state_for_symlink", STATE)
        state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(state)
        self.assertTrue(state.lean_status(str(link), SID, root=self.root)["on"], "the other hooks see lean on")
        ctx = self.context(self.run_hook(payload={"session_id": SID, "cwd": str(link),
                                                  "hook_event_name": "SessionStart", "source": "startup"}))
        self.assertIsNotNone(ctx, "lean notice missing for a symlinked cwd")
        self.assertTrue(ctx.startswith("lean-mode: on (since "), ctx)

    def test_log_root_isolation(self):
        self.user_rules(10)
        self.lean_on()
        self.state("dev", "on")
        default_log = self.home / ".claude" / "logs" / "context-budget.log"
        self.assertIsNotNone(self.context(self.run_hook()))
        self.assertEqual(len(self.log_lines()), 1)
        self.assertFalse(default_log.exists(), "--log replaces the default log path")
        self.assertFalse((self.home / ".claude" / "state").exists(), "--root replaces the state folder")
        self.assertFalse((self.home / ".claude" / "handoffs").exists(), "--root replaces the events folder")
        self.assertTrue((self.root / "events.jsonl").is_file())
        proc = self.run_hook(log=False, root=False)
        self.assertIsNone(self.context(proc), "lean state under --root is not the default state")
        self.assertEqual(len(default_log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(len(self.log_lines()), 1)

    def test_errors_exit_1_never_0_or_2(self):
        self.user_rules(10)
        self.log.mkdir(parents=True)  # the log path is a folder: the line cannot be written
        cases = {"log not writable": self.run_hook(), "stdin not JSON": self.run_hook(payload="{nope"),
                 "unknown argument": self.run_hook("--bogus")}
        for name, proc in cases.items():
            with self.subTest(name):
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith("hook_session_start: "), proc.stderr)
                self.assertEqual(proc.stdout, "")


class StartVerdictTests(unittest.TestCase):
    def test_start_verdict_is_pure_and_names_three_largest(self):
        hook = load_hook()
        rows = [(Path("/a"), 10), (Path("/b"), None), (Path("/c"), CAP), (Path("/d"), 5), (Path("/e"), 7)]
        over = hook.start_verdict(rows)
        self.assertEqual((over["status"], over["total"], over["cap"]), ("FAIL", CAP + 22, CAP))
        self.assertEqual(over["largest"], [(Path("/c"), CAP), (Path("/a"), 10), (Path("/e"), 7)])
        self.assertEqual(hook.start_verdict(rows, cap=CAP + 22)["status"], "PASS")
        self.assertEqual(hook.start_verdict([])["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
