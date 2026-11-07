"""Measure observer SQL overhead against synthetic diagnostics in disposable PostgreSQL."""

# ruff: noqa: INP001, T201

import json
import os
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import UUID

import jobs_observer as observer
import psycopg
from psycopg.types.json import Jsonb


def create_tables(connection) -> None:
    """Create only the columns used by the observer, in an empty control database."""
    connection.execute("""
        CREATE TABLE documents_documentjob (
            id uuid PRIMARY KEY, kind text, status text, created timestamptz,
            updated timestamptz, attempts integer, timeout integer, cleaned boolean,
            error text, input jsonb, result jsonb
        )
    """)
    connection.execute("""
        CREATE TABLE documents_paperlesstask (
            id bigint PRIMARY KEY, task_id text UNIQUE, task_type text,
            trigger_source text, status text, date_created timestamptz,
            date_started timestamptz, date_done timestamptz
        )
    """)


def seed(connection) -> None:
    """Insert 13 jobs, 100 invocations, 1,209 dependencies and 16 task rows."""
    now = datetime.now(UTC)
    entry_number = 0
    for number in range(13):
        entries = {}
        for _ in range(8 if number < 9 else 7):
            entries[f"operation-{entry_number}"] = {
                "id": str(UUID(int=100 + entry_number)),
                "operation": "parser.process",
                "plugin": "synthetic-parser:1",
                "version": 2,
                "status": "succeeded",
                "attempts": 1,
                "replay_safe": True,
                "remote_allowed": True,
                "policy": {"permission": "documents.process_parser"},
                "dependencies": [
                    f"dependency-{index}"
                    for index in range(entry_number % 25 + (entry_number < 9))
                ],
            }
            entry_number += 1
        connection.execute(
            "INSERT INTO documents_documentjob VALUES (%s, %s, %s, %s, %s, 1, 1800, false, '', %s, %s)",
            (
                UUID(int=number + 1),
                "consume",
                "succeeded",
                now,
                now,
                Jsonb({"document_id": number + 1}),
                Jsonb({"phase": "complete", "invocations": entries}),
            ),
        )
    for number in range(16):
        connection.execute(
            "INSERT INTO documents_paperlesstask VALUES (%s, %s, 'consume_file', 'manual', 'success', %s, %s, %s)",
            (number, f"task-{number}", now, now, now),
        )


def backend_cpu(pid: int) -> float:
    """Read this SQL backend's CPU when sharing the PostgreSQL PID namespace."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def measure(connection, mode: str, seconds: float, pid: int) -> dict:
    """Run one matched 10 Hz window, excluding initialization and warmup."""
    full_query = (
        "SELECT clock_timestamp(), statement_timestamp(), ("
        + observer.JOBS_QUERY
        + "), ("
        + observer.TASKS_QUERY
        + ")"
    )
    parameters = (
        [*observer.RESULT_FIELDS, "followup"],
        [*observer.ENTRY_FIELDS, "policy", "dependencies", "error"],
    )
    versions = {}
    task_versions = {}

    def poll() -> tuple[list, list]:
        """Exercise the old projection or new unchanged-row path with identical data."""
        nonlocal versions, task_versions
        if mode == "full":
            _, _, jobs, tasks = connection.execute(full_query, parameters).fetchone()
        else:
            _, _, versions, jobs, task_versions, tasks = observer.poll(
                connection,
                has_jobs=True,
                jobs=versions,
                tasks=task_versions,
            )
        for row in jobs:
            observer.project(row)
        for row in tasks:
            observer.scalar_fields(row, observer.TASK_FIELDS)
        return jobs, tasks

    for _ in range(6):
        poll()
    queries = 0
    projected_jobs = 0
    projected_tasks = 0
    cpu_before = backend_cpu(pid)
    client_before = time.process_time()
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if mode != "paused":
            jobs, tasks = poll()
            projected_jobs += len(jobs)
            projected_tasks += len(tasks)
            queries += 1
        time.sleep(
            max(
                0,
                started
                + (queries * observer.INTERVAL if mode != "paused" else seconds)
                - time.monotonic(),
            )
        )
    return {
        "mode": mode,
        "wall_seconds": time.monotonic() - started,
        "queries": queries,
        "projected_job_rows": projected_jobs,
        "projected_task_rows": projected_tasks,
        "sql_backend_cpu_seconds": backend_cpu(pid) - cpu_before,
        "client_cpu_seconds": time.process_time() - client_before,
    }


def main() -> None:
    """Print sanitized before/after measurements; never connect to production data."""
    with psycopg.connect(
        autocommit=True, application_name="observer-control"
    ) as connection:
        create_tables(connection)
        seed(connection)
        pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        result = {
            "fixture": {
                "jobs": 13,
                "invocations": 100,
                "dependencies": 1209,
                "tasks": 16,
            },
            "interval_seconds": observer.INTERVAL,
            "cpu_tick_seconds": 1 / os.sysconf("SC_CLK_TCK"),
            "windows": [
                measure(connection, mode, 10, pid)
                for mode in ("full", "versions", "paused", "paused", "versions", "full")
            ],
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
