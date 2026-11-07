"""Plot retained release diagnostics without changing historical artifacts."""

# ruff: noqa: INP001

import argparse
import json
import statistics
from pathlib import Path

MIB = 2**20
SERVICES = ("webserver", "db", "broker", "ai", "webhook-listener", "phoenix")
LABELS = {
    "webserver": "Paperless",
    "db": "PostgreSQL",
    "broker": "Redis",
    "ai": "AI service",
    "webhook-listener": "Listener",
    "phoenix": "Phoenix",
}
COLORS = {
    "webserver": "#3066be",
    "db": "#54a24b",
    "broker": "#e45756",
    "ai": "#b279a2",
    "webhook-listener": "#f2cf5b",
    "phoenix": "#72b7b2",
}


def rows(path: Path) -> list[dict]:
    """Read the retained JSONL records in observation order."""
    return [json.loads(line) for line in path.read_text().splitlines()]


def intervals(samples: list[dict]) -> list[tuple[str, float, float]]:
    """Return contiguous phase ranges rather than merging repeated phases."""
    result = []
    phase = samples[0]["phase"]
    begin = samples[0]["unix"]
    for current in samples[1:]:
        if current["phase"] != phase:
            result.append((phase, begin, current["unix"]))
            phase = current["phase"]
            begin = current["unix"]
    result.append((phase, begin, samples[-1]["unix"]))
    return result


def complete(samples: list[dict], expected: set[str]) -> list[dict]:
    """Exclude startup records that do not yet contain every measured service."""
    return [row for row in samples if set(row["services"]) == expected]


def annotate(axis, phases: list[tuple], origin: float) -> None:
    """Mark observed active phases while preserving real elapsed durations."""
    for name, begin, end in phases:
        if name.startswith("ingest") or name == "browse":
            axis.axvspan(
                (begin - origin) / 60,
                (end - origin) / 60,
                color="#e69f00",
                alpha=0.12,
                zorder=-1,
            )
    axis.grid(axis="y", color="#dddddd", linewidth=0.6)
    axis.spines[["top", "right"]].set_visible(False)


def idle_window(samples: list[dict], begin: float, end: float) -> list[dict]:
    """Select a planned final-idle interval using the recorded phase clock."""
    return [
        row
        for row in samples
        if row["phase"] == "final-idle" and begin <= row["unix"] < end
    ]


def cpu_per_minute(samples: list[dict]) -> float:
    """Normalize cumulative cgroup CPU usage to one minute of observation."""
    first, last = samples[0], samples[-1]
    delta = sum(
        last["services"][name]["cpu"]["usage_usec"]
        - first["services"][name]["cpu"]["usage_usec"]
        for name in first["services"]
    )
    return delta / 1e6 * 60 / (last["unix"] - first["unix"])


