from __future__ import annotations

import contextlib
import io
import pathlib
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Sequence

import tmux_orchestrator as orch


@dataclass
class FakeTransport:
    pasted: list[tuple[str, str]]
    submitted: list[tuple[str, str]]
    styled: list[str]
    notifications: list[str]
    fail_submit: bool = False
    submit_delays: list[float] = field(default_factory=list[float])
    submit_enter_counts: list[int] = field(default_factory=list[int])

    def paste_text(self, pane_id: str, text: str) -> None:
        self.pasted.append((pane_id, text))

    def submit_text(
        self,
        pane_id: str,
        text: str,
        *,
        enter_delay_seconds: float = orch.DEFAULT_SUBMIT_ENTER_DELAY_SECONDS,
        enter_count: int = orch.DEFAULT_SUBMIT_ENTER_COUNT,
    ) -> None:
        self.submitted.append((pane_id, text))
        self.submit_delays.append(enter_delay_seconds)
        self.submit_enter_counts.append(enter_count)
        if self.fail_submit:
            raise orch.TmuxError("enter failed")

    def style_worker(self, worker: orch.WorkerRecord) -> None:
        self.styled.append(worker.name)

    def notify_orchestrator(self, text: str) -> None:
        self.notifications.append(text)


class OrchestratorTest(unittest.TestCase):
    def make_store(self) -> tuple[tempfile.TemporaryDirectory[str], orch.Store]:
        tmp = tempfile.TemporaryDirectory()
        store = orch.Store(pathlib.Path(tmp.name))
        store.init_schema()
        return tmp, store

    def add_idle_worker(self, store: orch.Store, name: str = "worker.codex") -> orch.WorkerRecord:
        log = store.state_dir / "logs" / "panes" / f"{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        return store.upsert_worker_seen(name, "%1", log, "codex-yolo")

    def test_parse_result_block(self) -> None:
        text = """noise
@orchestrator <<'RESULT'
job_id: job_42
route_token: rt_abc
from: worker.codex
status: done
kind: patch
---
I added tests.
RESULT
"""
        blocks = orch.parse_message_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].target, "orchestrator")
        self.assertEqual(blocks[0].label, "RESULT")
        self.assertEqual(blocks[0].headers["job_id"], "job_42")
        self.assertEqual(blocks[0].body, "I added tests.")

    def test_dispatch_does_not_send_to_busy_worker(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id="job_running")
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_queued")
        fake = FakeTransport([], [], [], [])

        orch.dispatch_queued_jobs(store, fake)

        self.assertEqual(fake.pasted, [])
        self.assertEqual(fake.submitted, [])
        job = store.get_job("job_queued")
        assert job is not None
        self.assertEqual(job.state, orch.JobState.QUEUED)

    def test_dispatch_sends_to_idle_worker_and_marks_busy(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_queued", route_token="rt_ok")
        fake = FakeTransport([], [], [], [])

        orch.dispatch_queued_jobs(store, fake)

        self.assertEqual(len(fake.submitted), 1)
        self.assertIn("job_id: job_queued", fake.submitted[0][1])
        self.assertEqual(fake.submit_delays, [orch.CODEX_SUBMIT_ENTER_DELAY_SECONDS])
        self.assertEqual(fake.submit_enter_counts, [orch.CODEX_SUBMIT_ENTER_COUNT])
        updated_worker = store.get_worker(worker.name)
        job = store.get_job("job_queued")
        assert updated_worker is not None
        assert job is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.BUSY)
        self.assertEqual(job.state, orch.JobState.RUNNING)
        self.assertIsNotNone(job.started_at)
        self.assertIsNone(job.finished_at)
        self.assertIsNone(job.duration_ms)

    def test_dispatch_submit_failure_marks_job_failed_without_retrying(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_queued", route_token="rt_ok")
        fake = FakeTransport([], [], [], [], fail_submit=True)

        orch.dispatch_queued_jobs(store, fake)
        orch.dispatch_queued_jobs(store, fake)

        self.assertEqual(len(fake.submitted), 1)
        updated_worker = store.get_worker(worker.name)
        job = store.get_job("job_queued")
        assert updated_worker is not None
        assert job is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.FAILED)
        self.assertEqual(job.state, orch.JobState.FAILED)

    def test_result_missing_job_id_marks_protocol_error(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        fake = FakeTransport([], [], [], [])
        block = orch.MessageBlock(
            target="orchestrator",
            label="RESULT",
            headers={"from": worker.name, "status": "done", "route_token": "rt_ok"},
            body="done",
            raw="raw",
        )

        orch.handle_result_block(store, fake, worker.name, block)

        updated_worker = store.get_worker(worker.name)
        assert updated_worker is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.PROTOCOL_ERROR)
        self.assertEqual(fake.notifications, [f"protocol error from {worker.name}: RESULT missing job_id"])

    def test_valid_result_marks_job_done_and_worker_idle(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state("job_1", orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id="job_1")
        fake = FakeTransport([], [], [], [])
        block = orch.MessageBlock(
            target="orchestrator",
            label="RESULT",
            headers={
                "job_id": "job_1",
                "protocol": orch.PROTOCOL_VERSION,
                "from": worker.name,
                "status": "done",
                "route_token": "rt_ok",
            },
            body="done",
            raw="raw",
        )

        orch.handle_result_block(store, fake, worker.name, block)

        updated_worker = store.get_worker(worker.name)
        job = store.get_job("job_1")
        assert updated_worker is not None
        assert job is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.IDLE)
        self.assertEqual(job.state, orch.JobState.DONE)
        self.assertIsNone(updated_worker.recent_until)
        self.assertIsNotNone(job.finished_at)
        self.assertIsNotNone(job.duration_ms)

    def test_complete_active_job_marks_job_done_and_worker_idle(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state("job_1", orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id="job_1")
        fake = FakeTransport([], [], [], [])

        orch.complete_active_job(store, fake, "job_1", "done", "done through command", route_token="rt_ok")

        updated_worker = store.get_worker(worker.name)
        job = store.get_job("job_1")
        assert updated_worker is not None
        assert job is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.IDLE)
        self.assertEqual(job.state, orch.JobState.DONE)
        self.assertEqual(job.result_summary, "done through command")
        self.assertEqual(fake.styled, [worker.name])

    def test_complete_active_job_rejects_wrong_route_token(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state("job_1", orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id="job_1")
        fake = FakeTransport([], [], [], [])

        with self.assertRaises(orch.ProtocolError):
            orch.complete_active_job(store, fake, "job_1", "done", "done", route_token="rt_wrong")

    def test_complete_active_job_rejects_contradictory_terminal_completion(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state("job_1", orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id="job_1")
        fake = FakeTransport([], [], [], [])
        orch.complete_active_job(store, fake, "job_1", "done", "done", route_token="rt_ok")

        with self.assertRaises(orch.ProtocolError):
            orch.complete_active_job(store, fake, "job_1", "failed", "failed later", route_token="rt_ok")

    def test_worker_prompt_echo_does_not_contain_parseable_result(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        job = store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state(job.job_id, orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id=job.job_id)
        log_path = pathlib.Path(worker.pane_log or "")
        log_path.write_text(orch.render_worker_prompt(job), encoding="utf-8")
        fake = FakeTransport([], [], [], [])

        orch.process_single_log(store, fake, worker.name, log_path, "worker")

        updated_job = store.get_job(job.job_id)
        assert updated_job is not None
        self.assertEqual(updated_job.state, orch.JobState.RUNNING)

    def test_worker_prompt_contains_non_parseable_result_template(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        job = store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")

        prompt = orch.render_worker_prompt(job)

        self.assertIn(f"tmux-orchestrator complete {job.job_id} --route-token {job.route_token} --status done", prompt)
        self.assertFalse(any(line.startswith("@orchestrator <<'RESULT'") for line in prompt.splitlines()))
        self.assertEqual([block for block in orch.parse_message_blocks(prompt) if block.label == "RESULT"], [])

    def test_worker_result_after_prompt_template_marks_idle(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        job = store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state(job.job_id, orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id=job.job_id)
        log_path = pathlib.Path(worker.pane_log or "")
        log_path.write_text(
            orch.render_worker_prompt(job)
            + "\n"
            + f"""
@orchestrator <<'RESULT'
protocol: {orch.PROTOCOL_VERSION}
job_id: {job.job_id}
route_token: {job.route_token}
from: {worker.name}
status: done
kind: result
---
done
RESULT
""",
            encoding="utf-8",
        )
        fake = FakeTransport([], [], [], [])

        orch.process_single_log(store, fake, worker.name, log_path, "worker")

        updated_worker = store.get_worker(worker.name)
        updated_job = store.get_job(job.job_id)
        assert updated_worker is not None
        assert updated_job is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.IDLE)
        self.assertEqual(updated_job.state, orch.JobState.DONE)

    def test_result_without_protocol_header_marks_protocol_error(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state("job_1", orch.JobState.RUNNING)
        store.set_worker_state(worker.name, orch.WorkerState.BUSY, current_job_id="job_1")
        fake = FakeTransport([], [], [], [])
        block = orch.MessageBlock(
            target="orchestrator",
            label="RESULT",
            headers={
                "job_id": "job_1",
                "from": worker.name,
                "status": "done",
                "route_token": "rt_ok",
            },
            body="done",
            raw="raw",
        )

        orch.handle_result_block(store, fake, worker.name, block)

        updated_worker = store.get_worker(worker.name)
        job = store.get_job("job_1")
        assert updated_worker is not None
        assert job is not None
        self.assertEqual(updated_worker.state, orch.WorkerState.PROTOCOL_ERROR)
        self.assertEqual(job.state, orch.JobState.PROTOCOL_ERROR)

    def test_broadcast_idle_selects_only_idle_workers(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        idle = self.add_idle_worker(store, "worker.codex")
        busy = self.add_idle_worker(store, "worker.claude")
        store.set_worker_state(busy.name, orch.WorkerState.BUSY, current_job_id="job_running")

        selected = orch.pick_broadcast_workers(store, "idle")

        self.assertEqual([worker.name for worker in selected], [idle.name])

    def test_broadcast_empty_selection_fails_before_creating_parent(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)

        with self.assertRaises(orch.ProtocolError):
            orch.create_broadcast_jobs(store, "idle", "hello", "task")

        self.assertEqual(store.list_jobs(), [])

    def test_jobs_for_wait_identifier_prefers_parent_jobs(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        self.add_idle_worker(store, "worker.codex")
        self.add_idle_worker(store, "worker.claude")
        store.create_job(
            worker_name="worker.codex",
            body="hello",
            kind="task",
            job_id="job_1",
            parent_job_id="parent_1",
        )
        store.create_job(
            worker_name="worker.claude",
            body="hello",
            kind="task",
            job_id="job_2",
            parent_job_id="parent_1",
        )

        jobs = orch.jobs_for_wait_identifier(store, "parent_1")

        self.assertEqual([job.job_id for job in jobs], ["job_1", "job_2"])

    def test_wait_result_code_tracks_done_failure_and_waiting_input(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        worker = self.add_idle_worker(store)
        store.create_job(worker_name=worker.name, body="hello", kind="task", job_id="job_1", route_token="rt_ok")
        store.set_job_state("job_1", orch.JobState.RUNNING)

        running_job = store.get_job("job_1")
        assert running_job is not None
        self.assertIsNone(orch.wait_result_code([running_job]))

        store.set_job_state("job_1", orch.JobState.WAITING_INPUT)
        waiting_job = store.get_job("job_1")
        assert waiting_job is not None
        self.assertEqual(orch.wait_result_code([waiting_job]), 2)

        store.set_job_state("job_1", orch.JobState.DONE)
        done_job = store.get_job("job_1")
        assert done_job is not None
        self.assertEqual(orch.wait_result_code([done_job]), 0)

    def test_wait_command_returns_success_for_completed_parent(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)
        self.add_idle_worker(store, "worker.codex")
        self.add_idle_worker(store, "worker.claude")
        store.create_job(
            worker_name="worker.codex",
            body="hello",
            kind="task",
            job_id="job_1",
            parent_job_id="parent_1",
        )
        store.create_job(
            worker_name="worker.claude",
            body="hello",
            kind="task",
            job_id="job_2",
            parent_job_id="parent_1",
        )
        store.set_job_state("job_1", orch.JobState.DONE, result_summary="codex done")
        store.set_job_state("job_2", orch.JobState.DONE, result_summary="claude done")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = orch.wait_command(pathlib.Path(tmp.name), "parent_1", poll_interval=0.01, timeout_seconds=0, watch=False)

        self.assertEqual(code, 0)
        self.assertIn("claude done", output.getvalue())

    def test_orchestrator_defaults_to_codex_without_prompt(self) -> None:
        command = orch.choose_orchestrator_command(None)

        self.assertEqual(command[:2], ("codex-yolo", "--no-alt-screen"))
        self.assertIn("tmux-orchestrator status", command[2])

    def test_orchestrator_override_shell(self) -> None:
        self.assertTrue(orch.choose_orchestrator_command("shell")[0].endswith("sh"))

    def test_orchestrator_override_claude_gets_bootstrap_prompt(self) -> None:
        command = orch.choose_orchestrator_command("claude")

        self.assertEqual(command[0], "claude-yolo")
        self.assertIn("human-facing orchestrator", command[1])

    def test_orchestrator_override_qwen_and_gemini_get_bootstrap_prompt(self) -> None:
        qwen_command = orch.choose_orchestrator_command("qwen")
        gemini_command = orch.choose_orchestrator_command("gemini")

        self.assertEqual(qwen_command[:2], ("qwen-yolo", "--prompt-interactive"))
        self.assertIn("human-facing orchestrator", qwen_command[2])
        self.assertEqual(gemini_command[0], "gemini-yolo")
        self.assertIn("human-facing orchestrator", gemini_command[1])

    def test_daemon_command_puts_global_state_dir_before_subcommand(self) -> None:
        command = orch.daemon_command_argv(pathlib.Path("/tmp/tmux-orch-test"))

        state_dir_index = command.index("--state-dir")
        daemon_index = command.index("daemon")
        self.assertLess(state_dir_index, daemon_index)
        self.assertEqual(command[state_dir_index + 1], "/tmp/tmux-orch-test")

    def test_default_workers_are_small_set(self) -> None:
        names = [worker.name for worker in orch.DEFAULT_WORKERS]

        self.assertEqual(
            names,
            ["worker.claude", "worker.codex", "worker.qwen", "worker.gemini"],
        )

    def test_codex_worker_does_not_pin_unsupported_model(self) -> None:
        codex_worker = next(worker for worker in orch.DEFAULT_WORKERS if worker.name == "worker.codex")

        self.assertEqual(codex_worker.command, ("codex-yolo", "--no-alt-screen"))

    def test_codex_worker_uses_longer_submit_delay(self) -> None:
        self.assertEqual(orch.submit_delay_for_worker("worker.codex"), orch.CODEX_SUBMIT_ENTER_DELAY_SECONDS)
        self.assertEqual(orch.submit_delay_for_worker("worker.claude"), orch.DEFAULT_SUBMIT_ENTER_DELAY_SECONDS)

    def test_codex_worker_gets_two_submit_enters(self) -> None:
        self.assertEqual(orch.submit_enter_count_for_worker("worker.codex"), orch.CODEX_SUBMIT_ENTER_COUNT)
        self.assertEqual(orch.submit_enter_count_for_worker("worker.claude"), orch.DEFAULT_SUBMIT_ENTER_COUNT)

    def test_default_worker_window_names_follow_worker_convention(self) -> None:
        window_names = [worker.window_name for worker in orch.DEFAULT_WORKERS]

        self.assertEqual(window_names, ["worker.claude", "worker.codex", "worker.qwen", "worker.gemini"])

    def test_idle_workers_have_no_window_colour(self) -> None:
        self.assertEqual(orch.WINDOW_STYLE_BY_STATE[orch.WorkerState.IDLE], "")

    def test_short_window_name_is_not_a_worker_convention(self) -> None:
        pane = orch.PaneInfo(
            session_name="sandbox",
            window_index="2",
            window_name="codex",
            pane_index="0",
            pane_id="%3",
            pane_title="bash",
            pane_current_command="codex",
            pane_dead=False,
        )

        self.assertIsNone(pane.logical_name)

    def test_worker_recent_until_migration_supports_existing_database(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = pathlib.Path(tmp.name)
        state_dir.mkdir(parents=True, exist_ok=True)
        db_path = state_dir / "state.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE workers (
                    name TEXT PRIMARY KEY,
                    pane_id TEXT,
                    pane_log TEXT,
                    state TEXT NOT NULL,
                    current_job_id TEXT,
                    command TEXT,
                    last_seen_at TEXT,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO workers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("worker.codex", "%1", None, "idle", None, "codex-yolo", None, None),
            )
        store = orch.Store(state_dir)

        store.init_schema()

        workers = store.list_workers()
        self.assertEqual(workers[0].name, "worker.codex")
        self.assertIsNone(workers[0].recent_until)

    def test_list_panes_is_scoped_to_current_tmux_session(self) -> None:
        tmp, store = self.make_store()
        self.addCleanup(tmp.cleanup)

        class RecordingTmux(orch.Tmux):
            def __init__(self, state_dir: pathlib.Path, store: orch.Store) -> None:
                super().__init__(state_dir, store)
                self.commands: list[tuple[str, ...]] = []

            def run(
                self,
                args: Sequence[str],
                *,
                check: bool = True,
                stdin_text: str | None = None,
            ) -> str:
                del check, stdin_text
                self.commands.append(tuple(args))
                if args[0] == "display-message":
                    return "sandbox\n"
                if args[0] == "list-panes":
                    return "sandbox\t1\tworker.codex\t0\t%1\tworker.codex\tbash\t0\n"
                return ""

        tmux = RecordingTmux(store.state_dir, store)

        panes = tmux.list_panes()

        list_command = next(command for command in tmux.commands if command[0] == "list-panes")
        self.assertEqual(list_command[:4], ("list-panes", "-s", "-t", "sandbox"))
        self.assertEqual(panes[0].session_name, "sandbox")


if __name__ == "__main__":
    unittest.main()
