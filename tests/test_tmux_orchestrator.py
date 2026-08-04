"""Unit tests for tmux-orchestrator.

These drive the submit handshake through Tmux's private helpers deliberately:
that is where the tmux-level bugs live, and the public commands are thin
wrappers over them. Hence the file-wide suppression below.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import io
import unittest
from typing import Sequence
from unittest import mock

import tmux_orchestrator as orch


def no_sleep(_seconds: float) -> None:
    """Stand-in for time.sleep where elapsed time does not matter."""


class FakeClock:
    """Deterministic stand-in for time.sleep/time.monotonic."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def recording_tmux(samples: list[str]) -> tuple[orch.Tmux, list[list[str]]]:
    """A Tmux whose capture-pane replays `samples`, recording every call.

    Once the script runs out it keeps returning the last sample, so a pane that
    has gone quiet stays quiet instead of appearing to change to "".
    """
    tmux = orch.Tmux()
    sent: list[list[str]] = []
    pending = list(samples)
    last = [samples[0] if samples else ""]

    def fake_run(args: Sequence[str], *, stdin: str | None = None, check: bool = True) -> str:
        del stdin, check
        sent.append(list(args))
        if args[0] == "capture-pane":
            if pending:
                last[0] = pending.pop(0)
            return last[0]
        return ""

    tmux.run = fake_run
    return tmux, sent


def enter_count(sent: list[list[str]]) -> int:
    return sum(1 for args in sent if args[0] == "send-keys" and args[-1] == "Enter")


def win(index: str, name: str, command: str = "node", *, active: bool = False) -> orch.Window:
    return orch.Window(
        session="sandbox",
        index=index,
        name=name,
        active=active,
        pane_id=f"%{index}",
        command=command,
        pid="1000",
    )


class ParseWindowLineTest(unittest.TestCase):
    def test_parses_a_full_line(self) -> None:
        line = "sandbox\t2\tbackend\t1\t%7\tcodex\t4242"
        window = orch.parse_window_line(line)
        assert window is not None
        self.assertEqual(window.index, "2")
        self.assertEqual(window.name, "backend")
        self.assertTrue(window.active)
        self.assertEqual(window.pane_id, "%7")
        self.assertEqual(window.command, "codex")

    def test_rejects_short_line(self) -> None:
        self.assertIsNone(orch.parse_window_line("sandbox\t2\tbackend"))


class AgentDetectionTest(unittest.TestCase):
    def test_codex_by_command(self) -> None:
        self.assertTrue(orch.looks_like_codex(win("1", "review", "codex")))

    def test_codex_by_window_name(self) -> None:
        self.assertTrue(orch.looks_like_codex(win("1", "codex-review", "node")))

    def test_non_codex(self) -> None:
        self.assertFalse(orch.looks_like_codex(win("1", "backend", "node")))

    def test_codex_by_process_argv_when_name_and_command_hide_it(self) -> None:
        # The real-world miss: codex installed as an npm package runs as a bare
        # `node`, in a window the coordinator renamed after its role. Only the
        # argv still says "codex".
        names = ["node", "codex", "--yolo"]
        self.assertTrue(orch.looks_like_codex(win("2", "sol", "node"), names))

    def test_non_codex_argv_stays_non_codex(self) -> None:
        names = ["claude", "--dangerously-skip-permissions"]
        self.assertFalse(orch.looks_like_codex(win("1", "backend", "node"), names))

    def test_a_subprocess_mentioning_codex_is_not_a_codex_window(self) -> None:
        # Names are matched exactly, so an agent grepping for "codex|submit"
        # cannot masquerade as Codex.
        names = ["grep", "-E", "codex|submit"]
        self.assertFalse(orch.looks_like_codex(win("1", "backend", "node"), names))

    def test_label_uses_process_argv(self) -> None:
        names = ["node", "codex"]
        self.assertEqual(orch.agent_label(win("2", "sol", "node"), names), "codex")

    def test_label_prefers_known_token(self) -> None:
        self.assertEqual(orch.agent_label(win("1", "frontend-claude", "node")), "claude")
        self.assertEqual(orch.agent_label(win("2", "backend", "codex")), "codex")

    def test_label_falls_back_to_command(self) -> None:
        self.assertEqual(orch.agent_label(win("1", "shell", "bash")), "bash")

    def test_is_agent_window(self) -> None:
        # Anything that is not a bare shell counts as an agent, because windows
        # are named by role ("backend") not by model, and agents run as node/codex.
        self.assertTrue(orch.is_agent_window(win("1", "backend", "codex")))
        self.assertTrue(orch.is_agent_window(win("2", "frontend", "node")))
        self.assertFalse(orch.is_agent_window(win("3", "notes", "bash")))
        self.assertFalse(orch.is_agent_window(win("4", "shell", "zsh")))
        self.assertFalse(orch.is_agent_window(win("5", "login", "-bash")))


class ResolveTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.windows = [
            win("0", "coordinator", "node"),
            win("1", "frontend", "node"),
            win("2", "backend", "codex"),
            win("3", "backend-review", "node"),
        ]

    def test_by_index(self) -> None:
        self.assertEqual(orch.resolve_target(self.windows, "2").name, "backend")

    def test_by_exact_name_even_when_substring_of_another(self) -> None:
        # "backend" is a substring of "backend-review", but exact match wins.
        self.assertEqual(orch.resolve_target(self.windows, "backend").index, "2")

    def test_by_unique_substring(self) -> None:
        self.assertEqual(orch.resolve_target(self.windows, "front").name, "frontend")

    def test_ambiguous_substring_raises(self) -> None:
        with self.assertRaises(orch.UsageError):
            orch.resolve_target(self.windows, "back")  # backend + backend-review
        with self.assertRaises(orch.UsageError):
            orch.resolve_target(self.windows, "e")  # matches many

    def test_unknown_target_raises(self) -> None:
        with self.assertRaises(orch.UsageError):
            orch.resolve_target(self.windows, "nope")


class ResolveTextTest(unittest.TestCase):
    def test_joins_positional_args(self) -> None:
        with mock.patch.object(orch.sys, "stdin", io.StringIO("")):
            orch.sys.stdin.isatty = lambda: True  # type: ignore[method-assign]
            self.assertEqual(orch.resolve_text(["hello", "world"], False), "hello world")

    def test_reads_stdin_on_flag(self) -> None:
        fake = io.StringIO("multi\nline\n")
        fake.isatty = lambda: False  # type: ignore[method-assign]
        with mock.patch.object(orch.sys, "stdin", fake):
            self.assertEqual(orch.resolve_text([], True), "multi\nline")

    def test_reads_stdin_on_dash(self) -> None:
        fake = io.StringIO("piped\n")
        fake.isatty = lambda: False  # type: ignore[method-assign]
        with mock.patch.object(orch.sys, "stdin", fake):
            self.assertEqual(orch.resolve_text(["-"], False), "piped")

    def test_no_text_no_stdin_raises(self) -> None:
        fake = io.StringIO("")
        fake.isatty = lambda: True  # type: ignore[method-assign]
        with mock.patch.object(orch.sys, "stdin", fake):
            with self.assertRaises(orch.UsageError):
                orch.resolve_text([], False)


class CodexStrategyTest(unittest.TestCase):
    def test_auto_follows_detection(self) -> None:
        self.assertTrue(orch.use_codex_strategy(win("1", "backend", "codex"), "auto"))
        self.assertFalse(orch.use_codex_strategy(win("1", "backend", "node"), "auto"))

    def test_explicit_overrides(self) -> None:
        self.assertTrue(orch.use_codex_strategy(win("1", "backend", "node"), "codex"))
        self.assertFalse(orch.use_codex_strategy(win("1", "backend", "codex"), "generic"))

    def test_auto_uses_process_argv(self) -> None:
        names = ["node", "codex"]
        self.assertTrue(orch.use_codex_strategy(win("2", "sol", "node"), "auto", names))


class SubmitConfirmationTest(unittest.TestCase):
    """The submit is a handshake: Enter, then check the pane actually reacted."""

    def test_reports_success_when_the_pane_reacts(self) -> None:
        # before-sample, then a changed sample after Enter.
        tmux, sent = recording_tmux(["composer: hello", "thinking..."])
        with mock.patch.object(orch.time, "sleep", no_sleep):
            outcome = tmux._press_enter_confirmed("%1", settled=True)
        self.assertEqual(outcome, orch.SUBMIT_SENT)
        self.assertEqual(enter_count(sent), 1)

    def test_presses_again_when_the_first_enter_is_swallowed(self) -> None:
        # Pane identical after the first Enter (swallowed), changed after the second.
        tmux, sent = recording_tmux(["composer: hello", "composer: hello", "thinking..."])
        with mock.patch.object(orch.time, "sleep", no_sleep):
            outcome = tmux._press_enter_confirmed("%1", settled=True)
        self.assertEqual(outcome, orch.SUBMIT_SENT)
        self.assertEqual(enter_count(sent), 2)

    def test_reports_failure_when_the_pane_never_reacts(self) -> None:
        tmux, sent = recording_tmux(["composer: hello"] * 3)
        with mock.patch.object(orch.time, "sleep", no_sleep):
            outcome = tmux._press_enter_confirmed("%1", settled=True)
        self.assertEqual(outcome, orch.SUBMIT_UNSENT)
        self.assertEqual(enter_count(sent), 2)


