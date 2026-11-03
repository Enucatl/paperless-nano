# Isolated idle-memory comparison

This is the purpose-specific six-service harness used for issue #27. It starts
fresh Paperless, PostgreSQL, Redis, AI, webhook-listener and Phoenix services on
an internal Docker network, with no published ports. It copies a bounded private
snapshot into disposable volumes. It does not connect to a production database
or recover production jobs. See [the measurement report](../../../docs/memory-measurement.md)
for results and acceptance limits.

The controller requires Linux, Docker, cgroup v2 with `memory.peak`, host PID and
cgroup namespaces, and permission to read process `smaps_rollup`. It uses the
host Docker socket. Run one variant at a time, with a fresh `BENCH_VARIANT` name;
an existing results directory is rejected. Service volumes and the internal
network are removed on normal success or failure. An interrupted controller can
leave resources named `pm27-<variant>-*`; inspect and remove those resources only.

## Private snapshot

Prepare a private Docker volume with this layout. Keep its contents and raw
results out of Git; document text, paths, captured settings and service logs can
contain private information.

```text
config/containers.json
config/application-configuration.json
selected-documents.json
manifest.json
files/media/documents/...
results/                       # created by the harness
```

- `config/containers.json` is an array of six Docker inspect records. `Name`
  must end in `-db-1`, `-broker-1`, `-webserver-1`, `-ai-1`,
  `-webhook-listener-1` or `-phoenix-1`; `Config.Env` contains the effective
  service environment. Direct credential values are replaced in the clone,
  and secret files are generated there. The network has no external egress.
- `config/application-configuration.json` is a nonempty array of application
  configuration row dictionaries. Only the parser fields listed in `seed.py`
  are copied. Remote-service credentials and application identities are not
  imported from these rows.
- `selected-documents.json` contains exactly 14 active root-document row
  dictionaries. Include document scalar fields and timestamps, OCR content,
  file paths, checksums, MIME type and page count. Dates use the database's
  serialized ISO representation. The seed retains scalar fields known to the
  current Django model, supplies a new owner and excludes deletion/version
  bookkeeping and original relations.
- `manifest.json` has a `documents` array, one entry per selected document,
  containing `id` and `files`. Each file role (`original`, `thumbnail`, and
  `archive` when present) has `path`, `bytes` and `sha256`; paths are relative
  to the snapshot root and start with `files/media/`. Copy exactly these files.
  Record the source revision, selection method and service image IDs in the
  manifest alongside this required structure.

The current selection requires at least two distinct PDFs with 1–10 pages.
The smallest and largest qualifying PDFs, ordered by page count then ID, are
reserved for ingestion and omitted from the initial twelve-document database.
Their checksums must differ from seeded documents. Include another modest PDF
for reprocessing and OCR text with an eight-letter word for the search check.
The captured deployment must have local OCR, the built-in classifier and
built-in AI disabled, and the lean worker enabled; the seed asserts this scope.
The external AI services remain resident, but this workload does not exercise
external model inference.

To copy an already prepared local selection, with no retrieval credentials:

```sh
export BENCH_PRIVATE_DIR=/absolute/private/selected-snapshot
export BENCH_SNAPSHOT_VOLUME=paperless-memory-27-snapshot
docker volume create "$BENCH_SNAPSHOT_VOLUME"
docker run --rm --userns=host \
  -v "$BENCH_PRIVATE_DIR:/input:ro" \
  -v "$BENCH_SNAPSHOT_VOLUME:/snapshot" alpine:3.21 \
  sh -c 'umask 077; cp -a /input/. /snapshot/; chmod -R go-rwx /snapshot'
```

This input is trusted benchmark data, not an import API. Validate its manifest
and file checksums when making the selection. The harness does not sanitize
arbitrary supplied paths or implement a general snapshot exporter.

## Build the two revisions

Use the same immutable deployed image as `BASE_IMAGE` for both builds. The
recipe overlays backend source and service launchers from the checkout being
measured; it retains the base image's dependencies and frontend. This is valid
only when both revisions use that same dependency and frontend set. Otherwise
build the full repository image for each revision before using this harness.

