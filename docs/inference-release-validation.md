# OCR and rich metadata release validation

Validation date: 2026-11-05. Tracker: [#43](https://github.com/Enucatl/paperless-ngx/issues/43);
release gate: [#49](https://github.com/Enucatl/paperless-ngx/issues/49).

**Release publication is withheld.** The implementation and local checks below do
not establish live OCR/metadata parity, a verified production upgrade/backout, or
whole-workload memory behavior. Keep the existing deployment available.

## Implemented scope and compatibility

| Issue | Result | Commit |
| --- | --- | --- |
| #44 | Versioned proposal contract, behavior matrix and sanitized fixtures | `0edd18808` |
| #45 | Authorized atomic metadata application and durable recovery | `e4b57989f` |
| #46 | Installed OCR/metadata adapters and core OCR provenance publication | `3b40cb323` |
| #47 | Supervised processors, capability provisioning, Tasks diagnostics and PostgreSQL example | `72597e364` |
| #48 | Installation, provider swap and rollback runbook | `e6306a8a2` |
| #49 | Runtime validation and queued execution fixes; publication gates remain open | `f3c5aeb2c` |

The supported candidate pairs this core branch with `paperless-inference` 0.1.0,
Python 3.14 and PostgreSQL 18. Both wire contracts are version 1. The OCR adapter
accepts PDF input. Read [provider operations](provider-operations.md),
[processor deployment](processor-deployment.md), and the
[metadata contract](rich-metadata-contract.md) before installation. No database
migration was added by these changes. Provider rollback is not a database downgrade.

## Verification evidence

All tests ran in disposable Docker containers with `--rm --userns=host`;
dependencies lived in temporary Docker volumes, and test source was copied into
container storage. Counts below overlap and must not be added into one total.

| Check | Result |
| --- | --- |
| Pure rich-metadata contract | 42 passed |
| Core application on PostgreSQL 18 | 53 passed, including concurrent creation and recovery |
| Installed provider/baseline archive suite | 93 passed, including real Ghostscript and veraPDF archive validation |
| Core parser/provenance suite | 85 passed; later provenance additions have focused coverage |
| Processor/service/backend suite | 124 passed |
| Tasks frontend suite | 51 passed |
| Integrated core/service regression selection | 160 passed, 1 PostgreSQL-only skip on SQLite |
| PostgreSQL worker registration, document updates, rich metadata and OCR provenance | 62 passed |
| PostgreSQL processor cursor and worker-registration regressions | 19 passed |
| Final core correctness pass | 115 passed on PostgreSQL; 114 passed and 1 PostgreSQL-only skip on SQLite |
| Final provider payload/default checks | 12 passed |
| Core/provider dependency compatibility | All shared locked core versions unchanged; `uv pip check` passed |
| Installation/runbook smoke checks | Fresh SQLite migration/provisioning, PostgreSQL dump/restore and data/media archive roundtrip passed |
| Production frontend compilation | Passed as part of the core image build |
| Migration drift | `manage.py makemigrations --check --dry-run`: no changes detected |

The six pre-existing document-update search failures reproduced before this
release work. Those tests now use the existing `queued_search_processor` fixture,
which claims and completes real durable search operations before asserting real
index results. This does not restore synchronous indexing in production.

An actual fresh stack exposed a missing Celery registration: the worker discarded
`documents.search_operations.run_search` as an unknown task. Rich enrichment was
also absent from the worker registry. `CELERY_IMPORTS` now explicitly loads both
coordinator modules. The existing fresh-process worker test asserts their presence
in the real metadata registry and checks parent/child task parity.

The PostgreSQL deployment also exposed a cursor-lifetime bug: publishing a task
can close an obsolete database connection, invalidating the processor's open
server-side cursor after its first operation. A sweep now snapshots only job IDs,
then reloads each still-waiting job before processing. The PostgreSQL regression
closes the connection during the first operation and verifies that the second job
still runs; large OCR result payloads are not all retained in the sweep snapshot.

## Final correctness, consistency and simplification pass

Independent reviewers traced the combined core, provider, deployment and operator
paths. The final review commit fixes three further issues:

- Managed metadata updates now write only proposed custom fields. Previously,
  rewriting unrelated document-link fields could modify linked documents and
  reconcile their files inside the metadata transaction.
- OCR commits before admitting its deterministic metadata child. A missing or
  changed managed field now leaves successful OCR readable and recoverable,
  instead of rolling it back. Intervening document edits block stale admission;
  resuming the committed job does not repeat OCR.
- Metadata inference again defaults to the baseline's `reasoning_effort=minimal`,
  while preserving explicit overrides and an empty opt-out. Transport tests check
  the actual outgoing setting.

The pass also removed 16 lines of unused parser registry attributes/methods from
the explicitly selected provider and aligned the recovery and provider documents
with the final behavior. Relevant Ruff formatting/lint and whitespace checks
passed. No unresolved correctness finding remained in those reviewed paths;
this is not a claim that all upstream code or live deployment gates were checked.

Each issue had a fresh implementation agent and a separate commit. Existing user
changes in the two CI workflows and `docs/memory-analysis.md` were preserved and
excluded from these commits. Temporary test environments were removed after
verification; the local candidate images below remain available for inspection.

## Image and deployment evidence

The exact core Dockerfile first failed on transient GitHub HTTP 504 responses.
The S6 asset succeeded on retry. The pinned `jbig2enc_0.30-1_amd64.deb` download
continued to fail through the browser-download URL. A temporary validation
Dockerfile fetched the same release asset through GitHub's asset API instead;
SHA-256 was checked against the release digest:

```text
993507c43d128393d22d14a8646492336aca6b6327f227d75df8a1009f1f364f
```

No dependency version or repository Dockerfile was changed to bypass the failure.
The core image and its provider extension built successfully. These are local
`:validation` images, not published release artifacts. The build includes the
production frontend. Repeating the unmodified build still depends on the upstream
asset download route being available.

An isolated PostgreSQL 18 + Valkey 9 + Paperless stack reached HTTP 200, completed
migrations and system checks, and provisioned the capability-only account. This
used disposable test data and no production documents. The initial startup found
the task-registration defect above. Rebuilt images include both runtime fixes and
the final core/provider correctness fixes. The resulting stack started successfully,
registered both tasks, and completed a search admitted after an idle period without
manual processor commands. The query returned the synthetic document and all three
retained search jobs were `succeeded`. No unregistered-task or cursor error occurred
in that final run.

Final local image IDs (tested worktree based on `e6306a8a2`, including the fixes in
this report and the final review changes):

```text
paperless-release49-core:validation
sha256:66dd987e04bf062761afeaaf87d427ac673779a1e336ddb55b1332203be61de2
paperless-release49-inference:validation
sha256:4fa068145bb1b01b54d460a4981c82f33418a05f0a1544ae66cd289d2a34a48b
```

## Limited runtime measurements

These measurements cover the shipped default service classes, one autoscaling
worker, the ten-second processor sweep, PostgreSQL and Valkey on Linux x86-64.
They are a small synthetic search smoke workload, **not** OCR/metadata performance
or a comparison with the previous deployment. Periodic sweeps and scheduled tasks
remain active during nominal idle time. The host exposed 15.05 GiB to Docker.

The recreated app reached HTTP 200 after 11.07 seconds. A search admitted after
the idle interval completed in approximately 10.65 seconds, measured from the
first coordinator receipt to its final completion in worker logs. The real index
query confirmed the result. This includes the configured sweep wait.

| Service | Post-startup working-set snapshot | 20+ seconds after search | Cumulative cgroup peak |
| --- | ---: | ---: | ---: |
| Paperless and its supervised processes | 646.4 MiB | 546.5 MiB | 804.42 MiB |
| PostgreSQL 18 | 42.25 MiB | 42.59 MiB | 113.00 MiB |
| Valkey 9 | 5.40 MiB | 5.63 MiB | 14.61 MiB |

Working-set snapshots come from `docker stats --no-stream`; cumulative peaks come
from each container's cgroup-v2 `memory.peak` and include file cache. Peaks are
not simultaneous and must not be summed. PostgreSQL/Valkey peaks include initial
migrations and the earlier validation app restart. These are individual snapshots,
not steady-state averages or a peak isolated to inference. Remote model memory and
successful inference latency were unavailable, not zero. The narrow workload does
not establish the release's whole-stack memory gate or satisfy a target by itself.

## Live endpoint gates

The configured OCR `/metadata` request timed out after ten seconds. A synthetic
invoice sent through the installed metadata provider reached the configured
OpenRouter endpoint but failed with HTTP 401 (0.758 seconds for the diagnostic
attempt). Credentials and response bodies were not printed. Neither outcome is
successful inference or parity evidence.

Consequently the following remain release gates:

- Real new ingestion and existing-document reprocessing through OCR, rich metadata,
  searchable archive/provenance and ordinary search, with representative PDFs.
- Exact behavior parity against live endpoints, including language/correspondent
  cases and preservation of unrelated document data.
- Upgrade from an explicitly retained previous deployment; pending and legacy jobs;
  coordinated database/private-file restoration; live provider swap and backout.
- Full-stack startup idle, active/peak, return-to-idle and latency measurements for
  equivalent successful workloads. Remote model resource costs must be measured
  separately; failed endpoint calls cannot stand in for this workload.
- Publication of an immutable artifact and release notes only after these gates pass.

Earlier foundation issues #28, #29, #37, #40 and #42 remain subject to their runtime
acceptance requirements. Focused parser, operation-boundary, service and worker
checks provide useful coverage; their issue status and older memory measurements
are not release certification. Chat, alternative search and multi-host/NFS
processor safety are outside this release.

## Reproducing the focused checks

Use the locked core and provider environments described in the package README.
For PostgreSQL tests, supply a disposable PostgreSQL 18 connection and install the
runtime `libpq5` library in the test container. Copy the source into container
storage to keep bytecode and test output off the host, then run:

```sh
uv run pytest -o addopts='' \
  src/paperless_worker/tests/test_bootstrap.py \
  src/documents/tests/test_document_updates.py \
  src/documents/tests/test_rich_enrichment_jobs.py \
  src/documents/tests/test_ocr_provenance.py
```

Run the provider suite from `providers/paperless-inference` with its frozen lockfile
and Ghostscript, Java, fonts and veraPDF installed. Run the Tasks frontend suite
and production build in the Node 24 container. A test selection passing is not a
claim that the entire upstream backend/frontend suite was run.
