"""Tests for scripts/hook_return.py (return length gate: SubagentStop and the SubagentHandback report tool).

Every run uses a temporary HOME, a temporary state folder (`--root`) and a temporary data folder
(`--data`), so the real ~/.claude is never read or written. Lean mode is turned on for the exact folder
the input names as `cwd`.
"""
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "hook_return.py"
FIXTURES = ROOT / "tests" / "fixtures" / "hooks"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


devenv_state = load("devenv_state")


def body(lines):
    return "\n".join(f"line {i}" for i in range(1, lines + 1))


class HookCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "state"
        self.data = self.tmp / "data"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.events_file = self.root / "events.jsonl"

    # --- inputs ---------------------------------------------------------------------------------------
    def fixture(self, name):
        data = json.loads((FIXTURES / f"return_{name}.json").read_text(encoding="utf-8"))
        data["cwd"] = str(self.proj)
        return data

    def stop_input(self, text, **changes):
        data = self.fixture("stop")
        data["last_assistant_message"] = text
        data.update(changes)
        return data

    def handback_input(self, text, **changes):
        data = self.fixture("handback")
        data["tool_input"] = {"message": text}
        data.update(changes)
        return data

    # --- state ----------------------------------------------------------------------------------------
    def lean_on(self):
        devenv_state.lean_set(True, str(self.proj), root=self.root)
        self.assertTrue(devenv_state.lean_status(str(self.proj), None, root=self.root)["on"])

    def dev_on(self):
        devenv_state.dev_set(True, root=self.root)

    def records(self):
        if not self.events_file.is_file():
            return []
        return [json.loads(ln) for ln in self.events_file.read_text(encoding="utf-8").splitlines()]

    # --- running --------------------------------------------------------------------------------------
    def run_hook(self, mode, payload, *, data=None, script=SCRIPT, extra=()):
        args = [sys.executable, str(script), mode]
        if mode == "handback":
            args += ["--data", str(self.data if data is None else data)]
        args += ["--root", str(self.root), *extra]
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        proc = subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              env=env, cwd=str(self.tmp), timeout=60)
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc, out

    def assert_passed(self, proc, out):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if out is not None:
            self.assertNotIn("decision", out)
            self.assertNotEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def assert_blocked(self, proc, out):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["decision"], "block")
        return out["reason"]

    def assert_denied(self, proc, out):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertEqual(spec["permissionDecision"], "deny")
        return spec["permissionDecisionReason"]


class StopGateTests(HookCase):
    """SubagentStop (default permission mode: the final reply is `last_assistant_message`)."""

    def test_31_lines_blocked(self):
        self.lean_on()
        reason = self.assert_blocked(*self.run_hook("stop", self.stop_input(body(31))))
        self.assertIn("31", reason)
        self.assertIn("30", reason)

    def test_30_lines_pass(self):
        self.lean_on()
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(30))))

    def test_blank_lines_counted(self):
        self.lean_on()
        text = body(15) + "\n\n" + body(15)  # 30 text lines and 1 blank line
        self.assert_blocked(*self.run_hook("stop", self.stop_input(text)))

    def test_stop_hook_active_passes(self):
        self.lean_on()
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(40), stop_hook_active=True)))

    def test_empty_agent_type_passes(self):
        self.lean_on()
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(40), agent_type="")))
        payload = self.stop_input(body(40))
        del payload["agent_type"]
        self.assert_passed(*self.run_hook("stop", payload))

    def test_lean_off_passes(self):
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(40))))

    def test_folder_above_the_project_passes(self):
        self.lean_on()
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(40), cwd=str(self.tmp))))
        self.assert_passed(*self.run_hook("handback", self.handback_input(body(40), cwd=str(self.tmp))))

    def test_missing_last_message_passes(self):
        """Auto mode: the report went through SubagentHandback and SubagentStop has no reply field."""
        self.lean_on()
        payload = self.stop_input(body(40))
        del payload["last_assistant_message"]
        self.assert_passed(*self.run_hook("stop", payload))
        self.assert_passed(*self.run_hook("stop", self.stop_input(None)))

    def test_reason_has_no_off_instruction(self):
        self.lean_on()
        reasons = [self.assert_blocked(*self.run_hook("stop", self.stop_input(body(31)))),
                   self.assert_denied(*self.run_hook("handback", self.handback_input(body(31))))]
        for reason in reasons:
            self.assertIn("file", reason)
            self.assertIn("summary", reason)
            for word in ("off", "disable", "lean", "devenv_state", "rules", "devenv.local.md"):
                self.assertNotRegex(reason.lower(), rf"\b{re.escape(word)}\b")

    def test_limit_from_user_layer(self):
        self.lean_on()
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "devenv.local.md").write_text("## lean-limits\nreturn_lines: 5\n",
                                                               encoding="utf-8")
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(5))))
        reason = self.assert_blocked(*self.run_hook("stop", self.stop_input(body(6))))
        self.assertIn("5", reason)

    def test_limit_from_project_layer(self):
        if shutil.which("git") is None:
            self.fail("git is required for this test")
        subprocess.run(["git", *GIT_ID, "init", "-q", str(self.proj)], check=True, capture_output=True)
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "devenv.local.md").write_text("## lean-limits\nreturn_lines: 3\n",
                                                               encoding="utf-8")
        self.lean_on()
        self.assert_passed(*self.run_hook("stop", self.stop_input(body(3))))
        self.assert_blocked(*self.run_hook("stop", self.stop_input(body(4))))


