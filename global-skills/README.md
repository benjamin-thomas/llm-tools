# Global Skills

This directory is the versioned source of truth for global CLI skills.

Layout:

```text
global-skills/
  codex/<skill>/SKILL.md
  claude/<skill>/SKILL.md
  gemini/<skill>/SKILL.md
  qwen/<skill>/SKILL.md
  pi/<prompt>/SKILL.md
```

Use `llm-skills doctor` to inspect local skills and print the exact commands
to import or install them. `doctor` never writes to disk.

Common commands:

```bash
llm-skills doctor
llm-skills --color always doctor
llm-skills import codex worker-orchestrator
llm-skills import pi tdd
llm-skills install --all
```

`import` moves one real local skill directory into this repository and creates a
symlink back to the CLI's native skills directory. For `pi`, it moves one
`~/.pi/agent/prompts/<name>.md` prompt into `global-skills/pi/<name>/SKILL.md`
and symlinks the prompt file back. `install` only creates missing symlinks for
skills already present in this repository.
