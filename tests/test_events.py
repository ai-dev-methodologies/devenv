"""Tests for scripts/events.py (developer mode enforcement record).

Every test points HOME and USERPROFILE at a temporary folder or passes `root=`, so the real
~/.claude is never read or written.
"""
import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TS_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


events = load("events")
run_log = load("run_log")


class EventsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home_state = self.home / ".claude" / "state"
        self.default_events = self.home / ".claude" / "handoffs" / ".state" / "events.jsonl"

    def set_dev(self, folder, text):
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "devenv-dev.json").open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def records(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class EventTest(EventsCase):
    def test_event_written_when_dev_on(self):
        self.set_dev(self.home_state, '{"on": true}\n')
        self.assertIsNone(events.record("session_start", lean_on=True, start_bytes=4096))
        self.assertIsNone(events.record("return_pass", lean_on=False, bytes=12))
        recs = self.records(self.default_events)
        self.assertEqual(len(recs), 2)
        self.assertEqual(list(recs[0]), ["ts", "version", "kind", "lean_on", "start_bytes"])
        self.assertRegex(recs[0]["ts"], TS_FORMAT)
        self.assertEqual(recs[0]["version"], run_log.VERSION)
        self.assertEqual((recs[0]["kind"], recs[0]["lean_on"], recs[0]["start_bytes"]),
                         ("session_start", True, 4096))
        self.assertEqual((recs[1]["kind"], recs[1]["lean_on"], recs[1]["bytes"]), ("return_pass", False, 12))
        self.assertTrue(self.default_events.read_bytes().endswith(b"}\n"))
        self.assertNotIn(b"\r", self.default_events.read_bytes())

    def test_event_ints_only(self):
        self.set_dev(self.home_state, '{"on": true}\n')
        secret = "sk-SECRETVALUE cat /etc/passwd"
        self.assertIsNone(events.record(
            "bash_replace", lean_on="yes", bytes=1200, lines=secret, flag=True, ratio=1.5, missing=None,
            ts=7, version=8, **{"bad key": 3, secret: 4}))
        raw = self.default_events.read_text(encoding="utf-8")
        self.assertNotIn("SECRETVALUE", raw)
        rec = json.loads(raw)
        self.assertEqual(rec["kind"], "bash_replace")
        self.assertIsNone(rec["lean_on"])
        self.assertEqual(rec["bytes"], 1200)
        for key in ("lines", "flag", "ratio", "missing"):
            self.assertIsNone(rec[key], key)
        self.assertRegex(rec["ts"], TS_FORMAT)
        self.assertEqual(rec["version"], run_log.VERSION)
        self.assertNotIn("bad key", rec)
        self.assertEqual(set(rec), {"ts", "version", "kind", "lean_on", "bytes", "lines", "flag", "ratio",
                                    "missing"})

    def test_event_unknown_kind_not_written(self):
        self.set_dev(self.home_state, '{"on": true}\n')
        reason = events.record("free text kind", lean_on=True)
        self.assertIsInstance(reason, str)
        self.assertNotIn("free text kind", reason)
        self.assertFalse(self.default_events.exists())
        self.assertEqual(set(events.KINDS), {"read_deny", "bash_replace", "return_block", "handback_deny",
                                             "return_pass", "session_start"})

    def test_event_skipped_when_dev_off(self):
        for label, text in (("no state file", None), ("state file off", '{"on": false}\n')):
            with self.subTest(label):
                if text is not None:
                    self.set_dev(self.home_state, text)
                self.assertIsNone(events.record("read_deny", lean_on=True, bytes=10))
                self.assertFalse(self.default_events.exists())
                self.assertFalse(self.default_events.parent.exists())

    def test_event_unknown_dev_state_returns_reason(self):
        self.set_dev(self.home_state, "not json")
        reason = events.record("read_deny", lean_on=True, bytes=10)
        self.assertIsInstance(reason, str)
        self.assertIn("developer mode", reason)
        self.assertFalse(self.default_events.exists())

    def test_event_root_isolation(self):
        root = self.tmp / "replay"
        self.set_dev(root, '{"on": true}\n')
        self.assertIsNone(events.record("return_block", lean_on=True, root=root, lines=40, bytes=3000))
        self.assertEqual(len(self.records(root / "events.jsonl")), 1)
        self.assertFalse((self.home / ".claude").exists())
        # with root=, the developer mode under HOME is never read
        self.set_dev(self.home_state, '{"on": true}\n')
        empty = self.tmp / "empty"
        self.assertIsNone(events.record("return_block", lean_on=True, root=empty, lines=40))
        self.assertFalse(empty.exists())
        self.assertFalse(self.default_events.exists())

    def test_event_write_failure_returns_reason_without_raising(self):
        root = self.tmp / "blocked"
        self.set_dev(root, '{"on": true}\n')
        (root / "events.jsonl").mkdir()  # a folder where the file is needed
        reason = events.record("handback_deny", lean_on=False, root=root, lines=31)
        self.assertIsInstance(reason, str)
        self.assertTrue(reason)
        with mock.patch.object(events, "load_sibling", side_effect=RuntimeError("broken")):
            reason = events.record("read_deny", lean_on=True, root=root)
        self.assertIsInstance(reason, str)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
