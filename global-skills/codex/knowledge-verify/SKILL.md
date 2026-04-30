---
name: knowledge-verify
description: Verify the _knowledge base against the codebase for missing, incorrect, or obsolete knowledge, using multi-layer Codex sub-agent review before proposing fixes.
---

# Knowledge Verify

Systematically check `_knowledge/` for drift against the actual codebase.

## Purpose of the Knowledge Base

The knowledge base serves two readers:
- a human who needs fast onboarding
- the next LLM agent who needs high-signal context

It is a condensed second specification. It should stay aligned with the code, documenting what the system does and why when that information is subtle, cross-cutting, or easy to misunderstand.

## Drift Types

1. Missing: non-obvious knowledge exists in the codebase but is not documented.
2. Incorrect: documented knowledge no longer matches the code.
3. Obsolete: documented knowledge describes something removed or no longer relevant.

## Workflow

1. Survey the knowledge base
- Read `_knowledge/INDEX.md` and all knowledge files recursively.
- Build an inventory of claims:
  - referenced paths
  - referenced symbols
  - invariants
  - architectural assertions
  - domain concepts
- Size the review effort based on KB size.

2. Dispatch parallel sub-agents
- Use up to 10 sub-agents in the initial pass.
- Scale by KB size:
  - small KB: 2 fast agents + 1 deeper agent
  - medium KB: 4 fast agents + 2 deeper agents
  - large KB: 7 fast agents + 3 deeper agents
- Feed each agent the relevant knowledge file content directly. Do not make them rediscover it.

Fast agents should check:
- referenced files still exist
- referenced symbols still exist
- dependency/build references still exist
- `relates` links point to real categories or files

Deeper agents should check:
- `INDEX.md` consistency
- duplicated concepts across knowledge files
- whether code meaning still matches the documented explanation
- whether major code areas changed after the knowledge file's `updated` date
- whether there are important gaps not captured in `_knowledge/`

Recommended agent choices:
- use `explorer` agents for mechanical and structure checks
- use stronger default agents for semantic checks when needed

3. Escalate for confidence
- Confirm promising fast-agent findings with a deeper agent.
- Confirm deeper semantic findings with the strongest practical agent you have available.
- If both layers find nothing, do one final high-level pass focused on invariants, boundaries, and domain accuracy rather than file existence.

4. Collect and deduplicate
- Synthesize the confirmed findings.
- Group by knowledge file or gap.

5. Build the findings list
- For each surviving finding, record:
  - knowledge file or gap
  - drift type
  - concise description
  - supporting evidence from code and knowledge text

If no findings survive review, report:
`Knowledge base verified - no drift detected.`

6. Walk through findings one at a time
- Present one finding.
- Ask the user for confirmation or nuance.
- Propose the exact content change.
- Wait for explicit approval.
- Apply it if approved.
- Move to the next finding.

Do not batch multiple findings into one proposal.

## Rules

- Never modify knowledge files without explicit user approval.
- Never skip the multi-layer verification before presenting findings.
- Never dump raw sub-agent output to the user; synthesize it first.
- Adapt the number of agents to KB size.
- Discard nonsensical or clearly corrupted sub-agent findings.
- Keep the walk-through focused: one finding, one decision, then move on.
