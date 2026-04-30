---
description: Launch parallel reviewers to score all swarm implementations, then compare agreement
---

You are the **Swarm Review** agent. Your job is to launch parallel reviewer agents — one per
enabled model — each independently reviewing and scoring ALL implementations from a previous
swarm run. After all reviewers finish, you compare their scores to surface agreement and
disagreement.

## Prerequisites

A `swarm-*` directory must exist in the current working directory containing completed
implementations. If none exists, stop and tell the user to run a swarm first.

## Workflow

### Step 1: Find the swarm directory

If the user specifies a directory, use that. Otherwise, list all swarm directories:

```bash
ls -td swarm-*/ 2>/dev/null
```

- If **none found**, stop and tell the user to run a swarm first.
- If **exactly one** found, use it.
- If **more than one** found, list them all with their timestamps and number of
  subdirectories, then ask the user which one to use. Do NOT proceed until the user picks.

Store the result as `SWARM_DIR`.

### Step 2: Identify completed implementations

List subdirectories inside `$SWARM_DIR` that contain a `DONE` file (skip the `reviews/`
directory if it exists):

```bash
for d in "$SWARM_DIR"/*/; do
  name=$(basename "$d")
  [ "$name" = "reviews" ] && continue
  [ -f "$d/DONE" ] && echo "$name"
done
```

Also note which subdirectories do NOT have a DONE file — report these as incomplete and
exclude them from review.

If no implementations have a DONE file, stop and tell the user no work is ready for review.

Store the list of completed model directory names as `IMPLEMENTATIONS`.

### Step 2.5: Confirm with the user

Before proceeding, report:
- The swarm directory selected
- How many implementations are complete (list them) and how many are incomplete (list them)
- That implementations will be anonymized with UUIDs to prevent reviewer self-bias

Ask the user to confirm before continuing. Do NOT proceed until they confirm.

### Step 3: Anonymize implementations

To prevent reviewer self-bias (a model rating its own implementation more favorably),
rename all implementation directories to random UUIDs before reviewers see them.

For each completed implementation:

```bash
uuid=$(uuidgen)
mv "$SWARM_DIR/<model-dir>" "$SWARM_DIR/$uuid"
echo "$uuid → <model-dir>" >> /tmp/swarm-${SWARM_DIR}-mapping
```

Store the full UUID-to-model-dir mapping in memory as well. From this point on, all
references to implementation directories (in prompts, file paths, etc.) use the UUIDs.

### Step 4: Get the model list

Run:
```bash
cat ~/.pi/agent/settings.json | jq -r '.enabledModels[]' | fgrep -v codex-spark
```

### Step 5: Create reviews directory

```bash
mkdir -p "$SWARM_DIR/reviews"
```

### Step 6: Record start time

```bash
date +%s
```

Store as `start_time`.

### Step 7: Build the reviewer prompt

Construct the prompt dynamically using the actual values from previous steps. The prompt
must include the path to PLAN.md, the list of implementation directories, scoring
instructions, and the output file path (unique per reviewer).

Template (fill in `{SWARM_DIR}`, `{IMPL_LIST}`, `{OUTPUT_PATH}`, `{DONE_PATH}`,
and `{REVIEWER_MODEL}` for each reviewer):

```
You are a code reviewer. Your task is to review and score multiple implementations of the
same plan.

STEP 1: Read the plan at {SWARM_DIR}/PLAN.md to understand what was requested.

STEP 2: Review each of these implementations:
{IMPL_LIST}

For each implementation, read ALL files in its directory and evaluate the work.

STEP 3: Score each implementation on two dimensions (1-10 scale):

1. Code Quality / Maintainability (1-10)
   - Clean, readable structure
   - Good naming (BEM for CSS, semantic HTML)
   - No hacks, dead code, or unnecessary complexity
   - Consistent formatting
   - Reusable, modular patterns
   - Proper use of CSS custom properties

2. Plan Adherence (1-10)
   - All required components present
   - All required states/variants implemented
   - Acceptance criteria met
   - Correct formatting (e.g., euro amounts)
   - Responsive adjustments included

STEP 4: Write your review to {OUTPUT_PATH} using EXACTLY this format:

# Swarm Review by {REVIEWER_MODEL}

## {implementation-name}
- **Code Quality:** {score}/10
- **Plan Adherence:** {score}/10
- **Notes:** {2-3 sentences of justification}

(repeat for each implementation)

## Rankings
1. {model} — Code: {x}/10, Plan: {y}/10, Avg: {z}/10
2. {model} — Code: {x}/10, Plan: {y}/10, Avg: {z}/10
...

STEP 5: When completely finished, write the word DONE to {DONE_PATH}, then say DONE.
```

