"""Inspect durable document jobs and resume only explicitly safe completion."""

import json
from uuid import UUID

from django.core.management import CommandError
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents import ingestion_jobs
from documents import jobs
from documents.management.commands.base import PaperlessCommand
from documents.models import DocumentJob


class Command(PaperlessCommand):
    """Expose recovery evidence without replaying uncertain ingestion effects."""

    help = "Inspect a durable document job or redispatch safe pending work."
    supports_progress_bar = False
    supports_multiprocessing = False

    def add_arguments(self, parser) -> None:
        """Require a concrete job and explicit acknowledgment of uncertain effects."""
        super().add_arguments(parser)
        parser.add_argument("--job", type=UUID, required=True, help="Durable job UUID.")
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Redispatch recoverable work; never restart uncertain ingestion.",
        )
        parser.add_argument(
            "--acknowledge-effects",
            action="store_true",
            help=(
                "With --resume, acknowledge that a committed ingestion's started "
                "post-consume script/callbacks have been handled externally; do not "
                "repeat them. Inspect the job and reconcile those effects first."
            ),
        )

    def handle(self, *args, **options) -> None:
        """Print recovery evidence, optionally dispatching its safe remaining work."""
        if options["acknowledge_effects"] and not options["resume"]:
            raise CommandError("--acknowledge-effects requires --resume")
        try:
            job = DocumentJob.objects.get(pk=options["job"])
        except DocumentJob.DoesNotExist as error:
            raise CommandError("Document job does not exist") from error
        directory = jobs.job_directory(job.pk)
        if options["resume"]:
            try:
                with FileLock(directory.with_suffix(".lock"), timeout=0):
                    job.refresh_from_db()
                    if job.kind == "callback":
                        raise CommandError(
                            "Callback jobs cannot be replayed by this command. "
                            "Reconcile their effects, then acknowledge the parent "
                            "ingestion job's completion."
                        )
                    if (
                        job.kind == "consume"
                        and job.result.get("lifecycle")
                        and job.status == DocumentJob.Status.ATTENTION
                        and job.result.get("phase") == "preparing"
                    ):
                        if options["acknowledge_effects"]:
                            raise CommandError(
                                "Preparation requires a validated plugin reconciliation result, not acknowledgment"
                            )
                        job.status = DocumentJob.Status.RUNNING
                        job.save(update_fields=["status"])
                    elif (
                        job.kind == "consume"
                        and job.status == DocumentJob.Status.ATTENTION
                    ):
                        payload = job.result.get("payload")
                        if (
                            not isinstance(payload, dict)
                            or "document_id" not in payload
                        ):
                            raise CommandError(
                                "Uncommitted ingestion cannot be replayed safely. "
                                "Inspect the retained input and publication journal, "
                                "reconcile external effects and published files, then "
                                "submit a new ingestion only after that review. "
                                "This command leaves the job and its files unchanged."
                            )
                        if not options["acknowledge_effects"]:
                            raise CommandError(
                                "Committed ingestion requires --acknowledge-effects "
                                "after its script/callback effects have been reconciled."
                            )
                        for effect in ("post_consume", "delivery"):
                            if job.result.get(effect) in {
                                "started",
                                "failure-started",
                                "failure-done",
                            }:
                                job.result[effect] = "done"
                        job.result["callbacks_acknowledged"] = True
                        for entry in job.result.get("invocations", {}).values():
                            if entry["status"] in {"running", "unknown", "failed"}:
                                entry.update(
                                    status="succeeded", result=None, acknowledged=True
                                )
                        followup_ids = [
                            followup["id"]
                            for followup in job.result.get("workflow_followups", [])
                        ]
                        for callback in DocumentJob.objects.filter(
                            pk__in=followup_ids,
                            kind="callback",
                            status__in=[
                                DocumentJob.Status.RUNNING,
                                DocumentJob.Status.ATTENTION,
                                DocumentJob.Status.FAILED,
                            ],
                        ):
                            callback.status = DocumentJob.Status.SUCCEEDED
                            callback.result.setdefault("payload", None)
                            callback.result["acknowledged"] = True
                            callback.cleaned = True
                            callback.error = ""
                            callback.save(
                                update_fields=["status", "result", "cleaned", "error"],
                            )
                        job.status = DocumentJob.Status.COMMITTED
                        job.error = ""
                        job.updated = timezone.now()
                        job.save(update_fields=["result", "status", "error", "updated"])
                    elif options["acknowledge_effects"]:
                        raise CommandError(
                            "Effects may only be acknowledged for committed ingestion "
                            "that needs attention."
                        )
                    elif job.status not in {
                        DocumentJob.Status.QUEUED,
                        DocumentJob.Status.PREPARED,
                        DocumentJob.Status.COMMITTED,
                        DocumentJob.Status.SUCCEEDED,
                    }:
                        raise CommandError(
                            f"Job status {job.status!r} cannot be resumed by this command; "
                            "its recovery inputs remain retained."
                        )
            except Timeout as error:
                raise CommandError("The job is currently executing") from error
            if job.kind == "consume":
                ingestion_jobs.dispatch_consumption(job)
            else:
                jobs._dispatch(job)
        manifest_path = directory / "publication.json"
        report = {
            "id": str(job.pk),
            "kind": job.kind,
            "status": job.status,
            "error": job.error,
            "attempts": job.attempts,
            "timeout": job.timeout,
            "cleaned": job.cleaned,
            "staging_directory": str(directory),
            "source_path": job.input.get("source_path"),
            "publication": json.loads(manifest_path.read_text())
            if manifest_path.exists()
            else [],
            "payload": job.result.get("payload"),
            "post_consume": job.result.get("post_consume"),
            "delivery": job.result.get("delivery"),
            "followup": job.result.get("followup", []),
            "workflow_followups": job.result.get("workflow_followups", []),
            "progress": job.result.get("progress", job.status),
            "phase": job.result.get("phase"),
            "invocations": job.result.get("invocations", {}),
            "dispatched": options["resume"],
        }
        self.stdout.write(json.dumps(report, indent=2))
