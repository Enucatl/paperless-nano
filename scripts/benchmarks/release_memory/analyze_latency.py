"""Derive bounded invocation timings from retained, read-only observations."""

# ruff: noqa: INP001

import json
import os
import sys
import tempfile
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from uuid import UUID
from uuid import uuid5


def timestamp(value: str) -> float:
    """Convert a retained timezone-aware database timestamp to Unix seconds."""
    return datetime.fromisoformat(value).timestamp()


def bracket(row: dict) -> list[float | None]:
    """Bound a first observed transition by consecutive database statements."""
    return [row.get("previous_db_statement_unix"), row["db_statement_unix"]]


def sum_bounds(items: list[dict], key: str) -> list[float | None]:
    """Sum measured bounds without treating missing transitions as zero time."""
    return [
        None
        if any(item.get(key, [None, None])[index] is None for item in items)
        else sum(item[key][index] for item in items)
        for index in range(2)
    ]


def process_timings(path: Path, workload: list[dict]) -> dict:
    """Describe sampled processing and coordinator identities during each upload."""
    source = path / "processes.jsonl"
    if not source.exists():
        return {}
    identities = {}
    with source.open() as stream:
        for line in stream:
            row = json.loads(line)
            processes = row["services"].get("webserver", {}).get("processes", [])
            by_pid = {process["pid"]: process for process in processes}
            for process in processes:
                role = process.get("role")
                if role == "document_processor":
                    role = "provider-child"
                elif role in {"celery", "paperless_worker"} and by_pid.get(
                    process.get("ppid"), {}
                ).get("role") in {"celery", "paperless_worker"}:
                    role = "coordinator-child"
                else:
                    continue
                identity = f"{process['pid']}:{process['start_ticks']}"
                value = identities.setdefault(
                    identity,
                    {
                        "identity": identity,
                        "pid": process["pid"],
                        "start_ticks": process["start_ticks"],
                        "role": role,
                        "first_observed_unix": row["unix"],
                        "cpu_ticks_first": process["cpu_ticks"],
                    },
                )
                value["last_observed_unix"] = row["unix"]
                value["cpu_ticks_last"] = process["cpu_ticks"]
    uploads = []
    frequency = os.sysconf("SC_CLK_TCK")
    for item in workload:
        begin = item["admitted_unix"]
        end = item.get("metadata_title_search_completed_unix", item["completed_unix"])
        active = [
            value
            for value in identities.values()
            if value["first_observed_unix"] <= end
            and value["last_observed_unix"] >= begin
        ]
        workers = sorted(
            (value for value in active if value["role"] == "coordinator-child"),
            key=lambda value: value["start_ticks"],
        )
        uploads.append(
            {
                "document_id": item["document_id"],
                "begin_unix": begin,
                "end_unix": end,
                "provider_child_identities": [
                    value["identity"]
                    for value in active
                    if value["role"] == "provider-child"
                ],
                "coordinator_child_identities": [
                    value["identity"] for value in workers
                ],
                "coordinator_child_start_gap_seconds": [
                    (second["start_ticks"] - first["start_ticks"]) / frequency
                    for first, second in pairwise(workers)
                ],
            }
        )
    return {
        "clock_ticks_per_second": frequency,
        "identities": list(identities.values()),
        "uploads": uploads,
    }


