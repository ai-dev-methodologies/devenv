"""Contract tests for the lean, lean-off and cycle skills (wording the hooks and the rules layer rely on).

Frontmatter and the wrapper form of script calls are checked for every skill in test_docs.SKILL_SCRIPTS;
this file makes sure every skill folder is in that table and checks what each skill body must say.
"""
import importlib.util
import re
import unittest
from pathlib import Path

import test_docs

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
LEAN = SKILLS / "lean" / "SKILL.md"
LEAN_OFF = SKILLS / "lean-off" / "SKILL.md"
CYCLE = SKILLS / "cycle" / "SKILL.md"
STATE_CALL = test_docs.SCRIPT_PREFIX + "devenv_state.py"
# Any call that turns lean off, however it is spaced.
OFF_CALL = re.compile(r"devenv_state\.py\s+lean\s+off(\s|$)")
OFF_LINE = ("!`" + STATE_CALL + " lean off --cwd ${CLAUDE_PROJECT_DIR} " + test_docs.PY_OPTION + "`")

# Traces of one person's setup that must not reach the generic skills (split so this file never matches).
TRACES = [
    re.compile("Asia/" + "Seoul"),
    re.compile(r"\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b"),
    re.compile("[가-힣]"),  # the skills are written in English
    re.compile("explorer" + "-low|executor" + "-(med|high|max)|reviewer" + "-(high|xhigh)|judge" + "-max"),
    re.compile("doc" + "-reviewer|delegate" + "-first"),
    re.compile("fa" + "ble|myth" + "os", re.I),
    re.compile("wait" + "_runner|ralp" + "lan|lean" + "-mode\\.json"),
]


