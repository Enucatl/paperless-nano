# Installed inference providers

Version 0.1.0 implements rich metadata contract v1 and parser contract v1 on
Python 3.14. Install with the matching core release; the provider is not a
standalone service. It imports inference/PDF libraries only when executing work.

Build the core from the release commit, then extend that immutable image:

```sh
docker build -t paperless-core:release .
docker build -f providers/paperless-inference/Dockerfile \
  --build-arg PAPERLESS_IMAGE=paperless-core:release \
  -t paperless-inference:release .
```

Use this same image/configuration for core and eligible processors. The package
lock pins Python dependencies, including the existing shared inference client
at its source revision. Shared dependencies match the core lock; the extracted
font recovery is tested with core's fonttools 4.62 rather than requiring the
external runner's 4.68 minimum. The image retains core dependencies and runs
`uv pip check`. Ghostscript comes from core; the extension installs Java and
checksum-verified veraPDF 1.30.3. No listener, stage queue, or document API
credentials are required. Provider installation alone does not start processors;
follow the supervised deployment instructions.

Configure:

```sh
PAPERLESS_PARSER=paperless_inference.providers.PaddleOCRProvider
PAPERLESS_ENRICHMENT_PROVIDER=paperless_inference.providers.MetadataProvider
PAPERLESS_ENRICHMENT_ALLOW_REMOTE=true
INFERENCE_OCR_ENDPOINT=http://paddle:8080
INFERENCE_OCR_TIMEOUT=600
INFERENCE_OCR_PIPELINE=PaddleOCR-VL-1.6
INFERENCE_OCR_MODEL=PaddlePaddle/PaddleOCR-VL-1.6
INFERENCE_OCR_LAYOUT_MODEL=PP-DocLayoutV3
INFERENCE_METADATA_ENDPOINT=http://metadata:8080/v1
INFERENCE_METADATA_MODEL=your-immutable-model-revision
```

Read the actual OCR deployment's `/metadata` response to set the three identity
values; these examples are not an assertion about your endpoint. The provider
checks identity before and after OCR. Explicitly allow remote OCR on submissions
or via core's remote OCR default. Set managed summary/date/audit field IDs through
`PAPERLESS_ENRICHMENT_FIELDS`; core owns schema provisioning and publication.
Set `PAPERLESS_OCR_PROVENANCE_FIELD` to an existing longtext custom field ID for
the structured `ai_ocr_output` record.

Metadata defaults preserve the original prompt/schema, strict JSON schema
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

OCR accepts PDF input, validates every page, removes image payloads from stored
provenance, preserves original pages in searchable PDF/A-2u archives, and checks
Unicode conformance using veraPDF. As in the baseline, `layout_model=none`
produces text/provenance without an archive. Core supplies thumbnails separately.
Metadata extraction returns proposals only. Empty content yields no changes;
endpoint failure propagates to durable operation recovery.

Changing model, prompt revision, endpoint or effective configuration invalidates
pending work through core's retained snapshots. A model name must identify an
immutable deployment: a server silently replacing weights under the same name
cannot be detected by this API. Cancellation does not undo a billed request.

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

Tests distinguish mocked endpoint responses from actual model parity. The
extracted archive suite runs real Ghostscript/veraPDF conversions; release
validation must additionally exercise the configured live model deployment.
