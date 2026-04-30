---
name: tdd
description: Strict RED-GREEN-REFACTOR TDD workflow with user checkpoints between phases, AAA test structure, and next-intention reporting at every step
---

# TDD (Test-Driven Development)

You are in **TDD (Test-Driven Development)** mode. Follow strict RED-GREEN-REFACTOR discipline with user checkpoints between each phase.

## Why This Workflow

The purpose of this workflow is to validate code in small, reviewable chunks. After each GREEN phase, the user can stage the passing code and eventually commit a clean logical unit. Do not write multiple behaviors at once — one step at a time.

## Your Workflow

### Phase 1: RED — Write a Failing Test

1. **Understand the requirement** — Ask clarifying questions if needed.
2. **Write one test** that describes a single behavior.
3. **Run the test** — it MUST fail. If it passes unexpectedly, **STOP** and investigate before proceeding.
4. **Report the failure** using this template:

```
## RED Phase Complete
- Test file: [path]
- Test name: [description]
- Failure: [key error message — show the actual failure reason]
- Planned implementation: [what you plan to write, concretely, so the user can validate the direction]

**Next intention**: Make this test pass with the minimal implementation described above.

Continue? (y/N)
```

5. **STOP and wait for user approval.**

### Phase 2: GREEN — Make It Pass

1. Write the **minimal implementation** — just enough to make the test pass.
2. **Run the test** — it should now pass.
3. **Report success** using this template:

```
## GREEN Phase Complete
- Implementation: [path(s)]
- Test status: PASSING

**Next intention**: [what comes next — either the next failing test to write, or a refactoring opportunity, or that the feature is complete. Explain *why* briefly.]

Continue? (y/N)
```

4. **STOP and wait for user approval.**

### Phase 3: REFACTOR — Clean Up (Optional)

1. Refactor code while keeping all tests green.
2. **Run tests after each refactor step.**
3. Report what changed:

```
## REFACTOR Phase Complete
- Changed: [what was refactored]
- Reason: [why — readability, dedup, performance, etc.]
- Test status: ALL PASSING

**Next intention**: [next step — next RED test, or done with this unit]

Continue? (y/N)
```

4. **STOP and wait for user approval.**

## Rules

- **NEVER** write implementation code before a failing test.
- **NEVER** continue past a checkpoint without user approval.
- **Keep implementations minimal** — just enough to pass.
- **One behavior per test cycle.**
- If anything unexpected happens (test passes when it shouldn't, strange error, unrelated failure), **STOP immediately** and report the issue. Do not decide on your own — ask the user.

## Test Structure: Arrange-Act-Assert (AAA)

Always structure tests using the AAA pattern for readability and clean reasoning.

### Pure functions — Act + Assert

When inputs are trivial, Arrange is not needed:

```
// Act
result = add(2, 3)

// Assert
assert_equal(5, result)
```

### Complex setup or stateful code — Arrange + Act + Assert

When there are multiple inputs, dependencies, or prior state:

```
// Arrange
parent = Category.make("Electronics")
child  = Category.make(parent, "Phones")
item   = Product.make(name: "iPhone", category: child)

// Act
breadcrumb = get_breadcrumb(item)

// Assert
assert_equal("Electronics > Phones > iPhone", breadcrumb)
```

### Assertion Guidelines

- **Hardcode expected values** in assertions. Do not duplicate the logic under test.
- **Assert only what is relevant** to the behavior — avoid over-specification.
- For branches that should fail/raise, assert the exception or failure condition.

## Checkpoint Behavior

- Every phase ends with a checkpoint. Wait for the user to respond before continuing.
- The user may say "go ahead" for multiple cycles — follow their lead.
- If the user stages or commits between phases, acknowledge it and continue.
