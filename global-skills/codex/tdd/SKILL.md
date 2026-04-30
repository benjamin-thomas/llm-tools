---
name: tdd
description: Execute strict test-driven development with explicit RED-GREEN-REFACTOR checkpoints. Use when implementing or changing code should be driven by failing tests first, with user approval between phases, minimal implementation scope, and clear reporting of failures/passing status.
---

# TDD

## Workflow

Follow strict RED -> GREEN -> REFACTOR discipline.

1. RED phase
- Clarify the target behavior.
- Write one test for one behavior.
- Run the targeted test.
- Verify the test fails for the expected reason.
- If the test passes unexpectedly, stop and investigate before writing implementation.
- Report using this template:

```text
## RED Phase Complete
- Test file: <path>
- Test name: <description>
- Failure: <key error message>
- Planned implementation: <minimal change>

Continue? (y/N)
```

- Stop and wait for user approval.

2. GREEN phase
- Write the minimal implementation to pass the failing test.
- Run the same targeted test.
- Confirm it passes.
- Report using this template:

```text
## GREEN Phase Complete
- Implementation: <path(s)>
- Test status: PASSING

**Next test intention**: <next behavior>
I'll add a test that:
- <setup step 1>
- <setup step 2>
- <assertion>

Continue? (y/N)
```

- Stop and wait for user approval.

3. REFACTOR phase (optional)
- Refactor only while keeping tests green.
- Re-run relevant tests after each refactor step.
- Summarize what changed and confirm test status.

## Execution Rules

- Never write implementation code before a failing test.
- Keep changes minimal and scoped to current behavior.
- Keep exactly one behavior per cycle.
- Prefer targeted tests first, then broader suite when appropriate.
- If failures are ambiguous or unrelated, stabilize test setup before continuing.

## Test Writing Standard

Use AAA structure where helpful.

- Pure/simple logic: Act + Assert is acceptable.
- Complex setup or stateful behavior: use Arrange + Act + Assert.
- Hardcode expected values in assertions.
- Assert only relevant outcomes; avoid over-specification.
