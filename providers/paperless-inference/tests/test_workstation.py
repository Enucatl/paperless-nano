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

from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference
from paperless_inference.provider_operations import ProviderMismatch
from paperless_inference.providers import MetadataProvider
from paperless_inference.providers import PaddleOCRProvider
from paperless_inference.workstation import APIError
from paperless_inference.workstation import Workstation
from paperless_inference.workstation import map_files


class AlternativeParser:
    """Implement the parser contract without Paddle settings or custom selection."""

    identity = "test-parser:1"
    uses_remote_service = False
    replay_safe = True
    calls = 0

    def configuration_for(self, context: dict) -> dict:
        """Use the shared contextual configuration convention."""
        assert context == {}
        return {"mode": "portable"}

    def process(self, request: dict, root: Path) -> dict:
        """Read every immutable input and emit different files sharing a basename."""
        type(self).calls += 1
        assert resolve_reference(root, request["source"]).read_bytes() == b"source"
        assert (
            resolve_reference(root, request["attachments"][0]).read_bytes()
            == b"sidecar"
        )
        references = {}
        for field, data in {"archive": b"archive", "thumbnail": b"thumbnail"}.items():
            path = root / request["output_dir"] / field / "output.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            references[field] = file_reference(root, path)
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": self.identity,
            "text": "Portable parser",
            "page_count": 1,
            "date": None,
            "archive_type": "searchable_pdf",
            **references,
        }


class AlternativeMetadata:
    """Implement the rich metadata contract without inference model settings."""

    identity = {"provider": "test-metadata:1", "model": "local", "prompt": "fixed"}
    configuration = {"title": "Local metadata"}
    uses_remote_service = False
    replay_safe = True

    def enrich(self, request: dict) -> dict:
        """Return an ordinary metadata proposal."""
        return {
            "version": 1,
            "revision": request["revision"],
            "identity": self.identity,
            "title": self.configuration["title"],
        }


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


def metadata_claim(monkeypatch, provider_class=MetadataProvider):
    """Use the actual core envelope rather than an adapter-only configuration."""
    monkeypatch.setenv("INFERENCE_METADATA_ENDPOINT", "http://models:8080/v1")
    monkeypatch.setenv("INFERENCE_METADATA_MODEL", "retained-model")
    provider = provider_class()
    request = {
        "version": 1,
        "revision": "revision",
        "content": "Invoice",
        "identity": provider.identity,
        "configuration": {
            "configuration": provider.configuration,
            "provider": f"{provider_class.__module__}.{provider_class.__qualname__}",
            "allow_remote": True,
            "uses_remote_service": provider.uses_remote_service,
            "replay_safe": provider.replay_safe,
        },
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
        "plugin": provider.identity["provider"],
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
    assert record["result"] == proposal
    assert instance.pending.stat().st_mode & 0o777 == 0o600
    replies[("GET", "/api/operations/?status=pending")] = (200, [])
    replies[("POST", base + "result/")] = (202, {"status": "staged"})
    monkeypatch.setenv("PAPERLESS_ENRICHMENT_PROVIDER", "not_installed.Provider")
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


def test_metadata_changed_configuration_rejected(worker, monkeypatch):
    """Retained configuration cannot silently execute against changed settings."""
    instance, _, _ = worker
    claim = metadata_claim(monkeypatch)
    monkeypatch.setenv("INFERENCE_METADATA_MODEL", "changed-model")
    with pytest.raises(ValueError, match="configuration changed"):
        instance.compute(claim, "/unused/")


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
        "settings": provider.configuration_snapshot(),
        "allow_remote": True,
        "mime_type": "application/pdf",
        "archive_policy": "always",
        "output_dir": "parser-output",
    }
    return {
        "version": 2,
        "status": "leased",
        "remote_allowed": True,
        "plugin": provider.identity,
        "operation": "parser.process",
        "token": "private-claim-token",
        "id": str(uuid4()),
        "input": {
            "request": request,
            "selection": provider.selection(request, Path("/unused")),
        },
    }, data