def comparison_summary(plt, source: Path, target: Path) -> None:
    """Show both repetitions and all document timings without hiding their spread."""
    figure, axes = plt.subplots(1, 3, figsize=(14, 5))
    variants = (
        ("baseline", "Legacy", "#3066be"),
        ("candidate", "Polling", "#b279a2"),
        ("current", "Current 60s", "#54a24b"),
    )
    output = {}
    for x, (variant, label, color) in enumerate(variants):
        runs = []
        for repeat in (1, 2):
            root = source / f"{variant}{repeat}"
            result = json.loads((root / "result.json").read_text())
            expected = set(result["services"])
            memory = complete(rows(root / "memory.jsonl"), expected)
            processes = complete(rows(root / "processes.jsonl"), expected)
            assert result["idle_seconds"] == 300
            begin = next(
                snapshot["started_unix"]
                for snapshot in result["snapshots"]
                if snapshot["phase"] == "final-idle"
            )
            value = {
                "repeat": repeat,
                "final_idle_started_unix": begin,
                "upload_to_metadata_title_search_seconds": [
                    item["upload_to_metadata_title_search_seconds"]
                    for item in result["workload"]
                ],
            }
            for window, start, end in (
                ("first60", begin, begin + 60),
                ("final240", begin + 60, begin + 300),
            ):
                selected_memory = idle_window(memory, start, end)
                selected_processes = idle_window(processes, start, end)
                value[f"final_idle_{window}_pss_median_mib"] = statistics.median(
                    sum(service["pss_mib"] for service in row["services"].values())
                    for row in selected_processes
                )
                value[f"final_idle_{window}_cpu_seconds_per_minute"] = cpu_per_minute(
                    selected_memory
                )
            runs.append(value)
            offset = -0.07 if repeat == 1 else 0.07
            marker = "o" if repeat == 1 else "s"
            for axis, key in (
                (axes[0], "final_idle_final240_pss_median_mib"),
                (axes[2], "final_idle_final240_cpu_seconds_per_minute"),
            ):
                axis.scatter(x + offset, value[key], color=color, marker=marker, s=50)
            for document, seconds in enumerate(
                value["upload_to_metadata_title_search_seconds"]
            ):
                axes[1].scatter(
                    x + offset + (document - 1) * 0.025,
                    seconds,
                    color=color,
                    marker=marker,
                    s=40,
                    alpha=0.8,
                )
        output[variant] = runs
    for axis, title, ylabel in (
        (axes[0], "Steady idle RAM: final 240s", "Process PSS (MiB)"),
        (axes[1], "Upload to current-title search", "Elapsed seconds"),
        (axes[2], "Steady idle CPU: final 240s", "CPU seconds / minute"),
    ):
        axis.set_title(title, loc="left", fontweight="bold")
        axis.set_ylabel(ylabel)
        axis.set_xticks(range(3), [label for _, label, _ in variants])
        axis.set_xlim(-0.5, 2.5)
        axis.set_ylim(bottom=0)
        axis.grid(axis="y", color="#dddddd", linewidth=0.6)
        axis.spines[["top", "right"]].set_visible(False)
    figure.suptitle(
        "Three versions · two successful repetitions each",
        x=0.07,
        ha="left",
        fontweight="bold",
        fontsize=14,
    )
    figure.text(
        0.07,
        0.025,
        "Circles: repetition 1. Squares: repetition 2. RAM and CPU use seconds 60-300 of the final idle window; latency shows all three uploads per run.\n"
        "CPU includes all services and scheduled work; observer SQL contributes only while tracing is active. 1 CPU-s/min = 1.67% of one core.\n"
        "The first 60 seconds are excluded to separate child retention from steady observation; both intervals remain in the numeric data.",
        fontsize=9,
        color="#444444",
    )
    figure.subplots_adjust(left=0.07, right=0.98, top=0.84, bottom=0.24, wspace=0.30)
    for extension in ("svg", "png"):
        figure.savefig(target / f"release-comparison-summary.{extension}", dpi=170)
    plt.close(figure)
    (target / "comparison-summary-series.json").write_text(json.dumps(output, indent=2))


