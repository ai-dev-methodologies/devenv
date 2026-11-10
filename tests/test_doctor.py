"""doctor.py: static checks, the replay of every registered hook, live firing, developer mode and the skill.

Every check id gets a passing input and a WARN or FAIL input. The replay runs against a copy of the plugin
whose hook scripts or registration are broken on purpose (mutations both ways): a clean copy passes, a
hook that blocks everything, blocks nothing, drops a key from the Bash replacement, a misspelled script in
hooks.json and a missing interpreter all fail. Homes, projects and plugin copies live in temporary folders.
"""
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_docs

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "scripts" / "doctor.py"
SKILL = ROOT / "skills" / "doctor" / "SKILL.md"
SCOPE = "(proves: script, interpreter, registered command line)"
LINE = re.compile(r"^DOCTOR (\S+) (PASS|WARN|FAIL|SKIP) (.*)$")
SUMMARY = re.compile(r"^DOCTOR summary pass=(\d+) warn=(\d+) fail=(\d+) skip=(\d+)$")
REPLAY_IDS = ("replay-session-start", "replay-read", "replay-handback", "replay-bash", "replay-stop")
LIVE_IDS = ("live-session-start", "live-read", "live-handback", "live-bash", "live-stop")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor = load(DOCTOR, "doctor_under_test")
VERSION = load(ROOT / "scripts" / "run_log.py", "run_log_for_doctor_test").VERSION


def plugin_copy(dst: Path) -> Path:
    for name in ("scripts", "hooks", "skills", ".claude-plugin", "bin"):
        shutil.copytree(ROOT / name, dst / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "CHANGELOG.md", dst / "CHANGELOG.md")
    return dst


def align_versions(plugin: Path) -> None:
    manifest = plugin / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = VERSION
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor missing in {path.name}: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def by_id(results):
    return {r.id: r for r in results}


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.claude = self.home / ".claude"
        self.claude.mkdir(parents=True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.plugin = ROOT

    def ctx(self, **kw):
        values = dict(home=self.home, cwd=self.proj, python=sys.executable, plugin_root=self.plugin)
        values.update(kw)
        return doctor.Context(**values)

    def static(self, **kw):
        return by_id(doctor.static_checks(self.ctx(**kw)))

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def git_init(self, folder: Path):
        subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True)


