"""Tests for scripts/rules_layer.py (rule layers: engine defaults -> user layer -> project layer).

Every test uses a temporary HOME and temporary repositories, so real rules files are never read.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "rules_layer.py"
HANDOFF_STATE = ROOT / "scripts" / "handoff_state.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "state"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
OUTSIDE = "WARNING: rules file section outside override points:"
NEW = ".claude/devenv.local.md"
LEGACY = ".claude/handoff-rules.local.md"
BOTH_PROJECT = ("WARNING: both .claude/devenv.local.md and .claude/handoff-rules.local.md exist; "
                "using devenv.local.md")
BOTH_USER = ("WARNING: both ~/.claude/devenv.local.md and ~/.claude/handoff-rules.local.md exist; "
             "using devenv.local.md")
CYCLE = ("plan-lane", "approval-points", "critic-lanes", "external-critic", "judge-conditions",
         "review-rounds", "grading", "ledger", "dispatch-check", "priority-queue", "lane-ledger",
         "limits", "naming", "adapters")


def load():
    spec = importlib.util.spec_from_file_location("rules_layer_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        (self.home / ".claude" / "handoffs").mkdir(parents=True)
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        self.rl = load()
        self.state_script = HANDOFF_STATE

    def use_scripts_without_budget(self):
        """Run a copy of scripts/ without context_budget*.py, so two runs print no budget line with its clock."""
        folder = self.tmp / "engine-scripts"
        folder.mkdir()
        for path in (ROOT / "scripts").glob("*.py"):
            if not path.name.startswith("context_budget"):
                (folder / path.name).write_bytes(path.read_bytes())
        self.state_script = folder / "handoff_state.py"

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in (
            "HANDOFF_CHECKER", "HANDOFF_LOG", "CLAUDE_CODE_SESSION_ID", "HANDOFF_CALLER")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8",
                   HANDOFF_LOG=str(self.tmp / "runs.jsonl"))
        return env

    def git(self, *args, repo=None):
        return subprocess.run(["git", *GIT_ID, "-C", str(repo or self.repo), *args], capture_output=True,
                              text=True, encoding="utf-8", env=self.env(), check=True).stdout.strip()

    def init_repo(self, exclude_claude=True):
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_bytes(b"a\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        if exclude_claude:  # rules files stay out of `status:` so two runs can be compared byte for byte
            with (self.repo / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(".claude/\n")

    def put(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def run_state(self, cwd=None):
        return subprocess.run([sys.executable, str(self.state_script), str(cwd or self.repo)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=self.env(), cwd=str(self.tmp))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env(), cwd=str(self.tmp))

    @staticmethod
    def starting(text, prefix):
        return [ln for ln in text.splitlines() if ln.startswith(prefix)]

    def one_path(self, out, prefix):
        found = self.starting(out, prefix)
        self.assertEqual(len(found), 1, out)
        return found[0][len(prefix):].strip()


class ProjectLayerTests(Case):
    def test_devenv_local_preferred(self):
        self.init_repo()
        new = self.put(self.repo / NEW, "# rules\n\n## values\n- cap: 40 KB\n")
        user_new = self.put(self.home / NEW, "# mine\n")
        self.assertEqual(self.rl.project_file(self.repo), (new, []))
        self.assertEqual(self.rl.user_file(self.home), (user_new, []))
        proc = self.run_state()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(Path(self.one_path(proc.stdout, "rules file:")), new)
        self.assertEqual(Path(self.one_path(proc.stdout, "user rules file:")), user_new)
        self.assertEqual(self.starting(proc.stdout, "WARNING"), [])

    def test_legacy_fallback_stdout_identical(self):
        """With only the old file names, stdout is what the script printed before the rules layer existed:
        the old path on the two rules lines and one warning per section outside the four handoff points,
        `lean-limits` included (the old file is handoff's own)."""
        self.use_scripts_without_budget()
        self.init_repo()
        plain = self.run_state()
        self.assertEqual(plain.returncode, 0, plain.stderr)
        legacy = self.put(self.repo / LEGACY, (FIXTURES / "rules_outside.md").read_text(encoding="utf-8")
                          + "\n## lean-limits\nread_lines: 10\n")
        user_legacy = self.put(self.home / LEGACY, "# mine\n\n## anything-goes\n")
        expected = plain.stdout.replace("user rules file: none\n", f"user rules file: {user_legacy}\n", 1)
        expected = expected.replace("\nrules file: none\n", f"\nrules file: {legacy}\n"
                                    f"{OUTSIDE} random-notes\n{OUTSIDE} other-topic: extra\n"
                                    f"{OUTSIDE} lean-limits\n", 1)
        self.assertNotEqual(expected, plain.stdout)
        got = self.run_state()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, expected)
        self.assertEqual(self.rl.project_file(self.repo), (legacy, []))
        self.assertEqual(self.rl.user_file(self.home), (user_legacy, []))

    def test_both_files_warns(self):
        self.init_repo()
        new = self.put(self.repo / NEW, "# new\n")
        self.put(self.repo / LEGACY, "# old\n")
        user_new = self.put(self.home / NEW, "# new\n")
        self.put(self.home / LEGACY, "# old\n")
        self.assertEqual(self.rl.project_file(self.repo), (new, [BOTH_PROJECT]))
        self.assertEqual(self.rl.user_file(self.home), (user_new, [BOTH_USER]))
        out = self.run_state().stdout.splitlines()
        i = out.index(f"user rules file: {user_new}")
        self.assertEqual(out[i + 1], BOTH_USER)
        j = out.index(f"rules file: {new}")
        self.assertEqual(out[j + 1], BOTH_PROJECT)
        self.assertEqual([ln for ln in out if ln.startswith("WARNING")], [BOTH_USER, BOTH_PROJECT])

    def test_linked_worktree_main_fallback(self):
        self.init_repo()
        tree = self.tmp / "linked"
        self.git("worktree", "add", "-q", "-b", "side", str(tree))
        main_new = self.put(self.repo / NEW, "# main\n")
        self.assertEqual(self.rl.project_file(tree, self.repo), (main_new, []))
        self.assertEqual(Path(self.one_path(self.run_state(cwd=tree).stdout, "rules file:")), main_new)
        # a new file in the main tree wins over an old file in the linked tree
        self.put(tree / LEGACY, "# old\n")
        self.assertEqual(self.rl.project_file(tree, self.repo), (main_new, [BOTH_PROJECT]))
        # the linked tree's own new file wins over the main tree's
        tree_new = self.put(tree / NEW, "# tree\n")
        self.assertEqual(self.rl.project_file(tree, self.repo), (tree_new, [BOTH_PROJECT]))
        # only old files: the linked tree first, then the main tree (the order the script always used)
        for p in (tree_new, main_new):
            p.unlink()
        main_legacy = self.put(self.repo / LEGACY, "# main old\n")
        self.assertEqual(self.rl.project_file(tree, self.repo), (tree / LEGACY, []))
        (tree / LEGACY).unlink()
        self.assertEqual(self.rl.project_file(tree, self.repo), (main_legacy, []))
        self.assertEqual(self.rl.project_file(self.tmp / "nowhere"), (None, []))


