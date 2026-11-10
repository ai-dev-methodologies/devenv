"""Hook registrations in hooks/hooks.json and the bin/devenv-py wrapper.

Registrations: every handler is exec form with the user's interpreter (`${user_config.python}`), points at a
script that exists, has no `onFailure`, and the set is exactly the five hooks the engine ships. Each
registration also runs once with its placeholders replaced (scripts and a temporary data folder, `--root`
and HOME in a temporary folder) on an input that must pass, so an argument the script does not accept fails
here.

Wrapper: `devenv-py <script> <args...> --py <interpreter>` (or `--py <interpreter>` first) runs the script
with that interpreter only and returns its exit code; a missing `--py` or a path that is not an executable
file fails with one stderr line and a non-zero exit, and never falls back to another interpreter.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = ROOT / "hooks" / "hooks.json"
WRAPPER = ROOT / "bin" / "devenv-py"
FIXTURES = ROOT / "tests" / "fixtures" / "hooks"
PY = "${user_config.python}"
PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
PLUGIN_DATA = "${CLAUDE_PLUGIN_DATA}"


def load_module(name):
    spec = importlib.util.spec_from_file_location(f"{name}_registration_test", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HANDBACK_TOOL = load_module("hook_return").HANDBACK_TOOL

EXPECTED = sorted([
    ("SessionStart", None, [f"{PLUGIN_ROOT}/scripts/hook_session_start.py"]),
    ("PreToolUse", "Read|Write|Edit|Bash", [f"{PLUGIN_ROOT}/scripts/hook_read.py"]),
    ("PreToolUse", HANDBACK_TOOL, [f"{PLUGIN_ROOT}/scripts/hook_return.py", "handback", "--data", PLUGIN_DATA]),
    ("PostToolUse", "Bash", [f"{PLUGIN_ROOT}/scripts/hook_bash.py", "--out-dir", f"{PLUGIN_DATA}/bash-out"]),
    ("SubagentStop", None, [f"{PLUGIN_ROOT}/scripts/hook_return.py", "stop"]),
], key=repr)


def registrations():
    """[(event, matcher or None, handler dict)] in file order."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    found = []
    for event, groups in data["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                found.append((event, group.get("matcher"), handler))
    return found


class RegistrationTests(unittest.TestCase):
    def test_registration_set_is_exact(self):
        got = sorted(((event, matcher, handler.get("args")) for event, matcher, handler in registrations()),
                     key=repr)
        self.assertEqual(got, EXPECTED)

    def test_every_handler_is_exec_form_with_the_user_interpreter(self):
        for event, matcher, handler in registrations():
            with self.subTest(event=event, matcher=matcher):
                self.assertEqual(handler.get("type"), "command")
                self.assertEqual(handler.get("command"), PY)
                self.assertIsInstance(handler.get("args"), list)
                self.assertTrue(all(isinstance(a, str) for a in handler["args"]))

    def test_no_failure_policy_and_no_background_run(self):
        for event, matcher, handler in registrations():
            with self.subTest(event=event, matcher=matcher):
                self.assertNotIn("onFailure", handler)
                self.assertNotIn("async", handler)
                self.assertNotIn("asyncRewake", handler)

    def test_registered_scripts_exist(self):
        for event, matcher, handler in registrations():
            script = handler["args"][0]
            with self.subTest(event=event, script=script):
                self.assertTrue(script.startswith(PLUGIN_ROOT + "/scripts/"), script)
                self.assertTrue((ROOT / script[len(PLUGIN_ROOT) + 1:]).is_file(), script)

    def test_handback_matcher_is_the_report_tool(self):
        matchers = [m for e, m, h in registrations() if e == "PreToolUse" and "handback" in h["args"]]
        self.assertEqual(matchers, ["SubagentHandback"])


class RegistrationRunTests(unittest.TestCase):
    """Each registration, placeholders replaced, accepts its arguments and passes an input that must pass."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home, self.data, self.state, self.proj = (self.tmp / n for n in ("home", "data", "state", "proj"))
        for d in (self.home, self.data, self.state, self.proj):
            d.mkdir()

    def fixture(self, name):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        data["cwd"] = str(self.proj)
        return data

    def payload(self, event, matcher):
        if event == "SessionStart":
            return {"session_id": "registration-test", "cwd": str(self.proj), "hook_event_name": event,
                    "source": "startup"}
        if event == "PostToolUse":
            return self.fixture("bash_main.json")
        if event == "SubagentStop":
            return self.fixture("return_stop.json")
        if matcher == HANDBACK_TOOL:
            return self.fixture("return_handback.json")
        return self.fixture("read_bash.json")

    def test_each_registration_runs_and_passes(self):
        for event, matcher, handler in registrations():
            args = [a.replace(PLUGIN_ROOT, str(ROOT)).replace(PLUGIN_DATA, str(self.data)) for a in handler["args"]]
            with self.subTest(event=event, matcher=matcher):
                env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
                env.pop("HANDOFF_LOG", None)
                proc = subprocess.run([sys.executable, *args, "--root", str(self.state)],
                                      input=json.dumps(self.payload(event, matcher)), capture_output=True,
                                      text=True, encoding="utf-8", cwd=str(self.proj), env=env, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                self.assertEqual(proc.stdout.strip(), "", "lean is off: the hook passes with no output")


@unittest.skipIf(sys.platform == "win32", "bin/devenv-py is a POSIX sh wrapper (macOS and Linux)")
class WrapperTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.script = self.tmp / "echo_args.py"
        self.script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(int(sys.argv[-1]))\n",
                               encoding="utf-8")
        self.fake = self.tmp / "fake python"  # a space in the path must survive
        self.fake.write_text('#!/bin/sh\necho "fake:$#:$1"\nexit 9\n', encoding="utf-8")
        self.fake.chmod(0o755)

    def run_wrapper(self, *args, env=None):
        return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, encoding="utf-8",
                              env=env if env is not None else dict(os.environ), timeout=60)

    def assertFailsOneLine(self, proc):
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
        self.assertTrue(proc.stderr.startswith("devenv-py: "), proc.stderr)

    def test_wrapper_is_an_executable_sh_script(self):
        self.assertTrue(WRAPPER.read_text(encoding="utf-8").startswith("#!/bin/sh\n"))
        self.assertTrue(WRAPPER.stat().st_mode & stat.S_IXUSR)

    def test_trailing_py_runs_script_with_only_its_arguments(self):
        proc = self.run_wrapper(str(self.script), "a b", "$HOME", "", "--py", "0", "--py", sys.executable)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), ["a b", "$HOME", "", "--py", "0"])

    def test_leading_py_order_is_the_same(self):
        proc = self.run_wrapper("--py", sys.executable, str(self.script), "a", "b", "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), ["a", "b", "0"])

    def test_exit_code_is_passed_through(self):
        for code in ("1", "2", "3", "7"):
            with self.subTest(code=code):
                proc = self.run_wrapper(str(self.script), code, "--py", sys.executable)
                self.assertEqual(proc.returncode, int(code))

    def test_the_named_interpreter_is_used(self):
        proc = self.run_wrapper(str(self.script), "x", "--py", str(self.fake))
        self.assertEqual((proc.returncode, proc.stdout), (9, f"fake:2:{self.script}\n"))

    def test_missing_py_fails(self):
        env = dict(os.environ, CLAUDE_PLUGIN_OPTION_PYTHON=sys.executable)  # never used as a fallback
        for args in ((str(self.script), "0"), (str(self.script), "0", "--py"), ("--py",), ()):
            with self.subTest(args=args):
                self.assertFailsOneLine(self.run_wrapper(*args, env=env))

    def test_bad_interpreter_path_fails(self):
        not_exec = self.tmp / "plain.txt"
        not_exec.write_text("x", encoding="utf-8")
        for py in (str(self.tmp / "missing" / "python3"), str(not_exec), str(self.tmp), "python3", ""):
            with self.subTest(py=py):
                self.assertFailsOneLine(self.run_wrapper(str(self.script), "0", "--py", py))

    def test_py_without_script_fails(self):
        self.assertFailsOneLine(self.run_wrapper("--py", sys.executable))


if __name__ == "__main__":
    unittest.main()
