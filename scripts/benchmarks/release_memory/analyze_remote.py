"""Summarize remote resources within the completed matched-pair time windows."""

import json
import statistics
import sys
from pathlib import Path


def distribution(values: list[float]) -> dict:
    """Describe samples without treating lifetime peaks as run-local peaks."""
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def main() -> None:
    """Write measured remote CPU, RAM, GPU and observer summaries as JSON."""
    root = Path("/evidence")
    records = [
        json.loads(line)
        for line in (root / "remote-memory.jsonl").read_text().splitlines()
    ]
    samples = [row for row in records if "gpu" in row]
    result = {
        "identities": records[0]["identities"],
        "samples": len(samples),
        "first_unix": samples[0]["unix"],
        "last_unix": samples[-1]["unix"],
        "observer_seconds": distribution([row["sample_seconds"] for row in samples]),
        "gpu_mib_by_role": {
            role: distribution(
                [row["gpu_process_mib_by_role"][role] for row in samples]
            )
            for role in ("ocr", "bge", "other", "unknown")
        },
        "whole_gpu": {
            key: distribution([row["gpu"][key] for row in samples])
            for key in samples[0]["gpu"]
        },
        "clock_probes": json.loads((root / "clock-probes.json").read_text()),
        "errors": (root / "remote-error.txt").read_text(),
        "completed_runs": {},
    }
    for run in sorted(
        path.name for path in root.iterdir() if (path / "result.json").is_file()
    ):
        local = [
            json.loads(line)
            for line in (root / run / "memory.jsonl").read_text().splitlines()
        ]
        expected = 6 if run.startswith("baseline") else 3
        local = [row for row in local if len(row["services"]) == expected]
        phases = {}
        for phase in sorted({row["phase"] for row in local}):
            points = [row for row in local if row["phase"] == phase]
            selected = [
                row
                for row in samples
                if points[0]["unix"] <= row["unix"] <= points[-1]["unix"]
            ]
            if not selected:
                continue
            services = {}
            for name in selected[0]["services"]:
                values = [row["services"][name] for row in selected]
                services[name] = {
                    "working_set_mib": distribution(
                        [row["working_set"] / 2**20 for row in values]
                    ),
                    "anonymous_mib": distribution(
                        [row["anon"] / 2**20 for row in values]
                    ),
                    "raw_mib": distribution([row["current"] / 2**20 for row in values]),
                    "cpu_seconds": (
                        values[-1]["cpu"]["usage_usec"] - values[0]["cpu"]["usage_usec"]
                    )
                    / 1e6,
                    "read_bytes": values[-1]["io"].get("rbytes", 0)
                    - values[0]["io"].get("rbytes", 0),
                    "written_bytes": values[-1]["io"].get("wbytes", 0)
                    - values[0]["io"].get("wbytes", 0),
                }
            phases[phase] = {
                "samples": len(selected),
                "started_unix": selected[0]["unix"],
                "ended_unix": selected[-1]["unix"],
                "services": services,
                "whole_gpu_utilization_percent": distribution(
                    [row["gpu"]["utilization_percent"] for row in selected]
                ),
            }
        result["completed_runs"][run] = phases
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        assert distribution([3, 1, 5]) == {"min": 1, "median": 3, "max": 5}
        assert distribution([1, 3])["median"] == 2
    else:
        main()
