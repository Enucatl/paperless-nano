# ruff: noqa: INP001, T201
"""Run an isolated six-service residency experiment using selected documents."""

import json
import os
import secrets
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SNAPSHOT = Path("/snapshot")
TAG = os.environ.get("BENCH_VARIANT", "explore")
PREFIX = f"pm27-{TAG}"
VOLUME = os.environ.get("BENCH_SNAPSHOT_VOLUME", "paperless-memory-27-snapshot")
HARNESS_DIR = os.environ["BENCH_HARNESS_DIR"]
IMAGE = os.environ.get("BENCH_IMAGE", "paperless-memory27:baseline")
OUT = SNAPSHOT / "results" / TAG
TOKEN = secrets.token_hex(20)
PASSWORD = "isolated-benchmark-password"
LIVE = json.loads((SNAPSHOT / "config/containers.json").read_text())
SERVICES = {}
VOLUMES = []
PHASE = "setup"
STOP = threading.Event()
SAMPLES = []
START = time.monotonic()


def command(args, *, text=True, input=None, check=True, stderr_path=None):
    """Run a command and retain failures only in the private results directory."""
    result = subprocess.run(args, input=input, capture_output=True, text=text)
    if stderr_path is not None:
        stderr_path.write_text(result.stderr)
    if check and result.returncode:
        (OUT / "command-error.txt").write_text(
            result.stdout + result.stderr if text else "binary command failed"
        )
        raise RuntimeError(
            f"{args[0]} failed, exit {result.returncode}; private error log retained"
        )
    return result.stdout


def docker(*args, **kwargs):
    """Run the host Docker CLI through the mounted daemon socket."""
    return command(["docker", *args], **kwargs)


def env_for(service):
    """Copy captured service settings and replace direct credentials."""
    config = next(item for item in LIVE if item["Name"].endswith(f"-{service}-1"))
    env = dict(line.split("=", 1) for line in config["Config"]["Env"] if "=" in line)
    for key in list(env):
        if any(
            word in key.upper()
            for word in ("PASSWORD", "DBPASS", "SECRET", "TOKEN", "API_KEY")
        ) and not key.endswith("_FILE"):
            env[key] = PASSWORD
    return env


def env_args(env):
    """Build Docker environment arguments from captured settings."""
    return [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]


def volume(role):
    """Create and track one disposable service volume."""
    name = f"{PREFIX}-{role}"
    docker("volume", "create", name)
    VOLUMES.append(name)
    return name


def run(service, image, env, mounts=(), extra=(), entrypoint=None, args=()):
    """Start a measured service and resolve its host cgroup."""
    name = f"{PREFIX}-{service}"
    cmd = [
        "run",
        "--rm",
        "--userns=host",
        "-d",
        "--name",
        name,
        "--network",
        PREFIX,
        "--network-alias",
        service,
    ]
    for src, dest in mounts:
        cmd += ["-v", f"{src}:{dest}"]
    cmd += list(extra) + env_args(env)
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    cmd += [image, *args]
    docker(*cmd)
    info = json.loads(docker("inspect", name))[0]
    pid = info["State"]["Pid"]
    relative = Path(f"/proc/{pid}/cgroup").read_text().split("0::", 1)[1].strip()
    if not (Path("/host-cgroup") / relative.lstrip("/") / "memory.current").is_file():
        raise RuntimeError(
            "Sampler requires host cgroup namespace and host cgroup mount"
        )
    SERVICES[service] = {
        "name": name,
        "pid": pid,
        "cgroup": str(Path("/host-cgroup") / relative.lstrip("/")),
        "image": info["Image"],
    }
    return name


def helper(env, mounts, args, *, user=None, stderr_path=None):
    """Run a temporary Django helper outside measured service totals."""
    cmd = [
        "run",
        "--rm",
        "--userns=host",
        "--network",
        PREFIX,
        "--entrypoint",
        "python",
        *env_args(env),
    ]
    for src, dest in mounts:
        cmd += ["-v", f"{src}:{dest}"]
    if user is not None:
        cmd += ["--user", user]
    cmd += [IMAGE, *args]
    return docker(*cmd, stderr_path=stderr_path)


