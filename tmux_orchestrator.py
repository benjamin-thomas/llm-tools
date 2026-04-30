#!/usr/bin/env python3
# Type checking:
#   pyright --project pyrightconfig.json
#
# VS Code:
#   Install/enable the Python extension and Pylance. Pylance reads
#   pyrightconfig.json in this repository, so type errors in this file should
#   appear directly in the editor.

from __future__ import annotations

import argparse
import datetime as dt
import enum
import hashlib
import os
import pathlib
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol, Sequence, TypedDict, cast


APP_DIR_NAME = ".tmux-orchestrator"
DEFAULT_STATE_DIR = pathlib.Path(APP_DIR_NAME)
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_SUBMIT_ENTER_DELAY_SECONDS = 0.35
CODEX_SUBMIT_ENTER_DELAY_SECONDS = 1.00
DEFAULT_SUBMIT_ENTER_COUNT = 1
CODEX_SUBMIT_ENTER_COUNT = 2
CODEX_SUBMIT_EXTRA_ENTER_DELAY_SECONDS = 0.75
PROTOCOL_VERSION = "tmux-orchestrator/v1"

WorkerStateValue = Literal["idle", "busy", "waiting_input", "failed", "protocol_error"]
JobStateValue = Literal["queued", "running", "waiting_input", "failed", "done", "protocol_error"]
BlockLabel = Literal["TASK", "RESULT"]
EventLevel = Literal["debug", "info", "success", "warning", "error", "protocol_error"]
ResultStatusValue = Literal["done", "waiting_input", "failed"]


class WorkerState(str, enum.Enum):
    IDLE = "idle"
    BUSY = "busy"
    WAITING_INPUT = "waiting_input"
    FAILED = "failed"
    PROTOCOL_ERROR = "protocol_error"


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    FAILED = "failed"
    DONE = "done"
    PROTOCOL_ERROR = "protocol_error"


