"""Qualify an isolated release against twenty generated PDFs and real inference.

Run inside a disposable controller with the Docker CLI/socket, this script,
/model-secret/openrouter_api_key (read-only), and a private /evidence volume.
No production database, document storage or service is used.
"""

# ruff: noqa: INP001, T201

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path("/evidence")
STATE = ROOT / "qualification-state.json"
PREFIX = "paperless-release-qualification"
LANGUAGES = (
    (
        "en",
        "Alpine Research Supplies",
        "Invoice",
        "Laboratory notebooks",
        "Total amount due",
        "Thank you for your order",
    ),
    (
        "de",
        "Berner Bürobedarf",
        "Rechnung",
        "Notizbücher für das Labor",
        "Gesamtbetrag",
        "Vielen Dank für Ihre Bestellung",
    ),
    (
        "fr",
        "Fournitures du Léman",
        "Facture",
        "Cahiers pour le laboratoire",
        "Montant total",
        "Merci pour votre commande",
    ),
    (
        "it",
        "Forniture Ticinesi",
        "Fattura",
        "Quaderni per il laboratorio",
        "Importo totale",
        "Grazie per il vostro ordine",
    ),
)


def docker(*args: str, data: bytes | None = None) -> bytes:
    """Execute Docker without echoing credentials or application payloads."""
    result = subprocess.run(
        ["docker", *args], input=data, capture_output=True, check=False
    )
    if result.returncode:
        ROOT.mkdir(exist_ok=True)
        (ROOT / "private-command-error.log").write_bytes(result.stdout + result.stderr)
        raise RuntimeError(f"Docker {args[0]} failed; inspect private evidence")
    return result.stdout


def load() -> dict:
    """Read the private state of this one disposable stack."""
    state = json.loads(STATE.read_text())
    state["token"] = (Path(state["secret_dir"]) / "paperless_token").read_text().strip()
    return state


def save(state: dict) -> None:
    """Persist credentials privately; never include this file in public reports."""
    STATE.write_text(
        json.dumps(
            {
                key: value
                for key, value in state.items()
                if key not in {"token", "worker_token", "password"}
            },
            indent=2,
        )
    )
    STATE.chmod(0o600)


def env_args(env: dict) -> list[str]:
    """Build subprocess arguments without shell interpolation."""
    return [arg for name, value in env.items() for arg in ("-e", f"{name}={value}")]


def mounts(state: dict) -> list[str]:
    """Mount only this stack's private application volumes."""
    return [
        arg
        for role, target in (
            ("data", "/usr/src/paperless/data"),
            ("media", "/usr/src/paperless/media"),
            ("consume", "/usr/src/paperless/consume"),
            ("export", "/usr/src/paperless/export"),
        )
        for arg in ("-v", f"{state['volumes'][role]}:{target}")
    ]


def compose(state: dict, *args: str) -> bytes:
    """Use Compose secrets for every credential-bearing service or helper."""
    return docker(
        "compose", "--project-name", PREFIX, "--file", str(ROOT / "compose.json"), *args
    )


def write_compose(state: dict) -> None:
    """Write a private Compose definition with host-visible secret file paths."""
    app_volumes = [
        f"{state['volumes'][role]}:{target}"
        for role, target in (
            ("data", "/usr/src/paperless/data"),
            ("media", "/usr/src/paperless/media"),
            ("consume", "/usr/src/paperless/consume"),
            ("export", "/usr/src/paperless/export"),
        )
    ]
    document = {
        "services": {
            "app": {
                "image": state["image"],
                "container_name": PREFIX + "-app",
                "userns_mode": "host",
                "environment": state["env"],
                "ports": [f"0.0.0.0:{state['port']}:8000"],
                "volumes": app_volumes,
                "secrets": [
                    "db_password",
                    "secret_key",
                    "paperless_token",
                    "workstation_token",
                    "openrouter_api_key",
                ],
                "networks": {"default": {"aliases": ["webserver"]}},
            },
            "db": {
                "image": "postgres:18",
                "container_name": PREFIX + "-db",
                "userns_mode": "host",
                "environment": {
                    "POSTGRES_DB": "paperless",
                    "POSTGRES_USER": "paperless",
                    "POSTGRES_PASSWORD_FILE": "/run/secrets/db_password",
                },
                "volumes": [state["volumes"]["db"] + ":/var/lib/postgresql"],
                "secrets": ["db_password"],
                "networks": {"default": {"aliases": ["db"]}},
            },
            "broker": {
                "image": "docker.io/library/redis:8",
                "container_name": PREFIX + "-broker",
                "userns_mode": "host",
                "volumes": [state["volumes"]["broker"] + ":/data"],
                "networks": {"default": {"aliases": ["broker"]}},
            },
        },
        "networks": {"default": {"name": PREFIX, "external": True}},
        "volumes": {name: {"external": True} for name in state["all_volumes"]},
        "secrets": {
            name: {"file": str(Path(state["secret_dir"]) / name)}
            for name in (
                "db_password",
                "secret_key",
                "paperless_token",
                "workstation_token",
                "openrouter_api_key",
            )
        },
    }
    (ROOT / "compose.json").write_text(json.dumps(document, indent=2))
    (ROOT / "compose.json").chmod(0o600)