def sampler():
    """Record each service cgroup once per second until cleanup starts."""
    with (OUT / "memory.jsonl").open("w") as output:
        while not STOP.is_set():
            sample = {
                "seconds": time.monotonic() - START,
                "phase": PHASE,
                "services": {},
                "unavailable": [],
            }
            for service, info in list(SERVICES.items()):
                path = Path(info["cgroup"])
                try:
                    stat = dict(
                        line.split()
                        for line in (path / "memory.stat").read_text().splitlines()
                    )
                    events = dict(
                        line.split()
                        for line in (path / "memory.events").read_text().splitlines()
                    )
                    sample["services"][service] = {
                        "current": int((path / "memory.current").read_text()),
                        "peak": int((path / "memory.peak").read_text()),
                        "anon": int(stat["anon"]),
                        "file": int(stat["file"]),
                        "inactive_file": int(stat["inactive_file"]),
                        "events": {key: int(value) for key, value in events.items()},
                    }
                except (FileNotFoundError, ProcessLookupError):
                    sample["unavailable"].append(service)
                    continue
            SAMPLES.append(sample)
            output.write(json.dumps(sample) + "\n")
            output.flush()
            STOP.wait(1)


def api(path, *, method="GET", body=None, headers=None, raw=False):
    """Call the isolated API and retain request timing without response content."""
    request_headers = {"Authorization": f"Token {TOKEN}"}
    if headers:
        request_headers.update(headers)
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "http://webserver:8000" + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
        status = response.status
        if not raw and "application/json" not in response.headers.get(
            "Content-Type", ""
        ):
            raise RuntimeError(
                f"Expected a JSON API response at {path}; got HTTP {status}"
            )
    with (OUT / "latency.jsonl").open("a") as output:
        output.write(
            json.dumps(
                {
                    "path": path,
                    "seconds": time.monotonic() - start,
                    "status": status,
                    "bytes": len(data),
                }
            )
            + "\n"
        )
    return data if raw else json.loads(data)


