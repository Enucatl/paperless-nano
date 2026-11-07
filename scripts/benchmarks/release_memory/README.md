# Original, polling and Celery release comparison

This harness compares the retained six-service deployment at core revision
`8eb396d9833a56e6147092620b8832e39397f684`, the first integrated polling image
recorded in `docs/inference-release-validation.md`, and the issue #52 Celery
candidate image `paperless-release-celery:issue52`. Set `BENCH_CURRENT_IMAGE` to
the immutable candidate tag built from the source snapshot being tested. They run
sequentially on the same Docker host with fresh PostgreSQL 18 and Redis 8,
one web worker and one autoscaling task worker. It uses real OCR and metadata
endpoints but only generated synthetic invoices; no production data is copied.

The [2026-11-05 diagnostic report](../../../docs/inference-release-validation.md#instrumented-memory-and-latency-diagnosis-2026-11-05)
records one completed matched pair and an interrupted repetition. The workload's
content-word marker can precede newest-metadata search convergence; the report
derives that later completion separately from the retained job journal. The
that run's completion loop also waits to its deadline on terminal job failure;
that deadline is not an inference latency measurement. No product fixes were
made while gathering these observations.

The 2026-11-06 rerun preserves that historical archive. It additionally records
the current metadata-title search marker, stops promptly on terminal upload-task
or operation failure, and saves per-upload and HTTP timing evidence even when a
later upload fails. All three fresh variants use the improved observation rules.
Historical content-marker timings remain a separate reference; new nominal-idle
windows begin after metadata-title search verification.

Each run uploads a one-page and three-page PDF, browses their metadata/previews,
observes return to idle, then uploads another one-page PDF after idle. Completion
requires OCR text, all four managed fields, ordinary search visibility and an
archive download; the original checksum must be unchanged. Model output quality,
upgrades, reprocessing and crash recovery are outside this workload.

The controlled baseline uses **10-second polling**, rather than its deployed
300 seconds, to match the candidate's ten-second processor sweep. This measures
the architecture with comparable polling, not actual deployed latency. Metadata
temperature is explicitly zero on both sides (the deployed baseline uses 1.0).
The unpublished isolated Phoenix instance sets `PHOENIX_ENABLE_AUTH=false` so
baseline telemetry is successfully stored instead of rejected with HTTP 401.
Both variants retain their own normal orchestration, libraries and image contents.
Phoenix remains included in the old deployment's total; report per-service
results so its contribution is visible. No chat or alternative search is run.

`run.py --preflight` runs all three variants once with a short idle window.
The normal run uses baseline/candidate/current/current/candidate/baseline, with three
300-second observation windows per run. Do not count preflight as measurements.
The application container is never recreated during one run. Scheduled services
remain enabled, so these are nominal idle observation windows.

The sampler records simultaneous per-service raw cgroup memory, anonymous memory,
working set (raw less inactive file cache), lifetime peaks and memory events once
per second. Process PSS, anonymous/file/shared PSS, private dirty memory, CPU time,
page faults, parent PID and sanitized process roles are also sampled each second.
Snapshots distinguish resident services, task children and temporary processor
sweeps; parent IDs permit attribution of generic spawned children. Cgroup CPU
and block-I/O counters include short-lived children that process sampling misses.
Sampling duration, host load and resource pressure are retained to quantify
observer overhead and competing work. Exited-process races remain explicit.
Only samples with the full expected service set may enter aggregate summaries.
Summing per-container lifetime peaks produces an upper bound, not a simultaneous
peak. Setup helpers/controller are excluded. Fresh core migrations and account
provisioning precede sampling; database lifetime peaks include that setup.
API readiness starts when the core container launches. Baseline stack readiness
also includes first-time sidecar database/resource provisioning, so it is not a
clean restart latency comparison.

The Celery candidate uses its bounded ten-task child recycling default and
autoscales down to zero. Report process identities across immediate uploads and
after idle to verify reuse and provider release. The historical Redis-wakeup
comparison used a separate 60-second processor timeout; its archived results
remain unchanged. Current job recovery runs daily at 03:00; retain shipped defaults
and count observed recovery deliveries. The original and polling variants retain
their prior behavior. Pin PostgreSQL,
Redis and all app image IDs at controller startup so repeats cannot drift with tags.
The currently available PostgreSQL 18 image can differ from the historical image;
all fresh variants share it and the old measurements are not rewritten.

A separate disposable observer uses one read-only PostgreSQL query per 100ms on
both stacks. It records task lifecycle transitions, plus candidate job/invocation
admission, waiting, leases, result staging and acceptance. Result/configuration
payloads and lease tokens are excluded. Each observation includes database and
client timing bounds; states committed and replaced between polls may be missed.
Retained lease deadlines and job timeouts allow exact claim-time derivation even
when the leased state was too brief to sample. Query overhead is measured in
periodic summaries. The observer process is excluded from service memory, but its
database connection/query cost remains in PostgreSQL and is reported. Successful
baseline inference spans are exported after sampling, without payload attributes.

`launch.py` uses the host's existing SSH authentication to stream `remote.py`
observations directly into Docker storage. It observes the existing OCR
gateway/API/model containers and whole GPU on Cerebrum. The unrelated BGE container is reported
separately. GPU compute-process allocations are attributed to OCR, BGE, other,
or unknown processes by host PID/cgroup; exited PID races are recorded explicitly.
Service samples include cumulative CPU/I/O counters and their deltas, anonymous
and file memory, and observer duration. Permission errors fail observation.
SSH clock probes before and after the experiment bound local/remote alignment.
GPU allocation includes BGE and other users; one-second samples are
lower bounds for short peaks. Remote lifetime cgroup peaks predate this run and
must not be attributed to this workload. OpenRouter hosting resources and price
are not exposed by this sampler.

Run all service and helper containers with `--rm --userns=host`. The controller
needs host PID/cgroup namespaces, `SYS_PTRACE`, the Docker socket/CLI,
a read-only model secret at
`/model-secret/openrouter_api_key`, this
directory mounted at `/harness`, and a **fresh** evidence volume at `/evidence`.
Set `BENCH_CONTROLLER` to its Docker container name and disable Python bytecode.
The controller uses the same private model key as the retained deployment;
credentials are never written to measurement JSON or source. Only model serving
on the remote host is contacted; its production Paperless stack is not changed.

Transfer the four baseline images using their **named references**, then tag
them with the local aliases in `baseline.IMAGES`. The launcher verifies image
configuration and root filesystem layers against the retained source IDs over
SSH; Docker's classic/containerd stores can report different IDs for identical
transferred contents. Exporting multiple untagged IDs can lose image references.
The real-model endpoint must be reachable before launching. The issue #52
comparison was blocked on November 6 by both SSH and OCR HTTP connection timeouts
to Cerebrum; no Celery performance result is available. The server-only harness
below does not exercise offline-then-online workstation execution; run the focused
lifecycle checks separately and retain that live-model scenario as an open gate.

Build a frozen source snapshot with the included Dockerfile, using the retained
dependency image as its base, and tag it `paperless-release-celery:issue52` (or
set `BENCH_CURRENT_IMAGE`). The Dockerfile removes inherited processor service
files as well as copying the current source and S6 configuration.
It also overlays the current provider package on the retained Python 3.14
dependencies. Verify the base tag resolves to image
`sha256:6824eec4681c4087dbff484efab2b1da399c7b0e3adfd614ed78afbabfa6fc1a`
before building. The current variant seeds `processing_date` and `audit` by
their exact names; historical variants retain their field-ID mapping and names.

Run the self-checks in disposable containers before the experiment, then use
the repository's `wake-run` workflow to invoke:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmarks/release_memory/launch.py paperless-release-memory-UNIQUE --preflight
# After the preflight passes, use a new volume and omit --preflight.
```

The [completed three-version comparison](../../../docs/inference-release-validation.md#three-version-comparison-after-redis-wakeups-2026-11-06)
records two measured repeats per version, separates the first minute from later
idle, and distinguishes content visibility, metadata-title visibility and durable
search acceptance. Its data and plots are separate from the November 5 evidence.

The launcher reads the model secret privately as the host user and passes it
through stdin into Docker storage, avoiding NFS root-squash mount failures.
The controller runs from the retained candidate image, without added dependencies.
The launcher freezes the harness in a temporary Docker volume for each run, so
concurrent workspace edits cannot change a running experiment. Temporary stack
volumes and networks are removed on success or failure; retain the evidence volume only
until its reports have been exported. An interrupted controller can leave
resources named `pm-release-*`; remove only those experiment resources.

Evidence contains scripts, image/process identities, one-second samples, API
latencies, workload assertions/provenance and private service logs. Logs are for
diagnosis and should not be published without inspection. Keep sanitized results
separate from logs and credential-bearing Docker inspect/environment output.
