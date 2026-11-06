"""Compare isolated legacy and integrated OCR stacks on identical synthetic PDFs."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import baseline

ROOT = Path("/harness")
OUT = Path("/evidence")
IMAGE = "sha256:6824eec4681c4087dbff484efab2b1da399c7b0e3adfd614ed78afbabfa6fc1a"
CURRENT_IMAGE = os.environ.get(
    "BENCH_CURRENT_IMAGE", "paperless-release-current:55928c30e"
)
DATABASE = "postgres:18"
BROKER = "docker.io/library/redis:8"
IDLE = int(os.environ.get("BENCH_IDLE_SECONDS", "300"))
TOKEN = secrets.token_hex(20)
PASSWORD = secrets.token_hex(16)
PREFIX = "pm-release"
SERVICES = {}
LOGGERS = {}
VOLUMES = []
PHASE = "setup"
STOP = threading.Event()
SAMPLES = []
LATENCIES = []
RUN_OUT = OUT


def docker(*args: str, input: str | bytes | None = None) -> str:
    """Run Docker while keeping failure details in the evidence volume."""
    result = subprocess.run(
        ["docker", *args],
        input=input,
        capture_output=True,
        text=not isinstance(input, bytes),
        check=False,
    )
    stdout = (
        result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    )
    stderr = (
        result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
    )
    if result.returncode:
        (RUN_OUT / "command-error.txt").write_text(stdout + stderr)
        raise RuntimeError(f"docker {args[0]} failed; see private command-error.txt")
    return stdout


def env_args(env: dict) -> list[str]:
    """Pass effective configuration without writing credentials to evidence."""
    return [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]


def volume(role: str) -> str:
    """Create one disposable volume owned by this experiment."""
    name = f"{PREFIX}-{role}"
    docker("volume", "create", name)
    VOLUMES.append(name)
    return name


def start(
    service: str,
    image: str,
    env: dict,
    mounts: list,
    args: tuple = (),
    entrypoint: str | None = None,
) -> None:
    """Start one measured service and retain its immutable identity and cgroup."""
    name = f"{PREFIX}-{service}"
    command = [
        "run",
        "--rm",
        "--userns=host",
        "--detach",
        "--name",
        name,
        "--network",
        PREFIX,
        "--network-alias",
        service,
        "--no-healthcheck",
    ]
    for source, target in mounts:
        command += ["-v", f"{source}:{target}"]
    if entrypoint:
        command += ["--entrypoint", entrypoint]
    command += [*env_args(env), image, *args]
    docker(*command)
    record = json.loads(docker("inspect", name))[0]
    pid = record["State"]["Pid"]
    relative = Path(f"/proc/{pid}/cgroup").read_text().split("0::")[1].strip()
    SERVICES[service] = {
        "name": name,
        "image": record["Image"],
        "pid": pid,
        "cgroup": str(Path("/host-cgroup") / relative.lstrip("/")),
        "limits": {
            key: record["HostConfig"][key]
            for key in (
                "Memory",
                "MemorySwap",
                "NanoCpus",
                "CpuShares",
                "CpuQuota",
                "CpuPeriod",
            )
        },
    }
    log = (RUN_OUT / f"{service}.log").open("w")
    logger = subprocess.Popen(
        ["docker", "logs", "--follow", name], stdout=log, stderr=log
    )
    LOGGERS[service] = (logger, log)


def helper(image: str, env: dict, mounts: list, args: list) -> str:
    """Run setup outside measured phases in a disposable container."""
    command = [
        "run",
        "--rm",
        "--userns=host",
        "--network",
        PREFIX,
        "--entrypoint",
        "python",
    ]
    for source, target in mounts:
        command += ["-v", f"{source}:{target}"]
    return docker(*command, *env_args(env), image, *args)


def sample() -> None:
    """Capture simultaneous local cgroup totals at one-second resolution."""
    with (
        (RUN_OUT / "memory.jsonl").open("w") as output,
        (RUN_OUT / "processes.jsonl").open("w") as processes,
    ):
        while not STOP.is_set():
            begin = time.monotonic()
            row = {"unix": time.time(), "phase": PHASE, "services": {}}
            for service, info in list(SERVICES.items()):
                path = Path(info["cgroup"])
                try:
                    stat = dict(
                        line.split()
                        for line in (path / "memory.stat").read_text().splitlines()
                    )
                    current = int((path / "memory.current").read_text())
                    row["services"][service] = {
                        "current": current,
                        "peak": int((path / "memory.peak").read_text()),
                        "anon": int(stat["anon"]),
                        "file": int(stat["file"]),
                        "working_set": current - int(stat["inactive_file"]),
                        "cpu": {
                            key: int(value)
                            for key, value in (
                                line.split()
                                for line in (path / "cpu.stat").read_text().splitlines()
                            )
                        },
                        "io": io_totals((path / "io.stat").read_text()),
                        "events": dict(
                            line.split()
                            for line in (path / "memory.events")
                            .read_text()
                            .splitlines()
                        ),
                    }
                except FileNotFoundError:
                    row.setdefault("unavailable", []).append(service)
            SAMPLES.append(row)
            process_row = {
                "unix": time.time(),
                "phase": row["phase"],
                "services": pss(),
            }
            processes.write(json.dumps(process_row) + "\n")
            processes.flush()
            row["observer_seconds"] = time.monotonic() - begin
            row["host_loadavg"] = Path("/proc/loadavg").read_text().split()[:3]
            row["host_pressure"] = {
                key: (Path("/proc/pressure") / key).read_text().splitlines()
                for key in ("cpu", "memory", "io")
            }
            row["finished_unix"] = time.time()
            output.write(json.dumps(row) + "\n")
            output.flush()
            STOP.wait(max(0, 1 - (time.monotonic() - begin)))


def io_totals(value: str) -> dict:
    """Aggregate block-device I/O counters without persisting device paths."""
    totals = {}
    for line in value.splitlines():
        for item in line.split()[1:]:
            key, amount = item.split("=")
            totals[key] = totals.get(key, 0) + int(amount)
    return totals


def process_role(command: str) -> str:
    """Classify a process without retaining its potentially sensitive arguments."""
    if "paperless_services.launcher processor" in command:
        return "processor-parent"
    for role in (
        "document_processor",
        "paperless_services.processor",
        "paperless_services.watcher",
        "paperless_services.scheduler",
        "paperless_worker",
        "celery",
        "supercronic",
        "copilot_server",
        "paperless_listener",
        "phoenix",
        "granian",
        "spawn_main",
        "resource_tracker",
        "postgres",
        "redis-server",
        "ghostscript",
        "java",
        "s6-",
    ):
        if role in command:
            return role
    if command.split()[:1] == ["gs"]:
        return "ghostscript"
    return "other"


def pss() -> dict:
    """Read proportional process memory, recording short-lived process races."""
    result = {}
    for service, info in list(SERVICES.items()):
        pids = set()
        for path in Path(info["cgroup"]).rglob("cgroup.procs"):
            pids.update(path.read_text().split())
        rows = []
        for pid in pids:
            try:
                stat = dict(
                    line.split(":", 1)
                    for line in Path(f"/proc/{pid}/smaps_rollup")
                    .read_text()
                    .splitlines()
                    if ":" in line
                )
                identity = (
                    Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                )
                command = (
                    Path(f"/proc/{pid}/cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode(errors="replace")
                )
                rows.append(
                    {
                        "pid": int(pid),
                        "name": Path(f"/proc/{pid}/comm").read_text().strip(),
                        "pss_kib": int(stat["Pss"].split()[0]),
                        "pss_anon_kib": int(stat.get("Pss_Anon", "0").split()[0]),
                        "pss_file_kib": int(stat.get("Pss_File", "0").split()[0]),
                        "pss_shmem_kib": int(stat.get("Pss_Shmem", "0").split()[0]),
                        "private_dirty_kib": int(stat["Private_Dirty"].split()[0]),
                        "rss_kib": int(stat["Rss"].split()[0]),
                        "swap_kib": int(stat["Swap"].split()[0]),
                        "role": process_role(command),
                        "ppid": int(identity[1]),
                        "start_ticks": int(identity[19]),
                        "cpu_ticks": int(identity[11]) + int(identity[12]),
                        "minor_faults": int(identity[7]),
                        "major_faults": int(identity[9]),
                    }
                )
            except (FileNotFoundError, ProcessLookupError):
                rows.append({"pid": int(pid), "exited": True})
        result[service] = {
            "processes": rows,
            "pss_mib": sum(row.get("pss_kib", 0) for row in rows) / 1024,
        }
    return result


def api(path: str, *, method: str = "GET", body=None, headers=None, raw: bool = False):
    """Call the isolated normal HTTP API and retain timing without payloads."""
    request_headers = {"Authorization": f"Token {TOKEN}", **(headers or {})}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    begin = time.monotonic()
    request = urllib.request.Request(
        "http://webserver:8000" + path,
        data=body,
        method=method,
        headers=request_headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
        LATENCIES.append(
            {
                "phase": PHASE,
                "path": path,
                "seconds": time.monotonic() - begin,
                "started_unix": time.time() - (time.monotonic() - begin),
                "status": response.status,
                "bytes": len(payload),
            }
        )
    return payload if raw else json.loads(payload)


def wait_ready() -> None:
    """Wait for authenticated application readiness with a bounded deadline."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            api("/api/documents/?page_size=1")
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise RuntimeError("Application readiness exceeded 180 seconds")


