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

    def make_local_skill(self, home: pathlib.Path, skill: str) -> pathlib.Path:
        skill_path = home / "skills" / skill
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        return skill_path

    def make_local_pi_prompt(self, home: pathlib.Path, skill: str) -> pathlib.Path:
        prompt_path = home / "agent" / "prompts" / f"{skill}.md"
        prompt_path.parent.mkdir(parents=True)
        prompt_path.write_text(f"# {skill}\n", encoding="utf-8")
        return prompt_path

    def test_install_creates_relative_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "codex-home"
            repo_skill = self.make_repo_skill(root, "codex", "orchestrate")
            env = {"CODEX_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.install_command(root, "codex", "orchestrate", env, all_skills=False, dry_run=False)

            local_skill = home / "skills" / "orchestrate"
            self.assertEqual(code, 0)
            self.assertTrue(local_skill.is_symlink())
            self.assertEqual(local_skill.resolve(strict=True), repo_skill.resolve(strict=True))
            self.assertFalse(str(local_skill.readlink()).startswith("/"))

    def test_import_moves_local_skill_into_repo_and_symlinks_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "qwen-home"
            local_skill = self.make_local_skill(home, "review")
            env = {"QWEN_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.import_command(root, "qwen", "review", env, dry_run=False)

            repo_skill = root / "global-skills" / "qwen" / "review"
            self.assertEqual(code, 0)
            self.assertTrue(repo_skill.is_dir())
            self.assertTrue((repo_skill / "SKILL.md").is_file())
            self.assertTrue(local_skill.is_symlink())
            self.assertEqual(local_skill.resolve(strict=True), repo_skill.resolve(strict=True))

    def test_import_moves_pi_prompt_into_repo_skill_and_symlinks_file_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "pi-home"
            local_prompt = self.make_local_pi_prompt(home, "tdd")
            env = {"PI_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.import_command(root, "pi", "tdd", env, dry_run=False)

            repo_skill = root / "global-skills" / "pi" / "tdd"
            self.assertEqual(code, 0)
            self.assertTrue((repo_skill / "SKILL.md").is_file())
            self.assertTrue(local_prompt.is_symlink())
            self.assertEqual(local_prompt.resolve(strict=True), (repo_skill / "SKILL.md").resolve(strict=True))

    def test_install_creates_pi_prompt_symlink_to_repo_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "pi-home"
            repo_skill = self.make_repo_skill(root, "pi", "task-work")
            env = {"PI_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.install_command(root, "pi", "task-work", env, all_skills=False, dry_run=False)

            local_prompt = home / "agent" / "prompts" / "task-work.md"
            self.assertEqual(code, 0)
            self.assertTrue(local_prompt.is_symlink())
            self.assertEqual(local_prompt.resolve(strict=True), (repo_skill / "SKILL.md").resolve(strict=True))
            self.assertFalse(str(local_prompt.readlink()).startswith("/"))

    def test_install_refuses_existing_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "gemini-home"
            self.make_repo_skill(root, "gemini", "review")
            self.make_local_skill(home, "review")
            env = {"GEMINI_HOME": str(home)}
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors), self.assertRaises(llm_skills.SkillError):
                llm_skills.install_command(root, "gemini", "review", env, all_skills=False, dry_run=False)

    def test_doctor_is_compact_for_local_only_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "claude-home"
            self.make_local_skill(home, "ask-gpt")
            env = {"CLAUDE_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.doctor_command(root, "claude", env)

            self.assertEqual(code, 0)
            self.assertIn("local_only", output.getvalue())
            self.assertNotIn("llm-skills import claude ask-gpt", output.getvalue())

    def test_doctor_is_compact_for_repo_only_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "codex-home"
            self.make_repo_skill(root, "codex", "tmux-orchestrator")
            env = {"CODEX_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.doctor_command(root, "codex", env)

            self.assertEqual(code, 0)
            self.assertIn("repo_only", output.getvalue())
            self.assertNotIn("llm-skills install codex tmux-orchestrator", output.getvalue())

    def test_doctor_can_force_color(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "codex-home"
            self.make_repo_skill(root, "codex", "tmux-orchestrator")
            env = {"CODEX_HOME": str(home)}
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = llm_skills.doctor_command(root, "codex", env, color_mode="always")

            self.assertEqual(code, 0)
            self.assertIn("\033[36m", output.getvalue())
            self.assertIn("repo_only", output.getvalue())

    def test_import_refuses_to_overwrite_existing_repo_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            home = pathlib.Path(tmp) / "codex-home"
            self.make_repo_skill(root, "codex", "tdd")
            self.make_local_skill(home, "tdd")
            env = {"CODEX_HOME": str(home)}
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors), self.assertRaises(llm_skills.SkillError):
                llm_skills.import_command(root, "codex", "tdd", env, dry_run=False)


if __name__ == "__main__":
    unittest.main()