def main() -> None:
    """Export service-memory and accounting figures plus their numeric series."""
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("/evidence"))
    parser.add_argument("--output", type=Path, default=Path("/output"))
    parser.add_argument("--comparison", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(exist_ok=True)
    plt.rcParams.update({"font.size": 10, "svg.fonttype": "none"})
    variants = [
        ("baseline1", "Legacy: six services"),
        ("candidate1", "Integrated: three services"),
    ]
    if args.comparison:
        variants = [
            ("baseline1", "Legacy: six services"),
            ("candidate1", "Polling: three services"),
            ("current1", "Current: 60s child retention"),
        ]
    figure, axes = plt.subplots(
        2, len(variants), figsize=(6.5 * len(variants), 8), sharex=True, sharey="row"
    )
    series = {}
    max_minute = 0
    for column, (name, title) in enumerate(variants):
        root = args.input / name
        result = json.loads((root / "result.json").read_text())
        expected = set(result["services"])
        memory = complete(rows(root / "memory.jsonl"), expected)
        processes = complete(rows(root / "processes.jsonl"), expected)
        assert len(memory) > 900 and len(processes) > 900
        origin = result["workload"][0]["admitted_unix"]
        phases = intervals(memory)
        x_pss = [(row["unix"] - origin) / 60 for row in processes]
        ordered = [service for service in SERVICES if service in expected]
        pss = {
            service: [row["services"][service]["pss_mib"] for row in processes]
            for service in ordered
        }
        axes[0, column].stackplot(
            x_pss,
            *pss.values(),
            colors=[COLORS[service] for service in ordered],
            labels=[LABELS[service] for service in ordered],
        )
        axes[0, column].set_title(title, loc="left", fontweight="bold")
        x_mem = [(row["unix"] - origin) / 60 for row in memory]
        accounting = {
            key: [
                sum(service[key] for service in row["services"].values()) / MIB
                for row in memory
            ]
            for key in ("current", "working_set", "anon")
        }
        for key, label, color in (
            ("current", "Raw cgroup (includes file cache)", "#777777"),
            ("working_set", "Working set", "#3066be"),
            ("anon", "Anonymous memory", "#c44e52"),
        ):
            axes[1, column].plot(
                x_mem, accounting[key], label=label, color=color, linewidth=1
            )
        for axis in axes[:, column]:
            annotate(axis, phases, origin)
            axis.set_ylim(bottom=0)
            axis.axvline(0, color="#333333", linestyle="--", linewidth=0.7)
        axes[1, column].set_xlabel("Minutes from first upload admission")
        max_minute = max(max_minute, x_mem[-1])
        series[name] = {
            "origin_unix": origin,
            "phases": phases,
            "pss_minutes": x_pss,
            "pss_mib_by_service": pss,
            "cgroup_minutes": x_mem,
            "cgroup_mib": accounting,
            "workload": [
                {
                    key: row[key]
                    for key in (
                        "pages",
                        "admitted_unix",
                        "completed_unix",
                        "upload_to_metadata_and_search_seconds",
                    )
                    + (
                        (
                            "metadata_title_search_completed_unix",
                            "upload_to_metadata_title_search_seconds",
                        )
                        if args.comparison
                        else ()
                    )
                }
                for row in result["workload"]
            ],
        }
        if args.comparison:
            series[name]["processor_mode"] = result["processor_mode"]
            series[name]["processor_idle_timeout_seconds"] = result[
                "processor_idle_timeout_seconds"
            ]
    axes[0, 0].set_ylabel("Process PSS (MiB)")
    axes[1, 0].set_ylabel("Cgroup memory (MiB)")
    axes[0, 0].legend(loc="upper left", ncol=2, framealpha=0.9, fontsize=9)
    axes[1, 0].legend(loc="upper left", framealpha=0.9, fontsize=9)
    axes[1, 0].set_xlim(-5.6, max_minute + 0.2)
    figure.suptitle(
        "First repetition per version · identical successful 1-page, 3-page, then 1-page PDFs"
        if args.comparison
        else "One completed matched pair · identical 1-page, 3-page, then 1-page PDFs",
        x=0.07,
        ha="left",
        fontweight="bold",
        fontsize=14,
    )
    figure.text(
        0.07,
        0.025,
        (
            "Orange bands: named upload/browse phases. Idle starts after metadata-title search converges; lifecycle tracing follows the capture's recorded pause settings.\n"
            "Real elapsed time is retained. Current retains a processor child for up to 60 seconds after work. PSS sampling may miss brief peaks.\n"
            "This figure shows repetition 1 only; the companion summary shows both repetitions. Individual lifetime peaks are not summed into these curves."
        )
        if args.comparison
        else (
            "Orange bands: named upload/browse phases. Nominal idle may include queued completion and scheduled work.\n"
            "Real elapsed time is retained: the candidate spends longer processing. PSS is sampled; short child-process peaks may be missed.\n"
            "The interrupted candidate repetition is excluded. Individual container lifetime peaks are not summed into these curves."
        ),
        fontsize=9,
        color="#444444",
    )
    figure.subplots_adjust(
        left=0.07, right=0.98, top=0.90, bottom=0.15, hspace=0.24, wspace=0.10
    )
    stem = (
        "release-comparison-timeseries"
        if args.comparison
        else "release-memory-timeseries"
    )
    for extension in ("svg", "png"):
        figure.savefig(args.output / f"{stem}.{extension}", dpi=170)
    plt.close(figure)
    (args.output / "plot-series.json").write_text(
        json.dumps(series, separators=(",", ":"))
    )
    if args.comparison:
        comparison_summary(plt, args.input, args.output)


def self_check() -> None:
    """Verify that phase splitting and complete-sample filtering preserve facts."""
    samples = [
        {"unix": 1, "phase": "idle", "services": {"a": {}}},
        {"unix": 2, "phase": "active", "services": {"a": {}, "b": {}}},
        {"unix": 3, "phase": "idle", "services": {"a": {}, "b": {}}},
        {"unix": 4, "phase": "idle", "services": {"a": {}, "b": {}}},
    ]
    assert intervals(samples) == [("idle", 1, 2), ("active", 2, 3), ("idle", 3, 4)]
    assert complete(samples, {"a", "b"}) == samples[1:]
    cpu = [
        {"unix": t, "services": {"app": {"cpu": {"usage_usec": u}}}}
        for t, u in ((0, 1_000_000), (30, 3_000_000))
    ]
    assert cpu_per_minute(cpu) == 4
    idle = [{"unix": t, "phase": "final-idle"} for t in (0, 100, 150, 200)]
    assert idle_window(idle, 60, 300) == idle[1:]
    assert idle_window(idle, 0, 60) == idle[:1]


if __name__ == "__main__":
    import sys

    if "--self-check" in sys.argv:
        self_check()
    else:
        main()
