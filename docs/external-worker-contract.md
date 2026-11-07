# External worker HTTP contract

Paperless describes immutable OCR and metadata work. An authorized worker claims
an operation, reads its inputs, computes a proposal and returns it. Core validates
the proposal against the retained request and current permissions, then publishes
it. Workers own their code, model endpoints, prompts, dependencies and credentials.
They need no Paperless Python package, server image, database or shared storage.

The generic server selects `documents.external_workers.ExternalParser` and
`documents.external_workers.ExternalMetadata`. These are core contract descriptors,
not implementations to install on the worker. The [deployment guide](processor-deployment.md)
sets up account capabilities, storage and the example independent worker.

## Discover and claim

Authenticate every request with `Authorization: Token TOKEN` over HTTPS. Use a
dedicated account granted `documents.process_parser` and/or
`documents.process_metadata`. These permissions authorize computation; the
original requester still needs permission for the resulting document changes.

`GET /api/operations/?status=pending` returns authorized, remotely claimable
operations. Each item includes `job_id`, `key`, `id`, `operation`, `contract`,
`status`, `job_status` and `error`. Supported external operations are:

| Operation | Discovery `contract` | Result identity |
| --- | --- | --- |
| `parser.process` | `paperless.external.parser:v1` | `provider` and `execution`: `paperless.external.parser:v1` |
| `metadata.enrich` | `paperless.external.metadata` | `identity`: `{"provider":"paperless.external.metadata","contract":1}` |

Other operations may appear for accounts with broader grants. A null `contract`
denotes work outside this generic boundary. Skip unsupported operations/contracts
before claiming. Discovery does not reserve work.

`POST /api/operations/{job_id}/{key}/claim/` attempts to lease one operation.
Encode `key` as a single URL segment. A successful claim is a version-2 lease
envelope with `status: leased`, `id`, `operation`, `plugin`, `input`, `token`,
`lease_until`, `attempts`, `replay_safe`, `remote_allowed` and policy/journal fields.
Require the supported lease version, expected operation/contract and
`remote_allowed: true` before computing. Parser input is `input.request` plus
`input.selection`; metadata input is `input` itself. Their request/result schema
version is 1, independently of the lease envelope version.

A competing or stale claim returns HTTP 409. Claim responses may instead report
`unknown` or `failed`; only `leased` permits computation. Unclaimed waiting does
not consume a lease or attempt. Claims have a bounded deadline and at most three
attempts. There is no lease-renewal endpoint; finish computation and delivery
before the returned deadline. Recovery handles expired claims according to their
retained replay policy. A replay may repeat model cost, so computations must not
perform independent document writes or other irreversible application effects.

## Input and output files

A file reference is exactly `{"path":"relative/path","size":123,"sha256":"..."}`.
Treat the path as an opaque server reference for transfer. If mirrored locally,
reject absolute paths, traversal and symlinks. Verify the downloaded size and
SHA-256 before using the bytes.

- Download declared inputs with `GET /api/operations/{job_id}/{key}/files/?path=...`
  and `X-Operation-Token: CLAIM_TOKEN`. The account and live claim must match.
- Upload outputs with `POST` to that same `files/` endpoint, the claim header,
  and a multipart `file`. Core returns its stored file reference. Use that
  returned reference in the final proposal, not a worker-local path.
- Repeating an upload with the same filename and bytes is idempotent. Different
  bytes at an already uploaded name return HTTP 409. Uploaded files remain private
  until core accepts and publishes the result.

## OCR request and result

The parser request contains:

| Field | Meaning |
| --- | --- |
| `version`, `revision` | Schema version 1 and opaque immutable request fingerprint; echo both. |
| `source`, `mime_type`, `filename` | Input file reference and its admitted type/name. |
| `settings` | Empty object for the generic descriptor; model configuration belongs to the worker. |
| `operation` | `process` for external parsing. |
| `archive_policy`, `output_type` | Required archive behavior; generic external requests use `output_type: pdfa`. |
| `output_dir` | Core-owned relative namespace for this operation's renditions. |
| `allow_remote` | Must be true for external computation. |
| `invocation_id` | Stable invocation identity when present. |

The result requires `version: 1`, the unchanged `revision`,
`provider: "paperless.external.parser:v1"`, `text`, `page_count`, `date`, `archive`,
`thumbnail`, `archive_type` and `worker`. If supplied, `execution` must also equal
`paperless.external.parser:v1`. Unknown result fields are rejected.

