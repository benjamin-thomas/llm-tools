#!/usr/bin/env python3
# Type checking:
#   pyright --project pyrightconfig.json
#
# VS Code:
#   Install/enable the Python extension and Pylance. Pylance reads
#   pyrightconfig.json in this repository, so type errors in this file should
#   appear directly in the editor.

from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import shutil
import sys
import textwrap
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence, cast


CliName = Literal["codex", "claude", "gemini", "qwen", "pi"]
CliStorage = Literal["skill_dir", "prompt_file"]
ColorMode = Literal["auto", "always", "never"]
StatusValue = Literal[
    "managed",
    "repo_only",
    "local_only",
    "conflict",
    "foreign_link",
    "broken_link",
]

GLOBAL_SKILLS_DIR_NAME = "global-skills"
SKILL_MANIFEST_NAME = "SKILL.md"
CLI_NAMES: tuple[CliName, ...] = ("codex", "claude", "gemini", "qwen", "pi")


class SkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class CliSpec:
    name: CliName
    home_env_var: str
    default_home: str
    local_subpath: str
    storage: CliStorage


@dataclass(frozen=True)
class SkillInspection:
    cli: CliName
    skill: str
    status: StatusValue
    repo_path: pathlib.Path
    local_path: pathlib.Path
    detail: str
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusPresentation:
    color: str


CLI_SPECS: dict[CliName, CliSpec] = {
    "codex": CliSpec("codex", "CODEX_HOME", "~/.codex", "skills", "skill_dir"),
    "claude": CliSpec("claude", "CLAUDE_HOME", "~/.claude", "skills", "skill_dir"),
    "gemini": CliSpec("gemini", "GEMINI_HOME", "~/.gemini", "skills", "skill_dir"),
    "qwen": CliSpec("qwen", "QWEN_HOME", "~/.qwen", "skills", "skill_dir"),
    "pi": CliSpec("pi", "PI_HOME", "~/.pi", "agent/prompts", "prompt_file"),
}

STATUS_PRESENTATION: dict[StatusValue, StatusPresentation] = {
    "managed": StatusPresentation("32"),
    "repo_only": StatusPresentation("36"),
    "local_only": StatusPresentation("33"),
    "conflict": StatusPresentation("31;1"),
    "foreign_link": StatusPresentation("35"),
    "broken_link": StatusPresentation("31;1"),
}


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def global_skills_dir(root: pathlib.Path) -> pathlib.Path:
    return root / GLOBAL_SKILLS_DIR_NAME


def shell_quote_path(path: pathlib.Path) -> str:
    return shlex.quote(str(path))


def shell_quote_text(text: str) -> str:
    return shlex.quote(text)


def parse_cli_name(value: str) -> CliName:
    if value not in CLI_SPECS:
        names = ", ".join(CLI_NAMES)
        raise SkillError(f"unknown CLI: {value}; expected one of: {names}")
    return value


def local_home_for(spec: CliSpec, env: Mapping[str, str]) -> pathlib.Path:
    configured = env.get(spec.home_env_var)
    raw_path = configured if configured else spec.default_home
    return pathlib.Path(raw_path).expanduser()


def local_skills_dir(spec: CliSpec, env: Mapping[str, str]) -> pathlib.Path:
    return local_home_for(spec, env) / spec.local_subpath


def repo_skill_path(root: pathlib.Path, cli: CliName, skill: str) -> pathlib.Path:
    return global_skills_dir(root) / cli / skill


def local_skill_path(cli: CliName, skill: str, env: Mapping[str, str]) -> pathlib.Path:
    spec = CLI_SPECS[cli]
    local_name = f"{skill}.md" if spec.storage == "prompt_file" else skill
    return local_skills_dir(spec, env) / local_name


def repo_install_source_path(root: pathlib.Path, cli: CliName, skill: str) -> pathlib.Path:
    repo_path = repo_skill_path(root, cli, skill)
    if CLI_SPECS[cli].storage == "prompt_file":
        return repo_path / SKILL_MANIFEST_NAME
    return repo_path


def skill_has_manifest(path: pathlib.Path) -> bool:
    return (path / SKILL_MANIFEST_NAME).is_file()


def local_entry_exists(path: pathlib.Path) -> bool:
    return path.exists() or path.is_symlink()


def sorted_skill_dirs(parent: pathlib.Path) -> list[str]:
    if not parent.exists():
        return []
    names: list[str] = []
    for child in parent.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir() or child.is_symlink():
            names.append(child.name)
    return sorted(names)