class SectionTests(Case):
    def test_unknown_section_warns(self):
        self.init_repo()
        path = self.put(self.repo / NEW, (
            "# rules\n\n## values: caps\n- x\n\n## lean-limits\nread_lines: 50\n\n## plan-lane\nown\n\n"
            "## random-notes\ntext\n\n## lean-limit\nread_lines: 7\n\n```\n## inside-a-fence\n```\n"
            "### deeper headings are not sections\n"))
        expected = [f"{OUTSIDE} random-notes", f"{OUTSIDE} lean-limit"]
        self.assertEqual(self.rl.section_warnings(path), expected)
        self.assertEqual(self.starting(self.run_state().stdout, OUTSIDE), expected)
        # a known section applies, the misspelt one does not
        values, warns = self.rl.limits([path])
        self.assertEqual(values["read_lines"], 50)
        self.assertEqual(warns, [])

    def test_cycle_ids_are_known(self):
        self.assertEqual(tuple(self.rl.CYCLE_IDS), CYCLE)
        self.assertEqual(tuple(self.rl.HANDOFF_IDS), ("runner-completion", "state-files", "forbidden-writes",
                                                      "values"))
        self.assertEqual(set(self.rl.KNOWN_SECTIONS),
                         set(CYCLE) | set(self.rl.HANDOFF_IDS) | {"lean-limits", "doctor-settings"})
        self.assertEqual(len(self.rl.KNOWN_SECTIONS), 20)
        path = self.put(self.repo / NEW, "".join(f"## {sid}\ntext\n\n" for sid in self.rl.KNOWN_SECTIONS))
        self.assertEqual(self.rl.section_warnings(path), [])
        # the old file name keeps the four handoff points only
        legacy = self.put(self.repo / LEGACY, "".join(f"## {sid}\ntext\n\n" for sid in self.rl.KNOWN_SECTIONS))
        self.assertEqual(self.rl.section_warnings(legacy), [f"{OUTSIDE} {sid}" for sid in CYCLE]
                         + [f"{OUTSIDE} lean-limits", f"{OUTSIDE} doctor-settings"])

    def test_bad_limit_falls_back_with_warning(self):
        defaults = dict(self.rl.DEFAULT_LIMITS)
        self.assertEqual(defaults, {"read_bytes": 32768, "read_lines": 400, "bash_bytes": 16384,
                                    "bash_lines": 200, "bash_head_lines": 40, "return_lines": 30})
        self.assertEqual(self.rl.limits([]), (defaults, []))
        self.assertEqual(self.rl.limits([None, self.tmp / "missing.md"]), (defaults, []))
        user = self.put(self.home / NEW, "## lean-limits\n- read_lines: 100\nbash_lines: 300\n")
        values, warns = self.rl.limits([user])
        self.assertEqual((values["read_lines"], values["bash_lines"], warns), (100, 300, []))
        for bad in ("0", "-3", "abc", "1.5", "", "12 lines"):
            with self.subTest(value=bad):
                proj = self.put(self.repo / NEW, f"## lean-limits\nread_lines: {bad}\nreturn_lines: 25\n")
                values, warns = self.rl.limits([user, proj])
                self.assertEqual(values["read_lines"], 100, "a bad value keeps the lower layer's value")
                self.assertEqual(values["return_lines"], 25)
                self.assertEqual(len(warns), 1, warns)
                self.assertTrue(warns[0].startswith(f"WARNING: lean-limits read_lines in {proj}: "), warns)
                values, warns = self.rl.limits([proj])
                self.assertEqual(values["read_lines"], 400, "with no lower layer the engine default stays")
        proj = self.put(self.repo / NEW, "## lean-limits\nread_line: 5\n")
        values, warns = self.rl.limits([proj])
        self.assertEqual(values, defaults)
        self.assertEqual(warns, [f"WARNING: lean-limits unknown key in {proj}: read_line"])
        legacy = self.put(self.repo / LEGACY, "## lean-limits\nread_lines: 5\n")
        self.assertEqual(self.rl.limits([legacy]), (defaults, []), "the old file name never sets limits")

    def test_limit_over_the_cap_is_clipped_with_warning(self):
        self.assertEqual(self.rl.LIMIT_CAP_FACTOR, 4)
        caps = {key: 4 * value for key, value in self.rl.DEFAULT_LIMITS.items()}
        at_cap = self.put(self.repo / NEW, "## lean-limits\n" + "".join(f"{k}: {v}\n" for k, v in caps.items()))
        self.assertEqual(self.rl.limits([at_cap]), (caps, []), "exactly the cap is kept")
        proj = self.put(self.repo / NEW, "## lean-limits\nread_bytes: 999999999\nread_lines: 1601\n"
                                         "return_lines: 31\n")
        values, warns = self.rl.limits([proj])
        self.assertEqual((values["read_bytes"], values["read_lines"], values["return_lines"]), (131072, 1600, 31))
        self.assertEqual(warns, [
            f"WARNING: lean-limits read_bytes in {proj}: 999999999 is over the cap 131072 (4 x the default); "
            f"using 131072",
            f"WARNING: lean-limits read_lines in {proj}: 1601 is over the cap 1600 (4 x the default); using 1600"])


