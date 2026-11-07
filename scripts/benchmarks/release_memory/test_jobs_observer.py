"""Check observer snapshots, idle boundaries and failures against real PostgreSQL."""

# ruff: noqa: INP001

import json
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

import analyze_comparison
import analyze_latency
import jobs_observer as observer
import observer_control
import psycopg
import pytest
import run


@pytest.fixture
def connection():
    """Reset only the dedicated, disposable control database between tests."""
    with psycopg.connect(autocommit=True) as connection:
        connection.execute(
            "DROP TABLE IF EXISTS documents_documentjob, documents_paperlesstask"
        )
        observer_control.create_tables(connection)
        observer_control.seed(connection)
        yield connection


def test_changed_rows_and_consistent_versions(connection):
    """Detect result-only writes, reopening, deletion and same-ID reinsertion."""
    _, _, versions, jobs, task_versions, tasks = observer.poll(
        connection, has_jobs=True, jobs={}, tasks={}
    )
    assert len(jobs) == 13 and len(tasks) == 16
    assert sum(len(job["invocations"]) for job in jobs) == 100
    assert (
        sum(
            len(entry["dependencies"])
            for job in jobs
            for entry in job["invocations"].values()
        )
        == 1209
    )
    initial = jobs[0]
    before = connection.execute(
        "SELECT updated FROM documents_documentjob WHERE id=%s", (UUID(int=1),)
    ).fetchone()[0]
    _, _, same, jobs, same_tasks, tasks = observer.poll(
        connection, has_jobs=True, jobs=versions, tasks=task_versions
    )
    assert same == versions and same_tasks == task_versions
    assert jobs == tasks == []
    connection.execute(
        "UPDATE documents_documentjob SET result=jsonb_set(result, '{invocations,operation-0,status}', '\"pending\"') WHERE id=%s",
        (UUID(int=1),),
    )
    _, _, changed, jobs, _, tasks = observer.poll(
        connection, has_jobs=True, jobs=versions, tasks=task_versions
    )
    assert len(jobs) == 1 and tasks == []
    assert jobs[0]["invocations"]["operation-0"]["status"] == "pending"
    assert (
        connection.execute(
            "SELECT updated FROM documents_documentjob WHERE id=%s", (UUID(int=1),)
        ).fetchone()[0]
        == before
    )
    connection.execute(
        "UPDATE documents_documentjob SET status='waiting' WHERE id=%s", (UUID(int=1),)
    )
    _, _, changed, jobs, _, _ = observer.poll(
        connection, has_jobs=True, jobs=changed, tasks=task_versions
    )
    assert jobs[0]["status"] == "waiting"
    connection.execute("DELETE FROM documents_documentjob WHERE id=%s", (UUID(int=1),))
    _, _, deleted, jobs, _, _ = observer.poll(
        connection, has_jobs=True, jobs=changed, tasks=task_versions
    )
    assert str(UUID(int=1)) not in deleted and jobs == []
    connection.execute(
        "INSERT INTO documents_documentjob VALUES (%s, 'consume', 'waiting', now(), now(), 1, 1800, false, '', '{}', '{}')",
        (UUID(int=1),),
    )
    _, _, reinserted, jobs, _, _ = observer.poll(
        connection, has_jobs=True, jobs=deleted, tasks=task_versions
    )
    assert len(jobs) == 1 and jobs[0]["id"] == initial["id"]
    assert reinserted[initial["id"]] != versions[initial["id"]]
    connection.execute(
        "UPDATE documents_paperlesstask SET status='started' WHERE task_id='task-0'"
    )
    _, _, _, _, changed_tasks, tasks = observer.poll(
        connection, has_jobs=True, jobs=reinserted, tasks=task_versions
    )
    assert len(tasks) == 1 and tasks[0]["status"] == "started"
    connection.execute("DELETE FROM documents_paperlesstask WHERE task_id='task-0'")
    _, _, _, _, deleted_tasks, tasks = observer.poll(
        connection, has_jobs=True, jobs=reinserted, tasks=changed_tasks
    )
    assert "task-0" not in deleted_tasks and tasks == []


def test_legacy_tasks_without_document_jobs(connection):
    """Apply the same version-based polling to versions without the job table."""
    connection.execute("DROP TABLE documents_documentjob")
    _, _, versions, jobs, task_versions, tasks = observer.poll(
        connection, has_jobs=False, jobs={}, tasks={}
    )
    assert versions == {} and jobs == [] and len(tasks) == 16
    assert (
        observer.poll(connection, has_jobs=False, jobs={}, tasks=task_versions)[-1]
        == []
    )


