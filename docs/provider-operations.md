# Install, replace, and roll back external workers

Run job-admitting and recovery commands as the application user (`--user
paperless` in the supplied image, or your configured runtime UID). Root-created
private job files can prevent the supervised worker from opening its locks.

This is the current operator runbook for the OCR and rich metadata integration
(#43, #49 and #59). Historical design notes remain in `document-operations.md`.
Use the [supervised deployment guide](processor-deployment.md) for the concrete
PostgreSQL Compose setup, account provisioning, and managed-field creation.
Keep the previous deployment available until your representative documents pass
all checks below. A passing unit test suite alone is not a release qualification.
See the [validation report](inference-release-validation.md) for the accepted
historical qualification and current generic-worker publication gates, and the
[release notes](changelog.md#first-inference-release-unreleased) for upgrade requirements.

## Compatibility and required decisions

| Component | Supported combination |
| --- | --- |
| Core | Generic Paperless image with the external-worker contract; the `3.4.1` package version alone does not identify these fork changes. |
| External worker | Any independent implementation of the [HTTP contract](external-worker-contract.md); no Paperless Python imports or matching server image required. |
| Server runtime | Python 3.14, PostgreSQL 18 and Redis 8 for the supplied deployment. |
| Example worker | Standalone `paperless-inference` package, with its own PaddleOCR and metadata endpoints, model configuration, credentials and PDF tools. Its OCR implementation accepts PDFs only. |
| Storage | Server database and local data/media storage with reliable advisory file locks. External workers use their own private scratch/delivery storage. Shared multi-host/NFS server storage is not established by this example. |

Save server and worker image digests, supported contract versions, configuration
and managed-field IDs with each deployment. Workers own endpoint/model/prompt
selection and credentials. The core retains their reported execution provenance
but does not import or configure their implementations. Protect worker secret
files and backups; never add raw qualification archives to a release.

The supplied server selects the core `ExternalParser` and `ExternalMetadata`
descriptors. These create externally executed operations and cannot run locally.
Keep `search ingestion` capabilities on the server; grant `parser metadata` only
to the external worker accounts that need them. Remote processing still needs
the document-level OCR permission and `PAPERLESS_ENRICHMENT_ALLOW_REMOTE=true`.
The Compose example enables the remote OCR default. Saved application settings
can override environment defaults.

## Install and verify configuration

Follow the [server deployment guide](processor-deployment.md) and
[example worker guide](../providers/paperless-inference/README.md). Build and
version them independently. Keep all processes in one server deployment on its
same image/configuration; external workers only need contract compatibility.
The former external listener and direct document API-writeback runner must not
run alongside the durable operation pipeline.

Provision the dedicated processor account before uploading. To retain all bundled
metadata outputs, create fields named exactly `summary` (longtext),
`processing_date` (date), and `audit` (longtext). Confirm existing types rather
than replacing same-name definitions. Additional extracted keys match existing
field names exactly; unmatched keys are ignored and definitions are never created
automatically. No field-ID mapping is needed. OCR provenance needs its own
configured longtext field, excluded from metadata targets. Existing installations
must follow the [field migration instructions](rich-metadata-contract.md#custom-field-migration).
The requesting user needs document-edit access and permission to create
correspondents/tags when a proposal requests new entities; processor capability
grants do not confer document-edit authority.

Check the effective setup without processing pending work:

```sh
docker compose exec --user paperless webserver python manage.py check
```

Read the Celery worker logs and retained operation errors for import, field,
permission, and endpoint errors. System checks do not prove endpoint connectivity
or actual processing. Run the completion checks below before opening general
admission.

## Stop admission and drain before a swap

Pause upload clients and user uploads, scheduled mail ingestion, scanners writing
the consumption folder, and any external submitters. Temporarily disable the
consumer and mail schedule if needed, while leaving coordinators and eligible
processors running to drain admitted jobs. Include scheduled maintenance and
index writes in the drain. Check Tasks and inspect outstanding durable jobs:

```sh
docker compose exec --user paperless webserver python manage.py shell -c '
from documents.models import DocumentJob
for job in DocumentJob.objects.exclude(status__in=["succeeded", "cancelled"]).order_by("created"):
    print(job.pk, job.kind, job.status, job.error)
'
docker compose exec --user paperless webserver python manage.py document_jobs --job JOB_UUID
```

The job report includes retained invocations and publication evidence. Treat
`failed`, `rejected`, and `attention` as unresolved decisions, not a successful
drain. Completed but uncleaned jobs may still own private files; retain them.
For an offline processor, either bring back the old eligible host and finish
under the old configuration or cancel the pending job after inspecting its
state. Do not change retained identities, remove job files, or reset statuses in
the database to force a new provider to accept old work.

If an outcome is uncertain, first inspect actual document/files, workflow effects,
and external receipts. Use the specific reconciliation path in the
[durable-operation documentation](document-operations.md#durable-execution-and-processor-api).
`document_jobs --resume` accepts only its documented recoverable states; a command
exit only dispatches work. `--acknowledge-effects` is an explicit assertion that
supported committed ingestion or enrichment follow-up effects were handled
externally. It is not a general repair for unknown parser or preparation effects.
Do not use it merely to clear an error.

## Back up a consistent rollback set

After draining or explicitly recording unresolved jobs, stop **every** process
that writes the application database, data, or media, including workstations.
In the shipped single-application Compose setup this means stopping `webserver`.
Keep PostgreSQL running to create its logical backup. From the deployment
directory, create a private backup directory and run:

```sh
umask 077
mkdir -p backup
docker compose stop webserver
docker compose exec -T db pg_dump -U paperless -d paperless -Fc > backup/database.dump
docker compose run --rm --no-deps --entrypoint /bin/sh webserver -c \
  'tar -C /usr/src/paperless -cf - data media' > backup/files.tar
cp compose.yml inference.env .env backup/
```

Check that every command succeeded and the archives can be read before changing
anything. Preserve the entire `data` volume, including `jobs` and search storage,
plus media. A database dump without private operation files cannot recover
retained work; a document export is not a replacement for this set. Keep upstream
mail/consume inputs separately if they have not yet been admitted. Save both old
and candidate image digests (or `docker image save` archives when needed), source
commits, and each worker's endpoint/model configuration and delivery-state backup.
Validate a restoration in an isolated Compose project before relying on the backup.

For that restoration, copy the saved Compose/environment files into a separate
directory, select the saved image, use an unused project name, change the web
port to avoid the live service, and create empty `consume`/`export` directories.
Disable consumer/mail admission there. From that directory, restore into its new
empty volumes (replace `BACKUP_PATH` with the saved directory):

```sh
docker compose -p paperless-restore up -d db
# Wait for PostgreSQL to report ready before restoring:
docker compose -p paperless-restore logs db
docker compose -p paperless-restore exec -T db \
  pg_restore -U paperless -d paperless --exit-on-error < BACKUP_PATH/database.dump
docker compose -p paperless-restore run --rm --no-deps --entrypoint /bin/sh webserver -c \
  'tar -C /usr/src/paperless -xf -' < BACKUP_PATH/files.tar
```

Do not point this project at the original volumes. Isolate restored processors
and external side effects until pending/uncertain jobs have been reviewed;
restoring their records does not prove that an external effect has been undone.
Use one-shot `--entrypoint python webserver manage.py document_jobs --job UUID`
commands to inspect those jobs before starting application services. Once that
review is complete, `docker compose -p paperless-restore up -d` starts the isolated
application for document/search checks.

## Switch and prove completion

With writers stopped, update the server image/descriptor selection and compatible
worker images. Keep old artifacts and settings available. Review the migration
notes before starting the candidate image; do not let an older image migrate a
newer database. Recreate server containers after server changes, and worker containers after
worker changes:

```sh
docker compose up -d --force-recreate webserver
```

For an existing deployment, review pending legacy jobs after migrations. Completed
outcomes remain retained; legacy pending/leased work can require explicit review.
Do not interpret migration success as evidence that those operations ran.
Repeat configuration diagnostics, then use a small representative PDF and an
existing document with unrelated fields/tags. Admission of an existing document
uses the authenticated API (normal requester credentials, not the processor
account):

```text
POST /api/operations/
{"request_id":"A_NEW_UUID","document_id":123,"operation":"ocr_metadata"}
```

Use `metadata` to retry enrichment alone after successful OCR; use `ocr` when only
OCR is intended. Keep the same request UUID only for retrying that same admission;
a changed document/configuration or intentionally new operation needs a fresh
UUID. A `202` response means accepted, not finished. Reprocessing existing
metadata can replace the managed title/date/summary/correspondent/language values;
review the [overwrite and preservation contract](rich-metadata-contract.md).

Verify all of the following against the source and prior document state:

- Consumption/OCR and rich metadata jobs reach success; queued search work also
  completes. The document remains readable if enrichment fails.
- Text and searchable archive correspond to the PDF; OCR provenance records the
  expected source hash, models, page count, and structured page data.
- Title/date, summary (including intentional blank), processing date,
  correspondent matching/creation, first language or `und`, and audit are correct.
- Unrelated tags/custom fields and concurrent user edits survive. A stale result
  is rejected instead of overwriting an edit.
- A distinctive extracted term is found through ordinary search after its write
  completes. Upload another PDF after idle and restart, and repeat reprocessing.

`document_index reindex`, `document_index optimize`, `document_thumbnails`, and
classifier-backed `document_retagger` can queue work and exit before it completes.
Do not restore traffic based only on their exit codes or admission counts.
`document_retagger` remains classification/rule matching; it is not the rich
metadata refresh command. Ordinary search uses its configured search provider,
independently of the OCR/enrichment selection.

## Back out without losing later edits

For a **worker rollback on the same compatible core**, stop admission, finish or
account for active claims, preserve worker delivery state, and restore the old
worker image and its configuration. Completed documents retain their text,
archives, metadata and provenance; switching workers does not revert results.
Unclaimed generic operations may be handled by any compatible authorized worker.
Drain active claims before switching, and never change the bytes of a computed
proposal while retrying delivery with the same token.

Changing an endpoint or model belongs to worker configuration. The server does
not pin it in a generic request; choose and audit worker releases accordingly.
Each returned result records the worker's actual implementation/model/prompt
identity. The input revision, requested outputs and permissions remain core
constraints regardless of which worker computed the result.

To return to the built-in parser, select
`paperless.parsers.providers.DefaultParser` and provision the server's `parser`
capability. To stop rich enrichment, clear `PAPERLESS_ENRICHMENT_PROVIDER` and
review the previous classifier settings: clearing rich enrichment can re-enable
the classification fallback. Preserve custom fields and their values. Drain or
review generic jobs before changing descriptor selection.

When upgrading from the former installed-provider deployment, first drain or
explicitly review/cancel its jobs under the old images and provider configuration.
Old jobs retain their Python/provider identities and are not converted to generic
contracts. Do not remove the old provider package until its jobs are accounted
for. Retained thumbnail policies may still require the original parser grant.

A **database downgrade is not a provider rollback**. Reverse migrations and running
an older core against the current database are not supported by this runbook.
If the old core cannot read the new schema, restore the pre-upgrade database,
data, and media set into an isolated deployment with the exact old image. First
account for documents and edits made since that backup: restoring it discards
those changes unless they are separately preserved and deliberately reimported.
Do not merge private job directories from different database snapshots. Cut over
only after verifying the restored documents, tasks, and ordinary search.

## Actionable states and compatibility rules

| State or change | Operator action |
| --- | --- |
| Waiting for processor | If intentional, retain inputs and bring the host back later. Otherwise inspect capability selection, account/grants, endpoint, and processor logs. Waiting alone consumes no attempt. |
| Processing or expired claim | Allow the lease/recovery owner to resolve it. Pure computations may retry; uncertain effectful work requires evidence. Do not start another independent API-writeback runner. |
| Checking result | Result delivery is staged. Wait for core validation/application; never equate submission with persistence. |
| Failed endpoint attempt | Fix connectivity/configuration and inspect bounded retry state. A terminal failure requires deliberate fresh admission after review. |
| Rejected/conflicted | Preserve current edits; inspect changed input, fields, provider/configuration, or permissions. Correct the cause and submit a fresh request. |
| Needs review | Inspect retained publication and effect evidence. Reconcile confirmed outcomes; do not blindly rerun workflows. |
| Cancelled | Late results cannot apply. Already committed data, external requests/costs, and uncertain effects are not undone. |

Changing core descriptor selection, custom-field definitions, source/content
revision or relevant authority can invalidate pending proposals. Never silently
relabel a pending request. Current permissions are checked again before
application; revoking requester edit/create permissions or processor capabilities
can stop previously admitted work. Concurrent creation of unrelated authorized
entities does not itself invalidate a rich proposal; retained candidate access
is still rechecked.

Generic contract identifiers describe supported wire behavior, not worker Python
classes or models. Workers reject unsupported contracts before claiming work.
Unsupported result versions, changed revisions and malformed values are rejected
by core. Retained admission dates keep retries stable; delayed processing uses
the request's original processing day. See the [wire contract](external-worker-contract.md)
for request/result shapes and delivery rules.

Release qualification must record a real replacement-and-back exercise with
completed and pending/conflicted jobs, an isolated backup restoration, endpoint
failure/recovery, and whole-stack memory results. The runbook itself and mocked
endpoint tests do not establish those deployment gates.
