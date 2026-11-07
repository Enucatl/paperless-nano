"""Exercise authenticated transport, private files and replay-safe delivery."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from paperless_inference.files import file_reference
from paperless_inference.providers import MetadataProvider
from paperless_inference.providers import PaddleOCRProvider
from paperless_inference.workstation import APIError
from paperless_inference.workstation import Workstation


@pytest.fixture
def worker(tmp_path):
    """Serve actual HTTP so headers, paths and file transport are checked."""
    token = tmp_path / "token"
    token.write_text("private-api-token")
    calls = []
    replies = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.reply()

        def reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, dict(self.headers), body))
            assert self.headers["Authorization"] == "Token private-api-token"
            status, result = replies[(self.command, self.path)]
            if isinstance(result, list) and result and isinstance(result[0], tuple):
                status, result = result.pop(0)
            encoded = (
                result if isinstance(result, bytes) else json.dumps(result).encode()
            )
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    instance = Workstation(
        f"http://127.0.0.1:{server.server_port}", token, tmp_path / "state"
    )
    yield instance, replies, calls
    server.shutdown()
    server.server_close()
    thread.join()


def metadata_claim(monkeypatch):
    """Use the actual core envelope rather than an adapter-only configuration."""
    monkeypatch.setenv("INFERENCE_METADATA_ENDPOINT", "http://models:8080/v1")
    monkeypatch.setenv("INFERENCE_METADATA_MODEL", "retained-model")
    request = {
        "version": 1,
        "revision": "revision",
        "content": "Invoice",
        "identity": {"provider": "paperless.external.metadata", "contract": 1},
        "configuration": {},
        "managed_fields": {},
        "metadata": {},
        "date": "2026-11-07",
        "document_date_max": "2026-11-07",
        "candidates": {"tags": [], "correspondents": []},
    }
    return {
        "version": 2,
        "status": "leased",
        "remote_allowed": True,
        "plugin": "paperless.external.metadata",
        "operation": "metadata.enrich",
        "input": request,
        "token": "private-claim-token",
        "id": str(uuid4()),
    }


def pending(claim):
    """Describe one discoverable operation using the public list response."""
    return {
        "job_id": str(uuid4()),
        "key": "metadata",
        "status": "pending",
        "job_status": "waiting",
        "operation": claim["operation"],
        "contract": claim["plugin"],
    }


def test_metadata_delivery_retry_never_repeats_inference(worker, monkeypatch):
    """An uncertain result POST survives restart and uses the same token/proposal."""
    instance, replies, calls = worker
    claim = metadata_claim(monkeypatch)
    operation = pending(claim)
    base = instance.base(operation["job_id"], operation["key"])
    proposal = {
        "version": 1,
        "revision": "revision",
        "identity": claim["input"]["identity"],
        "title": "Invoice",
    }
    compute = Mock(return_value=proposal)
    monkeypatch.setattr(MetadataProvider, "enrich", compute)
    replies[("GET", "/api/operations/?status=pending")] = (200, [operation])
    replies[("POST", base + "claim/")] = (200, claim)
    replies[("POST", base + "result/")] = (503, {})
    with pytest.raises(APIError):
        instance.run_once()
    record = json.loads(instance.pending.read_text())
    assert record["result"]["title"] == proposal["title"]
    assert record["result"]["worker"]["model"] == "retained-model"
    proposal = record["result"]
    assert instance.pending.stat().st_mode & 0o777 == 0o600
    replies[("GET", "/api/operations/?status=pending")] = (200, [])
    replies[("POST", base + "result/")] = (202, {"status": "staged"})
    restarted = Workstation(instance.url, instance.token_file, instance.state)
    assert restarted.run_once() == 0
    assert not restarted.pending.exists()
    assert compute.call_count == 1
    submissions = [json.loads(c[3]) for c in calls if c[1].endswith("result/")]
    assert submissions == [{"token": claim["token"], "result": proposal}] * 2


def test_competing_claim_skips_computation(worker, monkeypatch):
    """A concurrent worker winning a lease does not initialize a provider."""
    instance, replies, _ = worker
    claim = metadata_claim(monkeypatch)
    operation = pending(claim)
    replies[("GET", "/api/operations/?status=pending")] = (200, [operation])
    replies[
        ("POST", instance.base(operation["job_id"], operation["key"]) + "claim/")
    ] = (409, {"error": "private"})
    compute = Mock()
    monkeypatch.setattr(instance, "compute", compute)
    assert instance.run_once() == 0
    compute.assert_not_called()


def test_metadata_configuration_belongs_to_worker(worker, monkeypatch):
    """A generic request executes with independently chosen model settings."""
    instance, _, _ = worker
    claim = metadata_claim(monkeypatch)
    monkeypatch.setenv("INFERENCE_METADATA_MODEL", "changed-model")
    compute = Mock(return_value={"version": 1, "revision": "revision"})
    monkeypatch.setattr(MetadataProvider, "enrich", compute)
    result = instance.compute(claim, "/unused/")
    assert compute.call_args.args[0]["identity"]["model"] == "changed-model"
    assert result["worker"]["model"] == "changed-model"
    assert result["identity"] == {
        "provider": "paperless.external.metadata",
        "contract": 1,
    }


def parser_claim(monkeypatch):
    """Retain a content-checked source and actual Paddle provider selection."""
    for name, value in {
        "ENDPOINT": "http://ocr:8108",
        "PIPELINE": "PaddleOCR-VL-1.6",
        "MODEL": "PaddlePaddle/PaddleOCR-VL-1.6",
        "LAYOUT_MODEL": "PP-DocLayoutV3",
    }.items():
        monkeypatch.setenv("INFERENCE_OCR_" + name, value)
    provider = PaddleOCRProvider()
    data = b"%PDF-original-fixture"
    request = {
        "version": 1,
        "revision": "revision",
        "source": {
            "path": "input/original.pdf",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "settings": {},
        "allow_remote": True,
        "mime_type": "application/pdf",
        "archive_policy": "always",
        "output_type": "pdfa",
        "output_dir": "parser-output",
    }
    return {
        "version": 2,
        "status": "leased",
        "remote_allowed": True,
        "plugin": "paperless.external.parser:v1",
        "operation": "parser.process",
        "token": "private-claim-token",
        "id": str(uuid4()),
        "input": {
            "request": request,
            "selection": provider.selection(request, Path("/unused")),
        },
    }, data


def test_parser_private_transport_and_archive_reference(worker, monkeypatch):
    """Verify input digest, upload actual bytes and submit the server reference."""
    instance, replies, calls = worker
    claim, data = parser_claim(monkeypatch)
    operation = pending(claim)
    operation["key"] = "parser.process"
    base = instance.base(operation["job_id"], operation["key"])
    replies[("GET", "/api/operations/?status=pending")] = (200, [operation])
    replies[("POST", base + "claim/")] = (200, claim)
    replies[("GET", base + "files/?path=input%2Foriginal.pdf")] = (200, data)
    archive = b"%PDF-searchable-output"
    remote = {
        "path": "parser-output/private-claim-token/archive.pdf",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size": len(archive),
    }
    replies[("POST", base + "files/")] = (201, remote)
    replies[("POST", base + "result/")] = (202, {"status": "staged"})

    def process(_self, request, root):
        assert (root / request["source"]["path"]).read_bytes() == data
        output = root / request["output_dir"] / "archive.pdf"
        output.parent.mkdir()
        output.write_bytes(archive)
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": PaddleOCRProvider.identity,
            "text": "Searchable invoice",
            "page_count": 1,
            "date": None,
            "archive": file_reference(root, output),
            "thumbnail": None,
            "archive_type": "pdfa",
        }

    monkeypatch.setattr(PaddleOCRProvider, "process", process)
    assert instance.run_once() == 1
    upload = next(c for c in calls if c[1] == base + "files/")
    assert archive in upload[3]
    assert upload[2]["X-Operation-Token"] == claim["token"]
    submission = json.loads(next(c[3] for c in calls if c[1] == base + "result/"))
    assert submission["result"]["archive"] == remote
    assert not (instance.state / "files").exists()


@pytest.mark.parametrize("path", ["../escape.pdf", "/tmp/escape.pdf"])
def test_parser_rejects_unsafe_input_path(worker, monkeypatch, path):
    """A hostile input cannot write outside the private operation directory."""
    instance, _, calls = worker
    claim, _ = parser_claim(monkeypatch)
    claim["input"]["request"]["source"]["path"] = path
    with pytest.raises(ValueError):
        instance.compute(claim, "/unused/")
    assert calls == []


def test_parser_rejects_corrupt_download(worker, monkeypatch):
    """Changed file bytes cannot reach inference even when the size matches."""
    instance, replies, _ = worker
    claim, data = parser_claim(monkeypatch)
    replies[("GET", "/unused/files/?path=input%2Foriginal.pdf")] = (
        200,
        b"x" * len(data),
    )
    process = Mock()
    monkeypatch.setattr(PaddleOCRProvider, "process", process)
    with pytest.raises(ValueError):
        instance.compute(claim, "/unused/")
    process.assert_not_called()


def test_pending_result_cannot_move_to_another_instance(worker):
    """Changing the API target cannot send a retained proposal to another server."""
    instance, _, calls = worker
    with pytest.raises(ValueError, match="another Paperless instance"):
        instance.deliver({"url": "http://other-instance"})
    assert calls == []


def test_worker_has_no_server_dependency():
    """The installed worker runs with neither Django nor Paperless installed."""
    import importlib.util

    for module in ("paperless", "documents", "django"):
        assert importlib.util.find_spec(module) is None


def test_discovery_skips_other_contracts_before_claim(worker, monkeypatch):
    """This worker leaves legacy or unknown implementations for eligible workers."""
    instance, replies, calls = worker
    operation = pending(metadata_claim(monkeypatch))
    operation["contract"] = "another-contract:v4"
    replies[("GET", "/api/operations/?status=pending")] = (200, [operation])
    assert instance.run_once() == 0
    assert len(calls) == 1


def test_metadata_rejects_unknown_contract_before_inference(worker, monkeypatch):
    """A new request contract cannot silently inherit current behavior."""
    instance, _, _ = worker
    claim = metadata_claim(monkeypatch)
    claim["input"]["identity"]["contract"] = 2
    compute = Mock()
    monkeypatch.setattr(MetadataProvider, "enrich", compute)
    with pytest.raises(ValueError, match="Unsupported metadata contract"):
        instance.compute(claim, "/unused/")
    compute.assert_not_called()


def test_parser_rejects_symlink_input_path(worker, monkeypatch, tmp_path):
    """File helpers cannot follow a link outside private scratch storage."""
    from paperless_inference.files import relative_path

    root = tmp_path / "files"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        relative_path(root, "escape/private.pdf")


@pytest.mark.parametrize("output_type", ["pdf", "pdfa-1", "pdfa-2", "pdfa-3"])
def test_parser_rejects_unsupported_archive_constraint(
    worker, monkeypatch, output_type
):
    """External requests use the generic PDF/A requirement, never local profiles."""
    instance, _, calls = worker
    claim, _ = parser_claim(monkeypatch)
    claim["input"]["request"]["output_type"] = output_type
    with pytest.raises(ValueError, match="cannot satisfy"):
        instance.compute(claim, "/unused/")
    assert calls == []


@pytest.mark.parametrize("field", ["version", "contract"])
def test_metadata_rejects_boolean_version(worker, monkeypatch, field):
    """Boolean values cannot masquerade as integer wire versions."""
    instance, _, _ = worker
    claim = metadata_claim(monkeypatch)
    target = claim["input"] if field == "version" else claim["input"]["identity"]
    target[field] = True
    with pytest.raises(ValueError, match="Unsupported metadata contract"):
        instance.compute(claim, "/unused/")
