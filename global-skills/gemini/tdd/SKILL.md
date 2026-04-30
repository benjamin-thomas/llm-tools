---
name: tdd
description: Strict RED-GREEN-REFACTOR TDD workflow with user checkpoints between phases
---

# TDD (Test-Driven Development) Skill

You are in **TDD (Test-Driven Development)** mode. Follow strict RED-GREEN-REFACTOR discipline with user checkpoints.

## Your Workflow

### Phase 1: RED - Write a Failing Test

1. **Understand the requirement** - Ask clarifying questions if needed.
2. **Write a test** that describes the expected behavior.
3. **Run the test** - It MUST fail (if it passes, investigate why).
4. **Report the failure:**
   ```
   ## RED Phase Complete
   - Test file: [path]
   - Test name: [description]
   - Failure: [error message]
   - Planned implementation: [brief description]
   ```
5. **STOP and wait for user approval** (Ask: "Continue to GREEN? (y/N)")

### Phase 2: GREEN - Make It Pass

1. Write the **minimal code** to make the test pass.
2. **Run the test** - It should now pass.
3. **Report success:**
   ```
   ## GREEN Phase Complete
   - Implementation: [path]
   - Test status: PASSING
   ```
4. **STOP and wait for user approval** (Ask: "Continue to REFACTOR? (y/N)")

### Phase 3: REFACTOR (optional)

1. Clean up code while keeping tests green.
2. Run tests after each change.
3. Report what you refactored.
4. **State the next test intention** before completing the cycle:
   ```
   **Next test intention**: [Brief description of what the next RED test will verify]
   ```

## Rules

- NEVER write implementation code before a failing test.
- NEVER continue past a checkpoint without user approval.
- Keep implementations MINIMAL - just enough to pass.
- One behavior per test cycle.
- Use the AAA (Arrange-Act-Assert) pattern for tests.
- Hardcode expected values in assertions; avoid duplicating logic.

## Verification

Always use `run_shell_command` to execute tests and verify the current state (RED or GREEN).
