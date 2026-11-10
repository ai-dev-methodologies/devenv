"""Cross-platform code conditions for the scripts and the installer (spec sections 10, 13.4, A7).

Checked on the AST so comments and strings never trigger a finding:
  - the source parses as Python 3.9 syntax
  - every open() / .open() / read_text passes encoding="utf-8"
  - every write uses open with a w/a/x mode and passes newline="\\n"; write_text is refused
    because its newline= argument needs Python 3.10
  - subprocess calls take a list as first argument and never shell=True
  - no os.path (pathlib only)
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "check_handoff.py": ROOT / "scripts" / "check_handoff.py",
    "handoff_state.py": ROOT / "scripts" / "handoff_state.py",
    "run_log.py": ROOT / "scripts" / "run_log.py",
    "run_report.py": ROOT / "scripts" / "run_report.py",
    "rules_layer.py": ROOT / "scripts" / "rules_layer.py",
}
SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def kw(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def is_const(node, value):
    return isinstance(node, ast.Constant) and node.value == value


def call_name(call):
    """(receiver name or None, attribute/function name) of a Call."""
    f = call.func
    if isinstance(f, ast.Name):
        return None, f.id
    if isinstance(f, ast.Attribute):
        recv = f.value.id if isinstance(f.value, ast.Name) else None
        return recv, f.attr
    return None, None


def open_mode(call):
    mode = kw(call, "mode")
    if mode is None:
        index = 1 if call_name(call)[0] in (None, "io") else 0  # open(f, mode) vs path.open(mode)
        mode = call.args[index] if len(call.args) > index else None
    return mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else ""


def is_list_expr(node):
    if isinstance(node, (ast.List, ast.ListComp)):
        return True
    return isinstance(node, ast.BinOp) and (is_list_expr(node.left) or is_list_expr(node.right))


def scan(source):
    """Return a list of findings (strings) for one script's source."""
    findings = []
    try:
        ast.parse(source, feature_version=(3, 9))
    except SyntaxError as exc:
        findings.append(f"line {exc.lineno}: not Python 3.9 syntax ({exc.msg})")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            recv, name = call_name(node)
            where = f"line {node.lineno}"
            is_open = name == "open" and (recv is None or recv != "webbrowser")
            if name == "write_text":
                findings.append(f'{where}: write_text() cannot take newline= on Python 3.9; use open("w", ..., newline="\\n")')
            elif is_open and "b" in open_mode(node):
                pass  # binary mode takes bytes: no encoding, no newline translation
            elif is_open or name == "read_text":
                if not is_const(kw(node, "encoding"), "utf-8"):
                    findings.append(f'{where}: {name}() lacks encoding="utf-8"')
                writes = is_open and set(open_mode(node)) & set("wax")
                if writes and not is_const(kw(node, "newline"), "\n"):
                    findings.append(f'{where}: {name}() write lacks newline="\\n"')
            if recv == "subprocess" and name in SUBPROCESS_FUNCS:
                if not node.args or not is_list_expr(node.args[0]):
                    findings.append(f"{where}: subprocess.{name} first argument is not a list")
                if is_const(kw(node, "shell"), True):
                    findings.append(f"{where}: subprocess.{name} uses shell=True")
            if recv == "os" and name in ("system", "popen"):
                findings.append(f"{where}: os.{name} is a shell call")
        elif isinstance(node, ast.Attribute):
            if node.attr == "path" and isinstance(node.value, ast.Name) and node.value.id == "os":
                findings.append(f"line {node.lineno}: os.path used")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "os.path" or (node.module == "os" and any(a.name == "path" for a in node.names)):
                findings.append(f"line {node.lineno}: imports os.path")
        elif isinstance(node, ast.Import):
            if any(a.name == "os.path" for a in node.names):
                findings.append(f"line {node.lineno}: imports os.path")
    return findings


class ScannerSelfTest(unittest.TestCase):
    """The scanner itself must catch each violation, or a green result means nothing."""

    def test_A7_scanner_flags_each_violation(self):
        bad = {
            "read_text without encoding": "p.read_text()",
            "read_text wrong encoding": 'p.read_text(encoding="latin-1")',
            "open without encoding": 'open("f")',
            "write_text without newline": 'p.write_text("x", encoding="utf-8")',
            "write_text with newline (3.10+)": 'p.write_text("x", encoding="utf-8", newline="\\n")',
            "match statement (3.10+)": "match x:\n    case 1:\n        pass\n",
            "open write without newline": 'open("f", "w", encoding="utf-8")',
            "path.open write without newline": 'p.open("w", encoding="utf-8")',
            "path.open text read without encoding": 'p.open("r")',
            "open text append without encoding": 'open("f", mode="a", newline="\\n")',
            "shell=True": 'import subprocess\nsubprocess.run(["a"], shell=True)',
            "string command": 'import subprocess\nsubprocess.run("a b")',
            "os.path attribute": "import os\nos.path.join('a', 'b')",
            "from os import path": "from os import path",
            "from os.path import join": "from os.path import join",
        }
        for label, src in bad.items():
            with self.subTest(case=label):
                self.assertTrue(scan(src), f"scanner missed: {label}")

    def test_A7_scanner_accepts_conforming_code(self):
        good = (
            "import subprocess\n"
            "from pathlib import Path\n"
            'p = Path("x")\n'
            'p.read_text(encoding="utf-8")\n'
            'with p.open("w", encoding="utf-8", newline="\\n") as f:\n'
            '    f.write("x")\n'
            'open("f", encoding="utf-8")\n'
            'open("f", "w", encoding="utf-8", newline="\\n")\n'
            'subprocess.run(["git", "status"], capture_output=True)\n'
            'p.open("rb")\n'
            'open("f", mode="wb")\n'
        )
        self.assertEqual(scan(good), [])


class CrossPlatformTest(unittest.TestCase):
    def check(self, filename):
        findings = scan(SCRIPTS[filename].read_text(encoding="utf-8"))
        self.assertEqual(findings, [], f"{filename}:\n  " + "\n  ".join(findings))

    def test_A7_check_handoff_conditions(self):
        self.check("check_handoff.py")

    def test_A7_handoff_state_conditions(self):
        self.check("handoff_state.py")

    def test_A7_run_log_conditions(self):
        self.check("run_log.py")

    def test_A7_run_report_conditions(self):
        self.check("run_report.py")

    def test_A7_rules_layer_conditions(self):
        self.check("rules_layer.py")

    def test_A7_scripts_exist(self):
        for name, path in SCRIPTS.items():
            self.assertTrue(path.is_file(), f"{name} missing")


if __name__ == "__main__":
    unittest.main()
