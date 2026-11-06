"""Adapt the retained pre-provider deployment to an isolated release benchmark.

Start PostgreSQL with databases ``paperless``, ``ai_chat`` and ``phoenix`` owned
by ``paperless``. Run SEED_PY through the old core's Django shell after migrations,
then start the listener and AI with their normal module commands. AI startup
creates the real document-added/updated workflows, stage tags and custom fields.
Mount the same private inference secret files used by the candidate. Never point
these environments at the production database, broker or Paperless API.
"""

import json
from collections.abc import Callable
from typing import Any

SOURCE_IMAGES = {
    "webserver": "sha256:5e5fddd812005d90f95542d232bbde717b4a010058def8ffeadbbb908be78614",
    "ai": "sha256:a7da476299bf22a312ba23ff71eed642c87e143724c02ef07ef9fdeb68c8a9ea",
    "webhook-listener": "sha256:8305bb1dbb18817156afb11501524aa164c1aacd3e18112fa8fdb81a81037906",
    "phoenix": "sha256:573971806f592833c84ea7c268d61638282bcedc56f8daf016b2358599260931",
}
IMAGES = {
    "webserver": "paperless-release-baseline-core:retained",
    "ai": "paperless-release-baseline-ai:retained",
    "webhook-listener": "paperless-release-baseline-listener:retained",
    "phoenix": "paperless-release-baseline-phoenix:retained",
}
CORE_REVISION = "8eb396d9833a56e6147092620b8832e39397f684"
FIELDS = {
    "processed": "ai_processed",
    "summary": "ai_summary",
    "audit": "ai_result",
    "ocr": "ai_ocr_output",
}

SEED_PY = """
import os
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
user, _ = get_user_model().objects.get_or_create(username='release-benchmark')
user.is_staff = True
user.is_superuser = True
user.set_unusable_password()
user.save()
Token.objects.update_or_create(user=user, defaults={'key': os.environ['BENCH_TOKEN']})
"""


def core_environment(common: dict[str, str], token: str) -> dict[str, str]:
    """Use the old service selectors and disable duplicate local inference."""
    return {
        **common,
        "PAPERLESS_USE_API_CONSUMER": "1",
        "PAPERLESS_USE_API_SCHEDULER": "1",
        "PAPERLESS_USE_LEAN_WORKER": "1",
        "PAPERLESS_API_TOKEN": token,
        "PAPERLESS_LOCAL_OCR_ENABLED": "false",
        "PAPERLESS_OCR_MODE": "off",
        "PAPERLESS_ARCHIVE_FILE_GENERATION": "never",
        "PAPERLESS_CLASSIFIER_ENABLED": "false",
        "PAPERLESS_TRAIN_TASK_CRON": "disable",
        "PAPERLESS_AI_ALLOWED": "false",
    }


def sidecar_environments(
    inference: dict[str, str],
    token: str,
    password: str,
    webhook_secret: str,
    poll_interval: int = 300,
) -> dict[str, dict[str, str]]:
    """Build clone settings; preserve the deployed 300-second polling default."""
    shared = {
        "PAPERLESS_URL": "http://webserver:8000",
        "PAPERLESS_TOKEN": token,
        "WEBHOOK_SECRET": webhook_secret,
        "REDIS_URL": "redis://broker:6379/1",
    }
    return {
        "webhook-listener": {**shared, "AGENT_NAME": "paperless-webhook-ingress"},
        "ai": {
            **shared,
            **inference,
            "AGENT_NAME": "paperless-ai",
            "MALLOC_ARENA_MAX": "2",
            "MANAGE_PAPERLESS_WORKFLOWS": "true",
            "PAPERLESS_WEBHOOK_URL": "http://webhook-listener:8001/webhook/document",
            "CHAT_DATABASE_URL": "postgresql://paperless@db:5432/ai_chat",
            "CHAT_DATABASE_PASSWORD": password,
            "INFERENCE_CHAT_MODEL": "openai/gpt-6-luna",
            "INFERENCE_CHAT_ENDPOINT": "https://openrouter.ai/api/v1",
            "POLL_INTERVAL": str(poll_interval),
            "OCR_CONCURRENCY": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://phoenix:6006/v1/traces",
        },
        "phoenix": {
            "PHOENIX_ENABLE_AUTH": "false",
            "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS": "90",
            "PHOENIX_DISABLE_AGENT_ASSISTANT": "true",
            "PHOENIX_POSTGRES_HOST": "db",
            "PHOENIX_POSTGRES_PORT": "5432",
            "PHOENIX_POSTGRES_USER": "paperless",
            "PHOENIX_POSTGRES_PASSWORD": password,
            "PHOENIX_POSTGRES_DB": "phoenix",
        },
    }


def resource_ids(api: Callable[..., Any]) -> dict[str, Any]:
    """Resolve fields and stage tags created by the real AI startup path."""
    fields = api("/api/custom_fields/?page_size=1000")["results"]
    tags = api("/api/tags/?page_size=1000")["results"]
    by_name = {row["name"]: row["id"] for row in fields}
    return {
        "fields": {role: by_name[name] for role, name in FIELDS.items()},
        "stages": {
            row["name"]: row["id"]
            for row in tags
            if row["name"] in ("ai:run-ocr", "ai:run-metadata")
        },
    }


def completed(
    document: dict[str, Any],
    resources: dict[str, Any],
    previous_audit: str | None = None,
) -> bool:
    """Require published OCR and metadata, not only initial ingestion success."""
    values = {row["field"]: row["value"] for row in document["custom_fields"]}
    fields = resources["fields"]
    if any(not values.get(fields[role]) for role in FIELDS):
        return False
    if set(document["tags"]) & set(resources["stages"].values()):
        return False
    audit = values[fields["audit"]]
    if not document.get("content", "").strip() or audit == previous_audit:
        return False
    assert isinstance(json.loads(audit)["ai_metadata"], dict)
    assert isinstance(json.loads(values[fields["ocr"]]), dict)
    return True


def begin_reprocess(
    api: Callable[..., Any], document: dict[str, Any], resources: dict[str, Any]
) -> str:
    """Re-admit through the old documented tag/workflow path."""
    audit_id = resources["fields"]["audit"]
    previous = next(
        row["value"] for row in document["custom_fields"] if row["field"] == audit_id
    )
    tags = sorted(set(document["tags"]) | {resources["stages"]["ai:run-ocr"]})
    api(f"/api/documents/{document['id']}/", method="PATCH", body={"tags": tags})
    return previous


if __name__ == "__main__":
    resources = {
        "fields": {role: index for index, role in enumerate(FIELDS)},
        "stages": {"ai:run-ocr": 9},
    }
    document = {
        "id": 1,
        "content": "Invoice",
        "tags": [],
        "custom_fields": [
            {"field": 0, "value": "2026-11-05"},
            {"field": 1, "value": "Summary"},
            {"field": 2, "value": '{"ai_metadata":{}}'},
            {"field": 3, "value": "{}"},
        ],
    }
    assert completed(document, resources)
    assert not completed(document, resources, '{"ai_metadata":{}}')
    assert not completed({**document, "tags": [9]}, resources)
    assert not completed(
        {**document, "custom_fields": document["custom_fields"][:-1]}, resources
    )
