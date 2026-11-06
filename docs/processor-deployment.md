# OCR and metadata processing through Celery

The supported example runs PostgreSQL, Redis, and one Paperless container with
its web server, consumption watcher, scheduler, and autoscaling Celery worker. Model endpoints remain external. The installed OCR adapter currently
accepts **PDF inputs only**; do not use this parser selection for a mixed-format
consumption folder. Keep the existing deployment available until your own
representative PDFs pass the release parity checks.

## Install and provision

Build the matching core and provider images using the commands in
[`providers/paperless-inference/README.md`](../providers/paperless-inference/README.md).
Copy `docker/compose/docker-compose.inference.yml` to a deployment directory as
`compose.yml`, and copy `docker/compose/inference.env` beside it. Create `.env`
with a strong `PAPERLESS_DBPASS` and, optionally, an immutable `PAPERLESS_IMAGE`
tag or digest. Create empty `consume` and `export` directories. Fill in
`inference.env` with a random secret key, your reachable model endpoints,
credentials if required, and immutable model identities. The OCR identities
must agree with that endpoint's `/metadata` response.

Start the stack and provision its dedicated account after migrations finish:

```sh
docker compose up -d
docker compose logs webserver
# After the init-complete message:
docker compose exec --user paperless webserver python manage.py document_processor_account \
  --user server-processor --capability parser --capability metadata \
  --capability search --capability ingestion
docker compose exec --user paperless webserver python manage.py createsuperuser
```

Provisioning is idempotent and replaces only the capabilities of a dedicated
non-login account; it refuses to repurpose a normal, administrative, disabled,
or group-bearing account. It schedules eligible pending jobs through Celery after
the account transaction commits. No document-edit grant or API token is needed
by server execution. The uploader/requester still needs normal document and
entity creation permissions. See the rich metadata contract for authorization.

Account activity and capability grants are checked before each operation.
After changing grants, rerun `document_processor_account` to resume pending work.

Create the desired metadata fields through core, before admitting documents.
Names match extracted keys exactly; absent definitions are ignored. OCR provenance
requires a separate longtext field:

```sh
docker compose exec --user paperless webserver python manage.py shell -c '
from documents.models import CustomField
for name, kind in [("summary", "longtext"), ("processing_date", "date"), ("audit", "longtext"), ("ai_ocr_output", "longtext")]:
    field, _ = CustomField.objects.get_or_create(name=name, defaults={"data_type": kind})
    assert field.data_type == kind, f"Existing {name} has incompatible type"
    if name == "ai_ocr_output":
        print("PAPERLESS_OCR_PROVENANCE_FIELD=" + str(field.pk))
'
```

Copy the printed OCR provenance setting into `inference.env`, then recreate the
container. Metadata fields need no ID mapping. For an existing deployment, follow
the [field migration instructions](rich-metadata-contract.md#custom-field-migration).

```sh
docker compose up -d --force-recreate webserver
```

Upload a representative PDF through the UI. Follow its consumption, enrichment,
and search tasks to successful completion; admission or a management-command
exit is not completion. Inspect its text, searchable archive, title/date,
summary, processing date, correspondent, language tag, metadata audit and OCR
provenance. Search for a distinctive extracted word. Upload another PDF after
an idle period and reprocess an existing document to confirm later work also
completes without launching commands manually.

## Placement and diagnostics

`PAPERLESS_PROCESSOR_USER` and the whitespace-separated
`PAPERLESS_PROCESSOR_CAPABILITIES` explicitly enable server execution. Supported
capabilities are `parser metadata search ingestion`; only include work this
host should run. Removing `parser`, for example, keeps parser work available
for an explicitly authorized workstation without local fallback. Do not confuse
external model inference with workstation placement: a server provider may call
an external endpoint when the retained request permits it.

With neither variable set, server provider execution is disabled. Pending work
remains in PostgreSQL for the workstation claim/result API. Server workers never
connect to or probe a workstation. A week of unclaimed waiting consumes no lease,
execution attempt, provider initialization, or recurring per-job Celery task.
An accepted workstation result schedules server continuation after commit and
release of the job lock. Inspect Tasks, retained errors, worker logs, and account
grants when unexpected waiting occurs. Provider identity and retained
configuration checks remain part of the shared runner.

Tasks distinguishes waiting, processing, checking a staged result, failed, and
review-required outcomes, and displays retained operation errors. Intentional
offline waiting is harmless. Unexpected waiting calls for the diagnostic
command, processor logs, account grants, and endpoint checks. Pure endpoint
failures retain their error and permit the existing bounded three attempts;
uncertain effectful failures require reconciliation rather than blind retries.
After correcting a terminal failure, inspect the retained job and submit fresh
work as appropriate. Cancelling does not undo completed work or endpoint costs.
Terminal processor failures also complete the top-level task with its retained
error and elapsed time, even when no further coordinator task runs. Startup and
scheduled recovery repair older terminal jobs whose task history still says
pending or started; completed task history is preserved.

## Restart, storage, and memory

Celery owns server execution. Each document task advances through ready server
operations, validating and accepting results locally and checkpointing between
stages. External inference runs outside database transactions. A task returns
when complete, cancelled, failed, or waiting for workstation-only work; unrelated
server jobs can use the available pool capacity.

`PAPERLESS_TASK_WORKERS` sets maximum concurrency. Both standard and lean workers
use a shared prefork pool that autoscales to zero children after idle, releasing
Django and provider memory. `PAPERLESS_WORKER_MAX_TASKS_PER_CHILD` bounds child
recycling (default `10`); a child may execute several tasks before replacement.
The custom lean Celery bootstrap remains available unchanged. Remove any old
`PAPERLESS_PROCESSOR_IDLE_TIMEOUT` or `PAPERLESS_PROCESSOR_CLASS` configuration;
the separate processor service and Redis wake queues are retired.

PostgreSQL remains authoritative for jobs, claims, and results; Redis is the
Celery broker and cache. Duplicate deliveries do not authorize duplicate effects.
Existing per-job file locks serialize execution. All server workers must share
the database, settings, and private job files on a filesystem with reliable
advisory locks; independent copies of `DATA_DIR/jobs` are unsupported. This
example does not establish multi-host/NFS safety.

The existing recovery task repairs missed dispatches and interrupted work, daily
at 03:00 by default. Configure `PAPERLESS_DOCUMENT_JOB_RECOVERY_CRON` or use
`disable` to turn off scheduled recovery. Manual recovery remains available.
A crash between commit and broker publication may delay work until recovery.
Pending workstation-only work remains waiting without repeated dispatches;
active leases remain untouched. Expired claims follow retained replay-safety
rules: uncertain external effects require reconciliation, not blind replay.
Whole-stack memory and latency claims require measurements; changing dispatch
alone does not establish an improvement.

Stop admission and processing before a coordinated backup. Preserve PostgreSQL,
the entire data volume (including private jobs and search data), and media
together, with the exact core/provider images and configuration. Restore the
matching set. Provider configuration rollback and database downgrade are
separate operations; see the [installation/swap/rollback runbook](provider-operations.md)
for release compatibility and pending-job decisions.