# --- static checks ---------------------------------------------------------------------------------------
class PythonVersionTests(DoctorCase):
    def test_this_interpreter_passes(self):
        r = self.static()["python-version"]
        self.assertEqual("PASS", r.status, r.evidence)

    def test_old_interpreter_fails(self):
        fake = self.write(self.tmp / "python3.9", "#!/bin/sh\necho 3.9.18\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        r = self.static(python=str(fake))["python-version"]
        self.assertEqual("FAIL", r.status)
        self.assertIn("3.9.18", r.evidence)

    def test_missing_interpreter_fails(self):
        r = self.static(python=str(self.tmp / "no-such-python"))["python-version"]
        self.assertEqual("FAIL", r.status)
        self.assertIn("not found", r.evidence)


class PluginVersionTests(DoctorCase):
    def test_three_equal_values_pass_and_a_different_one_fails(self):
        self.plugin = plugin_copy(self.tmp / "plugin")
        align_versions(self.plugin)
        self.assertEqual("PASS", self.static()["plugin-version"].status)
        manifest = self.plugin / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["version"] = "0.0.1"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        r = self.static()["plugin-version"]
        self.assertEqual("FAIL", r.status)
        self.assertIn("0.0.1", r.evidence)


class HookScriptTests(DoctorCase):
    def test_registered_scripts_exist(self):
        self.assertEqual("PASS", self.static()["hook-scripts"].status)

    def test_misspelled_script_fails(self):
        self.plugin = plugin_copy(self.tmp / "plugin")
        mutate(self.plugin / "hooks" / "hooks.json", "scripts/hook_read.py", "scripts/hook_reed.py")
        r = self.static()["hook-scripts"]
        self.assertEqual("FAIL", r.status)
        self.assertIn("hook_reed.py", r.evidence)


class SkillDescriptionTests(DoctorCase):
    def test_short_descriptions_pass_and_a_long_one_fails(self):
        self.assertEqual("PASS", self.static()["skill-description"].status)
        self.plugin = plugin_copy(self.tmp / "plugin")
        mutate(self.plugin / "skills" / "cycle" / "SKILL.md", "description: ", "description: " + "x" * 300)
        r = self.static()["skill-description"]
        self.assertEqual("FAIL", r.status)
        self.assertIn("cycle", r.evidence)


class StateFileTests(DoctorCase):
    def test_missing_files_pass(self):
        found = self.static()
        self.assertEqual("PASS", found["lean-state"].status)
        self.assertEqual("PASS", found["dev-state"].status)

    def test_unreadable_files_fail(self):
        self.write(self.claude / "state" / "lean-mode.json", "{not json")
        self.write(self.claude / "state" / "devenv-dev.json", '{"on": "yes"}')
        found = self.static()
        self.assertEqual("FAIL", found["lean-state"].status)
        self.assertEqual("FAIL", found["dev-state"].status)


class RulesLayerTests(DoctorCase):
    def test_clean_layers_pass(self):
        found = self.static()
        for cid in ("rules-both-files", "rules-sections", "rules-limits"):
            self.assertEqual("PASS", found[cid].status, cid)

    def test_both_user_files_warn(self):
        self.write(self.claude / "devenv.local.md", "# user\n")
        self.write(self.claude / "handoff-rules.local.md", "# old\n")
        r = self.static()["rules-both-files"]
        self.assertEqual("WARN", r.status)
        self.assertIn("both", r.evidence)

    def test_section_outside_override_points_warns(self):
        self.git_init(self.proj)
        self.write(self.proj / ".claude" / "devenv.local.md", "## bogus-section\n")
        r = self.static()["rules-sections"]
        self.assertEqual("WARN", r.status)
        self.assertIn("bogus-section", r.evidence)

    def test_bad_limit_warns(self):
        self.write(self.claude / "devenv.local.md", "## lean-limits\nread_lines: many\n")
        r = self.static()["rules-limits"]
        self.assertEqual("WARN", r.status)
        self.assertIn("read_lines", r.evidence)

    def test_limit_over_the_cap_warns(self):
        self.write(self.claude / "devenv.local.md", "## lean-limits\nreturn_lines: 1000\n")
        r = self.static()["rules-limits"]
        self.assertEqual("WARN", r.status)
        self.assertIn("over the cap 120", r.evidence)


class RulesIgnoredTests(DoctorCase):
    def test_not_git_skips(self):
        self.write(self.proj / ".claude" / "devenv.local.md", "# p\n")
        self.assertEqual("SKIP", self.static()["rules-ignored"].status)

    def test_not_ignored_warns_and_ignored_passes(self):
        self.git_init(self.proj)
        self.write(self.proj / ".claude" / "devenv.local.md", "# p\n")
        r = self.static()["rules-ignored"]
        self.assertEqual("WARN", r.status)
        self.assertIn(".claude/devenv.local.md", r.evidence)
        self.write(self.proj / ".gitignore", ".claude/devenv.local.md\n")
        self.assertEqual("PASS", self.static()["rules-ignored"].status)


class PathTests(DoctorCase):
    def test_existing_fenced_and_placeholder_paths_pass(self):
        self.write(self.claude / "docs" / "real.md", "x")
        self.write(self.claude / "rules" / "a.md",
                   "See ~/.claude/docs/real.md, and `~/.claude/docs/`.\n"
                   "Pattern ~/.claude/rules/*.md and ~/.claude/projects/<slug>/memory/.\n"
                   "```\ncat ~/.claude/docs/fenced-missing.md\n```\n")
        r = self.static()["paths"]
        self.assertEqual("PASS", r.status, r.evidence)

    def test_missing_path_warns(self):
        self.write(self.claude / "CLAUDE.md", "Read ~/.claude/docs/gone.md first.\n")
        r = self.static()["paths"]
        self.assertEqual("WARN", r.status)
        self.assertIn("~/.claude/docs/gone.md", r.evidence)
        self.assertIn("CLAUDE.md:1", r.evidence)


class SettingsNameTests(DoctorCase):
    def test_no_list_skips(self):
        self.assertEqual("SKIP", self.static()["settings-names"].status)

    def test_missing_name_warns_and_present_names_pass(self):
        self.write(self.claude / "devenv.local.md", "## doctor-settings\n- env.API_TOKEN\n- statusLine\n")
        self.write(self.claude / "settings.json", json.dumps({"env": {"API_TOKEN": "v"}}))
        r = self.static()["settings-names"]
        self.assertEqual("WARN", r.status)
        self.assertIn("statusLine", r.evidence)
        self.assertNotIn("API_TOKEN", r.evidence)
        self.write(self.claude / "settings.local.json", json.dumps({"statusLine": {"type": "command"}}))
        self.assertEqual("PASS", self.static()["settings-names"].status)

    def test_names_with_at_and_slash_are_read(self):
        self.write(self.claude / "devenv.local.md",
                   "## doctor-settings\n- enabledPlugins.demo-kit@demo-market\n- `mcpServers.org/tool`\n")
        self.write(self.claude / "settings.json", json.dumps({"enabledPlugins": {"demo-kit@demo-market": True}}))
        self.write(self.claude / "settings.local.json", json.dumps({"mcpServers": {"org/tool": {}}}))
        r = self.static()["settings-names"]
        self.assertEqual(("PASS", "2 expected names present"), (r.status, r.evidence))
        self.write(self.claude / "settings.local.json", json.dumps({}))
        r = self.static()["settings-names"]
        self.assertEqual("WARN", r.status)
        self.assertIn("1 of 2 expected names missing from settings: mcpServers.org/tool", r.evidence)

    def test_keys_holding_a_dot_are_found(self):
        self.write(self.claude / "devenv.local.md", "## doctor-settings\n- extraKnownMarketplaces.my.market\n"
                   "- extraKnownMarketplaces.my.market.source.repo\n- env.API_TOKEN\n")
        settings = {"extraKnownMarketplaces": {"my.market": {"source": {"repo": "demo/demo"}}}, "env": {"API_TOKEN": "v"}}
        self.write(self.claude / "settings.json", json.dumps(settings))
        r = self.static()["settings-names"]
        self.assertEqual(("PASS", "3 expected names present"), (r.status, r.evidence))
        self.write(self.claude / "devenv.local.md", "## doctor-settings\n- extraKnownMarketplaces.my.other\n")
        r = self.static()["settings-names"]
        self.assertEqual("WARN", r.status)
        self.assertIn("extraKnownMarketplaces.my.other", r.evidence)

    def test_unreadable_list_lines_warn_with_count_and_a_cut_first_line(self):
        long_line = "- env.SECRET_VALUE=abcdefghijklmnopqrstuvwxyz0123456789-tail-kept-out"
        self.write(self.claude / "devenv.local.md", "## doctor-settings\nNames this doctor looks for.\n"
                   f"- env.API_TOKEN\n{long_line}\n- settings.json env.X\n")
        self.write(self.claude / "settings.json", json.dumps({"env": {"API_TOKEN": "v"}}))
        r = self.static()["settings-names"]
        self.assertEqual("WARN", r.status)
        self.assertIn("2 unreadable list line(s) under ## doctor-settings", r.evidence)
        self.assertIn(long_line[:40], r.evidence)
        self.assertNotIn(long_line[:41], r.evidence)
        self.assertNotIn("missing", r.evidence)
        self.write(self.claude / "devenv.local.md", "## doctor-settings\n- settings.json env.X\n")
        r = self.static()["settings-names"]
        self.assertEqual("WARN", r.status)
        self.assertIn("1 unreadable list line(s) under ## doctor-settings", r.evidence)
        self.assertIn("- settings.json env.X", r.evidence)


class LegacyTests(DoctorCase):
    def test_no_older_install_passes(self):
        found = self.static()
        self.assertEqual("PASS", found["legacy-install"].status)
        self.assertEqual("PASS", found["legacy-budget-hook"].status)

    def test_older_scripts_warn(self):
        self.write(self.claude / "scripts" / "run_log.py", "")
        r = self.static()["legacy-install"]
        self.assertEqual("WARN", r.status)
        self.assertIn("run_log.py", r.evidence)

    def test_older_budget_registration_with_engine_hook_warns(self):
        settings = {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "python3 ~/.claude/scripts/context_budget.py --record"}]}]}}
        self.write(self.claude / "settings.json", json.dumps(settings))
        r = self.static()["legacy-budget-hook"]
        self.assertEqual("WARN", r.status)
        self.assertIn("duplicate", r.evidence)


