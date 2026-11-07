"""Observe isolated task/job transitions without Django or payload capture."""

# ruff: noqa: INP001

import hashlib
import json
import signal
import sys
import threading
import time
from typing import Any

INTERVAL = 0.1
HEARTBEAT = 10.0
ENTRY_FIELDS = (
    "id",
    "plugin",
    "operation",
    "status",
    "version",
    "attempts",
    "lease_until",
    "processor",
    "processor_location",
    "replay_safe",
    "remote_allowed",
)
RESULT_FIELDS = (
    "phase",
    "progress",
    "resume_status",
    "lifecycle",
    "metadata_job",
    "post_consume",
    "delivery",
    "cleanup",
    "callbacks_acknowledged",
    "metadata_changed",
)
TASK_FIELDS = (
    "task_id",
    "task_type",
    "trigger_source",
    "status",
    "date_created",
    "date_started",
    "date_done",
)
JOB_FIELDS = (
    "id",
    "kind",
    "status",
    "created",
    "updated",
    "attempts",
    "timeout",
    "cleaned",
)
# Select only diagnostic fields in PostgreSQL, avoiding large OCR result transfers.
JOBS_QUERY = """
SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
        'id', id, 'kind', kind, 'status', status,
        'created', created, 'updated', updated, 'attempts', attempts,
        'timeout', timeout, 'cleaned', cleaned, 'error', error,
        'document_id', COALESCE(input->'document_id',
            result->'payload'->'document_id', result->'document_id'),
        'root_id', result->'root_id',
        'result', (SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb)
            FROM jsonb_each(result) WHERE key = ANY(%s)),
        'invocations', (SELECT COALESCE(jsonb_object_agg(key,
            (SELECT jsonb_object_agg(field,
                 CASE WHEN field = 'policy'
                 THEN jsonb_build_object('permission', item->'permission')
                 ELSE item END)
             FROM jsonb_each(value) AS fields(field, item)
             WHERE field = ANY(%s))), '{}'::jsonb)
            FROM jsonb_each(COALESCE(result->'invocations', '{}'::jsonb)))
    ) ORDER BY created, id), '[]'::jsonb)
FROM documents_documentjob
"""
TASKS_QUERY = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'task_id', task_id, 'task_type', task_type, 'trigger_source', trigger_source,
    'status', status, 'date_created', date_created, 'date_started', date_started,
    'date_done', date_done
) ORDER BY date_created, id), '[]'::jsonb)
FROM documents_paperlesstask
"""


def poll(connection, *, has_jobs: bool, jobs: dict, tasks: dict) -> tuple:
    """Fetch row versions and changed diagnostics in one PostgreSQL snapshot."""
    # shortcut: xmin keys are a bounded-run cache; restart with a full snapshot after disconnects.
    job_versions = (
        "SELECT COALESCE(jsonb_object_agg(id, xmin::text), '{}'::jsonb) "
        "FROM documents_documentjob"
    )
    task_versions = (
        "SELECT COALESCE(jsonb_object_agg(task_id, xmin::text), '{}'::jsonb) "
        "FROM documents_paperlesstask"
    )
    query = (
        "SELECT clock_timestamp(), statement_timestamp(), ("
        + (job_versions if has_jobs else "SELECT '{}'::jsonb")
        + "), ("
        + (
            JOBS_QUERY + " WHERE id::text || ':' || xmin::text <> ALL(%s)"
            if has_jobs
            else "SELECT '[]'::jsonb"
        )
        + "), ("
        + task_versions
        + "), ("
        + TASKS_QUERY
        + " WHERE task_id || ':' || xmin::text <> ALL(%s))"
    )
    parameters = (
        [
            [*RESULT_FIELDS, "followup"],
            [*ENTRY_FIELDS, "policy", "dependencies", "error"],
            [f"{key}:{version}" for key, version in jobs.items()],
        ]
        if has_jobs
        else []
    )
    parameters.append([f"{key}:{version}" for key, version in tasks.items()])
    return connection.execute(query, parameters).fetchone()


def error_summary(error: Any) -> dict:
    """Fingerprint errors without exposing embedded paths, text or credentials."""
    text = str(error or "")
    return {
        "present": bool(text),
        "characters": len(text),
        "sha256": hashlib.sha256(text.encode()).hexdigest() if text else None,
    }


def scalar_fields(value: dict, fields: tuple[str, ...]) -> dict:
    """Copy only named scalar fields, never arbitrary nested payloads."""
    return {
        key: value[key]
        for key in fields
        if key in value and isinstance(value[key], (str, int, float, bool, type(None)))
    }


def project(row: dict) -> dict:
    """Whitelist job and invocation diagnostics, with secret-free error hashes."""
    job = scalar_fields(row, JOB_FIELDS)
    job["error"] = error_summary(row.get("error"))
    for field in ("document_id", "root_id"):
        value = row.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            job[field] = value
    result = row.get("result", {})
    job["result"] = scalar_fields(result, RESULT_FIELDS)
    if "followup" in result:
        job["result"]["followup"] = [
            item
            for item in result["followup"]
            if item
            in {
                "metadata",
                "workflows",
                "extensions",
                "post_consume",
                "index",
                "cache",
                "delivery",
                "cleanup",
                "thumbnail",
                "notify",
                "search",
            }
        ]
    job["invocations"] = {}
    for key, raw in row.get("invocations", {}).items():
        entry = scalar_fields(raw, ENTRY_FIELDS)
        entry["error"] = error_summary(raw.get("error"))
        entry["policy"] = scalar_fields(raw.get("policy", {}), ("permission",))
        entry["dependencies"] = [
            item for item in raw.get("dependencies", []) if isinstance(item, str)
        ]
        job["invocations"][key] = entry
    return job


def changed(previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    """Return new or changed projections while ignoring excluded payload changes."""
    return [job for key, job in current.items() if previous.get(key) != job]


def emit(event: str, **fields: Any) -> None:
    """Write one immediately flushed JSONL observation."""
    sys.stdout.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")
    sys.stdout.flush()


def observe() -> None:
    """Poll through a read-only session and emit bounded transition observations."""
    import psycopg

    stop = threading.Event()
    paused = threading.Event()
    wake = threading.Event()

    def control(signum, _frame) -> None:
        """Handle idle boundaries only between completed database observations."""
        if signum == signal.SIGUSR1:
            paused.set()
        elif signum == signal.SIGUSR2:
            paused.clear()
        else:
            stop.set()
        wake.set()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2):
        signal.signal(signum, control)
    with psycopg.connect(
        autocommit=True,
        application_name="paperless-release-job-observer",
        options="-c default_transaction_read_only=on -c statement_timeout=5000",
    ) as connection:
        has_jobs = connection.execute(
            "SELECT to_regclass('documents_documentjob') IS NOT NULL",
        ).fetchone()[0]
        previous = {}
        previous_tasks = {}
        previous_poll = None
        previous_statement = None
        heartbeat = time.monotonic()
        queries = 0
        total_query = 0.0
        max_query = 0.0
        versions = {}
        task_versions = {}
        was_paused = False
        while True:
            final = stop.is_set()
            if paused.is_set() and not final:
                if not was_paused:
                    emit("observer_paused", observed_unix=time.time(), queries=queries)
                    was_paused = True
                wake.wait()
                wake.clear()
                continue
            if was_paused:
                previous_poll = None
                previous_statement = None
            snapshot = previous_poll is None or final
            started = time.monotonic()
            poll_unix = time.time()
            db_clock, db_statement, versions, rows, task_versions, tasks = poll(
                connection,
                has_jobs=has_jobs,
                jobs={} if snapshot else versions,
                tasks={} if snapshot else task_versions,
            )
            query_seconds = time.monotonic() - started
            observed = time.time()
            queries += 1
            total_query += query_seconds
            max_query = max(max_query, query_seconds)
            bounds = {
                "observed_unix": observed,
                "poll_started_unix": poll_unix,
                "previous_poll_unix": previous_poll,
                "db_clock_unix": db_clock.timestamp(),
                "db_statement_unix": db_statement.timestamp(),
                "previous_db_statement_unix": previous_statement,
                "query_seconds": query_seconds,
            }
            current = {key: job for key, job in previous.items() if key in versions}
            current.update({row["id"]: project(row) for row in rows})
            current_tasks = {
                key: task
                for key, task in previous_tasks.items()
                if key in task_versions
            }
            current_tasks.update(
                {row["task_id"]: scalar_fields(row, TASK_FIELDS) for row in tasks},
            )
            snapshot_name = "final" if final else "resumed" if was_paused else "initial"
            for job in current.values() if snapshot else changed(previous, current):
                emit(
                    "job_snapshot" if snapshot else "job_transition",
                    snapshot=snapshot_name if snapshot else None,
                    job=job,
                    **bounds,
                )
            for task in (
                current_tasks.values()
                if snapshot
                else changed(previous_tasks, current_tasks)
            ):
                emit(
                    "task_snapshot" if snapshot else "task_transition",
                    snapshot=snapshot_name if snapshot else None,
                    task=task,
                    **bounds,
                )
            for key in previous_tasks.keys() - current_tasks.keys():
                emit("task_disappeared", task_id=key, **bounds)
            for key in previous.keys() - current.keys():
                emit("job_disappeared", job_id=key, **bounds)
            if snapshot or time.monotonic() - heartbeat >= HEARTBEAT:
                emit(
                    "observer_summary",
                    final=final,
                    interval_seconds=INTERVAL,
                    jobs=len(current),
                    tasks=len(current_tasks),
                    document_job_table=has_jobs,
                    queries=queries,
                    query_total_seconds=total_query,
                    query_max_seconds=max_query,
                    query_mean_seconds=total_query / queries,
                    **bounds,
                )
                heartbeat = time.monotonic()
            if was_paused and not final:
                emit("observer_resumed", observed_unix=observed, queries=queries)
            was_paused = False
            if final:
                return
            previous = current
            previous_tasks = current_tasks
            previous_poll = poll_unix
            previous_statement = db_statement.timestamp()
            wake.wait(max(0.0, INTERVAL - (time.monotonic() - started)))
            wake.clear()


def self_check() -> None:
    """Assert secret exclusion and useful transition detection without a database."""
    raw = {
        "id": "job",
        "kind": "ingest",
        "status": "waiting",
        "timeout": 1800,
        "document_id": 17,
        "root_id": 17,
        "error": "secret credential",
        "input": "secret text",
        "configuration": {"token": "secret"},
        "result": {"phase": "preparing", "content": "secret", "followup": ["index"]},
        "invocations": {
            "parser": {
                "id": "entry",
                "plugin": "provider",
                "operation": "parser.process",
                "status": "pending",
                "lease_until": "2026-11-05T12:00:00+00:00",
                "policy": {"permission": "documents.process_parser", "token": "secret"},
                "dependencies": ["previous"],
                "token": "secret",
                "result": "secret",
                "input": "secret",
                "configuration": "secret",
                "error": "secret",
            }
        },
    }
    first = project(raw)
    assert "secret" not in json.dumps(first)
    assert first["document_id"] == 17
    assert first["timeout"] == 1800
    assert first["invocations"]["parser"]["policy"] == {
        "permission": "documents.process_parser",
    }
    assert first["invocations"]["parser"]["dependencies"] == ["previous"]
    assert changed({}, {"job": first}) == [first]
    assert changed({"job": first}, {"job": project(raw)}) == []
    raw["invocations"]["parser"]["result"] = "another hidden payload"
    assert changed({"job": first}, {"job": project(raw)}) == []
    raw["invocations"]["parser"]["status"] = "leased"
    assert len(changed({"job": first}, {"job": project(raw)})) == 1
    task = {"task_id": "task", "status": "STARTED", "result": "secret"}
    assert scalar_fields(task, TASK_FIELDS) == {
        "task_id": "task",
        "status": "STARTED",
    }
    emit("self_check", passed=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        self_check()
    else:
        try:
            observe()
        except Exception as error:
            emit("observer_error", error_type=type(error).__name__)
            sys.exit(1)
