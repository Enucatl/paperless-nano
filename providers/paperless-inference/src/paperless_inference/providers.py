"""Lazy adapters: compute proposals and private files, never edit documents."""

import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path

from paperless_inference.config import ProviderConfig
from paperless_inference.config import endpoint


def digest(value: object) -> str:
    """Bind an identity to finite, deterministic JSON."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


class MetadataProvider:
    """Extract the existing rich metadata behavior as an untrusted proposal."""

    uses_remote_service = True
    replay_safe = True

    @property
    def configuration(self) -> dict:
        """Snapshot effective model inputs without reading credentials."""
        config = asdict(ProviderConfig.metadata())
        del config["ocr_endpoint"], config["ocr_timeout"]
        config["correspondent_match_threshold"] = float(
            os.environ.get("INFERENCE_CORRESPONDENT_MATCH_THRESHOLD", "0.80")
        )
        if not 0 <= config["correspondent_match_threshold"] <= 1:
            raise ValueError("Correspondent threshold must be between zero and one")
        return config

    @property
    def identity(self) -> dict:
        """Pin implementation, model and the complete schema/prompt revision."""
        config = self.configuration
        return {
            "provider": "paperless-inference.metadata:1",
            "model": config["metadata_model"],
            "prompt": "structured-metadata:1:" + digest(config["metadata_prompt"]),
        }

    def enrich(self, request: dict) -> dict:
        """Compute normalized metadata against the retained authorized candidates."""
        from paperless_inference.metadata import StructuredOutputStrategy
        from paperless_inference.resolver import CorrespondentResolver

        if request["identity"] != self.identity:
            raise ValueError("Metadata identity changed")
        result = {
            "version": 1,
            "revision": request["revision"],
            "identity": self.identity,
        }
        if not request["content"].strip():
            return result
        config = ProviderConfig.metadata()
        extracted = asyncio.run(
            StructuredOutputStrategy().extract(request["content"], config)
        )
        result.update(
            summary=(extracted.summary or "").strip(), processing_date=request["date"]
        )
        if extracted.title:
            result["title"] = extracted.title[:128]
        if extracted.date and date(1900, 1, 1) <= extracted.date <= date.fromisoformat(
            request["document_date_max"]
        ):
            result["document_date"] = extracted.date.isoformat()
        resolution = None
        if extracted.correspondent and extracted.correspondent.strip():
            resolution = CorrespondentResolver(
                self.configuration["correspondent_match_threshold"]
            ).resolve(
                extracted.correspondent.strip(), request["candidates"]["correspondents"]
            )
            result["correspondent"] = (
                {"id": resolution.correspondent_id}
                if resolution.action == "existing"
                else {"create": extracted.correspondent.strip()}
            )
        language = "language:" + (extracted.languages or ["und"])[0]
        existing = next(
            (tag for tag in request["candidates"]["tags"] if tag["name"] == language),
            None,
        )
        result["language"] = (
            {"id": existing["id"]} if existing else {"create": language}
        )
        result["audit"] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metadata_model": config.metadata_model,
            "metadata_endpoint": config.metadata_endpoint,
            "paperless_version": request["metadata"].get("paperless_version"),
            "ai_metadata": {
                "title": extracted.title,
                "document_date": extracted.date.isoformat() if extracted.date else None,
                "correspondent": extracted.correspondent,
                "summary": extracted.summary,
                "languages": extracted.languages,
            },
            **(
                {"correspondent_resolution": resolution.to_audit_dict()}
                if resolution
                else {}
            ),
        }
        return result


class PaddleOCRProvider:
    """Run full-document Paddle OCR with core-owned archive/provenance publication."""

    identity = "paperless-inference.paddle:1"
    uses_remote_service = True
    replay_safe = True

    def configuration_snapshot(self) -> dict:
        """Pin deployment identity locally; network checks happen during execution."""
        metadata = {
            key: os.environ.get("INFERENCE_OCR_" + key.upper(), "")
            for key in ("pipeline", "model", "layout_model")
        }
        if any(not value.strip() for value in metadata.values()):
            raise ValueError(
                "Pin INFERENCE_OCR_PIPELINE, MODEL and LAYOUT_MODEL from /metadata"
            )
        timeout = float(os.environ.get("INFERENCE_OCR_TIMEOUT", "600"))
        if timeout <= 0:
            raise ValueError("INFERENCE_OCR_TIMEOUT must be positive")
        return {
            "endpoint": endpoint("INFERENCE_OCR_ENDPOINT"),
            "timeout": timeout,
            "metadata": metadata,
        }

    def selection(self, request: dict, root: Path) -> dict:
        """Bind execution to the retained endpoint and model deployment."""
        return {
            "identity": self.identity + ":" + digest(request["settings"]),
            "uses_remote_service": True,
            "replay_safe": True,
        }

    def process(self, request: dict, root: Path) -> dict:
        """Create validated private OCR output without any authoritative writeback."""
        from paperless.parsers.contract import file_reference
        from paperless.parsers.contract import relative_path
        from paperless.parsers.contract import resolve_reference
        from paperless_inference.paddle_ocr import fetch_ocr_metadata
        from paperless_inference.paddle_ocr import run_paddle_ocr

        if not request["allow_remote"] or request["mime_type"] != "application/pdf":
            raise ValueError("Paddle requires a PDF with explicit remote permission")
        settings = request["settings"]
        if settings != self.configuration_snapshot():
            raise ValueError("OCR configuration changed")
        source = resolve_reference(root, request["source"])
        output = relative_path(root, request["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        config = ProviderConfig(
            ocr_endpoint=settings["endpoint"], ocr_timeout=settings["timeout"]
        )

        async def run() -> tuple:
            """Verify the pinned service identity before spending an OCR request."""
            metadata = await fetch_ocr_metadata(config)
            if metadata != settings["metadata"]:
                raise ValueError("OCR endpoint model identity changed")
            archive = (
                output / "archive.pdf"
                if request["archive_policy"] != "never"
                and metadata["layout_model"] != "none"
                else None
            )
            text, provenance, pages, _ = await run_paddle_ocr(
                str(source), config, metadata, archive_path=archive
            )
            if await fetch_ocr_metadata(config) != metadata:
                raise ValueError("OCR endpoint model identity changed during execution")
            return text, provenance, pages, archive

        text, provenance, pages, archive = asyncio.run(run())
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": self.identity,
            "execution": self.selection(request, root)["identity"],
            "text": text,
            "page_count": pages,
            "date": None,
            "archive": file_reference(root, archive) if archive else None,
            "archive_type": "pdfa" if archive else None,
            "thumbnail": None,
            "provenance": provenance,
        }
