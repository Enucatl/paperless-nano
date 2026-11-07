# Paperless Nano: Atlas and Cerebrum migration

Production migration: 2026-11-07. Atlas runs the generic Nano server; Cerebrum
runs the independent worker during weekly windows against its existing PaddleOCR services. The original
Compose project, PostgreSQL 18, Redis 8 and document volumes are retained.

## Initial deployed versions

- Published core base:
  `ghcr.io/enucatl/paperless-nano@sha256:3a0979b6e45110841590599c64c7134ae4becffa28dd9252eb44bbd1211e0860`.
- Initial running core:
  `sha256:73ed9c0b20eb9d07882a87710a1cfca2896ff5762efc2bc011037ce077d1449a`
  (`paperless-nano:20261107-thumbnail-fix`), a locally built layer over that base.
- Worker:
  `ghcr.io/enucatl/paperless-nano-worker@sha256:0f54a54c5e158476f47c9b828c5fd66b0535fafa95659fe90627cca5e920beef`.
- Both published base images use tag `v3.4.2-nano-97bc71617`.

The live check found that the published core adds its own `thumbnail_provider`
annotation before strict external-result validation, which rejects the annotation.
The local layer fixes this ordering and preserves the raw worker proposal during
cached retries. Strict worker-result validation remains unchanged. All 61 focused
regression tests passed in disposable PostgreSQL/Redis containers (19.28 seconds).
The fix and regression tests are maintained in this repository. The initial
rollout used this local layer before release publication. Both hosts retain its build recipe, source patch,
image archive and verified checksums in their private backup `hotfix/` directories.

## Configuration changes

Atlas (`atlas.home.arpa:/opt/docker/paperless-ai`, mounted at
`/export/atlas/paperless-ai`) selects the generic external parser and metadata
contracts, `server-processor` with only `search ingestion`, and OCR provenance
field 5. The application title is now `Paperless Nano`.

The old service flags were replaced by `PAPERLESS_CONSUMER_CLASS`,
`PAPERLESS_SCHEDULER_CLASS` and `PAPERLESS_WORKER_CLASS`. The legacy AI and webhook
services are stopped with restart disabled and retained under the `legacy`
Compose profile. Their workflow triggers are disabled. Do not start the old
writeback runner alongside the durable operation pipeline.

Cerebrum (`cerebrum.home.arpa:/opt/docker/vllm`, mounted at
`/export/cerebrum/vllm`) runs `paperless-workstation/` with a private persistent
state volume and dedicated `parser metadata` account/token. Model credentials
stay on Cerebrum. OCR uses port 8108 with verified PaddleOCR-VL-1.6 model identities;
metadata uses `inception/mercury-2.5` through OpenRouter. The worker mounts the host
CA bundle with certificate verification enabled.

The production URL is `https://paperless.atlas.home.arpa`. Its operation API has a
separate Traefik route using Paperless token/capability authentication, without
interactive Authelia. That route strips remote-user headers. The main page and
other API paths retain SSO. Unauthenticated and invalid-token requests are rejected.

Existing managed-field IDs and values were retained: field 1 was renamed from
`ai_processed` to `processing_date`, field 2 from `ai_result` to `audit`, and field
4 from `ai_summary` to `summary`. Field 5 remains `ai_ocr_output`.

Normal consume-folder and mail ingestion are restored. The consumption API watcher
and scheduler are running, mail rule 1 is enabled, and legacy workflows 1 and 2
remain disabled. Core is healthy and the worker's authenticated discovery succeeds.
Final configuration snapshots are under each backup's private `post-cutover/`
directory; recorded checksums passed, including the off-host Atlas copy. Post-ready
Loki checks from 18:16:19 to 18:20 UTC found no new error/failed/traceback entries
for the server and workstation services.

## Backup and verification

The coordinated stopped-writer rollback set is private at:

- Atlas: `/opt/docker/paperless-ai/tmp/nano-migration-20261107`.
- Cerebrum: `/opt/docker/vllm/paperless-workstation/backups/nano-migration-20261107`.

It includes PostgreSQL, globals, complete data/media/export/consume archive,
Redis snapshot, configuration/secrets and old image identities. The Cerebrum copy
passed checksum verification. An isolated restore and actual candidate migration
preserved 2,355 active documents, complete document metadata, custom fields/values,
tags, task history and 4,396 source/archive SHA-256 hashes. Four trashed documents
were excluded by the default manager; all their files are included in the archive.
Both old and candidate images passed Django checks. Temporary verification
containers, volumes and network were removed.

Synthetic document 2639 passed fresh consumption and existing-document OCR plus
metadata reprocessing to terminal success. Source/provenance hashes, managed
metadata, unrelated marker-tag preservation, ordinary search, searchable archive
text and actual veraPDF PDF/A-2u validation passed. Private evidence is under
`smoke/` on both hosts. After restarting both services and waiting 46 seconds idle,
fresh document 2640 also passed terminal OCR/metadata/search, source integrity,
archive text and veraPDF PDF/A-2u validation. Its opaque test reference was
recognized as Markdown math; verification normalized non-alphanumeric formatting
and ordinary search used the stable word `Verification`. Raw OCR is retained in
`smoke-after-restart/`. Historical qualification measurements retain their original
images and broker; this migration does not replace those measurements.

## Retained historical state and backout

The old installation had 51 unfinished task-history entries with an empty Celery
queue. Forty-five consumption receipts have filename-based completion evidence,
which cannot establish identical content for ambiguous filenames. Six mail-fetch
outcomes remain unknown. Those history records are retained, not blindly replayed
or marked complete. Four legacy OCR-tagged documents (2607, 2608, 2627, 2628) are
trashed and remain trashed. Active legacy failed document 2623 completed a fresh
OCR/metadata/search job successfully with its source hash and unrelated metadata
preserved; its failed Redis queue entry was removed only after success. All five
source PDFs match stored checksums. Private evidence is `historical-review.json.log`
and `legacy-retry/`.

For backout, stop writers and restore the matching old database/files/config and
old image together. Never start the old image against the upgraded database.
Account for documents and edits created after the backup before restoring it.
See the [rollback runbook](provider-operations.md) and the live deployment's
`docs/nano-deployment.md`. The example external worker accepts PDFs only.

## Weekly-only worker lifecycle

The normal worker is intentionally offline between weekly windows. Cerebrum's
extraction and worker containers have `restart: "no"`; boot alone starts neither.
`vllm-weekly-window.timer` on Proxmox runs Tuesdays at 01:05 UTC. Its workflow
waits for model health, runs serial temporary `workstation --once` containers
for the configured hour and waits for the current pass before cleanup. It stops
only services it started and preserves infrastructure if worker completion is
uncertain. Pending operations remain on Atlas until the next window.

The tested script was installed on Proxmox and in the currently served Puppet
production module. Ten focused tests and a real zero-window startup/worker/cleanup
cycle passed. The weekly implementation and regression tests are maintained in
the Puppet control repository alongside its production deployment.