class TypedLineOptionSafetyTest(unittest.TestCase):
    """A prompt line beginning with `-` must not be parsed as a tmux flag.

    Without the `--` separator tmux answers "unknown option --" and the send
    dies partway through, leaving a truncated prompt in the composer that looks
    like a complete one. Any Markdown bullet list triggers it.
    """

    def test_literal_send_is_separated_from_options(self) -> None:
        tmux, sent = recording_tmux([])
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep):
            tmux._submit_by_typing("%1", "- dashed bullet", enter=False)

        literal = [args for args in sent if args[0] == "send-keys" and "-l" in args]
        self.assertEqual(literal, [["send-keys", "-t", "%1", "-l", "--", "- dashed bullet"]])

    def test_every_line_of_a_multiline_prompt_is_separated(self) -> None:
        tmux, sent = recording_tmux([])
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep):
            tmux._submit_by_typing("%1", "intro\n- one\n- two", enter=False)

        literal = [args for args in sent if args[0] == "send-keys" and "-l" in args]
        self.assertTrue(all(args[4] == "--" for args in literal), literal)
        self.assertEqual([args[5] for args in literal], ["intro", "- one", "- two"])

    def test_rename_separates_a_dash_leading_window_name(self) -> None:
        tmux, sent = recording_tmux([])
        tmux.rename_window("sandbox:2", "-weird")
        rename = [args for args in sent if args[0] == "rename-window"]
        self.assertEqual(rename, [["rename-window", "-t", "sandbox:2", "--", "-weird"]])


class BracketedPasteTest(unittest.TestCase):
    """A paste must be delivered inside the terminal's bracketed-paste markers.

    Without `-p`, tmux hands the composer a bare stream with every newline
    turned into a carriage return. The TUI then has to guess from timing where
    the paste begins and ends — which is what splits one prompt into several
    paste blocks and lets an Enter fall into the gap. tmux only adds the markers
    when the application asked for them, so `-p` is always safe to pass.
    """

    def test_paste_buffer_requests_bracketed_paste(self) -> None:
        tmux, sent = recording_tmux(["idle", "burst", "burst", "burst", "burst"])
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep):
            tmux._submit_by_paste("%1", "hello", enter=False, enter_delay=0.0)

        paste = [args for args in sent if args[0] == "paste-buffer"]
        self.assertEqual(len(paste), 1)
        self.assertIn("-p", paste[0])

    def test_buffer_is_deleted_when_the_paste_fails(self) -> None:
        # -d only deletes on a successful paste, so a failure would leak the
        # buffer into the tmux server for the rest of its life.
        tmux = orch.Tmux()
        sent: list[list[str]] = []

        def fake_run(args: Sequence[str], *, stdin: str | None = None, check: bool = True) -> str:
            del stdin, check
            sent.append(list(args))
            if args[0] == "paste-buffer":
                raise orch.TmuxError("no such pane")
            return ""

        tmux.run = fake_run
        with self.assertRaises(orch.TmuxError):
            tmux._submit_by_paste("%1", "hello", enter=False, enter_delay=0.0)

        deleted = [args for args in sent if args[0] == "delete-buffer"]
        loaded = [args for args in sent if args[0] == "load-buffer"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0][2], loaded[0][2])  # same buffer name


class ComposerQuietBaselineTest(unittest.TestCase):
    """Quiet must mean 'burst finished', never 'burst not started'."""

    def _wait(self, samples: list[str], baseline: str | None) -> tuple[bool, int]:
        tmux, sent = recording_tmux(samples)
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep), mock.patch.object(
            orch.time, "monotonic", clock.monotonic
        ):
            settled = tmux._wait_for_composer_quiet("%1", floor=0.0, baseline=baseline)
        return settled, sum(1 for args in sent if args[0] == "capture-pane")

    def test_waits_through_a_pane_that_has_not_started_rendering(self) -> None:
        # The pane sits unchanged for a full second — longer than the 0.35s
        # quiet threshold — before the burst finally draws. Calling that quiet
        # is the bug: Enter would be pressed into a paste that has not landed.
        # There are 10 idle samples, so settling before capture 11 means the
        # wait ended while the pane still looked exactly like it did pre-paste.
        idle_samples = 10
        settled, captures = self._wait(["idle"] * idle_samples + ["burst"] * 10, "idle")
        self.assertTrue(settled)
        self.assertGreater(
            captures,
            idle_samples,
            "settled while the pane was still byte-identical to its pre-paste state",
        )

    def test_a_burst_that_lands_immediately_is_not_delayed(self) -> None:
        settled, captures = self._wait(["burst"] * 10, "idle")
        self.assertTrue(settled)
        self.assertLessEqual(captures, 6)

    def test_a_pane_that_never_renders_gives_up_rather_than_hanging(self) -> None:
        # Bounded by PASTE_QUIET_TIMEOUT_SECONDS: a composer that draws nothing
        # at all must not block the coordinator forever.
        settled, _ = self._wait(["idle"] * 500, "idle")
        self.assertFalse(settled)

    def test_a_pane_that_never_stops_changing_does_not_settle(self) -> None:
        # An agent that is already thinking repaints a spinner forever.
        churn = [f"thinking {i}" for i in range(400)]
        settled, _ = self._wait(churn, "idle")
        self.assertFalse(settled)