def pdf(identifier: int, pages: int) -> bytes:
    """Create repeatable invoice PDFs without an extra generation dependency."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for page in range(pages):
        page_id = len(objects) + 1
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        lines = [
            "Alpine Research Supplies",
            f"INVOICE BENCHMARK {identifier} PAGE {page + 1}",
            "Invoice date: 2026-10-15",
            "Customer: Synthetic Example Ltd",
            "Description: laboratory notebooks",
            "Quantity: 12",
            "Total CHF 240.00",
            "Payment due: 2026-11-15",
            "Reference: benchmarksearchable",
        ]
        content = (
            "BT /F1 16 Tf 50 780 Td "
            + " 0 -32 Td ".join(f"({line}) Tj" for line in lines)
            + " ET"
        ).encode()
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream"
        )
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>".encode()
    data = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        data
        + f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )


def fields(document: dict) -> dict:
    """Return the current field values keyed by integer ID."""
    return {row["field"]: row["value"] for row in document["custom_fields"]}


def task_failed(task: dict) -> bool:
    """Detect terminal receipt or operation failure without depending on their agreement."""
    return task.get("status") in {"failure", "revoked"} or (
        task.get("operation") or {}
    ).get("status") in {"failed", "unknown", "conflicted", "cancelled"}


def check_task(task_id: str) -> None:
    """Stop a failed upload promptly while retaining operation details in service evidence."""
    response = api("/api/tasks/?task_id=" + urllib.parse.quote(task_id))
    rows = response.get("results", []) if isinstance(response, dict) else response
    if any(row["task_id"] == task_id and task_failed(row) for row in rows):
        raise RuntimeError(
            f"Upload task {task_id} has a terminal task/operation failure"
        )


def ingest(variant: str, identifier: int, pages: int, resources: dict) -> dict:
    """Measure upload through OCR, metadata, downloadable archive and search."""
    data = pdf(identifier, pages)
    boundary = "paperless-benchmark-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="benchmark-{identifier}.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    previous = {row["id"] for row in api("/api/documents/?page_size=100")["results"]}
    begin = time.monotonic()
    admitted_unix = time.time()
    task = api(
        "/api/documents/post_document/",
        method="POST",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    deadline = begin + 900
    document = None
    while time.monotonic() < deadline:
        check_task(task)
        rows = api("/api/documents/?page_size=100")["results"]
        for row in rows:
            if row["id"] not in previous:
                if variant == "baseline":
                    complete = baseline.completed(row, resources)
                else:
                    values = fields(row)
                    complete = all(values.get(key) for key in (1, 2, 3, 4)) and row.get(
                        "correspondent"
                    )
                if complete and "benchmarksearchable" in row["content"].lower():
                    document = row
                    break
        if document:
            matches = api(
                "/api/documents/?query="
                + urllib.parse.quote("content:benchmarksearchable")
            )["results"]
            if document["id"] in {row["id"] for row in matches}:
                break
        time.sleep(1)
    else:
        raise RuntimeError(f"{variant} full pipeline exceeded 900s; task={task}")
    elapsed = time.monotonic() - begin
    pipeline_completed_unix = time.time()
    document = api(f"/api/documents/{document['id']}/")
    title_query = "title:" + json.dumps(document["title"], ensure_ascii=False)
    while time.monotonic() < deadline:
        matches = api("/api/documents/?query=" + urllib.parse.quote(title_query))[
            "results"
        ]
        if document["id"] in {row["id"] for row in matches}:
            break
        check_task(task)
        time.sleep(1)
    else:
        raise RuntimeError(
            f"{variant} metadata title search did not converge; task={task}"
        )
    title_search_completed_unix = time.time()
    title_search_seconds = time.monotonic() - begin
    original = api(f"/api/documents/{document['id']}/download/?original=true", raw=True)
    archive = api(f"/api/documents/{document['id']}/download/?original=false", raw=True)
    assert hashlib.sha256(original).digest() == hashlib.sha256(data).digest()
    assert archive.startswith(b"%PDF") and archive != original
    meta = api(f"/api/documents/{document['id']}/metadata/")
    assert meta["has_archive_version"]
    provenance_id = resources["fields"]["ocr"] if variant == "baseline" else 4
    provenance = fields(document)[provenance_id]
    if isinstance(provenance, str):
        provenance = json.loads(provenance)
    result = {
        "document_id": document["id"],
        "pages": pages,
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "upload_to_metadata_and_search_seconds": elapsed,
        "archive_bytes": len(archive),
        "original_bytes": len(original),
        "provenance": provenance,
        "completed_unix": pipeline_completed_unix,
        "verification_finished_unix": time.time(),
        "admitted_unix": admitted_unix,
        "metadata_title_search_completed_unix": title_search_completed_unix,
        "upload_to_metadata_title_search_seconds": title_search_seconds,
        "original_preserved": True,
        "archive_downloaded": True,
        "search_verified": True,
    }
    with (RUN_OUT / "workload.jsonl").open("a") as output:
        output.write(json.dumps(result) + "\n")
    return result


def observe(phase: str) -> dict:
    """Observe one unchanged running stack for the full idle window."""
    global PHASE
    PHASE = phase
    begin = time.time()
    time.sleep(IDLE)
    return {"phase": phase, "started_unix": begin, "unix": time.time(), "pss": pss()}


SEED = """
import os
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from documents.models import CustomField
from django.core.management import call_command
user=User.objects.create_superuser(username="benchadmin", password="isolated-benchmark-only", email="benchmark@example.invalid")
Token.objects.create(user=user,key=os.environ["BENCH_TOKEN"])
for name,kind in [("summary","longtext"),("ai_processed","date"),("ai_metadata","longtext"),("ai_ocr_output","longtext")]:
    CustomField.objects.create(name=name,data_type=kind)
