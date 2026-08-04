# Global Skills

This directory is the versioned source of truth for global CLI skills.

Layout — **two sections**, not one directory per CLI:

```text
global-skills/
  claude/<skill>/SKILL.md   # Claude Code only
  agents/<skill>/SKILL.md   # shared by every other agent
```

Claude Code gets its own section because its skills are written against Claude's
tooling and are noticeably richer. Everything else shares one copy.

Each section installs into exactly one directory:

```text
claude  ->  ~/.claude/skills
other   ->  ~/.agents/skills      (Codex, Kimi, ...)
```

`~/.agents/skills` is the cross-agent convention: Codex reads it (verified
against 0.146.0 — with `~/.codex/skills` removed entirely it still loads skills
from `~/.agents/skills`), and so does Kimi. Adding another agent that follows
the convention costs nothing — it already reads the directory `other` installs
to.

Leave `~/.codex/skills` alone: Codex recreates it for its own bundled
`.system` skills. Nothing of ours belongs there.

Use `llm-skills doctor` to inspect local skills and print the exact commands
to import or install them. `doctor` never writes to disk.

Common commands:

```bash
llm-skills doctor
llm-skills --color always doctor
llm-skills doctor --cli other          # limit to one section
llm-skills import other worker-orchestrator
llm-skills install --all
```

`doctor` prints, per skill, a short md5 of its `SKILL.md` and the two paths the
symlink connects. It finishes with any same-named skills whose contents differ
between the two sections. A difference is not automatically wrong — the sections
exist precisely so they *can* differ — but it is how a skill silently rots in
one section (an abridged copy that dropped half its content), so it is worth a
look rather than being invisible.

`import` moves one real local skill directory into this repository and creates a
symlink back to the CLI's native skills directory. `install` only creates
missing symlinks for skills already present in this repository.
