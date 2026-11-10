"""Tests for scripts/devenv_state.py (lean and developer mode state files).

Every test uses a temporary state folder (`root=` or `--root`) or a temporary HOME, so the
real state files are never read or written.
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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "devenv_state.py"
HANDOFF_STATE = ROOT / "scripts" / "handoff_state.py"
SINCE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
SINCE = "2026-11-10T01:02:03Z"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def load_state():
    spec = importlib.util.spec_from_file_location("devenv_state_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.root = self.tmp / "state"
        self.lean_file = self.root / "lean-mode.json"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.ds = load_state()

    def put(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            data = json.dumps(data)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.lean_file.write_bytes(data)

    def read(self):
        return json.loads(self.lean_file.read_text(encoding="utf-8"))

    def status(self, cwd, session_id=None):
        return self.ds.lean_status(str(cwd), session_id, root=self.root)

    def assertState(self, st, on, scope, since=None):
        self.assertEqual((st["on"], st["scope"], st["since"]), (on, scope, since), st)


class LegacyFormatTests(StateCase):
    def test_legacy_project_matches_at_or_inside_the_project_key(self):
        self.put({"on": True, "since": SINCE, "project": self.proj.as_posix()})
        self.assertState(self.status(self.proj), True, "project", SINCE)
        # outside git the project key is the cwd itself: a folder above it is another project
        self.assertState(self.status(self.proj / "sub" / "deep"), False, "none")
        self.assertState(self.status(self.tmp / "other"), False, "none")
        self.assertState(self.status(self.tmp / "projx"), False, "none")  # a name prefix is not a folder
        self.put({"on": True, "since": SINCE, "project": (self.proj / "sub").as_posix()})  # an older skill's cwd
        self.assertState(self.status(self.proj), True, "project", SINCE)

    def test_legacy_off_only_file(self):
        self.put({"on": False})
        st = self.status(self.proj)
        self.assertState(st, False, "none")
        self.assertIsNone(st["error"])

    def test_legacy_on_without_project_is_off(self):
        self.put({"on": True, "since": SINCE})
        self.assertState(self.status(self.proj), False, "none")

    def test_legacy_since_must_be_strict_utc(self):
        self.put({"on": True, "since": "yesterday", "project": self.proj.as_posix()})
        self.assertState(self.status(self.proj), True, "project", None)

    def test_missing_file_is_off(self):
        st = self.status(self.proj)
        self.assertState(st, False, "none")
        self.assertIsNone(st["error"])


class NewFormatTests(StateCase):
    def test_project_entry_at_or_inside_the_project_key(self):
        p = self.proj.as_posix()
        self.put({"on": False, "projects": {
            p: {"on": True, "since": SINCE},
            p + "/sub": {"on": False, "since": "2026-11-09T00:00:00Z"},
        }})
        self.assertState(self.status(self.proj), True, "project", SINCE)  # the key's own entry wins
        self.assertState(self.status(self.proj / "sub"), False, "project", "2026-11-09T00:00:00Z")
        self.assertState(self.status(self.proj / "a"), False, "none")  # an entry above the key does not count
        self.assertState(self.status(self.tmp), True, "project", SINCE)  # no own entry: one inside that is on
        self.put({"on": False, "projects": {p + "/sub": {"on": False, "since": SINCE}}})
        self.assertState(self.status(self.tmp), False, "project", SINCE)

    def test_session_entry_wins(self):
        p = self.proj.as_posix()
        self.put({"on": True, "since": SINCE, "project": p,
                  "projects": {p: {"on": True, "since": SINCE}},
                  "session": {"id": "s-1", "on": False, "since": SINCE, "project": p}})
        self.assertState(self.status(self.proj, "s-1"), False, "session", SINCE)
        self.assertState(self.status(self.tmp / "elsewhere", "s-1"), False, "none")  # another project
        self.assertState(self.status(self.proj, "s-2"), True, "project", SINCE)
        self.assertState(self.status(self.proj, None), True, "project", SINCE)

    def test_top_level_mirror_is_not_read_in_new_format(self):
        # A session-only "on" is mirrored to the top level; other sessions must not see it.
        p = self.proj.as_posix()
        self.put({"on": True, "since": SINCE, "project": p, "projects": {},
                  "session": {"id": "s-1", "on": True, "since": SINCE, "project": p}})
        self.assertState(self.status(self.proj, "s-2"), False, "none")
        self.assertState(self.status(self.proj, "s-1"), True, "session", SINCE)

    def test_dotdot_path_never_matches(self):
        p = self.proj.as_posix()
        self.put({"on": True, "since": SINCE, "project": p + "/../proj"})
        self.assertState(self.status(self.proj), False, "none")
        self.put({"on": False, "projects": {p + "/x/..": {"on": True, "since": SINCE}}})
        self.assertState(self.status(self.proj / "x"), False, "none")
        self.put({"on": False, "projects": {p: {"on": True, "since": SINCE}}})
        self.assertState(self.status(p + "/../proj"), False, "none")
        with self.assertRaises(ValueError):
            self.ds.lean_set(True, p + "/../proj", root=self.root)
        with self.assertRaises(ValueError):
            self.ds.lean_set(True, "relative/dir", root=self.root)

    def test_bad_json_is_unknown_scope(self):
        p = self.proj.as_posix()
        cases = {
            "empty": b"",
            "truncated": b'{"on": tr',
            "not utf-8": b'{"on": true, "project": "\xff"}',
            "list": b"[]",
            "empty object": b"{}",
            "on not bool": {"on": "yes"},
            "projects not dict": {"on": False, "projects": []},
            "entry not dict": {"on": False, "projects": {p: True}},
            "entry on not bool": {"on": False, "projects": {p: {"on": 1}}},
            "session not dict": {"on": False, "projects": {}, "session": "s-1"},
            "session id missing": {"on": False, "projects": {}, "session": {"on": True}},
            "session on not bool": {"on": False, "projects": {}, "session": {"id": "s", "on": None}},
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.put(data)
                st = self.status(self.proj, "s")
                self.assertState(st, None, "unknown")
                self.assertTrue(st["error"])

    def test_unreadable_file_is_unknown_scope(self):
        self.root.mkdir(parents=True)
        self.lean_file.mkdir()  # a folder where the file should be
        st = self.status(self.proj)
        self.assertState(st, None, "unknown")
        self.lean_file.rmdir()
        self.put({"on": False})
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            st = self.status(self.proj)
        self.assertState(st, None, "unknown")
        self.assertIn("denied", st["error"])


class SetTests(StateCase):
    def test_set_keeps_top_level_in_sync(self):
        p, q = self.proj.as_posix(), (self.tmp / "q").as_posix()
        self.ds.lean_set(True, p, root=self.root)
        data = self.read()
        self.assertIs(data["on"], True)
        self.assertEqual(data["project"], p)
        self.assertRegex(data["since"], SINCE_RE)
        self.assertEqual(data["projects"][p]["since"], data["since"])
        self.ds.lean_set(False, q, root=self.root)
        data = self.read()
        self.assertEqual((data["on"], data["project"]), (False, q))
        self.assertState(self.status(self.proj), True, "project", data["projects"][p]["since"])
        self.ds.lean_set(True, q, session_id="s-1", root=self.root)
        data = self.read()
        self.assertEqual((data["on"], data["project"], list(data["sessions"])), (True, q, ["s-1"]))
        self.assertEqual(data["since"], data["sessions"]["s-1"]["since"])

    def test_set_migrates_legacy_entry(self):
        a, b = self.proj.as_posix(), (self.tmp / "b").as_posix()
        self.put({"on": True, "since": SINCE, "project": a})
        self.ds.lean_set(True, b, root=self.root)
        self.assertState(self.status(self.proj), True, "project", SINCE)
        self.assertEqual(self.status(b)["scope"], "project")

    def test_off_removes_entries_at_or_inside_the_project_key(self):
        p = self.proj.as_posix()
        self.ds.lean_set(True, p, root=self.root)
        self.ds.lean_set(True, p + "/sub", root=self.root)  # outside git: its own project
        self.ds.lean_set(False, p, root=self.root)
        self.assertEqual(self.read()["projects"], {})
        self.assertState(self.status(self.proj), False, "none")
        self.assertState(self.status(self.proj / "sub"), False, "none")
        self.ds.lean_set(True, p, root=self.root)
        self.ds.lean_set(False, p + "/sub", root=self.root)  # leaves the folder above alone
        self.assertState(self.status(self.proj / "sub"), False, "none")
        self.assertTrue(self.status(self.proj)["on"])

    def test_project_off_drops_session_entry_inside_it(self):
        p = self.proj.as_posix()
        self.ds.lean_set(True, p + "/sub", session_id="s-1", root=self.root)
        self.ds.lean_set(False, p, root=self.root)
        self.assertNotIn("session", self.read())
        self.assertFalse(self.status(self.proj / "sub", "s-1")["on"])

    def test_session_off_only_this_session(self):
        p = self.proj.as_posix()
        self.ds.lean_set(True, p, root=self.root)
        self.ds.lean_set(False, p, session_id="s-1", root=self.root)
        self.assertState(self.status(self.proj, "s-1"), False, "session", self.read()["since"])
        self.assertTrue(self.status(self.proj, "s-2")["on"])

    def test_set_refuses_unreadable_file(self):
        self.put(b'{"on": tr')
        with self.assertRaises(self.ds.StateError):
            self.ds.lean_set(False, self.proj.as_posix(), root=self.root)
        self.assertEqual(self.lean_file.read_bytes(), b'{"on": tr')

    def test_write_is_atomic(self):
        self.put({"on": False})
        before = self.lean_file.read_bytes()
        with mock.patch.object(self.ds.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.ds.lean_set(True, self.proj.as_posix(), root=self.root)
        self.assertEqual(self.lean_file.read_bytes(), before)
        self.assertEqual(sorted(x.name for x in self.root.iterdir()), ["lean-mode.json"])
        self.ds.lean_set(True, self.proj.as_posix(), root=self.root)
        self.assertEqual(sorted(x.name for x in self.root.iterdir()), ["lean-mode.json"])


class SessionMapTests(StateCase):
    """Session entries are kept per session id: one session's write never changes another's."""

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--root", str(self.root)], capture_output=True,
                              text=True, encoding="utf-8", env=dict(os.environ, HOME=str(self.tmp / "home")))

    def test_two_sessions_keep_their_own_entries(self):
        other = self.tmp / "other"
        other.mkdir()
        self.ds.lean_set(True, self.proj.as_posix(), session_id="s-A", root=self.root)
        self.ds.lean_set(True, other.as_posix(), session_id="s-B", root=self.root)
        self.assertEqual(sorted(self.read()["sessions"]), ["s-A", "s-B"])
        for strict in (False, True):
            with self.subTest(strict=strict):
                st = self.ds.lean_status(self.proj.as_posix(), "s-A", root=self.root, strict=strict)
                self.assertEqual((st["on"], st["scope"]), (True, "session"))
                st = self.ds.lean_status(other.as_posix(), "s-B", root=self.root, strict=strict)
                self.assertEqual((st["on"], st["scope"]), (True, "session"))

    def test_another_session_id_does_not_change_this_session(self):
        self.ds.lean_set(True, self.proj.as_posix(), session_id="s-A", root=self.root)
        for args in (("lean", "on", "--session", "x", "--cwd", "/tmp"),
                     ("lean", "on", "--session", "x", "--cwd", self.proj.as_posix()),
                     ("lean", "off", "--session", "x", "--cwd", self.proj.as_posix())):
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args).returncode, 0)
                st = self.ds.lean_status(self.proj.as_posix(), "s-A", root=self.root, strict=True)
                self.assertEqual((st["on"], st["scope"]), (True, "session"))
        self.assertEqual(self.read()["sessions"]["s-A"]["on"], True)

    def test_older_single_session_slot_is_read_and_moved(self):
        p = self.proj.as_posix()
        self.put({"on": True, "since": SINCE, "project": p, "projects": {},
                  "session": {"id": "s-A", "on": True, "since": SINCE, "project": p}})
        self.assertState(self.ds.lean_status(p, "s-A", root=self.root, strict=True), True, "session", SINCE)
        self.ds.lean_set(True, p, session_id="s-B", root=self.root)
        data = self.read()
        self.assertNotIn("session", data)
        self.assertEqual(data["sessions"]["s-A"], {"on": True, "since": SINCE, "project": p})
        self.assertState(self.status(self.proj, "s-A"), True, "session", SINCE)

    def test_session_entries_are_capped_to_the_latest(self):
        cap = self.ds.MAX_SESSIONS
        self.put({"on": False, "projects": {}, "sessions": {
            f"old-{n}": {"on": True, "since": f"2026-01-01T00:00:{n:02d}Z", "project": self.proj.as_posix()}
            for n in range(cap)}})
        self.ds.lean_set(True, self.proj.as_posix(), session_id="new", root=self.root)
        kept = self.read()["sessions"]
        self.assertEqual(len(kept), cap)
        self.assertIn("new", kept)
        self.assertNotIn("old-0", kept)  # the oldest entry goes first

    def test_bad_sessions_shape_is_unknown(self):
        p = self.proj.as_posix()
        for data in ({"on": False, "projects": {}, "sessions": []},
                     {"on": False, "projects": {}, "sessions": {"s": True}},
                     {"on": False, "projects": {}, "sessions": {"s": {"on": "yes"}}},
                     {"on": False, "projects": {}, "sessions": {"": {"on": True, "project": p}}}):
            with self.subTest(data=data):
                self.put(data)
                self.assertState(self.status(self.proj, "s"), None, "unknown")


class StrictTests(StateCase):
    """strict=True (the enforcing hooks): only an entry whose own project key is the query's key counts."""

    def git_init(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", *GIT_ID, "-C", str(path), "init", "-q", "-b", "main"], check=True)

    def strict(self, cwd, session_id=None):
        return self.ds.lean_status(str(cwd), session_id, root=self.root, strict=True)

    def test_folder_above_a_project_is_not_on(self):
        self.ds.lean_set(True, self.proj.as_posix(), root=self.root)
        self.assertState(self.strict(self.tmp), False, "none")
        self.assertState(self.strict("/"), False, "none")
        self.assertTrue(self.status(self.tmp)["on"])  # the resume line keeps the inside-counts reading
        self.assertTrue(self.strict(self.proj)["on"])

    def test_outer_repo_with_a_nested_repo_is_not_on(self):
        outer, inner = self.tmp / "outer", self.tmp / "outer" / "inner"
        self.git_init(outer)
        self.git_init(inner)
        (inner / "sub").mkdir()
        self.ds.lean_set(True, inner.as_posix(), root=self.root)
        self.assertState(self.strict(outer), False, "none")
        self.assertTrue(self.strict(inner / "sub")["on"])

    def test_older_file_with_a_subfolder_cwd(self):
        repo = self.tmp / "repo"
        self.git_init(repo)
        (repo / "sub").mkdir()
        self.put({"on": True, "since": SINCE, "project": (repo / "sub").as_posix()})
        self.assertState(self.strict(repo), True, "project", SINCE)  # the stored folder's own key is the repo
        (repo / "sub").rmdir()  # the stored folder is gone: no key can be found, so not on
        self.assertState(self.strict(repo), False, "none")
        self.assertTrue(self.status(repo)["on"])
        (self.proj / "sub").mkdir()  # outside git a subfolder is its own project
        self.put({"on": True, "since": SINCE, "project": (self.proj / "sub").as_posix()})
        self.assertState(self.strict(self.proj), False, "none")
        self.assertTrue(self.strict(self.proj / "sub")["on"])

    def test_session_entry_needs_the_same_key(self):
        self.ds.lean_set(True, self.proj.as_posix(), session_id="s-1", root=self.root)
        self.assertTrue(self.strict(self.proj, "s-1")["on"])
        self.assertState(self.strict(self.tmp, "s-1"), False, "none")
        self.assertTrue(self.status(self.tmp, "s-1")["on"])

    def test_off_keeps_other_projects_inside(self):
        other, third = self.tmp / "other", self.proj / "third"
        self.git_init(other)
        third.mkdir()
        for path in (self.proj, other, third):
            self.ds.lean_set(True, path.as_posix(), root=self.root)
        st = self.ds.lean_set(False, self.tmp.as_posix(), root=self.root)  # nothing is set for the folder above
        self.assertState(st, False, "none")
        self.assertEqual(sorted(self.read()["projects"]), sorted(p.as_posix() for p in (self.proj, other, third)))
        self.ds.lean_set(False, self.proj.as_posix(), root=self.root)
        self.assertEqual(sorted(self.read()["projects"]), sorted(p.as_posix() for p in (other, third)))
        for path in (other, third):
            self.assertTrue(self.strict(path)["on"])


class DevModeTests(StateCase):
    def test_dev_mode_default_off(self):
        self.assertIs(self.ds.dev_mode(root=self.root), False)
        self.assertEqual(self.ds.dev_status(root=self.root)["state"], "off")
        self.ds.dev_set(True, root=self.root)
        self.assertIs(self.ds.dev_mode(root=self.root), True)
        data = json.loads((self.root / "devenv-dev.json").read_text(encoding="utf-8"))
        self.assertIs(data["on"], True)
        self.assertRegex(data["since"], SINCE_RE)
        self.ds.dev_set(False, root=self.root)
        self.assertIs(self.ds.dev_mode(root=self.root), False)

    def test_dev_mode_bad_file_is_unknown(self):
        self.root.mkdir(parents=True)
        for raw in (b"", b"{", b'{"on": "yes"}', b"[]"):
            with self.subTest(raw=raw):
                (self.root / "devenv-dev.json").write_bytes(raw)
                st = self.ds.dev_status(root=self.root)
                self.assertEqual(st["state"], "unknown")
                self.assertTrue(st["error"])
                self.assertIs(self.ds.dev_mode(root=self.root), False)

    def test_default_root_is_home_state_folder(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "h")}):
            self.assertEqual(self.ds.default_root(), self.tmp / "h" / ".claude" / "state")


