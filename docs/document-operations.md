# Document operation inventory

This is the focused source inventory for [#21](https://github.com/Enucatl/paperless-ngx/issues/21),
under the architecture plan in [#20](https://github.com/Enucatl/paperless-ngx/issues/20).
It records the behavior inspected before the first document-update extraction.
The boundaries below are implementation targets, not claims that crash recovery,
stale-result rejection, or process isolation already exist.

The traces and #22 acceptance below describe the state before #23. The current
ingestion, reprocessing, and callback recovery contract is documented under
[Durable document jobs](#durable-document-jobs-23).

## Owners and dependencies

| Concern           | Current entry points and dependencies                                                                                                               | Smallest useful ownership boundary                                                                                                                                                            |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Document updates  | `documents.views.DocumentViewSet`, `serialisers.DocumentSerializer`, `bulk_edit`, `tasks.bulk_update_documents`, workflow mutations, Django signals | A document-update operation owns persistence, file reconciliation, workflow ordering, and final follow-up work; HTTP decoding and authorization remain at the API boundary.                   |
| Ingestion         | `PostDocumentView`, consumer API, folder watcher, mail; `tasks.consume_file`, consumer plugins, ORM, scratch paths                                  | An ingestion operation accepts a durable input reference and metadata; it owns publication and completion. Extend ownership after updates.                                                    |
| Classification    | `classifier.DocumentClassifier`, ORM training queries, matching functions, model file and Django cache                                              | Explicit content/candidate snapshot in, proposed assignments and model identity out; core retains permissions, rules, and application. Training and model activation are separate operations. |
| OCR and rendition | Parser registry/protocol, Django configuration, parser context managers, temporary output paths                                                     | Extend the existing parser contract with effective configuration, capabilities, durable input/output references, and provider identity. Core owns permanent files.                            |
| Search            | `search._backend`, ORM records/relations, Tantivy, Celery retry dispatch                                                                            | Map final document state to plain index records; keep persistence and retry scheduling outside the engine.                                                                                    |
| Job execution     | Celery tasks/signals, `PaperlessTask`, local paths and dataclasses                                                                                  | Durable serializable job input/result and revision contract; retain Celery as the adapter.                                                                                                    |

No database replacement or generic repository layer is needed for these boundaries.
Notifications can remain events; required state changes need explicit owners.

## Upload a scanned PDF

`views.PostDocumentView.post` authenticates the caller and checks
`documents.add_document`. `PostDocumentSerializer` validates upload metadata.
The web process normalizes the filename, writes the upload under `SCRATCH_DIR`,
and submits `ConsumableDocument` plus `DocumentMetadataOverrides` to
`tasks.consume_file`. The API returns the task ID, before consumption completes.
The task input contains a local path, not independently durable staged storage.

In the worker, `consume_file` runs preflight, ASN, collation, barcode, ASN again,
consumption workflows, then `ConsumerPlugin`. Version uploads use the reduced
preflight/consumer pipeline. Preflight checks existence, duplicate checksums
(including trash), and directories. Duplicate deletion follows the existing
`CONSUMER_DELETE_DUPLICATES` setting. Plugins share overrides and a temporary
working directory; `finally` runs each plugin's cleanup. Exceptions report
failure and propagate; stop and duplicate outcomes have explicit result types.

`consumer.ConsumerPlugin.run` copies the input to a working directory, runs the
pre-consume script, chooses a parser with the permitted remote-processing policy,
and parses text/date/page count, thumbnail, and optional archive. Archive policy
distinguishes required renditions and parser capability. It loads one classifier
for the post-consume matching hooks. Inside `transaction.atomic`, it stores the
document or version and emits `document_consumption_finished`.

`DocumentsConfig.ready` connects that event in this order: inbox tags,
correspondent, document type, tags, storage path, added workflows, full-text
index, then AI index scheduling. Version documents skip ordinary workflows;
indexing resolves their root. The consumer then publishes original, thumbnail,
and optional archive under `MEDIA_LOCK`, using unique filenames, root containment
checks, and stored-path length checks. A subsequent save reconciles filenames.
Version creation also notifies the root. Input and working files are deleted
inside the transaction after publication; the post-consume script runs later.

Tests: `test_consumer.py` covers duplicates, version creation, remote policy,
scripts, and refusal to publish outside originals; `test_api_documents.py` covers
upload validation and custom fields; `test_worker_outcomes.py` and
`test_task_signals.py` cover task outcomes/tracking. Crash points below require
additional tests when #23 is implemented.

## Change tags or custom fields

For PUT/PATCH, `DocumentViewSet.update` obtains the permitted root document and
resolves the selected/latest content version. Metadata belongs to the root;
content updates may target a version. `DocumentSerializer` validates values and
permissions, handles legacy created-date input, tag hierarchy and inbox removal,
custom-field/document-link relationships, and audit actor context. Nested
serializer persistence also applies ownership and permission changes. Removing a
document-link field removes reciprocal links; soft-deleted field instances are
then hard deleted.

Before extraction, document saves, tag relation changes, and custom-field saves
invoke `signals.handlers.update_filename_and_move_files`. The helper refreshes
the instance under `MEDIA_LOCK`, computes filenames, checks length/containment,
resolves collisions, moves original/archive files, updates path columns, clears
caches, prunes empty directories, and reconciles versions. A missing old path
with a checksum-matching expected destination is treated as an already-completed
move. On handled move/DB failures it attempts to restore moved files and logs the
failure. Metadata can remain saved even if file reconciliation fails; this is
not an all-or-nothing database/filesystem transaction. Database refresh failures
outside its recovery block propagate.

The view serializes its response before emitting `document_updated`, so workflow
changes are not necessarily reflected in that response. The event currently
runs update workflows, websocket notification, and AI scheduling. The view then
queries final state and updates/removes the full-text index, including workflow
deletion. Preserve this response timing and final-state indexing during extraction.

Bulk metadata functions in `bulk_edit.py` write with queryset/through-table
operations and enqueue `tasks.bulk_update_documents`. The task runs workflows,
notifications, a synthetic `post_save`, full-text batch indexing, and one AI
batch update. Custom-field saves can already have reconciled files before the
task. This split makes the displaced persistence/side-effect ownership explicit:
moving only the view's last few lines would leave the duplicate paths intact.

`signals.handlers.run_workflows` orders workflows and actions, refreshes between
workflows, skips deleted documents, and persists only assignment fields. Tags
and custom fields mutate relations immediately. Assignment code uses a freshly
fetched document for tags because the file signal's refresh would discard other
pending fields. Email, webhook, password removal, and trash actions have their
own ordering; trash is deferred until the workflow's other actions finish.
Scheduled workflows are another caller and send websocket updates directly.

The migration inventory includes API update/version-label/version-replacement
paths, bulk/background completion, workflow assignment/removal, root notification
after version consumption, custom-field select maintenance, and the importer's
deliberate signal disconnection. Ingestion and OCR publication remain separate
operations initially. Admin/management and direct ORM mutation paths must be
checked before removing any filesystem receiver globally.

Focused parity coverage to preserve or extend:

- `test_api_permissions.py`: owner-aware access, owner-only permission changes,
  omitted grants on PATCH, user/group grants, and bulk permissions.
- `test_api_documents.py`: final workflow state indexing, inbox removal,
  custom-field validation, and response shape; tag hierarchy tests in the
  serializer/API and bulk suites.
- `test_api_document_versions.py`: root permissions, selected/latest content,
  unrelated version rejection, and root reindexing.
- `test_workflows.py`: assignment plus tags/storage path, assignment then email,
  concurrent tags, trash ordering, scheduled actions, and AI assignment.
- `test_file_handling.py`: root/version storage paths, collisions, missing
  permissions, checksum recovery after a stale save, custom-field templates,
  containment, and database refresh failure.
- `test_tasks.py` and `test_bulk_edit.py`: bulk completion, batch indexing and
  AI dispatch, no-change behavior, and deleted-document handling.

For #22, add narrow tests that assert exactly one execution sequence for a
combined metadata/tag/custom-field update, workflow final-state indexing, and a
file failure. Do not broaden extraction to durable OCR jobs in the same change.

## Implemented update boundary (#22)

`documents.updates` now owns metadata application and ordered update completion:

1. The API authenticates, resolves the root/content version, and validates the
   existing serializer schema. `save_document_metadata` checks permission changes
   before any mutation, applies date/tag/inbox rules, writes only supplied scalar
   fields, replaces custom-field values, and maintains reciprocal links and audit
   actor context. Filename columns are excluded from metadata writes. Missing
   custom-field values retain their existing HTTP 400 response shape, but are
   rejected before scalar writes; previously nested validation could return 400
   after saving the title.
2. `document_metadata_update` defers file signals for that document while related
   writes finish, then explicitly invokes the existing locked file reconciler.
   Workflow assignment/removal and AI assignment use this same scope. AI tags
   remain additive; workflow actions keep their existing order and per-workflow
   save boundary. Pending scalar assignments no longer need a separate tag instance.
3. The API captures its response after metadata/file reconciliation and before
   workflows. `document_update` reloads effective version content, runs updated
   workflows explicitly, emits the notification event, then indexes the final
   state (or removes a trashed document). The event no longer executes workflows.
4. Bulk edits keep their existing synchronous queryset writes and asynchronous
   completion. Completion uses the same operation, explicitly requests the file
   reconciliation those queryset writes bypass, and retains batched full-text/AI
   indexing. Version-label/deletion and version-consumption completion also enter
   the operation. Version deletion now indexes the root after workflows, once.

The signal guard is local to the selected documents and execution context,
resets on exceptions, and permits nested metadata scopes. It does not globally
disconnect receivers or suppress other requests. The locked algorithm now lives
in `file_handling.reconcile_document_files`; legacy filesystem receivers are
thin adapters for out-of-scope ingestion/reprocessing and raw ORM writes. The
importer still disconnects its existing receivers deliberately. Notifications
and cache invalidation remain event-driven.

The follow-up caller audit established these explicit policies:

| Caller                                            | Persistence and file owner                                                                                                  | Follow-up policy                                                                                         |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| API document update                               | `save_document_metadata` and metadata scope                                                                                 | `document_update` runs updated workflows, notifications, final full-text index.                          |
| Bulk scalar/tag/permission commands               | Existing concrete queryset operations in `bulk_edit`; no per-row serializer saves                                           | Dispatch after writes; task delegates to `complete_document_updates`, preserving batched search/AI work. |
| Bulk custom fields                                | `modify_document_custom_fields`; all additions, then removals, then one file reconciliation per affected template           | Same asynchronous completion. Reciprocal links use the same pre-replacement logic as ordinary updates.   |
| Workflow actions and AI results                   | Metadata scopes preserve selective scalar writes and additive tags                                                          | Existing trigger policies; AI results do not rerun their originating workflows.                          |
| Admin                                             | Separate model/related metadata scopes                                                                                      | Index after related fields have been saved; no updated workflows.                                        |
| Notes API                                         | Metadata scope around modified timestamp save                                                                               | Existing immediate index update; no updated workflows/notifications.                                     |
| Renamer                                           | Explicit locked file reconciliation and suggestion invalidation                                                             | No synthetic `post_save`, workflows, or indexing.                                                        |
| Retagger                                          | Shared matching helpers enter metadata scopes only when saving an assignment; suggest/no-match paths do not reconcile files | No new workflows/indexing. Classification providers remain #24.                                          |
| Custom-field select maintenance                   | `update_select_custom_field_documents` clears invalid values and reconciles templates referencing custom fields             | Existing task name and dispatch preserved; no updated workflows.                                         |
| Storage-path edit/deletion and tag-parent changes | Save/change relations first, then dispatch affected document IDs                                                            | Existing bulk completion; template edits can no longer dispatch before persistence.                      |

Three ordering corrections accompany consolidation: admin search records now
include final related fields; storage-path dispatch observes the saved template;
and bulk document-link replacement removes old reciprocal links before writing
new values. Bulk custom-field changes no longer move files through intermediate
combinations of field values. Queryset scalar/tag/permission edits retain their
existing audit/authorization adapters and synchronous write semantics.

There is no new transaction spanning database and filesystem writes and no move
of required persistence into post-commit callbacks. File locks, path validation,
collision handling, checksum recovery, and handled-move-failure behavior remain
in the existing reconciler. A handled move failure leaves metadata saved and the
old file intact; an unhandled persistence/workflow failure stops subsequent
notifications and indexing. This extraction does not provide durable retries or
stale-result protection: those belong to #23, alongside ingestion/reprocessing
ownership. Direct bulk writes also retain their existing dispatch failure window.

`test_document_updates.py` characterizes response timing, final search state,
workflow ordering, combined metadata/file changes, permission rejection, scoped
file handling, workflow failure, malformed custom fields, and fresh version
content matching. Existing suites cover versions, nested tags, custom-field
relationships, file recovery, bulk behavior, and import boundaries. No RAM
reduction is claimed for this extraction.

### Bounded #22 acceptance

The update caller audit is complete for API metadata and notes, versions, bulk
metadata/completion, workflow actions, AI results, admin, renaming, retagging,
select-field maintenance, storage-path changes, and tag-parent propagation.
The shared update operation owns updated-workflow ordering and completion;
metadata scopes own reconciliation for callers whose established policy does
not run updated workflows. The file algorithm has one implementation, with
legacy receivers serving only as compatibility adapters. Reciprocal-link removal
also enters the target's metadata scope and retains the template guard.

Ingestion, reprocessing, importer restoration, and arbitrary raw ORM writes keep
the compatibility receivers until their own operation boundaries are extracted.
This does not imply durable dispatch, a document revision check, or end-to-end
crash recovery. Those are #23 acceptance requirements, not unfinished #22 work.
The next implementation is the shared durable input/result and publication
contract in #23; classification and OCR providers follow that contract.

### Initial extraction verification (2026-11-03)

The final selection passed **896 tests**, plus Ruff lint and formatting checks
for all changed Python files. Tests ran as an unprivileged user in disposable
`docker run --rm --userns=host` containers using
`ghcr.io/astral-sh/uv:0.9.30-python3.14-trixie-slim`, Python 3.14.2, Django 5.2.17,
SQLite, and Ghostscript 10.05.1. Dependencies came from
`uv sync --frozen --no-dev --group testing --group lint`, with `UV_LINK_MODE=copy`
to keep NLTK corpus files independent of the uv cache. Source was copied from a
read-only mount; dependencies and artifacts stayed in the disposable container
and temporary volume. The volume and temporary image were removed afterward.

The selection used `pytest -o addopts="" -n 4 --dist=loadscope --randomly-seed=1`
with these files under `src/documents/tests/`: `test_document_updates.py`,
`test_file_handling.py`, `test_workflows.py`, `test_api_documents.py`,
`test_api_permissions.py`, `test_api_custom_fields.py`,
`test_api_document_versions.py`, `test_api_bulk_edit.py`, `test_bulk_edit.py`,
`test_tasks.py`, `test_api_filter_by_custom_fields.py`,
`test_merge_documents_as_versions.py`, `test_consumer.py`,
`test_consumer_archive.py`, and `test_consumer_api.py`. It also included
`src/paperless/tests/test_import_boundaries.py`,
`src/paperless/tests/parsers/test_ocr_imports.py`,
`src/paperless_ai/tests/test_lazy_imports.py`, and
`src/paperless_ai/tests/test_ai_indexing.py`.

The full repository suite and PostgreSQL/MariaDB were not run. No deployment or
RAM comparison was performed. Existing PDF form-copy/static-directory warnings
remain; this work does not change those paths.

### Caller consolidation verification (2026-11-03)

The follow-up passed **1,038 tests** in the same disposable Docker environment.
The selection above additionally included `test_admin.py`, `test_management.py`,
`test_management_retagger.py`, `test_matchables.py`, and the new
`test_bulk_document_updates.py`, `test_document_maintenance_updates.py`, and
`test_storage_path_updates.py`, all under `src/documents/tests/`.

Focused additions cover one final filename move for multi-field bulk edits,
asynchronous workflow completion, reciprocal-link replacement/removal,
add-before-remove ordering across linked documents, admin final-state indexing,
notes/renamer trigger policies, retagger suggest/no-match safety, and storage-path
save-before-dispatch ordering. All configured changed-file lint hooks passed,
including documentation formatting, spelling, and Ruff. Temporary test volumes,
containers, and the downloaded test image were removed.

Full-suite, PostgreSQL/MariaDB, deployment, and RAM testing remain unperformed.
The existing PDF form-copy/static-directory warnings remain. The bounded #22
caller consolidation is implemented and verified; #23 remains the next task.

## Request suggestions with an empty cache

`DocumentViewSet.suggestions` checks owner-aware change permission, checks the
suggestion cache, then loads the classifier on a miss in the web process. It
extracts candidate dates and calls `matching.match_correspondents`, `match_tags`,
`match_document_types`, and `match_storage_paths` with the request user. Matching
filters permitted candidate objects and combines rules with model inference.
The response contains candidate IDs and formatted dates, without applying them.
The ordinary endpoint remains classifier-based even when AI is enabled.

`caching.py` stores suggestions with classifier format/hash and refreshes cache
lifetime on hits. Missing/disabled models permit rule-based behavior; corrupt
or incompatible models are logged and removed by `load_classifier`, while IO
failures fall back without a model. Model integrity checking must survive a
provider extraction. Recycling workers cannot unload web-process imports.

Tests: suggestion hit/miss/empty/date/AI-enabled cases in
`test_api_documents.py`, model integrity cases in `test_classifier.py`, and
matching/permission suites. #24 needs provider failure, model-change, forbidden
candidate, and stale-result application tests before changing execution policy.

## Reprocess while a user edits

`bulk_edit.reprocess` resolves a root to its latest version and queues
`tasks.update_document_content_maybe_archive_file`. The worker reads the document,
selects the parser with remote policy, parses and creates a thumbnail, then enters
an atomic DB block. It updates content and optional archive metadata directly,
records audit changes, refreshes barcodes, and moves archive/thumbnail under
`MEDIA_LOCK`. It refreshes the document, updates full-text/AI indexes, and clears
document/root caches. It does not run ordinary document-update workflows.

The initial read predates parsing; there is no input-revision comparison before
applying the result. Selected-column updates preserve unrelated fields, but a
concurrent content edit can be overwritten and naming can use stale metadata.
Exceptions are logged and swallowed. An earlier successful file move is not
undone by DB rollback. These are existing limitations for #23/#25, not guarantees
to encode into a new provider contract.

Tests: `test_tasks.py` covers archive/no-archive behavior, remote policy, and
version/root cache and AI updates. Add revision-race tests around parse completion,
deletion during parsing, and failure between archive and thumbnail publication
when defining the durable result-application operation.

## Train during ingestion

`tasks.train_classifier` skips disabled/no-auto-object configurations; otherwise
it loads or constructs a `DocumentClassifier`, gathers non-inbox training rows,
fits, and saves only when training data changed. Labels and content are read in
separate passes keyed by document IDs. Ingestion independently loads a model
snapshot after parsing and reuses it across its matching hooks.

`classifier._SignedFileWriter` writes a signed `.part` file and renames it into
place only after success. Readers read and verify one byte snapshot before
unpickling. This prevents readers from consuming a partially published model,
but does not establish a database training snapshot or serialize two trainers
writing the same `.part` name. Existing tests cover integrity, failed save
cleanup, unchanged-data training, content ordering, and deletion during training.
#24 needs training/activation identity and overlapping training/ingestion tests;
no model or sklearn replacement belongs in #22.

## Worker stops after writing output

Parser outputs are temporary and owned by parser/task context managers. There
is no durable publication manifest connecting them to retry lifetime. Ingestion
can leave published files after transaction rollback; it can also remove input
before the transaction's commit is durable. Workflow external effects and
full-text index writes can precede that commit. Reprocessing can replace one
output before failing to replace the next. `PaperlessTask` tracks Celery lifecycle
and results but is not a publication journal. Search lock exhaustion schedules
Celery retries in the engine; follow-up dispatch is not a durable outbox.

Existing checksum-aware rename recovery and sanity checking are useful local
protections, not end-to-end crash recovery. #23 should define durable staged input
and output ownership, document revision, effective configuration/provider identity,
validated publication, cleanup after durable success, and idempotent result
application. Test termination before/after output publication, commit, follow-up
dispatch, and cleanup; repeated delivery must not duplicate creation or irreversible
workflow effects. Retain inputs needed for recovery and explicitly reject stale
results. Keep execution on Celery until those contracts are demonstrated.

## Later memory measurements

The user reports about **300 MB idle**, for one home user ingesting roughly two
documents per week and idle about 99.9% of the time. Its measurement scope still
needs verification. The historical table in [memory-analysis.md](memory-analysis.md)
is separate evidence and is not a comparable new baseline. About **2 GB active**
and a proposed **3 GB peak cap** are provisional goals, not enforced limits.

Before #27, record revision/image, configuration, database choice, worker settings,
representative documents, classifier data, host/kernel, and measurement units.
Include web/server, watcher, scheduler, worker parent/children, database, cache,
and every other local OCR/AI service. Record external services separately.

Use the same deployment and sampling method before and after replacements:
measure cgroup memory for each container/service and aggregate deployment total;
use process PSS for attribution without summing shared RSS. Record cache-accounting
policy and host background load. Sample startup, settled idle, cold suggestions,
scanned upload, reprocess, and training during ingestion. Continue sampling until
the deployment returns to idle; report peak, operation latency, cold-start delay,
and elapsed return-to-idle time as well as idle memory. Repeat runs and distinguish
cold from warm caches. Verify new uploads and scheduled work after a long idle.

Source extraction does not require a RAM reduction. Runtime comparisons and
crash/stale-result tests belong to the later component/execution changes. Run
verification in disposable `docker run --rm --userns=host` containers, with
dependencies/artifacts inside them and temporary volumes removed. Retain the
existing `paperless/tests/test_import_boundaries.py`, OCR import, and AI lazy-import
checks throughout extraction.

## Durable document jobs (#23)

`documents.jobs` and `documents.ingestion_jobs` own concrete reprocessing and
ingestion jobs. `documents.job_callbacks` journals their existing deletion, ASN
restoration, and mail-action callbacks, plus ingestion's deferred AI and webhook
actions. Django, Celery, the plugin pipeline, and the existing parsers remain in
place. These are operation boundaries; no generic
repository or replacement provider framework is introduced.

### Shared contract and reprocessing

`documents.jobs` now owns reprocessing admission, execution, publication, and
completion. Both reprocess API routes carry the requesting actor through
`bulk_edit.reprocess`; the archiver and legacy task calls enter the same operation.
Celery remains the execution adapter. The existing parser implementations,
remote-processing policy, audit entries, barcode refresh, and version selection
remain in use. Reprocessing still does not run consumption or update workflows.

`DocumentJob` is the durable journal, separate from deletable `PaperlessTask`
telemetry. Admission copies the original into `DATA_DIR/jobs/<uuid>` before
queueing and records JSON input identity, document revision, actor, requested
remote policy, and timeout. The payload contains a UUID and document ID, with no
scratch paths or ORM objects. Workers can use a different scratch directory;
they must share the database, data directory, and media directory. Job directories
are private to the service account (0700); ordinary document API permissions do
not grant direct access to staging. Storage operators/backups must treat these
files and the journal's content snapshots as document data. Deferred webhook
inputs can also contain rendered attachment bytes and authentication headers;
the database and its backups require the same restricted access. The document
exporter exports completed document state, not operational jobs or staging; full recovery
backups must include the database, data directory, and media directory together.

The worker records built-in effective OCR/barcode/remote configuration (without
the remote API key), parser execution settings, Tika endpoints, email layout,
the archive decision, and parser class/name/version. It copies and validates
outputs before the parser's temporary context exits. PDF
and WebP outputs must have matching checksums, MIME types, and readable structure;
required PDF output cannot be omitted. Configuration is checked again after
processing. This is execution provenance for existing parsers, not the immutable,
framework-independent provider configuration contract planned in #25. Third-party
parser-specific settings and engine versions still need that extraction.

The recorded revision includes content, file identity, modified timestamp, and
version linkage. Immediately before publication the operation rechecks the latest
version, the actor's current active/global/object permissions, and the revision.
A conditional UPDATE inside the applying transaction prevents a concurrent
queryset edit from slipping past the comparison, including on SQLite. The root
row is locked against concurrent version admission. Media locking during
publication is nonblocking inside the transaction, so contention rolls back
for retry instead of inverting the ingestion lock order. Trusted
CLI callers explicitly have no actor. A deleted actor never becomes a trusted
caller. Changed or deleted documents reject results without replacing their files.

An archive is published to a new collision-checked filename, preserving its
storage directory/template and the prior archive until commit. Its destination
is journaled before publication; copies use a destination-side temporary file,
fsync, and atomic replacement. A crash after publication but before commit leaves
the old document and archive usable. Recovery reuses the verified new generation.
The document and job's committed state, result revision, and required follow-up
list are saved in one database transaction. A private `content_job` marker lets
thumbnail completion survive unrelated metadata edits while preventing an older
job from replacing a newer job's thumbnail.

Thumbnail publication, full-text and optional AI indexing, and cache invalidation
are repeatable completion work. Full-text indexing uses the batch writer so lock
failure cannot masquerade as a successful queued retry. Deferred AI migration
leaves completion pending. Duplicate delivery resumes completion without parsing
or applying the content again. Staging is deleted only after durable success;
interrupted cleanup can be retried independently. Old archives are removed only
when no document, including trash, references the name and the checksum still
matches. If the live original disappeared or changed unexpectedly, cleanup retains
the snapshot and reports the problem. New archive generations may have a collision
suffix until a later normal filename reconciliation. Reprocessing now always
advances the modified timestamp so content-only results invalidate metadata caches.

A per-job file lock serializes execution. Lock files live outside staging and
remain after cleanup so removing a live lock cannot create two lock owners.
`staging`, `queued`, `running`, `prepared`, `committed`, `succeeded`, `failed`,
`rejected`, and `attention` are durable states. Result progress records parsing,
publication, completion, and finished work independently of Celery lifecycle
callbacks. Reprocessing uses the existing worker timeout and at most three
attempts; a late result cannot be applied. A returned parser error queues a
retry (or fails the third attempt); a killed worker remains running until its
recorded timeout permits recovery. A busy duplicate is ignored by Celery rather
than reporting premature success. Retry retains the admitted revision and
input and records the new execution configuration. Prepared publication and
committed completion retry without rerunning the parser. Unsupported formats and
execution failures now report failure to the task/CLI
caller instead of being swallowed as apparent success. Terminal failures and
rejections retain their staging for inspection; there is no automatic destructive
expiry policy. The existing scheduler invokes recovery every five minutes,
redispatching aged unfinished jobs. Running jobs are redispatched only after their
recorded timeout; a still-live executor retains its lock. Broker errors leave
queued work durable. Completion and cleanup errors remain visible and retryable.

### Ingestion admission, publication, and completion

`DurableConsumeTask` stages the existing `consume_file.apply_async` and Celery
signature callers before publication: ordinary and version uploads, folder
watcher/API admission, mail attachments/messages, barcode children, and PDF
merge/split/edit operations. Direct and older task calls enter the same admission
when executed. Messages carry the job UUID; serialized metadata, source identity,
version revision, actor, and callback options live in the job journal. If admission
runs inside a database transaction, dispatch waits for commit and the durable
queued row remains available to the recovery sweep if broker publication fails.
The adapter retains Django's commit-aware task methods. The lean worker permits
this specific built-in task base and restores its real completion hook in the
child; its general rejection of custom task bases and hooks remains in force.

The immutable input copy is separate from the plugin's working file. The
admitted `source_path` preserves folder/workflow identity; `original_path` keeps
its existing meaning for barcode-split children. Uploads and user PDF operations
record the authenticated actor separately from the resulting document owner.
Version admission and application recheck the root revision/latest version and
current change permission; new-document application rechecks add permission.
Trusted watcher/mail/CLI paths retain their established actor policy.

Ingestion records `running` before invoking the first plugin. It makes one
automatic pipeline attempt because consumption/added workflows and pre-consume
scripts can have irreversible effects. A failed or interrupted attempt retains
its input and becomes `attention`; another delivery cannot recreate the document
or repeat those workflows. The original workflow order and assignment policies
remain. This is deliberately conservative recovery, not a claim of exactly-once
external effects.

Parser outputs are copied into the job directory and validated before permanent
publication. The existing media lock, containment checks, filename limits, and
collision handling remain. Each permanent file write and subsequent ingestion
file relocation is recorded in `publication.json` before it happens. This fsynced
manifest survives database rollback. Publication copies are atomic and never
consume their recoverable staged source. Existing destinations cannot be silently
overwritten. An interrupted transaction may leave an unreferenced permanent file;
its manifest and staged copy remain available for explicit reconciliation.

The document transaction also records the result ID, published file checksums,
and required completion work. Full-text/AI indexing is deferred from the old
ingestion signals to this recorded completion. A duplicate delivery after commit
resumes completion, not the plugin pipeline. The post-consume script has durable
pending/started/done markers: an interruption after starting becomes `attention`
because its external outcome is unknown. Safe indexing/cache work can be repeated.
Deferred workflow AI and webhook intents are recorded with stable callback IDs
before the document transaction commits. Completion executes their existing
implementations through durable callback receipts before indexing final state.
Completed actions are skipped on redelivery. AI timeouts can retry only while the
built-in implementation guarantees no metadata mutation has occurred; an exhausted
retry or uncertain action requires operator reconciliation. Webhook failures are
not blindly repeated because the remote endpoint may already have acted.
AI application rechecks the original actor and the document's persisted fields,
relations, and versions after obtaining suggestions; concurrent edits reject the
result before metadata mutation.
The metadata-update context remains only a signal-ordering scope; it is not the
publication journal or a filesystem transaction.

Cleanup follows durable success and any required callback completion. It first
checks that the committed original/archive still match their database checksums.
It removes the external input only if its original file identity and checksum
still match, protecting a newer file placed at the same path. A plugin stop that
leaves its working input untouched also leaves the external input untouched.
Interrupted cleanup retries independently; a missing or changed committed file
retains recovery material and reports an error.

Barcode children are individually admitted before their parent removes its working
input, with parent job IDs recorded. Collation uses a shared locked directory at
`DATA_DIR/jobs/collation` for durable jobs. Its first half survives worker scratch
cleanup; the merged output is fsynced before either half is released. Existing
merged filenames are preserved through collision suffixes. Failed collation keeps
both halves; expired first halves are renamed to `expired-<uuid>.pdf` for operator
recovery, with no automatic destructive expiry policy.

### Concrete callback ownership

The existing mail chords and PDF-operation completion callbacks keep their Celery
shape. Admission freezes callback IDs and serializes their options. Ingestion
records delivery started/done separately from each callback's own execution
journal. The concrete mail-action, source-deletion, and ASN-restoration tasks use
`run_callback`: a completed duplicate returns its saved result, a busy duplicate
is ignored, and an interrupted or failed effect requires attention. Missing
callback completion prevents input cleanup and is surfaced after the recorded
timeout. Repeated ingestion failure cannot redispatch its native error callback.

Deletion callbacks snapshot all persisted document values, tags, custom fields,
notes, barcodes, and versions. Before deleting they reject changed state and
recheck the caller's current delete permission. ASN restoration is conditional
and never overwrites a newer non-null ASN. Its error signatures are immutable so
Celery's prepended failed-task ID cannot replace the backup argument. Mail errors
remain visible in the durable callback and existing processed-mail records.

These records make an ambiguous effect visible and retain recovery inputs; they
do not silently retry an email, mail move/delete, script, or other non-idempotent
effect whose outcome cannot be established.

### Inspection and recovery

The existing scheduler runs the recovery sweep every five minutes. It republishes
safe queued/prepared/committed work and incomplete cleanup using stable IDs.
Expired running reprocessing jobs can retry within their attempt limit. Interrupted
admission, ingestion, and callbacks become `attention`; they are not repeatedly
executed. The job lock prevents a still-live executor from being overtaken.

Use the management command from the application's configured environment:

```sh
python src/manage.py document_jobs --job <uuid>
python src/manage.py document_jobs --job <uuid> --resume
python src/manage.py document_jobs --job <uuid> --resume --acknowledge-effects
```

Inspection reports status, error, attempts, timeout, progress, staging location,
publication manifest, result payload, effect markers, and remaining follow-up work.
`--resume` dispatches permitted queued/prepared/committed/succeeded recovery work;
it refuses a live lock, failed/rejected work, uncertain callbacks, or uncommitted
ingestion that could repeat external effects. Inspection and refused recovery do
not remove staged inputs or published files.

`--acknowledge-effects` is only for a committed ingestion in `attention`, after an
operator has reconciled its script, workflow follow-up, and callback effects. It
marks known uncertain workflow receipts as explicitly acknowledged and skips
already-handled effects. Workflow intents that have not started still execute;
safe completion then resumes. It is not permission to restart the ingestion
pipeline. Uncommitted uncertain ingestion needs inspection
of its input/publication evidence and reconciliation of external effects and
orphan files before a new submission. Callback jobs themselves cannot be replayed
through this command. Retained expired collation halves also require explicit
operator inspection/cleanup.

### Scope and limits

The #23 implementation covers durable ingestion, reprocessing, their file
publication, and their concrete completion callbacks. It preserves the database
choices, parser/plugin implementations, API result shapes, and workflow order.
Repeated delivery does not imply retrying ambiguous work: an explicit attention
outcome is part of the recovery contract.

| #23 requirement                                  | Concrete implementation and acceptance evidence                                                                                                                            |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Durable identity and serializable inputs/results | UUID `DocumentJob` rows, staged input copies, JSON metadata and tagged callback values; transport and scratch-loss tests.                                                  |
| Revision, configuration, and provider identity   | Admitted document/version snapshots, effective built-in configuration, parser identity; stale-edit, changed-version, and configuration checks.                             |
| Status, timeout, retry, and repeated delivery    | Explicit job states, per-job locks, bounded reprocess/AI retries, callback receipts, and attention for uncertain effects; duplicate, timeout, and retry tests.             |
| Permission and stale-result checks               | Fresh actor/global/object checks and conditional writes before application; revoked/deleted-actor, concurrent-edit, and destructive-callback tests.                        |
| Durable staging and ownership                    | Private shared job storage, copied parser outputs, durable collation halves, publication manifests, and checked source cleanup; scratch-loss and source-replacement tests. |
| Validation before publication                    | Checksums, MIME/structural validation, required archive checks, path containment, collision handling, and atomic copies; invalid-output and file-safety tests.             |
| Retain recoverable inputs until success          | Commit and required completion precede cleanup; missing permanent files, failed callbacks, and ambiguous effects retain staging; cleanup interruption tests.               |
| Record required follow-up work                   | Commit records indexing/cache/script work; workflow AI/webhook intents and Celery callback IDs have durable receipts; dispatch failure and callback redelivery tests.      |
| Exercise crash points                            | Publication before commit, commit before completion, callback dispatch/acknowledgment, effect interruption, and cleanup are covered by focused job tests.                  |

Recovery does not guess that an unreferenced published file is safe to delete.
Input/result snapshots, rejected jobs, ambiguous effects, and expired collation
halves may therefore consume disk until reviewed. Shared-storage loss is outside
this single-host process-crash contract. Immutable provider configuration and
provider-specific identity remain #24/#25 work; general metadata/workflow dispatch
outside these jobs and AI index concurrency during embedding are not converted
into a universal durable outbox. No idle-memory reduction is claimed here.

### Job verification (2026-11-03)

The final expanded selection ran **1,634 tests**: **1,633 passed**. The one
failure was a new webhook assertion expecting an empty substitution where its
single-brace fixture was literal text. Correcting that assertion and rerunning
the affected test passed, so all **1,634 selected cases passed across the broad
and focused runs**. The preceding expanded run exposed an unnecessary missing
mail-rule lookup and test assumptions about immutable callbacks, deferred webhook
execution, and database IDs; these were corrected before the final run.
Earlier focused runs also covered all 14 collation cases, 58 worker/callback
cases, and 18 deferred-workflow/recovery cases. The final selection includes
these operation and worker suites; counts from overlapping runs are not added.

Tests used disposable `docker run --rm --userns=host` containers with an
unprivileged user, Python 3.14.2, Django 5.2.17, SQLite, Ghostscript 10.05.1, and
frozen uv testing/lint dependencies. Source was copied from a read-only mount;
dependencies, test artifacts, and generated databases stayed in the temporary
container/volume. The temporary verification image and volume were removed after verification.

The broader command was `pytest -o addopts='' -n 4 --dist=loadscope
--randomly-seed=1`, selecting these files under `src/documents/tests/`:
`test_document_jobs.py`, `test_ingestion_jobs.py`, `test_job_callbacks.py`,
`test_document_jobs_command.py`, `test_tasks.py`, `test_bulk_edit.py`,
`test_api_bulk_edit.py`, `test_scheduler_api.py`, `test_api_documents.py`,
`test_api_permissions.py`, `test_api_document_versions.py`,
`test_version_conditionals.py`, `test_file_handling.py`,
`test_document_updates.py`, `test_workflows.py`, `test_consumer.py`,
`test_consumer_archive.py`, `test_management.py`, `test_management_exporter.py`,
`test_management_importer.py`, `test_task_signals.py`, and
`test_worker_outcomes.py`, `test_double_sided.py`, `test_barcodes.py`,
`test_consumer_api.py`, `test_api_post_document_nfc.py`,
`test_management_consumer.py`, `test_matchables.py`, `test_management_retagger.py`,
`test_document_maintenance_updates.py`, `test_bulk_document_updates.py`,
`test_storage_path_updates.py`, `test_merge_documents_as_versions.py`,
`test_admin.py`, `test_api_custom_fields.py`, `test_ingestion_followups.py`, and
`test_api_workflows.py`. It also included `src/paperless_worker/tests/`,
`src/paperless/tests/test_worker_trace.py`,
`src/paperless/tests/test_worker_serialization.py`,
`src/paperless/tests/test_disabled_feature_workflows.py`,
`src/paperless/tests/test_import_boundaries.py`,
`src/paperless/tests/parsers/test_ocr_imports.py`,
`src/paperless_ai/tests/test_lazy_imports.py`, and
`src/paperless_ai/tests/test_ai_indexing.py`; and under `src/paperless_mail/tests/`,
`test_mail.py`, `test_mail_nfc.py`, `test_preprocessor.py`, `test_api.py`, and
`test_process_mail_accounts_overlap.py`. Live-mail service tests were excluded.

The job cases cover staged input survival, serialization, stale edits, changed
versions, deleted/revoked actors, timeout and retry limits, publication/commit
interruptions, completion and cleanup failures, invalid output, lost broker
publication, media-lock contention, index ordering, and concurrent/legacy delivery.
Ingestion cases exercise publication before commit, commit before completion,
duplicate creation prevention, ambiguous pipeline/script/callback outcomes,
changed cleanup targets, native callback/errback redelivery, retained collation
halves, and explicit recovery-command refusal/acknowledgment.
Existing suites cover real OCR, archive policy, remote policy, version handling,
permissions, workflows, import/export, and task tracking.

`makemigrations --check --dry-run --skip-checks` found no missing migrations in
the final tree. Changed-file hooks passed, including Ruff, Prettier, and
codespell. The final assertion and verification-documentation edits also passed
the changed-file hooks.
The full suite, PostgreSQL/MariaDB, deployment, and RAM checks were not performed.
Existing PDF form-copy and missing-static-directory warnings remain.

## Replaceable classification (#24)

`documents.classification` defines a JSON-compatible version 1 request and
result. Requests contain content, a document revision, relevant metadata and
permitted `{id, name}` candidates grouped by taxonomy. Results contain the same
revision, provider/model identity and proposed IDs. The optional AI suggestions
payload retains title, dates and new names; its existing IDs must agree with the
validated assignments. Providers receive no ORM documents, users, HTTP requests
or Celery context. They do not persist assignments or choose retry policy.

`classification_operations` owns source snapshots, provider selection and
candidate permission checks. Classic inference enters it through the four
existing matching functions. The built-in adapter retains sklearn predictions,
text truncation, vectorization caching, signed cache validation, confidence
thresholds and language processing. Model predictions for deleted or unavailable
labels are discarded by that adapter, as before; a provider result outside its
supplied candidate set is rejected by the core. Rules still use full effective
content and retain their ordering relative to automatic matches.

Ingestion reuses its selected model through the existing ordered hooks. The
retagger keeps selective targets, first-match policy, overwrite and suggest
modes. The assignment handlers retain manual overrides, inbox tags, nested tags
and file reconciliation. They lock/check the target and run in a transaction;
replacement tags are removed only after inference and validation succeed.
Metadata, tag, effective-version, candidate, configuration and model changes
during inference reject the result. Nothing queues automatic inference retries:
a new invocation takes a fresh snapshot, while ingestion failures retain the
existing durable job recovery rules. Raw external database writes are not made
part of a global transaction protocol by this extraction.

AI classification uses the same contract. Core code retrieves permitted RAG
context, builds prompts and resolves returned names. The provider runs the
existing LLM/localization implementation using plain values. Core validation
checks the document, configuration and existing candidate IDs again. All AI
workflow callers, including scheduled/update workflows, recheck the revision
before applying results. Creation of missing objects, selected fields, overwrite
policy, additive tags and workflow ordering remain core policies. Background AI
callbacks keep their existing bounded retries and ingestion receipts.

### Selecting a provider and execution policy

The default `PAPERLESS_CLASSIFICATION_PROVIDER` is empty and selects the existing
classifier. A dotted no-argument factory can replace classic automatic matching.
For example, set it to
`documents.classification_providers.NameTaggingProvider` to use the included
small provider that proposes permitted auto tags whose names occur in the text.
It proposes no scalar values; core rules continue to work. This is an explicit
alternative, not a fallback or a new default. No persistence code changes when
selecting it. `CLASSIFIER_ENABLED` still controls classic automatic inference.

Factories must expose a cheap `identity` tuple (implementation version, immutable
model/configuration identifier) and `classify(request)`. A changed model or
configuration must change that identity. Factories may be constructed to check
cache freshness without doing inference; expensive model loading belongs in
execution. The built-in identity uses the signature of the fully verified model
snapshot and effective inference settings. Reading the active signature for cache
freshness never substitutes for HMAC verification before unpickling. Remote
services that silently replace weights behind an unchanged model identifier
cannot be detected locally.

Execution remains synchronous in the current web/worker/CLI callers. The contract
defines no process lifecycle, pool, timeout timer or memory cap. Providers report
timeouts with `TimeoutError`, operational failures with
`ClassificationProviderError`, and never return a partial successful result.
Classic suggestions return 503 for timeout, 502 for a declared provider failure
and 409 for a stale/invalid result. Unexpected exceptions propagate as failures.
No failed response is cached. The separate AI endpoint retains its existing
400/502/503 configuration/backend/timeout behavior and adds 409 for rejected
results. Missing, disabled, incompatible or corrupt built-in models still permit
rule matching; existing corrupt-model logging/removal is retained.

### Cache and model ownership

Successful suggestion response shapes are unchanged. Classic cache entries are
bound to document/version state, requesting user, permitted candidates/rules,
provider/model identity and inference/date configuration. Existing invalidation
continues to delete the document cache entry; only the latest variant is stored.
ETags use the same scope so edits, revocations and provider changes cannot yield
an old 304. Last-Modified remains informational; date-only conditional requests
are evaluated again because timestamps cannot represent permission/configuration
changes. AI cache keys also include effective model/configuration identity and
continue to resolve candidate permissions freshly on hits.

`classifier_training.train_classifier` owns built-in training independently of
inference. The Celery task and management command delegate to it. Training jobs
are serialized; each model writer has a unique temporary file. Activation is an
atomic signed-file replacement and only successful activation advertises cache
metadata. A stale reader cannot republish metadata for a newer model, and failed
model cleanup cannot remove a concurrently activated replacement. Successful
inference loading still works with read-only model storage. This retains format,
sklearn compatibility and signed-pickle checks.

This implements the #24 boundary without an idle-RAM claim. Django, Celery,
databases, OCR and search implementations remain in place. #25 (OCR), #26
(search) and #27 (measured residency reductions) remain separate work. The
reported 300 MB baseline, approximately 2 GB active goal and proposed 3 GB peak
cap have not been remeasured or enforced here.

### Classification verification (2026-11-03)

The broad run passed **938 tests**, with **one existing skip** for classifier
instance caching that was already disabled for memory usage. The final focused
run passed **71 tests**, covering the subsequent session-user, provider-startup
failure and revoked-superuser fixes. These selections overlap and are not added
together. Earlier failures exposed persisted date normalization and file
reconciliation updating a separate ORM instance; both are fixed and the broad
run was repeated successfully. Existing missing-static-directory warnings remain.

All tests ran in disposable `docker run --rm --userns=host` containers as an
unprivileged user, with a read-only source mount and copied working tree. The
environment used Python 3.14.2, Django 5.2.17, SQLite, Ghostscript 10.05.1 and
frozen uv testing/lint dependencies. Dependencies and generated artifacts stayed
in temporary container storage and a named Docker volume. Temporary verification
resources were removed after the checks.

The broad command was `pytest -o addopts='' -n 4 --dist=loadscope
--randomly-seed=1`, selecting these files under `src/documents/tests/`:
`test_classification.py`, `test_classifier_activation.py`, `test_classifier.py`,
`test_matchables.py`, `test_permission_filtering_security.py`,
`test_api_documents.py`, `test_api_permissions.py`, `test_api_document_versions.py`,
`test_version_conditionals.py`, `test_consumer.py`, `test_ingestion_jobs.py`,
`test_ingestion_followups.py`, `test_job_callbacks.py`, `test_tasks.py`,
`test_workflows.py`, `test_management_retagger.py`,
`test_document_maintenance_updates.py`, `test_document_updates.py` and
`test_management.py`. It also selected `src/paperless_worker/tests/`;
`test_classification_provider.py`, `test_ai_classifier.py`, `test_matching.py`
and `test_lazy_imports.py` under `src/paperless_ai/tests/`; and
`test_import_boundaries.py`, `test_disabled_feature_workflows.py`,
`test_worker_trace.py` and `test_worker_serialization.py` under
`src/paperless/tests/`.

The final focused run used the same options with
`-k 'classification or suggestions or ai_classifier'`, selecting
`test_classification.py`, `test_api_documents.py` and
`test_permission_filtering_security.py` under `src/documents/tests/`, plus
`test_classification_provider.py` and `test_ai_classifier.py` under
`src/paperless_ai/tests/`. A preceding 16-case run also passed the classic
contract/persistence/cache cases after the session-user fix.

`makemigrations --check --dry-run --skip-checks` found no missing migrations.
Changed-file hooks passed, including Ruff, Prettier and codespell. The existing
uncommitted workflow and memory-report changes were preserved separately.

The full suite, PostgreSQL/MariaDB, deployment and RAM checks were not performed.
The #23 recovery limitation remains: ambiguous external effects retain their
inputs and require explicit operator reconciliation.

## Explicit parser replacement (#28)

`PAPERLESS_PARSER` selects an installed Python class, defaulting to
`paperless.parsers.providers.DefaultParser`. The default dispatches to the existing
built-ins and `paperless_ngx.parsers` entry-point plugins, preserving MIME scoring,
archive decisions, OCR overrides, mail layout and plugin precedence. Explicit
replacements need no entry-point registration, score or capability declarations.
They own computation and format decisions; core owns admission, permissions,
source identity, durable jobs, validation and publication.

The class has a no-argument constructor, a stable `identity`, a boolean
`uses_remote_service`, `configuration_snapshot()` and `process(request, root)`.
The snapshot must be JSON-compatible and contain implementation-owned settings
without credentials. Core records it and rejects changes before retry or publication.
Credentials are read by the implementation at execution time. Subclassing
`ParserProvider` supplies an empty snapshot for implementations with no settings.
Bump the identity when changing processing semantics. Installed code is trusted,
not sandboxed; plugins must accurately declare remote processing and honor the
request's permission. Core independently rejects a remote replacement when that
permission is absent. Selection never silently falls back to another OCR service.

Requests in `paperless.parsers.contract.ParserRequest` include contract version,
revision, immutable source reference, MIME type, original filename, private output
directory, operation intent, archive preference (`auto`, `always`, `never`),
preferred PDF output type, remote permission, and the implementation's own `settings`
snapshot. They do not expose Django models or permanent file paths. Results contain
version, revision, provider identity, text (possibly empty), positive page count or
null, ISO date/datetime or null, optional archive and thumbnail references, and
`archive_type` (`searchable_pdf`, `pdfa`, or null). File references contain a relative
path, SHA-256 digest and byte size. Use `resolve_reference` and `file_reference` to
read the input and describe output files inside the supplied output directory.

### Installation and minimal replacement

Install the package in the same Python environment as the web process and workers
(for Docker, include it in a derived image), then set its dotted class path:

```python
# example/parsers.py
from paperless.parsers.providers import PlainTextProvider

class CustomParser(PlainTextProvider):
    """A minimal text-only replacement using the explicit contract."""
    identity = "example.text:1"
```

```ini
PAPERLESS_PARSER=example.parsers.CustomParser
```

`PlainTextProvider.process` is a complete, framework-free reference implementation.
A replacement should raise an error for formats it cannot process. Upload admission
accepts detected MIME types and folder consumption uses system-known extensions;
format acceptance belongs to the selected replacement, without loading the default
registry. Restart services after changing the implementation. Finish or resolve
pending jobs first: prepared results from a different provider/configuration are
rejected rather than published under a new selection.

### Advisory archives and separate auxiliary operations

Archive preferences are advisory for replacements: omitted archives, supported
format differences and archives returned under `never` are accepted. The default
implementation preserves its existing `auto`/`always`/`never` behavior, including
Office display PDFs taking precedence over `never`, OCR user-argument output-type
overrides and mail output settings. A replacement may omit a display PDF, accepting
that a browser may be unable to preview its original format. Reprocessing without
a returned archive preserves an existing archive.

A returned thumbnail is optional. If omitted, core lazily selects a local legacy
thumbnail implementation and calls only its thumbnail operation. No suitable local
implementation or no valid thumbnail fails the job. A valid returned thumbnail
avoids loading default engines. Thumbnail regeneration always uses the separate
local thumbnail path and never calls replacement `process` or reruns OCR. Metadata
inspection likewise uses local legacy `extract_metadata`, returning the existing
empty result when unsupported. These auxiliary calls may load their relevant
legacy parsers; ordinary replacement execution and recovery do not.

`PAPERLESS_LOCAL_OCR_ENABLED` is removed. To preserve the default parser's former
OCR-off behavior, use `PAPERLESS_OCR_MODE=off` and
`PAPERLESS_ARCHIVE_FILE_GENERATION=never` (also update any saved application
preferences, which retain precedence). To remove default engine execution, select
a replacement that owns that strategy. Remote-processing authorization remains
separate. The removed switch no longer locks OCR preferences in the configuration
API; AI deployment restrictions are unchanged.

### File ownership, failures and execution

The core copies processing input into a unique attempt directory under the
private job directory, keeping the admitted input intact even if a provider
modifies its own copy. It records the request before execution and validates
results against a separate snapshot. It rejects changed input, changed provider
identity, malformed text/page/date values, escaped paths, symlinks, input aliases,
missing or invalid PDF/WebP files. Output bytes and directories
are synced before recording successful results. PDF validation establishes a
readable, nonempty PDF; full standards certification of PDF/A remains the
provider's responsibility.

The core retains database writes, permissions, document/version revision checks,
workflows, publication locks and collision handling. Configuration and selected
provider identity are checked again before applying prepared work. Reprocessing
without a new archive retains the existing archive. The original and admitted
input are never replaced by provider output. Successful durable completion owns
cleanup; failed jobs retain evidence and recovery inputs. Thumbnail regeneration
also rejects stale results and retains its private copy if the original vanishes.

Execution policy remains outside the contract. Celery's existing worker count
bounds simultaneous documents; `THREADS_PER_WORKER` bounds OCRmyPDF jobs even
when user arguments request more, and the existing OpenMP limit bounds
Tesseract threads. Worker time limits and service request timeouts remain in
place. A provider may raise `TimeoutError`; reprocessing retains its existing
three-attempt policy, while interrupted or failed ingestion retains the #23
attention/reconciliation rules. A retry rejects a changed configuration or
provider instead of silently selecting another implementation. No custom process
supervisor, new queue or memory cap is introduced. Jobs predating parser-request
journaling retain their existing completion path; jobs with an older request/configuration
snapshot are rejected after the selection migration. The recovery command's acknowledgment semantics are
unchanged; this work never blindly replays ingestion or uncertain effects.

To allow a provider to import the contract without Django, `paperless` public
framework exports resolve on access; normal Django startup explicitly initializes
Celery and checks. Startup, check registration and lean-worker compatibility are
covered by tests. This is required for the standalone contract and makes no
idle-memory claim. This change does not remeasure or enforce residency goals.

### Parser-selection verification (2026-11-04)

The #28 regression selection passed **796 cases** covering explicit and default
providers, ingestion, reprocessing/recovery, archives, thumbnails, metadata,
configuration, remote permission, consumer tasks, registry plugins and import
boundaries. A final focused run passed **39 cases**, including the default archive
policy suite and legacy mail-rule context; these overlap the larger selection.
Tests used Python 3.14.10, Django 5.2.17 and SQLite in disposable
`docker run --rm --userns=host` containers as an unprivileged user. Frozen uv
settings supplied testing/lint dependencies; `uv run --no-sync ruff format .`
and changed-file Ruff checks passed. Temporary test storage was removed.
The full suite, live OCR services and other database engines were not run.

### OCR provider verification (2026-11-03)

The broad run selected **2,411 cases**: **2,409 passed**, one preexisting
classifier-cache test was skipped, and one new thumbnail test failed because it
assumed the attempt input basename was `input.pdf`. The implementation preserves
the source basename. Updating that assertion and rerunning all five thumbnail
safety cases passed. Thus **2,410 selected cases passed across the broad and
focused runs**, with one existing skip. Earlier focused parser/framework (385)
and operation/consumer (136) selections overlap and are not added to this count.

All tests ran in disposable `docker run --rm --userns=host` containers as an
unprivileged user, with source mounted read-only and copied into temporary
container/volume storage. The environment used Python 3.14.2, Django 5.2.17,
SQLite, Ghostscript 10.05.1 and frozen uv testing/lint dependencies. The broad
command used `pytest -o addopts='' -n 4 --dist=loadscope --randomly-seed=1
--tb=short`. It excluded the live-service parser files `test_mail_parser_live.py`
and `test_tika_liva.py`. The exact selections were:

- `src/documents/tests/`: `test_document_jobs.py`, `test_ingestion_jobs.py`, `test_job_callbacks.py`, `test_document_jobs_command.py`, `test_tasks.py`, `test_bulk_edit.py`, `test_api_bulk_edit.py`, `test_scheduler_api.py`, `test_api_documents.py`, `test_api_permissions.py`, `test_api_document_versions.py`, `test_version_conditionals.py`, `test_file_handling.py`, `test_document_updates.py`, `test_workflows.py`, `test_consumer.py`, `test_consumer_archive.py`, `test_management.py`, `test_management_exporter.py`, `test_management_importer.py`, `test_task_signals.py`, `test_worker_outcomes.py`, `test_double_sided.py`, `test_barcodes.py`, `test_consumer_api.py`, `test_api_post_document_nfc.py`, `test_management_consumer.py`, `test_matchables.py`, `test_management_retagger.py`, `test_document_maintenance_updates.py`, `test_bulk_document_updates.py`, `test_storage_path_updates.py`, `test_merge_documents_as_versions.py`, `test_admin.py`, `test_api_custom_fields.py`, `test_ingestion_followups.py`, `test_api_workflows.py`, `test_classification.py`, `test_classifier_activation.py`, `test_classifier.py`, `test_permission_filtering_security.py`, `test_parser_operations.py`, `test_thumbnail_operations.py`, `test_management_thumbnails.py`, `test_parsers.py`.
- `src/paperless_mail/tests/`: `test_mail.py`, `test_mail_nfc.py`, `test_preprocessor.py`, `test_api.py`, `test_process_mail_accounts_overlap.py`.
- `src/paperless_ai/tests/`: `test_classification_provider.py`, `test_ai_classifier.py`, `test_matching.py`, `test_lazy_imports.py`, `test_ai_indexing.py`.
- `src/paperless/tests/`: `test_worker_trace.py`, `test_worker_serialization.py`, `test_disabled_feature_workflows.py`, `test_import_boundaries.py`, `test_celery.py`, `test_checks.py`, `test_checks_v3.py`, `test_docker_service_checks.py`, `test_registry.py`, `test_ocr_config.py`, `test_remote_ocr_config.py`, `test_parser_utils.py`, `test_config_precedence.py`, `test_asgi.py`.
- `src/paperless_worker/tests/` (entire directory, subject to the exclusions above).
- `src/paperless/tests/parsers/` (entire directory, subject to the exclusions above).

Coverage includes real OCR, replacement providers, effective settings and engine
concurrency, remote permission and credential redaction, archive/display policy,
local thumbnail fallback, malformed output, timeouts and bounded retries,
changed provider/configuration/document rejection, retained inputs, cleanup and
publication crash points, workflows, permissions, versions, classification, and
normal/lean-worker startup. A standalone `python -S` import of the contract and
text provider also succeeded without Django or installed site packages.

`makemigrations --check --dry-run --skip-checks` found no missing migrations.
Changed-file hooks passed, including Ruff, Prettier and codespell. The temporary
verification image and volume were removed after verification. Verification
preserved the #24 baseline and unrelated workflow/memory-report changes. No
GitHub issue was modified during verification, and nothing was pushed or published.

The full suite, PostgreSQL/MariaDB, live remote services, deployment and RAM
measurements were not checked. Existing PDF form-copy, missing-static-directory
and asynchronous streaming warnings remain. Full PDF/A standards certification
and sandboxing installed providers are outside this boundary; uncertain ingestion
effects still require the #23 explicit reconciliation procedure.

## Search records and execution (#26)

The public `documents.search` API remains the application-facing adapter.
`search._backend` coordinates persistence and engine calls;
`search._persistence` resolves roots, prefetches index relations and effective
version content, and fetches direct user/group grants in chunks. Group grants
stay group IDs so removing a user from a group takes effect on the next query.
The adapter fetches current memberships for each permission-filtered operation.

`search._records.document_to_record` maps that state to plain field/value lists.
It preserves normalized text, effective version content and barcodes, structured
notes/custom fields, autocomplete terms, date-range handling, and permissions.
`search._engine.TantivyEngine` accepts those records and explicit visibility IDs;
it neither reads ORM state nor dispatches tasks. Query language, fuzzy threshold
and the active request timezone are explicit inputs, resolved per request rather
than cached in the singleton. Index opening and rebuild sentinels receive the
configured index language explicitly. The existing Tantivy schema and query
syntax are unchanged.

The engine attempts one writer-lock acquisition and reports failure.
`documents.search_jobs` owns the existing four lock attempts, capped exponential
jitter, and the 60-second deferred Celery dispatch. Deferred task names and
bounded Celery autoretry remain unchanged. Batch operations propagate exhausted
lock errors; the single-document adapter and merge-removal caller retain their
existing deferred behavior. A deferred update reads current database state,
rather than queuing a captured record. A missing row is skipped; deletion paths
continue to remove index IDs explicitly, including rows already deleted.

The batch adapter acquires the writer **before** reading records. Durable
reprocessing and ingestion completion therefore retain their existing fresh-read
ordering, and update completion still indexes final workflow state. Rebuilds
stream records and permission grants in chunks and preserve progress callbacks,
interrupted-rebuild markers and fresh writer handles across processes. Writer
initialization failures also release the file lock. The engine retains atomic
batch commit/rollback and releases the writer after commit failures.

This is an ownership extraction, not a search-engine replacement or a residency
change. API responses, query semantics, permission policies, database choices,
workflow ordering and document job recovery semantics are unchanged. No RAM
saving is inferred from this source change. #27 still requires a measured
comparison on a representative deployment and workload.

### Search boundary verification (2026-11-03)

The focused selection passed **632 cases**: all `src/documents/tests/search/`
and `src/documents/tests/test_api_search*.py`, including the six new boundary
tests. The broader selection ran **1,448 cases**: **1,443 passed** initially;
three admin tests still accessed the moved private engine attributes, and two
unchanged file-handling tests failed around their hard-coded document ID 1.
After updating the admin helper, all **85 cases** in `test_admin.py` and
`test_file_handling.py` passed together. Thus **2,080 selected cases passed across
these broad and focused runs**; the 85-case rerun overlaps the broader selection
and is not added to that total. Repeating the identical 1,448-case broad
selection against the exact pre-change commit `dd6b80b14` produced **1,446 passed
and the same two file-handling failures**, confirming preexisting order-dependent
filename assumptions. Those unrelated tests were not changed.

All runs used disposable `docker run --rm --userns=host` containers as an
unprivileged user, with read-only source copied into temporary Docker storage.
Dependencies and test artifacts stayed inside that storage. The environment was
Python 3.14.2, Django 5.2.17, SQLite, Ghostscript 10.05.1, frozen uv testing/lint
dependencies, and `pytest -o addopts='' -n 4 --dist=loadscope --randomly-seed=1
--tb=short`. The broad selection was:

- `src/documents/tests/`: `test_document_jobs.py`, `test_ingestion_jobs.py`, `test_job_callbacks.py`, `test_document_jobs_command.py`, `test_tasks.py`, `test_bulk_edit.py`, `test_api_bulk_edit.py`, `test_scheduler_api.py`, `test_api_documents.py`, `test_api_permissions.py`, `test_api_document_versions.py`, `test_version_conditionals.py`, `test_file_handling.py`, `test_document_updates.py`, `test_workflows.py`, `test_consumer.py`, `test_consumer_archive.py`, `test_management.py`, `test_management_exporter.py`, `test_management_importer.py`, `test_task_signals.py`, `test_worker_outcomes.py`, `test_consumer_api.py`, `test_document_maintenance_updates.py`, `test_bulk_document_updates.py`, `test_storage_path_updates.py`, `test_merge_documents_as_versions.py`, `test_admin.py`, `test_ingestion_followups.py`, `test_api_workflows.py`, `test_permission_filtering_security.py`, `test_classification.py`, `test_classifier_activation.py`, `test_parser_operations.py`, `test_thumbnail_operations.py`.
- `src/paperless/tests/`: `test_import_boundaries.py`, `test_worker_trace.py`, `test_worker_serialization.py`, `test_disabled_feature_workflows.py`, `test_celery.py`, `test_checks.py`, `test_checks_v3.py`, `test_docker_service_checks.py`.
- `src/paperless_worker/tests/` (entire directory).
- `src/paperless_ai/tests/`: `test_ai_classifier.py`, `test_ai_indexing.py`, `test_lazy_imports.py`, `test_classification_provider.py`.

Coverage includes query syntax/errors/highlights, Unicode/CJK, fuzzy and date
queries, permission revocation, final workflow state, root/version records,
chunked permission loading, plain records without database access, explicit
query settings, lock retries/deferred tasks, writer failure cleanup, rebuilds,
durable job recovery and normal/lean-worker compatibility.

`makemigrations --check --dry-run --skip-checks` found no missing migrations.
Changed-file hooks passed, including Ruff, Prettier and codespell. No production
runtime, full suite, PostgreSQL/MariaDB, live remote services, RAM measurements or
long-idle deployment workload was checked. No push, deployment or image publication
was performed. The preexisting workflow and memory-report changes remain excluded.

## Language detection residency (#27)

Document metadata resolves access permissions and the requested effective version
before passing its content to `documents.language.detect_language`. The detector
runs the existing `langdetect` implementation in a short-lived Python process;
its language profiles are released when that process exits. UTF-8 text crosses a
private standard-input pipe, and the parent accepts only a language-code result.
The request contains only document text, without paths or database objects.
The helper does not initialize Django, publish files, modify documents or enqueue
work. `subprocess.run` owns waiting, timeout termination and child reaping, without
a persistent pool or custom lifecycle manager.

The metadata response, permission/version selection and cache behavior are
unchanged. Empty content returns the existing English fallback; failed or timed-out
inference still uses that fallback in the metadata endpoint. Each nonempty request
pays model startup time. Concurrent requests may overlap temporary detector memory;
this must be counted when evaluating the candidate.

Verification passed **386 selected cases**, including real
concurrent multilingual inference, Unicode pipe encoding with an ASCII child I/O
environment, arbitrary working directories, absence of detector imports in the
caller, invalid results and timeout fallback. API checks cover permission denial,
selected-version content and existing metadata behavior. The selection also covers
durable ingestion/reprocessing, retries, stale-result rejection, recovery, parser
operations and normal/lean-worker compatibility. It used Python 3.14.9, Django
5.2.17, SQLite and frozen uv testing/lint dependencies in disposable
`docker run --rm --userns=host` containers. `uv run --no-sync ruff format .` ran in
the disposable source copy; only the owned test file needed formatting. Ruff passed
for all five changed Python files.

The exact selection used `pytest -o addopts='' -n 4 --dist=loadscope
--randomly-seed=1 --tb=short`:

- `src/documents/tests/`: `test_language.py`, `test_api_documents.py`,
  `test_api_document_versions.py`, `test_version_conditionals.py`,
  `test_api_permissions.py`, `test_document_jobs.py`, `test_ingestion_jobs.py`,
  `test_job_callbacks.py`, `test_document_jobs_command.py`,
  `test_parser_operations.py`, `test_thumbnail_operations.py`.
- `src/paperless/tests/`: `test_import_boundaries.py`, `test_asgi.py`,
  `test_celery.py`, `test_worker_trace.py`.
- `src/paperless_worker/tests/` (entire directory).

Three runs per variant measured a 59.21 MiB (14.7%) reduction in post-feature
Paperless PSS and a 59.12 MiB (5.0%) reduction across the six local services.
Metadata requests cost approximately 0.31 seconds extra. Four repeat runs also
verified actual queued retry/recovery, committed completion without reparsing,
and stale-result rejection after the idle workload. The methods, individual
measurements, rejected alternatives and scope limits are recorded in
[memory-measurement.md](memory-measurement.md). Existing missing-static
and synchronous-ASGI-stream warnings remain. The full suite and MariaDB were not
run for this change; the isolated residency workload uses PostgreSQL 18.

The final migration check found no model changes. All applicable changed-file
hooks passed (12 passed, seven with no matching files), including Ruff, Prettier,
codespell and Dockerfile checks. Independent review found no material correctness
or safety issue. Preexisting workflow and memory-report diffs were preserved.