class StartLoadTests(DoctorCase):
    def test_small_load_passes_and_over_cap_fails_with_largest_files(self):
        self.write(self.claude / "CLAUDE.md", "x" * 1000)
        self.assertEqual("PASS", self.static()["start-load"].status)
        self.write(self.claude / "rules" / "huge.md", "y" * 50000)
        r = self.static()["start-load"]
        self.assertEqual("FAIL", r.status)
        self.assertIn("huge.md 50000", r.evidence)
        self.assertIn("cap=49152", r.evidence)


class HookErrorTests(DoctorCase):
    def transcript(self, name, *attachments):
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.proj.resolve()))
        lines = [json.dumps({"type": "attachment", "attachment": a}) for a in attachments]
        return self.write(self.claude / "projects" / slug / name, "\n".join(lines) + "\n")

    def error(self, command):
        return {"type": "hook_non_blocking_error", "hookEvent": "PreToolUse", "exitCode": "1",
                "stderr": "Failed with non-blocking status code: ENOENT", "command": command}

    def test_no_transcript_skips(self):
        self.assertEqual("SKIP", self.static()["hook-errors"].status)

    def test_devenv_error_warns_and_other_errors_pass(self):
        self.transcript("a.jsonl", self.error("node other-plugin/hook.js"))
        self.assertEqual("PASS", self.static()["hook-errors"].status)
        self.transcript("b.jsonl", self.error("/nonexistent/python3 ${CLAUDE_PLUGIN_ROOT}/scripts/hook_read.py"))
        r = self.static()["hook-errors"]
        self.assertEqual("WARN", r.status)
        self.assertIn("1 ", r.evidence)


