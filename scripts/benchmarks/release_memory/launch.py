"""Orchestrate isolated measurement containers using the host's SSH identity."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import baseline

IMAGE = "paperless-release49-inference:secrets-validation"
CURRENT_IMAGE = "paperless-release-current:55928c30e"
SECRET = "pm-release-model-secret"
HARNESS = "pm-release-harness-snapshot"
CONTROLLER = "pm-release-controller"


def command(*args: str, input: bytes | None = None) -> bytes:
    """Run a bounded setup command, propagating errors without printing input."""
    return subprocess.run(args, input=input, capture_output=True, check=True).stdout


def fingerprint(record: dict) -> str:
    """Compare transferred image configuration and uncompressed root layers."""
    value = {key: record[key] for key in ("Config", "RootFS", "Architecture", "Os")}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(evidence: str, name: str, data: bytes) -> None:
    """Write evidence directly into Docker storage."""
    command(
        "docker",
        "run",
        "--rm",
        "--userns=host",
        "-i",
        "-v",
        f"{evidence}:/evidence",
        "--entrypoint",
        "sh",
        IMAGE,
        "-c",
        f"cat > /evidence/{name}",
        input=data,
    )


def clock_probes() -> list[dict]:
    """Bound model-host clock offsets by local request/response timestamps."""
    probes = []
    for _ in range(3):
        before = time.time()
        remote = float(
            command(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "cerebrum.home.arpa",
                "python3 -c 'import time; print(time.time())'",
            )
        )
        after = time.time()
        probes.append(
            {
                "local_started_unix": before,
                "local_finished_unix": after,
                "remote_unix": remote,
                "remote_minus_local_bounds_seconds": [remote - after, remote - before],
            }
        )
    return probes


def main() -> None:
    """Validate image transfer, stream remote samples and run the controller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_volume")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    evidence = args.evidence_volume
    existing = subprocess.run(
        ["docker", "volume", "inspect", evidence], capture_output=True, check=False
    )
    assert existing.returncode != 0, "Use a fresh evidence volume"
    command("docker", "volume", "create", evidence)
    directory = Path(__file__).resolve().parent
    identities = {}
    for service, source in baseline.SOURCE_IMAGES.items():
        original = json.loads(
            command(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "atlas.home.arpa",
                f"docker image inspect {source}",
            )
        )[0]
        local = json.loads(
            command("docker", "image", "inspect", baseline.IMAGES[service])
        )[0]
        assert fingerprint(original) == fingerprint(local), (
            f"Transferred {service} differs from retained source"
        )
        identities[service] = {
            "source_config_id": source,
            "local_image_id": local["Id"],
            "verified_config_and_layers_sha256": fingerprint(local),
        }
    save(evidence, "source-identities.json", json.dumps(identities, indent=2).encode())
    current = json.loads(command("docker", "image", "inspect", CURRENT_IMAGE))[0]
    save(
        evidence,
        "current-image.json",
        json.dumps(
            {
                "image": current["Id"],
                "labels": current["Config"].get("Labels", {}),
                "rootfs": current["RootFS"],
            },
            indent=2,
        ).encode(),
    )
    save(evidence, "launch.py", Path(__file__).read_bytes())
    info = json.loads(command("docker", "info", "--format", "{{json .}}"))
    save(
        evidence,
        "host.json",
        json.dumps(
            {
                key: info[key]
                for key in (
                    "NCPU",
                    "MemTotal",
                    "KernelVersion",
                    "OperatingSystem",
                    "Architecture",
                    "CgroupVersion",
                    "ServerVersion",
                )
            },
            indent=2,
        ).encode(),
    )
    clocks = {"before": clock_probes()}
    save(evidence, "clock-probes.json", json.dumps(clocks, indent=2).encode())
    command("docker", "volume", "create", SECRET)
    remote = None
    collector = None
    try:
        command("docker", "volume", "create", HARNESS)
        command(
            "docker",
            "run",
            "--rm",
            "--userns=host",
            "-v",
            f"{directory}:/input:ro",
            "-v",
            f"{HARNESS}:/harness",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "cp -a /input/. /harness/",
        )
        key = Path(
            "/export/atlas/paperless-ai/secrets/openrouter_api_key.txt"
        ).read_bytes()
        command(
            "docker",
            "run",
            "--rm",
            "--userns=host",
            "-i",
            "-v",
            f"{SECRET}:/secret",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "umask 077; cat > /secret/openrouter_api_key",
            input=key,
        )
        del key
        collector = subprocess.Popen(
            [
                "docker",
                "run",
                "--rm",
                "--userns=host",
                "-i",
                "-v",
                f"{evidence}:/evidence",
                "--entrypoint",
                "sh",
                IMAGE,
                "-c",
                "cat > /evidence/remote-memory.jsonl",
            ],
            stdin=subprocess.PIPE,
        )
        remote = subprocess.Popen(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "cerebrum.home.arpa",
                "python3 -u -",
            ],
            stdin=subprocess.PIPE,
            stdout=collector.stdin,
            stderr=subprocess.PIPE,
        )
        collector.stdin.close()
        remote.stdin.write(
            command(
                "docker",
                "run",
                "--rm",
                "--userns=host",
                "-v",
                f"{HARNESS}:/harness:ro",
                "--entrypoint",
                "cat",
                IMAGE,
                "/harness/remote.py",
            )
        )
        remote.stdin.close()
        cmd = [
            "docker",
            "run",
            "--rm",
            "--userns=host",
            "--name",
            CONTROLLER,
            "--pid=host",
            "--cgroupns=host",
            "--cap-add",
            "SYS_PTRACE",
            "--no-healthcheck",
            "-e",
            f"BENCH_CONTROLLER={CONTROLLER}",
            "-e",
            f"BENCH_HARNESS_VOLUME={HARNESS}",
            "-e",
            f"BENCH_CURRENT_IMAGE={CURRENT_IMAGE}",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            "/usr/bin/docker:/usr/bin/docker:ro",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            "/sys/fs/cgroup:/host-cgroup:ro",
            "-v",
            f"{HARNESS}:/harness:ro",
            "-v",
            f"{SECRET}:/model-secret:ro",
            "-v",
            f"{evidence}:/evidence",
            "--entrypoint",
            "python",
        ]
        if args.preflight:
            cmd += ["-e", "BENCH_IDLE_SECONDS=1"]
        cmd += [IMAGE, "/harness/run.py"]
        if args.preflight:
            cmd += ["--preflight"]
        subprocess.run(cmd, check=True)
        assert remote.poll() is None, "Remote sampling failed; inspect remote-error.txt"
        clocks["after"] = clock_probes()
        save(evidence, "clock-probes.json", json.dumps(clocks, indent=2).encode())
    finally:
        if remote:
            remote.terminate()
            remote.wait(timeout=20)
            save(evidence, "remote-error.txt", remote.stderr.read())
        if collector:
            collector.wait(timeout=20)
        command("docker", "volume", "rm", SECRET)
        command("docker", "volume", "rm", HARNESS)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        record = {
            "Config": {"Env": ["A=1"]},
            "RootFS": {"Layers": ["one"]},
            "Architecture": "amd64",
            "Os": "linux",
        }
        assert fingerprint(record) == fingerprint(
            {**record, "Id": "different-store-id"}
        )
        assert fingerprint(record) != fingerprint(
            {**record, "RootFS": {"Layers": ["other"]}}
        )
        print("Transferred image identity checks passed")
    else:
        main()