def manage(state: dict, code: str) -> str:
    """Run an isolated management shell as the normal application user."""
    wrapper = 'export PAPERLESS_DBPASS="$(cat /run/secrets/db_password)" PAPERLESS_SECRET_KEY="$(cat /run/secrets/secret_key)"; exec python manage.py shell -c "$1"'
    return compose(
        state,
        "run",
        "--rm",
        "--no-deps",
        "--user",
        "1000:1000",
        "--entrypoint",
        "/bin/sh",
        "app",
        "-ec",
        wrapper,
        "sh",
        code,
    ).decode()


def api(
    state: dict, path: str, *, method: str = "GET", body=None, headers=None, raw=False
):
    """Call only the isolated HTTP service using its temporary administrator."""
    request_headers = {"Authorization": "Token " + state["token"], **(headers or {})}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        state["url"] + path, data=body, method=method, headers=request_headers
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    return data if raw else json.loads(data)


def ready(state: dict) -> None:
    """Wait for the authenticated document API, with a finite deadline."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            api(state, "/api/documents/?page_size=1")
            return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise RuntimeError("Isolated API readiness exceeded 180 seconds")


def start_app(state: dict) -> None:
    """Start the supervised app with Compose-mounted secrets and temporary API."""
    write_compose(state)
    compose(state, "up", "--detach", "app")
    ready(state)


def stop_app(state: dict) -> None:
    """Stop and remove only the isolated application container."""
    compose(state, "stop", "app")
    compose(state, "rm", "--force", "app")


def start_database(state: dict) -> None:
    """Start PostgreSQL 18 with only the temporary database volume."""
    write_compose(state)
    compose(state, "up", "--detach", "db")
    for _ in range(60):
        result = subprocess.run(
            ["docker", "exec", PREFIX + "-db", "pg_isready", "-U", "paperless"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL readiness exceeded 60 seconds")


def setup(args) -> None:
    """Provision a fresh stack and dedicated non-login workstation account."""
    assert not STATE.exists(), "Use a fresh private evidence volume"
    ROOT.mkdir(exist_ok=True)
    state = {
        "image": args.image,
        "port": args.port,
        "url": "http://webserver:8000",
        "external_url": args.external_url,
        "token": secrets.token_hex(20),
        "worker_token": secrets.token_hex(20),
        "password": secrets.token_hex(20),
        "secret_dir": os.environ["QUALIFICATION_SECRETS_DIR"],
        "volumes": {},
        "all_volumes": [],
    }
    docker("network", "create", PREFIX)
    if os.environ.get("QUALIFICATION_CONTROLLER"):
        docker("network", "connect", PREFIX, os.environ["QUALIFICATION_CONTROLLER"])
    for role in ("db", "data", "media", "consume", "export", "broker"):
        name = PREFIX + "-" + role
        docker("volume", "create", name)
        state["volumes"][role] = name
        state["all_volumes"].append(name)
    state["env"] = {
        "PAPERLESS_DBENGINE": "postgresql",
        "PAPERLESS_DBHOST": "db",
        "PAPERLESS_DBNAME": "paperless",
        "PAPERLESS_DBUSER": "paperless",
        "PAPERLESS_DBPASS_FILE": "/run/secrets/db_password",
        "PAPERLESS_REDIS": "redis://broker:6379",
        "PAPERLESS_SECRET_KEY_FILE": "/run/secrets/secret_key",
        "PAPERLESS_URL": args.external_url,
        "PAPERLESS_ALLOWED_HOSTS": "*",
        "PAPERLESS_TIME_ZONE": "UTC",
        "PAPERLESS_TASK_WORKERS": "2",
        "PAPERLESS_THREADS_PER_WORKER": "2",
        "PAPERLESS_WEBSERVER_WORKERS": "1",
        "PAPERLESS_TRAIN_TASK_CRON": "disable",
        "PAPERLESS_CLASSIFIER_ENABLED": "false",
        "PAPERLESS_AI_ALLOWED": "false",
        "PAPERLESS_API_TOKEN_FILE": "/run/secrets/paperless_token",
        "PAPERLESS_CONSUMER_CLASS": "paperless_services.watcher.APIConsumer",
        "PAPERLESS_SCHEDULER_CLASS": "paperless_services.scheduler.APIScheduler",
        "PAPERLESS_WORKER_CLASS": "paperless_worker.bootstrap.LeanWorker",
        "PAPERLESS_PARSER": "paperless_inference.providers.PaddleOCRProvider",
        "PAPERLESS_ENRICHMENT_PROVIDER": "paperless_inference.providers.MetadataProvider",
        "PAPERLESS_ENRICHMENT_ALLOW_REMOTE": "true",
        "PAPERLESS_OCR_PROVENANCE_FIELD": "4",
        "PAPERLESS_PROCESSOR_USER": "server-processor",
        "PAPERLESS_PROCESSOR_CAPABILITIES": "parser metadata search ingestion",
        "PAPERLESS_ARCHIVE_FILE_GENERATION": "always",
        "PAPERLESS_OCR_MODE": "off",
        "INFERENCE_OCR_ENDPOINT": args.ocr_endpoint,
        "INFERENCE_OCR_PIPELINE": "PaddleOCR-VL-1.6",
        "INFERENCE_OCR_MODEL": "PaddlePaddle/PaddleOCR-VL-1.6",
        "INFERENCE_OCR_LAYOUT_MODEL": "PP-DocLayoutV3",
        "INFERENCE_OCR_TIMEOUT": "600",
        "INFERENCE_METADATA_ENDPOINT": "https://openrouter.ai/api/v1",
        "INFERENCE_METADATA_MODEL": "inception/mercury-2.5",
        "INFERENCE_METADATA_TEMPERATURE": "0",
        "OPENROUTER_API_KEY_FILE": "/run/secrets/openrouter_api_key",
    }
    save(state)
    secret_dir = Path(state["secret_dir"])
    secret_dir.mkdir(mode=0o700, exist_ok=False)
    for name, value in (
        ("db_password", state["password"].encode()),
        ("secret_key", secrets.token_hex(32).encode()),
        ("paperless_token", state["token"].encode()),
        ("workstation_token", state["worker_token"].encode()),
        ("openrouter_api_key", Path("/model-secret/openrouter_api_key").read_bytes()),
    ):
        path = secret_dir / name
        path.write_bytes(value)
        path.chmod(0o444 if name == "db_password" else 0o400)
        if os.getuid() == 0:
            os.chown(path, 1000, 1000)
    write_compose(state)
    docker(
        "run",
        "--rm",
        "--userns=host",
        *mounts(state),
        "--entrypoint",
        "sh",
        state["image"],
        "-c",
        "chown -R 1000:1000 /usr/src/paperless/data /usr/src/paperless/media /usr/src/paperless/consume /usr/src/paperless/export",
    )
    start_database(state)
    compose(state, "up", "--detach", "broker")
    manage(
        state,
        "from django.core.management import call_command;call_command('migrate',interactive=False)",
    )
    manage(
        state,
        f"""
