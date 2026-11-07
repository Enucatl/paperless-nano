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

## Workstation pull execution

The optional pull client runs `python -m paperless_inference.workstation` in a
separate container on the workstation. It uses the same immutable core/provider
release as the server. It does not run a web server, Celery worker, scheduler,
PostgreSQL or Redis, and it does not mount the server's data or media directories.
Cerebrum's existing `/opt/docker/vllm` extraction stack remains the model server;
the pull client is a separate Compose project beside it, for example
`/opt/docker/vllm/paperless-workstation`.

```text
Paperless admission / Celery -> PostgreSQL durable operation
                                      ^
                                      | HTTPS claim / input / output / result
                                      |
                             Cerebrum pull client
                                      |
                                      v
                       PaddleOCR endpoint / metadata endpoint
```

The client lists authorized pending operations, claims one with a bounded lease,
downloads the declared immutable input, and runs the installed OCR or metadata
provider. It checks the retained provider/configuration identity and input hash.
It uploads private result files and submits the proposal with its claim token.
Paperless validates and accepts the proposal before scheduling server continuation
and ordinary search. A submitted result is not proof that document publication
completed; follow Tasks and verify the final document/search result.

Provision a dedicated non-login account with only the capabilities the workstation
will execute:

```sh
docker compose exec --user paperless webserver python manage.py document_processor_account \
  --user workstation-processor --capability parser --capability metadata
```

Create a DRF API token for that account using an administrator-controlled process,
and transfer it directly to a private workstation secret file without printing it
or adding it to source control. The account needs no document-edit permission and
must not be a superuser. The original document requester still needs the normal
permissions for the proposed document and entity changes.

For workstation-only OCR, remove `parser` from the server account's grants and
`PAPERLESS_PROCESSOR_CAPABILITIES`; keep `metadata search ingestion` on the server
if only OCR should move. For workstation-only metadata, remove `metadata` too.
The `ingestion` capability also executes the existing local thumbnail renderer;
disabling server OCR must not disable thumbnail publication. Retained thumbnail
operations admitted before this routing change still require their original
parser grant until drained; their authorization is not silently rewritten.
Reprovision the server account with its remaining capabilities and recreate the
server container. An offline unclaimed operation then remains durable without a
server execution attempt, occupied child, or outbound connection to Cerebrum.
The workstation independently initiates contact when it starts.

Build the client wrapper from the matching installed provider image:

```sh
docker build -f providers/paperless-inference/Dockerfile.workstation \
  --build-arg PAPERLESS_IMAGE=YOUR_IMMUTABLE_CORE_PROVIDER_IMAGE \
  -t YOUR_IMMUTABLE_WORKSTATION_IMAGE .
```

Copy `providers/paperless-inference/compose.workstation.yml` to the workstation
project as `compose.yml`. Set the nonsecret interpolation variables in `.env`:
`PAPERLESS_WORKSTATION_IMAGE`, `PAPERLESS_URL`, `INFERENCE_OCR_ENDPOINT`, and
`INFERENCE_METADATA_MODEL`. The model endpoint, pinned model identities,
metadata temperature and other provider settings must exactly match the server's
retained configuration. In particular, do not substitute `localhost` for the
configured Cerebrum hostname: the endpoint URL participates in OCR identity.
If the workstation resolves its own hostname to a loopback address, add a
deployment-specific `extra_hosts` mapping to Docker's `host-gateway` for that
hostname; do not change the retained endpoint URL to work around container DNS.

Set `PAPERLESS_API_TOKEN_SECRET_FILE` and `OPENROUTER_SECRET_FILE` to the private
credential file paths. Compose mounts both as secrets; their values do not belong
in `.env`, image layers, command arguments or logs. The client image runs as
UID/GID 1000, so the files must be readable by that user. Start the existing model
stack, then the client:

```sh
# In /opt/docker/vllm:
docker compose --profile extraction up -d --wait --wait-timeout 900
# In /opt/docker/vllm/paperless-workstation:
docker compose up -d
docker compose logs workstation
# Bounded single pass, with the resident client stopped first:
docker compose stop workstation
docker compose run --rm workstation --once
```

The client supports only `parser.process` and `metadata.enrich`; server-owned
search, ingestion extensions and lifecycle hooks remain on the server. One client
executes serially. While running, it checks for pending work every 30 seconds
when empty, or five seconds after a productive pass; this is workstation-side
admission polling, not the benchmark's 100 ms database observer. Logs report only
operation counts and error types. The workstation need not be running when work
is admitted.

The named `workstation_state` volume contains private downloaded files and a
computed-result delivery record. Persist it across container restarts. Delivery
retries reuse that record, rather than repeating successful inference after an
uncertain upload/submission response. A file lock prevents two processes sharing
that state from running concurrently. The record is removed after result staging
is acknowledged. If a claim expires or is cancelled, inspect the retained server
state and private client record before discarding it; the client does not bypass
claim checks or silently reroute to another Paperless URL. A crash during compute
can require a replay-safe retry and repeat model cost.

Only the workstation's outbound API and model connections are needed. No inbound
workstation worker port or shared server filesystem is introduced. Use HTTPS for
credential-bearing API traffic outside the trusted isolated test network.