class EvidenceTests(DoctorCase):
    def test_evidence_never_contains_setting_values(self):
        secrets = ("sekret-token-123", "secret-cmd-xyz", "sekret-hook-777", "sekret-status")
        settings = {
            "env": {"API_TOKEN": secrets[0]},
            "permissions": {"allow": [f"Bash({secrets[1]})"]},
            "hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": f"python3 /x/context_budget.py --token {secrets[2]}"}]}]},
        }
        self.write(self.claude / "settings.json", json.dumps(settings))
        self.write(self.claude / "settings.local.json", json.dumps({"statusLine": secrets[3]}))
        self.write(self.claude / "devenv.local.md",
                   "## doctor-settings\n- env.API_TOKEN\n- permissions.allow\n- statusLine\n- model\n")
        results = doctor.static_checks(self.ctx())
        text = "\n".join(doctor.render(results, verbose=True))
        for value in secrets:
            self.assertNotIn(value, text)
        self.assertIn("model", by_id(results)["settings-names"].evidence)


# --- replay ----------------------------------------------------------------------------------------------
class ReplayTests(DoctorCase):
    def setUp(self):
        super().setUp()
        self.plugin = plugin_copy(self.tmp / "plugin")

    def replay(self, *only, **kw):
        return by_id(doctor.replay_checks(self.ctx(**kw), only=set(only) or None))

    def assertReplay(self, status, *only, **kw):
        found = self.replay(*only, **kw)
        for cid in (only or REPLAY_IDS):
            with self.subTest(check=cid):
                self.assertEqual(status, found[cid].status, found[cid].evidence)
                self.assertTrue(found[cid].evidence.endswith(SCOPE), found[cid].evidence)
        return found

    def test_clean_plugin_replays_pass(self):
        self.assertReplay("PASS")

    def test_hook_read_blocking_everything_fails(self):
        mutate(self.plugin / "scripts" / "hook_read.py", "    raw = sys.stdin.read()\n",
               "    raw = sys.stdin.read()\n    print(json.dumps(deny('mutant')))\n    return 0\n")
        r = self.assertReplay("FAIL", "replay-read")["replay-read"]
        self.assertIn("pass small Read", r.evidence)

    def test_hook_read_blocking_nothing_fails(self):
        mutate(self.plugin / "scripts" / "hook_read.py", "    raw = sys.stdin.read()\n",
               "    raw = sys.stdin.read()\n    return 0\n")
        r = self.assertReplay("FAIL", "replay-read")["replay-read"]
        self.assertIn("deny large Read", r.evidence)

    def test_hook_bash_replacing_everything_fails(self):
        mutate(self.plugin / "scripts" / "hook_bash.py", "    if lines <= limits[\"bash_lines\"] and size <= limits[\"bash_bytes\"]:\n        return None\n", "")
        mutate(self.plugin / "scripts" / "hook_bash.py", "    if not unknown and status.get(\"on\") is not True:\n        return None\n", "")
        self.assertReplay("FAIL", "replay-bash")

    def test_hook_bash_replacing_nothing_fails(self):
        mutate(self.plugin / "scripts" / "hook_bash.py", "    answer = decide(", "    answer = None and decide(")
        self.assertReplay("FAIL", "replay-bash")

    def test_bad_format_fails(self):
        # The replacement drops a required key and the hook's own shape check is bypassed: only the
        # doctor's own check of the replacement object can see it.
        mutate(self.plugin / "scripts" / "hook_bash.py", "    problems = output_shape_problems(new, response)\n",
               "    new.pop('interrupted')\n    problems = []\n")
        r = self.assertReplay("FAIL", "replay-bash")["replay-bash"]
        self.assertIn("interrupted", r.evidence)

    def test_hook_return_blocking_everything_fails(self):
        mutate(self.plugin / "scripts" / "hook_return.py", "    if args.point == \"stop\":\n        out = decide_stop",
               "    sys.stdout.write(json.dumps({'decision': 'block', 'reason': 'm'} if args.point == 'stop' else "
               "{'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', "
               "'permissionDecisionReason': 'm'}}))\n    return 0\n    if args.point == \"stop\":\n        out = decide_stop")
        self.assertReplay("FAIL", "replay-stop", "replay-handback")

    def test_hook_return_blocking_nothing_fails(self):
        mutate(self.plugin / "scripts" / "hook_return.py", "    if args.point == \"stop\":\n        out = decide_stop",
               "    return 0\n    if args.point == \"stop\":\n        out = decide_stop")
        self.assertReplay("FAIL", "replay-stop", "replay-handback")

    def test_session_start_notice_always_or_never_fails(self):
        hook = self.plugin / "scripts" / "hook_session_start.py"
        original = hook.read_text(encoding="utf-8")
        mutate(hook, "    if not lean.get(\"on\"):\n        return None\n", "")
        self.assertReplay("FAIL", "replay-session-start")
        hook.write_text(original, encoding="utf-8")
        mutate(hook, "    if text is not None:\n", "    if False:\n")
        self.assertReplay("FAIL", "replay-session-start")

    def test_broken_registration_fails(self):
        mutate(self.plugin / "hooks" / "hooks.json", "scripts/hook_read.py", "scripts/hook_reed.py")
        r = self.assertReplay("FAIL", "replay-read")["replay-read"]
        self.assertIn("hook_reed.py", r.evidence)

    def test_missing_registration_fails(self):
        hooks = self.plugin / "hooks" / "hooks.json"
        data = json.loads(hooks.read_text(encoding="utf-8"))
        del data["hooks"]["SubagentStop"]
        hooks.write_text(json.dumps(data), encoding="utf-8")
        r = self.assertReplay("FAIL", "replay-stop")["replay-stop"]
        self.assertIn("not registered", r.evidence)

    def test_missing_interpreter_fails(self):
        found = self.assertReplay("FAIL", python=str(self.tmp / "no-such-python"))
        self.assertIn("interpreter not found", found["replay-read"].evidence)

    def test_replay_never_touches_real_state(self):
        # A home with developer mode on: a replay that wrote to a default path would grow these files.
        fake = self.tmp / "realhome"
        state = fake / ".claude" / "state"
        handoffs = fake / ".claude" / "handoffs" / ".state"
        self.write(state / "lean-mode.json", json.dumps({"on": True, "project": str(self.proj)}))
        self.write(state / "devenv-dev.json", json.dumps({"on": True, "since": "2026-01-01T00:00:00Z"}))
        self.write(handoffs / "events.jsonl", "")
        self.write(handoffs / "runs.jsonl", "")
        self.write(fake / ".claude" / "logs" / "context-budget.log", "")
        watched = [state, handoffs, fake / ".claude" / "logs"]

        def snapshot():
            return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                    for folder in watched for p in sorted(folder.rglob("*"))}

        before = snapshot()
        env = dict(os.environ, HOME=str(fake), USERPROFILE=str(fake))
        env.pop("HANDOFF_LOG", None)
        proc = subprocess.run([sys.executable, str(self.plugin / "scripts" / "doctor.py"), "--cwd", str(self.proj),
                               "--python", sys.executable, "--verbose"], capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=300)
        self.assertNotEqual(2, proc.returncode, proc.stdout + proc.stderr)
        for cid in REPLAY_IDS:
            self.assertIn(f"DOCTOR {cid} PASS", proc.stdout)
        self.assertEqual(before, snapshot())