from pathlib import Path
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from documents.models import CustomField, Tag
from django.core.management import call_command
admin=User.objects.create_superuser(username="qualification",password=None,email="qualification@example.invalid")
Token.objects.create(user=admin,key={state["token"]!r})
for name,kind in [("summary","longtext"),("processing_date","date"),("audit","longtext"),("ocr_provenance","longtext"),("unrelated","string")]:
    CustomField.objects.create(name=name,data_type=kind)
Tag.objects.create(name="preserve-tag")
call_command("document_processor_account","--user","server-processor","--capability","parser","--capability","metadata","--capability","search","--capability","ingestion")
call_command("document_processor_account","--user","workstation-processor","--capability","parser","--capability","metadata")
Token.objects.create(user=User.objects.get(username="workstation-processor"),key={state["worker_token"]!r})
""",
    )
    start_app(state)
    (ROOT / "workstation-token").write_text(state["worker_token"])
    (ROOT / "workstation-token").chmod(0o600)
    print(
        json.dumps(
            {
                "ready": True,
                "url": state["external_url"],
                "image": state["image"],
                "worker_token_file": "/evidence/workstation-token",
            }
        )
    )


def make_pdf(identifier: int) -> tuple[bytes, dict]:
    """Generate distinct one-to-four-page invoices in four Latin-script languages."""
    import fitz

    language, correspondent, heading, item, total, thanks = LANGUAGES[
        (identifier - 1) % len(LANGUAGES)
    ]
    pages = 1 + (identifier - 1) % 4
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        text = f"{correspondent}\n{heading} Q{identifier:03d}\n2026-10-15\n\nCustomer: Synthetic Research Ltd\n{item}\nQuantity: {identifier + 2}\n{total}: CHF {identifier * 37}.00\n\n{thanks}\nReference: releasequalifier\nDocument {identifier}, page {index + 1} of {pages}"
        page.insert_textbox(
            fitz.Rect(50, 60, 545, 780), text, fontsize=17, fontname="helv"
        )
    data = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return data, {
        "number": identifier,
        "language": language,
        "correspondent": correspondent,
        "pages": pages,
        "sha256": hashlib.sha256(data).hexdigest(),
        "filename": f"qualification-{identifier:02d}.pdf",
    }


def records() -> list[dict]:
    """Load submitted-document identities without repeating uploads."""
    path = ROOT / "uploads.json"
    return json.loads(path.read_text()) if path.exists() else []


def upload(args) -> None:
    """Upload a bounded portion of the twenty-PDF workload, optionally awaiting it."""
    state = load()
    submitted = records()
    for number in range(args.start, args.start + args.count):
        assert 1 <= number <= 20 and all(r["number"] != number for r in submitted)
        data, record = make_pdf(number)
        directory = ROOT / "inputs"
        directory.mkdir(exist_ok=True)
        (directory / record["filename"]).write_bytes(data)
        boundary = "qualification-upload-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{record["filename"]}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        record["submitted_unix"] = time.time()
        record["task_id"] = api(
            state,
            "/api/documents/post_document/",
            method="POST",
            body=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        submitted.append(record)
        (ROOT / "uploads.json").write_text(json.dumps(submitted, indent=2))
        if args.wait:
            verify_record(state, record)
        print(
            json.dumps(
                {
                    "submitted": number,
                    "task_id": record["task_id"],
                    "verified": args.wait,
                }
            ),
            flush=True,
        )


def fields(document: dict) -> dict:
    """Index the API's custom field values by their fixture IDs."""
    return {item["field"]: item["value"] for item in document["custom_fields"]}