@pytest.mark.parametrize("restart", [False, True])
def test_parser_private_transport_and_archive_reference(worker, monkeypatch, restart):
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
    replies[("POST", base + "result/")] = (
        (503, {}) if restart else (202, {"status": "staged"})
    )

    def process(_self, request, root):
        assert (root / request["source"]["path"]).read_bytes() == data
        output = root / request["output_dir"] / "archive.pdf"
        output.parent.mkdir()
        output.write_bytes(archive)
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": PaddleOCRProvider.identity,
            "execution": claim["input"]["selection"]["identity"],
            "text": "Searchable invoice",
            "page_count": 1,
            "date": None,
            "archive": file_reference(root, output),
            "thumbnail": None,
            "archive_type": "pdfa",
        }

    monkeypatch.setattr(PaddleOCRProvider, "process", process)
    if restart:
        with pytest.raises(APIError):
            instance.run_once()
        record = json.loads(instance.pending.read_text())
        # Older clients indexed acknowledged uploads by result field name.
        record["uploaded"] = {"archive": remote}
        instance.save(record)
        monkeypatch.setenv("PAPERLESS_PARSER", "not_installed.Provider")
        replies[("POST", base + "result/")] = (202, {"status": "staged"})
        replies[("GET", "/api/operations/?status=pending")] = (200, [])
        instance = Workstation(instance.url, instance.token_file, instance.state)
        assert instance.run_once() == 0
        assert len([c for c in calls if c[1] == base + "files/"]) == 1
    else:
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


@pytest.mark.parametrize("corruption", ["digest", "oversized", "truncated"])
def test_parser_rejects_corrupt_download(worker, monkeypatch, corruption):
    """Changed file bytes cannot reach inference even when the size matches."""
    instance, replies, _ = worker
    claim, data = parser_claim(monkeypatch)
    corrupt = {
        "digest": b"x" * len(data),
        "oversized": data + b"extra",
        "truncated": data[:-1],
    }[corruption]
    replies[("GET", "/unused/files/?path=input%2Foriginal.pdf")] = (200, corrupt)
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


def alternative_claim(monkeypatch):
    """Select another installed provider using configuration alone."""
    monkeypatch.setenv("PAPERLESS_PARSER", f"{__name__}.AlternativeParser")
    claim, _ = parser_claim(monkeypatch)
    claim["plugin"] = AlternativeParser.identity
    claim["input"]["selection"] = {
        "identity": AlternativeParser.identity,
        "uses_remote_service": False,
        "replay_safe": True,
    }
    request = claim["input"]["request"]
    request["settings"] = {"mode": "portable"}
    request["source"] = {
        "path": "input/source.bin",
        "sha256": hashlib.sha256(b"source").hexdigest(),
        "size": 6,
    }
    request["attachments"] = [
        {
            "path": "input/sidecar.bin",
            "sha256": hashlib.sha256(b"sidecar").hexdigest(),
            "size": 7,
        }
    ]
    return claim