def rules_layer():
    spec = importlib.util.spec_from_file_location("rules_layer", ROOT / "scripts" / "rules_layer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body(text):
    return text.split("\n---\n", 1)[1]


def flat(text):
    """The text with every run of whitespace as one space, so wrapped sentences still match."""
    return " ".join(text.split())


class SkillFolderTests(unittest.TestCase):
    def test_every_skill_folder_has_a_row_in_the_skill_table(self):
        folders = {p.parent.name for p in SKILLS.glob("*/SKILL.md")}
        self.assertEqual(set(), folders - set(test_docs.SKILL_SCRIPTS))

    def test_every_description_is_300_characters_or_fewer(self):
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                fields = test_docs.frontmatter(test_docs.read(path))
                self.assertIsNotNone(fields, "no frontmatter block")
                self.assertTrue(fields.get("description"))
                self.assertLessEqual(len(fields["description"]), 300)

    def test_new_skills_carry_no_personal_traces(self):
        for path in (LEAN, LEAN_OFF, CYCLE):
            for n, line in enumerate(test_docs.read(path).splitlines(), 1):
                for rx in TRACES:
                    with self.subTest(file=path.parent.name, line=n, pattern=rx.pattern):
                        self.assertIsNone(rx.search(line), f"{path.parent.name}:{n}: {line}")


class LeanSkillTests(test_docs.Base):
    @classmethod
    def setUpClass(cls):
        cls.raw = test_docs.read(LEAN)
        cls.text = flat(cls.raw)

    def test_on_status_and_session_option_use_the_state_script(self):
        self.has(STATE_CALL + " lean on --cwd ${CLAUDE_PROJECT_DIR} " + test_docs.PY_OPTION)
        self.has(STATE_CALL + " lean on --cwd ${CLAUDE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID} "
                 + test_docs.PY_OPTION)
        self.has(STATE_CALL + " lean status --cwd ${CLAUDE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID} "
                 + test_docs.PY_OPTION)
        self.matches(r"(?i)never write the state file with the Write or Edit tool")

    def test_lean_has_no_off_call_and_points_the_user_at_lean_off(self):
        self.assertIsNone(OFF_CALL.search(self.raw), "the lean skill must not run the off command")
        self.has("the user types `/devenv:lean-off` themselves")

    def test_no_argument_turns_lean_on_for_this_project(self):
        self.matches(r"(?i)With no argument, this skill turns lean mode on for this project")

    def test_argument_table_rows(self):
        on = STATE_CALL + " lean on --cwd ${CLAUDE_PROJECT_DIR} " + test_docs.PY_OPTION
        session = (STATE_CALL + " lean on --cwd ${CLAUDE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID} "
                   + test_docs.PY_OPTION)
        status = (STATE_CALL + " lean status --cwd ${CLAUDE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID} "
                  + test_docs.PY_OPTION)
        rows = {}
        for line in body(self.raw).splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("|") and len(cells) == 3 and not set(cells[0]) <= set("-"):
                rows[cells[0]] = cells[1:]
        self.assertIn("nothing, or `on`", rows)
        self.assertEqual(f"`{on}`", rows["nothing, or `on`"][1])
        self.assertIn("`session` or `on session`", rows)
        self.assertEqual(f"`{session}`", rows["`session` or `on session`"][1])
        self.assertIn("`status`", rows)
        self.assertEqual(f"`{status}`", rows["`status`"][1])
        self.assertRegex(rows["`status`"][0], r"(?i)turns nothing on")
        off = [cells for key, cells in rows.items() if "`off`" in key]
        self.assertEqual(1, len(off), rows)
        self.assertIn("`/devenv:lean-off`", off[0][0])
        self.assertNotIn("devenv_state.py", off[0][1])
        self.assertFalse([k for k in rows if "nothing" in k and k != "nothing, or `on`"], rows)

    def test_on_answer_names_scope_project_and_the_off_skill(self):
        self.matches(r"(?s)After turning it on, answer in one line with the scope.{0,200}?`\$\{CLAUDE_PROJECT_DIR\}`"
                     r".{0,300}?`/devenv:lean-off`")

    def test_argument_hint_and_description(self):
        fields = test_docs.frontmatter(self.raw)
        self.assertEqual('"[on | session | status]"', fields.get("argument-hint"))
        self.assertRegex(fields["description"], r"(?i)with no argument it turns lean mode on for this project")
        self.assertLessEqual(len(fields["description"]), 300)

    def test_protected_items_are_never_shortened_or_reworded(self):
        self.matches(r"Code, commands, paths, error messages, numbers and negations are never shortened or"
                     r"\s+reworded\.")

    def test_short_report_cap_exceptions(self):
        self.matches(r"(?s)Only two things are exempt from the short report cap: security warnings and the"
                     r"\s+confirmation block before an irreversible action\. Write both in full sentences\.")
        self.matches(r"The 30-line return gate for subagents has no exceptions\.")

    def test_enforcement_section_states_what_is_blocked_and_what_is_not(self):
        self.has("## What the hooks enforce")
        self.matches(r"(?s)Read of a large text file.{0,200}?is refused")
        self.matches(r"(?s)Bash output over the limit is replaced")
        self.matches(r"(?s)A subagent return longer than 30 lines is blocked")
        self.matches(r"(?s)What the hooks do not cover:.{0,300}?tools whose output format is not documented")
        self.has("## lean-limits")


class LeanUsageDocsTests(unittest.TestCase):
    """Every reader-facing guide states the current usage: no argument turns lean on."""

    def test_guides_use_the_new_lean_usage(self):
        for path in (ROOT / "README.md", ROOT / "README.ko.md", ROOT / "docs" / "article.md",
                     ROOT / "docs" / "article.ko.md"):
            text = test_docs.read(path)
            with self.subTest(file=path.name):
                self.assertNotIn("`/devenv:lean on", text)
                for usage in ("`/devenv:lean`", "`/devenv:lean status`", "`/devenv:lean session`",
                              "`/devenv:lean-off`"):
                    self.assertIn(usage, text)


class LeanOffSkillTests(test_docs.Base):
    @classmethod
    def setUpClass(cls):
        cls.raw = test_docs.read(LEAN_OFF)
        cls.text = flat(cls.raw)

    def test_one_bang_line_turns_lean_off_without_redirection(self):
        bang = [line for line in body(self.raw).splitlines() if line.startswith("!`")]
        self.assertEqual([OFF_LINE], bang)
        self.assertNotIn(">", bang[0])

    def test_reports_the_result_in_one_line(self):
        self.matches(r"(?i)report the result in one line")


class CycleSkillTests(test_docs.Base):
    @classmethod
    def setUpClass(cls):
        cls.raw = test_docs.read(CYCLE)
        cls.text = flat(cls.raw)
        cls.rules = rules_layer()

    def test_override_points_match_the_rules_layer(self):
        rows = re.findall(r"^\| `([a-z-]+)` \|", self.raw, re.M)
        self.assertEqual(list(self.rules.CYCLE_IDS), rows)
        self.has("~/.claude/devenv.local.md")
        self.has("<toplevel>/.claude/devenv.local.md")
        self.has(self.rules.OUTSIDE_WARNING)
        self.has("/devenv:doctor init-project")
        self.has("`.claude/devenv.local.md` in `.gitignore`")

    def test_steps_run_once_in_order_without_re_review(self):
        self.matches(r"(?s)PLAN.{0,400}?CRITIC.{0,400}?EXECUTE.{0,400}?VERIFY")
        self.matches(r"(?s)one planner run.{0,600}?one critic lane.{0,600}?no re-review after the revision")
        self.matches(r"(?is)one adversarial verify pass.{0,300}?only H findings")

    def test_roles_map_to_agent_type_model_and_effort(self):
        self.has("| Role | Agent type | Model | Effort |")
        for role in ("planner", "critic", "executor", "verifier"):
            with self.subTest(role=role):
                self.matches(r"^\| " + role + r" \| `general-purpose` \| `[a-z]+` \| `[a-z]+` \|", self.raw, re.M)
        self.matches(r"(?i)a rules layer may replace the agent type, model or effort of any role")
        self.matches(r"(?i)every agent call names model and effort explicitly")

    def test_finding_format_and_evidence(self):
        self.has("`{severity, claim, evidence, location}`")
        self.matches(r"(?s)`evidence` is the command that was run and its output, or the quoted line and"
                     r"\s+its call path")

    def test_unsupported_h_is_counted_as_m_and_kept_verbatim(self):
        self.matches(r"(?s)An H without evidence counts as M and is not fixed")
        self.matches(r"(?s)`Unsupported H` list.{0,40}?word for word")
        self.matches(r"(?s)security or data-loss H.{0,120}?top of the report.{0,40}?even when it is downgraded")

    def test_role_return_rules(self):
        for phrase in (
            "The last message is the deliverable.",
            "Never end on a message without content.",
            "Stay within the line limit the order sets.",
            "Do not run anything in the background; return as soon as the work is done.",
            "Line 2 is `H n · M n · L n`.",
            "Every finding has the four fields.",
            "A return carries at most 10 findings.",
            "When the order names a report file, write the details only to that file and return a summary"
            " of 20 lines or fewer with its path.",
            "Return the list of changed files and each verification command with its result.",
        ):
            with self.subTest(phrase=phrase):
                self.has(phrase)


if __name__ == "__main__":
    unittest.main()