# --- live ------------------------------------------------------------------------------------------------
class LiveTests(DoctorCase):
    def events(self, *rows):
        path = self.claude / "handoffs" / ".state" / "events.jsonl"
        lines = [json.dumps(dict({"ts": "2026-11-10T00:00:00Z", "lean_on": True}, **row)) for row in rows]
        self.write(path, "\n".join(lines) + "\n")

    def live(self):
        return by_id(doctor.live_checks(self.ctx()))

    def test_no_record_skips(self):
        found = self.live()
        for cid in LIVE_IDS:
            self.assertEqual("SKIP", found[cid].status)
            self.assertTrue(found[cid].evidence.startswith("(no live firing recorded)"))
        self.assertIn("debug", found["live-bash"].evidence)

    def test_current_version_firings_pass(self):
        self.events({"version": VERSION, "kind": "session_start"},
                    {"version": VERSION, "kind": "read_deny", "guard": 0, "unknown": 0},
                    {"version": VERSION, "kind": "handback_deny"},
                    {"version": VERSION, "kind": "bash_replace", "replaced": 1},
                    {"version": VERSION, "kind": "return_block"})
        for cid, r in self.live().items():
            self.assertEqual("PASS", r.status, cid)

    def test_what_does_not_count(self):
        self.events({"version": "1999.01.01-1", "kind": "session_start"},
                    {"version": VERSION, "kind": "read_deny", "guard": 1, "unknown": 0},
                    {"version": VERSION, "kind": "read_deny", "guard": 0, "unknown": 1},
                    {"version": VERSION, "kind": "bash_replace", "replaced": 0},
                    {"version": VERSION, "kind": "return_pass"})
        found = self.live()
        for cid in LIVE_IDS:
            self.assertEqual("SKIP", found[cid].status, cid)
        self.assertIn("guard=1", found["live-read"].evidence)
        self.assertIn("unknown=1", found["live-read"].evidence)

    def test_live_skip_not_counted(self):
        R = doctor.Result
        self.assertEqual(0, doctor.exit_code([R("live-read", "SKIP", "(no live firing recorded)")], False))
        self.assertEqual(1, doctor.exit_code([R("replay-read", "SKIP", "no temporary folder")], False))


