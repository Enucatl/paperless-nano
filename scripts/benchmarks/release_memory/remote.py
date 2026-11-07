"""Stream read-only model-host measurements without changing its services."""

# ruff: noqa: INP001, T201

import csv
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def counters(path: Path) -> dict[str, int]:
    """Read the kernel's cumulative key/value counters."""
    return {
        key: int(value)
        for key, value in (line.split() for line in path.read_text().splitlines())
    }


def io_totals(text: str) -> dict[str, int]:
    """Sum cgroup I/O counters across its reported block devices."""
    totals = {}
    for line in text.splitlines():
        for item in line.split()[1:]:
            key, value = item.split("=", 1)
            totals[key] = totals.get(key, 0) + int(value)
    return totals


def gpu_processes(text: str, services: dict, proc: Path = Path("/proc")) -> list[dict]:
    """Attribute GPU compute allocations by host PID and cgroup ancestry."""
    rows = []
    for fields in csv.reader(io.StringIO(text)):
        if not fields:
            continue
        pid, memory, uuid = (field.strip() for field in fields)
        row = {
            "pid": int(pid),
            "used_mib": float(memory) if memory.replace(".", "", 1).isdigit() else None,
            "gpu_uuid": uuid,
        }
        try:
            relative = (proc / pid / "cgroup").read_text().split("0::", 1)[1].strip()
        except (FileNotFoundError, ProcessLookupError):
            row.update(service=None, role="unknown", exited=True)
        else:
            name = next(
                (
                    name
                    for name, info in services.items()
                    if relative == info["relative_cgroup"]
                    or relative.startswith(info["relative_cgroup"].rstrip("/") + "/")
                ),
                None,
            )
            row.update(
                service=name,
                role="bge" if name == "vllm-bge-1" else "ocr" if name else "other",
            )
        rows.append(row)
    return rows


def service_names(available: set[str]) -> list[str]:
    """Require the OCR stack and include the unrelated BGE service only if running."""
    names = [
        "vllm-paddleocr-gateway-1",
        "vllm-paddleocr-vl-api-1",
        "vllm-paddleocr-vlm-server-1",
    ]
    assert set(names) <= available, "All three OCR services must be running"
    if "vllm-bge-1" in available:
        names.append("vllm-bge-1")
    return names


def main(*, once: bool = False) -> None:
    """Sample service resources and separately attributed GPU allocations."""
    available = set(
        subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"], text=True, timeout=10
        ).splitlines()
    )
    names = service_names(available)
    records = json.loads(
        subprocess.check_output(["docker", "inspect", *names], timeout=10)
    )
    services = {}
    for record in records:
        pid = record["State"]["Pid"]
        relative = Path(f"/proc/{pid}/cgroup").read_text().split("0::", 1)[1].strip()
        services[record["Name"].lstrip("/")] = {
            "image": record["Image"],
            "cgroup": str(Path("/sys/fs/cgroup") / relative.lstrip("/")),
            "relative_cgroup": relative,
            "pid": pid,
        }
    print(json.dumps({"identities": services, "unix": time.time()}), flush=True)
    previous = {}
    previous_start = None
    while True:
        begin = time.monotonic()
        sample = {
            "unix": time.time(),
            "services": {},
            "interval_seconds": None
            if previous_start is None
            else begin - previous_start,
        }
        for name, info in services.items():
            path = Path(info["cgroup"])
            try:
                stat = counters(path / "memory.stat")
                current = int((path / "memory.current").read_text())
                cpu = counters(path / "cpu.stat")
                disk = io_totals((path / "io.stat").read_text())
                service = {
                    "current": current,
                    "peak": int((path / "memory.peak").read_text()),
                    "anon": stat["anon"],
                    "file": stat["file"],
                    "inactive_anon": stat["inactive_anon"],
                    "inactive_file": stat["inactive_file"],
                    "working_set": current - stat["inactive_file"],
                    "cpu": cpu,
                    "io": disk,
                    "memory_events": counters(path / "memory.events"),
                }
            except (FileNotFoundError, ProcessLookupError):
                sample.setdefault("unavailable", []).append(name)
                continue
            if name in previous:
                for key in ("cpu", "io"):
                    service[key + "_delta"] = {
                        counter: value - previous[name][key].get(counter, 0)
                        for counter, value in service[key].items()
                    }
            sample["services"][name] = service
            previous[name] = service
        gpu = (
            subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,utilization.gpu,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                timeout=10,
            )
            .strip()
            .split(",")
        )
        sample["gpu"] = {
            key: float(value)
            for key, value in zip(
                ("used_mib", "utilization_percent", "power_watts"), gpu, strict=True
            )
        }
        sample["gpu_processes"] = gpu_processes(
            subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,used_gpu_memory,gpu_uuid",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                timeout=10,
            ),
            services,
        )
        sample["gpu_process_mib_by_role"] = {
            role: sum(
                row["used_mib"] or 0
                for row in sample["gpu_processes"]
                if row["role"] == role
            )
            for role in ("ocr", "bge", "other", "unknown")
        }
        sample["sample_seconds"] = time.monotonic() - begin
        sample["ended_unix"] = time.time()
        print(json.dumps(sample), flush=True)
        if once:
            return
        previous_start = begin
        time.sleep(max(0, 1 - sample["sample_seconds"]))


def self_check() -> None:
    """Check I/O summation and cgroup mapping, including PID-exit races."""
    ocr = {
        "vllm-paddleocr-gateway-1",
        "vllm-paddleocr-vl-api-1",
        "vllm-paddleocr-vlm-server-1",
    }
    assert len(service_names(ocr)) == 3
    assert service_names(ocr | {"vllm-bge-1"})[-1] == "vllm-bge-1"
    assert io_totals("8:0 rbytes=12 wbytes=3\n8:1 rbytes=8 wbytes=4\n") == {
        "rbytes": 20,
        "wbytes": 7,
    }
    with tempfile.TemporaryDirectory() as directory:
        proc = Path(directory)
        for pid, relative in (
            ("10", "/docker/ocr/child"),
            ("11", "/docker/bge"),
            ("12", "/docker/ocr-unrelated"),
        ):
            (proc / pid).mkdir()
            (proc / pid / "cgroup").write_text("0::" + relative)
        rows = gpu_processes(
            "10, 7000, GPU-x\n11, 4000, GPU-x\n12, 100, GPU-x\n13, [N/A], GPU-x\n",
            {
                "ocr": {"relative_cgroup": "/docker/ocr"},
                "vllm-bge-1": {"relative_cgroup": "/docker/bge"},
            },
            proc,
        )
        assert [row["role"] for row in rows] == ["ocr", "bge", "other", "unknown"]
        assert (
            rows[0]["used_mib"] == 7000
            and rows[-1]["used_mib"] is None
            and rows[-1]["exited"]
        )


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
    else:
        main(once="--once" in sys.argv)