def wait_http(url, timeout=180):
    """Wait for a service to accept requests."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except urllib.error.HTTPError as exc:
            if url.endswith("/api/") and exc.code in (401, 403):
                return
            time.sleep(0.5)
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise RuntimeError("service readiness deadline exceeded")


def pss_snapshot(label):
    """Attribute resident memory to processes without recording their arguments."""
    result = {}
    for service, info in SERVICES.items():
        pids = set()
        for file in Path(info["cgroup"]).rglob("cgroup.procs"):
            pids.update(file.read_text().split())
        rows = []
        for pid in pids:
            try:
                cmd = (
                    Path(f"/proc/{pid}/cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode(errors="replace")
                )
                role = next(
                    (
                        term
                        for term in [
                            "granian",
                            "spawn_main",
                            "resource_tracker",
                            "paperless_worker",
                            "paperless_services",
                            "supercronic",
                            "postgres",
                            "redis",
                            "copilot_server",
                            "phoenix",
                            "paperless_listener",
                        ]
                        if term in cmd
                    ),
                    "other",
                )
                values = dict(
                    (line.split(":", 1)[0], line.split(":", 1)[1].strip())
                    for line in Path(f"/proc/{pid}/smaps_rollup")
                    .read_text()
                    .splitlines()[1:]
                )
                rows.append(
                    {
                        "pid": int(pid),
                        "role": role,
                        "pss_kib": int(values["Pss"].split()[0]),
                        "rss_kib": int(values["Rss"].split()[0]),
                    }
                )
            except (FileNotFoundError, ProcessLookupError, PermissionError) as exc:
                rows.append({"pid": int(pid), "error": type(exc).__name__})
        result[service] = rows
    (OUT / f"pss-{label}.json").write_text(json.dumps(result, indent=2))


def observe(phase, seconds):
    """Wait through a named phase and capture its ending process residency."""
    global PHASE
    PHASE = phase
    if os.environ.get("BENCH_WORKLOAD_PREFLIGHT"):
        seconds = 1
    print(json.dumps({"phase": phase, "seconds": seconds}), flush=True)
    time.sleep(seconds)
    pss_snapshot(phase)


def summarize():
    """Summarize simultaneous samples containing all six measured services."""
    result = {}
    for phase in sorted({s["phase"] for s in SAMPLES}):
        rows = [s for s in SAMPLES if s["phase"] == phase and len(s["services"]) == 6]
        if not rows:
            continue
        totals = [
            sum(v["current"] for v in s["services"].values()) / 2**20 for s in rows
        ]
        result[phase] = {
            "samples": len(rows),
            "median_mib": statistics.median(totals),
            "min_mib": min(totals),
            "max_mib": max(totals),
            "services_median_mib": {
                service: statistics.median(
                    s["services"][service]["current"] / 2**20 for s in rows
                )
                for service in rows[0]["services"]
            },
        }
    (OUT / "summary.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"summary": result}), flush=True)


def main():
    """Run one variant in fresh services and retain private measurement evidence."""
    global PHASE
    OUT.mkdir(parents=True, exist_ok=False)
    for name in ("run.py", "seed.py", "workload.py", "runtime_checks.py"):
        (OUT / name).write_bytes((Path(__file__).parent / name).read_bytes())
    (OUT / "experiment.json").write_text(
        json.dumps(
            {
                "variant": TAG,
                "image": IMAGE,
                "idle_seconds": int(os.environ.get("BENCH_IDLE_SECONDS", "90")),
                "python_allocator": os.environ.get("BENCH_PYTHONMALLOC", "default"),
                "workload_preflight": bool(os.environ.get("BENCH_WORKLOAD_PREFLIGHT")),
                "workload": "metadata-detail-v2",
            },
            indent=2,
        )
    )
    thread = threading.Thread(target=sampler)
    thread.start()
    try:
        docker("network", "create", "--internal", PREFIX)
        docker("network", "connect", PREFIX, os.environ["BENCH_CONTROLLER"])
        data, media, consume, secret_vol, pgdata, phoenixdata = [
            volume(role)
            for role in ["data", "media", "consume", "secrets", "pgdata", "phoenixdata"]
        ]
        setup_mounts = [
            (VOLUME, "/snapshot:ro"),
            (data, "/usr/src/paperless/data"),
            (media, "/usr/src/paperless/media"),
            (consume, "/usr/src/paperless/consume"),
            (secret_vol, "/run/secrets"),
        ]
        setup = """import json,os,shutil; from pathlib import Path
for name in ["data","media","consume"]:
 p=Path("/usr/src/paperless")/name; p.mkdir(exist_ok=True); os.chown(p,1000,1000)
Path("/usr/src/paperless/data/index").mkdir(exist_ok=True)
shutil.copytree("/snapshot/files/media", "/usr/src/paperless/media",dirs_exist_ok=True)
rows=json.loads(Path("/snapshot/selected-documents.json").read_text())
candidates=sorted([r for r in rows if r["mime_type"]=="application/pdf" and 1 <= (r["page_count"] or 0) <= 10],key=lambda r:(r["page_count"],r["id"]))
reserved={candidates[0]["id"], candidates[-1]["id"]}
manifest=json.loads(Path("/snapshot/manifest.json").read_text())
for row in manifest["documents"]:
 if row["id"] in reserved:
  for item in row["files"].values():
   (Path("/usr/src/paperless/media")/Path(item["path"]).relative_to("files/media")).unlink()
for p in Path("/usr/src/paperless/media").rglob("*"): os.chown(p,1000,1000)
names={"paperless_token","webhook_secret","phoenix_postgres_password","ai_chat_postgres_password"}
for obj in json.loads(Path("/snapshot/config/containers.json").read_text()):
 for item in obj["Config"]["Env"]:
  if "=" not in item: continue
  key,value=item.split("=",1)
  if key.endswith("_FILE") and value.startswith("/run/secrets/"): names.add(Path(value).name)
for name in names:
 p=Path("/run/secrets")/name; p.write_text(os.environ["BENCH_TOKEN"] if name=="paperless_token" else "isolated-benchmark-password"); p.chmod(0o444)