`text` is a string. `page_count` is a positive integer or null; `date` is an ISO
date/time string or null. `archive` and `thumbnail` are uploaded file references
or null. Core normally renders thumbnails locally, so external workers may
return `thumbnail: null`. An absent archive requires `archive_type: null`.
Any supplied archive must be PDF/A with `archive_type: pdfa`. The worker chooses
the PDF/A flavor; the server does not mandate a particular part or conformance
level. `archive_policy: always` requires an archive; `never` forbids one. Workers
must produce and validate conforming bytes: a claimed `archive_type` is not proof
of PDF/A conformance. The example worker chooses and validates PDF/A-2u.

Generic external requests use `output_type: pdfa` independently of the server's
local `PAPERLESS_OCR_OUTPUT_TYPE` setting. Built-in local parsers retain their
existing output-format settings and behavior.

Optional `provenance` is a JSON object whose `schema_version` is 1,
`source_sha256` matches the input and `page_count` matches the result. It requires
the server's configured OCR provenance field. Workers may include their detailed
page/model information there. Core checks file containment, file identities,
nonempty renditions and source/provenance consistency before publication.

## Metadata request and result

The metadata request contains `version: 1`, an opaque `revision`, source `content`,
current `metadata`, permitted relation `candidates`, existing `managed_fields`,
the retained processing `date`, `document_date_max`, `configuration` and
`identity: {"provider":"paperless.external.metadata","contract":1}`.
The configuration's implementation settings are empty for this descriptor.
Internal descriptor class names in the envelope are not imports or configuration
that an external worker must match.

Return `version`, the unchanged `revision` and `identity`, and `worker`.
The following proposal fields are optional; omissions preserve existing values:

| Field | Constraint |
| --- | --- |
| `title` | String of 1–128 characters. |
| `document_date` | ISO date from 1900-01-01 through the retained `document_date_max`. |
| `summary` | String; an explicit empty string clears its managed field. |
| `processing_date` | Exactly the request's retained `date`. |
| `audit` | JSON object. |
| `correspondent`, `language` | Exactly `{"id": permitted_id}` or `{"create": "name"}`; creation requires the requester's current permission. Language names match `language:[a-z]{2,3}`. |
| `custom_fields` | List of unique `{"field": id, "data_type": type, "value": value}` entries for the request's permitted fields and types. |

Unknown fields, malformed values, unsupported custom-field types and unauthorized
relations are rejected. Empty source content cannot produce metadata changes.
Core rechecks the document revision, field definitions, candidate visibility and
requester permissions before applying the proposal atomically. See the
[rich metadata contract](rich-metadata-contract.md) for preservation details.

## Worker provenance and delivery

Both result types require `worker`, containing a nonempty `implementation` string
and optional nonempty `model` and `prompt` strings. No other keys are accepted.
For example:

```json
{"implementation":"example-worker:1.0","model":"model-revision","prompt":"prompt-v2"}
```

This records the worker's reported execution identity. It is distinct from the
wire contract and does not cause core to load worker code or verify model weights.
Keep credentials, endpoint secrets and private configuration out of provenance.

Persist computed output and the exact proposal before delivery. Submit
`POST /api/operations/{job_id}/{key}/result/` with JSON
`{"token":"CLAIM_TOKEN","result":{...}}`. HTTP 202 means staged for validation,
not published. Repeating the identical result with the same account/token is
idempotent after staging or success; changing the result is not a delivery retry.

Current authorization is checked on claims, file access and submission. Revoked
permissions, a cancelled job, an expired lease or reassignment invalidate the
claim. A 409 requires inspecting current state, not automatically claiming anew
and reusing an old result. Cancellation is available to the requester/admin at
`POST /api/operations/{job_id}/cancel/`; it cannot undo publication or external
costs. Follow task/job status through core validation, publication and search
continuation before treating processing as complete.

## Compatibility

Server and workers may have independent releases, languages and dependencies.
They must agree on these operation/request/result versions. Workers must reject
versions they do not implement. When changing worker implementation, drain active
claims or preserve their exact delivery records; unclaimed generic work may be
claimed by any compatible authorized worker.

Old jobs admitted through installed Python providers retain those identities and
configuration snapshots. Finish them with the old deployment or explicitly
review/cancel them before removing its packages. Do not relabel old jobs into
generic contracts. The [operations runbook](provider-operations.md) describes
paired backup/restore and upgrade handling.
