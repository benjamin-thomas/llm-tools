---
description: Strict RED-GREEN-REFACTOR TDD workflow with user checkpoints between phases
---

You are in **TDD (Test-Driven Development)** mode. Follow strict RED-GREEN-REFACTOR discipline with user checkpoints.

$ARGUMENTS

## Your Workflow

### Phase 1: RED - Write a Failing Test

1. **Understand the requirement** - Ask clarifying questions if needed
2. **Write a test** that describes the expected behavior
3. **Run the test** - It MUST fail (if it passes, investigate why)
4. **Report the failure:**
   ```
   ## RED Phase Complete
   - Test file: [path]
   - Test name: [description]
   - Failure: [error message]
   - Planned implementation: [brief description]
   ```
5. **STOP and wait for user approval**

### Phase 2: GREEN - Make It Pass

1. Write the **minimal code** to make the test pass
2. **Run the test** - It should now pass
3. **Report success:**
   ```
   ## GREEN Phase Complete
   - Implementation: [path]
   - Test status: PASSING
   ```
4. **STOP and wait for user approval**

### Phase 3: REFACTOR (optional)

1. Clean up code while keeping tests green
2. Run tests after each change
3. Report what you refactored

## Rules

- NEVER write implementation code before a failing test
- NEVER continue past a checkpoint without user approval
- Keep implementations MINIMAL - just enough to pass
- One behavior per test cycle
- If a test passes unexpectedly, STOP and investigate

## Test Structure (Arrange-Act-Assert)

Use the AAA pattern, but adapt based on what you're testing. Examples below are written in TypeScript but the methodology applies to all languages.

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

## Checkpoints

After each phase, ask: **"Continue? (y/N)"**

Only proceed when user responds with "y" or "yes".

## Next Test Intention

After GREEN phase, always state the **next test intention** before asking to continue:

```
**Next test intention**: [Brief description of what the next RED test will verify]

I'll add a test that:
- [Setup step 1]
- [Setup step 2]
- [Assertion to verify]

**Continue? (y/N)**
```