def test_configured_parser_transports_declared_files(worker, monkeypatch):
    """Another installed provider uses the same authenticated parser exchange."""
    instance, replies, calls = worker
    claim = alternative_claim(monkeypatch)
    operation = pending(claim)
    base = instance.base(operation["job_id"], operation["key"])
    replies[("GET", "/api/operations/?status=pending")] = (200, [operation])
    replies[("POST", base + "claim/")] = (200, claim)
    replies[("GET", base + "files/?path=input%2Fsource.bin")] = (200, b"source")
    replies[("GET", base + "files/?path=input%2Fsidecar.bin")] = (200, b"sidecar")
    remote = [
        {
            "path": f"parser-output/claim/{field}.bin",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        for field, data in (("archive", b"archive"), ("thumbnail", b"thumbnail"))
    ]
    replies[("POST", base + "files/")] = (201, [(201, ref) for ref in remote])
    replies[("POST", base + "result/")] = (202, {"status": "staged"})
    AlternativeParser.calls = 0
    assert instance.run_once() == 1
    assert AlternativeParser.calls == 1
    uploads = [c for c in calls if c[1] == base + "files/"]
    assert len(uploads) == 2
    assert uploads[0][3] != uploads[1][3]
    assert b'filename="' + remote[0]["sha256"].encode() + b'.bin"' in uploads[0][3]
    assert b'filename="' + remote[1]["sha256"].encode() + b'.bin"' in uploads[1][3]
    submission = json.loads(next(c[3] for c in calls if c[1] == base + "result/"))
    assert submission["result"]["archive"] == remote[0]
    assert submission["result"]["thumbnail"] == remote[1]


@pytest.mark.parametrize(
    "mismatch", ["identity", "configuration", "selection", "class"]
)
def test_configured_parser_rejects_retained_mismatch(worker, monkeypatch, mismatch):
    """Old-provider work cannot silently execute under a replacement."""
    instance, replies, _ = worker
    claim = alternative_claim(monkeypatch)
    if mismatch == "identity":
        claim["plugin"] = "previous-provider:1"
    elif mismatch == "configuration":
        claim["input"]["request"]["settings"] = {"mode": "changed"}
    elif mismatch == "selection":
        claim["input"]["selection"]["replay_safe"] = False
    else:
        monkeypatch.setenv(
            "PAPERLESS_PARSER", "paperless_inference.providers.PaddleOCRProvider"
        )
    replies[("GET", "/unused/files/?path=input%2Fsource.bin")] = (200, b"source")
    replies[("GET", "/unused/files/?path=input%2Fsidecar.bin")] = (200, b"sidecar")
    process = Mock()
    monkeypatch.setattr(AlternativeParser, "process", process)
    with pytest.raises(ProviderMismatch):
        instance.compute(claim, "/unused/")
    process.assert_not_called()


@pytest.mark.parametrize("allow_remote", [False, True])
def test_configured_metadata_provider(worker, monkeypatch, allow_remote):
    """Metadata selection uses the same local dotted-path convention."""
    instance, _, calls = worker
    monkeypatch.setenv(
        "PAPERLESS_ENRICHMENT_PROVIDER", f"{__name__}.AlternativeMetadata"
    )
    claim = metadata_claim(monkeypatch, AlternativeMetadata)
    claim["input"]["configuration"]["allow_remote"] = allow_remote
    assert instance.compute(claim, "/unused/")["title"] == "Local metadata"
    assert calls == []


@pytest.mark.parametrize("allow_remote", [False, "true"])
def test_metadata_provider_respects_retained_remote_policy(
    worker, monkeypatch, allow_remote
):
    """Remote computation requires an explicit boolean permission from core."""
    instance, _, calls = worker
    claim = metadata_claim(monkeypatch)
    claim["input"]["configuration"]["allow_remote"] = allow_remote
    enrich = Mock()
    monkeypatch.setattr(MetadataProvider, "enrich", enrich)
    with pytest.raises(ProviderMismatch):
        instance.compute(claim, "/unused/")
    enrich.assert_not_called()
    assert calls == []


def test_provider_cannot_mutate_retained_validation_policy(worker, monkeypatch):
    """Mutating the runtime request or source cannot rewrite the frozen contract."""
    instance, replies, _ = worker
    claim, data = parser_claim(monkeypatch)
    original_revision = claim["input"]["request"]["revision"]
    replies[("GET", "/unused/files/?path=input%2Foriginal.pdf")] = (200, data)

    def process(_self, request, root):
        """Try to hide source mutation by rewriting the runtime request."""
        source = root / request["source"]["path"]
        source.write_bytes(b"changed")
        request["source"] = file_reference(root, source)
        request["revision"] = "changed"
        return {
            "version": 1,
            "revision": original_revision,
            "provider": PaddleOCRProvider.identity,
            "execution": PaddleOCRProvider().selection(request, root)["identity"],
            "text": "Invalid",
            "page_count": 1,
            "date": None,
            "archive": None,
            "thumbnail": None,
            "archive_type": None,
        }

    monkeypatch.setattr(PaddleOCRProvider, "process", process)
    with pytest.raises(ValueError, match="changed after it was referenced"):
        instance.compute(claim, "/unused/")
    assert claim["input"]["request"]["revision"] == original_revision


def test_provider_cannot_mutate_additional_immutable_input(worker, monkeypatch):
    """Inputs beyond the source retain their content identity after computation."""
    instance, replies, _ = worker
    claim = alternative_claim(monkeypatch)
    replies[("GET", "/unused/files/?path=input%2Fsource.bin")] = (200, b"source")
    replies[("GET", "/unused/files/?path=input%2Fsidecar.bin")] = (200, b"sidecar")
    original = AlternativeParser.process

    def process(self, request, root):
        """Try to hide mutation of another declared immutable input."""
        result = original(self, request, root)
        path = resolve_reference(root, request["attachments"][0])
        path.write_bytes(b"changed")
        request["attachments"][0] = file_reference(root, path)
        return result

    monkeypatch.setattr(AlternativeParser, "process", process)
    with pytest.raises(ValueError, match="changed after it was referenced"):
        instance.compute(claim, "/unused/")


def test_unsupported_operation_is_not_claimed_or_executed(worker):
    """Generic file transport does not grant arbitrary journal execution."""
    instance, replies, calls = worker
    operation = pending({"operation": "search.write"})
    replies[("GET", "/api/operations/?status=pending")] = (200, [operation])
    assert instance.run_once() == 0
    with pytest.raises(ValueError, match="Unsupported workstation operation"):
        instance.compute(
            {**operation, "version": 2, "status": "leased", "remote_allowed": True},
            "/unused/",
        )
    assert len(calls) == 1


def test_file_transport_walks_explicit_nested_references():
    """Transport keeps contract structure and ignores ordinary scalar fields."""
    reference = {"path": "private/file", "size": 1, "sha256": "digest"}
    value = {"diagnostics": [{"rendition": reference}], "text": "unchanged"}
    remote = {**reference, "path": "remote/file"}
    transfer = Mock(return_value=remote)
    assert map_files(value, transfer) == {
        "diagnostics": [{"rendition": remote}],
        "text": "unchanged",
    }
    transfer.assert_called_once_with(reference)