def verify_record(state: dict, record: dict) -> dict:
    """Verify actual OCR, metadata, source integrity, PDF/A archive and search."""
    import fitz
    from paperless_inference.archive import _validate_pdfa_2u

    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        tasks = api(
            state, "/api/tasks/?task_id=" + urllib.parse.quote(record["task_id"])
        )["results"]
        if any(t["status"] in {"failure", "revoked"} for t in tasks):
            raise AssertionError(
                f"Upload {record['number']} has a failed top-level task"
            )
        docs = api(state, "/api/documents/?page_size=100")["results"]
        document = next(
            (d for d in docs if d["original_file_name"] == record["filename"]), None
        )
        if (
            document
            and all(fields(document).get(key) for key in (1, 2, 3, 4))
            and document.get("correspondent")
            and "releasequalifier" in document["content"].lower()
        ):
            query = "title:" + json.dumps(document["title"], ensure_ascii=False)
            matched = api(state, "/api/documents/?query=" + urllib.parse.quote(query))[
                "results"
            ]
            if document["id"] in {d["id"] for d in matched}:
                break
        time.sleep(1)
    else:
        raise AssertionError(
            f"Upload {record['number']} did not reach metadata-title search within 1200 seconds"
        )
    values = fields(document)
    audit = json.loads(values[3]) if isinstance(values[3], str) else values[3]
    provenance = json.loads(values[4]) if isinstance(values[4], str) else values[4]
    assert audit["metadata_model"] == state["env"]["INFERENCE_METADATA_MODEL"]
    assert document["created"].startswith("2026-10-15"), document["created"]
    tags = api(state, "/api/tags/?page_size=100")["results"]
    assert any(
        t["id"] in document["tags"] and t["name"] == "language:" + record["language"]
        for t in tags
    ), document["tags"]
    original = api(
        state, f"/api/documents/{document['id']}/download/?original=true", raw=True
    )
    archive = api(
        state, f"/api/documents/{document['id']}/download/?original=false", raw=True
    )
    assert hashlib.sha256(original).hexdigest() == record["sha256"]
    assert archive.startswith(b"%PDF") and original != archive
    with fitz.open(stream=archive, filetype="pdf") as pdf:
        assert len(pdf) == record["pages"]
        assert "releasequalifier" in "".join(page.get_text() for page in pdf).lower()
    with TemporaryDirectory() as directory:
        path = Path(directory) / "archive.pdf"
        path.write_bytes(archive)
        _validate_pdfa_2u(path)
    result = {
        **record,
        "document_id": document["id"],
        "title": document["title"],
        "archive_bytes": len(archive),
        "verified_unix": time.time(),
        "provenance": provenance,
    }
    with (ROOT / "verified.jsonl").open("a") as output:
        output.write(json.dumps(result) + "\n")
    return result