class IgnoreStateTests(Case):
    def test_ignore_state_three_ways(self):
        rl = self.rl
        path = self.repo / NEW
        self.assertEqual(rl.ignore_state(path)[0], rl.NOT_APPLICABLE, "no file")
        plain = self.put(self.tmp / "plain" / NEW, "# rules\n")
        self.assertEqual(rl.ignore_state(plain)[0], rl.NOT_APPLICABLE, "not a git repository")
        self.init_repo(exclude_claude=False)
        self.put(path, "# rules\n")
        self.assertEqual(rl.ignore_state(path)[0], rl.NOT_IGNORED)
        self.put(self.repo / ".gitignore", ".claude/devenv.local.md\n")
        state, detail = rl.ignore_state(path)
        self.assertEqual(state, rl.IGNORED)
        self.assertIn(".gitignore", detail)
        self.git("add", "-f", NEW)  # a tracked file is published whatever .gitignore says
        self.assertEqual(rl.ignore_state(path)[0], rl.NOT_IGNORED)
        self.assertEqual(rl.ignore_state(None)[0], rl.NOT_APPLICABLE)


class InitProjectTests(Case):
    def test_init_project_suggests_gitignore(self):
        self.init_repo(exclude_claude=False)
        proc = self.run_cli("init-project", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        target = self.repo / NEW
        self.assertTrue(target.is_file())
        self.assertEqual(self.rl.section_warnings(target), [])
        self.assertEqual(self.rl.sections(target), [], "the sample has no live section")
        out = proc.stdout.splitlines()
        self.assertIn(f"created: {target}", out)
        self.assertIn("gitignore: not ignored", out)
        self.assertIn("  .claude/devenv.local.md", out)
        self.assertIn(f"check: git -C {self.repo} check-ignore -v .claude/devenv.local.md", out)
        self.assertFalse((self.repo / ".gitignore").exists(), ".gitignore is changed only on request")
        # already ignored: no suggestion
        target.unlink()
        self.put(self.repo / ".gitignore", "*.local.md\n")
        proc = self.run_cli("init-project", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gitignore: ignored", proc.stdout)
        self.assertNotIn("suggest:", proc.stdout)
        # explicit option: the line is appended (after a missing final newline) and checked
        target.unlink()
        self.put(self.repo / ".gitignore", "build/")
        proc = self.run_cli("init-project", str(self.repo), "--append-gitignore")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.repo / ".gitignore").read_bytes(), b"build/\n.claude/devenv.local.md\n")
        self.assertEqual(self.rl.ignore_state(target)[0], self.rl.IGNORED)
        self.assertIn("gitignore: appended .claude/devenv.local.md", proc.stdout)
        # not a git repository: only that fact
        plain = self.tmp / "plain"
        plain.mkdir()
        proc = self.run_cli("init-project", str(plain))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((plain / NEW).is_file())
        self.assertIn("gitignore: not a git repository", proc.stdout)
        self.assertNotIn("suggest:", proc.stdout)
        # usage errors
        self.assertEqual(self.run_cli("init-project", str(self.tmp / "missing")).returncode, 2)
        self.assertEqual(self.run_cli("init-project").returncode, 2)

    def test_init_project_never_overwrites(self):
        self.init_repo()
        target = self.put(self.repo / NEW, "# mine\n\n## values\n- keep me\n")
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        proc = self.run_cli("init-project", str(self.repo), "--append-gitignore")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        self.assertIn(f"exists: {target} (not overwritten)", proc.stdout)
        self.assertIn("gitignore: ignored", proc.stdout, ".git/info/exclude counts")
        self.assertFalse((self.repo / ".gitignore").exists(), "nothing to append when already ignored")

    def test_init_project_warns_about_the_old_file(self):
        self.init_repo()
        self.put(self.repo / LEGACY, "## values\n- old\n")
        proc = self.run_cli("init-project", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARNING: .claude/handoff-rules.local.md exists; the new file now takes its place, "
                      "so move its sections into .claude/devenv.local.md", proc.stdout)

    def test_init_project_from_a_subfolder_uses_the_toplevel(self):
        self.init_repo()
        sub = self.repo / "deep" / "er"
        sub.mkdir(parents=True)
        proc = self.run_cli("init-project", str(sub))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.repo / NEW).is_file())
        self.assertFalse((sub / NEW).exists())


if __name__ == "__main__":
    unittest.main()