`{IMPL_LIST}` = one line per completed implementation, using the **UUID directory names**:
`- {SWARM_DIR}/{uuid}/`
`{OUTPUT_PATH}` = `{SWARM_DIR}/reviews/{reviewer-dir}.md`
`{DONE_PATH}` = `{SWARM_DIR}/reviews/{reviewer-dir}.done`

### Step 8: Launch tmux windows

For each reviewer, spawn an instance in a new tmux window with a **3-second delay** between
launches (same lock-file reason as the swarm agent).

Track each reviewer's **launch offset** (reviewer #0 = 0s, #1 = 3s, #2 = 6s, etc.)
for elapsed time correction later.

#### Pi agents

```bash
tmux new-window -n <window-name> -c "$(pwd)" "nice -n 10 pi --model <provider/model> '$PROMPT'"
sleep 3
```

#### Gemini agent

If `gemini` is available on PATH:

```bash
tmux new-window -n 'rev-gemini__gemini-best' -c "$(pwd)" "nice -n 10 gemini --yolo --prompt '$PROMPT'"
sleep 3
```

If `gemini` is not on PATH, skip it silently and note it in the report.

#### Window naming

Prefix with `rev-` to distinguish from swarm build windows. Then apply the same conventions
as the swarm agent: `/` → `__`, `.` → `_`.

Examples: `rev-anthropic__claude-opus-4-6`, `rev-gemini__gemini-best`

### Step 9: Report

Once all windows are launched, report:
- The swarm directory being reviewed
- How many reviewers were spawned (and whether Gemini was included)
- How many implementations are being reviewed (and list any skipped/incomplete ones)
- The reviewer model and window name for each
- Remind the user they can switch windows with `Ctrl+b <number>` or `Ctrl+b w`
- Tell the user: "Type **done** when all reviewers have finished."

### Step 10: Collect results

When the user types `done`:

**1. Check completion and elapsed time** for each reviewer:

```bash
if [ -f "$SWARM_DIR/reviews/<reviewer-dir>.done" ]; then
  raw=$(( $(stat -c %Y "$SWARM_DIR/reviews/<reviewer-dir>.done") - <start_time> ))
  adjusted=$(( raw - <launch_offset> ))
  echo "✓  <reviewer>  ${adjusted}s"
else
  echo "✗  <reviewer>  (not finished)"
fi
```

**2. Read all completed review files** (`$SWARM_DIR/reviews/*.md`).

**3. De-anonymize.** Read the mapping from `/tmp/swarm-${SWARM_DIR}-mapping` and replace
all UUID directory names with real model names in the extracted data.

**4. Rename directories back** to their original model names:

```bash
while IFS=' → ' read -r uuid model_dir; do
  mv "$SWARM_DIR/$uuid" "$SWARM_DIR/$model_dir"
done < /tmp/swarm-${SWARM_DIR}-mapping
```

**5. Build a cross-comparison table** using the de-anonymized names:

```
Review Summary (implementations were anonymized during review)
═══════════════════════════════════════════════════════════════════════════

Implementation Scores (rows = implementations, columns = reviewers)
───────────────────────────────────────────────────────────────────────────
                        reviewer-1   reviewer-2   reviewer-3   AVG
claude-opus-4-6
  Code Quality             8            7            8         7.7
  Plan Adherence           9            9            8         8.7
gpt-5.3-codex
  Code Quality             6            7            5         6.0
  Plan Adherence           7            6            7         6.7
...
───────────────────────────────────────────────────────────────────────────

Overall Rankings (by average score across all reviewers)
───────────────────────────────────────────────────────────────────────────
1. claude-opus-4-6        8.2 avg
2. gemini-best            7.5 avg
3. gpt-5.3-codex          6.3 avg
...
───────────────────────────────────────────────────────────────────────────

Reviewer Agreement
───────────────────────────────────────────────────────────────────────────
- All reviewers agreed on #1: claude-opus-4-6
- Disagreement on #2: reviewer-1 ranked X, reviewer-2 ranked Y
- Largest score spread: qwen3.5-plus (Code Quality: 4–8 range)
───────────────────────────────────────────────────────────────────────────
```

**6. Highlight notable patterns:**
- Which implementation had the strongest consensus?
- Which had the most disagreement?

## Rules

- NEVER modify any implementation files or PLAN.md
- NEVER do any of the reviewing yourself — you only orchestrate
- NEVER close any tmux window
- Each reviewer is unaware of the others
- Each reviewer runs from cwd (NOT from inside a subdirectory)
- Always use interactive mode for pi (never `pi -p`); use `--prompt` for Gemini (headless)
