"""context_budget.py through its own check script (context_budget_check.py, six cases on a temporary HOME)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "context_budget_check.py"


class ContextBudgetCheckTests(unittest.TestCase):
    def test_check_script_passes_all_cases(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("6 PASS / 0 FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
