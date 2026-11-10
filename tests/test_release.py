"""Release contract: one version in three places, and the license the manifest names.

The version lives in the top CHANGELOG heading, `.claude-plugin/plugin.json` and `run_log.VERSION` (every run
log line and developer mode event carries it). A release that changes only one of them fails here. Accepted
limit: a release whose content changed but whose version did not is not caught; raising the version is the
release step's job.
"""
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ("CHANGELOG.md", ".claude-plugin/plugin.json", "scripts/run_log.py")


def versions(root: Path) -> dict:
    """{file: version or None} read from the three files under `root`."""
    out = {}
    m = re.search(r"(?m)^## \[([^\]]+)\]", (root / FILES[0]).read_text(encoding="utf-8"))
    out[FILES[0]] = m.group(1) if m else None
    out[FILES[1]] = json.loads((root / FILES[1]).read_text(encoding="utf-8")).get("version")
    m = re.search(r'(?m)^VERSION = "([^"]+)"', (root / FILES[2]).read_text(encoding="utf-8"))
    out[FILES[2]] = m.group(1) if m else None
    return out


def agree(found: dict) -> bool:
    return None not in found.values() and len(set(found.values())) == 1


class ReleaseVersionTests(unittest.TestCase):
    def test_three_versions_are_equal(self):
        found = versions(ROOT)
        self.assertTrue(agree(found), f"versions differ: {found}")
        self.assertRegex(found[FILES[0]], r"^\d{4}\.\d{2}\.\d{2}-\d+$")

    def test_version_read_matches_the_imported_module(self):
        spec = importlib.util.spec_from_file_location("run_log_for_release", ROOT / FILES[2])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.VERSION, versions(ROOT)[FILES[2]])

    def test_changelog_heading_date_matches_the_version_date(self):
        m = re.search(r"(?m)^## \[(\d{4})\.(\d{2})\.(\d{2})-\d+\] - (\d{4}-\d{2}-\d{2})$",
                      (ROOT / FILES[0]).read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "top CHANGELOG heading is not `## [YYYY.MM.DD-N] - YYYY-MM-DD`")
        self.assertEqual("-".join(m.groups()[:3]), m.group(4))

    def test_one_changed_value_fails(self):
        # Mutation pinned in the suite: a copy with any one of the three values changed must disagree.
        edits = {
            FILES[0]: lambda t: re.sub(r"(?m)^## \[[^\]]+\]", "## [1999.01.01-9]", t, count=1),
            FILES[1]: lambda t: re.sub(r'"version": "[^"]*"', '"version": "1999.01.01-9"', t, count=1),
            FILES[2]: lambda t: re.sub(r'(?m)^VERSION = "[^"]*"', 'VERSION = "1999.01.01-9"', t, count=1),
        }
        for changed, edit in edits.items():
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp)
                for name in FILES:
                    (copy / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, copy / name)
                path = copy / changed
                before = path.read_text(encoding="utf-8")
                after = edit(before)
                self.assertNotEqual(before, after, "the mutation did not apply")
                path.write_text(after, encoding="utf-8")
                self.assertFalse(agree(versions(copy)), versions(copy))


class LicenseTests(unittest.TestCase):
    def test_mit_license_file_and_manifest_field(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"), "LICENSE is not the MIT text")
        self.assertIn("Copyright (c) 2026 devenv maintainers", text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", text)
        manifest = json.loads((ROOT / FILES[1]).read_text(encoding="utf-8"))
        self.assertEqual("MIT", manifest.get("license"))

    def test_author_names_no_person(self):
        manifest = json.loads((ROOT / FILES[1]).read_text(encoding="utf-8"))
        self.assertEqual({"name": "devenv maintainers"}, manifest.get("author"))


if __name__ == "__main__":
    unittest.main()
