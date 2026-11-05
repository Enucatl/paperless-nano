# Document operation inventory

This is the focused source inventory for [#21](https://github.com/Enucatl/paperless-ngx/issues/21),
under the architecture plan in [#20](https://github.com/Enucatl/paperless-ngx/issues/20).
It records the behavior inspected before the first document-update extraction.
The boundaries below are implementation targets, not claims that crash recovery,
stale-result rejection, or process isolation already exist.

The traces and #22 acceptance below describe the state before #23. The current
ingestion, reprocessing, and callback recovery contract is documented under
[Recoverable ingestion coordinator](#recoverable-ingestion-coordinator-35);
[Durable document jobs](#durable-document-jobs-23) records the earlier design.

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

## Current document-update completion contract (#30)

This is the behavior after #22 and #28, before the structural changes in
#31–#34. The earlier “Change tags or custom fields” section describes the
pre-extraction flow. “Completed” has several meanings: metadata persistence,
synchronous follow-up completion, and eventual search/AI visibility are distinct.
No update-wide database/filesystem transaction or durable completion receipt is
created by `document_update` or `document_metadata_update`.

### Caller and sequence map

| Entry point                                  | Persistence and files                                                                                                                                                                                                                                                       | Workflows, notification, indexes, and response                                                                                                                                                                                                                                                                                          |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DocumentViewSet.update` (PUT/PATCH)         | Root serializer validation and `save_document_metadata`; root metadata scope reconciles files after related writes. Selected/latest version content is saved in its own metadata scope when required.                                                                       | Capture the response **before workflows**. On scope exit, reload root/effective content → updated workflows → `document_updated` → final full-text write/removal. Return the captured response only if these synchronous stages return successfully.                                                                                    |
| `bulk_edit` scalar/tag/permission operations | Queryset/through-table writes happen synchronously, then enqueue `bulk_update_documents`. Those writes bypass filename signals. Custom-field operations already reconcile affected templates before enqueueing.                                                             | The API's “OK” means writes and dispatch succeeded, not workflow completion. The task calls `complete_document_updates`: clear each document's caches → updated workflows → **post-workflow file reconciliation** → notification with per-document AI disabled. After all documents: one full-text batch, then one AI batch if enabled. |
| `DocumentViewSet.update_version_label`       | Validate input/root access/version membership; save label in the version's metadata scope; update root modified time; record changed-label audit entry against the root.                                                                                                    | Complete the root with updated workflows → notification → final full-text index. Then return the selected version instance's fields. This is not a freshly serialized post-workflow document.                                                                                                                                           |
| `DocumentViewSet.delete_version`             | Validate root access/version membership and forbid root deletion; remove version search entry; delete the version; update root modified time and audit. Compute the remaining current version.                                                                              | Complete the root with updated workflows → notification → final full-text index. Return the previously computed current-version ID; it is not recomputed after workflows.                                                                                                                                                               |
| `ConsumerPlugin` adding a version            | Creation/publication and the consumer's transaction own the new version; save it before entering root completion.                                                                                                                                                           | Root updated workflows and notification run inside the surrounding consumer transaction. Skip per-document AI; durable ingestion also skips this immediate full-text write because ingestion completion owns indexing. This is not the standalone HTTP update guarantee.                                                                |
| `run_workflows` on persisted documents       | Refresh before each workflow, match effective content, execute actions in order inside one metadata scope per matched workflow; save selective scalar fields, then reconcile files. Record `WorkflowRun` afterward; a trash action runs after the workflow's other actions. | Nested assignment/removal scopes do not recursively trigger updated workflows. Later workflows see earlier mutations. A failure stops remaining actions/workflows. Direct scheduled workflows notify through their existing adapter; callers do not all enter `document_update`.                                                        |
| Workflow consumption overrides               | Mutate the override value object before a document exists; no metadata scope or document file reconciliation.                                                                                                                                                               | The consumer applies overrides later. This is preparation, not a completed document update.                                                                                                                                                                                                                                             |
| AI assignment, admin, notes, maintenance     | Use explicit metadata scopes or the existing file reconciler; each adapter retains its own indexing/cache policy.                                                                                                                                                           | They do not automatically invoke updated workflows. See the #22 caller inventory below; metadata persistence alone is not full update completion.                                                                                                                                                                                       |

`document_metadata_update` restores its document-local deferral guard even on
failure, but reconciles only after its body returns normally. Nested scopes for
the same document reconcile at the outer boundary; reciprocal linked documents
have their own scope. Raw ORM saves still have the legacy filename receiver at
this stage. File reconciliation refreshes metadata under `MEDIA_LOCK`, protects
paths/collisions, and reconciles version paths. It can move files before the API
response snapshot and again after a workflow changes filename-dependent fields.

`documents.apps.DocumentsConfig.ready` connects `document_updated` receivers in
this order: websocket notification, then AI scheduling. It does **not** connect
updated workflows or full-text indexing to that event. The websocket receiver
refreshes the document and sends current modified/access information. The AI
receiver, when enabled and not explicitly suppressed, registers
`update_document_in_llm_index` with `apply_async_on_commit`. Without an outer
transaction, registration dispatches immediately; inside one it waits for commit.
A successful response therefore does not promise an updated AI index or client
receipt of the websocket message. An AI dispatch error can occur after websocket
notification and, with an outer transaction, after database commit.

Full-text writes read final committed/visible document state rather than the API
snapshot. Trashed/missing documents are removed. A single-document Tantivy write
may exhaust its bounded lock retries and enqueue a fresh-state write/removal for
later; that path returns successfully once dispatch succeeds. Bulk batch lock
failure propagates instead of being silently accepted. Search visibility is
therefore not an unconditional guarantee of HTTP success.

### Failure and retry boundary

| Failure point                                               | Durable/visible result and later work                                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Validation/access rejection before mutation                 | No update completion runs. Existing permission and malformed-custom-field tests assert no metadata change.                                                                                                                                                                                                                                               |
| Exception during a metadata scope                           | Earlier writes may already be persisted. The guard resets; that scope does not reconcile files. Neither context manager adds rollback.                                                                                                                                                                                                                   |
| Handled file move/path/database error inside reconciliation | Log the failure, attempt to restore moved files, retain old filename fields. Saved metadata remains; workflows/notification/indexing may continue and the HTTP request may succeed. Failed restoration is not proof that files are at their old paths; sanity checks and logs are needed. Errors outside the reconciler's handled block still propagate. |
| Workflow failure                                            | Earlier metadata, file moves, workflow actions, and external effects may remain. No subsequent update notification or final index write is attempted. The prepared API response is not returned as success. Version label/deletion mutations and root timestamps also survive standalone endpoint completion failure.                                    |
| Notification receiver failure                               | Workflows and prior persistence remain. Django's ordinary `Signal.send` stops at the failing receiver; subsequent receivers and final full-text indexing do not run. A preceding websocket send may already have happened.                                                                                                                               |
| Full-text write or deferred-dispatch failure                | Notification/AI scheduling may already have happened; metadata and workflow results remain. The error propagates. Lock exhaustion with successful single-write dispatch is the exception described above.                                                                                                                                                |
| Bulk enqueue failure                                        | Synchronous metadata writes remain, but completion may never run. There is no update outbox for discovering that missing dispatch.                                                                                                                                                                                                                       |
| Failure during bulk completion                              | Earlier documents may already have completed workflows, moves, and notifications; later ones may be untouched. A failure before the batch prevents the final full-text and AI batches; an AI-batch failure occurs after full-text completion.                                                                                                            |

There is no automatic whole-update replay guarantee. The plain
`bulk_update_documents` task has no explicit automatic retry policy, and manually
rerunning completion reruns workflows, including externally effectful actions.
Reconciliation can repair a filename using current metadata without replaying
workflows, but it cannot undo an email or webhook. Ingestion/reprocessing durable
job recovery is a separate contract; it must not be assumed to cover ordinary
metadata updates. An enclosing caller transaction can roll back its database
writes, but cannot roll back published file moves, notifications, or external
workflow effects.

### Verification and subsequent decisions

`test_document_updates.py` establishes the pre-workflow API snapshot, final search
state, effective-version matching, ordered workflow mutations, permission and
validation rejection, handled move failure, guard restoration, and workflow
failure. Added cases distinguish notification/search failure from HTTP success
and show that version label/deletion persists when root completion fails.
`test_bulk_document_updates.py` additionally records the workflow → file →
notification → search batch → AI batch order and verifies that workflow failure
leaves synchronous bulk writes intact while preventing later completion stages.
Existing version tests cover permission, selection, response fields, audit, and
root reindexing; workflow/file suites retain action ordering and file safety.

Verification: **310 passed** across `test_document_updates.py`,
`test_bulk_document_updates.py`, `test_api_document_versions.py`,
`test_workflows.py`, `test_file_handling.py`, and `test_tasks.py`. The run used
Python 3.14.10, Django 5.2.17, SQLite, frozen uv dependencies, and
`pytest -o addopts="" --tb=short -n 4 --dist=loadscope --randomly-seed=1` in a
non-root disposable `docker run --rm --userns=host` container. Ruff formatting
ran on the disposable source copy; Ruff checks and `git diff --check` passed.
No production code changed; the full suite and other database engines were not
run for this characterization step.

No production behavior changes in #30. #31 and #32 should relocate ownership
without changing this contract. #33 must finish write-path migration before
removing the filename receiver. #34 must explicitly decide how operators detect
and repair partial updates, whether any durable mechanism is justified, and
which actions may safely repeat. Changing response timing, delivery guarantees,
or replay semantics is a separate decision, not an incidental extraction.

## Version operations (#31)

`documents.updates.update_document_version_label` and
`delete_document_version` now own version mutation, root timestamps, actor-aware
audit entries, and root completion through `document_update`. The HTTP adapters
retain request validation, root permissions, version membership/root-deletion
checks, and response serialization. No transaction or retry behavior changed.
Deletion still selects the returned current-version ID before workflows run;
label responses still use the selected version instance. Exceptions during
completion retain the partial-update behavior described above.

The version API and update contract tests cover permissions, version selection,
audit attribution, notifications, indexing, and persisted mutations after failed
completion. A label-audit regression also checks that repeated unchanged labels
do not add a second label-change entry.

Verification: **285 passed** across the document-update, bulk-update, version API,
version-conditional, workflow, and file-handling suites in a disposable
`docker run --rm --userns=host` container. Tests ran as a non-root user with
Python 3.14.10, Django 5.2.17, SQLite, and frozen uv dependencies. Ruff formatting,
changed-file lint, and `git diff --check` passed.

## Workflow orchestration (#32)

`documents.workflows.runner.run_workflows` now owns matching and ordered action
execution. Document updates, consumption overrides, and scheduled workflows call
it directly. `signals.handlers.run_workflows_added` remains a thin adapter for
the document-consumption signal. The runner keeps the existing metadata scopes,
fresh-state/effective-content matching, action ordering, deletion handling,
exception propagation, and logger name. This extraction changes no completion
or retry guarantees from #30.

Verification: **470 passed** across workflows, document and bulk updates,
maintenance updates, version and document APIs, ingestion follow-ups, tag
hierarchy, tasks, and consumers. Tests used Python 3.14.10, Django 5.2.17, SQLite,
frozen uv dependencies, and `pytest -o addopts="" --tb=short -n 4 --dist=loadscope
--randomly-seed=1` in a non-root disposable `docker run --rm --userns=host`
container. Ruff formatting/checks and `git diff --check` passed. The full suite
and other database engines were not run for this relocation.

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
Providers can implement `configuration_for(context)` to include document context
(currently `mailrule_id`) in their effective snapshot. `selection(request, root)`
returns a JSON object with a nonempty execution `identity`, boolean `replay_safe`,
and boolean `uses_remote_service`. Include internal engine versions and effective
configuration in that declaration: core freezes and compares the entire object on
retry and publication. A result from an internally selected engine includes its
`execution` identity; without that field the provider identity is used. All
providers use the same checks. Missing replay promises default to unsafe. An
optional `reconcile(request, root)` can return a validated result for the same
`invocation_id` after an uncertain outcome; it must not repeat unknown effects.

Optional `accepted_mime_types()`, `initialize_worker()`, and `check()` methods let
an implementation own format admission, worker initialization and deployment
checks. Local thumbnail rendering remains a separate auxiliary implementation.

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

## Explicit file reconciliation ownership (#33)

The legacy `Document.post_save`, document-tag `m2m_changed`, and
`CustomFieldInstance.post_save` filename bridge has been removed. Raw ORM writes
now persist data only; code that changes filename-dependent metadata must use
`document_metadata_update` or call `reconcile_document_files` after its writes.
The importer no longer disconnects those receivers. This supersedes the legacy
receiver described in the earlier #22/#30 snapshots.

The public signal-suppression helper is gone. A private context-local set remains
only to coalesce nested explicit metadata scopes: an inner scope for the same
document waits for its outer scope; a different linked document has its own
boundary. Failed scopes restore that ownership and do not reconcile incomplete
writes. The scopes do not add transactions or replay workflows.

### Write-path inventory

| Writes                                                                   | Reconciliation owner                                                                                                                                                                                                                 |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Document API metadata, tags, permissions and custom fields               | `save_document_metadata` completes the root scope after all writes. Selected-version content and version labels have explicit scopes. Notes retain their own scope.                                                                  |
| Admin scalar and related document edits                                  | `DocumentAdmin.save_model` / `save_related`; full-text indexing follows related writes. Custom-field instance values are read-only in admin.                                                                                         |
| Workflow assignment/removal and AI suggestions                           | The workflow runner encloses each matched workflow; AI assignment uses a nested metadata scope. Consumption overrides remain values until the consumer publishes the document.                                                       |
| Inbox/classifier tags and scalar classification                          | Assignment adapters own explicit scopes, including tag removal with no replacement. Dry-run/no-change classification does not reconcile.                                                                                             |
| Bulk scalar/tag/owner/ASN writes and tag-parent propagation              | Existing queued `complete_document_updates` reconciles after workflows. Through-table/queryset writes already bypassed signals. Merging versions retains that batched completion owner.                                              |
| Individual custom-field serializer writes                                | `save_custom_field_value` owns a scope when the filename template uses custom fields; nested API/bulk scopes coalesce it.                                                                                                            |
| Bulk custom fields and reciprocal links                                  | Bulk fields reconcile after the complete batch. Reciprocal additions and removals explicitly reconcile affected targets; source ownership remains with its operation. Select-option updates use their existing background operation. |
| New ingestion and version ingestion                                      | Initial documents have no published filename. The consumer computes paths after metadata/workflows, publishes under the media lock, then explicitly saves/reconciles outside that lock.                                              |
| Reprocessing/external archive publication                                | Existing publication code owns generated archive paths and protected database reference updates; metadata completion is unchanged.                                                                                                   |
| Export/import and maintenance                                            | Import restores manifest paths and files without renaming; renamer directly reconciles each document. Retagger uses assignment operations. Trash/delete retain their separate cleanup owners.                                        |
| Root timestamps, job state, caches, audit, notes and non-document models | Their existing operations retain ownership; these are not replacement implicit filename hooks.                                                                                                                                       |

`reconcile_document_files` itself is unchanged: it refreshes persisted metadata
under the media lock, protects root paths, handles collisions, recovers moved
files/checksums, and reconciles version paths. Filename columns are still excluded
from stale serializer writes. A raw extension/plugin ORM write must now explicitly
request reconciliation; restoring the signal as a compatibility workaround would
reintroduce partial-write moves.

Verification: **627 tests passed**, plus **10 maintenance tests** after adding
the removal-only tag regression (nine overlap). Coverage includes explicit file
moves, collisions, path rejection, filesystem
and database failure recovery, interleaved/stale filename edits, nested and
failed scopes, reciprocal link additions/removals, admin and classifier writes,
consumer publication, import, workflows, and durable document jobs. Raw ORM
regression coverage proves there is no remaining filename receiver. Tests run
in non-root disposable `docker run --rm --userns=host` containers with Python
3.14.10, Django 5.2.17, SQLite and frozen uv dependencies. Full-suite and other
database-engine testing remain outside this focused change.

## Partial document-update recovery (#34)

Ordinary metadata updates retain the #30 completion contract: persisted metadata
is authoritative even if a later stage fails. A successful API response remains
the pre-workflow snapshot, returned after synchronous completion; handled file
errors can still return success. Bulk success still means persistence and task
dispatch, not completed workflows. There is no automatic whole-update retry or
new delivery guarantee.

### Retry inventory and safe repair

| Stage                                      | Existing retry and detection                                                                                                                                                                                                                                      | Recovery action                                                                                                                                                                                                                                                                                          |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Metadata/file reconciliation               | No automatic retry. Handled move/path/database failures log `Exception during file handling`; metadata stays saved and filename references retain their previous values. Errors acquiring the lock or refreshing the row propagate.                               | Correct permissions, space, or path configuration, then run `document_renamer`. It refreshes current metadata under the media lock and does not execute workflows. Run `document_sanity_checker` afterward.                                                                                              |
| Partial filesystem move/failed restoration | A completed move is compensated when possible. Failed compensation now logs document ID and both paths at error level. The sanity checker finds missing, corrupt, or orphaned files, but does not identify every template mismatch.                               | Preserve both paths. Reconciliation can recognize an already-moved candidate when its checksum matches and the old file is absent. Otherwise inspect recorded checksums and resolve the paths manually; do not delete an orphan just because it is unreferenced. Re-run the sanity checker after repair. |
| Workflow execution                         | Exceptions propagate and stop notification/final indexing. Earlier writes and external actions may already have happened. `WorkflowRun` records completed workflows; absence of a record does not prove no action ran. The runner has no automatic replay policy. | Inspect the request/task error, workflow logs and external destination. Reconcile ambiguous email/webhook/hook outcomes with that destination. Repair desired metadata explicitly, then files and indexes. Re-run a workflow only after establishing that its earlier actions are safe to repeat.        |
| Websocket notification / AI dispatch       | A receiver exception propagates. Earlier receivers may already have run; no notification receipt or durable dispatch intent exists for these updates. AI index tasks have no explicit automatic retry policy.                                                     | Reload the document/list to read authoritative state. For enabled AI indexing, `document_llmindex update` refreshes changed documents; use its documented rebuild operation when a full rebuild is needed. Do not replay all workflows merely to resend a notification.                                  |
| Full-text indexing                         | Single writes retry lock acquisition four times, then dispatch a fresh-state task. That task retries lock errors up to five times. Other errors and failed dispatch propagate. Batch writes can fail rather than defer.                                           | After resolving the index/broker error, run `document_index reindex`; it derives the index from current database state without workflows. Explicit removal handles deleted IDs; a full rebuild removes stale entries.                                                                                    |
| Bulk completion                            | No explicit automatic retry on `bulk_update_documents`. Enqueue failure leaves metadata saved; task failure can leave earlier documents completed and later documents untouched. Task success is not an external-effect receipt.                                  | Use the failed request/task's document IDs and inspect each document. Repair files/indexes separately. Do not blindly requeue the batch: that repeats workflows for already-processed documents.                                                                                                         |

Run these commands through the deployment's normal management-command entry
point, for example `python3 manage.py document_renamer` from `src` on a bare-metal
installation. The renamer and index command operate on the collection; extension
code with a known document can instead call `reconcile_document_files(document)`
and the search service's `add_or_update` on a fresh row. These repairs intentionally
do not claim to finish an unresolved workflow. See [management utilities](administration.md#management-commands)
for command execution and the existing file/index maintenance guidance.

### Smallest mechanism and its limits

Existing maintenance commands are sufficient for derived file/index repair.
They cannot automatically reconstruct whether arbitrary external workflow effects
completed, and ordinary metadata updates do not promise that guarantee. Adding
an update queue or outbox alone would not make replay of those effects safe, so
this change adds neither. Lost bulk dispatch and interrupted workflows still
require operator review; there is no persistent per-update completion ledger.
The explicit ingestion lifecycle in #35 has different durability requirements.

The demonstrated file-safety gap is fixed in the shared reconciler: compensation
now moves only files this invocation successfully moved. Previously, a rejected
collision target could be treated as rollback input and overwrite the original.
Compensation also refuses an occupied original path, reports failed restoration,
and attempts original/archive restoration independently. It retains ambiguous
artifacts for inspection instead of silently treating failed restoration as
success. The media lock coordinates application writers; arbitrary external
filesystem edits still require operator coordination. SQL rollback does not undo
published file moves or external workflow effects.

Focused coverage exercises a collision discovered after filename selection,
failed rollback with and without an occupied previous path, and recovery of files
and search after a workflow's partial mutation without replaying that workflow.
Existing cases cover handled filesystem/database failures, already-moved checksum
recovery, API/notification/index failures and bulk ordering. These decisions
complete #29's update-operation work together with #30–#33: operation ownership,
explicit reconciliation, preserved completion ordering, and documented recovery.

Verification: **368 tests passed** across document updates, file handling, bulk
updates, API versions, maintenance updates, tasks, workflows, ingestion jobs and
reprocessing jobs. Tests used non-root disposable `docker run --rm --userns=host`
containers, Python 3.14.10, Django 5.2.17, SQLite and frozen uv dependencies.
`uv run --no-sync ruff format .`, changed-file Ruff checks and
`git diff --check` passed. Full-suite, other database engines and live external
workflow destinations were not tested.

## Recoverable ingestion coordinator (#35)

This section supersedes #23's conservative whole-ingestion recovery policy.
`ingestion_jobs.run_consumption` owns the lifecycle; `ingestion.prepare`,
`ingestion.publish`, and explicit completion steps perform the work. Consumers
return preparation results and publication results, without advancing job state.
The existing `DocumentJob` JSON fields hold a versioned lifecycle (`lifecycle: 1`),
its durable `phase`, invocation records, and required completion intents. No new
queue, workflow-engine dependency, or database table is introduced.

### Phases and restart invariants

| Phase      | Durable meaning                                                                                                                                                                                                                                                   | Restart behavior                                                                                                                                            |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Accepted   | Input identity, checksum, options, script selections, and a private input copy exist. The job is `queued`; broker delivery is not required for discoverability.                                                                                                   | The existing periodic recovery task republishes undispatched jobs. Interrupted admission remains `staging` and requires inspection.                         |
| Preparing  | Each invoked plugin has a stable UUID, implementation identity/version, input references/metadata, outcome status, result, and error.                                                                                                                             | Reuse successful outcomes. Replay only explicitly safe invocations; otherwise ask the plugin to reconcile or retain an unknown outcome for operator action. |
| Prepared   | Every required preparation invocation succeeded, retained outputs passed validation, and metadata, detected date, MIME type, page count, text, and file references survive worker loss. A terminal duplicate/stop outcome is an alternative to document creation. | Validate retained references and authority, reconcile owned publication leftovers, then publish without repeating OCR.                                      |
| Committed  | Document/version creation, final stored file references, commit checkpoint, root identity, and required completion intents committed in one database transaction.                                                                                                 | Never create the document again. Start or resume completion.                                                                                                |
| Completing | Named metadata assignment, workflows, extension notification, hook, indexing/cache, and transport completion have independent outcomes.                                                                                                                           | Skip successful steps; retry safe derived-state work. Unknown external effects block completion until reconciled.                                           |
| Complete   | Required work succeeded or was explicitly reconciled.                                                                                                                                                                                                             | Retry outstanding storage/source cleanup only; never replay completed processing.                                                                           |

`ingestion_state.transition` checks legal phase order and required outcome
presence. `attention`, `rejected`, and `failed` are operational statuses; they do
not erase the phase or invocation evidence. The existing `running` status maps to
Preparing. `committed` covers Committed/Completing. With native Celery callbacks,
`succeeded` records successful task execution while `phase` remains Completing
until callback receipts or an explicit operator acknowledgment allow cleanup.
The phase is the authoritative end-to-end lifecycle, not Celery's task status.

A job's lock lives outside its cleanup directory. All execution and operator
recovery hold that lock; duplicate delivery receives `Ignore`. The commit also
locks the job row and, for version ingestion, the root row. Workers must share
`DATA_DIR`, including its lock files, as they already share durable job inputs.
This works with SQLite and PostgreSQL; separate private worker data directories
are not a distributed locking mechanism.

### Steps, transactions, and compatibility

| Component                     | Ownership and effects                                                                                                                                                                                                                                                                                     |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preflight and ASN checks      | Existing ordered plugins. Read-only checks can repeat; duplicate deletion is conservatively treated as effectful.                                                                                                                                                                                         |
| Collation and barcode plugins | Existing setup/run interfaces and stop/child-ingestion behavior remain. Outcomes retain metadata and a copied input before cleanup. Interrupted split/dispatch/collation is opaque unless the implementation promises safe replay or reconciles it.                                                       |
| Consumption workflows         | Still run before parsing and produce overrides. They may have external effects, so whole-workflow execution is not automatically replayed.                                                                                                                                                                |
| Parser preparation            | `ConsumerPlugin.prepare` handles format repair, started notification, pre-consume hook, parsing, date detection, and retained outputs. No document is created here. Started notification and pre-consume hooks have their own recorded invocations.                                                       |
| Storage publication           | `ConsumerPlugin.publish` creates the document/version and files inside the coordinator's transaction. Permission, revision, current-version, parser configuration/identity, and retained-file integrity are checked before publication. Version timestamps and audit attribution remain part of creation. |
| Metadata and workflows        | Inbox tags, correspondent, type, tags, storage path, added workflows, and version-root updated workflows are named post-commit steps. Their existing explicit metadata scopes reconcile filenames, including version paths. Current effective content is used for matching.                               |
| Extensions and scripts        | The finished signal and post-consume hook run after commit and file publication. Their outcomes are tracked separately from document creation. Scripts receive a stable `INVOCATION_ID` as well as the existing `TASK_ID`.                                                                                |
| Required workflow tasks       | Existing durable webhook/AI callback records retain individual task outcomes. Their intents are recorded before dispatch/execution; ambiguous webhook effects are not replayed.                                                                                                                           |
| Search/AI/cache               | Read current committed state and may repeat. Completed indexing/cache steps are recorded, so later cleanup failure does not replay them.                                                                                                                                                                  |
| Source/storage cleanup        | A separate retryable outcome. Verify authoritative published files before removing retained inputs; delete only the admitted source/shadow identity. Failure leaves cleanup outstanding.                                                                                                                  |

**Intentional timing change:** document-added metadata assignments, workflows,
and `document_consumption_finished` now run after document/file commit for durable
ingestion. A workflow or extension failure leaves a committed document needing
completion rather than rolling its database row back around irreversible effects.
Extensions receiving the finished signal see published files and receive
`coordinated=True` and `invocation_id`. Built-in receivers skip that coordinated
notification because the coordinator has already invoked their explicit steps.
Legacy direct signal calls retain their adapters. The started signal remains
pre-processing and receives its own durable `invocation_id`; its external effects
are conservatively tracked. Direct
execution-only `ConsumerPlugin.run` remains an adapter around prepare/publish,
while normal task admission always uses the durable coordinator.

File naming reflects post-commit workflow metadata through the explicit
reconciler. Completion does not report success until those steps return. A
workflow that trashes/deletes a document preserves that outcome; subsequent
indexing reads current state and removes missing entries. A failed opaque workflow
may have applied earlier actions: do not rerun it simply because ingestion is
incomplete.

Preparation checks the recorded timeout before entering Prepared. An overlong
attempt retains its validated outcomes without publishing a document; a later
attempt can reuse those outcomes. Time spent waiting for recovery after Prepared
does not expire an otherwise valid result.

### Plugin result and recovery contract

`ConsumeTaskPlugin` retains its constructor, `able_to_run`, `setup`, `run`, and
`cleanup` interface. Construction must not perform external effects. Each run
receives `invocation_id`; the UUID remains stable across retry/reconciliation.
The implementation owns its effects and private workspace. It may declare
`replay_safe = True` only when repeating the entire invocation, including setup,
is safe. The default is false. A later deployment cannot retroactively grant a
replay promise to an already unknown invocation.
`StopConsumeTaskError` from either `setup` or `run` records a terminal stopped
outcome and proceeds to tracked cleanup.

The coordinator records `pending`, `running`, `succeeded`, `failed` (declared safe
execution failed), and `unknown` outcomes. Both an exception after an external
effect and a crash after return but before outcome persistence can be unknown.
Each invocation gets a working input copy; retained earlier inputs are immutable
checkpoints, so in-place plugin edits do not invalidate completed predecessors.
Successful output metadata and source references are retained before cleanup.
Only the built-in barcode cleanup can discard its known temporary workspace;
other cleanup follows the plugin's `cleanup_replay_safe` promise (default false).
Unresolved preparation retains artifacts rather than invoking destructive cleanup.

An optional `reconcile(invocation_id)` returns the retained public outcome after
the plugin establishes what happened externally, or raises if it cannot. Its
result is a JSON object containing `metadata` (the dataclass field mapping),
`source` (a contained SHA-256/size file reference, or null for a terminal stop),
`mime_type`, and optionally `stopped` (duplicate/stop response), `message`, or the
consumer's `prepared` output. The coordinator validates recovered references and
metadata before recording success. Reconciliation must itself be safe to repeat;
it is not permission to rerun the original effect. Execution-only plugins need
not implement it: the default raises an attention error with the invocation ID.

Parser construction and configuration snapshots must be side-effect-free;
processing receives the stable invocation identity. Parser requests also receive
optional `invocation_id`. Explicit parsers can
promise `replay_safe = True`; built-in local text, mail, and Tesseract computation
are treated as replayable. Remote/custom implementations default to no replay.
An ingestion preparation containing an effectful hook or started listener remains
conservative even when its parser is replayable. Reprocessing uses the same
`ingestion_state.invoke` outcome recorder and parser replay policy; a successful
validated parser result is retained if later preparation fails. Its existing
revision-bound archive publication and update-specific completion remain distinct
from ingestion's creation operation. Both operations share file references,
integrity checks, job locks, and configuration/provider validation.

### Storage ownership and operator recovery

Publication uses a private sibling copy in the destination filesystem, an fsynced
intent manifest, and an exclusive hard link to the final target. This supports
separate data/media filesystems. The retained sibling proves ownership by inode,
even if the worker dies immediately after creating the link. Before retrying an
uncommitted publication, storage removes only targets still sharing that owned
inode. A matching checksum alone is insufficient; unrelated replacement files,
symlinks, and collisions require inspection and are never overwritten or deleted.
Owned temporary copies are removed during reconciliation or successful cleanup.
Post-commit filename moves belong to the ordinary explicit file reconciler (#34);
the old ingestion context hook that journaled arbitrary workflow moves is removed.

Use `document_jobs --job UUID` to inspect phase, invocation IDs, identities,
inputs/results/errors, publication evidence, and outstanding work. Use `--resume`
for Accepted/Prepared/Committed/Complete work. Preparing jobs with an unknown
plugin outcome can resume only through their recorded replay promise or the
plugin's validated reconciler; they cannot be resolved with a blanket effects
acknowledgment. If an external provider needs a custom recovery procedure, retain
the input/workspace and reconcile that invocation before retrying.

For committed jobs needing attention, inspect persisted metadata, workflow action
logs, and external hook/webhook systems. After handling ambiguous effects,
`document_jobs --job UUID --resume --acknowledge-effects` records those unresolved
completion invocations as acknowledged and resumes only remaining work. This is
an operator assertion that effects were reconciled, not proof of exactly-once
execution. File collisions and legacy uncommitted publication require manual
inspection; no command guesses which unrelated file to remove.

### Migration

No schema migration is required. New admissions carry lifecycle version 1.
Queued legacy jobs have not invoked plugins and adopt the coordinator when run.
Legacy running/staging or unrecognized prepared records remain attention cases;
no old progress string is interpreted as a safe preparation checkpoint. Legacy
committed jobs retain their existing post-consume/callback completion semantics
and do not rerun added workflows. Failed/attention jobs and unresolved staging
files are retained. Completed-but-uncleaned jobs retry cleanup with original
identity checks. Old publication manifests remain inspectable, but lack the new
inode ownership proof and are not automatically removed. Interrupted legacy
reprocessing without invocation evidence also requires attention instead of
assuming an arbitrary parser is replayable.

Verification: **624 tests passed on SQLite**, covering ingestion lifecycle/jobs,
consumer/archive/API behavior, callbacks, recovery commands, reprocessing, parser
contracts, barcodes, collation, workflows, versions, file handling, and document
updates. **84 tests passed on PostgreSQL 18** across lifecycle, ingestion,
reprocessing, workflow follow-ups, and callbacks. The PostgreSQL run exposed a
preexisting explicit-PK fixture/sequence collision; the version-race test now
chooses a distinct fixture ID. After the final progress/phase changes, **130
focused SQLite tests passed**. Counts overlap. Ruff formatting/checks and
`git diff --check` passed. All runs used non-root disposable
`docker run --rm --userns=host` containers, Python 3.14.10, Django 5.2.17,
frozen uv dependencies, and `pytest -o addopts="" --tb=short -n 4
--dist=loadscope --randomly-seed=1`. The PostgreSQL service and its temporary data
were removed; the shared temporary dependency volume is retained only for the
remaining sequential issue implementations.

Coverage includes admission/broker loss, opaque exceptions and process death,
result-persistence interruption, optional reconciliation, safe replay,
Prepared restart without OCR, retained-output tampering, publication/SQL rollback,
replacement-file protection, post-commit signal visibility, version-root workflow
filenames, duplicate execution locks, callback uncertainty, and cleanup retry.
The full repository suite, MariaDB, live OCR services, and real external hooks
were not run; replay safety for third-party effects remains their authors'
responsibility, not a guarantee inferred by Paperless.

## Replaceable document search (#36)

`documents.search.get_backend()` is now a provider-neutral application service.
`PAPERLESS_SEARCH_PROVIDER` selects an installed no-argument class; unset retains
`documents.search.tantivy.TantivyProvider`. Only the chosen implementation is
imported. Public types live in `documents.search.contract`; underscore modules,
`TantivyBackend` (an internal native-test convenience), native schemas/tokenizers
and native sorting constants are not the replacement interface.

Core owns root identity, latest-version content, document facts, current
permissions and deferred job dispatch. `DocumentFacts` is a dictionary of named
fact lists: `id`, checksum, title, effective content, original filename, taxonomy
names, notes/user names, custom-field name/value pairs, effective barcode pairs,
dates, ASN, page/note counts, owner ID and direct user/group grants. Absent
optional facts have no entry. Text encoding is NFC; dates retain the full
application range. Core does not require sort copies, bigrams, snippet text,
autocomplete tokens or Tantivy's nanosecond date range. The native implementation
derives those values just before writing its schema. A replacement may use the
facts directly, join authoritative database rows, or maintain its own tables.
It need not pretend a SQL database is an external index engine.

### Provider operations and lifetime

| Operation                                                       | Contract                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `open()` / `close()`                                            | Acquire/release process-local resources. Close must also work before opening and after failed initialization. The service replaces its cached instance when the class changes; restart processes after changing implementation-owned settings.                                                                                                                       |
| `search(SearchRequest)`                                         | Return `SearchPage(count, hits, offset, matching_ids)`. Count is the exact filtered/authorized count before pagination; hit ranks are one-based positions in the full order. `matching_ids` is a callable returning ordered IDs, optionally a lazy SQL queryset. Normal API pagination never evaluates all IDs; legacy API `all` and explicit selection requests do. |
| `matching_ids(request)` / `highlight(request, ids, rank_start)` | Matching uses the same query, authorization and ordering semantics as pages. Highlights contain only authorized IDs. A hit has `id`, `score`, `rank`, and the existing field-to-highlight mapping. Escape untrusted text before adding supported highlight markup.                                                                                                   |
| `autocomplete(term, limit, scope)`                              | Suggestions must derive exclusively from the current authorized scope, including when old index permission fields disagree.                                                                                                                                                                                                                                          |
| `batch_update()`                                                | Return an atomic context manager with `add_or_update(DocumentFacts)` and `remove(id)`. Failed batches propagate and must roll back; no hidden partial acceptance. Core reads fresh state after entering the provider batch.                                                                                                                                          |
| `rebuild(records, writer_heap_bytes=...)`                       | Replace derived state from the core stream. The memory argument is an advisory existing CLI budget; SQL providers can ignore it. Core retains progress reporting.                                                                                                                                                                                                    |
| `needs_rebuild()` / `wipe()` / `optimize()` / `last_modified()` | Implement selected-provider freshness, derived-data removal, maintenance and health. A timestamp may be `None`. Never delete authoritative document state. Freshness checks run before `open()` so opening cannot hide a needed rebuild.                                                                                                                             |

`SearchRequest` carries a live, authorized root `QuerySet`, query text, mode
(`query`, `text`, `title`), optional advanced filter, ordering, direction,
offset/limit, optional similarity seed, and language/fuzzy-threshold/timezone
configuration. The queryset already includes application filters and current
owner/direct/group permissions. SQL implementations compose it as a query or
subquery before matching, ranking, count and slicing. Do not materialize it into
an ID list unless the implementation requires that. Core checks returned page
and highlight IDs; selection subqueries remain restricted to current scope.
Provider authors still own correct counts, term visibility, query behavior and
resource handling. Autocomplete cannot be authorized after leaking a term, so
its scope requirement is part of the provider contract.

The built-in provider retains its existing full-ID intersection for native
ranking and ORM-only ordering; that strategy is not imposed on replacements.
It now uses current database scope for every read, including stale-grant
revocation without reindexing. Similarity callers must have access to the seed;
core's similarity operation rejects an invisible seed before invoking a provider.
Normal default query syntax, highlights, sorting and result shapes remain intact.

`SearchQueryError` means a user-correctable invalid or unsupported expression and
maps to the existing HTTP 400 response. Other failures propagate normally.
`documents.exceptions.SearchWriteBusy` is an optional explicit declaration that a
single write can safely be retried from fresh state. Core schedules that retry;
batch failures remain visible. Tantivy's `SearchIndexLockError` derives from it,
and only the Tantivy provider performs bounded file-lock backoff. A PostgreSQL
provider need not use either error or any filesystem lock. Tasks, workflows,
ingestion completion, deletion and rebuilds all use the same selected service.

### Query compatibility and replacement installation

The public language remains the documented Paperless advanced query syntax,
including saved field queries, boolean/grouped expressions, phrases, ranges,
relative dates, wildcard/fuzzy expressions and structured-field paths. `text` and
`title` retain their existing simple-search meaning, and a simultaneous `query`
is an additional filter. `ordering=score` is ascending relevance;
`ordering=-score` is descending; absent ordering is best relevance first.

A replacement translates supported syntax to its implementation and raises
`SearchQueryError` for any unsupported construct, filter, ordering or similarity
operation. It must not silently drop a clause, treat structured syntax as literal
text, or substitute its own query language. Exact cross-engine ranking is not
promised, but filters and authorization must retain meaning. Providers must
document unsupported operations before switching; saved queries are not rewritten
or deleted. The default keeps its existing complete behavior.

The complete minimal independent implementation is `DatabaseProvider` in
[`test_provider_contract.py`](https://github.com/Enucatl/paperless-ngx/blob/architecture/idle-memory/src/documents/tests/search/test_provider_contract.py).
It composes the supplied queryset, counts/slices in SQL, returns a lazy selection
query, reads effective version content, supplies scoped completion/highlights,
and implements every lifecycle/write/maintenance method. It deliberately
supports only literal search and rejects advanced syntax. The companion
`PostgreSQLProvider` shows an executable FTS mapping; neither uses the native
engine/schema internally. They are contract examples, not production search
replacements.

For an external package, place the implementation and its imports in
`example_search/provider.py` and package it normally with `pyproject.toml`.
Install the built wheel into each Paperless Python environment/image, then set:

```ini
PAPERLESS_SEARCH_PROVIDER=example_search.provider.DatabaseProvider
```

No registry, score, capabilities dictionary or native index metadata is needed.
A provider with Django models can ship an application and migrations, install its
package, and add its application config to `PAPERLESS_APPS`; deploy its migrations
before enabling it. Provider-specific credentials/settings belong to that
package. An explicitly set `PAPERLESS_SEARCH_LANGUAGE` is passed to replacements
without imposing Tantivy's language vocabulary.

Quiesce writers before switching, preserve the old derived store, configure the
new class, run `document_index reindex --recreate`, and restart web/workers.
`--if-needed`, `optimize`, Docker startup and system status follow the selected
provider. Docker creates a native index directory only for the built-in provider.
If switching back, rebuild the old store from current documents; no authoritative
rows, permanent files or saved views are removed by a provider switch.

### PostgreSQL contract assessment

The contract supports a PostgreSQL implementation without a native-engine shim.
The executable assessment composes `request.scope` with effective-content SQL,
`SearchVector`, `SearchQuery(..., search_type="plain")` and `SearchRank`, then
executes SQL count, ordering and LIMIT/OFFSET. This proves current authorization,
version content, ranking and pagination can stay in the database. The query
contains no file locks, duplicated sort/bigram fields or Python ID intersection.
These primitives map to PostgreSQL `tsvector`, `tsquery` and `ts_rank`.
[Django FTS documentation](https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/search/)
describes these expression classes.

For a production provider, weighted vectors can combine title, effective content
and metadata; typed dates/numbers and structured fields remain SQL predicates.
An owned search table/GIN index can store derived vectors and be updated through
the atomic batch contract, or expressions can query authoritative rows. Use the
provided scope as an authorization subquery before ranking and pagination.
`ts_headline` can supply snippets after scope filtering, but its output must be
escaped/sanitized before HTTP display. Prefix vocabulary aggregation must join
visible rows. Similarity needs an explicit provider strategy; PostgreSQL's FTS
query functions are not a drop-in translation of every existing saved query.
[PostgreSQL text-search controls](https://www.postgresql.org/docs/18/textsearch-controls.html)
describe vectors, query constructors, ranking and headlines.

Remaining implementation constraints are explicit: translate Paperless's full
advanced grammar or reject unsupported constructs; preserve relative-date
request timezone; define fuzzy and similarity semantics; maintain vectors for
root/version lifecycle changes; and keep permission-sensitive completion terms
current. The assessment intentionally rejects advanced queries and similarity.
It validates the interface's suitability, not a production provider's feature
parity, relevance quality or performance. No memory/performance gain is claimed.

Verification: **927 passed** in the expanded SQLite selection (one PostgreSQL-only
assessment skipped), followed by **97 passed** on PostgreSQL 18 after the final
ordering regression was added, then **96 passed** in the final focused SQLite
run covering last-page and invalid-page handling (one PostgreSQL case skipped).
The SQLite selection covered all native search
suites, search APIs/error/length handling, management/status/admin, permissions,
update/bulk completion, ingestion/reprocessing, tasks, startup/settings and AI
full-text fallback. The PostgreSQL selection covered the independent provider and
real FTS assessment, record boundaries, search API, and update/bulk completion.
Both used Python 3.14.10, Django 5.2.17, frozen uv dependencies and
`pytest -o addopts="" --tb=short -n 4 --dist=loadscope --randomly-seed=1` in
non-root disposable `docker run --rm --userns=host` containers. PostgreSQL data
was temporary and removed with its service. Ruff checks/formatting and diff
checks passed. Existing SQLite-only test assumptions about generated IDs and
NULL ordering were corrected to exercise PostgreSQL's existing behavior; a
lock-release assertion now checks the actual acquired lock rather than unrelated
objects' garbage collection. The full suite, MariaDB and production external
providers were not run; the FTS assessment is not a production PostgreSQL provider.

## Service implementation contract (#37)

`paperless_services.contract.Service` documents the structural interface: a
no-argument constructor and `run() -> None`. Subclassing is not required. The shared
launcher reads only environment selection and uses `importlib` to load that class;
it never imports Django settings, initializes unselected engines, discovers entry
points, or checks behavioral compatibility. Import, constructor, and run failures
propagate normally to S6 supervision.

An implementation runs in the foreground until shutdown. It owns SIGTERM/SIGINT
handling, graceful cleanup, task correctness, retries and compatibility with its
service's contracts. It may `exec` its foreground executable, as the traditional
consumer/beat and lightweight Supercronic scheduler do. It must not daemonize or
leave unmanaged children. Unexpected failure must raise or exit nonzero; returning
normally ends the supervised process. Paperless owns class loading and invocation,
not certification of third-party behavior.

For a minimal installed replacement, package this module as `example_services` in
a wheel and install it with `uv pip install --system /path/example_services.whl` in a derived Paperless image. It needs no entry-point registration:

```python
# example_services.py
import os


class Consumer:
    def run(self) -> None:
        """Delegate signals and exit status to an installed compatible watcher."""
        os.execvp("my-paperless-watcher", ["my-paperless-watcher"])
```

Select it with `PAPERLESS_CONSUMER_CLASS=example_services.Consumer`. The referenced
executable must be installed too and must obey the consumption API contract:
`documents.service_views` and `paperless_services.watcher` define path admission,
authorization, stable idempotency keys and durable receipt reconciliation. Scheduler
implementations follow `documents.service_views` and
`paperless_services.scheduler`: fetch schedules and dispatch named jobs; they do
not duplicate task execution. Worker implementations follow Paperless's Celery
configuration/task registration, authenticated serialization, retries, callbacks,
durable job delivery and shutdown behavior; see `paperless_worker.bootstrap`,
`paperless_worker.serialization` and the ingestion/reprocessing sections above.
A queue consumer that merely calls task functions is not behaviorally equivalent.

Built-in classes reuse existing implementations. S6 keeps privilege dropping,
initial-check dependencies and service-disable controls. The traditional worker
and lean worker keep their existing scale-to-zero behavior, and language inference
still uses its temporary process. Defaults and the explicit migration from retired
flags are in [configuration](configuration.md#service-implementations).

Verification: **117 tests passed** across service selection, S6 shell execution and
system checks, scheduler, lean-worker bootstrap/outcomes, task serialization/trace,
import boundaries and temporary-process language detection. Tests ran as non-root
in disposable `docker run --rm --userns=host` containers using Python 3.14.10,
Django 5.2.17 and frozen uv dependencies. Ruff format/check and diff checks passed.
The launcher/interface import no Django modules; Celery's existing lean bootstrap
imports Django version utility modules but never Django settings/apps in its
parent. Full image deployment and live custom implementations were not exercised.

## Integrated review and verification

The final review traced the combined parser, update, ingestion, search and service
boundaries. Fixes keep workflow attachments current after filename changes,
propagate ambiguous email failures into durable attention state, preserve
setup-time plugin stops, check preparation timeouts before publication, and expose
the started signal's durable invocation identity. Search now scopes a similarity
seed before calling any selected provider. Test fixtures explicitly own their
configuration and document identities instead of depending on execution order.

The final combined SQLite selection passed **2,230 tests**, with **12 skipped**,
covering parser contracts, document/search APIs, workflows, file handling,
ingestion/reprocessing, imports, settings, services and workers. The migration
check reported no model changes. Earlier issue-specific PostgreSQL 18 runs passed
84 lifecycle/job tests and 97 search/update tests, including the database-side FTS
assessment. All tests used non-root disposable Docker containers with frozen uv
dependencies. Changed-file hooks and formatting were checked before the final
commit; the temporary test volume was removed afterward.

The full suite, MariaDB and live deployments were not tested. Post-commit ingestion
workflow/signal timing and explicit configuration migrations remain the documented
compatibility changes. Unknown external effects still require reconciliation;
the coordinator does not infer that a failed action is safe to repeat.

## Ingestion ownership and deferred operations (#38–#40)

Ingestion task adapters call the application coordinator. Shared input decoding,
state transitions and publication validation belong to `ingestion_state`;
retained files and publication reconciliation belong to `ingestion_storage`.
Shared finite invocation outcomes live in `operation_journal`, below parser,
metadata, search and ingestion orchestration. `job_state` and `job_storage` own
common authorization, revisions, waiting state and atomic file operations. Plugin selection lives in `plugins.pipeline`, alongside the
processing boundary. These modules do not import Celery task plumbing to select
plugins or report progress. Consumer compatibility, plugin order, terminal stop
outcomes, hooks, version publication and cleanup retain their existing owners.

### Completion inventory

The completion boundary is a finite operation, not a worker process. The same
`DocumentJob` invocation records identify local and remote executions.

| Override boundary | Finite work and completion expectation | Lifetime or interactive interfaces |
| --- | --- | --- |
| Parser provider | `parser.process` returns validated text, date, page count and private file references. Required ingestion cannot publish before successful parsing. Reprocessing keeps the old document available. | Provider construction and selection are local declarations; no host availability check belongs there. |
| Classification provider | `metadata.classify` returns proposals for the retained content and permitted candidates. Core rechecks identity, permissions and document revision, then applies assignments. Optional enrichment does not hide an imported document. | Suggestions remain synchronous request/response calls; they do not silently wait for a remote workstation. |
| Search provider | `search.write`, `search.rebuild`, `search.optimize`, `search.wipe` are finite maintenance operations. Deferred inputs retain plain facts, selection and configuration. Changed facts, permissions or rebuild membership invalidate acceptance. External index effects with an unknown outcome require reconciliation. | Search, matching IDs, highlights, autocomplete, health and rebuild-needed queries remain synchronous. `open`/`close` manage process resources. |
| Ingestion extensions | Preparation and cleanup have distinct invocation IDs and outcomes. Plugin outputs must satisfy the existing metadata/file contract. Hooks, workflows and extension signals retain conservative effect handling. Core publication and assignment remain core-owned. | Plugin construction/setup describes one processing attempt; it is not a long-lived service. |
| Service implementations | Watchers submit durable consumption requests; schedulers dispatch existing named operations; workers deliver job IDs. Finite application work uses the operation above, independent of the selected host. | `Service.run()` is a foreground lifetime contract and cannot return a deferred task receipt. Interactive service APIs return admission/status responses. |

Built-in classifier training/activation is a separate implementation operation,
not an interchangeable classification inference method. Likewise, an arbitrary
Celery task does not become a provider contract merely because a replaceable
worker delivers it. No new generic workflow or plugin registry is introduced.

### Durable execution and processor API

For current installation, provider replacement, backup, and rollback procedures,
use the [provider operations runbook](provider-operations.md). The sections below
retain the architecture and migration history.

Finite provider work uses one durable queued lifecycle: the coordinator records an
invocation and releases its worker; an eligible processor claims it, computes a
result, and submits it for core validation. This applies to parsing, auxiliary
thumbnails, classification, search maintenance and ingestion plugin preparation.
Server processors use local files without an HTTP round trip; workstation
processors use the authenticated API below. Placement is a deployment and
processor eligibility decision.

Interactive queries and suggestions, provider construction, plugin cleanup,
publication and coordinator bookkeeping retain their existing owners.
Pre-consume scripts, consumption-started signals, completion workflows,
completion signals and post-consume scripts remain core-owned journaled hooks.
They cannot be claimed as generic processor operations.

Create a regular service account in Users & Groups and grant its group the
**Process** permission for the required processor capability:

| Permission row | API permission |
| --- | --- |
| ParserProcessor | `process_parser` |
| MetadataProcessor | `process_metadata` |
| SearchProcessor | `process_search` |
| IngestionProcessor | `process_ingestion` |

These are `documents` permissions; they do not grant the service account document
editing rights. A parser grant covers parser work, not metadata or search work.
Superusers retain administrative access. Use the account's normal API token on
the workstation: list `/api/operations/`, claim a job/key at `claim/`, fetch only
its declared inputs from `files/` with `X-Operation-Token`, upload private outputs
to `files/` with that token, then POST `token` and `result` to `result/`. The
processor uses the returned invocation identity, configuration and inputs with
its synchronous provider implementation. No new processor client is included.

For a supervised server deployment, follow [Supervised OCR and metadata
processing](processor-deployment.md). The shipped opt-in service starts finite
processor sweeps automatically, provisions a dedicated capability account through
`document_processor_account`, and releases Django/model memory between sweeps.
Select `PAPERLESS_PROCESSOR_USER` and the explicit whitespace-separated
`PAPERLESS_PROCESSOR_CAPABILITIES`; unselected work remains waiting for another
eligible processor without local fallback.

Inspect pending work and account grants without claiming it:

```sh
python manage.py document_processor --user server-processor --configured \
  --capability parser --capability metadata --capability search \
  --capability ingestion --diagnose
```

Remove `--diagnose` to consume one snapshot manually. Exact `--operation` and
`--provider` filters remain supported. Provider/configuration checks are owned
by the core runners, not inferred from an operator-copied identity.

`document_thumbnails` queues thumbnail-only jobs, preserving document content
and archives. Classifier-backed `document_retagger` queues metadata jobs with its
selected fields, overwrite policy and matching-rule snapshot, and reports the
queued count. Follow completion in Tasks. Its interactive `--suggest` mode and
rule-only processing without an active classifier retain their existing behavior.

Search index changes are asynchronous: document edits become searchable after
the queued index operation completes. Management commands that perform search
maintenance now require access to the migrated application database.

Policies are retained with each accepted invocation. Provider identity, effective
configuration, input references, requester, successful dependencies, attempt token
and outcome survive broker, application and worker restarts. Waiting has no age
expiry and consumes no execution attempts. A claim starts a bounded lease using
the recorded execution timeout; a stale token cannot submit a result. Pure work
can receive a new claim after expiry. Effectful work with uncertain completion
moves to attention instead of automatic replay. Actual local and remote attempts
are bounded to three; an offline queue does not consume that budget.

The authenticated `/api/operations/` API admits `ocr`, `metadata` and
`ocr_metadata` using a stable UUID `request_id` and a `document_id`. OCR followed
by metadata retains the second-stage intent and document revision in the OCR
application transaction. After committing OCR, completion admits a deterministic
metadata job; a broker outage or metadata-admission failure cannot lose that
intent or rerun OCR. Admission failures retain the committed job and explain the
needed repair before resuming. Current documents remain readable throughout optional
enrichment; conflicting edits are preserved and the stale proposal is rejected.

Processors use the ordinary API authentication plus the operation's retained
processor capability. Operation listing exposes status; claim, input-file retrieval,
output-file upload and result submission require the matching operation and
claim token. Output files are staged privately; processors never receive an API
that writes authoritative document fields. Result submission only records a
proposal. The resumed core validator checks the domain result before publication.
The requester or an administrator can cancel an unfinished job; late claims and
results cannot revive cancellation. Retained uncertain effects are not represented
as undone. Administrators can submit a verified outcome and required evidence to
`/api/operations/{job}/{key}/reconcile/`; this does not replay the external action.
Reconciliation of a cancelled operation records evidence without applying it.

Remote ingestion plugins cannot rewrite the retained requester, owner, grants,
remote-processing permission, or relation/custom-field assignments. Such changes
require a core authorization/candidate contract, rather than a generic plugin
receipt. Parser deferral also respects the original remote-processing policy.
Classification proposals preserve existing scalar assignments. Configured rich
metadata proposals follow the explicit [replacement and preservation contract](rich-metadata-contract.md).
Both reject concurrent relevant edits; committed changes reconcile filenames
before refreshing search and caches.

A processor must use the retained request and configuration rather than infer
new settings from the current deployment. Reusing an accepted submission or an
identical result is idempotent. Changing an implementation, model, configuration,
source revision or permission while work is pending produces a visible conflict;
submit fresh work after reviewing the change. Keep the database and private
`DATA_DIR/jobs` storage together across restarts and backups.

### Migration from operation/user-ID configuration

The operation-name/user-ID routing setting `PAPERLESS_DEFERRED_OPERATIONS` is
obsolete. Remove it from deployment configuration before starting the upgraded
application. Remove provider `execution_mode` declarations as well; these no
longer select execution placement.

Before upgrading, stop admission of new work and drain pending jobs with the old
version. Preserve the database and private job storage together. Do not assume
that stopping a processor means its external effects did not happen: leased or
uncertain operations must be reconciled with evidence, or explicitly cancelled
before fresh work is admitted. Cancellation does not undo external effects.
The migration preserves completed and cancelled jobs and marks other legacy jobs
as needing attention. Review retained outcomes, reconcile uncertain effects, then
cancel and submit fresh work as appropriate; it does not silently route legacy
invocations to a new processor.

Removing a Process permission or group membership takes effect on the next
claim, file transfer, or result submission, including an identical retry of an
already submitted result. Account deactivation also blocks processor access.
Revocation does not erase a lease or undo a previously accepted result. A revoked
processor cannot use its token; pure work can be reclaimed by an authorized
processor after expiry, while uncertain effects require reconciliation.
Cancellation and stale-token checks still apply after permissions are restored.
Requester document permissions and remote-processing restrictions are checked
independently of processor access.

### Provider declarations

Parser orchestration validates public `configuration_snapshot`, optional
context-aware `configuration_for`, and `selection` declarations. An internally
selected engine identifies itself through the same declaration; core does not
recognize its Python module to infer replay safety or remote access. Missing
replay declarations are conservative. Legacy parser construction, engine dispatch
and mail configuration overrides live inside the built-in implementation.
Auxiliary thumbnail generation uses its own queued invocation and retains its
file checks.

Classification providers expose an implementation/model identity and optional
JSON configuration. `current_identity` can report current activation without
loading a model; otherwise core compares a newly selected provider. The built-in
classifier and legacy classifier instances obey this same contract. Search cache
selection uses provider-declared configuration; language vocabulary validation
belongs to the provider that uses it.

Deferred full-index rebuilds currently materialize their record snapshot in one
job; incremental writes avoid that memory cost. Live workstation deployment and
external-provider effects need integration testing in the target environment.

### Review and verification for #38–#40

The independent final review added requester-aware listing/cancellation, bounded
actual attempts, terminal validation conflicts, immutable atomic uploads, model
activation checks, metadata file reconciliation, and protection against remote
plugin privilege changes. Auxiliary thumbnail attempts now have their own durable
outcome, so a successful main parse cannot authorize replay of an uncertain
thumbnail side effect. Search provider replacement closes the prior implementation
before loading the next one and does not reconstruct a cached provider on every
lookup.

The broad integration selection passed 1,259 tests with one PostgreSQL-only skip.
Four failures identified a provider-lifetime regression and three container setup
issues; these were corrected and checked in targeted reruns. The final
unprivileged integration selection passed 258 tests with one PostgreSQL-only skip,
covering ingestion, parsing, deferred execution, search, file permissions and
startup scripts. The final cache/search/reviewer selection passed another 24 tests
with one PostgreSQL-only skip. Earlier metadata/file/workflow coverage passed 281 tests plus two
subtests, with its root-sensitive permission check passing separately as a
non-root user. Migration consistency and changed-file Ruff checks passed. Tests
ran only in disposable `docker run --rm --userns=host` containers; no test volumes
were retained.

The full suite, PostgreSQL execution, production deployment and live workstation
integration were not run for these changes. Offline waiting and recovery were
simulated with persisted jobs, expired claims, repeat deliveries and stale results.

### Queued lifecycle verification (#42)

The expanded backend selection passed 455 tests with one skip; its remaining
workflow-index test passed after its fixture was updated to drive the real queued
index operation. Frontend checks passed 64 tests and TypeScript compilation.
Affected runtime Ruff checks, formatting and migration consistency checks passed.
Tests ran in disposable Docker containers with no retained test volumes.

The full repository suite, production performance and live workstation integration
were not run. Deployments must arrange eligible processor execution; unfinished
legacy jobs require review rather than automatic replay.

The follow-up correctness and simplicity review assumed concurrent server workers
sharing job storage. It fixed processor sweeps aborting on lock contention,
validation conflicts being reset to waiting, and expired claims left in queued
jobs after a server crash. Tasks now fetch operation journals once per page.
Thumbnail-only jobs preserve the modified timestamp and skip unrelated search/AI
indexing. The review regression selection passed 106 tests in disposable Docker;
affected-file lint and formatting also passed.
