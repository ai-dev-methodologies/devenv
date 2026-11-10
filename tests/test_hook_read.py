"""Tests for scripts/hook_read.py (PreToolUse: large Read denial and lean-off guard).

The hook runs as a subprocess with `--root <tmp>` (state folder, events and user layer) and HOME pointed at a
temporary folder, so the real ~/.claude is never read or written. Inputs start from
tests/fixtures/hooks/read_*.json. Lean mode is turned on and looked up for the same folder only.
"""
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
SCRIPTS = ROOT / "scripts"
HOOK = SCRIPTS / "hook_read.py"
FIXTURES = ROOT / "tests" / "fixtures" / "hooks"
OFF_INSTRUCTION = re.compile(r"\boff\b|lean-off|disable|devenv_state|allow_large|turn\b", re.IGNORECASE)


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


devenv_state = load("devenv_state")


class HookReadCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.root = self.tmp / "root"
        self.home = self.tmp / "home"
        self.proj = self.tmp / "proj"
        for d in (self.root, self.home, self.proj):
            d.mkdir()

    # --- helpers -------------------------------------------------------------------------------------
    def lean_on(self):
        devenv_state.lean_set(True, str(self.proj), root=self.root)

    def dev_on(self):
        devenv_state.dev_set(True, root=self.root)

    def text_file(self, name, lines, width=10, where=None):
        path = (where or self.proj) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join("x" * width + "\n" for _ in range(lines)), encoding="utf-8")
        return path

    def payload(self, tool, **tool_input):
        data = json.loads((FIXTURES / f"read_{tool}.json").read_text(encoding="utf-8"))
        data["cwd"] = str(self.proj)
        data["tool_input"].update(tool_input)
        return data

    def read(self, path, **extra):
        return self.payload("read", file_path=str(path), **extra)

    def bash(self, command):
        return self.payload("bash", command=command)

    def run_hook(self, data=None, raw=None, args=None):
        stdin = raw if raw is not None else json.dumps(data)
        argv = args if args is not None else ["--root", str(self.root)]
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        return subprocess.run([sys.executable, str(HOOK), *argv], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", cwd=str(self.proj), env=env, timeout=60)

    def decide(self, data):
        """(decision or None, reason or None, parsed output or None); asserts exit 0."""
        proc = self.run_hook(data)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return None, None, None
        out = json.loads(proc.stdout)
        spec = out.get("hookSpecificOutput") or {}
        return spec.get("permissionDecision"), spec.get("permissionDecisionReason"), out

    def assertDenied(self, data):
        decision, reason, _ = self.decide(data)
        self.assertEqual(decision, "deny")
        return reason

    def assertAllowed(self, data):
        decision, _, out = self.decide(data)
        self.assertIsNone(decision, out)
        return out

    def events(self):
        path = self.root / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class ReadDenialTests(HookReadCase):
    def setUp(self):
        super().setUp()
        self.lean_on()

    def test_big_read_no_limit_denied(self):
        big = self.text_file("big.txt", 500)
        reason = self.assertDenied(self.read(big))
        self.assertIn("500 lines", reason)
        self.assertIn(f"{500 * 11} bytes", reason)
        self.assertIn("offset", reason)
        self.assertIn("limit", reason)
        self.assertIn("subagent", reason)

    def test_big_read_with_small_limit_allowed(self):
        big = self.text_file("big.txt", 500)
        self.assertAllowed(self.read(big, limit=100))
        self.assertAllowed(self.read(big, offset=200, limit=400))  # exactly the line limit

    def test_allow_large_on_big_file_denied(self):
        big = self.text_file("big.txt", 500)
        self.assertDenied(self.read(big, limit=100, allow_large=True))

    def test_limit_over_line_limit_denied(self):
        big = self.text_file("big.txt", 500)
        self.assertDenied(self.read(big, limit=401))

    def test_big_by_bytes_only_denied(self):
        wide = self.text_file("wide.txt", 2, width=20000)  # 2 lines, 40002 bytes
        self.assertDenied(self.read(wide))
        self.assertAllowed(self.read(wide, limit=1))

    def test_small_file_allowed(self):
        small = self.text_file("small.txt", 400)  # exactly the line limit, 4400 bytes
        self.assertAllowed(self.read(small))

    def test_binary_allowed(self):
        blob = self.proj / "blob.bin"
        blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00" + b"\n" * 100000)
        self.assertAllowed(self.read(blob))

    def test_handoffs_dir_allowed(self):
        inside = self.text_file("handoff.md", 500, where=self.root / "handoffs" / "sub")
        outside = self.text_file("handoff.md", 500)
        self.assertAllowed(self.read(inside))
        self.assertDenied(self.read(outside))

    def test_missing_file_allowed(self):
        self.assertAllowed(self.read(self.proj / "missing.txt"))

    def test_user_layer_limits(self):
        (self.root / "devenv.local.md").write_text("## lean-limits\nread_lines: 10\n", encoding="utf-8")
        mid = self.text_file("mid.txt", 20)
        self.assertDenied(self.read(mid))
        self.assertAllowed(self.read(mid, limit=10))

    def test_bad_limit_value_uses_default(self):
        (self.root / "devenv.local.md").write_text("## lean-limits\nread_lines: ten\n", encoding="utf-8")
        mid = self.text_file("mid.txt", 20)
        self.assertAllowed(self.read(mid))
        self.assertDenied(self.read(self.text_file("big.txt", 401)))

    def test_project_layer_limits(self):
        git = subprocess.run(["git", "init", "-q", str(self.proj)], capture_output=True, text=True)
        if git.returncode != 0:
            self.fail(f"git init failed: {git.stderr}")
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "devenv.local.md").write_text("## lean-limits\nread_lines: 10\n",
                                                                encoding="utf-8")
        self.assertDenied(self.read(self.text_file("mid.txt", 20)))

    def test_rules_layer_cannot_lift_the_limit_past_the_cap(self):
        self.lean_on()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True, capture_output=True)
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "devenv.local.md").write_text(
            "## lean-limits\nread_bytes: 999999999\nread_lines: 999999999\n", encoding="utf-8")
        self.assertAllowed(self.read(self.text_file("at_cap.txt", 1600)))
        self.assertDenied(self.read(self.text_file("over_cap.txt", 1601)))

    def test_read_deny_recorded(self):
        self.dev_on()
        big = self.text_file("big.txt", 500)
        self.assertDenied(self.read(big))
        self.assertAllowed(self.read(big, limit=10))
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["kind"], "read_deny")
        self.assertIs(rows[0]["lean_on"], True)
        self.assertEqual((rows[0]["lines"], rows[0]["bytes"], rows[0]["guard"]), (500, 5500, 0))

    def test_other_tools_ignored(self):
        data = self.read(self.text_file("big.txt", 500))
        data["tool_name"] = "Grep"
        self.assertAllowed(data)