```sh
export BENCH_BASE_IMAGE=sha256:YOUR_LOCAL_DEPLOYED_IMAGE_ID
export BENCH_BASELINE_CHECKOUT=/absolute/path/to/baseline-checkout
export BENCH_CANDIDATE_CHECKOUT=/absolute/path/to/candidate-checkout
export BENCH_HARNESS_DIR="$(pwd)/scripts/benchmarks/idle_memory"
docker build -f "$BENCH_HARNESS_DIR/Dockerfile" \
  --build-arg BASE_IMAGE="$BENCH_BASE_IMAGE" \
  --build-arg BENCHMARK_REVISION="$(git -C "$BENCH_BASELINE_CHECKOUT" rev-parse HEAD)" \
  -t paperless-memory27:baseline "$BENCH_BASELINE_CHECKOUT"
docker build -f "$BENCH_HARNESS_DIR/Dockerfile" \
  --build-arg BASE_IMAGE="$BENCH_BASE_IMAGE" \
  --build-arg BENCHMARK_REVISION="$(git -C "$BENCH_CANDIDATE_CHECKOUT" rev-parse HEAD)" \
  -t paperless-memory27:candidate "$BENCH_CANDIDATE_CHECKOUT"
```

Retain any uncommitted source diff with private run evidence: the revision
label alone cannot identify uncommitted code. Load the six service images
locally first. The harness uses `postgres:18`, `redis:8`,
`paperless-ai-ai:latest`, `paperless-ai-webhook-listener:latest`, and
`arizephoenix/phoenix:latest`; record their resolved image IDs and keep them
unchanged between runs. The two AI images come from the companion deployment,
not this repository.

## Self-check and run

The configuration self-check reads the snapshot without starting services or
writing results. The workload self-check exercises its task-response handling.

```sh
docker run --rm --userns=host --no-healthcheck \
  -e BENCH_HARNESS_DIR \
  -v "$BENCH_HARNESS_DIR:/harness:ro" \
  -v "$BENCH_SNAPSHOT_VOLUME:/snapshot:ro" \
  --entrypoint python paperless-memory27:baseline /harness/run.py --self-check
docker run --rm --userns=host --no-healthcheck \
  -v "$BENCH_HARNESS_DIR:/harness:ro" \
  --entrypoint python paperless-memory27:baseline /harness/workload.py
```

Run a short `BENCH_PREFLIGHT=1` startup/API check first with its own variant
name. `BENCH_WORKLOAD_PREFLIGHT=1` runs the feature assertions with one-second
idle phases; it is not a residency measurement. Omit both for measurement:

```sh
export BENCH_VARIANT=baseline1
export BENCH_IMAGE=paperless-memory27:baseline
docker run --rm --userns=host --name pm27-controller \
  --pid=host --cgroupns=host --cap-add SYS_PTRACE --no-healthcheck \
  -e BENCH_CONTROLLER=pm27-controller -e BENCH_VARIANT -e BENCH_IMAGE \
  -e BENCH_HARNESS_DIR -e BENCH_SNAPSHOT_VOLUME -e BENCH_IDLE_SECONDS=300 \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /sys/fs/cgroup:/host-cgroup:ro \
  -v "$BENCH_HARNESS_DIR:/harness:ro" \
  -v "$BENCH_SNAPSHOT_VOLUME:/snapshot" \
  --entrypoint python paperless-memory27:baseline /harness/run.py
```

`BENCH_HARNESS_DIR` must be an absolute path on the Docker host: the controller
passes it to sibling helper containers. Use the same snapshot and idle duration
for both images, alternate baseline/candidate order, and use a new variant name
for each repeat. Follow the main thread's `wake-run` instructions for long runs.

Each private `results/<variant>/` contains the executed scripts, run settings,
one-second per-service cgroup samples, phase summaries, PSS snapshots, endpoint
latencies, readiness duration, workload assertions and service logs. After
sampling stops, `runtime_checks.py` verifies one retry through the real Celery
recovery sweep, resumes committed completion without reparsing, and rejects a
stale result while retaining its input. These checks use a seeded PDF in the
disposable clone; their memory is excluded from the measured phases. Their
structured result and private diagnostic log are retained separately. The
controller and temporary setup helpers are excluded from the six-service sum.
The sum is simultaneous; summing individual service lifetime peaks would
overstate a total peak. Raw `memory.current` includes file cache. Compare PSS
and anonymous memory separately before attributing any reduction to code.

The seed includes fresh ownership and one controlled final-state workflow, not
the original users, permissions, mail, tags, versions or external-effect
workflows. The workload checks downloads, previews, metadata, search,
suggestions, final-state indexing, ingestion, reprocessing and ingestion after
idle. Scheduled task observations alone do not prove every task completed.
This bounded selection cannot establish a universal peak cap or replace the
repository's permission, retry, stale-result and recovery tests.