def test_versions_and_details_share_snapshot(connection, monkeypatch):
    """A concurrent commit cannot pair details with another version's cache key."""
    monkeypatch.setattr(
        observer,
        "JOBS_QUERY",
        observer.JOBS_QUERY.replace(
            "'id', id,", "'tuple_version', xmin::text, 'id', id,"
        ),
    )
    stop = threading.Event()
    errors = []

    def update() -> None:
        """Commit concurrent result-only changes until polling finishes."""
        try:
            with psycopg.connect(autocommit=True) as writer:
                while not stop.is_set():
                    writer.execute(
                        "UPDATE documents_documentjob SET result=jsonb_set(result, '{progress}', to_jsonb(random())) WHERE id=%s",
                        (UUID(int=1),),
                    )
        except Exception as error:
            errors.append(error)

    writer = threading.Thread(target=update, daemon=True)
    writer.start()
    try:
        for _ in range(20):
            _, _, versions, jobs, _, _ = observer.poll(
                connection, has_jobs=True, jobs={}, tasks={}
            )
            assert all(row["tuple_version"] == versions[row["id"]] for row in jobs)
    finally:
        stop.set()
        writer.join(timeout=10)
    assert not writer.is_alive() and errors == []


def start_observer() -> tuple:
    """Start a real observer with line-oriented event collection for bounded waits."""
    child = subprocess.Popen(
        [sys.executable, str(Path(observer.__file__))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    events = queue.Queue()

    def read() -> None:
        """Collect flushed events without blocking timeout assertions."""
        for line in child.stdout:
            events.put(json.loads(line))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    return child, events, reader


def stop_observer(child, reader) -> None:
    """Release a real observer and both pipes even when its assertion failed."""
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=10)
    reader.join(timeout=10)
    child.stdout.close()
    child.stderr.close()


@pytest.fixture
def process(connection):
    """Seed PostgreSQL before starting the observer and always clean it up."""
    child, events, reader = start_observer()
    yield child, events
    stop_observer(child, reader)


def until(events, name: str) -> tuple[dict, list]:
    """Read through a required event without an unbounded test wait."""
    seen = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = events.get(timeout=max(0.01, deadline - time.monotonic()))
        seen.append(row)
        if row["event"] == name:
            return row, seen
    raise AssertionError(f"Missing {name}")


def test_pause_resume_snapshot_and_timing_bounds(process, connection):
    """Acknowledge pause after SQL, skip idle SQL and reset resume timing bounds."""
    child, events = process
    until(events, "observer_summary")
    child.send_signal(signal.SIGUSR1)
    paused, _ = until(events, "observer_paused")
    before = connection.execute(
        "SELECT query_start FROM pg_stat_activity WHERE application_name='paperless-release-job-observer'"
    ).fetchone()[0]
    connection.execute(
        "UPDATE documents_documentjob SET status='waiting' WHERE id=%s", (UUID(int=1),)
    )
    connection.execute("DELETE FROM documents_documentjob WHERE id=%s", (UUID(int=2),))
    connection.execute(
        "INSERT INTO documents_documentjob VALUES (%s, 'consume', 'waiting', now(), now(), 1, 1800, false, '', '{}', '{}')",
        (UUID(int=14),),
    )
    time.sleep(0.3)
    after = connection.execute(
        "SELECT query_start FROM pg_stat_activity WHERE application_name='paperless-release-job-observer'"
    ).fetchone()[0]
    assert before == after
    child.send_signal(signal.SIGUSR2)
    resumed, rows = until(events, "observer_resumed")
    assert resumed["queries"] == paused["queries"] + 1
    jobs = [row for row in rows if row["event"] == "job_snapshot"]
    assert len(jobs) == 13
    assert all(
        row["snapshot"] == "resumed"
        and row["previous_db_statement_unix"] is None
        and row["previous_poll_unix"] is None
        for row in jobs
    )
    assert (
        next(row for row in jobs if row["job"]["id"] == str(UUID(int=1)))["job"][
            "status"
        ]
        == "waiting"
    )
    assert any(row["job"]["id"] == str(UUID(int=14)) for row in jobs)
    assert any(
        row["event"] == "job_disappeared" and row["job_id"] == str(UUID(int=2))
        for row in rows
    )


def test_disconnect_fails_closed_and_restart_is_full_snapshot(process, connection):
    """A disconnected run fails explicitly; a fresh connection cannot reuse cache."""
    child, events = process
    until(events, "observer_summary")
    pid = connection.execute(
        "SELECT pid FROM pg_stat_activity WHERE application_name='paperless-release-job-observer'"
    ).fetchone()[0]
    connection.execute("SELECT pg_terminate_backend(%s)", (pid,))
    error, _ = until(events, "observer_error")
    assert set(error) == {"event", "error_type"}
    assert child.wait(timeout=10) == 1
    restarted, events, reader = start_observer()
    try:
        _, rows = until(events, "observer_summary")
        jobs = [row for row in rows if "job" in row]
        assert len(jobs) == 13
        assert all(
            row["snapshot"] == "initial" and row["previous_db_statement_unix"] is None
            for row in jobs
        )
    finally:
        stop_observer(restarted, reader)


def test_idle_driver_boundaries(monkeypatch):
    """Measurement starts after pause ACK and resumes before returning to workload."""
    calls = []
    monkeypatch.setattr(
        run, "pause_observer", lambda paused: calls.append(("pause", paused, run.PHASE))
    )
    monkeypatch.setattr(
        run.time, "sleep", lambda seconds: calls.append(("sleep", run.PHASE))
    )
    monkeypatch.setattr(run, "pss", lambda: calls.append(("pss", run.PHASE)) or {})
    result = run.observe("startup-idle")
    assert calls == [
        ("pause", True, "observer-pause"),
        ("sleep", "startup-idle"),
        ("pss", "observer-resume"),
        ("pause", False, "observer-resume"),
    ]
    assert result["lifecycle_observer_paused"] is True
    assert result["unix"] >= result["started_unix"]


@pytest.mark.parametrize("partial_start", [False, True])
def test_ack_reader_waits_for_complete_new_record(monkeypatch, tmp_path, partial_start):
    """Ignore old acknowledgements and retain an incomplete final JSONL record."""
    source = tmp_path / "jobs.jsonl"
    source.write_text(
        '{"event":"observer_paused"}\n' + ('{"event":"job_sna' if partial_start else "")
    )
    complete = threading.Event()

    def append() -> None:
        """Model Docker writing an acknowledgement across multiple writes."""
        with source.open("a") as output:
            if partial_start:
                output.write('pshot"}\n')
            output.write('{"event":"observer_pa')
            output.flush()
            time.sleep(0.05)
            complete.set()
            output.write('used"}\n')
            output.flush()

    writer = threading.Thread(target=append, daemon=True)

    def docker(*args) -> None:
        """Append only after the caller has positioned its reader at EOF."""
        assert args[:3] == ("kill", "--signal", "USR1")
        writer.start()

    monkeypatch.setattr(run, "RUN_OUT", tmp_path)
    monkeypatch.setattr(run, "docker", docker)
    run.pause_observer(paused=True)
    assert complete.is_set()
    writer.join(timeout=10)
    assert not writer.is_alive()


def test_idle_counters_exclude_pre_pause_interval(tmp_path):
    """Keep historical deltas but exclude SQL before a new run's idle start."""
    service = {
        "current": 1,
        "working_set": 1,
        "anon": 1,
        "file": 0,
        "peak": 1,
        "io": {},
    }
    samples = [
        {
            "unix": float(index),
            "phase": "startup-idle",
            "services": {"db": {**service, "cpu": {"usage_usec": value}}},
        }
        for index, value in enumerate((0, 1000000, 1001000))
    ]
    historical = analyze_comparison.summarize_window(samples[1:], [], samples, [])
    current = analyze_comparison.summarize_window(
        samples[1:], [], samples, [], counter_not_before=0.5
    )
    assert (
        historical["cpu_seconds"] == 1.001 and historical["counter_span_seconds"] == 2
    )
    assert current["cpu_seconds"] == 0.001 and current["counter_span_seconds"] == 1
    assert current["counter_start_unix"] == 1
    assert current["rates"]["cpu_seconds_per_second"]["p95"] == 0.001
    (tmp_path / "memory.jsonl").write_text(
        "\n".join(json.dumps(row) for row in samples)
    )
    retained = {
        "services": {"db": {"image": "synthetic"}},
        "snapshots": [{"phase": "startup-idle", "started_unix": 0.5, "unix": 3}],
    }
    (tmp_path / "result.json").write_text(json.dumps(retained))
    assert (
        analyze_comparison.analyze_run(tmp_path)["phases"]["startup-idle"]["all"][
            "cpu_seconds"
        ]
        == 1.001
    )
    retained["snapshots"][0]["lifecycle_observer_paused"] = True
    (tmp_path / "result.json").write_text(json.dumps(retained))
    assert (
        analyze_comparison.analyze_run(tmp_path)["phases"]["startup-idle"]["all"][
            "cpu_seconds"
        ]
        == 0.001
    )


def test_resumed_analysis_preserves_gap(tmp_path):
    """A resumed snapshot cannot establish a transition's lower timing bound."""
    rows = []
    for status, lower, upper in (("pending", 1.0, 2.0), ("succeeded", None, 10.0)):
        entry = {
            "policy": {"permission": "documents.process_search"},
            "operation": "search.write",
            "status": status,
            "attempts": int(status != "pending"),
        }
        if status == "succeeded":
            entry["lease_until"] = "1970-01-01T00:30:04+00:00"
        rows.append(
            {
                "observed_unix": upper,
                "previous_db_statement_unix": lower,
                "db_statement_unix": upper,
                "job": {
                    "id": "search-job",
                    "kind": "search",
                    "timeout": 1800,
                    "invocations": {"write": entry},
                },
            },
        )
    (tmp_path / "jobs.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    item = analyze_latency.analyze_run(tmp_path)["invocations"][0]
    assert item["status_bounds"]["succeeded"] == [None, 10.0]
    assert item["claim_to_succeeded_seconds_bounds"] == [None, 6.0]


def test_secret_filtering():
    """Retain existing sanitization checks on both snapshots and projections."""
    observer.self_check()