class HandbackGateTests(HookCase):
    """PreToolUse on SubagentHandback (auto mode: the only point that can stop a long report)."""

    def test_handback_denied_once_then_passes(self):
        self.lean_on()
        reason = self.assert_denied(*self.run_hook("handback", self.handback_input(body(31))))
        self.assertIn("31", reason)
        self.assertIn("30", reason)
        self.assertTrue((self.data / "handback" / "a0123456789abcdef").is_file())
        self.assert_passed(*self.run_hook("handback", self.handback_input(body(31))))
        # another subagent gets its own single denial
        self.assert_denied(*self.run_hook("handback", self.handback_input(body(31), agent_id="a99")))

    def test_handback_30_lines_pass(self):
        self.lean_on()
        self.assert_passed(*self.run_hook("handback", self.handback_input(body(30))))
        self.assertFalse((self.data / "handback").exists())

    def test_handback_lean_off_passes(self):
        self.assert_passed(*self.run_hook("handback", self.handback_input(body(31))))

    def test_handback_marker_unwritable_passes(self):
        """A marker that cannot be written would deny every retry, so the call goes through instead."""
        self.lean_on()
        blocker = self.tmp / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        proc, out = self.run_hook("handback", self.handback_input(body(31)), data=blocker)
        self.assert_passed(proc, out)
        self.assertIn("marker", proc.stderr)

    def test_handback_relative_data_passes(self):
        self.lean_on()
        proc, out = self.run_hook("handback", self.handback_input(body(31)), data="")
        self.assert_passed(proc, out)
        self.assertFalse((self.tmp / "handback").exists())

    def test_handback_unsafe_agent_id_passes(self):
        self.lean_on()
        for agent_id in ("../escape", "", None):
            payload = self.handback_input(body(31), agent_id=agent_id)
            self.assert_passed(*self.run_hook("handback", payload))
        self.assertFalse((self.data / "escape").exists())

    def test_other_tool_passes(self):
        self.lean_on()
        self.assert_passed(*self.run_hook("handback", self.handback_input(body(31), tool_name="Bash")))


class SharedTests(HookCase):
    def test_count_lines(self):
        hook = load("hook_return")
        self.assertEqual(hook.count_lines(""), 0)
        self.assertEqual(hook.count_lines("a"), 1)
        self.assertEqual(hook.count_lines("a\n"), 1)
        self.assertEqual(hook.count_lines("a\n\nb"), 3)
        self.assertEqual(hook.count_lines("a\r\nb\rc"), 3)
        self.assertEqual(hook.count_lines("a\n\n"), 2)

    def test_lean_unknown_passes_with_warning_and_record(self):
        self.root.mkdir()
        (self.root / "lean-mode.json").write_text("{broken", encoding="utf-8")
        self.dev_on()
        for mode, payload in (("stop", self.stop_input(body(40))), ("handback", self.handback_input(body(40)))):
            proc, out = self.run_hook(mode, payload)
            self.assert_passed(proc, out)
            self.assertIn("lean", out["systemMessage"])
        recs = self.records()
        self.assertEqual([r["kind"] for r in recs], ["return_pass", "return_pass"])
        self.assertTrue(all(r["lean_unknown"] == 1 and r["lean_on"] is None for r in recs))

    def test_events_recorded_under_root(self):
        self.lean_on()
        self.dev_on()
        self.run_hook("stop", self.stop_input(body(31)))
        self.run_hook("stop", self.stop_input(body(2)))
        self.run_hook("handback", self.handback_input(body(31)))
        recs = self.records()
        self.assertEqual([r["kind"] for r in recs], ["return_block", "return_pass", "handback_deny"])
        self.assertEqual((recs[0]["lines"], recs[0]["bytes"]), (31, len(body(31).encode())))
        self.assertEqual(recs[1]["bytes"], len(body(2).encode()))
        self.assertNotIn("lines", recs[1])
        self.assertEqual(recs[2]["lines"], 31)
        self.assertTrue(all(r["lean_on"] is True for r in recs))
        self.assertFalse((self.home / ".claude" / "handoffs").exists())

    def test_broken_stdin_passes(self):
        self.lean_on()
        for mode in ("stop", "handback"):
            for payload in ("{not json", "[1, 2]", ""):
                self.assert_passed(*self.run_hook(mode, payload))

    def test_usage_error_never_exits_0_or_2(self):
        """Exit 2 would block the subagent or the tool; exit 0 would hide a broken registration."""
        for args in ([sys.executable, str(SCRIPT)], [sys.executable, str(SCRIPT), "handback"],
                     [sys.executable, str(SCRIPT), "nope"]):
            proc = subprocess.run(args, input="{}", capture_output=True, text=True, timeout=60,
                                  env=dict(os.environ, HOME=str(self.home)))
            self.assertNotIn(proc.returncode, (0, 2), args)
            self.assertTrue(proc.stderr.strip())

    def test_script_error_not_hidden(self):
        """A missing sibling module is an engine error: non-zero exit, and never 2 (that would block)."""
        alone = self.tmp / "alone"
        alone.mkdir()
        shutil.copy(SCRIPT, alone / SCRIPT.name)
        self.lean_on()
        proc, _ = self.run_hook("stop", self.stop_input(body(31)), script=alone / SCRIPT.name)
        self.assertNotIn(proc.returncode, (0, 2))


if __name__ == "__main__":
    unittest.main()
