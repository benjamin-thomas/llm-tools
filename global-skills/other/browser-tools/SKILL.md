---
name: browser-tools
description: Inspect or interact with a running local web app through the Chrome DevTools Protocol scripts a repository provides. Use when verifying UI changes, reproducing browser bugs, checking rendered page state, taking screenshots, or any task that benefits from looking at the app in a real browser. Triggers include "check the UI", "verify the page", "test in the browser", "screenshot the …", "what does /path look like", or references to a local dev app URL.
---

# Browser Tools

Drive a real Chrome over CDP using the scripts the repository ships — most often
under `manage/dev/browser/`. Chrome listens on `:9222`.

## Unblock yourself — do not ask the user for setup

Every failure below is routine, has a known fix, and you can apply it without
permission. Work down this table before reporting anything as broken. Save the
user's attention for what only they can answer: which element did I mean, is
this the layout you wanted, is this bug reproduced correctly.

| Symptom | Fix — just do it |
| --- | --- |
| `ERR_MODULE_NOT_FOUND`, `Cannot find package 'puppeteer-core'` | Deps are not installed **in this checkout**. `cd <script-dir> && npm install`, then retry. |
| `✗ Could not connect to browser`, `ECONNREFUSED` on `:9222` | Chrome isn't up. Run the repo's `start.js`. |
| `✗ Could not find Chrome` | See [Sandboxes](#sandboxes) — the binary is usually at `/opt/google/chrome/google-chrome`. |
| Navigation fails / `ECONNREFUSED` on the **app** port | Almost always the wrong port, not a down server. See [Find the app's port](#find-the-apps-port). |
| Redirected to a login page | Look for a seed or login helper in the repo before asking a human to log in. |
| A selector matches 0 or many elements | Don't guess a new one from rendered text — use `pick.js` and ask the user to click it. |

A missing dependency or a stopped Chrome is a first step, not an error to
escalate. Escalating these is the single most common way this skill wastes the
user's time.

## Discover the project setup

Before the first browser call in an unfamiliar repo:

1. Read the project's agent instructions (`AGENTS.md`, `CLAUDE.md`) and any
   `.../browser/README.md`.
2. Locate the scripts:
   ```bash
   find . -path '*/browser/start.js' -not -path '*/node_modules/*'
   ```
3. Take the app URL from the repo or the user. Never assume a host or port.

If the repo ships no browser scripts at all, fall back to Playwright or another
available browser tool — and say which one you picked.

## Bootstrap the scripts

The scripts are usually a tiny standalone npm package (`puppeteer-core`) that is
**not** covered by the project's main install step, and `node_modules/` is
gitignored. So a fresh clone, and **every new git worktree**, starts without it.

Check and install in one step, before the first script call:

```bash
cd manage/dev/browser && [ -d node_modules ] || npm install
```

## Find the app's port

Dev-server ports are frequently assigned per checkout rather than fixed in code,
so read the port instead of assuming `3000`/`8000`. Look for a generated env
file the dev tooling writes:

```bash
grep -E '^[A-Z_]*PORT=' tmp/dev.env 2>/dev/null   # or .env.local, .dev.env, …
```

Assuming the wrong port is the number-one reason a navigation "fails to
connect". Check the port before concluding the server is down.

## Assume the dev server is already running

Developers typically keep it running all day. Do not start, restart, or health-
check it on your own initiative. If a request genuinely fails after you've
confirmed the port, report that — don't try to bring the server up.

## Git worktrees

When the project uses worktrees, three things are per-worktree and one is
global. Getting this backwards causes confusing results:

- **Per-worktree**: `node_modules/` for the browser scripts, the Chrome profile
  (usually `./tmp/browser-profile`), and often the dev-server port.
- **Global**: port `:9222`. Only one Chrome can hold it. `start.js` reuses a
  running instance, so a Chrome launched from worktree A serves your commands
  from worktree B — with A's profile and A's cookies.

Practical consequences:

- Always navigate to an explicit, full URL carrying **your** worktree's port.
  Never trust the tab's current URL to belong to your checkout.
- If a login looks wrong or the page shows another branch's data, you're most
  likely driving another worktree's Chrome. Confirm with
  `eval.js 'location.href'` before you debug anything else.

## Sandboxes

These scripts commonly run inside a bwrap sandbox (`sandbox-agent`), which
already provides what Chrome needs: the host Chrome install directory mounted
read-only, a `/dev/shm` tmpfs, and `DISPLAY`. So:

- `google-chrome` on `PATH` and `/opt/google/chrome/google-chrome` both normally
  resolve inside the sandbox. If neither exists, the host has no Chrome — that
  is a real escalation, not something to work around.
- Only the project directory is writable. This is why the Chrome profile belongs
  under `./tmp/`, not `~/.cache/`. A profile path pointing into `$HOME` will fail
  in the sandbox — treat that as a bug in the repo's scripts and say so.

## Scripts

Common names — not every repo has every one; read the local `README.md`.

| Script | Purpose |
| --- | --- |
| `start.js` | Launch Chrome with remote debugging on `:9222`. Reuses an existing instance. |
| `nav.js <url> [--new]` | Navigate the active tab (or open a new one). |
| `eval.js '<expr>'` | Run JS in the active tab and print the result. Your default tool for both inspection and scripted interaction. |
| `screenshot.js` | Capture the viewport to a file. Use sparingly — DOM inspection is cheaper. |
| `pick.js "<prompt>"` | **Ask the user** to click element(s). Prints selector info. Use when you don't know the right selector. |
| `click.js '<selector>' [--wait-for '<selector>']` | Click a unique selector. Fails loudly on missing/multiple/disabled/hidden. |

## Workflow

### 1. Inspect via the DOM, not screenshots

Prefer `eval.js` returning a small JSON-shaped object. Screenshots cost context;
structured data is cheap.

```bash
manage/dev/browser/eval.js '({
  url: location.href,
  title: document.title,
  headings: Array.from(document.querySelectorAll("h1,h2")).map(h => h.textContent.trim()),
  buttons: Array.from(document.querySelectorAll("button")).map(b => ({ text: b.textContent.trim(), testid: b.dataset.testid })),
})'
```

Screenshot only when the question is genuinely visual: alignment, spacing,
layout polish, what a panel looks like.

### 2. When you need a selector you don't know — ask

If you're about to guess a selector from rendered text, **stop and use
`pick.js`**:

```bash
manage/dev/browser/pick.js "Click the button that opens a new project"
```

The user Ctrl/Cmd-clicks one or more elements in their open browser, hits Enter,
and you get back tag / id / class / `data-testid` / text / parents. Then click it
with `click.js`.

If the picker returns a fragile selector (a deep `div > div > div` path with no
id or `data-testid`), that's a signal — propose adding a `data-testid` in the
source rather than hard-coding the fragile path.

### 3. Clicking

Always prefer a unique, stable selector (`[data-testid="…"]`, `#id`, a unique
`aria-label`). `click.js` errors out when the selector matches zero or many
elements, which is the safe default.

```bash
manage/dev/browser/click.js '[data-testid="new-project"]'
manage/dev/browser/click.js '[data-testid="new-project"]' --wait-for '[data-testid="project-form"]'
```

Use `--wait-for` for clicks that open something — pass the selector of what you
expect to appear.

### 4. Batch JS for multi-step interactions

When `click.js` + `eval.js` would take several round-trips, do it in one
`eval.js` IIFE:

```bash
manage/dev/browser/eval.js '(async () => {
  document.querySelector("#name").value = "test";
  document.querySelector("form").requestSubmit();
  await new Promise(r => setTimeout(r, 300));
  return { url: location.href, errors: Array.from(document.querySelectorAll(".error")).map(e => e.textContent) };
})()'
```

### 5. Report what you actually observed

Quote the DOM state or the URL you read back. "The button is now disabled" is
worth little without the `eval.js` output that shows it.
