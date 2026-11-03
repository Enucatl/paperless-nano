# Document operation inventory

This is the focused source inventory for [#21](https://github.com/Enucatl/paperless-ngx/issues/21),
under the architecture plan in [#20](https://github.com/Enucatl/paperless-ngx/issues/20).
It records the behavior inspected before the first document-update extraction.
The boundaries below are implementation targets, not claims that crash recovery,
stale-result rejection, or process isolation already exist.

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