def analyze_run(path: Path) -> dict:
    """Keep timing evidence distinct from inferred execution timestamps."""
    jobs = {}
    tasks = {}
    invocations = {}
    last_observed = None
    with (path / "jobs.jsonl").open() as source:
        for line in source:
            row = json.loads(line)
            last_observed = row["observed_unix"]
            if "task" in row:
                tasks[row["task"]["task_id"]] = row["task"]
            if "job" not in row:
                continue
            job = row["job"]
            jobs[job["id"]] = job
            for key, entry in job["invocations"].items():
                if not entry.get("policy"):
                    continue
                identity = (job["id"], key)
                item = invocations.setdefault(
                    identity,
                    {
                        "job_id": job["id"],
                        "kind": job["kind"],
                        "key": key,
                        "operation": entry["operation"],
                        "first_seen_bounds": bracket(row),
                        "status_bounds": {},
                        "claims": {},
                    },
                )
                item["status_bounds"].setdefault(entry["status"], bracket(row))
                item["last_status"] = entry["status"]
                item["last_attempts"] = entry.get("attempts", 0)
                if "lease_until" in entry:
                    claim = timestamp(entry["lease_until"]) - (job["timeout"] or 1800)
                    item["claims"][claim] = entry.get("attempts", 0)
    for item in invocations.values():
        item["claims"] = [
            {"claim_unix_derived_from_lease": claim, "attempt": attempt}
            for claim, attempt in sorted(item["claims"].items())
        ]
        if not item["claims"]:
            continue
        claim = item["claims"][0]["claim_unix_derived_from_lease"]
        lower, upper = item["first_seen_bounds"]
        item["queue_seconds_bounds"] = [
            max(0, claim - upper),
            None if lower is None else max(0, claim - lower),
        ]
        for status in ("staged", "succeeded"):
            if status in item["status_bounds"]:
                lower, upper = item["status_bounds"][status]
                item[f"claim_to_{status}_seconds_bounds"] = [
                    None if lower is None else max(0, lower - claim),
                    max(0, upper - claim),
                ]
        if "staged" in item["status_bounds"] and "succeeded" in item["status_bounds"]:
            staged = item["status_bounds"]["staged"]
            accepted = item["status_bounds"]["succeeded"]
            item["staged_to_acceptance_seconds_bounds"] = [
                None if accepted[0] is None else max(0, accepted[0] - staged[1]),
                None if staged[0] is None else max(0, accepted[1] - staged[0]),
            ]
    consume = sorted(
        (job for job in jobs.values() if job["kind"] == "consume"),
        key=lambda job: job["created"],
    )
    result_path = path / "result.json"
    workload = (
        json.loads(result_path.read_text())["workload"] if result_path.exists() else []
    )
    uploads = []
    for index, job in enumerate(consume):
        child_id = job["result"].get("metadata_job") or str(
            uuid5(UUID(job["id"]), "metadata")
        )
        child = jobs.get(child_id)
        next_created = (
            timestamp(consume[index + 1]["created"])
            if index + 1 < len(consume)
            else float("inf")
        )
        searches = [
            search
            for search in jobs.values()
            if search["kind"] == "search"
            and timestamp(job["created"]) < timestamp(search["created"]) < next_created
        ]
        upload = {
            "job_id": job["id"],
            "status": job["status"],
            "created": job["created"],
            "updated": job["updated"],
            "metadata_job_id": child["id"] if child else None,
            "task": tasks.get(job["id"]),
            "search_jobs": [
                {key: search[key] for key in ("id", "created", "updated", "status")}
                for search in sorted(searches, key=lambda value: value["created"])
            ],
        }
        serial = [
            item
            for item in invocations.values()
            if item["job_id"] in (job["id"], child_id)
        ]
        upload["serial_operation_count"] = len(serial)
        upload["serial_bounds"] = {
            key: sum_bounds(serial, key)
            for key in (
                "queue_seconds_bounds",
                "claim_to_staged_seconds_bounds",
                "staged_to_acceptance_seconds_bounds",
            )
        }
        upload["initial_coordinator_seconds_bounds"] = {}
        for owner, key, name in (
            (job, "prepare:0", "consume"),
            (child, "metadata", "metadata"),
        ):
            if owner is None:
                continue
            item = next(
                (
                    item
                    for item in serial
                    if item["job_id"] == owner["id"] and item["key"] == key
                ),
                None,
            )
            if item is not None:
                created = timestamp(owner["created"])
                upload["initial_coordinator_seconds_bounds"][name] = [
                    None if value is None else max(0, value - created)
                    for value in item["first_seen_bounds"]
                ]
        if index < len(workload):
            completed = workload[index]["completed_unix"]
            upload["observed_workload_seconds"] = workload[index][
                "upload_to_metadata_and_search_seconds"
            ]
            upload["ocr_endpoint_seconds"] = workload[index]["provenance"][
                "elapsed_seconds"
            ]
            upload["workload_completed_unix"] = completed
            for field in (
                "metadata_title_search_completed_unix",
                "upload_to_metadata_title_search_seconds",
            ):
                if field in workload[index]:
                    upload[field] = workload[index][field]
            if "metadata_title_search_completed_unix" in upload:
                upload["content_marker_to_metadata_title_search_seconds"] = (
                    upload["metadata_title_search_completed_unix"] - completed
                )
            if child:
                later_searches = [
                    search
                    for search in searches
                    if timestamp(search["created"]) >= timestamp(child["created"])
                    and search["status"] == "succeeded"
                ]
                if later_searches:
                    final_search = max(
                        later_searches, key=lambda value: value["created"]
                    )
                    upload["latest_successful_search_after_workload_seconds"] = (
                        timestamp(final_search["updated"]) - completed
                    )
                    upload["latest_successful_search_from_admission_seconds"] = (
                        timestamp(final_search["updated"])
                        - workload[index]["admitted_unix"]
                    )
        uploads.append(upload)
    return {
        "run": path.name,
        "last_observed_unix": last_observed,
        "complete_result": result_path.exists(),
        "uploads": uploads,
        "invocations": list(invocations.values()),
        "process_timing": process_timings(path, workload),
        "workload_markers": [
            {
                **{
                    key: item[key]
                    for key in (
                        "document_id",
                        "pages",
                        "admitted_unix",
                        "completed_unix",
                        "upload_to_metadata_and_search_seconds",
                        "metadata_title_search_completed_unix",
                        "upload_to_metadata_title_search_seconds",
                    )
                    if key in item
                },
                "ocr_endpoint_seconds": item["provenance"]["elapsed_seconds"],
            }
            for item in workload
        ],
        "baseline_metadata_span_seconds": [
            timestamp(item["end_time"]) - timestamp(item["start_time"])
            for item in json.loads((path / "inference-traces.json").read_text())
        ]
        if (path / "inference-traces.json").exists()
        else [],
    }


