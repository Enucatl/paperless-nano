# Supervised OCR and metadata processing

The supported example runs PostgreSQL, Valkey, and one Paperless container with
its existing web/coordinator services plus an explicitly enabled server
processor. Model endpoints remain external. The installed OCR adapter currently
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
docker compose exec webserver python manage.py document_processor_account \
  --user server-processor --capability parser --capability metadata \
  --capability search --capability ingestion
docker compose exec webserver python manage.py createsuperuser
```

Before provisioning, the processor reports that its account is missing and
retries after ten seconds. Provisioning is idempotent and replaces only the
capabilities of a dedicated non-login account; it refuses to repurpose a normal,
administrative, disabled, or group-bearing account. No document-edit grant or
API token is needed by the server processor. The uploader/requester still needs
normal document and entity creation permissions. See the rich metadata contract
for proposal authorization.

Create the managed custom fields through core, before admitting documents:

```sh
docker compose exec webserver python manage.py shell -c '
import json
from documents.models import CustomField
fields = {}
for role, name, kind in [("summary", "summary", "longtext"), ("processing_date", "ai_processed", "date"), ("audit", "ai_metadata", "longtext"), ("ocr", "ai_ocr_output", "longtext")]:
    field, _ = CustomField.objects.get_or_create(name=name, defaults={"data_type": kind})
    assert field.data_type == kind, f"Existing {name} has incompatible type"
    fields[role] = field.pk
print("PAPERLESS_OCR_PROVENANCE_FIELD=" + str(fields.pop("ocr")))
print("PAPERLESS_ENRICHMENT_FIELDS=" + json.dumps(fields))
'
```

Copy the two printed settings into `inference.env`, then recreate the container:

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

With neither variable set, the bundled service is disabled and logs that
provider work needs an eligible processor. A partial or invalid configuration
fails visibly through supervision. Account or permission revocation is checked
before every operation, including during a sweep. Inspect selection and grants
without consuming an attempt:

```sh
docker compose exec webserver python manage.py document_processor \
  --user server-processor --configured \
  --capability parser --capability metadata --capability search \
  --capability ingestion --diagnose
```

This reports unselected work, missing grants, and eligible pending work.
Eligibility does not certify model connectivity or retained identity validity;
core checks those when executing. `--operation NAME --provider ID` remains
available for exact placement. `--configured` uses the configured implementation
and core's retained identity/configuration checks, so operators do not copy
opaque provider identities into the normal deployment.

Tasks distinguishes waiting, processing, checking a staged result, failed, and
review-required outcomes, and displays retained operation errors. Intentional
offline waiting is harmless. Unexpected waiting calls for the diagnostic
command, processor logs, account grants, and endpoint checks. Pure endpoint
failures retain their error and permit the existing bounded three attempts;
uncertain effectful failures require reconciliation rather than blind retries.
After correcting a terminal failure, inspect the retained job and submit fresh
work as appropriate. Cancelling does not undo completed work or endpoint costs.

## Restart, storage, and memory

The lightweight supervisor runs one finite `document_processor` sweep, exits
that child, and waits ten seconds before the next sweep. Django and inference
libraries therefore leave memory after each sweep. It forwards shutdown to the
child process group. Existing durable leases and recovery handle interruption;
no second stage queue or document API writeback runner is introduced. Whole-stack
memory measurements remain a release gate; this lifecycle alone is not a
measurement.

The supported example uses one PostgreSQL database and one shared local data and
media filesystem. Competing processors use existing per-job file locks; a busy
job is skipped so other work can proceed. Additional server processes must use
the same database, settings, and private job files on a filesystem with reliable
advisory locks. Do not place independent copies of `DATA_DIR/jobs` behind several
processors. The example does not establish multi-host/NFS safety.

Stop admission and processing before a coordinated backup. Preserve PostgreSQL,
the entire data volume (including private jobs and search data), and media
together, with the exact core/provider images and configuration. Restore the
matching set. Provider configuration rollback and database downgrade are
separate operations; see the installation/swap/rollback runbook for release
compatibility and pending-job decisions.