class TmuxError(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    window_name: str
    command: tuple[str, ...]
    submit_delay_seconds: float = DEFAULT_SUBMIT_ENTER_DELAY_SECONDS
    submit_enter_count: int = DEFAULT_SUBMIT_ENTER_COUNT


@dataclass(frozen=True)
class PaneInfo:
    session_name: str
    window_index: str
    window_name: str
    pane_index: str
    pane_id: str
    pane_title: str
    pane_current_command: str
    pane_dead: bool

    @property
    def logical_name(self) -> str | None:
        for candidate in (self.pane_title, self.window_name):
            if candidate == "orchestrator" or candidate == "logs":
                return candidate
            if candidate.startswith("worker."):
                return candidate
        return None


@dataclass(frozen=True)
class WorkerRecord:
    name: str
    pane_id: str | None
    pane_log: str | None
    state: WorkerState
    current_job_id: str | None
    command: str | None
    last_seen_at: str | None
    last_error: str | None
    recent_until: float | None


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    parent_job_id: str | None
    worker_name: str
    state: JobState
    kind: str
    body: str
    route_token: str
    attempt: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    result_summary: str | None
    last_error: str | None


@dataclass(frozen=True)
class MessageBlock:
    target: str
    label: BlockLabel
    headers: Mapping[str, str]
    body: str
    raw: str


WaitResultCode = Literal[0, 1, 2, 124]


class WorkerSummary(TypedDict):
    name: str
    state: WorkerStateValue
    pane_id: str | None
    current_job_id: str | None
    last_error: str | None


class JobSummary(TypedDict):
    job_id: str
    worker_name: str
    state: JobStateValue
    kind: str
    parent_job_id: str | None
    last_error: str | None


class Transport(Protocol):
    def paste_text(self, pane_id: str, text: str) -> None:
        ...

    def submit_text(
        self,
        pane_id: str,
        text: str,
        *,
        enter_delay_seconds: float = DEFAULT_SUBMIT_ENTER_DELAY_SECONDS,
        enter_count: int = DEFAULT_SUBMIT_ENTER_COUNT,
    ) -> None:
        ...

    def style_worker(self, worker: WorkerRecord) -> None:
        ...

    def notify_orchestrator(self, text: str) -> None:
        ...


DEFAULT_WORKERS: tuple[WorkerSpec, ...] = (
    WorkerSpec("worker.claude", "worker.claude", ("claude-yolo",)),
    WorkerSpec(
        "worker.codex",
        "worker.codex",
        ("codex-yolo", "--no-alt-screen"),
        CODEX_SUBMIT_ENTER_DELAY_SECONDS,
        CODEX_SUBMIT_ENTER_COUNT,
    ),
    WorkerSpec("worker.qwen", "worker.qwen", ("qwen-yolo",)),
    WorkerSpec("worker.gemini", "worker.gemini", ("gemini-yolo",)),
)

DEFAULT_WORKER_SUBMIT_DELAY_BY_NAME: dict[str, float] = {
    spec.name: spec.submit_delay_seconds for spec in DEFAULT_WORKERS
}

DEFAULT_WORKER_SUBMIT_ENTER_COUNT_BY_NAME: dict[str, int] = {
    spec.name: spec.submit_enter_count for spec in DEFAULT_WORKERS
}

DEPRECATED_SHORT_WORKER_WINDOW_NAMES: tuple[str, ...] = ("claude", "codex", "qwen", "gemini")

DEPRECATED_DEFAULT_WORKER_NAMES: tuple[str, ...] = (
    "worker.claude-haiku",
    "worker.claude-opus",
    "worker.claude-sonnet",
    "worker.codex-gpt5",
)

ORCHESTRATOR_BOOTSTRAP_PROMPT = """\
You are the human-facing orchestrator inside a local tmux-orchestrator session.

The human talks to you in natural language. Your job is to help coordinate visible tmux worker panes through the local CLI, while keeping the human in the loop.

Use these commands when useful:
- tmux-orchestrator status
- tmux-orchestrator send <worker.name> "<task>"
- tmux-orchestrator broadcast --to idle "<task>"
- tmux-orchestrator wait <job_id-or-parent_job_id> --watch
- tmux-orchestrator mark-done <worker.name> <job_id>
- tmux-orchestrator fail <worker.name> <job_id>
- tmux-orchestrator retry <job_id>

Workers are named worker.* and must not communicate directly with each other. The router creates job_id and route_token values when you use send/broadcast. Prefer using the CLI commands instead of hand-writing protocol blocks.

Workers are responsible for completing their own jobs by running tmux-orchestrator complete <job_id> --route-token <route_token> --status done --summary "...". The router marks that worker idle as soon as complete succeeds, even if other workers are still running.

RESULT blocks are only a fallback for workers that cannot run shell commands. Use mark-done only as a manual fallback when a worker visibly finished but failed to call complete.

After send or broadcast, use tmux-orchestrator wait <job_id-or-parent_job_id> --watch to wait for local SQLite state changes. Do not implement custom monitor loops in natural language.

Check status and .tmux-orchestrator/logs/events.log when the human asks what is happening. Delegate only when it helps, summarize worker results clearly, and ask the human before taking broad or risky actions.

When you use tmux-orchestrator send or broadcast, the router submits the task to the worker automatically. Do not operate worker panes manually unless you are diagnosing a stuck interface.

Acknowledge briefly that you are ready to orchestrate, then wait for the human's next instruction.
"""


ANSI_RESET = "\033[0m"
ANSI_BY_LEVEL: dict[EventLevel, str] = {
    "debug": "\033[2m",
    "info": "\033[34m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "protocol_error": "\033[1;31m",
}

PANE_STYLE_BY_STATE: dict[WorkerState, str] = {
    WorkerState.IDLE: "default",
    WorkerState.BUSY: "fg=blue",
    WorkerState.WAITING_INPUT: "fg=yellow",
    WorkerState.FAILED: "fg=red",
    WorkerState.PROTOCOL_ERROR: "fg=brightred,bold",
}

WINDOW_STYLE_BY_STATE: dict[WorkerState, str] = {
    WorkerState.IDLE: "",
    WorkerState.BUSY: "bg=colour27,fg=colour255,bold",
    WorkerState.WAITING_INPUT: "bg=colour226,fg=colour16,bold",
    WorkerState.FAILED: "bg=colour160,fg=colour255,bold",
    WorkerState.PROTOCOL_ERROR: "bg=colour196,fg=white,bold",
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def duration_ms_between(started_at: str, finished_at: str) -> int:
    elapsed = parse_iso_datetime(finished_at) - parse_iso_datetime(started_at)
    return max(0, int(elapsed.total_seconds() * 1000))


def format_duration_ms(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "-"
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)
    return f"{minutes}m{remaining_seconds:02d}s"


def job_elapsed_ms(job: JobRecord) -> int | None:
    if job.started_at is None:
        return None
    finished_at = job.finished_at or now_iso()
    return duration_ms_between(job.started_at, finished_at)


def job_duration_label(job: JobRecord) -> str:
    if job.duration_ms is not None:
        return format_duration_ms(job.duration_ms)
    elapsed_ms = job_elapsed_ms(job)
    if elapsed_ms is None:
        return "-"
    return format_duration_ms(elapsed_ms) + " elapsed"


def default_state_dir() -> pathlib.Path:
    return DEFAULT_STATE_DIR


def ensure_state_dirs(state_dir: pathlib.Path) -> None:
    (state_dir / "logs" / "workers").mkdir(parents=True, exist_ok=True)
    (state_dir / "logs" / "panes").mkdir(parents=True, exist_ok=True)


def safe_log_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def generate_job_id() -> str:
    return "job_" + secrets.token_hex(6)


def generate_parent_job_id() -> str:
    return "parent_" + secrets.token_hex(6)


def generate_route_token() -> str:
    return "rt_" + secrets.token_hex(12)


def parse_worker_state(value: str) -> WorkerState:
    try:
        return WorkerState(value)
    except ValueError as exc:
        raise ProtocolError(f"unknown worker state in database: {value}") from exc


def parse_job_state(value: str) -> JobState:
    try:
        return JobState(value)
    except ValueError as exc:
        raise ProtocolError(f"unknown job state in database: {value}") from exc


def maybe_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"cannot convert {type(value).__name__} to float")


def row_object(row: sqlite3.Row, key: str) -> object:
    return cast(object, row[key])


def row_str(row: sqlite3.Row, key: str) -> str:
    value = row_object(row, key)
    if isinstance(value, str):
        return value
    raise TypeError(f"database column {key} is not str")


def row_opt_str(row: sqlite3.Row, key: str) -> str | None:
    value = row_object(row, key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise TypeError(f"database column {key} is not optional str")


def row_int(row: sqlite3.Row, key: str) -> int:
    value = row_object(row, key)
    if isinstance(value, int):
        return value
    raise TypeError(f"database column {key} is not int")


def row_opt_int(row: sqlite3.Row, key: str) -> int | None:
    value = row_object(row, key)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise TypeError(f"database column {key} is not optional int")


class EventLog:
    def __init__(self, state_dir: pathlib.Path) -> None:
        ensure_state_dirs(state_dir)
        self.path = state_dir / "logs" / "events.log"
        self.use_color = "NO_COLOR" not in os.environ
        self.path.touch(exist_ok=True)

    def write(self, level: EventLevel, message: str, *, worker: str | None = None, job_id: str | None = None) -> None:
        prefix_parts = [now_iso(), level.upper()]
        if worker is not None:
            prefix_parts.append(worker)
        if job_id is not None:
            prefix_parts.append(job_id)
        prefix = " ".join(prefix_parts)
        clean_message = message.replace("\n", "\\n")
        line = f"[{prefix}] {clean_message}"
        if self.use_color:
            color = ANSI_BY_LEVEL[level]
            line = f"{color}{line}{ANSI_RESET}"
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")


class Store:
    def __init__(self, state_dir: pathlib.Path) -> None:
        ensure_state_dirs(state_dir)
        self.state_dir = state_dir
        self.db_path = state_dir / "state.sqlite3"
        self.events = EventLog(state_dir)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    name TEXT PRIMARY KEY,
                    pane_id TEXT,
                    pane_log TEXT,
                    state TEXT NOT NULL,
                    current_job_id TEXT,
                    command TEXT,
                    last_seen_at TEXT,
                    last_error TEXT,
                    recent_until REAL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    parent_job_id TEXT,
                    worker_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    route_token TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    result_summary TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS processed_blocks (
                    block_hash TEXT PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    label TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self.ensure_worker_runtime_columns(conn)
            self.ensure_job_timing_columns(conn)

    def ensure_worker_runtime_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(workers)").fetchall()
        existing_columns = {row_str(cast(sqlite3.Row, row), "name") for row in rows}
        if "recent_until" not in existing_columns:
            conn.execute("ALTER TABLE workers ADD COLUMN recent_until REAL")

    def ensure_job_timing_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
        existing_columns = {row_str(cast(sqlite3.Row, row), "name") for row in rows}
        if "started_at" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN started_at TEXT")
        if "finished_at" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN finished_at TEXT")
        if "duration_ms" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN duration_ms INTEGER")
        if "result_summary" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN result_summary TEXT")

    def reset_runtime_state(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                DELETE FROM processed_blocks;
                DELETE FROM jobs;
                DELETE FROM workers;
                DELETE FROM meta;
                """
            )
        panes_dir = self.state_dir / "logs" / "panes"
        if panes_dir.exists():
            for path in panes_dir.glob("*.log"):
                try:
                    path.unlink()
                except OSError as exc:
                    self.event("warning", f"could not remove stale pane log {path}: {exc}")
        self.event("info", "runtime state reset")

    def event(self, level: EventLevel, message: str, *, worker: str | None = None, job_id: str | None = None) -> None:
        self.events.write(level, message, worker=worker, job_id=job_id)

    def upsert_worker_seen(self, name: str, pane_id: str, pane_log: pathlib.Path, command: str | None) -> WorkerRecord:
        current = self.get_worker(name)
        next_state = current.state if current is not None else WorkerState.IDLE
        last_error = current.last_error if current is not None else None
        current_job_id = current.current_job_id if current is not None else None
        recent_until = current.recent_until if current is not None else None
        if next_state in (WorkerState.FAILED, WorkerState.PROTOCOL_ERROR) and current_job_id is None:
            next_state = WorkerState.IDLE
            last_error = None
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workers (
                    name, pane_id, pane_log, state, current_job_id, command,
                    last_seen_at, last_error, recent_until
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    pane_id = excluded.pane_id,
                    pane_log = excluded.pane_log,
                    command = COALESCE(excluded.command, workers.command),
                    last_seen_at = excluded.last_seen_at,
                    state = excluded.state,
                    current_job_id = excluded.current_job_id,
                    last_error = excluded.last_error,
                    recent_until = excluded.recent_until
                """,
                (
                    name,
                    pane_id,
                    str(pane_log),
                    next_state.value,
                    current_job_id,
                    command,
                    timestamp,
                    last_error,
                    recent_until,
                ),
            )
        record = self.get_worker(name)
        if record is None:
            raise RuntimeError(f"failed to upsert worker {name}")
        return record

    def ensure_missing_default_worker_failed(self, spec: WorkerSpec, error: str) -> None:
        timestamp = now_iso()
        command = shlex.join(spec.command)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workers (
                    name, pane_id, pane_log, state, current_job_id, command,
                    last_seen_at, last_error, recent_until
                )
                VALUES (?, NULL, NULL, ?, NULL, ?, ?, ?, NULL)
                ON CONFLICT(name) DO UPDATE SET
                    state = excluded.state,
                    command = excluded.command,
                    last_seen_at = excluded.last_seen_at,
                    last_error = excluded.last_error
                """,
                (spec.name, WorkerState.FAILED.value, command, timestamp, error),
            )
        self.event("error", error, worker=spec.name)

    def get_worker(self, name: str) -> WorkerRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workers WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return worker_from_row(cast(sqlite3.Row, row))

    def list_workers(self) -> list[WorkerRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
        return [worker_from_row(cast(sqlite3.Row, row)) for row in rows]

    def list_jobs(self, states: Sequence[JobState] | None = None) -> list[JobRecord]:
        with self.connect() as conn:
            if states is None:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at, job_id").fetchall()
            else:
                placeholders = ",".join("?" for _ in states)
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at, job_id",
                    tuple(state.value for state in states),
                ).fetchall()
        return [job_from_row(cast(sqlite3.Row, row)) for row in rows]

    def get_job(self, job_id: str) -> JobRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return job_from_row(cast(sqlite3.Row, row))

    def list_jobs_by_parent(self, parent_job_id: str) -> list[JobRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE parent_job_id = ? ORDER BY created_at, job_id",
                (parent_job_id,),
            ).fetchall()
        return [job_from_row(cast(sqlite3.Row, row)) for row in rows]

    def create_job(
        self,
        *,
        worker_name: str,
        body: str,
        kind: str,
        job_id: str | None = None,
        parent_job_id: str | None = None,
        route_token: str | None = None,
    ) -> JobRecord:
        actual_job_id = job_id or generate_job_id()
        actual_route_token = route_token or generate_route_token()
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, parent_job_id, worker_name, state, kind, body,
                    route_token, attempt, created_at, updated_at,
                    started_at, finished_at, duration_ms, result_summary, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    actual_job_id,
                    parent_job_id,
                    worker_name,
                    JobState.QUEUED.value,
                    kind,
                    body,
                    actual_route_token,
                    1,
                    timestamp,
                    timestamp,
                ),
            )
        record = self.get_job(actual_job_id)
        if record is None:
            raise RuntimeError(f"failed to create job {actual_job_id}")
        self.event("info", f"queued {kind}", worker=worker_name, job_id=actual_job_id)
        return record

    def set_worker_state(
        self,
        worker_name: str,
        state: WorkerState,
        *,
        current_job_id: str | None,
        error: str | None = None,
        recent_until: float | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workers
                SET state = ?, current_job_id = ?, last_error = ?, recent_until = ?
                WHERE name = ?
                """,
                (state.value, current_job_id, error, recent_until, worker_name),
            )

    def set_job_state(
        self,
        job_id: str,
        state: JobState,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        current = self.get_job(job_id)
        if current is None:
            return
        timestamp = now_iso()
        started_at = current.started_at
        finished_at = current.finished_at
        duration_ms = current.duration_ms
        next_result_summary = result_summary if result_summary is not None else current.result_summary
        if state == JobState.RUNNING and started_at is None:
            started_at = timestamp
            finished_at = None
            duration_ms = None
            next_result_summary = None
        elif state in (JobState.DONE, JobState.FAILED, JobState.PROTOCOL_ERROR):
            finished_at = timestamp
            if started_at is not None:
                duration_ms = duration_ms_between(started_at, finished_at)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET state = ?, updated_at = ?, started_at = ?, finished_at = ?,
                    duration_ms = ?, result_summary = ?, last_error = ?
                WHERE job_id = ?
                """,
                (state.value, timestamp, started_at, finished_at, duration_ms, next_result_summary, error, job_id),
            )

    def mark_block_processed(self, block_hash: str, logical_name: str, label: BlockLabel) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_blocks (block_hash, logical_name, label, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (block_hash, logical_name, label, now_iso()),
            )

    def is_block_processed(self, block_hash: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_blocks WHERE block_hash = ?",
                (block_hash,),
            ).fetchone()
        return row is not None

    def retry_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job is None:
            raise ProtocolError(f"unknown job: {job_id}")
        new_job_id = generate_job_id()
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, parent_job_id, worker_name, state, kind, body,
                    route_token, attempt, created_at, updated_at,
                    started_at, finished_at, duration_ms, result_summary, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    new_job_id,
                    job.parent_job_id or job.job_id,
                    job.worker_name,
                    JobState.QUEUED.value,
                    job.kind,
                    job.body,
                    generate_route_token(),
                    job.attempt + 1,
                    timestamp,
                    timestamp,
                ),
            )
        record = self.get_job(new_job_id)
        if record is None:
            raise RuntimeError(f"failed to retry job {job_id}")
        self.event("info", f"retry queued from {job_id}", worker=record.worker_name, job_id=record.job_id)
        return record


def worker_from_row(row: sqlite3.Row) -> WorkerRecord:
    return WorkerRecord(
        name=row_str(row, "name"),
        pane_id=row_opt_str(row, "pane_id"),
        pane_log=row_opt_str(row, "pane_log"),
        state=parse_worker_state(row_str(row, "state")),
        current_job_id=row_opt_str(row, "current_job_id"),
        command=row_opt_str(row, "command"),
        last_seen_at=row_opt_str(row, "last_seen_at"),
        last_error=row_opt_str(row, "last_error"),
        recent_until=maybe_float(row_object(row, "recent_until")),
    )


def job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row_str(row, "job_id"),
        parent_job_id=row_opt_str(row, "parent_job_id"),
        worker_name=row_str(row, "worker_name"),
        state=parse_job_state(row_str(row, "state")),
        kind=row_str(row, "kind"),
        body=row_str(row, "body"),
        route_token=row_str(row, "route_token"),
        attempt=row_int(row, "attempt"),
        created_at=row_str(row, "created_at"),
        updated_at=row_str(row, "updated_at"),
        started_at=row_opt_str(row, "started_at"),
        finished_at=row_opt_str(row, "finished_at"),
        duration_ms=row_opt_int(row, "duration_ms"),
        result_summary=row_opt_str(row, "result_summary"),
        last_error=row_opt_str(row, "last_error"),
    )


class Tmux:
    def __init__(self, state_dir: pathlib.Path, store: Store) -> None:
        self.state_dir = state_dir
        self.store = store

    def run(self, args: Sequence[str], *, check: bool = True, stdin_text: str | None = None) -> str:
        command = ["tmux", *args]
        completed = subprocess.run(
            command,
            check=False,
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise TmuxError(f"tmux {' '.join(args)} failed: {stderr}")
        return completed.stdout

    def configure_session(self) -> None:
        commands: tuple[tuple[str, ...], ...] = (
            ("set-option", "-g", "automatic-rename", "off"),
            ("set-option", "-g", "allow-rename", "off"),
            ("set-option", "-g", "pane-border-status", "off"),
            ("set-option", "-gu", "window-status-format"),
            ("set-option", "-gu", "window-status-current-format"),
        )
        for command in commands:
            try:
                self.run(command)
            except TmuxError as exc:
                self.store.event("warning", str(exc))

    def rename_current_to_orchestrator(self) -> None:
        self.run(("rename-window", "orchestrator"))
        self.run(("select-pane", "-T", "orchestrator"))

    def current_session_name(self) -> str | None:
        pane_id = os.environ.get("TMUX_PANE")
        if pane_id:
            output = self.run(("display-message", "-p", "-t", pane_id, "#{session_name}"), check=False)
        else:
            output = self.run(("display-message", "-p", "#{session_name}"), check=False)
        session_name = output.strip().splitlines()
        if not session_name:
            return None
        return session_name[0]

    def list_panes(self) -> list[PaneInfo]:
        fmt = "\t".join(
            (
                "#{session_name}",
                "#{window_index}",
                "#{window_name}",
                "#{pane_index}",
                "#{pane_id}",
                "#{pane_title}",
                "#{pane_current_command}",
                "#{pane_dead}",
            )
        )
        session_name = self.current_session_name()
        if session_name is None:
            output = self.run(("list-panes", "-s", "-F", fmt))
        else:
            output = self.run(("list-panes", "-s", "-t", session_name, "-F", fmt))
        panes: list[PaneInfo] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) != 8:
                continue
            panes.append(
                PaneInfo(
                    session_name=parts[0],
                    window_index=parts[1],
                    window_name=parts[2],
                    pane_index=parts[3],
                    pane_id=parts[4],
                    pane_title=parts[5],
                    pane_current_command=parts[6],
                    pane_dead=parts[7] == "1",
                )
            )
        return panes

    def find_logical_panes(self) -> dict[str, list[PaneInfo]]:
        found: dict[str, list[PaneInfo]] = {}
        for pane in self.list_panes():
            logical_name = pane.logical_name
            if logical_name is None:
                continue
            found.setdefault(logical_name, []).append(pane)
        return found

    def find_pane_by_id(self, pane_id: str) -> PaneInfo | None:
        for pane in self.list_panes():
            if pane.pane_id == pane_id:
                return pane
        return None

    def window_exists(self, name: str) -> bool:
        for pane in self.list_panes():
            if pane.window_name == name or pane.pane_title == name:
                return True
        return False

    def find_default_worker_pane(self, spec: WorkerSpec) -> PaneInfo | None:
        for pane in self.list_panes():
            if pane.pane_title == spec.name or pane.window_name == spec.window_name:
                return pane
        return None

    def create_window(
        self,
        window_name: str,
        command: str,
        *,
        logical_name: str | None = None,
        target_index: int | None = None,
    ) -> None:
        args: list[str] = ["new-window"]
        if target_index is not None:
            args.extend(("-t", f":{target_index}"))
        args.extend(("-n", window_name, command))
        self.run(tuple(args))
        title = logical_name or window_name
        panes = [pane for pane in self.list_panes() if pane.window_name == window_name or pane.pane_title == title]
        for pane in panes:
            self.run(("select-pane", "-t", pane.pane_id, "-T", title), check=False)

    def set_window_status_style(self, pane: PaneInfo, style: str) -> None:
        if style == "":
            self.clear_window_status_style(pane)
            return
        self.run(("set-window-option", "-t", pane.pane_id, "window-status-style", style), check=False)
        self.run(("set-window-option", "-t", pane.pane_id, "window-status-current-style", style), check=False)

    def clear_window_status_style(self, pane: PaneInfo) -> None:
        self.run(("set-window-option", "-u", "-t", pane.pane_id, "window-status-style"), check=False)
        self.run(("set-window-option", "-u", "-t", pane.pane_id, "window-status-current-style"), check=False)

    def set_window_status_style_for_logical_name(self, logical_name: str, style: str) -> None:
        panes = self.find_logical_panes().get(logical_name, [])
        if len(panes) != 1:
            return
        self.set_window_status_style(panes[0], style)

    def cleanup_bootstrap_windows(self) -> None:
        names_to_remove = set(DEPRECATED_DEFAULT_WORKER_NAMES)
        names_to_remove.add("logs")
        targets_to_remove: dict[str, str] = {}
        for pane in self.list_panes():
            logical_name = pane.logical_name
            deprecated_name = logical_name if logical_name in names_to_remove else None
            if pane.window_name in DEPRECATED_SHORT_WORKER_WINDOW_NAMES:
                deprecated_name = pane.window_name
            if deprecated_name is None:
                continue
            target = f"{pane.session_name}:{pane.window_index}"
            targets_to_remove[target] = deprecated_name
        for target, deprecated_name in targets_to_remove.items():
            self.run(("kill-window", "-t", target), check=False)
            self.store.event("info", f"removed deprecated bootstrap window {deprecated_name}")

    def select_logical_pane(self, logical_name: str) -> None:
        panes = self.find_logical_panes().get(logical_name, [])
        if len(panes) != 1:
            self.store.event("warning", f"could not focus {logical_name}: found {len(panes)} panes")
            return
        pane = panes[0]
        self.run(("select-window", "-t", f"{pane.session_name}:{pane.window_index}"), check=False)
        self.run(("select-pane", "-t", pane.pane_id), check=False)

    def ensure_default_worker_windows(self) -> None:
        for spec in DEFAULT_WORKERS:
            existing = self.find_default_worker_pane(spec)
            if existing is not None:
                if existing.window_name != spec.window_name:
                    target = f"{existing.session_name}:{existing.window_index}"
                    self.run(("rename-window", "-t", target, spec.window_name), check=False)
                self.run(("select-pane", "-t", existing.pane_id, "-T", spec.name), check=False)
                continue
            command_name = spec.command[0]
            if shutil.which(command_name) is None:
                error = f"missing wrapper {command_name}; worker cannot start"
                self.store.ensure_missing_default_worker_failed(spec, error)
                message = f"tmux-orchestrator: {error}"
                shell_command = f"printf '%s\\n' {shlex.quote(message)}; exec ${{SHELL:-/bin/bash}}"
                self.create_window(spec.window_name, shell_command, logical_name=spec.name)
                continue
            self.create_window(spec.window_name, "exec " + shlex.join(spec.command), logical_name=spec.name)
            self.store.event("info", "created default worker", worker=spec.name)

    def pipe_pane_to_log(self, pane: PaneInfo, logical_name: str) -> pathlib.Path:
        pane_log = self.state_dir / "logs" / "panes" / f"{safe_log_name(logical_name)}.log"
        pane_log.touch(exist_ok=True)
        command = f"cat >> {shlex.quote(str(pane_log))}"
        self.run(("pipe-pane", "-t", pane.pane_id), check=False)
        self.run(("pipe-pane", "-t", pane.pane_id, command), check=False)
        return pane_log

    def discover(self) -> list[WorkerRecord]:
        logical_panes = self.find_logical_panes()
        workers: list[WorkerRecord] = []
        for logical_name, panes in logical_panes.items():
            if logical_name == "logs":
                continue
            if len(panes) > 1:
                message = f"duplicate tmux panes named {logical_name}; dispatch disabled for this name"
                if logical_name.startswith("worker."):
                    self.store.set_worker_state(
                        logical_name,
                        WorkerState.PROTOCOL_ERROR,
                        current_job_id=None,
                        error=message,
                    )
                    self.store.event("protocol_error", message, worker=logical_name)
                continue
            pane = panes[0]
            self.run(("select-pane", "-t", pane.pane_id, "-T", logical_name), check=False)
            pane_log = self.pipe_pane_to_log(pane, logical_name)
            if logical_name.startswith("worker."):
                worker = self.store.upsert_worker_seen(
                    logical_name,
                    pane.pane_id,
                    pane_log,
                    pane.pane_current_command,
                )
                workers.append(worker)
                self.style_worker(worker)
        self.mark_missing_busy_workers_failed(logical_panes)
        return workers

    def mark_missing_busy_workers_failed(self, logical_panes: Mapping[str, Sequence[PaneInfo]]) -> None:
        for worker in self.store.list_workers():
            if worker.name in logical_panes:
                continue
            if worker.state in (WorkerState.BUSY, WorkerState.WAITING_INPUT):
                message = "worker pane disappeared while job was active"
                self.store.set_worker_state(worker.name, WorkerState.FAILED, current_job_id=None, error=message)
                if worker.current_job_id is not None:
                    self.store.set_job_state(worker.current_job_id, JobState.FAILED, error=message)
                self.store.event("error", message, worker=worker.name, job_id=worker.current_job_id)

    def paste_text(self, pane_id: str, text: str) -> None:
        buffer_name = f"tmux-orchestrator-{secrets.token_hex(4)}"
        self.run(("load-buffer", "-b", buffer_name, "-"), stdin_text=text)
        self.run(("paste-buffer", "-b", buffer_name, "-t", pane_id, "-d"))

    def submit_text(
        self,
        pane_id: str,
        text: str,
        *,
        enter_delay_seconds: float = DEFAULT_SUBMIT_ENTER_DELAY_SECONDS,
        enter_count: int = DEFAULT_SUBMIT_ENTER_COUNT,
    ) -> None:
        self.paste_text(pane_id, text)
        time.sleep(enter_delay_seconds)
        for enter_index in range(max(1, enter_count)):
            if enter_index > 0:
                time.sleep(CODEX_SUBMIT_EXTRA_ENTER_DELAY_SECONDS)
            self.run(("send-keys", "-t", pane_id, "C-m"))

    def style_worker(self, worker: WorkerRecord) -> None:
        if worker.pane_id is None:
            return
        style = PANE_STYLE_BY_STATE[worker.state]
        window_style = WINDOW_STYLE_BY_STATE[worker.state]
        self.run(("select-pane", "-t", worker.pane_id, "-P", style), check=False)
        self.run(("select-pane", "-t", worker.pane_id, "-T", worker.name), check=False)
        pane = self.find_pane_by_id(worker.pane_id)
        if pane is not None:
            self.set_window_status_style(pane, window_style)

    def notify_orchestrator(self, text: str) -> None:
        panes = self.find_logical_panes().get("orchestrator", [])
        self.run(("display-message", text), check=False)
        if len(panes) != 1:
            return
        notification = f"tmux-orchestrator: {text}\n"
        try:
            self.paste_text(panes[0].pane_id, notification)
        except TmuxError as exc:
            self.store.event("warning", f"could not notify orchestrator pane: {exc}")


def parse_message_blocks(text: str) -> list[MessageBlock]:
    lines = text.splitlines()
    blocks: list[MessageBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        start = parse_block_start(line)
        if start is None:
            index += 1
            continue
        target, label = start
        end_index = find_block_end(lines, index + 1, label)
        if end_index is None:
            break
        block_lines = lines[index + 1 : end_index]
        raw = "\n".join(lines[index : end_index + 1])
        headers, body = parse_block_payload(block_lines)
        blocks.append(MessageBlock(target=target, label=label, headers=headers, body=body, raw=raw))
        index = end_index + 1
    return blocks


def parse_block_start(line: str) -> tuple[str, BlockLabel] | None:
    if not line.startswith("@") or " <<'" not in line or not line.endswith("'"):
        return None
    target_part, label_part = line[1:].split(" <<'", 1)
    label = label_part[:-1]
    if label not in ("TASK", "RESULT"):
        return None
    if not target_part or any(ch.isspace() for ch in target_part):
        return None
    return target_part, label


def find_block_end(lines: Sequence[str], start_index: int, label: BlockLabel) -> int | None:
    for index in range(start_index, len(lines)):
        if lines[index] == label:
            return index
    return None


def parse_block_payload(lines: Sequence[str]) -> tuple[Mapping[str, str], str]:
    headers: dict[str, str] = {}
    body_start = len(lines)
    for index, line in enumerate(lines):
        if line == "---":
            body_start = index + 1
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    body = "\n".join(lines[body_start:])
    return headers, body


def block_hash(logical_name: str, block: MessageBlock) -> str:
    digest = hashlib.sha256()
    digest.update(logical_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(block.raw.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def submit_delay_for_worker(worker_name: str) -> float:
    return DEFAULT_WORKER_SUBMIT_DELAY_BY_NAME.get(worker_name, DEFAULT_SUBMIT_ENTER_DELAY_SECONDS)


def submit_enter_count_for_worker(worker_name: str) -> int:
    return DEFAULT_WORKER_SUBMIT_ENTER_COUNT_BY_NAME.get(worker_name, DEFAULT_SUBMIT_ENTER_COUNT)


def render_worker_prompt(job: JobRecord) -> str:
    task_block = textwrap.dedent(
        f"""\
        @${job.worker_name} <<'TASK'
        job_id: {job.job_id}
        route_token: {job.route_token}
        from: orchestrator
        kind: {job.kind}
        ---
        {job.body}
        TASK
        """
    ).replace("@$", "@").strip()
    return "\n".join(
        (
            f"You are {job.worker_name}. You can only answer the orchestrator. Do not message other workers.",
            "",
            "Process this task:",
            "",
            task_block,
            "",
            "Your job is not complete until you notify tmux-orchestrator that this job is complete.",
            "The router uses that notification to mark you idle and to unblock queued work.",
            "",
            "When finished, run exactly one completion command:",
            f'  tmux-orchestrator complete {job.job_id} --route-token {job.route_token} --status done --summary "Brief result for the human."',
            "",
            "If you need human input, run:",
            f'  tmux-orchestrator complete {job.job_id} --route-token {job.route_token} --status waiting_input --summary "What you need from the human."',
            "",
            "If the job failed, run:",
            f'  tmux-orchestrator complete {job.job_id} --route-token {job.route_token} --status failed --summary "Why it failed."',
            "",
            "After running the completion command, do not also print a RESULT block.",
            "",
            "Fallback only if you cannot run shell commands: print a RESULT block with protocol, job_id, route_token, from, status, kind, a --- line, a short summary, and a final RESULT line.",
        )
    )


def dispatch_queued_jobs(store: Store, transport: Transport) -> None:
    queued = store.list_jobs(states=(JobState.QUEUED,))
    for job in queued:
        worker = store.get_worker(job.worker_name)
        if worker is None:
            store.set_job_state(job.job_id, JobState.FAILED, error="unknown worker")
            store.event("error", "unknown worker", worker=job.worker_name, job_id=job.job_id)
            continue
        if worker.state != WorkerState.IDLE:
            continue
        if worker.pane_id is None:
            store.set_job_state(job.job_id, JobState.FAILED, error="worker has no pane")
            store.set_worker_state(worker.name, WorkerState.FAILED, current_job_id=None, error="worker has no pane")
            store.event("error", "worker has no pane", worker=worker.name, job_id=job.job_id)
            continue
        payload = render_worker_prompt(job)
        store.set_job_state(job.job_id, JobState.RUNNING)
        store.set_worker_state(worker.name, WorkerState.BUSY, current_job_id=job.job_id)
        updated = store.get_worker(worker.name)
        if updated is not None:
            transport.style_worker(updated)
        try:
            transport.submit_text(
                worker.pane_id,
                payload,
                enter_delay_seconds=submit_delay_for_worker(worker.name),
                enter_count=submit_enter_count_for_worker(worker.name),
            )
        except TmuxError as exc:
            message = f"dispatch submit failed: {exc}"
            store.set_job_state(job.job_id, JobState.FAILED, error=message)
            store.set_worker_state(worker.name, WorkerState.FAILED, current_job_id=None, error=message)
            failed_worker = store.get_worker(worker.name)
            if failed_worker is not None:
                transport.style_worker(failed_worker)
            store.event("error", message, worker=worker.name, job_id=job.job_id)
            continue
        store.event("info", "dispatched", worker=worker.name, job_id=job.job_id)


def handle_result_block(store: Store, transport: Transport, worker_name: str, block: MessageBlock) -> None:
    if block.target != "orchestrator":
        protocol_error(store, transport, worker_name, None, f"worker attempted to target {block.target}")
        return
    job_id = block.headers.get("job_id")
    if job_id is None or job_id == "":
        protocol_error(store, transport, worker_name, None, "RESULT missing job_id")
        return
    job = store.get_job(job_id)
    if job is None:
        protocol_error(store, transport, worker_name, job_id, "RESULT references unknown job")
        return
    if block.headers.get("protocol") != PROTOCOL_VERSION:
        protocol_error(store, transport, worker_name, job_id, "RESULT missing or invalid protocol header")
        return
    from_worker = block.headers.get("from")
    if from_worker != worker_name or from_worker != job.worker_name:
        protocol_error(store, transport, worker_name, job_id, f"RESULT from mismatch: {from_worker}")
        return
    worker = store.get_worker(worker_name)
    if job.state not in (JobState.RUNNING, JobState.WAITING_INPUT):
        store.event("warning", f"ignored RESULT for non-active job state={job.state.value}", worker=worker_name, job_id=job.job_id)
        return
    if worker is None or worker.current_job_id != job.job_id:
        protocol_error(store, transport, worker_name, job_id, "RESULT does not match worker active job")
        return
    route_token = block.headers.get("route_token")
    if route_token != job.route_token:
        protocol_error(store, transport, worker_name, job_id, "RESULT route_token mismatch")
        return
    status = block.headers.get("status")
    if status not in ("done", "waiting_input", "failed"):
        protocol_error(store, transport, worker_name, job.job_id, f"unknown RESULT status: {status}")
        return
    apply_worker_result(store, transport, worker_name, job, status, block.body)


def apply_worker_result(
    store: Store,
    transport: Transport,
    worker_name: str,
    job: JobRecord,
    status: ResultStatusValue,
    summary: str,
) -> None:
    if status == "done":
        store.set_job_state(job.job_id, JobState.DONE, result_summary=summarize_body(summary, "done"))
        store.set_worker_state(worker_name, WorkerState.IDLE, current_job_id=None)
        completed_job = store.get_job(job.job_id)
        duration = job_duration_label(completed_job) if completed_job is not None else "-"
        store.event("success", f"{summarize_body(summary, 'done')} duration={duration}", worker=worker_name, job_id=job.job_id)
    elif status == "waiting_input":
        store.set_job_state(job.job_id, JobState.WAITING_INPUT, result_summary=summarize_body(summary, "waiting for human input"))
        store.set_worker_state(worker_name, WorkerState.WAITING_INPUT, current_job_id=job.job_id)
        waiting_job = store.get_job(job.job_id)
        elapsed = job_duration_label(waiting_job) if waiting_job is not None else "-"
        store.event("warning", f"{summarize_body(summary, 'waiting for human input')} elapsed={elapsed}", worker=worker_name, job_id=job.job_id)
    else:
        message = summarize_body(summary, "worker reported failure")
        store.set_job_state(job.job_id, JobState.FAILED, error=message, result_summary=message)
        store.set_worker_state(worker_name, WorkerState.FAILED, current_job_id=None, error=message)
        failed_job = store.get_job(job.job_id)
        duration = job_duration_label(failed_job) if failed_job is not None else "-"
        store.event("error", f"{message} duration={duration}", worker=worker_name, job_id=job.job_id)
    worker = store.get_worker(worker_name)
    if worker is not None:
        transport.style_worker(worker)


def protocol_error(store: Store, transport: Transport, worker_name: str, job_id: str | None, message: str) -> None:
    store.set_worker_state(worker_name, WorkerState.PROTOCOL_ERROR, current_job_id=None, error=message)
    if job_id is not None:
        store.set_job_state(job_id, JobState.PROTOCOL_ERROR, error=message)
    errored_job = store.get_job(job_id) if job_id is not None else None
    duration = job_duration_label(errored_job) if errored_job is not None else "-"
    store.event("protocol_error", f"{message} duration={duration}", worker=worker_name, job_id=job_id)
    worker = store.get_worker(worker_name)
    if worker is not None:
        transport.style_worker(worker)
    transport.notify_orchestrator(f"protocol error from {worker_name}: {message}")


def summarize_body(body: str, fallback: str) -> str:
    clean = " ".join(line.strip() for line in body.splitlines() if line.strip())
    if not clean:
        return fallback
    if len(clean) <= 180:
        return clean
    return clean[:177] + "..."


def handle_task_block_from_orchestrator(store: Store, block: MessageBlock) -> None:
    if not block.target.startswith("worker."):
        store.event("protocol_error", f"orchestrator TASK target is not a worker: {block.target}")
        return
    if block.headers.get("from") != "orchestrator":
        store.event("protocol_error", "orchestrator TASK missing from: orchestrator")
        return
    job_id = block.headers.get("job_id")
    if job_id is None or job_id == "":
        store.event("protocol_error", "orchestrator TASK missing job_id")
        return
    existing = store.get_job(job_id)
    if existing is not None:
        return
    kind = block.headers.get("kind", "task")
    route_token = block.headers.get("route_token") or generate_route_token()
    store.create_job(
        worker_name=block.target,
        body=block.body,
        kind=kind,
        job_id=job_id,
        route_token=route_token,
    )


def process_pane_logs(store: Store, transport: Transport) -> None:
    workers = store.list_workers()
    for worker in workers:
        if worker.pane_log is None:
            continue
        process_single_log(store, transport, worker.name, pathlib.Path(worker.pane_log), "worker")
    orchestrator_log = store.state_dir / "logs" / "panes" / "orchestrator.log"
    if orchestrator_log.exists():
        process_single_log(store, transport, "orchestrator", orchestrator_log, "orchestrator")


def process_single_log(
    store: Store,
    transport: Transport,
    logical_name: str,
    path: pathlib.Path,
    role: Literal["worker", "orchestrator"],
) -> None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        store.event("warning", f"could not read pane log {path}: {exc}")
        return
    for block in parse_message_blocks(content):
        if role == "worker" and block.label != "RESULT":
            continue
        if role == "orchestrator" and block.label != "TASK":
            continue
        digest = block_hash(logical_name, block)
        if store.is_block_processed(digest):
            continue
        store.mark_block_processed(digest, logical_name, block.label)
        if role == "worker":
            handle_result_block(store, transport, logical_name, block)
        else:
            handle_task_block_from_orchestrator(store, block)


def pick_broadcast_workers(store: Store, target: str) -> list[WorkerRecord]:
    workers = store.list_workers()
    if target == "all":
        return [worker for worker in workers if worker.state != WorkerState.PROTOCOL_ERROR]
    if target == "idle":
        return [worker for worker in workers if worker.state == WorkerState.IDLE]
    wanted = {name.strip() for name in target.split(",") if name.strip()}
    return [worker for worker in workers if worker.name in wanted]


def mark_job_done(store: Store, transport: Transport, worker_name: str, job_id: str) -> None:
    job = store.get_job(job_id)
    if job is None:
        raise ProtocolError(f"unknown job: {job_id}")
    if job.worker_name != worker_name:
        raise ProtocolError(f"job {job_id} belongs to {job.worker_name}, not {worker_name}")
    store.set_job_state(job_id, JobState.DONE)
    store.set_worker_state(worker_name, WorkerState.IDLE, current_job_id=None)
    worker = store.get_worker(worker_name)
    if worker is not None:
        transport.style_worker(worker)
    completed_job = store.get_job(job_id)
    duration = job_duration_label(completed_job) if completed_job is not None else "-"
    store.event("success", f"marked done manually duration={duration}", worker=worker_name, job_id=job_id)


def mark_job_failed(store: Store, transport: Transport, worker_name: str, job_id: str, message: str) -> None:
    job = store.get_job(job_id)
    if job is None:
        raise ProtocolError(f"unknown job: {job_id}")
    if job.worker_name != worker_name:
        raise ProtocolError(f"job {job_id} belongs to {job.worker_name}, not {worker_name}")
    store.set_job_state(job_id, JobState.FAILED, error=message)
    store.set_worker_state(worker_name, WorkerState.FAILED, current_job_id=None, error=message)
    worker = store.get_worker(worker_name)
    if worker is not None:
        transport.style_worker(worker)
    failed_job = store.get_job(job_id)
    duration = job_duration_label(failed_job) if failed_job is not None else "-"
    store.event("error", f"{message} duration={duration}", worker=worker_name, job_id=job_id)


def job_state_for_result_status(status: ResultStatusValue) -> JobState:
    if status == "done":
        return JobState.DONE
    if status == "waiting_input":
        return JobState.WAITING_INPUT
    return JobState.FAILED


def complete_active_job(
    store: Store,
    transport: Transport,
    job_id: str,
    status: ResultStatusValue,
    summary: str,
    route_token: str,
) -> JobRecord:
    job = store.get_job(job_id)
    if job is None:
        raise ProtocolError(f"unknown job: {job_id}")
    if route_token != job.route_token:
        raise ProtocolError(f"route_token mismatch for job {job_id}")
    if job.state in (JobState.DONE, JobState.FAILED, JobState.PROTOCOL_ERROR):
        requested_state = job_state_for_result_status(status)
        if job.state != requested_state:
            raise ProtocolError(f"job {job_id} is already {job.state.value}, cannot complete as {status}")
        return job
    worker = store.get_worker(job.worker_name)
    if worker is None:
        raise ProtocolError(f"unknown worker for job {job_id}: {job.worker_name}")
    if worker.current_job_id != job.job_id:
        raise ProtocolError(f"job {job_id} is not active on {job.worker_name}")
    if job.state not in (JobState.RUNNING, JobState.WAITING_INPUT):
        raise ProtocolError(f"job {job_id} is not running: {job.state.value}")
    apply_worker_result(store, transport, job.worker_name, job, status, summary)
    updated = store.get_job(job.job_id)
    if updated is None:
        raise RuntimeError(f"completed job disappeared: {job.job_id}")
    return updated


def jobs_for_wait_identifier(store: Store, identifier: str) -> list[JobRecord]:
    child_jobs = store.list_jobs_by_parent(identifier)
    if child_jobs:
        return child_jobs
    job = store.get_job(identifier)
    if job is not None:
        return [job]
    raise ProtocolError(f"unknown job or parent_job_id: {identifier}")


def wait_result_code(jobs: Sequence[JobRecord]) -> WaitResultCode | None:
    if any(job.state == JobState.PROTOCOL_ERROR for job in jobs):
        return 1
    if any(job.state == JobState.FAILED for job in jobs):
        return 1
    if any(job.state == JobState.WAITING_INPUT for job in jobs):
        return 2
    if all(job.state == JobState.DONE for job in jobs):
        return 0
    return None


def wait_job_line(job: JobRecord) -> str:
    duration = job_duration_label(job)
    detail = job.result_summary or job.last_error
    suffix = f" summary={detail}" if detail else ""
    return f"{job.worker_name} {job.job_id} {job.state.value} duration={duration}{suffix}"


def wait_job_fingerprint(job: JobRecord) -> str:
    return "|".join(
        (
            job.state.value,
            str(job.duration_ms or ""),
            job.result_summary or "",
            job.last_error or "",
        )
    )


def print_wait_summary(jobs: Sequence[JobRecord]) -> None:
    print("Wait summary", flush=True)
    for job in jobs:
        print("  " + wait_job_line(job), flush=True)


def wait_command(
    state_dir: pathlib.Path,
    identifier: str,
    *,
    poll_interval: float,
    timeout_seconds: float,
    watch: bool,
) -> int:
    store = Store(state_dir)
    store.init_schema()
    started = time.monotonic()
    previous: dict[str, str] = {}
    announced = False
    while True:
        jobs = jobs_for_wait_identifier(store, identifier)
        if watch and not announced:
            print(f"Waiting for {identifier} ({len(jobs)} job(s))", flush=True)
            announced = True
        for job in jobs:
            fingerprint = wait_job_fingerprint(job)
            if watch and previous.get(job.job_id) != fingerprint:
                print(wait_job_line(job), flush=True)
            previous[job.job_id] = fingerprint
        result_code = wait_result_code(jobs)
        if result_code is not None:
            print_wait_summary(jobs)
            return result_code
        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            print_wait_summary(jobs)
            print(f"tmux-orchestrator: wait timed out after {format_duration_ms(int(timeout_seconds * 1000))}", file=sys.stderr)
            return 124
        time.sleep(poll_interval)


def run_daemon(state_dir: pathlib.Path, poll_interval: float) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    store.event("info", "router daemon started")
    while True:
        try:
            tmux.discover()
            process_pane_logs(store, tmux)
            dispatch_queued_jobs(store, tmux)
        except TmuxError as exc:
            store.event("error", f"tmux error in daemon: {exc}")
        except sqlite3.Error as exc:
            store.event("error", f"sqlite error in daemon: {exc}")
        time.sleep(poll_interval)


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(path: pathlib.Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def stop_daemon_if_running(state_dir: pathlib.Path) -> None:
    pid_path = state_dir / "router.pid"
    current_pid = read_pid(pid_path)
    if current_pid is not None and pid_is_alive(current_pid):
        try:
            os.kill(current_pid, 15)
        except OSError:
            pass
        for _ in range(20):
            if not pid_is_alive(current_pid):
                break
            time.sleep(0.05)
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def executable_invocation(value: str) -> str | None:
    if value in ("", "-c", "-m"):
        return None
    if os.sep in value:
        return value
    return shutil.which(value)


def daemon_entrypoint_argv() -> list[str]:
    candidates = (
        os.environ.get("TMUX_ORCHESTRATOR_ENTRYPOINT"),
        sys.argv[0],
    )
    for candidate in candidates:
        if candidate is None:
            continue
        invocation = executable_invocation(candidate)
        if invocation is not None:
            return [invocation]
    script_path = pathlib.Path(__file__)
    if os.access(script_path, os.X_OK):
        return [str(script_path)]
    return [sys.executable, str(script_path)]


def start_daemon_if_needed(state_dir: pathlib.Path) -> None:
    pid_path = state_dir / "router.pid"
    current_pid = read_pid(pid_path)
    if current_pid is not None and pid_is_alive(current_pid):
        return
    daemon_log_path = state_dir / "logs" / "daemon.log"
    daemon_log = daemon_log_path.open("ab")
    command = daemon_command_argv(state_dir)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=daemon_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_path.write_text(str(process.pid), encoding="utf-8")


def daemon_command_argv(state_dir: pathlib.Path) -> list[str]:
    return [*daemon_entrypoint_argv(), "--state-dir", str(state_dir), "daemon"]


def choose_orchestrator_command(option: str | None) -> tuple[str, ...]:
    if option is not None:
        return parse_orchestrator_option(option)
    return parse_orchestrator_option("codex")


def parse_orchestrator_option(option: str) -> tuple[str, ...]:
    if option == "codex":
        return ("codex-yolo", "--no-alt-screen", ORCHESTRATOR_BOOTSTRAP_PROMPT)
    if option == "claude":
        return ("claude-yolo", ORCHESTRATOR_BOOTSTRAP_PROMPT)
    if option == "qwen":
        return ("qwen-yolo", "--prompt-interactive", ORCHESTRATOR_BOOTSTRAP_PROMPT)
    if option == "gemini":
        return ("gemini-yolo", ORCHESTRATOR_BOOTSTRAP_PROMPT)
    if option == "shell":
        return (os.environ.get("SHELL") or "/bin/bash",)
    if option.startswith("custom:"):
        command = shlex.split(option.removeprefix("custom:"))
        if not command:
            raise ProtocolError("custom orchestrator command is empty")
        return tuple(command)
    raise ProtocolError(f"unknown orchestrator option: {option}")


def exec_orchestrator(command: Sequence[str]) -> None:
    if not command:
        raise ProtocolError("empty orchestrator command")
    executable = shutil.which(command[0])
    if executable is None:
        raise ProtocolError(f"missing orchestrator command: {command[0]}")
    os.execvp(executable, [executable, *command[1:]])


def run_setup_and_exec(state_dir: pathlib.Path, orchestrator_option: str | None) -> int:
    ensure_state_dirs(state_dir)
    store = Store(state_dir)
    store.init_schema()
    stop_daemon_if_running(state_dir)
    store.reset_runtime_state()
    tmux = Tmux(state_dir, store)
    orchestrator_command = choose_orchestrator_command(orchestrator_option)
    tmux.configure_session()
    tmux.rename_current_to_orchestrator()
    tmux.cleanup_bootstrap_windows()
    tmux.ensure_default_worker_windows()
    tmux.discover()
    start_daemon_if_needed(state_dir)
    tmux.select_logical_pane("orchestrator")
    store.event("info", "launching orchestrator CLI: " + shlex.join(orchestrator_command))
    exec_orchestrator(orchestrator_command)
    return 0


def print_status(store: Store) -> None:
    workers = store.list_workers()
    jobs = store.list_jobs()
    print("Workers")
    if not workers:
        print("  none")
    for worker in workers:
        current = worker.current_job_id or "-"
        pane = worker.pane_id or "-"
        error = f" error={worker.last_error}" if worker.last_error else ""
        print(f"  {worker.name:<26} {worker.state.value:<15} pane={pane:<6} job={current}{error}")
    print("")
    print("Jobs")
    if not jobs:
        print("  none")
    for job in jobs:
        parent = job.parent_job_id or "-"
        duration = job_duration_label(job)
        error = f" error={job.last_error}" if job.last_error else ""
        summary = f" summary={job.result_summary}" if job.result_summary else ""
        print(
            f"  {job.job_id:<18} {job.state.value:<15} worker={job.worker_name:<26} "
            f"duration={duration:<12} parent={parent} kind={job.kind}{summary}{error}"
        )


def init_command(state_dir: pathlib.Path) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    tmux.configure_session()
    tmux.ensure_default_worker_windows()
    tmux.discover()
    dispatch_queued_jobs(store, tmux)
    store.event("info", "init complete")
    return 0


def discover_command(state_dir: pathlib.Path) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    workers = tmux.discover()
    for worker in workers:
        print(f"{worker.name}\t{worker.state.value}\t{worker.pane_id or '-'}")
    return 0


def send_command(state_dir: pathlib.Path, worker_name: str, message: str, kind: str) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    tmux.discover()
    job = store.create_job(worker_name=worker_name, body=message, kind=kind)
    dispatch_queued_jobs(store, tmux)
    print(job.job_id)
    return 0


def broadcast_command(state_dir: pathlib.Path, target: str, message: str, kind: str) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    tmux.discover()
    parent_job_id = create_broadcast_jobs(store, target, message, kind)
    dispatch_queued_jobs(store, tmux)
    print(parent_job_id)
    return 0


def create_broadcast_jobs(store: Store, target: str, message: str, kind: str) -> str:
    workers = pick_broadcast_workers(store, target)
    if not workers:
        raise ProtocolError(f"broadcast target selected no workers: {target}")
    parent_job_id = generate_parent_job_id()
    for worker in workers:
        store.create_job(
            worker_name=worker.name,
            body=message,
            kind=kind,
            parent_job_id=parent_job_id,
        )
    return parent_job_id


def manual_done_command(state_dir: pathlib.Path, worker_name: str, job_id: str) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    mark_job_done(store, tmux, worker_name, job_id)
    dispatch_queued_jobs(store, tmux)
    return 0


def manual_fail_command(state_dir: pathlib.Path, worker_name: str, job_id: str, message: str) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    mark_job_failed(store, tmux, worker_name, job_id, message)
    return 0


def retry_command(state_dir: pathlib.Path, job_id: str) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    job = store.retry_job(job_id)
    dispatch_queued_jobs(store, tmux)
    print(job.job_id)
    return 0


def complete_command(state_dir: pathlib.Path, job_id: str, status: ResultStatusValue, summary: str, route_token: str) -> int:
    store = Store(state_dir)
    store.init_schema()
    tmux = Tmux(state_dir, store)
    job = complete_active_job(store, tmux, job_id, status, summary, route_token=route_token)
    print(f"{job.job_id} {job.state.value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmux-orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Coordinate visible interactive AI CLI workers through tmux.\n"
            "The default run mode starts Codex as the human-facing orchestrator, "
            "plus the default worker windows."
        ),
        epilog=textwrap.dedent(
            """\
            Typical launch:
              sandbox-agent . -- tmux-orchestrator run

            Orchestrator choices:
              tmux-orchestrator run                         # Codex orchestrator (default)
              tmux-orchestrator run --orchestrator codex    # explicit Codex
              tmux-orchestrator run --orchestrator claude   # Claude Code orchestrator
              tmux-orchestrator run --orchestrator qwen     # Qwen orchestrator
              tmux-orchestrator run --orchestrator gemini   # Gemini orchestrator
              tmux-orchestrator run --orchestrator shell    # shell-only smoke/debug mode
              tmux-orchestrator run --orchestrator 'custom:my-cli --flag'

            Common workflow inside the orchestrator pane:
              tmux-orchestrator status
              tmux-orchestrator send worker.claude "Review this diff"
              tmux-orchestrator broadcast --to idle "Give me a design critique"
              tmux-orchestrator wait parent_abc123 --watch
              tmux-orchestrator complete job_abc123 --route-token rt_abc123 --status done --summary "Worker finished"

            State and logs:
              .tmux-orchestrator/state.sqlite3
              .tmux-orchestrator/logs/events.log
              .tmux-orchestrator/logs/panes/
            """
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=str(default_state_dir()),
        help="state directory relative to the current project (default: .tmux-orchestrator)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="configure tmux, create default workers, discover panes, and dispatch queued jobs")
    subparsers.add_parser("discover", help="discover panes named worker.* and update router state")
    subparsers.add_parser("status", help="print workers and jobs from SQLite state")

    send_parser = subparsers.add_parser(
        "send",
        help="queue a task for one worker",
        description="Queue a task for one worker. If the worker is idle, the router dispatches it immediately.",
    )
    send_parser.add_argument("worker")
    send_parser.add_argument("message", nargs="+")
    send_parser.add_argument("--kind", default="task")

    broadcast_parser = subparsers.add_parser(
        "broadcast",
        help="queue one task per selected worker",
        description="Broadcast creates a parent job and one sub-job per selected worker.",
    )
    broadcast_parser.add_argument("message", nargs="+")
    broadcast_parser.add_argument("--to", default="idle", help="all, idle, or comma-separated worker names")
    broadcast_parser.add_argument("--kind", default="task")

    wait_parser = subparsers.add_parser(
        "wait",
        help="wait locally for one job or a broadcast parent job",
        description=(
            "Poll SQLite locally until a job or all sub-jobs of a parent_job_id finish. "
            "This does not read tmux output and does not call any LLM."
        ),
    )
    wait_parser.add_argument("identifier", help="job_id or parent_job_id")
    wait_parser.add_argument("--watch", action="store_true", help="print only state changes while waiting")
    wait_parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    wait_parser.add_argument("--timeout", type=float, default=0.0, help="seconds before returning 124; 0 means no timeout")

    run_parser = subparsers.add_parser(
        "run",
        help="start the full tmux orchestrator session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Set up the current tmux session, reset runtime state, start the router daemon, "
            "create default workers, and exec the human-facing orchestrator CLI."
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              sandbox-agent . -- tmux-orchestrator run
              sandbox-agent . -- tmux-orchestrator run --orchestrator claude
              sandbox-agent . -- tmux-orchestrator run --orchestrator qwen
              sandbox-agent . -- tmux-orchestrator run --orchestrator gemini
              sandbox-agent . -- tmux-orchestrator run --orchestrator shell
              sandbox-agent . -- tmux-orchestrator run --orchestrator 'custom:codex-yolo --no-alt-screen'

            Default workers:
              worker.claude   (tmux window: worker.claude)
              worker.codex    (tmux window: worker.codex)
              worker.qwen     (tmux window: worker.qwen)
              worker.gemini   (tmux window: worker.gemini)
            """
        ),
    )
    run_parser.add_argument(
        "--orchestrator",
        default=None,
        help="codex, claude, qwen, gemini, shell, or custom:<cmd>; default: codex",
    )

    daemon_parser = subparsers.add_parser("daemon", help="internal router loop; normally started by run")
    daemon_parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)

    done_parser = subparsers.add_parser("mark-done", help="manually mark a worker job done")
    done_parser.add_argument("worker")
    done_parser.add_argument("job_id")

    fail_parser = subparsers.add_parser("fail", help="manually mark a worker job failed")
    fail_parser.add_argument("worker")
    fail_parser.add_argument("job_id")
    fail_parser.add_argument("message", nargs="*", default=["failed manually"])

    retry_parser = subparsers.add_parser("retry", help="queue a retry of an existing job")
    retry_parser.add_argument("job_id")

    complete_parser = subparsers.add_parser(
        "complete",
        help="worker-facing command to complete the active job",
        description=(
            "Mark an active job as done, waiting_input, or failed. "
            "Workers should prefer this over free-form protocol text when they can run shell commands."
        ),
    )
    complete_parser.add_argument("job_id")
    complete_parser.add_argument("--route-token", required=True, help="job route token")
    complete_parser.add_argument("--status", choices=("done", "waiting_input", "failed"), default="done")
    complete_parser.add_argument("--summary", nargs="+", default=["completed"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_dir = pathlib.Path(cast(str, args.state_dir))
    command = cast(str, args.command)
    try:
        if command == "init":
            return init_command(state_dir)
        if command == "discover":
            return discover_command(state_dir)
        if command == "status":
            store = Store(state_dir)
            store.init_schema()
            print_status(store)
            return 0
        if command == "send":
            message = " ".join(cast(list[str], args.message))
            return send_command(state_dir, cast(str, args.worker), message, cast(str, args.kind))
        if command == "broadcast":
            message = " ".join(cast(list[str], args.message))
            return broadcast_command(state_dir, cast(str, args.to), message, cast(str, args.kind))
        if command == "wait":
            return wait_command(
                state_dir,
                cast(str, args.identifier),
                poll_interval=cast(float, args.poll_interval),
                timeout_seconds=cast(float, args.timeout),
                watch=cast(bool, args.watch),
            )
        if command == "run":
            return run_setup_and_exec(state_dir, cast(str | None, args.orchestrator))
        if command == "daemon":
            return run_daemon(state_dir, cast(float, args.poll_interval))
        if command == "mark-done":
            return manual_done_command(state_dir, cast(str, args.worker), cast(str, args.job_id))
        if command == "fail":
            message = " ".join(cast(list[str], args.message))
            return manual_fail_command(state_dir, cast(str, args.worker), cast(str, args.job_id), message)
        if command == "retry":
            return retry_command(state_dir, cast(str, args.job_id))
        if command == "complete":
            summary = " ".join(cast(list[str], args.summary))
            return complete_command(
                state_dir,
                cast(str, args.job_id),
                cast(ResultStatusValue, args.status),
                summary,
                cast(str, args.route_token),
            )
    except (ProtocolError, TmuxError, sqlite3.Error) as exc:
        print(f"tmux-orchestrator: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