call_command("document_processor_account", "--user", "server-processor", "--capability", "parser", "--capability", "metadata", "--capability", "search", "--capability", "ingestion")
"""


def run(variant: str, number: int) -> dict:
    """Run one fresh stack; always remove only its disposable resources."""
    global PREFIX, RUN_OUT, PHASE
    PREFIX = f"pm-release-{variant}{number}"
    RUN_OUT = OUT / f"{variant}{number}"
    RUN_OUT.mkdir()
    PHASE = "setup"
    SERVICES.clear()
    LOGGERS.clear()
    VOLUMES.clear()
    SAMPLES.clear()
    LATENCIES.clear()
    STOP.clear()
    docker("network", "create", PREFIX)
    # The controller joins only this disposable network for normal HTTP requests.
    docker("network", "connect", PREFIX, os.environ["BENCH_CONTROLLER"])
    observer = None
    jobs_observer = None
    jobs_output = None
    jobs_error = None
    try:
        data, media, consume, export, dbdata, brokerdata, secret = [
            volume(role)
            for role in (
                "data",
                "media",
                "consume",
                "export",
                "db",
                "broker",
                "secrets",
            )
        ]
        mounts = [
            (data, "/usr/src/paperless/data"),
            (media, "/usr/src/paperless/media"),
            (consume, "/usr/src/paperless/consume"),
            (export, "/usr/src/paperless/export"),
            (secret, "/run/secrets:ro"),
        ]
        # Read the live model credential only into a private disposable secret volume.
        docker(
            "run",
            "--rm",
            "--userns=host",
            "-v",
            f"{secret}:/secrets",
            "-i",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "cat > /secrets/openrouter_api_key; chown 1000:1000 /secrets/openrouter_api_key; chmod 755 /secrets; chmod 400 /secrets/openrouter_api_key",
            input=Path("/model-secret/openrouter_api_key").read_bytes(),
        )
        if variant == "baseline":
            docker(
                "run",
                "--rm",
                "--userns=host",
                "-v",
                f"{secret}:/secrets",
                "--entrypoint",
                "sh",
                IMAGE,
                "-c",
                "chown 10001:10001 /secrets/openrouter_api_key",
            )
        # Pass the fresh API token privately to the volume initializer, never to evidence.
        docker(
            "run",
            "--rm",
            "--userns=host",
            "-e",
            f"BENCH_TOKEN={TOKEN}",
            "-v",
            f"{secret}:/secrets",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            'printf %s "$BENCH_TOKEN" > /secrets/paperless_token; chown 1000:1000 /secrets/paperless_token; chmod 400 /secrets/paperless_token',
        )
        start(
            "db",
            DATABASE,
            {
                "POSTGRES_DB": "paperless",
                "POSTGRES_USER": "paperless",
                "POSTGRES_PASSWORD": PASSWORD,
            },
            [(dbdata, "/var/lib/postgresql")],
        )
        start("broker", BROKER, {}, [(brokerdata, "/data")])
        for _ in range(60):
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    SERVICES["db"]["name"],
                    "pg_isready",
                    "-U",
                    "paperless",
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL did not become ready")
        common = {
            "PAPERLESS_DBENGINE": "postgresql",
            "PAPERLESS_DBHOST": "db",
            "PAPERLESS_DBNAME": "paperless",
            "PAPERLESS_DBUSER": "paperless",
            "PAPERLESS_DBPASS": PASSWORD,
            "PAPERLESS_REDIS": "redis://broker:6379",
            "PAPERLESS_SECRET_KEY": PASSWORD,
            "PAPERLESS_URL": "http://webserver:8000",
            "PAPERLESS_ALLOWED_HOSTS": "*",
            "PAPERLESS_TIME_ZONE": "UTC",
            "PAPERLESS_TASK_WORKERS": "1",
            "PAPERLESS_THREADS_PER_WORKER": "2",
            "PAPERLESS_WEBSERVER_WORKERS": "1",
            "PAPERLESS_TRAIN_TASK_CRON": "disable",
            "PAPERLESS_CLASSIFIER_ENABLED": "false",
            "PAPERLESS_AI_ALLOWED": "false",
            "PAPERLESS_API_TOKEN_FILE": "/run/secrets/paperless_token",
            "BENCH_TOKEN": TOKEN,
        }
        inference = {
            "INFERENCE_OCR_ENDPOINT": "http://cerebrum.home.arpa:8108",
            "INFERENCE_OCR_PIPELINE": "PaddleOCR-VL-1.6",
            "INFERENCE_OCR_MODEL": "PaddlePaddle/PaddleOCR-VL-1.6",
            "INFERENCE_OCR_LAYOUT_MODEL": "PP-DocLayoutV3",
            "INFERENCE_OCR_TIMEOUT": "600",
            "INFERENCE_METADATA_ENDPOINT": "https://openrouter.ai/api/v1",
            "INFERENCE_METADATA_MODEL": "inception/mercury-2.5",
            "INFERENCE_METADATA_TEMPERATURE": "0",
            "OPENROUTER_API_KEY_FILE": "/run/secrets/openrouter_api_key",
        }
        if variant == "baseline":
            image = baseline.IMAGES["webserver"]
            env = baseline.core_environment(common, TOKEN)
            seed = baseline.SEED_PY
        else:
            image = CURRENT_IMAGE if variant == "current" else IMAGE
            env = {
                **common,
                **inference,
                "PAPERLESS_CONSUMER_CLASS": "paperless_services.watcher.APIConsumer",
                "PAPERLESS_SCHEDULER_CLASS": "paperless_services.scheduler.APIScheduler",
                "PAPERLESS_WORKER_CLASS": "paperless_worker.bootstrap.LeanWorker",
                "PAPERLESS_PARSER": "paperless_inference.providers.PaddleOCRProvider",
                "PAPERLESS_ENRICHMENT_PROVIDER": "paperless_inference.providers.MetadataProvider",
                "PAPERLESS_ENRICHMENT_ALLOW_REMOTE": "true",
                "PAPERLESS_ENRICHMENT_FIELDS": '{"summary":1,"processing_date":2,"audit":3}',
                "PAPERLESS_OCR_PROVENANCE_FIELD": "4",
                "PAPERLESS_PROCESSOR_USER": "server-processor",
                "PAPERLESS_PROCESSOR_CAPABILITIES": "parser metadata search ingestion",
                "PAPERLESS_ARCHIVE_FILE_GENERATION": "always",
                "PAPERLESS_OCR_MODE": "off",
            }
            seed = SEED
            if variant == "current":
                env["PAPERLESS_PROCESSOR_IDLE_TIMEOUT"] = "60"
        # Match final ownership before running migrations outside the measured process.
        docker(
            "run",
            "--rm",
            "--userns=host",
            *[
                arg
                for source, target in mounts[:4]
                for arg in ("-v", f"{source}:{target}")
            ],
            "--entrypoint",
            "sh",
            image,
            "-c",
            "chown -R 1000:1000 /usr/src/paperless/data /usr/src/paperless/media /usr/src/paperless/consume /usr/src/paperless/export",
        )
        helper(image, env, mounts, ["manage.py", "migrate", "--noinput"])
        helper(image, env, mounts, ["manage.py", "shell", "-c", seed])
        jobs_output = (RUN_OUT / "jobs.jsonl").open("w")
        jobs_error = (RUN_OUT / "jobs-observer-error.txt").open("w")
        jobs_observer = subprocess.Popen(
            [
                "docker",
                "run",
                "--rm",
                "--userns=host",
                "--name",
                f"{PREFIX}-observer",
                "--network",
                PREFIX,
                "-v",
                f"{os.environ['BENCH_HARNESS_VOLUME']}:/harness:ro",
                *env_args(
                    {
                        "PGHOST": "db",
                        "PGUSER": "paperless",
                        "PGPASSWORD": PASSWORD,
                        "PGDATABASE": "paperless",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                ),
                "--entrypoint",
                "python",
                IMAGE,
                "/harness/jobs_observer.py",
            ],
            stdout=jobs_output,
            stderr=jobs_error,
        )
        docker(
            "run",
            "--rm",
            "--userns=host",
            *[
                arg
                for source, target in mounts[:4]
                for arg in ("-v", f"{source}:{target}")
            ],
            "--entrypoint",
            "sh",
            image,
            "-c",
            "chown -R 1000:1000 /usr/src/paperless/data /usr/src/paperless/media /usr/src/paperless/consume /usr/src/paperless/export",
        )
        PHASE = "startup"
        observer = threading.Thread(target=sample)
        observer.start()
        begin = time.monotonic()
        start("webserver", image, env, mounts)
        wait_ready()
        api_ready = time.monotonic() - begin
        if variant == "baseline":
            for database in ("ai_chat", "phoenix"):
                docker(
                    "exec",
                    SERVICES["db"]["name"],
                    "createdb",
                    "-U",
                    "paperless",
                    database,
                )
            sidecars = baseline.sidecar_environments(
                inference, TOKEN, PASSWORD, secrets.token_hex(20), poll_interval=10
            )
            for name in ("phoenix", "webhook-listener", "ai"):
                if name == "ai":
                    start(
                        name,
                        baseline.IMAGES[name],
                        sidecars[name],
                        [(secret, "/run/secrets:ro")],
                        ("-m", "paperless_ai.search.copilot_server"),
                        "python",
                    )
                else:
                    start(
                        name,
                        baseline.IMAGES[name],
                        sidecars[name],
                        [(secret, "/run/secrets:ro")],
                    )
            for url in (
                "http://ai:8001/health",
                "http://webhook-listener:8001/health",
                "http://phoenix:6006/healthz",
            ):
                deadline = time.monotonic() + 180
                while True:
                    try:
                        with urllib.request.urlopen(url, timeout=3) as response:
                            assert response.status == 200
                        break
                    except (OSError, urllib.error.URLError):
                        if any(
                            logger.poll() is not None for logger, _ in LOGGERS.values()
                        ):
                            raise RuntimeError(
                                "A baseline service exited during startup; inspect streamed service logs"
                            ) from None
                        if time.monotonic() > deadline:
                            raise RuntimeError(
                                f"Sidecar readiness exceeded deadline: {url}"
                            ) from None
                        time.sleep(1)
            request = urllib.request.Request(
                "http://phoenix:6006/v1/traces",
                data=b"",
                headers={"Content-Type": "application/x-protobuf"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200, "Baseline telemetry ingestion must work"
            deadline = time.monotonic() + 180
            while True:
                try:
                    resources = baseline.resource_ids(api)
                    break
                except (AssertionError, KeyError, StopIteration, RuntimeError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(1)
        else:
            resources = {}
        stack_ready = time.monotonic() - begin
        snapshots = [observe("startup-idle")]
        results = []
        for identifier, pages in ((1, 1), (2, 3)):
            PHASE = f"ingest-{identifier}"
            results.append(ingest(variant, identifier, pages, resources))
        PHASE = "browse"
        for row in api("/api/documents/?page_size=100")["results"]:
            api(f"/api/documents/{row['id']}/")
            api(f"/api/documents/{row['id']}/metadata/")
            api(f"/api/documents/{row['id']}/preview/", raw=True)
            api(f"/api/documents/{row['id']}/thumb/", raw=True)
        snapshots.append(observe("post-feature-idle"))
        PHASE = "ingest-after-idle"
        results.append(ingest(variant, 3, 1, resources))
        snapshots.append(observe("final-idle"))
        assert api("/api/documents/?page_size=100")["count"] == 3
        assert observer.is_alive(), "Memory sampler stopped before workload completion"
        assert jobs_observer.poll() is None, (
            "Job timeline observer failed; inspect jobs-observer-error.txt"
        )
        expected = set(SERVICES)
        for snapshot in snapshots:
            assert any(
                row["phase"] == snapshot["phase"] and set(row["services"]) == expected
                for row in SAMPLES
            ), f"No complete memory sample for {snapshot['phase']}"
        result = {
            "variant": variant,
            "repeat": number,
            "api_ready_seconds": api_ready,
            "stack_ready_seconds": stack_ready,
            "idle_seconds": IDLE,
            "services": SERVICES.copy(),
            "snapshots": snapshots,
            "workload": results,
            "poll_interval_seconds": None if variant == "current" else 10,
            "processor_idle_timeout_seconds": 60 if variant == "current" else None,
            "processor_mode": "redis-events"
            if variant == "current"
            else "polling"
            if variant == "candidate"
            else "legacy-sidecars",
            "samples": len(SAMPLES),
        }
        STOP.set()
        observer.join()
        if variant == "baseline":
            # Query only after sampling so the diagnostic helper cannot affect measured peaks.
            traces = helper(
                IMAGE,
                {},
                [],
                [
                    "-c",
                    f'import json,psycopg; from psycopg.rows import dict_row; c=psycopg.connect(host="db",dbname="phoenix",user="paperless",password="{PASSWORD}",row_factory=dict_row); print(json.dumps(c.execute("select name,span_id,parent_id,span_kind,start_time,end_time,status_code,cumulative_llm_token_count_prompt,cumulative_llm_token_count_completion from spans order by start_time").fetchall(),default=str))',
                ],
            )
            trace_rows = json.loads(traces)
            (RUN_OUT / "inference-traces.json").write_text(
                json.dumps(trace_rows, indent=2)
            )
            result["phoenix_spans"] = len(trace_rows)
            assert result["phoenix_spans"] > 0, (
                "Baseline did not persist real inference traces"
            )
        (RUN_OUT / "result.json").write_text(json.dumps(result, indent=2))
        print(
            json.dumps(
                {
                    "completed": RUN_OUT.name,
                    "api_ready_seconds": api_ready,
                    "workload_seconds": [
                        row["upload_to_metadata_and_search_seconds"] for row in results
                    ],
                }
            ),
            flush=True,
        )
        return result
    finally:
        STOP.set()
        (RUN_OUT / "latency.json").write_text(json.dumps(LATENCIES, indent=2))
        if observer:
            observer.join()
        if jobs_observer:
            subprocess.run(
                ["docker", "stop", "--time", "5", f"{PREFIX}-observer"],
                capture_output=True,
                check=False,
            )
            jobs_observer.wait(timeout=15)
            jobs_output.close()
            jobs_error.close()
        for service, info in reversed(list(SERVICES.items())):
            subprocess.run(
                ["docker", "rm", "-f", info["name"]], capture_output=True, check=False
            )
            logger, log = LOGGERS[service]
            logger.wait(timeout=20)
            log.close()
        docker("network", "disconnect", PREFIX, os.environ["BENCH_CONTROLLER"])
        docker("network", "rm", PREFIX)
        for name in VOLUMES:
            docker("volume", "rm", name)


def main() -> None:
    """Compare original, polling and current implementations in forward/reverse order."""
    global IMAGE, CURRENT_IMAGE, DATABASE, BROKER
    OUT.mkdir(exist_ok=True)
    assert not (OUT / "summary.json").exists(), "Use a fresh evidence volume"
    (OUT / "run.py").write_text(Path(__file__).read_text())
    (OUT / "baseline.py").write_text((ROOT / "baseline.py").read_text())
    (OUT / "remote.py").write_text((ROOT / "remote.py").read_text())
    (OUT / "jobs_observer.py").write_text((ROOT / "jobs_observer.py").read_text())
    IMAGE, CURRENT_IMAGE, DATABASE, BROKER = [
        json.loads(docker("image", "inspect", name))[0]["Id"]
        for name in (IMAGE, CURRENT_IMAGE, DATABASE, BROKER)
    ]
    baseline.IMAGES = {
        key: json.loads(docker("image", "inspect", value))[0]["Id"]
        for key, value in baseline.IMAGES.items()
    }
    results = []
    variants = [("baseline", 1), ("candidate", 1), ("current", 1)]
    if "--preflight" not in sys.argv:
        variants += [("current", 2), ("candidate", 2), ("baseline", 2)]
    for variant, number in variants:
        results.append(run(variant, number))
    (OUT / "summary.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        assert pdf(1, 1) == pdf(1, 1) and pdf(1, 1) != pdf(2, 1)
        assert b"/Count 3" in pdf(2, 3)
        assert fields({"custom_fields": [{"field": 4, "value": "ok"}]}) == {4: "ok"}
        assert (
            process_role("python manage.py document_processor --user hidden")
            == "document_processor"
        )
        assert io_totals("8:0 rbytes=5 wbytes=7\n8:1 rbytes=3 wbytes=1") == {
            "rbytes": 8,
            "wbytes": 8,
        }
        assert (
            process_role("python3 -m paperless_services.launcher processor")
            == "processor-parent"
        )
        assert task_failed({"status": "started", "operation": {"status": "failed"}})
        assert not task_failed(
            {"status": "started", "operation": {"status": "pending"}}
        )
        print("PDF generation and completion-field checks passed")
    else:
        main()