def self_check() -> None:
    """Check timestamp inference and observation bounds without service access."""
    assert timestamp("2026-11-05T20:30:00+00:00") - 1800 == timestamp(
        "2026-11-05T20:00:00+00:00"
    )
    assert bracket({"previous_db_statement_unix": 1.0, "db_statement_unix": 1.1}) == [
        1.0,
        1.1,
    ]
    assert sum_bounds([{"q": [1.0, 2.0]}, {"q": [3.0, 4.0]}], "q") == [4.0, 6.0]
    assert sum_bounds([{"q": [1.0, 2.0]}, {}], "q") == [None, None]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        rows = []
        for status, lower, upper in (
            ("pending", 1000.0, 1000.1),
            ("staged", 1001.1, 1001.2),
            ("succeeded", 1001.2, 1001.3),
        ):
            entry = {
                "policy": {"permission": "documents.process_search"},
                "operation": "search.write",
                "status": status,
                "attempts": int(status != "pending"),
            }
            if status != "pending":
                entry["lease_until"] = "1970-01-01T00:46:41+00:00"
            rows.append(
                {
                    "observed_unix": upper,
                    "previous_db_statement_unix": lower,
                    "db_statement_unix": upper,
                    "job": {
                        "id": "job",
                        "kind": "search",
                        "created": "1970-01-01T00:16:40+00:00",
                        "timeout": 1800,
                        "invocations": {"write": entry},
                    },
                }
            )
        (path / "jobs.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
        item = analyze_run(path)["invocations"][0]
        assert item["claims"][0]["claim_unix_derived_from_lease"] == 1001.0
        assert [round(value, 1) for value in item["queue_seconds_bounds"]] == [0.9, 1.0]
        assert [
            round(value, 1) for value in item["claim_to_staged_seconds_bounds"]
        ] == [0.1, 0.2]
        consume = {
            "observed_unix": 1010.0,
            "db_statement_unix": 1010.0,
            "job": {
                "id": "00000000-0000-0000-0000-000000000001",
                "kind": "consume",
                "status": "succeeded",
                "created": "1970-01-01T00:16:40+00:00",
                "updated": "1970-01-01T00:16:50+00:00",
                "result": {},
                "invocations": {},
            },
        }
        (path / "jobs.jsonl").write_text(json.dumps(consume))
        (path / "result.json").write_text(
            json.dumps(
                {
                    "workload": [
                        {
                            "completed_unix": 1005.0,
                            "admitted_unix": 1000.0,
                            "upload_to_metadata_and_search_seconds": 5.0,
                            "metadata_title_search_completed_unix": 1008.0,
                            "upload_to_metadata_title_search_seconds": 8.0,
                            "provenance": {"elapsed_seconds": 1.0},
                        }
                    ]
                }
            )
        )
        upload = analyze_run(path)["uploads"][0]
        assert upload["observed_workload_seconds"] == 5.0
        assert upload["upload_to_metadata_title_search_seconds"] == 8.0
        assert upload["content_marker_to_metadata_title_search_seconds"] == 3.0
        (path / "result.json").unlink()
        assert not analyze_run(path)["complete_result"]
        (path / "processes.jsonl").write_text(
            json.dumps(
                {
                    "unix": 1001.0,
                    "services": {
                        "webserver": {
                            "processes": [
                                {
                                    "pid": 1,
                                    "ppid": 0,
                                    "start_ticks": 10,
                                    "cpu_ticks": 1,
                                    "role": "celery",
                                },
                                {
                                    "pid": 2,
                                    "ppid": 1,
                                    "start_ticks": 20,
                                    "cpu_ticks": 2,
                                    "role": "celery",
                                },
                                {
                                    "pid": 3,
                                    "ppid": 0,
                                    "start_ticks": 30,
                                    "cpu_ticks": 3,
                                    "role": "document_processor",
                                },
                            ]
                        }
                    },
                }
            )
        )
        timing = process_timings(
            path,
            [{"document_id": 1, "admitted_unix": 1000.0, "completed_unix": 1002.0}],
        )
        assert timing["uploads"][0]["provider_child_identities"] == ["3:30"]
        assert timing["uploads"][0]["coordinator_child_identities"] == ["2:20"]


def main(source: Path, output: Path) -> None:
    """Write derived diagnostics separately from immutable raw evidence."""
    output.mkdir(exist_ok=True)
    runs = [
        analyze_run(path)
        for path in sorted(source.iterdir())
        if (path / "jobs.jsonl").exists()
        and path.name.startswith(("baseline", "candidate", "current"))
    ]
    remote_first = remote_last = None
    remote_count = 0
    remote_path = source / "remote-memory.jsonl"
    if remote_path.exists():
        with remote_path.open() as remote:
            for line in remote:
                row = json.loads(line)
                if "services" in row:
                    remote_first = row["unix"] if remote_first is None else remote_first
                    remote_last = row["unix"]
                    remote_count += 1
    result = {
        "method": "Claim times are derived from retained lease_until minus job timeout. Queue/staging/acceptance times are bounded by consecutive database observations requested every 100ms; intermediate transitions can be missed. Metadata child IDs use the source-defined uuid5(parent, metadata). Search association uses creation windows, not query-result equality. Process identities are sampled once per second; their counts and observed lifetimes are lower bounds, while start_ticks differences use this host kernel's clock frequency.",
        "remote": {
            "first_unix": remote_first,
            "last_unix": remote_last,
            "samples": remote_count,
            "error": (source / "remote-error.txt").read_text()
            if (source / "remote-error.txt").exists()
            else None,
        },
        "runs": runs,
    }
    (output / "latency-diagnostics.json").write_text(json.dumps(result, indent=2))
    for run in runs:
        sys.stdout.write(
            json.dumps({"run": run["run"], "uploads": run["uploads"]}) + "\n"
        )


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