# --- output and exit codes -------------------------------------------------------------------------------
class OutputTests(DoctorCase):
    def test_exit_codes(self):
        R = doctor.Result
        self.assertEqual(0, doctor.exit_code([R("paths", "WARN", "x"), R("hook-errors", "SKIP", "x")], False))
        self.assertEqual(1, doctor.exit_code([R("start-load", "FAIL", "x")], False))
        self.assertEqual(2, doctor.exit_code([R("paths", "PASS", "x")], True))
        proc = subprocess.run([sys.executable, str(DOCTOR), "--no-such-option"], capture_output=True, text=True)
        self.assertEqual(2, proc.returncode)

    def test_internal_error_in_a_check_is_skip_and_exit_2(self):
        results, internal = doctor.guarded("paths", lambda: 1 / 0)
        self.assertTrue(internal)
        self.assertEqual("SKIP", results[0].status)
        self.assertIn("internal error", results[0].evidence)

    def run_cli(self, *extra):
        plugin = plugin_copy(self.tmp / "plugin")
        align_versions(plugin)
        env = dict(os.environ)
        env.pop("HANDOFF_LOG", None)
        return subprocess.run([sys.executable, str(plugin / "scripts" / "doctor.py"), "--home", str(self.home),
                               "--cwd", str(self.proj), "--python", sys.executable, *extra],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)

    def test_clean_run_folds_pass_lines_and_exits_0(self):
        proc = self.run_cli()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertTrue(SUMMARY.match(lines[-1]), lines[-1])
        self.assertEqual("0", SUMMARY.match(lines[-1]).group(3))
        for line in lines[:-1]:
            m = LINE.match(line)
            self.assertIsNotNone(m, line)
            self.assertNotEqual("PASS", m.group(2), line)

    def test_verbose_shows_every_line_and_the_counts_add_up(self):
        proc = self.run_cli("--verbose")
        lines = proc.stdout.splitlines()
        rows = [LINE.match(l) for l in lines[:-1]]
        self.assertTrue(all(rows), lines)
        ids = [m.group(1) for m in rows]
        for cid in REPLAY_IDS + LIVE_IDS + ("python-version", "start-load", "paths"):
            self.assertIn(cid, ids)
        counts = [int(n) for n in SUMMARY.match(lines[-1]).groups()]
        self.assertEqual(len(rows), sum(counts))
        self.assertEqual(sum(1 for m in rows if m.group(2) == "PASS"), counts[0])


