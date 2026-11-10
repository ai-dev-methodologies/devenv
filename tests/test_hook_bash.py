"""Tests for scripts/hook_bash.py (PostToolUse Bash output replacement).

The hook runs as a subprocess with HOME and USERPROFILE pointed at a temporary folder and with `--root`
(lean and developer mode state, events.jsonl) and `--out-dir` (full outputs) inside it, so the real
~/.claude is never read or written. Lean mode is turned on and looked up for the same folder.
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
SCRIPTS = ROOT / "scripts"
HOOK = SCRIPTS / "hook_bash.py"
FIXTURES = ROOT / "tests" / "fixtures" / "hooks"
REQUIRED = {"stdout": str, "stderr": str, "interrupted": bool, "isImage": bool}


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


devenv_state = load("devenv_state")


def numbers(count):
    return "".join(f"{i}\n" for i in range(1, count + 1))


class HookCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "state"
        self.out = self.tmp / "data" / "bash-out"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()

    def fixture(self, name="bash_main.json", **response):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        data["cwd"] = str(self.proj)
        data["tool_response"].update(response)
        return data

    def lean_on(self):
        devenv_state.lean_set(True, str(self.proj), root=self.root)

    def dev_on(self):
        devenv_state.dev_set(True, root=self.root)

    def run_hook(self, payload, out_dir=None):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--out-dir", str(out_dir or self.out), "--root", str(self.root)],
            input=text.encode("utf-8"), capture_output=True, env=env, cwd=str(self.tmp), timeout=60)
        out = proc.stdout.decode("utf-8")
        return proc.returncode, (json.loads(out) if out.strip() else None), proc.stderr.decode("utf-8")

    def replaced(self, result):
        code, out, err = result
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(out, "expected a replacement")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return out["hookSpecificOutput"]["updatedToolOutput"]

    def untouched(self, result):
        code, out, err = result
        self.assertEqual(code, 0, err)
        self.assertTrue(out is None or "updatedToolOutput" not in out.get("hookSpecificOutput", {}), out)
        return out

    def saved(self):
        return sorted(self.out.glob("*.txt")) if self.out.is_dir() else []

    def events(self):
        path = self.root / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


class ReplaceTest(HookCase):
    def test_big_output_replaced_keeps_keys(self):
        self.lean_on()
        payload = self.fixture(stdout=numbers(3000))
        new = self.replaced(self.run_hook(payload))
        self.assertEqual(set(new), set(payload["tool_response"]))
        self.assertIs(new["noOutputExpected"], False)
        self.assertIs(new["interrupted"], False)
        self.assertIs(new["isImage"], False)
        self.assertEqual(new["stderr"], "")
        lines = new["stdout"].splitlines()
        self.assertEqual(lines[:40], [str(i) for i in range(1, 41)])
        self.assertNotIn("41", lines)
        self.assertIn("3000 lines/13893 bytes", new["stdout"])

    def test_required_four_keys_and_types(self):
        hook_bash = load("hook_bash")
        self.lean_on()
        payload = self.fixture(stdout=numbers(3000), stderr="warn\n" * 30)
        new = self.replaced(self.run_hook(payload))
        for key, kind in REQUIRED.items():
            self.assertIsInstance(new[key], kind, key)
        self.assertEqual(hook_bash.output_shape_problems(new, payload["tool_response"]), [])
        self.assertEqual(new["stderr"].splitlines(), ["warn"] * 10)

    def test_full_output_file_written(self):
        self.lean_on()
        payload = self.fixture(stdout=numbers(3000), stderr="err line\n")
        new = self.replaced(self.run_hook(payload))
        files = self.saved()
        self.assertEqual(len(files), 1)
        self.assertIn(str(files[0]), new["stdout"])
        self.assertEqual(files[0].read_text(encoding="utf-8"),
                         numbers(3000) + "[devenv] stderr:\n" + "err line\n")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    def test_lines_over_limit_replaced(self):
        self.lean_on()
        new = self.replaced(self.run_hook(self.fixture(stdout=numbers(201))))
        self.assertIn("201 lines/", new["stdout"])

    def test_bytes_over_limit_replaced(self):
        self.lean_on()
        new = self.replaced(self.run_hook(self.fixture(stdout="x" * 20000)))
        self.assertIn("1 lines/20000 bytes", new["stdout"])
        self.assertLess(len(new["stdout"].encode("utf-8")), 16384)

    def test_replacement_text_has_size_and_rerun_but_no_off_instruction(self):
        self.lean_on()
        new = self.replaced(self.run_hook(self.fixture(stdout=numbers(3000))))
        tail = new["stdout"].splitlines()[40:]
        self.assertEqual(len(tail), 2, tail)
        self.assertRegex(tail[0], r"^\[devenv\] output 3000 lines/13893 bytes replaced; full output: .+\.txt "
                                  r"\(read it with offset/limit\)$")
        self.assertIn("run the command again", tail[1])
        text = "\n".join(tail).replace(str(self.out), "<out>").lower().replace("offset", "")
        for word in ("lean", "off", "disable", "devenv_state"):
            self.assertNotIn(word, text)

    def test_replace_event_recorded(self):
        self.lean_on()
        self.dev_on()
        self.replaced(self.run_hook(self.fixture(stdout=numbers(3000))))
        recs = self.events()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "bash_replace")
        self.assertIs(recs[0]["lean_on"], True)
        self.assertEqual((recs[0]["replaced"], recs[0]["lines"], recs[0]["bytes"]), (1, 3000, 13893))

    def test_project_limit_applies(self):
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "devenv.local.md").write_text(
            "## lean-limits\nbash_lines: 10\nbash_head_lines: 3\n", encoding="utf-8")
        self.lean_on()
        new = self.replaced(self.run_hook(self.fixture(stdout=numbers(11))))
        self.assertEqual(new["stdout"].splitlines()[:3], ["1", "2", "3"])
        self.assertTrue(new["stdout"].splitlines()[3].startswith("[devenv] output 11 lines/"))

    def test_bad_limit_uses_default(self):
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "devenv.local.md").write_text(
            "## lean-limits\nbash_lines: many\n", encoding="utf-8")
        self.lean_on()
        self.untouched(self.run_hook(self.fixture(stdout=numbers(150))))
        self.replaced(self.run_hook(self.fixture(stdout=numbers(250))))


class UntouchedTest(HookCase):
    def test_small_output_untouched(self):
        self.lean_on()
        self.untouched(self.run_hook(self.fixture(stdout=numbers(200))))
        self.assertEqual(self.saved(), [])

    def test_subagent_untouched(self):
        self.lean_on()
        self.untouched(self.run_hook(self.fixture("bash_subagent.json", stdout=numbers(3000))))
        self.assertEqual(self.saved(), [])

    def test_lean_off_untouched(self):
        self.untouched(self.run_hook(self.fixture(stdout=numbers(3000))))
        devenv_state.lean_set(True, str(self.proj), root=self.root)
        devenv_state.lean_set(False, str(self.proj), root=self.root)
        self.untouched(self.run_hook(self.fixture(stdout=numbers(3000))))
        self.assertEqual(self.saved(), [])

    def test_folder_above_the_project_untouched(self):
        self.lean_on()
        payload = self.fixture(stdout=numbers(3000))
        payload["cwd"] = str(self.tmp)
        self.untouched(self.run_hook(payload))
        self.assertEqual(self.saved(), [])

    def test_image_output_untouched(self):
        self.lean_on()
        self.untouched(self.run_hook(self.fixture("bash_image.json", stdout="A" * 40000)))

    def test_interrupted_untouched(self):
        self.lean_on()
        self.untouched(self.run_hook(self.fixture("bash_interrupted.json", stdout=numbers(3000))))

    def test_empty_output_untouched(self):
        self.lean_on()
        self.untouched(self.run_hook(self.fixture(stdout="", stderr="")))

    def test_missing_required_key_untouched(self):
        self.lean_on()
        payload = self.fixture(stdout=numbers(3000))
        del payload["tool_response"]["isImage"]
        self.untouched(self.run_hook(payload))
        self.assertEqual(self.saved(), [])

    def test_unknown_state_warns_and_keeps_output(self):
        self.root.mkdir(parents=True)
        (self.root / "lean-mode.json").write_text("{not json", encoding="utf-8")
        devenv_state.dev_set(True, root=self.root)
        out = self.untouched(self.run_hook(self.fixture(stdout=numbers(3000))))
        self.assertIn("lean state unknown", out["systemMessage"])
        recs = self.events()
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["kind"], recs[0]["lean_on"], recs[0]["replaced"], recs[0]["state_unknown"]),
                         ("bash_replace", None, 0, 1))

    def test_save_failure_keeps_output_and_warns(self):
        self.lean_on()
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        out = self.untouched(self.run_hook(self.fixture(stdout=numbers(3000)), out_dir=blocker / "bash-out"))
        self.assertIn("full output could not be saved", out["systemMessage"])

    def test_bad_stdin_json_not_masked(self):
        self.lean_on()
        code, out, err = self.run_hook("{broken")
        self.assertNotEqual(code, 0)
        self.assertIsNone(out)
        self.assertIn("hook_bash", err)


class PruneTest(HookCase):
    def test_prune_keeps_50(self):
        self.lean_on()
        self.out.mkdir(parents=True)
        old = [self.out / f"20200101T0000{i:02d}000000Z-0000{i:04x}.txt" for i in range(55)]
        for path in old:
            path.write_text("old\n", encoding="utf-8")
        other = self.out / "keep-me.log"
        other.write_text("not ours\n", encoding="utf-8")
        new = self.replaced(self.run_hook(self.fixture(stdout=numbers(3000))))
        files = self.saved()
        self.assertEqual(len(files), 50)
        self.assertTrue(other.is_file())
        self.assertTrue(any(str(f) in new["stdout"] for f in files))
        self.assertEqual(files[0].name, old[6].name)


class ShapeCheckTest(unittest.TestCase):
    def setUp(self):
        self.hook_bash = load("hook_bash")
        self.original = {"stdout": "a\n", "stderr": "", "interrupted": False, "isImage": False,
                         "noOutputExpected": False}

    def test_good_shape_has_no_problems(self):
        self.assertEqual(self.hook_bash.output_shape_problems(dict(self.original), self.original), [])
        self.assertEqual(self.hook_bash.output_shape_problems(
            {"stdout": "", "stderr": "", "interrupted": False, "isImage": False}), [])

    def test_each_missing_or_mistyped_key_is_reported(self):
        for key in REQUIRED:
            broken = dict(self.original)
            del broken[key]
            self.assertTrue(self.hook_bash.output_shape_problems(broken), key)
            broken = dict(self.original, **{key: 1})
            self.assertTrue(self.hook_bash.output_shape_problems(broken), key)
        self.assertTrue(self.hook_bash.output_shape_problems("text"))
        self.assertTrue(self.hook_bash.output_shape_problems({"stdout": "only"}))

    def test_dropped_or_changed_original_key_is_reported(self):
        dropped = dict(self.original)
        del dropped["noOutputExpected"]
        self.assertTrue(self.hook_bash.output_shape_problems(dropped, self.original))
        changed = dict(self.original, noOutputExpected=True)
        self.assertTrue(self.hook_bash.output_shape_problems(changed, self.original))

    def test_build_replacement_passes_shape_check(self):
        new = self.hook_bash.build_replacement(dict(self.original, stdout=numbers(500)), "/x/out.txt", 40,
                                               16384, 500, 1892)
        self.assertEqual(self.hook_bash.output_shape_problems(new, self.original), [])


if __name__ == "__main__":
    unittest.main()