class ScopeTests(HookReadCase):
    def subagent(self, data):
        data["agent_id"] = "a0123456789abcdef"
        data["agent_type"] = "general-purpose"
        return data

    def test_subagent_call_allowed(self):
        self.lean_on()
        big = self.text_file("big.txt", 500)
        self.assertAllowed(self.subagent(self.read(big)))
        self.assertDenied(self.read(big))  # the same call from the main thread

    def test_subagent_lean_off_attempts_denied(self):
        self.lean_on()
        state = self.root / "lean-mode.json"
        for data in (self.bash("python3 devenv_state.py lean off"),
                     self.bash("python3 devenv_state.py lean --cwd /p off"),
                     self.payload("write", file_path=str(state)),
                     self.payload("edit", file_path=str(state))):
            with self.subTest(tool=data["tool_name"], data=data["tool_input"]):
                self.assertDenied(self.subagent(data))

    def test_subagent_unrelated_calls_allowed(self):
        self.lean_on()
        for data in (self.bash("python3 devenv_state.py dev off"),
                     self.bash("python3 devenv_state.py lean on --cwd /work/off"),
                     self.payload("write", file_path=str(self.proj / "notes.md"))):
            with self.subTest(data=data["tool_input"]):
                self.assertAllowed(self.subagent(data))

    def test_folder_above_the_project_not_enforced(self):
        self.lean_on()
        big = self.text_file("big.txt", 500)
        for cwd in (self.tmp, "/"):
            with self.subTest(cwd=cwd):
                data = self.read(big)
                data["cwd"] = str(cwd)
                self.assertAllowed(data)
                data = self.bash("python3 devenv_state.py lean off")
                data["cwd"] = str(cwd)
                self.assertAllowed(data)
        self.assertDenied(self.read(big))

    def test_another_session_does_not_end_this_session(self):
        big = self.text_file("big.txt", 500)
        other = self.tmp / "other"
        other.mkdir()
        data = self.read(big)
        devenv_state.lean_set(True, str(self.proj), session_id=data["session_id"], root=self.root)
        self.assertDenied(data)
        devenv_state.lean_set(True, str(other), session_id="another-session", root=self.root)
        self.assertAllowed(self.bash("python3 devenv_state.py lean on --session x --cwd /tmp"))
        devenv_state.lean_set(True, "/tmp", session_id="x", root=self.root)
        self.assertDenied(data)

    def test_lean_off_allowed(self):
        big = self.text_file("big.txt", 500)
        self.assertAllowed(self.read(big))
        self.assertAllowed(self.bash("devenv_state.py lean off"))
        self.assertAllowed(self.payload("write", file_path=str(self.root / "lean-mode.json")))
        devenv_state.lean_set(True, str(self.proj), root=self.root)
        devenv_state.lean_set(False, str(self.proj), root=self.root)
        self.assertAllowed(self.read(big))

    def test_unknown_state_passes_with_warning(self):
        self.dev_on()
        (self.root / "lean-mode.json").write_text("{broken", encoding="utf-8")
        big = self.text_file("big.txt", 500)
        out = self.assertAllowed(self.read(big))
        self.assertIsNotNone(out, "unknown state must not pass silently")
        self.assertIn("lean", out.get("systemMessage", ""))
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["kind"], "read_deny")
        self.assertIsNone(rows[0]["lean_on"])
        self.assertEqual((rows[0]["unknown"], rows[0]["guard"]), (1, 0))

    def test_unknown_state_not_checked_for_unrelated_bash(self):
        (self.root / "lean-mode.json").write_text("{broken", encoding="utf-8")
        proc = self.run_hook(self.bash("ls -la"))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class LeanGuardTests(HookReadCase):
    def setUp(self):
        super().setUp()
        self.lean_on()
        self.state = self.root / "lean-mode.json"

    def test_state_write_denied(self):
        self.assertDenied(self.payload("write", file_path=str(self.state)))
        self.assertDenied(self.payload("edit", file_path=str(self.state)))
        self.assertDenied(self.payload("write", file_path=str(self.root / "sub" / ".." / "lean-mode.json")))
        self.assertAllowed(self.payload("write", file_path=str(self.root / "devenv-dev.json")))
        self.assertAllowed(self.payload("edit", file_path=str(self.proj / "lean-mode.json")))

    def test_lean_off_command_denied(self):
        for cmd in ("devenv-py /x/scripts/devenv_state.py lean off --cwd /p --py /usr/bin/python3",
                    "python3 scripts/devenv_state.py lean off",
                    "cd /p && python3 devenv_state.py  lean\toff"):
            with self.subTest(cmd=cmd):
                self.assertDenied(self.bash(cmd))

    def test_lean_off_any_option_order_denied(self):
        for cmd in ("python3 devenv_state.py lean --cwd /p off",
                    "python3 devenv_state.py lean off --cwd /p",
                    "python3 devenv_state.py --cwd /p lean off",
                    "python3 devenv_state.py lean --session abc --cwd /p off",
                    "devenv-py /x/scripts/devenv_state.py lean --cwd /p off --py /usr/bin/python3",
                    "python3 '/a b/scripts/devenv_state.py' lean --cwd '/c d' off",
                    "cd /p && python3 devenv_state.py lean --cwd /p off && echo done"):
            with self.subTest(cmd=cmd):
                self.assertDenied(self.bash(cmd))

    def test_off_outside_the_state_tool_arguments_allowed(self):
        for cmd in ("python3 devenv_state.py dev off --cwd /p",
                    "python3 devenv_state.py lean on --cwd /p/off/lean"):
            with self.subTest(cmd=cmd):
                self.assertAllowed(self.bash(cmd))

    def test_shell_forms_of_lean_off_denied(self):
        """Forms the shell turns into `devenv_state.py lean off` (review table): the command is normalized
        (line continuations joined, lower case, quotes, `$`, backslashes, backticks, braces and commas
        dropped, brace lists expanded) before the words are compared."""
        for cmd in ("python3 scripts/devenv_state.py lean \\\noff --cwd /p",
                    "python3 scripts/devenv_state.py lean o\\\nff",
                    "python3 scripts/devenv_state.py lean $'off'",
                    "python3 scripts/devenv_state.p[y] lean off",
                    "python3 scripts/devenv_stat?.py lean off",
                    "python3 scripts/devenv_state.py lean of{f,}",
                    "python3 scripts/devenv_state.py lean of{f,f}",
                    "python3 scripts/devenv_state.py lean o{f{f,},}",
                    "python3 scripts/DEVENV_STATE.PY lean off",
                    "cd scripts && python3 -m devenv_state lean off",
                    "python3 scripts/devenv_state.py \"LEAN\" 'Off'",
                    "python3 scripts/devenv_state.py le\\an o\\ff",
                    "python3 scripts/devenv_state.py lean $(echo off)",
                    "echo off | xargs python3 scripts/devenv_state.py lean",
                    "sh -c 'python3 scripts/devenv_state.py lean off'"):
            with self.subTest(cmd=cmd):
                self.assertDenied(self.bash(cmd))

    def test_harmless_commands_allowed(self):
        for cmd in ("python3 scripts/devenv_state.py dev off",
                    "python3 scripts/devenv_state.py lean on",
                    "python3 scripts/devenv_state.py lean status",
                    'git log --grep "lean off"',
                    "echo lean off",
                    "python3 scripts/devenv_state.py lean on --cwd /work/coffee-shop"):
            with self.subTest(cmd=cmd):
                self.assertAllowed(self.bash(cmd))

    def test_accepted_false_positives_say_why(self):
        """The state tool's name with the words `lean` and `off` anywhere in one command is denied even when
        the command would not change the state; the reason says the command was judged a lean-mode end."""
        for cmd in ("python3 devenv_state.py dev off && echo lean",
                    "python3 devenv_state.py lean status; echo off",
                    "echo lean off; python3 devenv_state.py lean status",
                    'grep -n "lean off" scripts/devenv_state.py'):
            with self.subTest(cmd=cmd):
                reason = self.assertDenied(self.bash(cmd))
                self.assertIn("judged to end lean mode", reason)

    def test_dev_off_and_status_allowed(self):
        for cmd in ("devenv-py /x/scripts/devenv_state.py dev off --py /usr/bin/python3",
                    "python3 devenv_state.py lean status --cwd /tmp/off",
                    "python3 devenv_state.py lean on --cwd /work/off",
                    "python3 devenv_state.py lean offline",
                    "ls /tmp/lean/off devenv_state.py",
                    "grep -n 'lean off' README.md"):
            with self.subTest(cmd=cmd):
                self.assertAllowed(self.bash(cmd))

    def test_guard_deny_recorded(self):
        self.dev_on()
        self.assertDenied(self.bash("python3 devenv_state.py lean off"))
        rows = self.events()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["kind"], rows[0]["guard"]), ("read_deny", 1))

    def test_deny_reason_has_no_off_instruction(self):
        reasons = [
            self.assertDenied(self.read(self.text_file("big.txt", 500))),
            self.assertDenied(self.read(self.text_file("big.txt", 500), allow_large=True, limit=10)),
            self.assertDenied(self.payload("write", file_path=str(self.state))),
            self.assertDenied(self.bash("python3 devenv_state.py lean off")),
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertTrue(reason)
                self.assertIsNone(OFF_INSTRUCTION.search(reason), reason)


class FailureVisibilityTests(HookReadCase):
    def test_broken_stdin_not_hidden(self):
        for raw in ("not json", "[1, 2]", ""):
            with self.subTest(raw=raw):
                proc = self.run_hook(raw=raw)
                self.assertEqual(proc.returncode, 1)  # non-blocking, and not hidden behind exit 0
                self.assertNotIn("deny", proc.stdout)
                self.assertIn("hook_read", proc.stderr)

    def test_bad_argument_does_not_block(self):
        proc = self.run_hook(self.bash("ls"), args=["--no-such-option"])
        self.assertEqual(proc.returncode, 1)  # 2 would block the tool call
        self.assertTrue(proc.stderr.strip())

    def test_default_paths_follow_home(self):
        hook = load("hook_read")
        home = Path.home()
        self.assertEqual(hook.state_file(None), home / ".claude" / "state" / "lean-mode.json")
        self.assertEqual(hook.handoffs_dir(None), home / ".claude" / "handoffs")
        self.assertEqual(hook.user_layer(None), home / ".claude" / "devenv.local.md")
        self.assertEqual(hook.state_file(self.root), self.root / "lean-mode.json")
        self.assertEqual(hook.handoffs_dir(self.root), self.root / "handoffs")
        self.assertEqual(hook.user_layer(self.root), self.root / "devenv.local.md")


if __name__ == "__main__":
    unittest.main()
