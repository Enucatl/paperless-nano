# Standalone PaddleOCR and metadata worker

`paperless-inference` is one external worker implementation for Paperless's
versioned operation API. It owns PaddleOCR and metadata model configuration,
prompts, credentials, PDF tools and Python dependencies. The Paperless server
needs neither this package nor its image; other workers can implement the same
HTTP contracts using their own language and processing libraries.

The worker supports `paperless.external.parser:v1` and
`paperless.external.metadata` contract version 1. It discovers and claims work,
downloads content-checked inputs, produces proposals and uploads outputs.
Paperless checks current permissions, document revisions and result constraints
before publishing. The `worker` result field reports this implementation's model
and prompt identities; the contract identity is independent of those choices.

Build the worker directly, without a Paperless base image:

```sh
docker build -f providers/paperless-inference/Dockerfile \
  -t paperless-inference:worker .
```

The image contains Python 3.14, the frozen worker dependencies, Ghostscript,
Java and checksum-verified veraPDF 1.30.3. It runs only
`python -m paperless_inference.workstation` as UID 1000. Use
[`compose.workstation.yml`](compose.workstation.yml) with this image and a private
state volume; see [processor deployment](../../docs/processor-deployment.md) for
account provisioning and server placement.

Configure these values on the worker:

```sh
PAPERLESS_URL=https://paperless.example.invalid
PAPERLESS_API_TOKEN_FILE=/run/secrets/paperless_api_token
INFERENCE_OCR_ENDPOINT=http://paddle:8080
INFERENCE_OCR_TIMEOUT=600
INFERENCE_OCR_PIPELINE=PaddleOCR-VL-1.6
INFERENCE_OCR_MODEL=PaddlePaddle/PaddleOCR-VL-1.6
INFERENCE_OCR_LAYOUT_MODEL=PP-DocLayoutV3
INFERENCE_METADATA_ENDPOINT=https://openrouter.ai/api/v1
INFERENCE_METADATA_MODEL=your-immutable-model-revision
OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key
```

Read the actual OCR deployment's `/metadata` response to set the three identity
values; these examples are not an assertion about your endpoint. The worker
checks its configured endpoint identity before and after OCR. Explicitly allow remote OCR on submissions
or via core's remote OCR default. Create existing custom fields named `summary`
(longtext), `processing_date` (date), and `audit` (longtext) for those outputs.
Additional extractor properties match existing custom-field names exactly,
including case; unmatched names are ignored and definitions are never created
automatically. The model prompt and strict response schema include supported
additional field names and types. Null model answers for additional fields leave
existing values unchanged. No metadata field-ID setting is needed. Core owns validation and
publication. See the [migration instructions](../../docs/rich-metadata-contract.md#custom-field-migration)
for existing ID-mapped installations.
Set `PAPERLESS_OCR_PROVENANCE_FIELD` to an existing longtext custom field ID for
the structured `ai_ocr_output` record.

Metadata defaults retain the baseline fields with strict JSON schema
response format, 1000 max tokens, temperature zero, minimal reasoning effort,
first valid language or `und`, and correspondent similarity threshold 0.80. Optional settings are
`INFERENCE_METADATA_RESPONSE_FORMAT` (`auto`, `json_schema`, `json_object`,
`none`), `INFERENCE_METADATA_MAX_TOKENS`, `INFERENCE_METADATA_TEMPERATURE`,
`INFERENCE_METADATA_REASONING_EFFORT`, `INFERENCE_METADATA_EXTRA_KWARGS`, and
`INFERENCE_CORRESPONDENT_MATCH_THRESHOLD`. Extra kwargs permit ordinary sampling
parameters only; credentials and routing overrides cannot enter retained jobs.
API keys use `OPENAI_API_KEY_FILE` or `OPENROUTER_API_KEY_FILE` when a readable
secret file is configured, falling back to `OPENAI_API_KEY` or
`OPENROUTER_API_KEY` as in the previous runner. Mount the secret read-only and
set the corresponding `_FILE` path in the container. Keys are read only during
inference and never enter configuration snapshots.
Endpoint URLs cannot contain embedded credentials or query parameters.

OCR accepts PDF input and the generic `output_type=pdfa` requirement, validates every page,
removes image payloads from stored provenance, preserves original pages in
searchable PDF/A-2u archives, and checks Unicode conformance using veraPDF.
The worker chooses PDF/A-2u; the server requires PDF/A without choosing its flavor.
Archive policies `auto`, `always` and `never` are supported. Malformed output
requirements fail before inference. `layout_model=none` can produce text/provenance
only when the request does not require an archive. Core supplies thumbnails
separately.
Metadata extraction returns proposals only. Empty content yields no changes;
endpoint failure propagates to durable operation recovery.

A pending unclaimed request can be executed by any authorized worker implementing
its contract; model and prompt changes do not require a server rebuild. An
already computed result is saved privately and delivery retries reuse that
result, even after worker restart or configuration changes. Preserve the state
volume until delivery is acknowledged. Expired or cancelled claims require
operator review of the retained proposal; the client does not discard it or
blindly repeat inference. Cancellation cannot undo a model request's charges.

## Source and intentional boundary changes

OCR, archive/font recovery, structured metadata extraction, language list,
prompt, correspondent resolver and their baseline tests are extracted from
`Enucatl/paperless-ai` commit
`690691928cd8953529610c342748324324d06df7` under GPLv3 (see `LICENSE`).
Only processing code is retained; runner, listener, direct API writeback, chat,
search alternatives, cluster cleanup and telemetry setup are excluded.
The shared inference transport remains the same pinned implementation, with
sessions closed after each operation instead of cached for a batch runner.
Processing day and document date upper bound come from the retained core request.
Entity creation and current permission checks belong to core; a rejected proposal
does not partially apply other metadata. See the rich metadata contract's
documented differences for those stronger atomicity and concurrency guarantees.

The test suite runs without Paperless or Django installed and checks HTTP
transport, unknown contracts, unsafe files and delivery retries. Mocked endpoint
responses remain distinct from actual model parity. The
extracted archive suite runs real Ghostscript/veraPDF conversions; release
validation must additionally exercise the configured live model deployment.