class BusyPaneIsNotReportedAsSentTest(unittest.TestCase):
    """Ambient redraw must not be mistaken for a reaction to Enter.

    This is the false success that matters most: an agent that is already busy
    repaints several times a second, so `before != after` is satisfied no matter
    what Enter did — and a prompt that never arrived came back reported as sent.
    """

    def test_unsettled_pane_reports_unconfirmed_not_sent(self) -> None:
        tmux, sent = recording_tmux([f"thinking {i}" for i in range(20)])
        with mock.patch.object(orch.time, "sleep", no_sleep):
            outcome = tmux._press_enter_confirmed("%1", settled=False)
        self.assertEqual(outcome, orch.SUBMIT_UNCONFIRMED)
        # Exactly one Enter: we cannot verify, so we do not keep hammering.
        self.assertEqual(
            sum(1 for args in sent if args[0] == "send-keys" and args[-1] == "Enter"), 1
        )

    def test_end_to_end_a_churning_pane_never_reports_sent(self) -> None:
        churn = [f"thinking {i}" for i in range(500)]
        tmux, _ = recording_tmux(churn)
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep), mock.patch.object(
            orch.time, "monotonic", clock.monotonic
        ):
            outcome = tmux._submit_by_paste("%1", "hi", enter=True, enter_delay=0.0)
        self.assertEqual(outcome, orch.SUBMIT_UNCONFIRMED)

    def test_unconfirmed_is_not_a_success_exit_code(self) -> None:
        self.assertEqual(orch.SUBMIT_EXIT_CODES[orch.SUBMIT_SENT], 0)
        self.assertEqual(orch.SUBMIT_EXIT_CODES[orch.SUBMIT_QUEUED], 0)
        self.assertNotEqual(orch.SUBMIT_EXIT_CODES[orch.SUBMIT_UNCONFIRMED], 0)
        self.assertNotEqual(orch.SUBMIT_EXIT_CODES[orch.SUBMIT_UNSENT], 0)
        # The three outcomes stay distinguishable to a caller reading $?.
        self.assertNotEqual(
            orch.SUBMIT_EXIT_CODES[orch.SUBMIT_UNCONFIRMED],
            orch.SUBMIT_EXIT_CODES[orch.SUBMIT_UNSENT],
        )


class SubmitBaselineCaptureTest(unittest.TestCase):
    """The baseline has to be sampled before the text is sent, not after."""

    def test_paste_samples_the_pane_before_loading_the_buffer(self) -> None:
        tmux, sent = recording_tmux(["idle", "burst", "burst", "burst", "burst"])
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep), mock.patch.object(
            orch.time, "monotonic", clock.monotonic
        ):
            tmux._submit_by_paste("%1", "hello", enter=False, enter_delay=0.0)

        verbs = [args[0] for args in sent]
        self.assertEqual(verbs[0], "capture-pane")
        self.assertLess(verbs.index("capture-pane"), verbs.index("load-buffer"))

    def test_typing_samples_the_pane_before_the_first_keystroke(self) -> None:
        tmux, sent = recording_tmux(["idle", "burst", "burst", "burst", "burst"])
        clock = FakeClock()
        with mock.patch.object(orch.time, "sleep", clock.sleep), mock.patch.object(
            orch.time, "monotonic", clock.monotonic
        ):
            tmux._submit_by_typing("%1", "hello", enter=False)

        verbs = [args[0] for args in sent]
        self.assertEqual(verbs[0], "capture-pane")
        self.assertLess(verbs.index("capture-pane"), verbs.index("send-keys"))


class EmptyPromptTest(unittest.TestCase):
    """An empty prompt would paste nothing and then stall the whole handshake."""

    def test_empty_stdin_is_rejected(self) -> None:
        fake = io.StringIO("\n  \n")
        fake.isatty = lambda: False  # type: ignore[method-assign]
        with mock.patch.object(orch.sys, "stdin", fake):
            with self.assertRaises(orch.UsageError):
                orch.resolve_text(["-"], False)


if __name__ == "__main__":
    unittest.main()