class CliTests(StateCase):
    def run_cli(self, *args, home=None):
        env = {k: v for k, v in os.environ.items()}
        env.update(HOME=str(home or self.tmp / "home"), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=str(self.proj))

    def test_cli_lean_round_trip(self):
        r = ["--root", str(self.root)]
        subprocess.run(["git", "-C", str(self.proj), "init", "-q", "-b", "main"], check=True)
        (self.proj / "sub").mkdir()
        proc = self.run_cli("lean", "on", "--cwd", self.proj.as_posix(), *r)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["on"], out["scope"]), (True, "project"))
        proc = self.run_cli("lean", "status", "--cwd", (self.proj / "sub").as_posix(), *r)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["on"]), (0, True))
        proc = self.run_cli("lean", "off", "--session", "s-1", "--cwd", self.proj.as_posix(), *r)
        self.assertEqual(json.loads(proc.stdout)["scope"], "session")
        proc = self.run_cli("lean", "status", *r)  # --cwd defaults to the working folder
        self.assertEqual(json.loads(proc.stdout)["on"], True)
        proc = self.run_cli("lean", "off", *r)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["on"]), (0, False))

    def test_cli_unknown_and_usage_exit_codes(self):
        self.put(b"")
        proc = self.run_cli("lean", "status", "--root", str(self.root))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["scope"], "unknown")
        proc = self.run_cli("lean", "on", "--root", str(self.root))
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.lean_file.read_bytes(), b"")
        self.assertEqual(self.run_cli("lean", "maybe").returncode, 2)
        self.assertEqual(self.run_cli("lean", "on", "--cwd", "rel/dir", "--root", str(self.root)).returncode, 2)

    def test_cli_dev_round_trip(self):
        r = ["--root", str(self.root)]
        self.assertEqual(json.loads(self.run_cli("dev", "status", *r).stdout)["state"], "off")
        self.assertEqual(json.loads(self.run_cli("dev", "on", *r).stdout)["state"], "on")
        self.assertEqual(json.loads(self.run_cli("dev", "status", *r).stdout)["state"], "on")
        self.assertEqual(json.loads(self.run_cli("dev", "off", *r).stdout)["state"], "off")

    def test_cli_without_root_writes_under_home(self):
        home = self.tmp / "home"
        proc = self.run_cli("lean", "on", "--cwd", self.proj.as_posix(), home=home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((home / ".claude" / "state" / "lean-mode.json").is_file())


class HandoffStateCompatTests(StateCase):
    """The resume script's `lean-mode:` line uses lean_status; its line text must not change."""

    def setUp(self):
        super().setUp()
        self.assertTrue(HANDOFF_STATE.is_file(), f"{HANDOFF_STATE} is missing: it is moved in by WU2")
        self.home = self.tmp / "home"
        self.root = self.home / ".claude" / "state"
        self.lean_file = self.root / "lean-mode.json"
        subprocess.run(["git", *GIT_ID, "-C", str(self.proj), "init", "-q", "-b", "main"], check=True)
        (self.proj / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", *GIT_ID, "-C", str(self.proj), "add", "-A"], check=True)
        subprocess.run(["git", *GIT_ID, "-C", str(self.proj), "commit", "-q", "-m", "init"], check=True)

    def lean_line(self, cwd=None):
        env = {k: v for k, v in os.environ.items() if k not in (
            "HANDOFF_CHECKER", "HANDOFF_LOG", "CLAUDE_CODE_SESSION_ID", "HANDOFF_CALLER")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8",
                   HANDOFF_LOG=str(self.tmp / "runs.jsonl"))
        proc = subprocess.run([sys.executable, str(HANDOFF_STATE), str(cwd or self.proj)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env, cwd=str(self.tmp))
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("lean-mode:")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return lines[0]

    def test_handoff_state_lean_line_unchanged_after_set(self):
        p = self.proj.as_posix()
        self.ds.lean_set(True, p, root=self.root)
        self.assertEqual(self.lean_line(), f"lean-mode: on (since {self.read()['since']})")
        self.ds.lean_set(False, p, root=self.root)
        self.assertEqual(self.lean_line(), "lean-mode: off")
        self.ds.lean_set(True, (self.tmp / "other").as_posix(), root=self.root)
        self.assertEqual(self.lean_line(), "lean-mode: off (set for another project)")
        self.assertFalse(self.status(self.proj)["on"])

    def test_both_readers_agree_on_legacy_files(self):
        p = self.proj.as_posix()
        cases = [
            ({"on": True, "since": SINCE, "project": p}, f"lean-mode: on (since {SINCE})", True),
            ({"on": True, "project": p}, "lean-mode: on", True),
            ({"on": False}, "lean-mode: off", False),
            ({"on": True, "since": SINCE, "project": (self.tmp / "x").as_posix()},
             "lean-mode: off (set for another project)", False),
            (b'{"on": tr', "lean-mode: unknown", None),
            ({"on": "yes"}, "lean-mode: unknown", None),
        ]
        for data, line, on in cases:
            with self.subTest(data=data):
                self.put(data)
                self.assertEqual(self.lean_line(), line)
                self.assertIs(self.status(self.proj)["on"], on)

    def linked_tree(self):
        tree = self.tmp / "linked"
        subprocess.run(["git", *GIT_ID, "-C", str(self.proj), "worktree", "add", "-q", "-b", "side", str(tree)],
                       check=True, capture_output=True)
        return tree

    def test_on_in_a_subfolder_stores_the_project_key(self):
        (self.proj / "sub").mkdir()
        st = self.ds.lean_set(True, (self.proj / "sub").as_posix(), root=self.root)
        self.assertEqual((st["on"], st["scope"]), (True, "project"))
        data = self.read()
        self.assertEqual((data["project"], list(data["projects"])), (self.proj.as_posix(), [self.proj.as_posix()]))
        self.assertState(self.status(self.proj), True, "project", data["since"])
        self.assertEqual(self.lean_line(), f"lean-mode: on (since {data['since']})")
        self.ds.lean_set(False, self.proj.as_posix(), root=self.root)  # off at the root is the same project
        self.assertState(self.status(self.proj / "sub"), False, "none")
        self.assertEqual(self.lean_line(self.proj / "sub"), "lean-mode: off")

    def test_linked_worktree_uses_the_main_tree(self):
        tree = self.linked_tree()
        self.assertEqual(self.ds.git_project(tree.as_posix()),
                         {"toplevel": tree.as_posix(), "linked": True, "project": self.proj.as_posix()})
        self.ds.lean_set(True, tree.as_posix(), root=self.root)
        self.assertEqual(self.read()["project"], self.proj.as_posix())
        since = self.read()["since"]
        for cwd in (self.proj, tree):
            self.assertState(self.status(cwd), True, "project", since)
            self.assertEqual(self.lean_line(cwd), f"lean-mode: on (since {since})")
        self.ds.lean_set(True, tree.as_posix(), session_id="s-1", root=self.root)
        self.assertState(self.status(self.proj, "s-1"), True, "session", self.read()["since"])
        self.assertState(self.status(self.tmp / "other", "s-1"), False, "none")  # same session, other project

    def test_a_folder_above_is_another_project(self):
        self.ds.lean_set(True, self.tmp.as_posix(), root=self.root)  # outside git: the folder itself
        self.assertEqual(self.read()["project"], self.tmp.as_posix())
        self.assertState(self.status(self.proj), False, "none")
        self.assertEqual(self.lean_line(), "lean-mode: off (set for another project)")
        self.assertTrue(self.status(self.tmp)["on"])

    def test_lean_line_and_lean_status_agree(self):
        tree = self.linked_tree()
        (self.proj / "sub").mkdir()
        other = self.tmp / "other"
        other.mkdir()
        p, sub = self.proj.as_posix(), (self.proj / "sub").as_posix()
        T, F = True, False
        files = {  # writes, then the expected `on` for each of `paths`
            "on in a subfolder": ([(T, sub, None)], (T, T, T, F, T)),
            "on here, then on elsewhere": ([(T, p, None), (T, other.as_posix(), None)], (T, T, T, T, T)),
            "on in the linked tree": ([(T, tree.as_posix(), None)], (T, T, T, F, T)),
            "on, then off in a subfolder": ([(T, p, None), (F, sub, None)], (F, F, F, F, F)),
            "on for one session only": ([(T, p, "s-1")], (F, F, F, F, F)),
            "on in the folder above": ([(T, self.tmp.as_posix(), None)], (F, F, F, F, T)),
        }
        paths = (self.proj, self.proj / "sub", tree, other, self.tmp)
        line_on = {"lean-mode: off": False, "lean-mode: off (set for another project)": False,
                   "lean-mode: unknown": None}
        for name, (writes, expected) in files.items():
            if self.lean_file.exists():
                self.lean_file.unlink()
            for on, cwd, session in writes:
                self.ds.lean_set(on, cwd, session_id=session, root=self.root)
            for cwd, want_on in zip(paths, expected):
                with self.subTest(file=name, cwd=cwd.name):
                    st = self.status(cwd)
                    self.assertIs(st["on"], want_on, st)
                    line = self.lean_line(cwd)
                    want = (f"lean-mode: on (since {st['since']})" if st["since"] else "lean-mode: on") \
                        if st["on"] else line
                    self.assertEqual(line, want)
                    self.assertIs(line_on.get(line, True), st["on"], line)


if __name__ == "__main__":
    unittest.main()
