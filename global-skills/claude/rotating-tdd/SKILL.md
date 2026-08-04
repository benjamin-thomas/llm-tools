---
name: rotating-tdd
description: Use when the user wants several CLI agents to implement something together under strict TDD, taking turns — one writes the failing test, another makes it pass, a third reviews, and the roles rotate each cycle. Builds on the worker-orchestrator skill for transport. The human launches the agent sessions first and reviews every RED and GREEN.
---

# Rotating TDD

Several agents implement one piece of work as a relay. Within each cycle the
roles are split and then **rotated**, so the agent that writes the
implementation is always satisfying *somebody else's* test.

That constraint is the whole point. A single agent writing both the test and
the implementation shapes the test around the code it already intends to write.
Splitting the roles removes that freedom; rotating them stops any one agent's
blind spots from dominating.

You are the **coordinator**. You dispatch, read panes, judge, and report to the
human. You do not write the production code yourself.

## Prerequisites — the human sets these up

**You cannot start until the human has launched the agents.** This skill does
not spawn them: the human picks each agent's CLI, model, and reasoning effort by
launching it themselves, one per tmux window.

Check with `tmux-orchestrator list`. You need **at least two** agent windows
besides your own; **three** is the intended shape (RED / GREEN / REVIEW). If
there are fewer, stop and tell the human exactly what to do:

```
This skill needs N agent windows besides mine, and I can see M.
Open a new tmux window per agent and launch its CLI there — pick the model
and reasoning effort you want for each — then tell me to continue.
```

Read `worker-orchestrator`'s SKILL.md: it is the transport layer for everything
below (`send`, `broadcast`, `read`, `wait`, `rename`).

First actions:
1. `tmux-orchestrator list` to get the indices.
2. `rename` each window after its model or role, so your later reports and the
   human's status bar read as names rather than `node`/`kimi-co`.
3. Confirm with the human which window is which model — the pane banner scrolls
   away, and you will be attributing design opinions to these names all session.

## Phase A — Explore

Send **every** agent the same questionnaire, independently. No agent sees
another's answer. This is the diversity you are paying for; do not let it
collapse by dispatching sequentially and leaking context.

The prompt must:
- Point at the work item (ticket, issue, or the human's description).
- Be **read-only**: no edits, no builds. Concurrent builds in a shared checkout
  collide, and a build lock failure looks like a design failure.
- Ask **specific, checkable** questions, not "what do you think". Force real
  snippets — exact grammar rules, exact function signatures, exact test names.
  Vague questions produce three agreeable summaries and tell you nothing.
- Include a question whose answer is only discoverable by actually reading the
  code, so you can tell exploration from paraphrase.
- End with: write the answer to `<scratch>/<name>.md`, print `DONE`, and stop.

Have them write to **files**, not just the pane. Panes truncate and wrap; files
diff cleanly and survive.

**Large prompts:** write the prompt to a file and dispatch a *pointer* to it —
`tmux-orchestrator send <idx> "Read <path> and follow it exactly"` — rather than
pasting the text in. Pasting depends on the target TUI's paste handling; a
one-line pointer does not, and the agent can re-read the file if it loses
context.

**Check the exit code on every dispatch.** `0` means the pane visibly reacted;
`1` means the prompt is sitting unsent in that composer; `3` means the pane was
still repainting so the submit could not be confirmed either way. On `1` or `3`,
`read` the window before continuing. Never assume a dispatch landed — an unsent
prompt looks exactly like an agent thinking hard.

## Phase B — Synthesize, and the human ratifies

Read all three designs yourself. Also read enough of the real code to *judge*
them — if you only relay, you are a message bus and the human gets three
opinions with no verdict.

Report to the human:

```
## Where they agree
<the consensus design, stated once, concretely>

## Where they diverge
<issue> — A says X, B says Y, C says Z.
  My read: <which is right, and why> / <genuinely the human's call>

## What only one agent caught
<the findings that justify having run three — often the highest-value part>

## Corrections to the work item itself
<where the ticket or issue is simply wrong, with evidence>

## Proposed cycle plan
Cycle 1: <one behaviour> ...
```

Then **stop** and get explicit approval. The human must understand and own the
design before any code moves. Disagreements between agents are resolved here,
on paper — never by letting two agents fight through the working tree.

If the agents disagree on something material, say so plainly rather than
papering over it with a blended design that none of them actually proposed.

## Phase C — The relay

Roles rotate every cycle:

| Cycle | RED | GREEN | REVIEW |
|-------|-----|-------|--------|
| 1 | A | B | C |
| 2 | B | C | A |
| 3 | C | A | B |

Each cycle:

1. **RED** — dispatch to the RED agent: write one failing test for the next
   behaviour, run it, report the failure, change no production code. Then
   `read` the pane and verify the test fails **for the stated reason**. A test
   that fails to compile, or fails because of a typo, is not a RED.
2. **Human checkpoint.** Show the test and the failure output. The human
   reviews, redirects, or stages it. Wait.
3. **GREEN** — dispatch to the GREEN agent: make that test pass with the
   minimal change, run the full suite, touch the test only if it is wrong (and
   say so loudly if it is). Then `read` and verify.
4. **REVIEW** — dispatch to the REVIEW agent, read-only: is the test honest, is
   the implementation minimal-but-correct, what breaks that nobody tested?
5. **Human checkpoint.** Show the diff, the suite result, and the reviewer's
   critique. The human reviews, redirects, or stages. Wait.

Then rotate and repeat.

### Rules that keep the relay from tangling

- **One writer at a time.** Only the agent whose turn it is may edit files.
  Say this in every dispatch. The other two are idle or read-only.
- **One build at a time.** Only the acting agent runs the test suite. A shared
  checkout has one build lock and one output directory.
- **Never `broadcast` a "make changes" instruction.** Broadcast is for
  read-only work (explore, review) only.
- **Read before you conclude.** A quiet pane is not a passing test. Capture the
  pane and check what it actually says.
- **Agents do not talk to each other.** Route every hand-off through yourself,
  including the reviewer's critique.
- **Carry the context forward.** A fresh dispatch does not know what the
  previous agent decided. Restate the ratified design, the current cycle, and
  what the previous agent just did.

## When to stop and escalate

Stop the relay and tell the human immediately when:

- Two agents disagree on whether the code is even correct, and you cannot
  adjudicate from the code.
- The GREEN agent rewrites the test rather than the implementation to get green.
- The reviewer finds a real defect the implementer declines to fix.
- The ratified design turns out to be wrong once it meets real code — better to
  re-ratify than to let three agents improvise around a broken plan.
- An agent runs out of context, or starts repeating itself.

Escalating early is correct. The human asked for a rock-solid result, not an
unattended one.

## Retrospective

At the end, report honestly on the process itself, not just the code:

- Which agent caught what the others missed.
- Where the rotation genuinely helped, and where it was pure overhead.
- Where they agreed but were all wrong.
- Anything about the transport (dispatch, submit, pane reading) that misfired.

Say plainly if the multi-agent approach was not worth it for this work item.
That is a useful finding, not a failure.
