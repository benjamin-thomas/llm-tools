---
name: knowledge-enrich
description: Proactively inspect the codebase to find non-obvious knowledge gaps on a given subject and propose focused _knowledge updates one entry at a time.
---

# Knowledge Enrich

Use this to go looking for knowledge that is missing from `_knowledge/`, rather than merely capturing what the current session already learned.

## Purpose of the Knowledge Base

The knowledge base serves two readers:
- a human who needs fast onboarding
- the next LLM agent who needs high-signal context

It should capture what the system does and why when that knowledge is non-obvious, cross-cutting, or otherwise hard to infer from reading a single file. Point to code for obvious details instead of restating them.

## Subject Handling

This skill accepts up to 3 subjects.

- With subject arguments: explore the codebase through those lenses, one at a time.
- Without a subject: survey the codebase and current knowledge base, then propose 3-5 candidate subjects ranked by likely value. The user picks up to 3.

## Workflow

1. Read the current knowledge base
- If `_knowledge/` exists, read `_knowledge/INDEX.md` and the existing knowledge files needed to understand current coverage.

2. Explore
- Investigate each subject using whatever methods fit:
  - read code
  - trace call paths
  - search for patterns
  - inspect history when helpful
- Look for cross-cutting concerns, invariants, architectural decisions, or domain mappings that are not obvious from one file.

3. Process findings one entry at a time
- For each worthwhile finding:
  - present what you found and why it seems worth documenting
  - ask the user if it is genuinely non-obvious and whether there is added context
  - propose the exact knowledge entry and any `INDEX.md` updates
  - wait for explicit approval
  - apply the approved change
- Do not batch multiple proposed entries together.

4. Wrap up
- Summarize what was added.
- Mention future candidate subjects if useful.

## Rules

- Never write without explicit user approval.
- Never skip the interview/discussion before proposing an entry.
- Never document what is already obvious from the code; point to the code instead.
- Max 3 subjects per run.
- Accept "that's obvious, skip it" as a valid decision.
- If you notice drift in existing knowledge, flag it. For larger drift, suggest `knowledge-verify`.