# --- developer mode --------------------------------------------------------------------------------------
class DevModeTests(DoctorCase):
    def cli(self, *args):
        env = dict(os.environ)
        env.pop("HANDOFF_LOG", None)
        return subprocess.run([sys.executable, str(DOCTOR), *args, "--home", str(self.home)], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)

    def test_on_status_off(self):
        self.assertIn("developer mode: on", self.cli("dev", "on").stdout)
        data = json.loads((self.claude / "state" / "devenv-dev.json").read_text(encoding="utf-8"))
        self.assertTrue(data["on"])
        self.assertIn("developer mode: on", self.cli("dev", "status").stdout)
        proc = self.cli("dev", "off")
        self.assertEqual(0, proc.returncode)
        self.assertIn("developer mode: off", proc.stdout)

    def test_report_when_off_names_developer_mode(self):
        proc = self.cli("dev", "report")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("developer mode is off", proc.stdout)
        self.assertNotIn("HANDOFF_LOG", proc.stdout + proc.stderr)

    def test_run_report_default_log_names_developer_mode(self):
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        env.pop("HANDOFF_LOG", None)
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_report.py")], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)
        self.assertEqual(2, proc.returncode)
        self.assertIn("developer mode is off", proc.stderr)
        self.assertNotIn("HANDOFF_LOG=off", proc.stderr)

    def test_dev_report_counts(self):
        self.cli("dev", "on")
        rows = [{"version": VERSION, "kind": "read_deny", "guard": 0, "unknown": 0},
                {"version": VERSION, "kind": "read_deny", "guard": 1, "unknown": 0},
                {"version": VERSION, "kind": "return_pass", "bytes": 100},
                {"version": VERSION, "kind": "return_block", "lines": 40, "bytes": 250},
                {"version": "2026.01.01-1", "kind": "handback_deny", "lines": 31, "bytes": 50}]
        path = self.claude / "handoffs" / ".state" / "events.jsonl"
        self.write(path, "\n".join(json.dumps(dict({"ts": "2026-11-10T00:00:00Z"}, **r)) for r in rows)
                   + "\nnot json\n")
        proc = self.cli("dev", "report")
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = proc.stdout
        self.assertIn(f"version {VERSION}: read_deny=2 (guard=1 unknown=0) return_block=1 return_pass=1 "
                      f"return_bytes=350", out)
        self.assertIn("version 2026.01.01-1: handback_deny=1 return_bytes=50", out)
        self.assertIn("1 unreadable", out)
        self.assertIn("run log: none", out)

    def test_dev_off_is_not_guarded_by_the_lean_off_guard(self):
        state = self.tmp / "state"
        load(ROOT / "scripts" / "devenv_state.py", "ds_for_doctor_test").lean_set(True, str(self.proj), root=state)
        payload = {"session_id": "s", "cwd": str(self.proj), "hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "devenv-py /p/scripts/doctor.py dev off --py /usr/bin/python3"}}
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "hook_read.py"), "--root", str(state)],
                              input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("", proc.stdout.strip())

    def test_init_project_creates_the_project_file(self):
        env = dict(os.environ)
        proc = subprocess.run([sys.executable, str(DOCTOR), "init-project", "--cwd", str(self.proj)],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue((self.proj / ".claude" / "devenv.local.md").is_file())
        self.assertIn("created:", proc.stdout)


class ContextBudgetHomeTests(DoctorCase):
    def test_measure_takes_the_home_folder(self):
        budget = load(ROOT / "scripts" / "context_budget.py", "cb_for_doctor_test")
        self.write(self.claude / "CLAUDE.md", "abc")
        rows = dict((str(p), s) for p, s in budget.measure(self.proj, home=self.home))
        self.assertEqual(3, rows[str(self.claude / "CLAUDE.md")])


# --- the skill -------------------------------------------------------------------------------------------
class DoctorSkillTests(test_docs.Base):
    @classmethod
    def setUpClass(cls):
        cls.raw = test_docs.read(SKILL)
        cls.text = " ".join(cls.raw.split())

    def test_user_only_and_one_allowed_rule(self):
        fields = test_docs.frontmatter(self.raw)
        self.assertEqual("doctor", fields.get("name"))
        self.assertEqual("true", fields.get("disable-model-invocation"))
        self.assertLessEqual(len(fields["description"]), 300)
        self.assertEqual([f"Bash({test_docs.SCRIPT_PREFIX}doctor.py *)"],
                         test_docs.allowed_rules(fields.get("allowed-tools")))

    def test_calls_pass_the_hook_interpreter(self):
        call = test_docs.SCRIPT_PREFIX + "doctor.py --cwd ${CLAUDE_PROJECT_DIR} --python ${user_config.python}"
        self.has(call + " " + test_docs.PY_OPTION)
        for arg in ("dev on", "dev off", "dev status", "dev report"):
            self.has(test_docs.SCRIPT_PREFIX + f"doctor.py {arg} " + test_docs.PY_OPTION)
        self.has(test_docs.SCRIPT_PREFIX + "doctor.py init-project --cwd ${CLAUDE_PROJECT_DIR} "
                 + test_docs.PY_OPTION)

    def test_output_is_passed_on_unchanged_with_each_scope(self):
        self.has("Pass the script output on unchanged")
        self.has("script, interpreter, registered command line")
        self.has("(no live firing recorded)")
        self.has("debug")

    def test_no_personal_traces(self):
        for n, line in enumerate(self.raw.splitlines(), 1):
            self.assertIsNone(re.search("[가-힣]", line), f"{n}: {line}")
            self.assertNotIn("/Us" + "ers/", line)  # split so the public scan never matches this file


if __name__ == "__main__":
    unittest.main()
