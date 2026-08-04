from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest

import llm_skills


class LlmSkillsTest(unittest.TestCase):
    def make_repo_skill(self, root: pathlib.Path, cli: str, skill: str) -> pathlib.Path:
        skill_path = root / "global-skills" / cli / skill
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        return skill_path

    def other_env(self, tmp: pathlib.Path) -> dict[str, str]:
        """Home for the "other" section, pinned inside the tmpdir.

        Must be pinned: an unset AGENTS_HOME defaults to the real ~/.agents,
        which would make the suite write into the user's actual skills dir.
        """
        return {"AGENTS_HOME": str(tmp / "agents-home")}

    def make_local_skill(self, home: pathlib.Path, skill: str) -> pathlib.Path:
        skill_path = home / "skills" / skill
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        return skill_path

    def test_install_creates_relative_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            repo_skill = self.make_repo_skill(root, "other", "orchestrate")
            env = self.other_env(pathlib.Path(tmp))
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.install_command(root, "other", "orchestrate", env, all_skills=False, dry_run=False)

            self.assertEqual(code, 0)
            local_skill = pathlib.Path(tmp) / "agents-home" / "skills" / "orchestrate"
            self.assertTrue(local_skill.is_symlink())
            self.assertEqual(local_skill.resolve(strict=True), repo_skill.resolve(strict=True))
            self.assertFalse(str(local_skill.readlink()).startswith("/"))

    def test_import_moves_local_skill_into_repo_and_symlinks_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "claude-home"
            local_skill = self.make_local_skill(home, "review")
            env = {"CLAUDE_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.import_command(root, "claude", "review", env, dry_run=False)

            repo_skill = root / "global-skills" / "claude" / "review"
            self.assertEqual(code, 0)
            self.assertTrue(repo_skill.is_dir())
            self.assertTrue((repo_skill / "SKILL.md").is_file())
            self.assertTrue(local_skill.is_symlink())
            self.assertEqual(local_skill.resolve(strict=True), repo_skill.resolve(strict=True))

    def test_install_refuses_existing_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "claude-home"
            self.make_repo_skill(root, "claude", "review")
            self.make_local_skill(home, "review")
            env = {"CLAUDE_HOME": str(home)}
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors), self.assertRaises(llm_skills.SkillError):
                llm_skills.install_command(root, "claude", "review", env, all_skills=False, dry_run=False)

    def test_doctor_is_compact_for_local_only_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "claude-home"
            self.make_local_skill(home, "browser-tools")
            env = {"CLAUDE_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.doctor_command(root, "claude", env)

            self.assertEqual(code, 0)
            self.assertIn("local_only", output.getvalue())
            self.assertNotIn("llm-skills import claude browser-tools", output.getvalue())

    def test_doctor_is_compact_for_repo_only_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            self.make_repo_skill(root, "other", "tmux-orchestrator")
            env = self.other_env(pathlib.Path(tmp))
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.doctor_command(root, "other", env)

            self.assertEqual(code, 0)
            self.assertIn("repo_only", output.getvalue())
            self.assertNotIn("llm-skills install other tmux-orchestrator", output.getvalue())

    def test_doctor_can_force_color(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            self.make_repo_skill(root, "other", "tmux-orchestrator")
            env = self.other_env(pathlib.Path(tmp))
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.doctor_command(root, "other", env, color_mode="always")

            self.assertEqual(code, 0)
            self.assertIn("\033[36m", output.getvalue())
            self.assertIn("repo_only", output.getvalue())

    def test_import_refuses_to_overwrite_existing_repo_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            self.make_repo_skill(root, "other", "tdd")
            self.make_local_skill(pathlib.Path(tmp) / "agents-home", "tdd")
            env = self.other_env(pathlib.Path(tmp))
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors), self.assertRaises(llm_skills.SkillError):
                llm_skills.import_command(root, "other", "tdd", env, dry_run=False)


class CrossSectionDriftTest(unittest.TestCase):
    """Same-named skills that drift apart between sections must be reported.

    Two sections exist so Claude can carry a richer variant, so a difference is
    not automatically a bug. It is, however, exactly how a skill rots unnoticed
    — one section quietly gets an abridged copy — so doctor surfaces it.
    """

    def write(self, root: pathlib.Path, section: str, skill: str, body: str) -> None:
        path = root / "global-skills" / section / skill
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(body, encoding="utf-8")

    def test_identical_copies_are_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "claude", "tdd", "# same\n")
            self.write(root, "other", "tdd", "# same\n")
            self.assertEqual(llm_skills.cross_section_drift(root), [])

    def test_differing_copies_are_reported_with_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "claude", "tdd", "# full version with examples\n")
            self.write(root, "other", "tdd", "# abridged\n")
            drift = llm_skills.cross_section_drift(root)

            self.assertEqual([skill for skill, _ in drift], ["tdd"])
            sigs = drift[0][1]
            self.assertNotEqual(sigs["claude"], sigs["other"])
            self.assertEqual(len(sigs["claude"]), 8)

    def test_a_skill_present_in_only_one_section_is_not_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "claude", "improve-code-quality", "# claude only\n")
            self.assertEqual(llm_skills.cross_section_drift(root), [])

    def test_doctor_prints_the_drift_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "claude", "tdd", "# full\n")
            self.write(root, "other", "tdd", "# abridged\n")
            env = {
                "CLAUDE_HOME": str(root / "claude-home"),
                "AGENTS_HOME": str(root / "agents-home"),
            }
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                llm_skills.doctor_command(root, None, env, color_mode="never")

            text = output.getvalue()
            self.assertIn("differ across sections", text)
            self.assertIn("tdd", text)


if __name__ == "__main__":
    unittest.main()