def repo_skills(root: pathlib.Path, cli: CliName) -> list[str]:
    parent = global_skills_dir(root) / cli
    return [
        skill_name
        for skill_name in sorted_skill_dirs(parent)
        if skill_has_manifest(parent / skill_name)
    ]


def local_skills(cli: CliName, env: Mapping[str, str]) -> list[str]:
    spec = CLI_SPECS[cli]
    parent = local_skills_dir(spec, env)
    if spec.storage == "skill_dir":
        return sorted_skill_dirs(parent)

    if not parent.exists():
        return []
    names: list[str] = []
    for child in parent.iterdir():
        if child.name.startswith("."):
            continue
        if child.suffix == ".md" and (child.is_file() or child.is_symlink()):
            names.append(child.stem)
    return sorted(names)


def resolves_to(path: pathlib.Path, expected: pathlib.Path) -> bool:
    return path.resolve(strict=False) == expected.resolve(strict=False)


def relative_symlink_target(source: pathlib.Path, link_parent: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.relpath(source, start=link_parent))


def command_install(cli: CliName, skill: str) -> str:
    return f"llm-skills install {shell_quote_text(cli)} {shell_quote_text(skill)}"


def command_import(cli: CliName, skill: str) -> str:
    return f"llm-skills import {shell_quote_text(cli)} {shell_quote_text(skill)}"


def command_diff(local_path: pathlib.Path, repo_path: pathlib.Path) -> str:
    return f"diff -ru {shell_quote_path(local_path)} {shell_quote_path(repo_path)}"


def command_backup_then_install(cli: CliName, skill: str, local_path: pathlib.Path) -> str:
    backup_path = local_path.with_name(f"{local_path.name}.backup")
    return f"mv {shell_quote_path(local_path)} {shell_quote_path(backup_path)} && {command_install(cli, skill)}"


def inspect_skill(root: pathlib.Path, cli: CliName, skill: str, env: Mapping[str, str]) -> SkillInspection:
    repo_path = repo_skill_path(root, cli, skill)
    repo_source_path = repo_install_source_path(root, cli, skill)
    local_path = local_skill_path(cli, skill, env)
    repo_exists = skill_has_manifest(repo_path)
    local_exists = local_entry_exists(local_path)

    if repo_exists and local_path.is_symlink() and resolves_to(local_path, repo_source_path):
        return SkillInspection(cli, skill, "managed", repo_path, local_path, "installed as a symlink to the repo")

    if repo_exists and not local_exists:
        return SkillInspection(
            cli,
            skill,
            "repo_only",
            repo_path,
            local_path,
            "versioned in the repo but not installed locally",
            (command_install(cli, skill),),
        )

    if repo_exists and local_path.is_symlink():
        if not local_path.exists():
            return SkillInspection(
                cli,
                skill,
                "broken_link",
                repo_path,
                local_path,
                "local path is a broken symlink",
                (f"rm {shell_quote_path(local_path)} && {command_install(cli, skill)}",),
            )
        return SkillInspection(
            cli,
            skill,
            "foreign_link",
            repo_path,
            local_path,
            "local path is a symlink, but not to this repo",
            (command_diff(local_path, repo_source_path),),
        )

    if repo_exists and local_exists:
        return SkillInspection(
            cli,
            skill,
            "conflict",
            repo_path,
            local_path,
            "repo has this skill, but a real local directory is already present",
            (
                command_diff(local_path, repo_source_path),
                command_backup_then_install(cli, skill, local_path),
            ),
        )

    if local_exists and not repo_exists:
        if local_path.is_symlink():
            return SkillInspection(
                cli,
                skill,
                "foreign_link",
                repo_path,
                local_path,
                "local skill is a symlink outside the managed repo",
                (),
            )
        return SkillInspection(
            cli,
            skill,
            "local_only",
            repo_path,
            local_path,
            "local skill is not versioned in this repo",
            (command_import(cli, skill),),
        )

    raise SkillError(f"could not inspect missing skill: {cli}/{skill}")


def inspect_cli(root: pathlib.Path, cli: CliName, env: Mapping[str, str]) -> list[SkillInspection]:
    skill_names = sorted(set(repo_skills(root, cli)) | set(local_skills(cli, env)))
    return [inspect_skill(root, cli, skill_name, env) for skill_name in skill_names]


def selected_clis(cli: str | None) -> tuple[CliName, ...]:
    if cli is None:
        return CLI_NAMES
    return (parse_cli_name(cli),)


