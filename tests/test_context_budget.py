"""context_budget.py through its own check script (context_budget_check.py, six cases on a temporary HOME)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "context_budget_check.py"
TOOL = ROOT / "scripts" / "context_budget.py"
HANGUL = re.compile("[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")


def run(script, *args, home=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if home is not None:
        env.update(HOME=str(home), USERPROFILE=str(home))
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120)


class ContextBudgetCheckTests(unittest.TestCase):
    def test_check_script_passes_all_cases(self):
        proc = run(CHECK)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("6 PASS / 0 FAIL", proc.stdout)
        self.assertEqual("summary: 6 cases, 6 PASS / 0 FAIL", proc.stdout.splitlines()[-1])
        self.assertIsNone(HANGUL.search(proc.stdout), proc.stdout)

    def test_check_script_refuses_arguments_in_english(self):
        proc = run(CHECK, "extra")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual("[REFUSED] takes no arguments", proc.stdout.strip())


class ContextBudgetOutputTests(unittest.TestCase):
    def test_table_rows_and_total_are_english(self):
        with tempfile.TemporaryDirectory() as t:
            home, cwd = Path(t) / "home", Path(t) / "proj"
            (home / ".claude").mkdir(parents=True)
            cwd.mkdir()
            (cwd / "CLAUDE.md").write_bytes(b"x" * 10)
            proc = run(TOOL, "--cwd", str(cwd), "--table", home=home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertTrue(any(ln.startswith("missing\t") and ln.endswith("CLAUDE.local.md") for ln in lines), lines)
        self.assertIn("10\ttotal (cap=49152)", lines)
        self.assertIsNone(HANGUL.search(proc.stdout), proc.stdout)

    def test_selfcheck_settings_is_english_and_keeps_its_exit_code(self):
        for count, code in ((1, 0), (2, 1)):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as t:
                home = Path(t)
                (home / ".claude").mkdir()
                hook = {"hooks": [{"type": "command", "command": "python3 ~/.claude/scripts/context_budget.py --record"}]}
                (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [hook] * count}}),
                                                                 encoding="utf-8")
                proc = run(TOOL, "--selfcheck-settings", home=home)
                self.assertEqual(proc.returncode, code)
                self.assertEqual(f"SessionStart context_budget.py entries: {count}", proc.stdout.strip())


if __name__ == "__main__":
    unittest.main()
