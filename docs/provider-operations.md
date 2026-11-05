# Install, replace, and roll back inference providers

This is the current operator runbook for the OCR and rich metadata integration
(#43–#49). Historical design notes remain in `document-operations.md`.
Use the [supervised deployment guide](processor-deployment.md) for the concrete
PostgreSQL Compose setup, account provisioning, and managed-field creation.
Keep the previous deployment available until your representative documents pass
all checks below. A passing unit test suite alone is not a release qualification.
See the [validation report](inference-release-validation.md) for verified checks
and the live qualification gates still blocking publication.

## Compatibility and required decisions

| Component | Supported combination |
| --- | --- |
| Core | The matching release commit containing #44–#49; the `3.4.1` package version alone does not identify these fork changes. |
| Provider | `paperless-inference` 0.1.0 built from that same source commit and its frozen lockfile. |
| Runtime | Python 3.14 in the matching core/provider image; PostgreSQL 18 for the shipped deployment. |
| Contracts | Parser v1 with optional validated provenance; rich metadata v1. Classification remains a separate contract. |
| OCR endpoint | Paddle full-document parsing API with `/metadata`; configured pipeline/model/layout identities must match its response. PDF inputs only. |
| Metadata endpoint | OpenAI-compatible chat completion endpoint, with the configured structured-output mode and an immutable model revision. |
| Archive tools | Ghostscript from the core image and checksum-verified veraPDF 1.30.3 from the provider image. |
| Storage | One database and shared local data/media storage with reliable advisory file locks. Multi-host/NFS operation is not established by this example. |

Save the source commit, image IDs/digests, provider lockfile, endpoint model
revisions, configuration, and managed-field IDs with each deployment. Neither a
mutable image tag nor a model alias is sufficient rollback evidence. Endpoint
credentials stay in the protected environment, never in job snapshots. Protect
backup configuration files as carefully as credentials.

Before admission, choose which host may run each processor capability. The
shipped host enables parser, metadata, search, and ingestion. Omitting a
capability intentionally leaves that work waiting for another authorized host;
there is no automatic local fallback. Remote model calls and processor placement
are separate decisions. Rich metadata requires
`PAPERLESS_ENRICHMENT_ALLOW_REMOTE=true`. For automatic remote OCR, verify
`PAPERLESS_REMOTE_OCR_MODE=always` and any overriding saved application setting.
Explicit OCR/combined operations and remote-enabled reprocessing retain their
own permission decision. Do not send a mixed-format consumption folder to the
PDF-only replacement.

## Install and verify configuration

Follow the [provider build instructions](../providers/paperless-inference/README.md)
and [deployment provisioning](processor-deployment.md#install-and-provision).
Build from one exact commit and use the same image/configuration for web,
coordinator, and server processors. Install the package before selecting its
class paths. Do not install the external listener, stage queue, or API-writeback
runner alongside this workflow. External model servers remain required.

Provision the dedicated processor account and the summary, processing-date,
audit, and OCR-provenance fields before uploading. Confirm their existing types
rather than silently replacing fields with the same names. The configured
`PAPERLESS_ENRICHMENT_FIELDS` mapping must contain distinct IDs; use longtext for
summary/audit and date for processing date. OCR provenance needs its own longtext
field. The requesting user needs document-edit access and permission to create
correspondents/tags when a proposal requests new entities; processor capability
grants do not confer document-edit authority.

Check the effective setup without processing pending work:

```sh
docker compose exec webserver python manage.py check
docker compose exec webserver python manage.py document_processor \
  --user server-processor --configured \
  --capability parser --capability metadata --capability search \
  --capability ingestion --diagnose
```

Read the startup/processor logs for import, field, permission, and endpoint
errors. Diagnostics report selection and grants; they do not prove endpoint
connectivity or actual processing. Run the completion checks below before
opening general admission.

## Stop admission and drain before a swap

Pause upload clients and user uploads, scheduled mail ingestion, scanners writing
the consumption folder, and any external submitters. Temporarily disable the
consumer and mail schedule if needed, while leaving coordinators and eligible
processors running to drain admitted jobs. Include scheduled maintenance and
index writes in the drain. Check Tasks and inspect outstanding durable jobs:

```sh
docker compose exec webserver python manage.py shell -c '
from documents.models import DocumentJob
for job in DocumentJob.objects.exclude(status__in=["succeeded", "cancelled"]).order_by("created"):
    print(job.pk, job.kind, job.status, job.error)
'
docker compose exec webserver python manage.py document_jobs --job JOB_UUID
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
commits, and endpoint/model configuration. Validate a restoration in an isolated
Compose project before relying on the backup.

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

With writers stopped, change the image/configuration and selected provider class
paths together. Keep old artifacts and settings available. Review the migration
notes before starting the candidate image; do not let an older image migrate a
newer database. Recreate all application and processor containers so no old
process keeps a different implementation loaded:

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

For a **provider configuration rollback on the same compatible core**, stop
admission, drain or account for candidate jobs, stop writers, restore the old
image/provider/endpoint settings, and recreate all relevant containers. Completed
documents retain their stored text, archives, metadata, and provenance; rollback
does not revert those results. Pending jobs retain the configuration they were
admitted with. Returning to exactly that configuration can permit an untouched
pending job to continue, but does not revive terminal rejected or cancelled work.
Resolve unknown effects before submitting fresh work, then repeat completion
checks under the old selection.

To return to the built-in parser on this core, select
`paperless.parsers.providers.DefaultParser`; to stop rich enrichment, clear
`PAPERLESS_ENRICHMENT_PROVIDER`. Restore the previous classifier settings as well:
clearing rich enrichment can re-enable the existing classification fallback.
Preserve custom fields and their values. Keep processor capabilities required by
the retained/new jobs. Do not delete the provider package while old configured
processes still use it.

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

Changing provider code identity, model revision, prompt, endpoint, effective
configuration, field mapping/type, source/content revision, or relevant authority
can invalidate pending proposals. Never silently relabel a pending request.
Current permissions are checked again before application; revoking requester
edit/create permissions or processor capabilities can stop previously admitted
work. Concurrent creation of unrelated authorized entities does not itself
invalidate a rich proposal; its retained candidate access is still rechecked.

Contract v1 rejects unknown result fields and incompatible versions. A new
writable field, changed meaning, or changed value type needs a new supported
contract and core implementation. Ship matching core/provider artifacts and run
the baseline against both sides before upgrading. Retained admission dates make
retries stable; a delayed retry keeps its original processing day rather than
stamping the retry day. No exact timestamp parity is claimed for that difference.

Release qualification must record a real replacement-and-back exercise with
completed and pending/conflicted jobs, an isolated backup restoration, endpoint
failure/recovery, and whole-stack memory results. The runbook itself and mocked
endpoint tests do not establish those deployment gates.