"""
        helper({"BENCH_TOKEN": TOKEN}, setup_mounts, ["-c", setup])
        pg = run(
            "db",
            "postgres:18",
            {
                "POSTGRES_DB": "paperless",
                "POSTGRES_USER": "paperless",
                "POSTGRES_PASSWORD": PASSWORD,
            },
            [(pgdata, "/var/lib/postgresql")],
        )
        for attempt in range(120):
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    pg,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "paperless",
                    "-d",
                    "paperless",
                ],
                capture_output=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("postgres readiness failed")
        for name in ("phoenix", "ai_chat"):
            docker(
                "exec",
                pg,
                "psql",
                "-X",
                "-U",
                "paperless",
                "-d",
                "paperless",
                "-c",
                f"CREATE ROLE {name} LOGIN PASSWORD '{PASSWORD}'",
            )
            docker(
                "exec",
                pg,
                "psql",
                "-X",
                "-U",
                "paperless",
                "-d",
                "paperless",
                "-c",
                f"CREATE DATABASE {name} OWNER {name}",
            )
        run(
            "broker",
            "redis:8",
            {},
            args=["redis-server", "--save", "", "--appendonly", "no"],
        )
        env = env_for("webserver")
        env.update(
            PAPERLESS_SECRET_KEY="isolated-benchmark-secret",
            PAPERLESS_DBPASS=PASSWORD,
            PAPERLESS_DBHOST="db",
            PAPERLESS_DBNAME="paperless",
            PAPERLESS_DBUSER="paperless",
            PAPERLESS_REDIS="redis://broker:6379",
            PAPERLESS_URL="http://webserver:8000",
            PAPERLESS_ALLOWED_HOSTS="webserver,localhost,127.0.0.1",
            PAPERLESS_API_TOKEN_FILE="/run/secrets/paperless_token",
            BENCH_TOKEN=TOKEN,
            BENCH_ISOLATED="1",
            PYTHONPATH="/usr/src/paperless/src",
        )
        env.pop("PAPERLESS_LOGOUT_REDIRECT_URL", None)
        # Empty mail configuration and controlled workflows keep external effects isolated.
        mounts = [
            (data, "/usr/src/paperless/data"),
            (media, "/usr/src/paperless/media"),
            (consume, "/usr/src/paperless/consume"),
            (secret_vol, "/run/secrets:ro"),
        ]
        helper(env, mounts, ["manage.py", "migrate", "--noinput"])
        seed_output = helper(
            env,
            [*mounts, (VOLUME, "/snapshot"), (HARNESS_DIR, "/harness:ro")],
            ["/harness/seed.py"],
        )
        print(json.dumps({"seed": seed_output.strip()}), flush=True)
        helper(
            env,
            mounts,
            [
                "-c",
                'import os;from pathlib import Path;[os.chown(p,1000,1000) for p in Path("/usr/src/paperless/data").rglob("*")]',
            ],
        )
        PHASE = "startup"
        web_start = time.monotonic()
        extra = [
            "--user",
            "1000:1000",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=1g,mode=1777",
            "--tmpfs",
            "/run:rw,exec,nosuid,size=16m,mode=1777",
        ]
        if os.environ.get("BENCH_PYTHONMALLOC"):
            env["PYTHONMALLOC"] = os.environ["BENCH_PYTHONMALLOC"]
        run("webserver", IMAGE, env, mounts, extra=extra)
        wait_http("http://webserver:8000/api/")
        readiness = time.monotonic() - web_start
        print(json.dumps({"web_readiness_seconds": readiness}), flush=True)
        (OUT / "readiness.json").write_text(json.dumps({"seconds": readiness}))
        for service, image, endpoint in [
            ("phoenix", "arizephoenix/phoenix:latest", "http://phoenix:6006/healthz"),
            (
                "webhook-listener",
                "paperless-ai-webhook-listener:latest",
                "http://webhook-listener:8001/health",
            ),
            ("ai", "paperless-ai-ai:latest", "http://ai:8001/health"),
        ]:
            e = env_for(service)
            e.update(
                PAPERLESS_URL="http://webserver:8000",
                PAPERLESS_TOKEN_FILE="/run/secrets/paperless_token",
                REDIS_URL="redis://broker:6379/1",
                OTEL_EXPORTER_OTLP_ENDPOINT="http://phoenix:6006",
                MANAGE_PAPERLESS_WORKFLOWS="false",
                PAPERLESS_WEBHOOK_URL="http://webhook-listener:8001/webhook",
                CHAT_DATABASE_URL="postgresql://ai_chat@db:5432/ai_chat",
                CHAT_DATABASE_PASSWORD_FILE="/run/secrets/ai_chat_postgres_password",
            )
            ms = [(secret_vol, "/run/secrets:ro")]
            if service == "phoenix":
                e.update(PHOENIX_POSTGRES_HOST="db", PHOENIX_POSTGRES_PASSWORD=PASSWORD)
                ms += [(phoenixdata, "/root/.phoenix")]
                args = ["-m", "phoenix.server.main", "serve"]
                entry = "python"
            else:
                args = [
                    "-m",
                    "paperless_listener.server"
                    if service == "webhook-listener"
                    else "paperless_ai.search.copilot_server",
                ]
                entry = "python"
            run(service, image, e, ms, entrypoint=entry, args=args)
            wait_http(endpoint)
        (OUT / "service-images.json").write_text(
            json.dumps({service: info["image"] for service, info in SERVICES.items()})
        )
        if os.environ.get("BENCH_PREFLIGHT"):
            assert api("/api/documents/?page_size=100")["count"] == 12
            print(
                "six-service startup and selected-document API preflight passed",
                flush=True,
            )
            return
        observe("startup-idle", int(os.environ.get("BENCH_IDLE_SECONDS", "90")))
        from workload import exercise

        def phase(value):
            global PHASE
            PHASE = value

        exercise(
            api,
            SNAPSHOT,
            OUT,
            phase,
            observe,
            int(os.environ.get("BENCH_IDLE_SECONDS", "90")),
        )
        # Runtime recovery checks must not alter the measured workload or idle samples.
        STOP.set()
        thread.join()
        seeded_ids = json.loads((SNAPSHOT / "results/seed-result.json").read_text())[
            "seeded_ids"
        ]
        document_id = next(
            row["id"]
            for row in json.loads((SNAPSHOT / "selected-documents.json").read_text())
            if row["id"] in seeded_ids
            and row["mime_type"] == "application/pdf"
            and 1 <= (row["page_count"] or 0) <= 10
        )
        checks = helper(
            {**env, "BENCH_DOCUMENT_ID": str(document_id)},
            [*mounts, (HARNESS_DIR, "/harness:ro")],
            ["/harness/runtime_checks.py"],
            user="1000:1000",
            stderr_path=OUT / "runtime-checks.log",
        )
        (OUT / "runtime-checks.json").write_text(checks)
        print(checks.strip(), flush=True)
    finally:
        STOP.set()
        thread.join()
        summarize()
        for service, info in reversed(list(SERVICES.items())):
            logs = subprocess.run(
                ["docker", "logs", info["name"]], capture_output=True, text=True
            )
            (OUT / f"{service}.log").write_text(logs.stdout + logs.stderr)
            docker("stop", "-t", "60", info["name"], check=False)
        docker(
            "network", "disconnect", PREFIX, os.environ["BENCH_CONTROLLER"], check=False
        )
        docker("network", "rm", PREFIX, check=False)
        for name in VOLUMES:
            docker("volume", "rm", name, check=False)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        assert len(LIVE) == 6
        for service in [
            "db",
            "broker",
            "webserver",
            "ai",
            "phoenix",
            "webhook-listener",
        ]:
            assert isinstance(env_for(service), dict)
        assert env_for("webserver")["PAPERLESS_USE_LEAN_WORKER"] == "1"
        assert len(json.loads((SNAPSHOT / "selected-documents.json").read_text())) == 14
        sample = {"a": {"current": 2**20}, "b": {"current": 2**20}}
        assert sum(v["current"] for v in sample.values()) / 2**20 == 2
        print("harness syntax/configuration/accounting self-check passed")
    else:
        os.umask(0o077)
        main()
