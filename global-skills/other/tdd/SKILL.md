---
name: tdd
description: Strict RED-GREEN-REFACTOR TDD workflow with user checkpoints between phases. Use when implementing or changing code should be driven by a failing test first, with minimal implementation scope and explicit user approval before moving past each phase.
---

You are in **TDD (Test-Driven Development)** mode. Follow strict RED-GREEN-REFACTOR discipline with user checkpoints.

$ARGUMENTS

## Your Workflow

### Phase 1: RED - Write a Failing Test

1. **Understand the requirement** - Ask clarifying questions if needed
2. **Write one test** for one behavior, describing the expected behavior
3. **Run the targeted test** - It MUST fail, and for the expected reason (if it passes, STOP and investigate why before writing any implementation)
4. **Report the failure:**
   ```
   ## RED Phase Complete
   - Test file: [path]
   - Test name: [description]
   - Failure: [key error message]
   - Planned implementation: [minimal change]

   Continue? (y/N)
   ```
5. **STOP and wait for user approval**

### Phase 2: GREEN - Make It Pass

1. Write the **minimal code** to make the test pass
2. **Run the same targeted test** - It should now pass
3. **Report success, including the next test intention:**
   ```
   ## GREEN Phase Complete
   - Implementation: [path(s)]
   - Test status: PASSING

   **Next test intention**: [brief description of what the next RED test will verify]

   I'll add a test that:
   - [Setup step 1]
   - [Setup step 2]
   - [Assertion to verify]

   Continue? (y/N)
   ```
4. **STOP and wait for user approval**

### Phase 3: REFACTOR (optional)

1. Clean up code while keeping tests green
2. Run tests after each change
3. Report what you refactored and confirm test status

## Rules

- NEVER write implementation code before a failing test
- NEVER continue past a checkpoint without user approval (only "y" or "yes" proceeds)
- Keep implementations MINIMAL - just enough to pass
- One behavior per test cycle
- If a test passes unexpectedly, STOP and investigate
- Prefer targeted tests first, then the broader suite when appropriate
- If failures are ambiguous or unrelated, stabilize the test setup before continuing

## Test Structure (Arrange-Act-Assert)

Use the AAA pattern, but adapt based on what you're testing. Examples below are written in TypeScript but the methodology applies to all languages — adapt syntax, comment style, and test framework accordingly.

### For pure functions (simple)
Use **Act + Assert** only - no need for Arrange when inputs are trivial:
```typescript
// Act
const result = Calculator.add(2, 3)

// Assert
expect(result).toBe(5)
```

### For pure functions (complex setup)
Use **Arrange + Act + Assert** when:
- Multiple arguments (3+) that benefit from naming
- Arguments depend on each other
- Setup logic needs explanation

```typescript
// Arrange
const parent = Category.make("Electronics")
const child = Category.makeChild(parent, "Phones")
const product = Product.make({ name: "iPhone", category: child })

// Act
const breadcrumb = Product.getBreadcrumb(product)

// Assert
expect(breadcrumb).toBe("Electronics > Phones > iPhone")
```

### For side effects / stateful code
Always use **Arrange + Act + Assert**:
```typescript
// Arrange
const video = VideoPlayer.make()
video.loadSource("test.mp4")
video.seekTo(30.0)

// Act
video.play()

// Assert
expect(video.isPlaying).toBe(true)
```

## Assertion Best Practices

- **Hardcode expected values** in assertions (don't duplicate logic)
- **Assert only relevant values** - avoid over-specification
- For wrong branches in pattern matching, use an exception/failure
