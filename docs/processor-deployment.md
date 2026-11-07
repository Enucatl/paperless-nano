# Generic external worker deployment

The server runs three containers: one generic Paperless image, PostgreSQL 18 and
Redis 8. Paperless contains its web server, watcher, scheduler and autoscaling
Celery worker. Independently packaged external workers connect through the
[operation API](external-worker-contract.md); they own model endpoints, prompts,
libraries and credentials. The server does not install `paperless-inference` or
need to know which OCR/model implementation a worker uses.

The supplied worker is one example using PaddleOCR for PDFs and an
OpenAI-compatible endpoint for metadata. It runs in a separate container on
Cerebrum alongside the existing model services. Any compatible worker may replace
it without rebuilding the Paperless image. At least one compatible worker must
eventually run for the external operations in this example to complete.
External archives must be PDF/A; the worker chooses the flavor. The example
worker validates PDF/A-2u. Local built-in parser output settings are unchanged.

## Install and provision

Build the generic core image from the selected release:

```sh
docker build -t paperless-core:release .
```

Copy `docker/compose/docker-compose.inference.yml` to a deployment directory as
`compose.yml`, and copy `docker/compose/inference.env` beside it. Create `.env`
with a strong `PAPERLESS_DBPASS` and an immutable `PAPERLESS_IMAGE` tag or digest.
Create empty `consume` and `export` directories and set a random secret key in
`inference.env`. Keep worker endpoint/model settings and credentials on the
worker host.

The Compose example selects `documents.external_workers.ExternalParser` and
`documents.external_workers.ExternalMetadata`, enables remote processing, and
places only `search ingestion` on the server. These core classes describe work
and validate the contract; they do not run an inference implementation.

Start the stack and provision its dedicated account after migrations finish:

```sh
docker compose up -d
docker compose logs webserver
# After the init-complete message:
docker compose exec --user paperless webserver python manage.py document_processor_account \
  --user server-processor --capability search --capability ingestion
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
`PAPERLESS_PROCESSOR_CAPABILITIES` enable eligible local operations. Keep
`search ingestion` on the server so search, ingestion preparation and thumbnail
rendering complete after external results arrive. The generic external OCR and
metadata descriptors always wait for an external worker, even if a server
account accidentally has `parser` or `metadata` grants.

Unclaimed external work stays in PostgreSQL without consuming a lease, execution
attempt or worker child. Core does not connect to the worker or its models. A
worker initiates an authenticated claim, then uploads outputs and submits its
result. Acceptance schedules server continuation after commit and release of the
job lock. The worker's implementation/model/prompt provenance is retained with the
result; it is not a Python implementation that the server must load.

Tasks distinguishes waiting, processing, checking a staged result, failed, and
review-required outcomes, and displays retained operation errors. Intentional
offline waiting is harmless. Unexpected waiting calls for the diagnostic
command, worker logs, account grants, and endpoint checks. Unfinished computations
can be retried after lease recovery within the existing bound of three attempts;
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
Django and local processing memory. `PAPERLESS_WORKER_MAX_TASKS_PER_CHILD` bounds child
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
together, with the server and worker image digests and their configuration. Restore the
matching set. Worker rollback and database downgrade are
separate operations; see the [installation/swap/rollback runbook](provider-operations.md)
for release compatibility and pending-job decisions.

## Independent worker execution

A worker needs outbound access to Paperless's authenticated HTTPS API and its own
model endpoints. It needs no server database, Redis, shared document storage or
inbound worker port. Workers may use any language and dependencies as long as they
implement the [versioned operation contract](external-worker-contract.md).

Provision a dedicated account with only its required capabilities:

```sh
docker compose exec --user paperless webserver python manage.py document_processor_account \
  --user workstation-processor --capability parser --capability metadata
```

Create a DRF API token for that account through an administrator-controlled
process. Transfer it directly to a private worker secret file without printing
or committing it. The account needs no document-edit permission and must not be
a superuser. Core still checks the original requester's document and entity
permissions when applying results.

To use the example worker, build its independent image:

```sh
docker build -f providers/paperless-inference/Dockerfile \
  -t paperless-inference:worker .
```

Copy `providers/paperless-inference/compose.workstation.yml` to its own deployment
project as `compose.yml`. Set `PAPERLESS_WORKSTATION_IMAGE` to that immutable image,
`PAPERLESS_URL` to the server API base URL, and `PAPERLESS_API_TOKEN_SECRET_FILE`
to the private API token file. Configure `INFERENCE_OCR_ENDPOINT`, OCR model
identities, `INFERENCE_METADATA_MODEL` and other inference settings on this worker
only. Set `OPENROUTER_SECRET_FILE` to its private model credential file.
See the [worker package guide](../providers/paperless-inference/README.md) for the
full implementation-specific setup. There is no server image or Python class to
match; server and worker must support the same wire contract.

The example runs as UID/GID 1000. Its secret files must be readable by that user.
Persist its private `workstation_state` volume across restarts: it contains
inputs and computed results awaiting delivery. Start its model services, then:

```sh
docker compose up -d
docker compose logs workstation
# One discovery pass, with the resident client stopped:
docker compose stop workstation
docker compose run --rm workstation --once
```

The example handles `parser.process` and `metadata.enrich` serially. It polls
while idle, claims supported work, validates input hashes, computes outputs,
uploads private files and submits the proposal. Core validates and publishes
results, then completes local thumbnail and search work. Follow Tasks to terminal
success; a submitted result alone is not publication.

Computed-result retries reuse the private delivery record. If a claim expires,
is cancelled or loses authorization, inspect server state and the retained record
before discarding it. A crash during computation may require a replay-safe retry
and repeat model cost. Use HTTPS for credential-bearing API traffic outside a
trusted isolated test network.

## Upgrade from installed providers

The former deployment selected `paperless_inference.providers.*` on the server
and required matching provider settings in its workstation wrapper. Drain that
work under the old images/configuration before switching. Explicitly review or
cancel jobs that cannot drain; preserve their private files and receipts. Never
rewrite a retained provider identity to make an old job look like a new generic
request.

Back up the paired database/data/media state, then switch the server to the
core-only image and the descriptor classes shown above. Remove server-side
`INFERENCE_*` and model credentials. Start compatible independently packaged
workers and admit new work. Retained thumbnail operations may still require their
original `parser` grant; drain them before removing it. See the
[upgrade and rollback runbook](provider-operations.md) for the complete sequence.