def verify(args) -> None:
    """Recheck every retained document after processing, upgrade or restore."""
    state = load()
    selected = [
        r for r in records() if args.start <= r["number"] < args.start + args.count
    ]
    assert selected
    for record in selected:
        result = verify_record(state, record)
        print(
            json.dumps(
                {"verified": result["number"], "document_id": result["document_id"]}
            ),
            flush=True,
        )


def configure(args) -> None:
    """Recreate only this app when changing provider or execution placement."""
    state = load()
    stop_app(state)
    if args.parser:
        state["env"]["PAPERLESS_PARSER"] = args.parser
    if args.capabilities:
        state["env"]["PAPERLESS_PROCESSOR_CAPABILITIES"] = args.capabilities
        grants = [
            part
            for capability in args.capabilities.split()
            for part in ("--capability", capability)
        ]
        manage(
            state,
            "from django.core.management import call_command;call_command('document_processor_account','--user','server-processor',"
            + ",".join(repr(s) for s in grants)
            + ")",
        )
    save(state)
    start_app(state)
    print(
        json.dumps(
            {
                "configured": True,
                "parser": state["env"]["PAPERLESS_PARSER"],
                "capabilities": state["env"]["PAPERLESS_PROCESSOR_CAPABILITIES"],
            }
        )
    )


