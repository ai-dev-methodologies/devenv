"""Manifest and repository layout contract for the devenv plugin."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"


def load(path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class PluginManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = load(PLUGIN_JSON)

    def test_name_is_devenv(self):
        self.assertEqual(self.manifest["name"], "devenv")

    def test_python_user_config_is_required_without_default(self):
        option = self.manifest["userConfig"]["python"]
        for key in ("type", "title", "description"):
            self.assertIn(key, option)
            self.assertTrue(option[key])
        self.assertEqual(option["type"], "string")
        self.assertIs(option["required"], True)
        # A default would bake one machine's interpreter path into a public repo.
        self.assertNotIn("default", option)
        self.assertNotIn("sensitive", option)


class MarketplaceManifestTest(unittest.TestCase):
    def setUp(self):
        self.marketplace = load(MARKETPLACE_JSON)

    def test_marketplace_name_is_devenv(self):
        self.assertEqual(self.marketplace["name"], "devenv")

    def test_single_plugin_entry_at_repo_root(self):
        plugins = self.marketplace["plugins"]
        self.assertEqual(len(plugins), 1)
        entry = plugins[0]
        self.assertEqual(entry["name"], "devenv")
        self.assertEqual(entry["source"], "./")

    def test_entry_has_no_version(self):
        # plugin.json owns the version; a second copy here would drift.
        self.assertNotIn("version", self.marketplace["plugins"][0])


class LayoutTest(unittest.TestCase):
    def test_hooks_file_has_hooks_object(self):
        # Registrations themselves are checked by test_hooks_registration.
        self.assertIsInstance(load(HOOKS_JSON).get("hooks"), dict)

    def test_gitignore_entries(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for entry in ("__pycache__/", ".venv/"):
            self.assertIn(entry, lines)

    def test_gitattributes_normalizes_line_endings(self):
        lines = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("* text=auto eol=lf", lines)


if __name__ == "__main__":
    unittest.main()
