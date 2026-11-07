# Paperless-nano: Atlas and Cerebrum migration

Readiness inspection: 2026-11-07. The mounted configuration is not yet ready for
production cutover. This checklist supplements the [deployment guide](processor-deployment.md)
and [backup/rollback runbook](provider-operations.md); it does not record a completed migration.

## Resolve before cutover

- Atlas (`atlas.home.arpa:/opt/docker/paperless-ai`, mounted at
  `/export/atlas/paperless-ai`) still selects the legacy Paperless image/configuration
  and separate AI/webhook services. Configure the generic external parser and metadata
  descriptors and server processor account before admitting new documents.
- Cerebrum (`cerebrum.home.arpa:/opt/docker/vllm`, mounted at
  `/export/cerebrum/vllm`) has an untracked `paperless-workstation/` project. Its
  configuration targets the qualification server on Aperture and a local qualification
  image; its referenced token and model credential files are absent. Replace these
  with the production API URL, published worker image and private credentials.
- Wait for successful GitHub Actions publication of both
  `ghcr.io/enucatl/paperless-nano` and `ghcr.io/enucatl/paperless-nano-worker` from the
  integrated `main` revision. Record and deploy immutable digests for both images.
  A workflow definition alone does not establish successful publication.
- Recheck the independently packaged images against an isolated representative
  OCR/metadata workload and upgrade/backout check. The existing
  [20-PDF qualification](inference-release-validation.md) measured the earlier coupled
  deployment; preserve that evidence with its original images and broker.

## Preserve and switch

1. Preserve local deployment changes: Atlas is one commit ahead of its origin;
   Cerebrum's workstation directory is untracked. Review the actual running images,
   outstanding operations and legacy webhook workflows before scheduling the switch.
2. Stop document admission and legacy AI/webhook processing, then drain or explicitly
   review unresolved work. Disable the legacy workflow triggers before enabling new
   ingestion. Keep their configuration available for backout.
3. Back up PostgreSQL, the entire Paperless data volume (including private jobs and
   search data), and media as one stopped-writer set. Record old image digests and
   configuration alongside it and verify an isolated restore. Atlas's current
   `backup.sh` produces document exports; those exports alone are insufficient for
   paired-state rollback.
4. Update Atlas in place, preserving its Compose project and existing database,
   `paperless-ai_data`, `paperless-ai_media`, export and consume storage, plus Traefik,
   authentication, certificates and secret key. Keep PostgreSQL 18 and Redis 8.
   Use the descriptor and environment settings in the deployment guide; do not replace
   this installation with fresh volumes from the example Compose file.
5. Provision dedicated server `search ingestion` and workstation `parser metadata`
   accounts as described in the guide. Create the workstation's private token and
   model credential files with permissions readable by UID 1000. Check custom-field
   names/types and the separate OCR provenance field; preserve existing IDs and
   values when following the [field migration](rich-metadata-contract.md#custom-field-migration).
6. Keep Cerebrum's existing model services and enable its `extraction` profile when
   needed; its OCR gateway is exposed on port 8108. Update the separate workstation
   project from the standalone worker Compose example, point it at the production
   Paperless API, and retain its private `workstation_state` volume across restarts.
7. Follow a fresh PDF and an existing document through terminal OCR, metadata and
   search success. Verify source/archive integrity, searchable text, managed metadata,
   unrelated fields/tags and OCR provenance. Repeat after idle and restart before
   restoring normal admission. A successful migration or accepted API request alone
   is not completion.

If backout is needed, stop writers and restore the matching old database/data/media
set and images using the rollback runbook. Do not start an old image against the
newly migrated database.

Runtime health, production pending jobs, field definitions and backup restorability
must be checked during preparation; this configuration inspection did not verify them.
