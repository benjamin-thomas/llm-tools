#!/usr/bin/env python3
"""tmux-orchestrator — a thin, stateless coordinator substrate for driving
sibling tmux windows that each run a CLI agent (claude, codex, kimi, ...).

There is no daemon, no database, and no wire protocol. You launch the agents
yourself, one per tmux window, picking each one's model and reasoning effort.
A *coordinator* (any CLI agent, or you at the keyboard) then uses this command
to dispatch prompts and read back whatever the panes show — interpreting the
raw output directly, the way a human watching the windows would.

    tmux-orchestrator list                       # what agents are running
    tmux-orchestrator send backend "fix the bug" # dispatch to one window
    tmux-orchestrator broadcast "re-review HEAD"  # dispatch to every agent
    tmux-orchestrator read backend               # read a window's output
    tmux-orchestrator wait backend               # block until it settles

Targets are a window index (2), an exact window name (backend), or a unique
substring of a name (back). Everything reads live tmux state on each call, so
the human and the coordinator can interleave calls freely.

See global-skills/<cli>/worker-orchestrator/SKILL.md for the coordinator guide.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import secrets
import subprocess
import sys
import time
from typing import Sequence

# Codex's Ratatui composer treats fast bulk input as a paste burst and, for a
# short window afterwards, swallows Enter as a newline rather than a submit (see
# PASTE_ENTER_SUPPRESS_WINDOW in codex-rs/tui/src/bottom_pane/paste_burst.rs).
# For Codex we type the text literally line by line (newlines become C-j so they
# stay inside the input box), then wait past that window before the final Enter.
CODEX_PASTE_BURST_FLUSH_SECONDS = 0.24
DEFAULT_SUBMIT_ENTER_DELAY_SECONDS = 0.15

# Codex is not alone: every composer we drive needs a moment to ingest a
# multi-KB paste (tmux delivers it in chunks) and redraw before it will read
# Enter as "submit" rather than as more of the burst. A fixed delay is a guess
# that loses the race on big prompts, so the submit is a handshake instead:
# wait until the pane stops changing, press Enter, then confirm the pane
# reacted. This is agent-agnostic — it needs no knowledge of the TUI.
PASTE_QUIET_SECONDS = 0.35
PASTE_QUIET_TIMEOUT_SECONDS = 8.0
PASTE_QUIET_INTERVAL_SECONDS = 0.1
SUBMIT_CONFIRM_SECONDS = 0.8
SUBMIT_SAMPLE_LINES = 40

# What a submit attempt is known to have achieved. The distinction that matters
# is UNSENT vs UNCONFIRMED: the first is "we watched a still pane ignore Enter",
# the second is "the pane was moving on its own, so we have no evidence either
# way". Collapsing the second into "sent" is what made a dropped dispatch look
# like a successful one.
SUBMIT_SENT = "sent"
SUBMIT_UNSENT = "unsent"
SUBMIT_UNCONFIRMED = "unconfirmed"
SUBMIT_QUEUED = "queued"

SUBMIT_EXIT_CODES = {
    SUBMIT_SENT: 0,
    SUBMIT_QUEUED: 0,
    SUBMIT_UNSENT: 1,
    SUBMIT_UNCONFIRMED: 3,
}

# Safety bound on the descendant walk. What actually stops the search is the
# shell boundary — see pane_process_names: it descends through shells (the pane's
# own shell, then any wrapper script) and stops at the first real process, which
# is the agent. That is what keeps a command the agent itself ran — say a grep
# whose arguments mention codex — from being mistaken for the agent. This depth
# is only a backstop against a pathological process tree.
PROCESS_TREE_MAX_DEPTH = 4

DEFAULT_READ_LINES = 200
DEFAULT_WAIT_TIMEOUT = 120.0
DEFAULT_WAIT_SETTLE = 4.0
DEFAULT_WAIT_INTERVAL = 1.0
WAIT_SAMPLE_LINES = 60

# Tokens used only to put a friendly TYPE label on `list` output. Window names
# you assign are matched first, then the pane's foreground command.
AGENT_TOKENS: tuple[str, ...] = (
    "codex", "claude", "kimi", "opencode",
    "gpt", "opus", "sonnet", "haiku",
)

# A window whose foreground process is a bare shell is treated as *not* an agent
# (so broadcast skips it by default). Everything else — node, codex, a running
# TUI — is assumed to be an agent. This is robust to the fact that you name your
# windows yourself ("frontend", "review") rather than after the model.
SHELL_COMMANDS: frozenset[str] = frozenset(
    {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
)

WINDOW_FMT = "\t".join(
    (
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{window_active}",
        "#{pane_id}",
        "#{pane_current_command}",
        "#{pane_pid}",
    )
)


class TmuxError(RuntimeError):
    """A `tmux` invocation failed."""


class UsageError(RuntimeError):
    """The caller asked for something impossible (bad target, missing text)."""


@dataclasses.dataclass(frozen=True)
class Window:
    session: str
    index: str
    name: str
    active: bool
    pane_id: str
    command: str
    pid: str


# --- pure helpers (unit-tested) ---------------------------------------------


def parse_window_line(line: str) -> Window | None:
    parts = line.split("\t")
    if len(parts) != 7:
        return None
    return Window(
        session=parts[0],
        index=parts[1],
        name=parts[2],
        active=parts[3] == "1",
        pane_id=parts[4],
        command=parts[5],
        pid=parts[6],
    )


# `process_names` are the argv-token basenames of the processes running in the
# pane (see Tmux.pane_process_names). They matter because tmux's
# `pane_current_command` only ever reports the executable name: a Codex CLI
# installed as an npm package shows up as a bare `node`, so name+command alone
# silently missed it and the Codex-safe submit path never fired.
#
# Names are matched EXACTLY, never as substrings. An agent window is full of
# short-lived subprocesses the agent itself spawned, and one of them running
# `grep -E 'codex|submit'` must not make the window look like Codex.
def looks_like_codex(window: Window, process_names: Sequence[str] = ()) -> bool:
    if "codex" in f"{window.name} {window.command}".lower():
        return True
    return "codex" in {name.lower() for name in process_names}


def agent_label(window: Window, process_names: Sequence[str] = ()) -> str:
    haystack = f"{window.name} {window.command}".lower()
    for token in AGENT_TOKENS:
        if token in haystack:
            return token
    names = {name.lower() for name in process_names}
    for token in AGENT_TOKENS:
        if token in names:
            return token
    return window.command or "?"


def is_agent_window(window: Window) -> bool:
    command = window.command.strip().lower().lstrip("-")
    return bool(command) and command not in SHELL_COMMANDS


def resolve_target(windows: Sequence[Window], target: str) -> Window:
    if target.isdigit():
        for window in windows:
            if window.index == target:
                return window

    exact = [w for w in windows if w.name == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise UsageError(
            f"{target!r} matches several windows: "
            + ", ".join(f"{w.index}:{w.name}" for w in exact)
            + " — use the window index"
        )

    lowered = target.lower()
    subs = [w for w in windows if lowered in w.name.lower()]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        raise UsageError(
            f"{target!r} is ambiguous: "
            + ", ".join(f"{w.index}:{w.name}" for w in subs)
            + " — be more specific or use the window index"
        )

    available = ", ".join(f"{w.index}:{w.name}" for w in windows) or "(none)"
    raise UsageError(f"no window matches {target!r}; available: {available}")


def resolve_text(text_args: Sequence[str], use_stdin: bool) -> str:
    piped = not sys.stdin.isatty()
    if use_stdin or list(text_args) == ["-"] or (not text_args and piped):
        text = sys.stdin.read().rstrip("\n")
        # An empty prompt pastes nothing, so the pane never redraws and the
        # submit handshake burns its whole timeout before reporting that it
        # could not confirm. Say what actually went wrong instead.
        if not text.strip():
            raise UsageError("refusing to send an empty prompt (stdin was empty)")
        return text
    if not text_args:
        raise UsageError("no text given (pass text, use --stdin, or pipe via '-')")
    return " ".join(text_args)


# --- tmux plumbing ----------------------------------------------------------


class Tmux:
    def __init__(self, socket: str | None = None) -> None:
        self.base = ["tmux"] + (["-S", socket] if socket else [])
        self._ps_snapshot: dict[str, tuple[str, str]] | None = None

    def run(self, args: Sequence[str], *, stdin: str | None = None, check: bool = True) -> str:
        proc = subprocess.run(
            [*self.base, *args],
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and proc.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def current_session(self) -> str | None:
        pane = os.environ.get("TMUX_PANE")
        args = ["display-message", "-p", *(["-t", pane] if pane else []), "#{session_name}"]
        out = self.run(args, check=False).strip()
        return out or None

    def self_window_index(self) -> str | None:
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return None
        out = self.run(["display-message", "-p", "-t", pane, "#{window_index}"], check=False).strip()
        return out or None

    def list_windows(self) -> list[Window]:
        session = self.current_session()
        if session:
            out = self.run(["list-windows", "-t", session, "-F", WINDOW_FMT])
        else:
            out = self.run(["list-windows", "-a", "-F", WINDOW_FMT])
        windows = [parse_window_line(line) for line in out.splitlines()]
        return [w for w in windows if w is not None]

    def rename_window(self, window_target: str, name: str) -> None:
        # Turn off automatic-rename for this window first, otherwise tmux would
        # revert the name to the foreground command on its next refresh.
        self.run(["set-window-option", "-t", window_target, "automatic-rename", "off"])
        # `--` for the same reason as the literal send-keys: a name starting
        # with `-` would otherwise be parsed as a flag.
        self.run(["rename-window", "-t", window_target, "--", name])

    def capture(self, pane_id: str, *, lines: int, all_history: bool) -> str:
        args = ["capture-pane", "-p", "-J", "-t", pane_id]
        if all_history:
            args += ["-S", "-"]
        elif lines:
            args += ["-S", f"-{lines}"]
        return self.run(args).rstrip("\n")

    def _ps(self) -> dict[str, tuple[str, str]]:
        """pid -> (ppid, argv), snapshotted once per run."""
        if self._ps_snapshot is None:
            try:
                out = subprocess.run(
                    ["ps", "-e", "-o", "pid=,ppid=,args="],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout
            except OSError:
                out = ""
            snapshot: dict[str, tuple[str, str]] = {}
            for line in out.splitlines():
                parts = line.split(maxsplit=2)
                if len(parts) == 3:
                    snapshot[parts[0]] = (parts[1], parts[2])
            self._ps_snapshot = snapshot
        return self._ps_snapshot

    def pane_process_names(self, window: Window) -> list[str]:
        """Argv-token basenames of the agent running in `window`.

        The pane's own process is the shell, so the agent is a descendant; its
        argv is the only place a name like `codex` survives when the executable
        is a generic runtime such as `node`. Reducing each token to its basename
        turns `node /home/me/.nvm/.../bin/codex --yolo` into `codex`, which the
        exact-match detectors above can use without false positives.
        """
        table = self._ps()
        children: dict[str, list[str]] = {}
        for pid, (ppid, _) in table.items():
            children.setdefault(ppid, []).append(pid)

        names: list[str] = []
        frontier = [(window.pid, 0)]
        seen: set[str] = set()
        while frontier:
            pid, depth = frontier.pop()
            if pid in seen or depth > PROCESS_TREE_MAX_DEPTH:
                continue
            seen.add(pid)
            entry = table.get(pid)
            if entry is None:
                continue
            tokens = [os.path.basename(token) for token in entry[1].split()]
            head = tokens[0].lstrip("-").lower() if tokens else ""
            if head not in SHELL_COMMANDS:
                # The first non-shell process on this branch is the agent.
                # Record it and stop: everything below is a command the agent
                # itself ran, and those must never colour the detection.
                names.extend(tokens)
                continue
            frontier.extend((child, depth + 1) for child in children.get(pid, ()))
        return names

    def submit(self, pane_id: str, text: str, *, codex: bool, enter: bool, enter_delay: float) -> str:
        """Put `text` in the pane's composer and (unless enter=False) submit it.

        Returns one of SUBMIT_SENT / SUBMIT_UNSENT / SUBMIT_UNCONFIRMED /
        SUBMIT_QUEUED. Anything other than SENT/QUEUED means the caller must
        not describe this as a delivered dispatch.
        """
        if codex:
            return self._submit_by_typing(pane_id, text, enter=enter)
        return self._submit_by_paste(pane_id, text, enter=enter, enter_delay=enter_delay)

    def _submit_by_paste(self, pane_id: str, text: str, *, enter: bool, enter_delay: float) -> str:
        baseline = self._sample(pane_id)
        buffer_name = f"tmux-orch-{os.getpid()}-{secrets.token_hex(4)}"
        self.run(["load-buffer", "-b", buffer_name, "-"], stdin=text)
        try:
            # -p wraps the text in the bracketed-paste markers (ESC[200~ …
            # ESC[201~) when the agent's TUI has asked for them. Without it tmux
            # hands over a bare stream with every newline turned into a carriage
            # return, so the composer has to *guess* from timing where the paste
            # starts and ends: it splits one prompt into several paste blocks,
            # and a TUI with no burst heuristic at all submits every line as its
            # own message. With -p the composer is told exactly where the burst
            # ends, which is what makes the Enter that follows unambiguous.
            # tmux only brackets when the application requested the mode, so
            # passing -p is always safe.
            self.run(["paste-buffer", "-p", "-b", buffer_name, "-t", pane_id, "-d"])
        except TmuxError:
            # -d only deletes the buffer on a successful paste.
            self.run(["delete-buffer", "-b", buffer_name], check=False)
            raise
        if not enter:
            return SUBMIT_QUEUED
        settled = self._wait_for_composer_quiet(pane_id, floor=enter_delay, baseline=baseline)
        return self._press_enter_confirmed(pane_id, settled=settled)

    def _submit_by_typing(self, pane_id: str, text: str, *, enter: bool) -> str:
        baseline = self._sample(pane_id)
        lines = text.split("\n")
        last = len(lines) - 1
        for index, line in enumerate(lines):
            # `--` ends option parsing. Without it tmux reads a line beginning
            # with `-` as a flag ("unknown option --") and the send dies partway
            # through, leaving a truncated prompt in the composer that looks
            # like a whole one. Any Markdown bullet list triggers this.
            self.run(["send-keys", "-t", pane_id, "-l", "--", line])
            if index != last:
                self.run(["send-keys", "-t", pane_id, "C-j"])
        if not enter:
            return SUBMIT_QUEUED
        settled = self._wait_for_composer_quiet(
            pane_id, floor=CODEX_PASTE_BURST_FLUSH_SECONDS, baseline=baseline
        )
        return self._press_enter_confirmed(pane_id, settled=settled)

    def _sample(self, pane_id: str) -> str:
        return self.capture(pane_id, lines=SUBMIT_SAMPLE_LINES, all_history=False)

    def _wait_for_composer_quiet(
        self, pane_id: str, *, floor: float, baseline: str | None = None
    ) -> bool:
        """Block until the pane stops redrawing, so Enter lands after the burst.

        Returns True if the pane actually held still. False means it never did
        — it is animating a spinner because the agent is already busy, or it is
        still ingesting. Either way the caller cannot then use "the pane
        changed" as evidence that Enter accomplished anything.

        `baseline` is the pane as it looked *before* the text was sent. A
        composer that has not begun rendering the burst is byte-identical to one
        that has finished it, so without this the quiet check can return during
        the gap before anything arrives. Enter then lands mid-burst, is absorbed
        as part of the paste, and the burst's own later redraw makes the
        swallowed key look like a successful submit.
        """
        time.sleep(max(floor, PASTE_QUIET_INTERVAL_SECONDS))
        deadline = time.monotonic() + PASTE_QUIET_TIMEOUT_SECONDS
        last = self._sample(pane_id)
        while baseline is not None and last == baseline and time.monotonic() < deadline:
            time.sleep(PASTE_QUIET_INTERVAL_SECONDS)
            last = self._sample(pane_id)
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(PASTE_QUIET_INTERVAL_SECONDS)
            current = self._sample(pane_id)
            now = time.monotonic()
            if current != last:
                last, last_change = current, now
            elif now - last_change >= PASTE_QUIET_SECONDS:
                return True
        return False

    def _press_enter_confirmed(self, pane_id: str, *, settled: bool) -> str:
        """Press Enter and report, honestly, what we could observe.

        The only agent-agnostic evidence available is "did the pane change".
        That evidence is worth nothing unless the pane was holding still first:
        an agent that is already thinking repaints a spinner several times a
        second, and that churn satisfies any before/after comparison no matter
        what Enter did — which is how a dispatch that never arrived came back
        reported as sent. So when the pane never settled we press once and say
        we could not tell.

        When it did settle, a submit always redraws: the composer clears, the
        message joins the transcript, a spinner starts. If nothing moved, the
        composer swallowed the key and the prompt is still sitting there. The
        second press is safe precisely because nothing moved: had the first one
        submitted, the composer would now be empty, where Enter is a no-op. If
        instead a dialog had opened, the pane would have changed and we stop.
        """
        if not settled:
            self.run(["send-keys", "-t", pane_id, "Enter"])
            return SUBMIT_UNCONFIRMED
        before = self._sample(pane_id)
        self.run(["send-keys", "-t", pane_id, "Enter"])
        time.sleep(SUBMIT_CONFIRM_SECONDS)
        if self._sample(pane_id) != before:
            return SUBMIT_SENT
        self.run(["send-keys", "-t", pane_id, "Enter"])
        time.sleep(SUBMIT_CONFIRM_SECONDS)
        return SUBMIT_SENT if self._sample(pane_id) != before else SUBMIT_UNSENT

    def wait_until_settled(
        self, pane_id: str, *, timeout: float, settle: float, interval: float
    ) -> bool:
        start = time.monotonic()
        last_text = self.capture(pane_id, lines=WAIT_SAMPLE_LINES, all_history=False)
        last_change = start
        while True:
            time.sleep(interval)
            now = time.monotonic()
            current = self.capture(pane_id, lines=WAIT_SAMPLE_LINES, all_history=False)
            if current != last_text:
                last_text = current
                last_change = now
            if now - last_change >= settle:
                return True
            if now - start >= timeout:
                return False


# --- codex strategy ---------------------------------------------------------


def use_codex_strategy(window: Window, type_flag: str, process_names: Sequence[str] = ()) -> bool:
    if type_flag == "codex":
        return True
    if type_flag == "generic":
        return False
    return looks_like_codex(window, process_names)


# --- commands ---------------------------------------------------------------


def cmd_list(tmux: Tmux, args: argparse.Namespace) -> int:
    windows = tmux.list_windows()
    if not windows:
        print("no tmux windows found", file=sys.stderr)
        return 0
    self_index = tmux.self_window_index()
    sessions = sorted({w.session for w in windows})
    multi = len(sessions) > 1

    if not multi:
        print(f"session: {sessions[0]}")

    header: tuple[str, ...] = (
        ("", "SESSION", "IDX", "WINDOW", "TYPE", "CMD")
        if multi
        else ("", "IDX", "WINDOW", "TYPE", "CMD")
    )
    rows: list[tuple[str, ...]] = [header]
    for window in windows:
        mark = "*" if window.index == self_index else ""
        label = agent_label(window, tmux.pane_process_names(window)) if is_agent_window(window) else "-"
        if multi:
            rows.append((mark, window.session, window.index, window.name, label, window.command))
        else:
            rows.append((mark, window.index, window.name, label, window.command))

    widths = [max(len(row[col]) for row in rows) for col in range(len(header))]
    for row in rows:
        print("  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip())
    if self_index is not None:
        print("\n* = this (coordinator) window", file=sys.stderr)
    return 0


def cmd_rename(tmux: Tmux, args: argparse.Namespace) -> int:
    window = resolve_target(tmux.list_windows(), args.target)
    tmux.rename_window(f"{window.session}:{window.index}", args.name)
    print(f"renamed window {window.index} → {args.name!r}", file=sys.stderr)
    return 0


def cmd_read(tmux: Tmux, args: argparse.Namespace) -> int:
    window = resolve_target(tmux.list_windows(), args.target)
    text = tmux.capture(window.pane_id, lines=args.lines, all_history=args.all)
    print(f"== {window.name} (window {window.index}) ==", file=sys.stderr)
    print(text)
    return 0


def report_submit(window: Window, outcome: str) -> None:
    """Say what was actually observed — never more than that."""
    where = f"{window.name} (window {window.index})"
    if outcome == SUBMIT_UNSENT:
        print(
            f"WARNING: {where} did not react to Enter — "
            "the prompt is probably still sitting in its composer, unsent. "
            "Check with `read`, and submit by hand if needed.",
            file=sys.stderr,
        )
    elif outcome == SUBMIT_UNCONFIRMED:
        print(
            f"NOTE: {where} was still redrawing, so the submit could NOT be confirmed "
            "— most likely it was already busy. The prompt may or may not have "
            "landed; check with `read` before assuming it did.",
            file=sys.stderr,
        )


def cmd_send(tmux: Tmux, args: argparse.Namespace) -> int:
    windows = tmux.list_windows()
    window = resolve_target(windows, args.target)
    text = resolve_text(args.text, args.stdin)
    codex = use_codex_strategy(window, args.type, tmux.pane_process_names(window))
    outcome = tmux.submit(
        window.pane_id,
        text,
        codex=codex,
        enter=not args.no_enter,
        enter_delay=args.enter_delay,
    )
    how = "typed (codex-safe)" if codex else "pasted"
    verb = "queued (no Enter)" if outcome == SUBMIT_QUEUED else "sent"
    print(f"{verb} → {window.name} (window {window.index}), {how}", file=sys.stderr)
    report_submit(window, outcome)
    return SUBMIT_EXIT_CODES[outcome]


def cmd_broadcast(tmux: Tmux, args: argparse.Namespace) -> int:
    windows = tmux.list_windows()
    self_index = tmux.self_window_index()
    text = resolve_text(args.text, args.stdin)

    targets: list[Window] = []
    for window in windows:
        if not args.all_windows and not is_agent_window(window):
            continue
        if window.index == self_index and not args.include_self:
            continue
        if args.only and args.only.lower() not in window.name.lower():
            continue
        targets.append(window)

    if not targets:
        print("broadcast matched no windows", file=sys.stderr)
        return 1

    outcomes: list[tuple[Window, str]] = []
    for window in targets:
        codex = use_codex_strategy(window, args.type, tmux.pane_process_names(window))
        outcome = tmux.submit(
            window.pane_id,
            text,
            codex=codex,
            enter=not args.no_enter,
            enter_delay=args.enter_delay,
        )
        outcomes.append((window, outcome))
        flag = {
            SUBMIT_UNSENT: "  [UNSENT]",
            SUBMIT_UNCONFIRMED: "  [UNCONFIRMED]",
        }.get(outcome, "")
        print(f"  → {window.name} (window {window.index}){flag}", file=sys.stderr)
    print(f"broadcast to {len(targets)} window(s)", file=sys.stderr)

    for window, outcome in outcomes:
        report_submit(window, outcome)
    # Worst outcome wins, and "we watched it ignore Enter" is a harder failure
    # than "we could not tell" — so it is a severity order, not max(exit code).
    seen = {outcome for _window, outcome in outcomes}
    for outcome in (SUBMIT_UNSENT, SUBMIT_UNCONFIRMED):
        if outcome in seen:
            return SUBMIT_EXIT_CODES[outcome]
    return 0


def cmd_wait(tmux: Tmux, args: argparse.Namespace) -> int:
    window = resolve_target(tmux.list_windows(), args.target)
    settled = tmux.wait_until_settled(
        window.pane_id,
        timeout=args.timeout,
        settle=args.settle,
        interval=args.interval,
    )
    if settled:
        print(f"{window.name} (window {window.index}) settled", file=sys.stderr)
        return 0
    print(
        f"{window.name} (window {window.index}) still changing after {args.timeout:g}s",
        file=sys.stderr,
    )
    return 124


# --- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmux-orchestrator",
        description="Dispatch prompts to, and read output from, sibling tmux windows "
        "running CLI agents. Stateless: no daemon, no database, no protocol.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Targets: a window index (2) — you never have to name a window — or a\n"
            "window name / unique substring once you (or the coordinator) rename one.\n\n"
            "Examples:\n"
            "  tmux-orchestrator list\n"
            "  tmux-orchestrator send 2 'run the tests and report failures'\n"
            "  git log -1 -p | tmux-orchestrator send 3 -\n"
            "  tmux-orchestrator broadcast 'a new commit landed — re-review HEAD'\n"
            "  tmux-orchestrator read 2 -n 400\n"
            "  tmux-orchestrator wait 2 --settle 6\n"
            "  tmux-orchestrator rename 2 backend    # optional readable label\n"
            "\n"
            "send/broadcast exit codes:\n"
            "  0  the pane was still, then visibly reacted to Enter\n"
            "  1  the pane was still and ignored Enter — prompt left in the composer\n"
            "  3  the pane was repainting (agent already busy), so the submit could\n"
            "     not be confirmed either way — read the pane before relying on it\n"
            "\n"
            "For anything longer than a few lines, prefer writing the prompt to a file\n"
            "and sending a one-line pointer to it:\n"
            "  tmux-orchestrator send 2 'Read /tmp/mission.md and follow it exactly'\n"
        ),
    )
    parser.add_argument(
        "-S",
        "--socket",
        help="tmux socket path (defaults to the ambient server via $TMUX)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser(
        "list", aliases=["ls", "status"], help="list windows and detected agent types"
    )
    p_list.set_defaults(func=cmd_list)

    p_read = subparsers.add_parser(
        "read", aliases=["cat", "show"], help="print a window's recent output"
    )
    p_read.add_argument("target", help="window index, name, or unique substring")
    p_read.add_argument(
        "-n", "--lines", type=int, default=DEFAULT_READ_LINES,
        help=f"lines of scrollback to include (default: {DEFAULT_READ_LINES})",
    )
    p_read.add_argument("--all", action="store_true", help="capture the entire scrollback")
    p_read.set_defaults(func=cmd_read)

    def add_submit_opts(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--stdin", action="store_true", help="read the message from stdin")
        sub.add_argument(
            "--type", choices=("auto", "codex", "generic"), default="auto",
            help="submit strategy; 'auto' types line-by-line for Codex, pastes otherwise",
        )
        sub.add_argument("--no-enter", action="store_true", help="paste/type without pressing Enter")
        sub.add_argument(
            "--enter-delay", type=float, default=DEFAULT_SUBMIT_ENTER_DELAY_SECONDS,
            help="seconds to wait before Enter on the non-Codex path",
        )

    p_send = subparsers.add_parser(
        "send", aliases=["dispatch"], help="paste text into one window and submit it"
    )
    p_send.add_argument("target", help="window index, name, or unique substring")
    p_send.add_argument("text", nargs="*", help="text to send (or '-' / --stdin to read stdin)")
    add_submit_opts(p_send)
    p_send.set_defaults(func=cmd_send)

    p_broadcast = subparsers.add_parser(
        "broadcast", aliases=["all"], help="send the same text to every agent window"
    )
    p_broadcast.add_argument("text", nargs="*", help="text to send (or '-' / --stdin to read stdin)")
    add_submit_opts(p_broadcast)
    p_broadcast.add_argument("--only", help="only windows whose name contains this substring")
    p_broadcast.add_argument(
        "--include-self", action="store_true", help="also send to the coordinator's own window"
    )
    p_broadcast.add_argument(
        "--all-windows", action="store_true",
        help="target every window, not just those that look like agents",
    )
    p_broadcast.set_defaults(func=cmd_broadcast)

    p_wait = subparsers.add_parser(
        "wait", aliases=["settle"], help="block until a window's output stops changing"
    )
    p_wait.add_argument("target", help="window index, name, or unique substring")
    p_wait.add_argument(
        "--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT,
        help=f"give up after this many seconds (default: {DEFAULT_WAIT_TIMEOUT:g}); exit 124",
    )
    p_wait.add_argument(
        "--settle", type=float, default=DEFAULT_WAIT_SETTLE,
        help=f"treat as done after this many quiet seconds (default: {DEFAULT_WAIT_SETTLE:g})",
    )
    p_wait.add_argument(
        "--interval", type=float, default=DEFAULT_WAIT_INTERVAL,
        help=f"polling interval in seconds (default: {DEFAULT_WAIT_INTERVAL:g})",
    )
    p_wait.set_defaults(func=cmd_wait)

    p_rename = subparsers.add_parser(
        "rename",
        aliases=["name", "label"],
        help="give a window a stable, readable name (turns off tmux auto-rename)",
    )
    p_rename.add_argument("target", help="window index, name, or unique substring")
    p_rename.add_argument("name", help="the new window name, e.g. backend")
    p_rename.set_defaults(func=cmd_rename)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tmux = Tmux(args.socket)
    try:
        return int(args.func(tmux, args))
    except UsageError as exc:
        print(f"tmux-orchestrator: {exc}", file=sys.stderr)
        return 2
    except TmuxError as exc:
        print(f"tmux-orchestrator: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