def parse_color_mode(value: str) -> ColorMode:
    if value not in ("auto", "always", "never"):
        raise SkillError(f"unknown color mode: {value}")
    return value


def should_use_color(mode: ColorMode) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def colorize(text: str, color_code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{color_code}m{text}\033[0m"


def colored_status(status: StatusValue, enabled: bool) -> str:
    presentation = STATUS_PRESENTATION[status]
    return colorize(f"{status:<12}", presentation.color, enabled)


def print_inspections(inspections: Sequence[SkillInspection], *, color: bool) -> None:
    if not inspections:
        print("No skills found.")
        return

    current_cli: CliName | None = None
    for item in inspections:
        if item.cli != current_cli:
            if current_cli is not None:
                print()
            current_cli = item.cli
            print(f"{colorize(item.cli, '1', color)}:")
        print(f"  {colored_status(item.status, color)} {item.skill:<28} {item.detail}")


def doctor_command(
    root: pathlib.Path,
    cli: str | None,
    env: Mapping[str, str],
    *,
    color_mode: ColorMode = "auto",
) -> int:
    all_inspections: list[SkillInspection] = []
    for cli_name in selected_clis(cli):
        all_inspections.extend(inspect_cli(root, cli_name, env))

    color = should_use_color(color_mode)
    print_inspections(all_inspections, color=color)

    actionable = [item for item in all_inspections if item.commands]
    if not actionable:
        print()
        print(colorize("No action needed.", "32", color))
    return 0


def list_command(
    root: pathlib.Path,
    cli: str | None,
    env: Mapping[str, str],
    *,
    color_mode: ColorMode = "auto",
) -> int:
    return doctor_command(root, cli, env, color_mode=color_mode)


def install_one(root: pathlib.Path, cli: CliName, skill: str, env: Mapping[str, str], *, dry_run: bool) -> None:
    repo_path = repo_skill_path(root, cli, skill)
    source = repo_install_source_path(root, cli, skill)
    if not skill_has_manifest(repo_path):
        raise SkillError(f"repo skill does not exist: {repo_path}")

    target_parent = local_skills_dir(CLI_SPECS[cli], env)
    target = local_skill_path(cli, skill, env)

    if target.is_symlink() and resolves_to(target, source):
        print(f"ok: {cli}/{skill} already installed")
        return

    if local_entry_exists(target):
        inspection = inspect_skill(root, cli, skill, env)
        print(f"conflict: {cli}/{skill}: {inspection.detail}", file=sys.stderr)
        for command in inspection.commands:
            print(f"  suggested: {command}", file=sys.stderr)
        raise SkillError(f"refusing to overwrite {target}")

    link_target = relative_symlink_target(source, target_parent)
    if dry_run:
        print(f"would create symlink: {target} -> {link_target}")
        return

    target_parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(link_target, target_is_directory=CLI_SPECS[cli].storage == "skill_dir")
    print(f"installed: {cli}/{skill} -> {source}")


def install_command(
    root: pathlib.Path,
    cli: str | None,
    skill: str | None,
    env: Mapping[str, str],
    *,
    all_skills: bool,
    dry_run: bool,
) -> int:
    if all_skills:
        for cli_name in selected_clis(cli):
            for skill_name in repo_skills(root, cli_name):
                install_one(root, cli_name, skill_name, env, dry_run=dry_run)
        return 0

    if cli is None or skill is None:
        raise SkillError("install needs <cli> <skill>, <cli> --all, or --all")

    install_one(root, parse_cli_name(cli), skill, env, dry_run=dry_run)
    return 0


def import_one(root: pathlib.Path, cli: CliName, skill: str, env: Mapping[str, str], *, dry_run: bool) -> None:
    spec = CLI_SPECS[cli]
    source = local_skill_path(cli, skill, env)
    target = repo_skill_path(root, cli, skill)
    target_source = repo_install_source_path(root, cli, skill)

    if source.is_symlink():
        if skill_has_manifest(target) and resolves_to(source, target_source):
            print(f"ok: {cli}/{skill} is already managed")
            return
        raise SkillError(f"refusing to import symlinked local skill: {source}")

    if not source.exists():
        raise SkillError(f"local skill does not exist: {source}")

    if spec.storage == "skill_dir":
        if not source.is_dir():
            raise SkillError(f"local skill is not a directory: {source}")
        if not skill_has_manifest(source):
            raise SkillError(f"local skill has no {SKILL_MANIFEST_NAME}: {source}")
    elif not source.is_file():
        raise SkillError(f"local prompt is not a file: {source}")

    if local_entry_exists(target):
        print(f"repo target already exists: {target}", file=sys.stderr)
        print(f"  suggested: {command_diff(source, target_source)}", file=sys.stderr)
        raise SkillError(f"refusing to overwrite repo skill: {target}")

    link_target = relative_symlink_target(target_source, source.parent)
    if dry_run:
        print(f"would move: {source} -> {target_source if spec.storage == 'prompt_file' else target}")
        print(f"would create symlink: {source} -> {link_target}")
        return

    if spec.storage == "skill_dir":
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    else:
        target.mkdir(parents=True, exist_ok=False)
        shutil.move(str(source), str(target_source))
    source.symlink_to(link_target, target_is_directory=spec.storage == "skill_dir")
    print(f"imported: {cli}/{skill} -> {target}")


def import_command(root: pathlib.Path, cli: str, skill: str, env: Mapping[str, str], *, dry_run: bool) -> int:
    import_one(root, parse_cli_name(cli), skill, env, dry_run=dry_run)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage versioned global skills for local AI CLIs with per-skill symlinks.",
        epilog=textwrap.dedent(
            """\
            Repository layout:
              global-skills/<cli>/<skill>/SKILL.md

            Supported CLIs and default local folders:
              codex   ${CODEX_HOME:-~/.codex}/skills
              claude  ${CLAUDE_HOME:-~/.claude}/skills
              gemini  ${GEMINI_HOME:-~/.gemini}/skills
              qwen    ${QWEN_HOME:-~/.qwen}/skills
              pi      ${PI_HOME:-~/.pi}/agent/prompts/*.md

            Typical workflow:
              llm-skills doctor
              llm-skills import codex worker-orchestrator
              llm-skills import pi tdd
              llm-skills install --all

            Safety model:
              doctor never writes.
              install only creates missing symlinks.
              import moves one real local skill/prompt into global-skills, then symlinks it back.
              existing real directories or prompt files are never overwritten automatically.
            """
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=str(repo_root()),
        help="repository root containing global-skills (default: directory containing llm_skills.py)",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize doctor/list output: auto, always, or never (default: auto)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="inspect repo/local skills",
        description="Inspect skills without modifying anything.",
    )
    doctor_parser.add_argument("--cli", choices=CLI_NAMES, help="limit inspection to one CLI")

    list_parser = subparsers.add_parser("list", help="alias for doctor")
    list_parser.add_argument("--cli", choices=CLI_NAMES, help="limit listing to one CLI")

    install_parser = subparsers.add_parser(
        "install",
        help="install repo skills locally as symlinks",
        description="Create symlinks from each CLI skill directory back to global-skills.",
    )
    install_parser.add_argument("cli", nargs="?", choices=CLI_NAMES)
    install_parser.add_argument("skill", nargs="?")
    install_parser.add_argument("--all", action="store_true", help="install all repo skills, optionally limited by <cli>")
    install_parser.add_argument("--dry-run", action="store_true", help="print filesystem operations without writing")

    import_parser = subparsers.add_parser(
        "import",
        help="move one local skill into the repo and symlink it back",
        description="Import a real local skill directory into global-skills/<cli>/<skill>.",
    )
    import_parser.add_argument("cli", choices=CLI_NAMES)
    import_parser.add_argument("skill")
    import_parser.add_argument("--dry-run", action="store_true", help="print filesystem operations without writing")

    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    effective_env = os.environ if env is None else env
    root = pathlib.Path(cast(str, args.repo_root)).expanduser().resolve()

    try:
        if args.command == "doctor":
            return doctor_command(
                root,
                cast(str | None, args.cli),
                effective_env,
                color_mode=parse_color_mode(cast(str, args.color)),
            )
        if args.command == "list":
            return list_command(
                root,
                cast(str | None, args.cli),
                effective_env,
                color_mode=parse_color_mode(cast(str, args.color)),
            )
        if args.command == "install":
            return install_command(
                root,
                cast(str | None, args.cli),
                cast(str | None, args.skill),
                effective_env,
                all_skills=cast(bool, args.all),
                dry_run=cast(bool, args.dry_run),
            )
        if args.command == "import":
            return import_command(
                root,
                cast(str, args.cli),
                cast(str, args.skill),
                effective_env,
                dry_run=cast(bool, args.dry_run),
            )
    except SkillError as exc:
        print(f"llm-skills: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
