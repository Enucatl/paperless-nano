# Rich metadata proposal contract (version 1)

This contract implements [#44](https://github.com/Enucatl/paperless-ngx/issues/44)
for release tracker #43. The executable boundary is
`src/documents/enrichment.py`. It is separate from `ClassificationRequest` and
`ClassificationResult`, whose permitted-ID assignment behavior remains unchanged.
Integration into durable enrichment jobs is #45; inference adapters are #46.

## Ownership and wire format

Providers compute proposals from plain JSON. They receive neither ORM objects,
Paperless credentials nor permission to patch documents, create entities, publish
files, or dispatch follow-up work. Core admits, validates, creates authorized
entities, persists, publishes, and recovers through the existing durable jobs and
operation journal. No extra stage queue or listener is required.

The maintained sanitized [invoice fixture](../src/documents/tests/fixtures/enrichment/invoice.json)
is a complete request/result example with an existing correspondent, new language,
all required metadata roles, one additional typed field, and unrelated values to
preserve. IDs, text, endpoint and identities are synthetic. Tests load this same
fixture, so documentation and wire checks share an example.

A request retains `version: 1`, the opaque document/content `revision`, `content`,
existing `metadata`, authorized `candidates` (`correspondents` and `tags`, each a
list of `{id, name}`), `managed_fields`, `configuration`, `identity`, and the UTC
admission `date` (`YYYY-MM-DD`). `document_date_max` separately retains the local
admission date used for inferred document-date bounds. Existing metadata includes title/date,
correspondent, tags and custom-field values, plus the root/document version and
owner needed by core. Candidate lists expose only requester-authorized values;
providers must never fetch a global list themselves.

`identity` contains nonempty immutable `provider`, `model` and `prompt` strings.
Implementation versions, model artifact revisions and prompt hashes belong here;
a mutable model alias alone is insufficient. `configuration` captures effective
thresholds, endpoint identity, generation parameters, remote-policy selection and
field mapping without API keys, tokens, URL credentials or signed query strings.
Core constructs and retains this snapshot, and compares it again at publication.

`managed_fields` maps role names to `{id, data_type}`. The reserved roles are
`summary` (string/longtext), `processing_date` (date), and `audit`
(string/longtext containing serialized JSON). Additional explicitly authorized
roles may use string, longtext, date, boolean, integer or float. Version 1 does not
propose URL, monetary, select or document-link values: these are outside the
existing metadata runner's behavior and require their own validation contract.
No arbitrary custom-field ID or field creation is permitted.

A result repeats `version`, `revision` and `identity`. Every other key is optional:

| Key | Meaning |
| --- | --- |
| `title` | Replacement string, 1–128 characters; adapter truncates before submission. |
| `document_date` | Replacement ISO date, from 1900-01-01 through retained `document_date_max`. |
| `summary` | Replacement string in the managed summary field; empty string clears. |
| `processing_date` | Retained request date in the managed processing-date field. |
| `audit` | JSON object, serialized by core into the managed audit field. |
| `correspondent` | Exactly `{id: permitted_id}` or `{create: observed_name}`. |
| `language` | Exactly `{id: permitted_language_tag_id}` or `{create: "language:en"}`. |
| `custom_fields` | List of `{field: id, data_type: retained_type, value: typed_value}` for additional managed roles. |

Omission always preserves. Null is rejected, including for relations and dates;
it never implicitly clears an existing value. Empty title is invalid. Empty
summary/string custom values explicitly replace with blank; an empty
`custom_fields` list changes nothing. The three reserved roles cannot also appear
in `custom_fields`; duplicate field IDs are rejected. Supplied scalar values
replace, never append. A language proposal replaces managed `language:` tags
with one selected tag, preserving unrelated tags and fields. Stage-selection tags
from the old runner are not part of this new orchestration. No result can change
owner, permissions, content, document type, storage path or arbitrary tags.

Creation is a proposal, not authority. Core rechecks document edit access,
existing relation access, field types/mapping and add permissions at commit.
It resolves concurrent same-name creations within its transaction and records the
actual chosen ID in its audit; a provider's diagnostic ID is not authoritative.
Core must preserve inaccessible/unrelated tags, not infer deletion authority from
the candidate list alone. Language syntax is `language:` followed by 2–3 lowercase
letters; the adapter's supported-language vocabulary supplies semantic validation.

## Baseline and explicit differences

Inspected source: `/export/atlas/paperless-ai`, commit
`690691928cd8953529610c342748324324d06df7`, especially `ai/src/paperless_ai/core/runner.py`
(`run_metadata_batch`, `run_ocr_batch`), `correspondent/resolver.py`,
`agents/smart_graph_agent.py`, `agents/base.py`, and `agents/paddle_ocr.py`.
The fixture represents the observed behavior, not a captured private document.

| Case | Existing runner behavior | Provider/core behavior required |
| --- | --- | --- |
| Long or absent title | Truthy title sliced to 128; absent/empty preserves. | Adapter slices; omits absent/empty. Boundary rejects unsliced values. |
| Date bounds | Only 1900-01-01 through local `date.today()` is written. | Same bounds against retained local admission date; out-of-range inference omitted. Retention makes retry stable. |
| Empty content | Whitespace-only content skips inference and removes queue entry. | Successful no-op, no processing date or audit mutation. |
| Blank summary | `(summary or "").strip()` is written even when blank. | Adapter preserves normalization; blank explicitly clears managed summary. |
| Correspondent exact/fuzzy match | NFKD/accent/punctuation/corporate-noise normalization; max(character, token-sort, adjusted containment) score; reuse at threshold (default 0.80). | Adapter retains resolver algorithm on authorized candidates; core rechecks proposed ID. |
| Ambiguous correspondent | Highest score wins; equal scores sort by ascending ID then name; second-best score is audit-only, no ambiguity veto. | Preserve deterministic ordering and audit diagnostics. |
| No correspondent match | Below threshold, no valid candidates, or noise-only observed name requests creation; blank observed name preserves. | Explicit creation proposal; core owns permission check and creation. |
| Correspondent lookup failure | Warning; remaining metadata still patches, correspondent omitted. | Pure resolver on retained candidates removes network lookup; inference/resolution failure may omit correspondent and record diagnostics. A submitted unauthorized creation rejects the whole proposal. |
| Language list | Inference model chooses first valid normalized code, otherwise `und`; runner takes first code or `und`. | Adapter preserves normalization/vocabulary and proposes one language tag. |
| Language replacement | All known `language:` tags replaced; new tag has matching algorithm 0 and color `#166534`. | Core replaces authorized managed language tags, preserves unrelated/inaccessible tags, owns creation defaults. |
| Processing date | UTC execution day, updated on retries. | Retained UTC admission day; retry must not change an already retained result. |
| Metadata audit | JSON timestamp/model/endpoint/Paperless version/raw extracted metadata and optional resolution diagnostics. | Preserve available diagnostic content, remove credentials, include immutable identities; core records actual creation/application outcome. |
| Unrelated fields/tags | Re-read before PATCH; retain fields outside three managed roles and tags outside language/stage tags. | Transactionally preserve unrelated values; concurrent relevant changes reject stale proposals. |
| OCR | Reject blank transcript; publish searchable archive when layout model is not `none`, text, and `ai_ocr_output` JSON; preserve unrelated fields. | Parser proposes private archive/text/provenance; core publishes together, with source checksum validation and recovery. |

Intentional safety differences are core-owned authorization/creation, secret-free
snapshots, stale-result rejection, and atomic application instead of independent
API writes (which could leave an orphan correspondent or a separately published
archive). These are release requirements from #43/#44, not a reduction to
classification. Admission-date retention is explicitly chosen for deterministic retry;
release validation must surface this operational difference rather than silently
claim exact timestamp parity.

OCR provenance belongs to the parser result, not the metadata provider. The
adapter/core integration may add an optional `provenance` JSON object to
`ParserResult`: preserve baseline `schema_version`, `pipeline`, `model`,
`layout_model`, `source_sha256`, `timestamp`, `page_count`, `elapsed_seconds`,
`dataInfo` and `pages`. Core validates its source identity and publishes it to the
configured `ai_ocr_output` longtext field along with the archive/content commit.
A metadata result cannot overwrite OCR provenance or publish permanent paths.
This document reserves that integration; #46 must implement and test it.

## Validation, lifecycle and compatibility

`validate_result` rejects malformed/version-incompatible results, unknown keys,
nonfinite/non-JSON values, incompatible identity/revision, forbidden IDs, unsafe
relation shapes, unsupported field types, duplicate field updates and changes
for empty input. It does not replace database authorization or field validators.
Core also checks current permissions, managed-field configuration/types, document
and effective-content revision, retained candidate IDs/names, current
provider/model/prompt and configuration inside the apply transaction. Changes to
these invalidate pending work; do not silently reinterpret or migrate the old
proposal. Newly added candidates alone do not invalidate the snapshot. Core may
reuse an authorized same-name entity created concurrently, but does not rerun
fuzzy matching on the new candidate set at application time. Renamed, deleted or
newly inaccessible retained candidates invalidate the proposal.

A valid result commits all its proposed metadata/entity changes atomically.
Malformed or unauthorized partial results do not partly apply. An adapter may
intentionally omit unavailable optional extraction fields; that omission remains
visible in audit diagnostics. Retained successful computation is replayed through
validation, not recomputed after a publication failure. Retries use the existing
operation identity, journal and recovery owner; cancellation before commit prevents
application, while cancellation after commit cannot undo already applied state.
Post-commit indexing/notifications recover through the existing completion path.
Inference errors produce no document mutations and follow durable retry policy.

Version 1 is strict. Unknown fields and versions are rejected; incompatible
meaning, new writable fields or a changed type require a new negotiated contract
version and tests. Updating provider code, model, prompt, endpoint or effective
configuration invalidates pending work even if the wire version stays 1.
Classification providers continue their existing contract independently.

## Core integration and recovery

Set `PAPERLESS_ENRICHMENT_PROVIDER` to the installed rich provider class. This
selects rich metadata for new ingestion and explicit metadata/combined OCR jobs;
`PAPERLESS_CLASSIFICATION_PROVIDER` and queued retagging retain their separate
classification contract. With no rich provider selected, the existing
classification enrichment remains available. Rich providers declare
`uses_remote_service` and `replay_safe`; omitted declarations are conservative
(remote and not replay-safe). Remote inference additionally requires
`PAPERLESS_ENRICHMENT_ALLOW_REMOTE=true`. The declaration and policy are retained
with the provider configuration and rechecked before computation/application.

`PAPERLESS_ENRICHMENT_FIELDS` is a JSON object mapping roles to existing custom
field IDs, for example `{"summary": 4, "processing_date": 5, "audit": 6}`.
Use longtext for summary/audit and date for processing date. IDs must be distinct;
additional roles are opt-in, and the OCR provenance field cannot be a rich
metadata target. Core never creates custom-field definitions. An authorized
document editor may change the configured field values, as with ordinary document
metadata; schema creation/change remains a separate permission.

Core rechecks requester edit access and processor capability at application,
including after a restart that retained an already accepted proposal. New
correspondents and language tags require the requester's respective add
permission; their owner is the requester, or the document owner for trusted
actorless jobs. Exact same-name/owner creations use the existing database unique
constraints, including the partial constraint for ownerless entities. A hidden
same-name entity belonging to another owner is never reused. Created language
tags retain the baseline color `#166534` and matching algorithm `0`. Audit output
records the actual applied relation IDs under `application`.

Metadata, relation creation and the committed checkpoint share one database
transaction. File moves are deferred until after that commit. A process exit
before commit rolls everything back; the retained accepted result can be
validated and applied again. After commit, retry resumes completion without
reapplying metadata or recreating entities. Empty-content no-op results do not
run update workflows or notifications. A successful OCR parent and its readable
archive remain intact if the separate metadata child rejects or fails.

The existing document-update workflow/notification path runs as one journaled,
non-replay-safe follow-up. If it fails or is interrupted, Tasks reports that
metadata committed and its follow-up effects need review. Inspect the job with
`document_jobs --job UUID`, inspect/reconcile the workflow and notification
outcomes, then run `document_jobs --job UUID --resume --acknowledge-effects`.
Acknowledgment skips those reviewed effects and dispatches remaining file/search
completion; it does not undo metadata or replay workflows. File/search failures
after a completed follow-up retain the committed checkpoint and can resume
normally. Search admission is still queued work: completion of the enrichment
job does not itself prove that a separately queued search update has finished.

Language replacement respects the existing tag hierarchy: if an unrelated or
inaccessible retained tag needs an old language tag as an ancestor, that ancestor
is preserved alongside the newly selected language. This intentional exception
prevents removal of unrelated descendants; flatten that hierarchy explicitly if
exactly one attached language tag is required.
