"""Summarize per-run evidence and complete-repeat medians without pooling samples."""

# ruff: noqa: INP001

import json
import re
import statistics
import sys
import tempfile
from collections import Counter
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from pathlib import Path

RUNS = ("baseline1", "candidate1", "current1", "current2", "candidate2", "baseline2")
IDLE_PHASES = ("startup-idle", "post-feature-idle", "final-idle")
MIB = 2**20


def distribution(values: list[float]) -> dict | None:
    """Summarize samples with a linearly interpolated percentile."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    fraction = position - lower
    return {
        "samples": len(values),
        "min": ordered[0],
        "p50": statistics.median(values),
        "mean": statistics.mean(values),
        "p95": ordered[lower]
        + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * fraction,
        "max": ordered[-1],
    }


def rows(path: Path) -> list[dict]:
    """Read available observations; missing files remain explicitly incomplete."""
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def process_role(service: str, process: dict, by_pid: dict) -> str:
    """Normalize service and parent relationships without guessing unknown commands."""
    if service != "webserver":
        return service
    role = process.get("role", "unavailable")
    parent = by_pid.get(process.get("ppid"), {})
    if role == "granian":
        return "http-child" if parent.get("role") == "granian" else "http-parent"
    if role in {"celery", "paperless_worker"}:
        return (
            "worker-child"
            if parent.get("role") in {"celery", "paperless_worker"}
            else "worker-parent"
        )
    if role == "document_processor":
        return "provider-processor"
    if role == "spawn_main":
        for _ in range(6):
            ancestor = parent.get("role")
            if ancestor == "granian":
                return "http-spawn"
            if ancestor in {"celery", "paperless_worker"}:
                return "worker-spawn"
            if ancestor in {"document_processor", "paperless_services.processor"}:
                return "provider-spawn"
            parent = by_pid.get(parent.get("ppid"), {})
        return "unattributed-spawn"
    return role


def processes(row: dict) -> list[dict]:
    """Retain process identity and named memory metrics, excluding command arguments."""
    result = []
    for service, info in row["services"].items():
        by_pid = {p["pid"]: p for p in info["processes"]}
        for process in by_pid.values():
            if "pss_kib" in process:
                result.append(
                    {
                        "identity": f"{process['pid']}:{process['start_ticks']}",
                        "role": process_role(service, process, by_pid),
                        **{
                            key: process[key]
                            for key in ("pid", "ppid", "start_ticks", "pss_kib")
                        },
                    }
                )
    return result


def recovery_events(path: Path) -> list[dict]:
    """Count logged Celery recovery deliveries, not unlogged inline recovery calls."""
    if not path.exists():
        return []
    pattern = re.compile(
        r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})\].*"
        r"Task documents\.tasks\.recover_document_jobs\[([^]]+)\] "
        r"(received|succeeded|failed|raised unexpected)"
    )
    result = []
    for line in path.read_text().splitlines():
        if match := pattern.search(line):
            result.append(
                {
                    "unix": datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f")
                    .replace(tzinfo=UTC)
                    .timestamp(),
                    "task_id": match[2],
                    "event": match[3],
                }
            )
    return result


def summarize_window(
    memory: list[dict],
    process_rows: list[dict],
    all_memory: list[dict],
    events: list[dict],
    *,
    counter_not_before: float | None = None,
) -> dict:
    """Compute sample statistics and counter deltas with explicit time bounds."""
    result = {
        "memory_samples": len(memory),
        "process_samples": len(process_rows),
        "metrics": {},
        "services": {},
        "roles": {},
        "rates": {},
    }
    if not memory:
        return result
    first, last = memory[0], memory[-1]
    index = all_memory.index(first)
    before = all_memory[index - 1] if index else first
    if counter_not_before is not None and before["unix"] < counter_not_before:
        before = first
    result.update(
        first_unix=first["unix"],
        last_unix=last["unix"],
        counter_start_unix=before["unix"],
        counter_span_seconds=last["unix"] - before["unix"],
    )
    for metric in ("current", "working_set", "anon", "file"):
        result["metrics"][metric + "_mib"] = distribution(
            [sum(s[metric] for s in row["services"].values()) / MIB for row in memory]
        )
    result["metrics"]["pss_mib"] = distribution(
        [sum(s["pss_mib"] for s in row["services"].values()) for row in process_rows]
    )
    for metric in ("pss_anon_kib", "pss_file_kib", "pss_shmem_kib", "swap_kib"):
        result["metrics"][metric.replace("_kib", "_mib")] = distribution(
            [
                sum(
                    p.get(metric, 0)
                    for s in row["services"].values()
                    for p in s["processes"]
                )
                / 1024
                for row in process_rows
            ]
        )
    for service in first["services"]:
        stats = {
            metric + "_mib": distribution(
                [row["services"][service][metric] / MIB for row in memory]
            )
            for metric in ("current", "working_set", "anon", "file")
        }
        stats["pss_mib"] = distribution(
            [row["services"][service]["pss_mib"] for row in process_rows]
        )
        begin = before["services"].get(service, first["services"][service])
        end = last["services"][service]
        stats["cpu_seconds"] = (
            end["cpu"]["usage_usec"] - begin["cpu"]["usage_usec"]
        ) / 1e6
        stats["read_mib"] = (
            end["io"].get("rbytes", 0) - begin["io"].get("rbytes", 0)
        ) / MIB
        stats["write_mib"] = (
            end["io"].get("wbytes", 0) - begin["io"].get("wbytes", 0)
        ) / MIB
        assert stats["cpu_seconds"] >= 0, "Counter reset requires a separate run"
        result["services"][service] = stats
    for metric in ("cpu_seconds", "read_mib", "write_mib"):
        result[metric] = sum(stats[metric] for stats in result["services"].values())
    positions = {id(row): index for index, row in enumerate(all_memory)}
    rates = defaultdict(lambda: defaultdict(list))
    for row in memory:
        index = positions[id(row)]
        if not index:
            continue
        previous = all_memory[index - 1]
        seconds = row["unix"] - previous["unix"]
        if (
            seconds <= 0
            or set(previous["services"]) != set(row["services"])
            or (
                counter_not_before is not None and previous["unix"] < counter_not_before
            )
        ):
            continue
        for service, current in row["services"].items():
            old = previous["services"][service]
            rates[service]["cpu_seconds_per_second"].append(
                (current["cpu"]["usage_usec"] - old["cpu"]["usage_usec"])
                / 1e6
                / seconds
            )
            for counter, metric in (
                ("rbytes", "read_mib_per_second"),
                ("wbytes", "write_mib_per_second"),
            ):
                rates[service][metric].append(
                    (current["io"].get(counter, 0) - old["io"].get(counter, 0))
                    / MIB
                    / seconds
                )
    for service, metrics in rates.items():
        result["services"][service]["rates"] = {
            metric: distribution(values) for metric, values in metrics.items()
        }
    if rates:
        for metric in (
            "cpu_seconds_per_second",
            "read_mib_per_second",
            "write_mib_per_second",
        ):
            result["rates"][metric] = distribution(
                [
                    sum(values)
                    for values in zip(
                        *(metrics[metric] for metrics in rates.values()), strict=True
                    )
                ]
            )
    per_role = []
    for row in process_rows:
        totals = defaultdict(float)
        for process in processes(row):
            totals[process["role"]] += process["pss_kib"] / 1024
        per_role.append(totals)
    for role in sorted(set().union(*(item.keys() for item in per_role))):
        result["roles"][role] = distribution([item.get(role, 0.0) for item in per_role])
    result["recovery_received"] = sum(
        before["unix"] < e["unix"] <= last["unix"] and e["event"] == "received"
        for e in events
    )
    return result


def analyze_run(path: Path) -> dict:
    """Describe complete or partial runs without silently treating missing runs as zero."""
    if not path.exists():
        return {"run": path.name, "state": "missing", "complete": False}
    result_path = path / "result.json"
    retained = json.loads(result_path.read_text()) if result_path.exists() else {}
    memory = rows(path / "memory.jsonl")
    process_rows = rows(path / "processes.jsonl")
    workload = retained.get("workload", [])
    expected = set(retained.get("services", {})) or (
        {"db", "broker", "webserver", "ai", "webhook-listener", "phoenix"}
        if path.name.startswith("baseline")
        else {"db", "broker", "webserver"}
    )
    full_memory = [row for row in memory if set(row["services"]) == expected]
    full_process = [row for row in process_rows if set(row["services"]) == expected]
    complete = len(workload) == 3 and all(
        all(
            upload.get(key)
            for key in ("original_preserved", "archive_downloaded", "search_verified")
        )
        for upload in workload
    )
    result = {
        "run": path.name,
        "state": "complete" if complete else "partial",
        "complete": complete,
        "expected_services": sorted(expected),
        "memory_samples": len(memory),
        "full_memory_samples": len(full_memory),
        "process_samples": len(process_rows),
        "full_process_samples": len(full_process),
        "phase_labelled_memory_samples": dict(
            Counter(row["phase"] for row in full_memory)
        ),
        "phases": {},
        "latencies": [],
        "process_lifetimes": {},
    }
    events = recovery_events(path / "webserver.log")
    result["recovery"] = {
        "events": events,
        "received_deliveries": sum(e["event"] == "received" for e in events),
        "unique_received_tasks": len(
            {e["task_id"] for e in events if e["event"] == "received"}
        ),
    }
    starts = {s["phase"]: s["started_unix"] for s in retained.get("snapshots", [])}
    ends = {s["phase"]: s["unix"] for s in retained.get("snapshots", [])}
    idle_bounds = {
        s["phase"]: s["started_unix"]
        for s in retained.get("snapshots", [])
        if s.get("lifecycle_observer_paused")
    }
    phases = list(dict.fromkeys(row["phase"] for row in full_memory))
    for phase in phases:
        selected_memory = [row for row in full_memory if row["phase"] == phase]
        selected_process = [row for row in full_process if row["phase"] == phase]
        start = starts.get(phase, selected_memory[0]["unix"])
        windows = {"all": (None, None)}
        if phase in IDLE_PHASES and phase in ends:
            windows["all"] = (start, ends[phase])
        if phase in IDLE_PHASES:
            windows.update(
                first60=(start, start + 60), final240=(start + 60, start + 300)
            )
        result["phases"][phase] = {}
        for name, (lower, upper) in windows.items():
            ms = [
                row
                for row in selected_memory
                if lower is None or lower <= row["unix"] < upper
            ]
            ps = [
                row
                for row in selected_process
                if lower is None or lower <= row["unix"] < upper
            ]
            result["phases"][phase][name] = summarize_window(
                ms,
                ps,
                memory,
                events,
                counter_not_before=idle_bounds.get(phase),
            )
    for row in full_process:
        for process in processes(row):
            identity = process["identity"]
            item = result["process_lifetimes"].setdefault(
                identity,
                {
                    "role": process["role"],
                    "first_role": process["role"],
                    "pid": process["pid"],
                    "ppid": process["ppid"],
                    "start_ticks": process["start_ticks"],
                    "first_seen_unix": row["unix"],
                    "first_phase": row["phase"],
                    "samples": 0,
                    "max_pss_mib": 0,
                },
            )
            item.update(
                role=process["role"],
                last_seen_unix=row["unix"],
                last_phase=row["phase"],
                samples=item["samples"] + 1,
                max_pss_mib=max(item["max_pss_mib"], process["pss_kib"] / 1024),
            )
    result["phase_process_identity_counts"] = {}
    seen = set()
    for phase in phases:
        identities = {
            p["identity"]: p
            for row in full_process
            if row["phase"] == phase
            for p in processes(row)
        }
        result["phase_process_identity_counts"][phase] = {
            "newly_observed": dict(
                Counter(p["role"] for key, p in identities.items() if key not in seen)
            ),
            "reused_from_earlier_phase": dict(
                Counter(p["role"] for key, p in identities.items() if key in seen)
            ),
        }
        seen.update(identities)
    result["idle_endpoints"] = {}
    for phase in IDLE_PHASES:
        selected = [
            row
            for row in full_process
            if row["phase"] == phase
            and (phase not in ends or starts[phase] <= row["unix"] < ends[phase])
        ]
        if selected:
            result["idle_endpoints"][phase] = {
                "first": processes(selected[0]),
                "last": processes(selected[-1]),
            }
    for upload in workload:
        result["latencies"].append(
            {
                key: upload[key]
                for key in (
                    "pages",
                    "admitted_unix",
                    "completed_unix",
                    "upload_to_metadata_and_search_seconds",
                    "metadata_title_search_completed_unix",
                    "upload_to_metadata_title_search_seconds",
                )
                if key in upload
            }
        )
    result["readiness"] = {
        key: retained[key]
        for key in ("api_ready_seconds", "stack_ready_seconds")
        if key in retained
    }
    result["service_images"] = {
        key: value["image"] for key, value in retained.get("services", {}).items()
    }
    if full_memory:
        peak = max(
            full_memory,
            key=lambda row: sum(s["current"] for s in row["services"].values()),
        )
        result["sampled_raw_peak"] = {
            "unix": peak["unix"],
            "phase": peak["phase"],
            **{
                metric + "_mib": sum(s[metric] for s in peak["services"].values()) / MIB
                for metric in ("current", "working_set", "anon", "file")
            },
        }
        result["service_lifetime_peak_sum_upper_bound_mib"] = (
            sum(s["peak"] for s in full_memory[-1]["services"].values()) / MIB
        )
    return result


def repeat_summary(runs: list[dict]) -> dict:
    """Take medians of run statistics, excluding incomplete repeats entirely."""
    result = {}
    for variant in ("baseline", "candidate", "current"):
        complete = [
            run for run in runs if run["run"].startswith(variant) and run["complete"]
        ]
        item = {
            "complete_runs": [run["run"] for run in complete],
            "n_runs": len(complete),
            "phase_statistics": {},
            "latency_statistics": {},
        }
        buckets = defaultdict(list)
        for run in complete:
            for phase, windows in run["phases"].items():
                for window, stats in windows.items():
                    for metric, values in stats["metrics"].items():
                        if values:
                            for statistic in ("p50", "mean", "p95"):
                                buckets[
                                    f"{phase}/{window}/{metric}/{statistic}"
                                ].append(values[statistic])
                    for metric, values in stats.get("rates", {}).items():
                        for statistic in ("p50", "mean", "p95"):
                            buckets[f"{phase}/{window}/{metric}/{statistic}"].append(
                                values[statistic]
                            )
                    for metric in ("cpu_seconds", "read_mib", "write_mib"):
                        if metric in stats:
                            buckets[f"{phase}/{window}/{metric}"].append(stats[metric])
        item["phase_statistics"] = {
            key: {
                "n_runs": len(values),
                "median_of_run_statistics": statistics.median(values),
            }
            for key, values in buckets.items()
        }
        for index in range(3):
            for metric in (
                "upload_to_metadata_and_search_seconds",
                "upload_to_metadata_title_search_seconds",
            ):
                values = [
                    run["latencies"][index][metric]
                    for run in complete
                    if metric in run["latencies"][index]
                ]
                if values:
                    item["latency_statistics"][f"upload{index + 1}/{metric}"] = {
                        "n_runs": len(values),
                        "median_of_run_values": statistics.median(values),
                    }
        result[variant] = item
    return result


def self_check() -> None:
    """Check parent classification, split boundaries and complete-repeat exclusion."""
    assert distribution([0, 10])["p95"] == 9.5
    assert distribution([]) is None
    parent = {"pid": 1, "role": "granian"}
    child = {"pid": 2, "ppid": 1, "role": "granian"}
    assert process_role("webserver", child, {1: parent}) == "http-child"
    assert process_role("db", {"role": "phoenix"}, {}) == "db"
    assert (
        process_role(
            "webserver",
            {"role": "spawn_main", "ppid": 2},
            {2: {"role": "document_processor"}},
        )
        == "provider-spawn"
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        assert analyze_run(root / "candidate2")["state"] == "missing"
        log = root / "webserver.log"
        log.write_text(
            "  . documents.tasks.recover_document_jobs\n"
            "[2026-11-06 00:00:00,000] Task documents.tasks.recover_document_jobs[task] received\n"
            "[2026-11-06 00:00:00,010] Task documents.tasks.recover_document_jobs[task] succeeded\n"
        )
        events = recovery_events(log)
        assert [event["event"] for event in events] == ["received", "succeeded"]
        assert events[0]["task_id"] == "task"
        service = {
            "current": MIB,
            "working_set": MIB,
            "anon": MIB,
            "file": 0,
            "peak": MIB,
            "cpu": {"usage_usec": 0},
            "io": {},
        }
        sample = [
            {
                "phase": "startup-idle",
                "unix": float(i),
                "services": {"db": {**service, "cpu": {"usage_usec": i * 1000}}},
            }
            for i in range(301)
        ]
        first = summarize_window(sample[:60], [], sample, [])
        final = summarize_window(sample[60:300], [], sample, [])
        assert first["memory_samples"] == 60 and final["memory_samples"] == 240
        assert final["cpu_seconds"] == 0.240
        assert final["counter_span_seconds"] == 240
        assert final["rates"]["cpu_seconds_per_second"]["p95"] == 0.001
        assert final["rates"]["cpu_seconds_per_second"]["samples"] == 240
        run = root / "current1"
        run.mkdir()
        (run / "memory.jsonl").write_text("\n".join(json.dumps(row) for row in sample))
        (run / "result.json").write_text(
            json.dumps(
                {
                    "services": {"db": {"image": "test"}},
                    "snapshots": [
                        {"phase": "startup-idle", "started_unix": 0, "unix": 300}
                    ],
                }
            )
        )
        clipped = analyze_run(run)
        assert clipped["phase_labelled_memory_samples"]["startup-idle"] == 301
        assert clipped["phases"]["startup-idle"]["all"]["memory_samples"] == 300
        fake = {
            "run": "current1",
            "complete": True,
            "phases": {"idle": {"all": first}},
            "latencies": [{"upload_to_metadata_and_search_seconds": 1}] * 3,
        }
        repeated = repeat_summary([fake, {"run": "current2", "complete": False}])
        assert repeated["current"]["n_runs"] == 1
        assert (
            repeated["current"]["phase_statistics"]["idle/all/current_mib/p50"][
                "median_of_run_statistics"
            ]
            == 1
        )


def main(source: Path, output: Path) -> None:
    """Write six-run diagnostics while preserving raw input and incomplete evidence."""
    output.mkdir(parents=True, exist_ok=True)
    runs = [analyze_run(source / name) for name in RUNS]
    result = {
        "method": "Complete-service samples only. Distributions are within each run; repeat summaries take medians of per-run statistics, never pool samples. Nominal-idle all windows are clipped to their recorded start/end, excluding sampler shutdown overruns retained in phase_labelled_memory_samples. Idle is split at 60s from its recorded start, with final240 covering [60,300)s. CPU/I/O deltas use the immediately preceding sample and can straddle boundaries by one sample. Process identity is PID plus kernel start ticks; missing short processes cannot be counted. Recovery counts are logged Celery task deliveries, not unlogged inline function invocations. Observer SQL contributes inside PostgreSQL; raw cgroup cache charges and PSS sharing depend on the host. Historical content markers and metadata-title convergence markers remain distinct.",
        "run_order": list(RUNS),
        "runs": runs,
        "repeats": repeat_summary(runs),
    }
    (output / "comparison-diagnostics.json").write_text(json.dumps(result, indent=2))
    sys.stdout.write(
        json.dumps(
            {
                "runs": [{"run": run["run"], "state": run["state"]} for run in runs],
                "repeat_counts": {
                    key: value["n_runs"] for key, value in result["repeats"].items()
                },
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
