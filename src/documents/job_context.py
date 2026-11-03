"""Scope ingestion's durable completion ownership without disconnecting signals."""

from contextvars import ContextVar

active_ingestion_job: ContextVar[str | None] = ContextVar(
    "active_ingestion_job", default=None
)