def preservation(args) -> None:
    """Reprocess existing content across provider swap/backout without metadata loss."""
    state = load()
    record = next(r for r in records() if r["number"] == args.start)
    doc = next(
        d
        for d in api(state, "/api/documents/?page_size=100")["results"]
        if d["original_file_name"] == record["filename"]
    )
    document_id = doc["id"]
    tag = next(
        t
        for t in api(state, "/api/tags/?page_size=100")["results"]
        if t["name"] == "preserve-tag"
    )
    preserved_title = "Release qualification retained title"
    updated_fields = [item for item in doc["custom_fields"] if item["field"] != 5] + [
        {"field": 5, "value": "unrelated value must survive"}
    ]
    doc = api(
        state,
        f"/api/documents/{document_id}/",
        method="PATCH",
        body={
            "title": preserved_title,
            "tags": sorted(set(doc["tags"]) | {tag["id"]}),
            "custom_fields": updated_fields,
        },
    )
    preserved_fields = {key: value for key, value in fields(doc).items() if key != 4}
    original_environment = dict(state["env"])
    runs = []
    try:
        for provider in (
            "paperless_inference.providers.PaddleOCRProvider",
            "paperless.parsers.providers.DefaultParser",
            "paperless_inference.providers.PaddleOCRProvider",
        ):
            stop_app(state)
            state["env"]["PAPERLESS_PARSER"] = provider
            state["env"]["PAPERLESS_PROCESSOR_CAPABILITIES"] = (
                "parser metadata search ingestion"
            )
            save(state)
            write_compose(state)
            manage(
                state,
                "from django.core.management import call_command;call_command('document_processor_account','--user','server-processor','--capability','parser','--capability','metadata','--capability','search','--capability','ingestion')",
            )
            start_app(state)
            before = {
                task["task_id"]
                for task in api(
                    state, "/api/tasks/?task_type=reprocess_document&page_size=100"
                )["results"]
            }
            started = time.monotonic()
            api(
                state,
                "/api/documents/reprocess/",
                method="POST",
                body={"documents": [document_id]},
            )
            deadline = started + 1200
            while time.monotonic() < deadline:
                tasks = [
                    task
                    for task in api(
                        state, "/api/tasks/?task_type=reprocess_document&page_size=100"
                    )["results"]
                    if task["task_id"] not in before
                ]
                assert not any(
                    task["status"] in {"failure", "revoked"} for task in tasks
                ), "Reprocessing failed"
                if tasks and all(task["status"] == "success" for task in tasks):
                    document = api(state, f"/api/documents/{document_id}/")
                    matched = api(
                        state,
                        "/api/documents/?query="
                        + urllib.parse.quote(
                            "title:" + json.dumps(preserved_title, ensure_ascii=False)
                        ),
                    )["results"]
                    if document_id in {row["id"] for row in matched}:
                        break
                time.sleep(1)
            else:
                raise AssertionError(
                    "Reprocessing did not complete and become searchable"
                )
            assert document["title"] == preserved_title
            assert tag["id"] in document["tags"]
            assert all(
                fields(document).get(key) == value
                for key, value in preserved_fields.items()
            )
            assert "releasequalifier" in document["content"].lower()
            original = api(
                state, f"/api/documents/{document_id}/download/?original=true", raw=True
            )
            assert hashlib.sha256(original).hexdigest() == record["sha256"]
            archive = api(
                state,
                f"/api/documents/{document_id}/download/?original=false",
                raw=True,
            )
            import fitz
            from paperless_inference.archive import _validate_pdfa_2u

            with fitz.open(stream=archive, filetype="pdf") as archived_pdf:
                assert len(archived_pdf) == record["pages"]
                assert (
                    "releasequalifier"
                    in "".join(page.get_text() for page in archived_pdf).lower()
                )
            with TemporaryDirectory() as directory:
                archive_path = Path(directory) / "archive.pdf"
                archive_path.write_bytes(archive)
                if provider == "paperless.parsers.providers.DefaultParser":
                    from xml.etree import ElementTree as ET

                    validation = subprocess.run(
                        [
                            "verapdf",
                            "--flavour",
                            "2b",
                            "--format",
                            "xml",
                            str(archive_path),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=120,
                    )
                    reports = ET.fromstring(validation.stdout).findall(
                        ".//validationReport"
                    )
                    assert len(reports) == 1 and reports[0].get("isCompliant") == "true"
                else:
                    _validate_pdfa_2u(archive_path)
            runs.append(
                {
                    "provider": provider,
                    "seconds": time.monotonic() - started,
                    "task_ids": [task["task_id"] for task in tasks],
                    "original_hash_preserved": True,
                    "title_preserved": True,
                    "unrelated_tag_and_fields_preserved": True,
                    "metadata_preserved": True,
                    "archive_pdfa_valid": True,
                    "title_search_verified": True,
                }
            )
            (ROOT / "preservation.json").write_text(
                json.dumps({"document_id": document_id, "runs": runs}, indent=2) + "\n"
            )
            print(json.dumps(runs[-1]), flush=True)
    finally:
        stop_app(state)
        state["env"] = original_environment
        save(state)
        write_compose(state)
        capabilities = [
            part
            for capability in original_environment[
                "PAPERLESS_PROCESSOR_CAPABILITIES"
            ].split()
            for part in ("--capability", capability)
        ]
        manage(
            state,
            "from django.core.management import call_command;call_command('document_processor_account','--user','server-processor',"
            + ",".join(repr(value) for value in capabilities)
            + ")",
        )
        start_app(state)


def backup(args) -> None:
    """Stop writers and capture the paired database and private-file trees."""
    state = load()
    manage(
        state,
        "from documents.models import DocumentJob;assert not DocumentJob.objects.exclude(status__in=['succeeded','failed','rejected','cancelled','attention','committed']).exists(), 'Drain work before backup'",
    )
    stop_app(state)
    (ROOT / "database.dump").write_bytes(
        docker("exec", PREFIX + "-db", "pg_dump", "-U", "paperless", "-Fc", "paperless")
    )
    (ROOT / "private-files.tar").write_bytes(
        docker(
            "run",
            "--rm",
            "--userns=host",
            "-v",
            state["volumes"]["data"] + ":/snapshot/data:ro",
            "-v",
            state["volumes"]["media"] + ":/snapshot/media:ro",
            "--entrypoint",
            "tar",
            state["image"],
            "-C",
            "/snapshot",
            "-cf",
            "-",
            "data",
            "media",
        )
    )
    start_app(state)
    print(json.dumps({"paired_backup": True}))


def restore(args) -> None:
    """Restore the paired backup into fresh database/data/media volumes."""
    state = load()
    assert (ROOT / "database.dump").exists() and (ROOT / "private-files.tar").exists()
    stop_app(state)
    compose(state, "stop", "db")
    compose(state, "rm", "--force", "db")
    for role in ("db", "data", "media"):
        name = PREFIX + "-restored-" + role
        docker("volume", "create", name)
        state["volumes"][role] = name
        state["all_volumes"].append(name)
    save(state)
    start_database(state)
    docker(
        "exec",
        "-i",
        PREFIX + "-db",
        "pg_restore",
        "-U",
        "paperless",
        "-d",
        "paperless",
        "--exit-on-error",
        data=(ROOT / "database.dump").read_bytes(),
    )
    docker(
        "run",
        "--rm",
        "--userns=host",
        "-i",
        "-v",
        state["volumes"]["data"] + ":/snapshot/data",
        "-v",
        state["volumes"]["media"] + ":/snapshot/media",
        "--entrypoint",
        "tar",
        state["image"],
        "-C",
        "/snapshot",
        "-xf",
        "-",
        data=(ROOT / "private-files.tar").read_bytes(),
    )
    start_app(state)
    print(json.dumps({"paired_restore": True}))


def logs(args) -> None:
    """Save private logs for diagnosis without printing environment or tokens."""
    for role in ("app", "db", "broker"):
        (ROOT / (role + ".log")).write_bytes(docker("logs", PREFIX + "-" + role))


def cleanup(args) -> None:
    """Remove only this explicitly named disposable stack and its volumes."""
    state = load()
    for role in ("app", "db", "broker"):
        subprocess.run(
            ["docker", "rm", "-f", PREFIX + "-" + role],
            capture_output=True,
            check=False,
        )
    if os.environ.get("QUALIFICATION_CONTROLLER"):
        subprocess.run(
            [
                "docker",
                "network",
                "disconnect",
                PREFIX,
                os.environ["QUALIFICATION_CONTROLLER"],
            ],
            capture_output=True,
            check=False,
        )
    docker("network", "rm", PREFIX)
    for name in state["all_volumes"]:
        docker("volume", "rm", name)
    for path in Path(state["secret_dir"]).iterdir():
        path.unlink()
    Path(state["secret_dir"]).rmdir()
    print(json.dumps({"cleaned": True}))


def main() -> None:
    """Expose explicit phases so an external workstation can join between them."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "setup",
            "upload",
            "verify",
            "configure",
            "preservation",
            "backup",
            "restore",
            "logs",
            "cleanup",
        ),
    )
    parser.add_argument("--image")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--external-url", default="http://aperture.home.arpa:18000")
    parser.add_argument("--ocr-endpoint", default="http://cerebrum.home.arpa:8108")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--parser")
    parser.add_argument("--capabilities")
    args = parser.parse_args()
    if args.command == "setup" and not args.image:
        parser.error("setup requires --image")
    globals()[args.command](args)


if __name__ == "__main__":
    main()
