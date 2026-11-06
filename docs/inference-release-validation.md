# OCR and rich metadata release validation

Validation date: 2026-11-05. Tracker: [#43](https://github.com/Enucatl/paperless-ngx/issues/43);
release gate: [#49](https://github.com/Enucatl/paperless-ngx/issues/49).

**Release publication is withheld.** Live synthetic ingestion, reprocessing,
provider swap/backout and paired database/private-file restore now pass. These
checks do not establish representative-corpus parity, an upgrade from the supported
previous deployment, or equivalent-workload memory behavior. Keep the existing
deployment available until those remaining gates pass.

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
dependencies and test artifacts stayed in temporary Docker storage. Source was
read from the workspace with bytecode/caches disabled or copied into the container.
Counts below overlap and must not be added into one total.

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
| Secret-file authentication and provider regressions | 16 passed; file precedence, rotation, missing/blank fallback and secret-free snapshots |
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
`:validation` and `:secrets-validation` images, not published release artifacts. The build includes the
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
not steady-state averages or a peak isolated to inference. Remote inference was
unavailable during this initial search-only run; the later successful live workload
and remote snapshots are recorded below. Neither set establishes an equivalent
whole-workload comparison or satisfies a target by itself.

## Live OCR and metadata continuation

The initial OCR timeout occurred while the workstation was unavailable. After it
started, `/health` and `/metadata` returned 200 with pipeline `PaddleOCR-VL-1.6`,
model `PaddlePaddle/PaddleOCR-VL-1.6`, and layout model `PP-DocLayoutV3`.

The earlier metadata HTTP 401 was a **credential-loading regression**, not proof
that the real key was invalid: the baseline loads `OPENROUTER_API_KEY_FILE` and
`OPENAI_API_KEY_FILE`, while the extracted adapter initially read only direct
environment values. The adapter now reads those files at execution, prioritizes
nonempty file values, and retains the previous runner's missing/blank-file fallback.
Credentials never enter configuration snapshots. Tests cover precedence, rotation,
fallback and absence from snapshots. The actual key was transferred privately to a
temporary read-only Docker secret volume, readable only by application UID 1000;
its bytes were not printed, added to the repository, or baked into an image.

The installed candidate used for the live continuation and final backout/restore:

```text
paperless-release49-inference:secrets-validation
sha256:6824eec4681c4087dbff484efab2b1da399c7b0e3adfd614ed78afbabfa6fc1a
```

This extends the core image above with the secret-file fix on the worktree based
on `2d027485f`. All data was synthetic and isolated from production. Normal HTTP
API admission/reprocessing used a temporary authenticated test user; processors
used the dedicated capability account. No listener or separate stage queue ran.

| Live check | Result |
| --- | --- |
| Installed metadata provider, synthetic invoice | Valid complete proposal in 2.232 seconds |
| Existing-document metadata application and ordinary search | Passed; title/date/summary/processing date/audit/correspondent/language applied; unrelated tag and custom field preserved |
| New PDF through authenticated upload → OCR → archive/provenance → metadata → search | Passed; ingestion 100.417 seconds, metadata child 13.644 seconds |
| OCR endpoint compute elapsed reported in provenance | 0.851 seconds, distinct from queue and publication latency |
| Published files | 1,038-byte original; 9,565-byte searchable archive; all four managed metadata/provenance fields present |
| API reprocess of the existing uploaded PDF | Passed in 20.177 seconds; retained user title, unrelated tag/custom field and non-OCR metadata |
| Archive/source integrity | Original SHA-256 unchanged and matched OCR provenance; archive text searchable; actual veraPDF validation passed |
| Provider swap to built-in `DefaultParser` | Fresh reprocess passed in 9.809 seconds; metadata and existing archive preserved |
| Pending work during provider swap | Old Paddle job rejected before retry with `Processing configuration or provider changed before retry`; no silent rerouting |
| Swap back to installed Paddle provider | Fresh reprocess passed in 19.746 seconds; metadata preserved and Paddle provenance verified |

The first management-shell metadata admission was accidentally run as root and
created a lock the application user could not open. The isolated harness ownership
was corrected and the job explicitly resumed; that recovery was not counted as an
automatic-success path. Both current guides now run job/recovery commands as
`--user paperless`. Subsequent full ingestion and reprocessing used the HTTP API
and completed through supervised processors without manual launches.

One intermediate search job reached `attention`: its write had executed and its
result was staged, but metadata changed the retained projection before coordinator
acceptance. The final newer search completed and queries verified the current
record. This is the existing stale-result/review behavior, not an unknown inference
result; it does **not** mean every task succeeded or that no transient old search
projection existed. Search-job coalescing was not added in this release.

### Coordinated restore

After all nonterminal work drained, the application was stopped. A PostgreSQL
custom-format dump and the complete data/media trees were captured together,
then restored to independent database/data/media volumes with a fresh broker.
The restored application checked authenticated document access, current metadata,
original hash against OCR provenance, searchable archive, ordinary search, and
the rejected pending job's retained private input. This tests restoration of this
candidate's paired state; it is not a database downgrade or a previous-version
upgrade test.

### Live workload resource observations

A local snapshot taken just after the OCR lease notification showed Docker working
sets of 766.7 MiB (Paperless), 46.79 MiB (PostgreSQL), and 4.11 MiB (Valkey).
Corresponding cumulative cgroup peaks were 1,064.20 MiB, 125.23 MiB and 13.84 MiB.
A later drained snapshot after swap-back reprocessing showed 552.4 MiB, 44.27 MiB
and 4.84 MiB; its Paperless process had been recreated for the provider swap, so
this is **not** a same-process return-to-idle comparison. Observer management
processes and periodic services can contribute to these samples.

The remote RTX 5090 workstation snapshots at 16:50:22Z and 16:51:56Z both showed
11,824 / 32,607 MiB GPU allocation and 0% utilization. The short OCR compute had
already finished before sampling; these are neither active-compute nor peak
measurements. OCR VLM/API Docker working sets were approximately 3.32 GiB /
1.917–1.918 GiB, gateway 9.406 MiB. A separately loaded BGE service used 4.35 GiB;
its GPU allocation contributes to the total and must not be attributed to OCR.
Remote resources are reported separately from the local core stack.

## Instrumented memory and latency diagnosis (2026-11-05)

**The integrated deployment uses substantially less resident memory, but pays for
it with repeated process initialization, higher CPU consumption and serial queue
latency.** This is a diagnostic result, not a product fix or release certification.
One complete matched baseline/candidate pair passed. The next candidate run lost
OCR endpoint connectivity; its incomplete results are reported separately and
the second baseline was never run. No repeat-median claim is made.

The [machine-readable diagnostics](inference-release-diagnostics.json) contain
phase distributions, process attribution, timing bounds, CPU/I/O, identities and
the interrupted run. The [reproduction harness](../scripts/benchmarks/release_memory/README.md)
describes instrumentation and exclusions. Raw samples, private synthetic-service
logs, frozen scripts and derived data are retained in the local, unpublished
`artifacts/release-memory-20261105.tar.gz` archive. Earlier validation and its
Valkey measurements above remain historical evidence; this experiment uses Redis.

![Matched successful pair: service memory and cgroup memory over time](inference-release-memory.png)

### What was compared

The old six-service deployment uses retained core revision
`8eb396d9833a56e6147092620b8832e39397f684`, plus its actual AI, listener and Phoenix
images. The integrated three-service deployment uses
`paperless-release49-inference:secrets-validation`, image
`sha256:6824eec4681c4087dbff484efab2b1da399c7b0e3adfd614ed78afbabfa6fc1a`.
Image configuration and root filesystem layers were verified after transfer;
Docker's classic and containerd image stores report different image identifiers
for transferred contents. Original and local identifiers are retained in JSON.

Both stacks ran sequentially on the same eight-CPU Linux x86-64 host with
15.05 GiB Docker-visible RAM, fresh PostgreSQL 18 databases and Redis 8, one web
worker and one autoscaling task worker. The workload uploaded the same one-page
and three-page synthetic invoices, browsed metadata/previews, then uploaded
another one-page invoice after idle. Originals retained their hashes; OCR text,
managed fields, archive downloads and content search passed. Each complete run
has three 300-second nominal-idle windows and keeps the same application
container and HTTP process identities throughout. Scheduled work remains enabled.

The comparison normalizes old polling from its deployed **300 seconds to 10**,
matching the new sweep delay, and metadata temperature from deployed 1.0 to zero
on both sides. The isolated Phoenix instance disables authentication so real
inference traces are stored; it publishes no port. Model servers remain warm and
external; no chat workload runs. These settings describe a controlled comparison,
not the exact latency or resource use of the current production deployment.

### Memory: persistent savings and larger core bursts

PSS is proportional process memory: shared pages are apportioned rather than
counted once per process. Values below are medians of all 300 one-second samples
within each window, not endpoint snapshots or medians across repeated runs.

| Nominal-idle phase | Old stack PSS MiB | Integrated PSS MiB | Reduction | Old / integrated working set MiB |
| --- | ---: | ---: | ---: | ---: |
| Startup | 1,054.1 | 366.3 | 65.3% | 1,033.2 / 370.3 |
| After features | 1,316.5 | 404.5 | 69.3% | 1,255.4 / 421.1 |
| After later ingestion | 1,333.4 | 406.8 | 69.5% | 1,295.5 / 423.4 |

Anonymous-memory medians corroborate the process-memory reduction: final idle
falls from **1,073.6 to 296.3 MiB**. The strongest attribution is removal of
resident Phoenix (about 504.8 MiB PSS), AI (105.8 MiB before use, about 244 MiB
after use), and the listener (68.3 MiB). PostgreSQL also shrinks, with fewer
application databases/connections. Core startup PSS actually rises slightly,
**282.9 to 297.3 MiB**; after features it is **388.0 versus 329.2 MiB**. This is
chiefly a deployment/lifetime saving, not a universally smaller web process.

The sampled simultaneous raw cgroup peak falls **1,856.4 to 1,063.7 MiB**, both
during the three-page upload. Working sets at those samples are **1,397.5 and
849.0 MiB**. Raw memory includes file cache. The sums of service lifetime peaks,
**1,980.1 and 1,100.2 MiB**, are upper bounds rather than simultaneous peaks.
The core container's own lifetime peak grows **649.4 to 964.4 MiB**: near the
integrated aggregate peak, PSS includes a 240.6 MiB processor sweep, its
136.5 MiB Java child, a 103.0 MiB task child and the 176.4 MiB HTTP child.
Processing has moved inside core; the full deployment still peaks lower.

The integrated HTTP child stays in the same process and grows from about
151.1 to 182.8 MiB PSS. A first sample without a task child or processor sweep
appears about 22 seconds into the post-feature window. This is a warmed idle
state, not a reset to startup. Post-feature and final nominal-idle windows include
about 10.35 and 7.98 seconds of late search completion before the newest projection
is accepted; their CPU averages are not measurements of fully drained idle.
Sweeps appear in 42/300, 42/300 and 43/300 idle
samples. Integrated idle PSS mean/P95 values are **402.1/456.6**, **423.9/491.3**
and **435.6/493.8 MiB**. Periodic children reappear and exit; one snapshot with a
worker does not establish a leak, and five minutes does not establish leak freedom.

Cache accounting matters. In the incomplete second candidate, startup/post-feature
PSS is almost unchanged at 366.4/404.3 MiB, but raw memory is about 98/132 MiB
lower, mostly because fewer cached file pages are charged to those cgroups.
Likewise, old-stack raw memory falls between the last two windows while PSS and
anonymous memory rise. Such raw reductions must not be credited as process release.

### Latency: queue waits dominate the request

The original workload marker checks populated metadata plus
`content:benchmarksearchable`. It can pass against an earlier OCR search
projection. The retained journal independently identifies when the **newest
metadata projection** is accepted, shown separately below. API polling adds up
to approximately one polling interval plus request time to the earlier marker;
journal transition bounds are approximately 100 ms, with measured query overhead.

| Upload | Old content-search marker s | Integrated marker s | Integrated newest search accepted, from admission s | Integrated serial queue waits s |
| --- | ---: | ---: | ---: | ---: |
| One page | 24.206 | 102.972 | 120.376 | 87.713–88.614 |
| Three pages | 22.084 | 112.512 | 123.921 | 94.524–95.426 |
| One page after idle | 22.077 | 106.310 | 114.611 | 90.617–91.519 |

The queue bounds sum nine required serial operations: six preparation plugins,
parser work, thumbnail work and metadata. **Preparation queues alone consume
55–65 seconds**, including checks that have little work on these inputs.
Execution through result staging for all nine sums only about **4.8–6.1 seconds**;
result acceptance/coordinator traversal adds further time. OCR request elapsed
is recorded separately in provenance; it includes transport/remote processing,
not just GPU kernels. The old metadata LLM spans take **1.42–1.77 seconds**.
These timings do not support attributing the roughly 90-second gap to model compute.

The mechanism is visible in both journal transitions and source:
[`execution_policy`](../src/documents/job_state.py) queues finite `prepare:*`
operations, [`prepare`](../src/documents/ingestion.py) defers each eligible plugin,
and [`invoke`](../src/documents/operation_journal.py) stops traversal after staging
the selected result. The [processor supervisor](../src/paperless_services/processor.py)
then exits that Django child, waits ten seconds and starts another sweep. Only
one newly available step of the same dependent ingestion chain is advanced in
each sweep. This explains repeated queue gaps and repeated initialization.
The early content marker precedes final search convergence by **8.3–17.4 seconds**.
Older stale search jobs can reach review/failure while newer jobs eventually
publish the correct projection; the complete run is not an all-tasks-success run.

### CPU and I/O: the memory saving has a recurring execution cost

| Phase | Old CPU-seconds | Integrated CPU-seconds |
| --- | ---: | ---: |
| Startup nominal idle, 300 s | 6.51 | 50.34 |
| First ingestion | 10.77 | 43.35 |
| Three-page ingestion | 8.51 | 50.37 |
| Post-feature nominal idle, 300 s | 4.21 | 59.28 |
| Ingestion after idle | 8.98 | 46.90 |
| Final nominal idle, 300 s | 8.00 | 61.42 |

Ingestion totals are **28.26 versus 140.61 CPU-seconds (4.98×)**. The three equal
idle windows total **18.71 versus 171.04 CPU-seconds (9.14×)**. Integrated idle
consumes about 0.17–0.20 of one CPU on this eight-CPU host, despite low median RAM.
One-second process sampling directly attributes at least about **29–31 CPU-seconds
per idle window** to processor sweeps, out of core's approximately 48–49 seconds.
Short process tails and worker churn prevent exact allocation of the remainder;
this is a measured lower contribution, not an exclusive attribution of all CPU.

Ingestion block reads are similar (**56.95 versus 55.80 MiB**); writes are
**6.50 versus 15.49 MiB**. Warm subsequent candidate phases record no block reads.
Host pressure is low, with no OOM events, CPU throttling or sampled swap. The
observations support queue/process-lifecycle costs, not disk saturation, as the
principal explanation. CPU figures include periodic services, HTTP polling and
the database observer; they are not instrumentation-free production estimates.

### Remote resources stay resident

Per-process GPU attribution is stable across all 2,890 available remote samples:
**9,712 MiB OCR**, split between API (2,738 MiB) and VLM (6,974 MiB), plus
**2,060 MiB unrelated BGE**. Whole-GPU allocation is 11,824 MiB, leaving about
52 MiB outside those compute-process allocations. Neither BGE nor the full GPU
total should be charged to OCR alone. Sampled utilization peaks at 44%; short
peaks may fall between one-second samples and utilization/power are whole-device.

Final nominal-idle remote working sets are approximately **1,980 MiB API**,
**3,412 MiB VLM** and **10 MiB gateway** on either side. BGE separately retains
about 4,455 MiB host working set. Those are container working sets, not process
PSS or GPU allocations, and are not added to the local PSS table. The integration
does not remove model-serving residency. Model startup and OpenRouter hosting
resources/charges were not measured; cloud weights behind the shared model name
cannot be proven immutable by this client.

### Interrupted repetition and diagnostic mismatches

The remote stream ends at **20:37:37.812 UTC**, with SSH reporting a connection
closed by the remote host. The next after-idle upload arrives at **20:41:10.348**.
Its three parser attempts fail OCR `/metadata` probes with network-unreachable
errors. The durable job becomes **failed at 20:43:02.843**, **112.495 seconds**
after admission. Both HTTP and SSH remain unreachable at the later diagnostic
check. This establishes dependency/connectivity loss; it does not establish a
shutdown, reboot or specific network cause.

The harness waits to 900 seconds because its document-completion loop does not
stop on a terminal durable-job failure. **900 seconds is a harness deadline, not
OCR compute time.** It also saves API timing/result summaries only after a whole
run succeeds; the interrupted run retains process, memory, job and service-log
evidence, but lacks that final summary. The first matched pair is intact.

The failed job's top-level `PaperlessTask` remains `started` with no completion
date, while its serialized operation exposes `failed` and the error. Consequently
top-level status counts/filtering/duration can disagree with operation details;
the error is not hidden. Successful uploads also log misleading missing-permission
warnings for the core-owned parent `prepare:6` entry, which has no processing
policy while its finite parser child waits. Neither observation proves that the
dedicated account lacks its configured grants. These are diagnostic findings;
no production code was changed.

### Measurement limits and retained evidence

Local memory/process sampling and helpers run in disposable Docker containers;
the remote read-only sampler runs on Cerebrum through SSH.
Complete-service rows alone enter aggregate comparisons. Memory sampling takes
median **22.76 ms old / 11.72 ms integrated** per second. The read-only SQL
observer polls both stacks every 100 ms, but its query is heavier on candidate
journals: **0.614 / 3.144 ms mean**, **1.794 / 8.307 ms maximum**. Its process is
excluded from service totals, while its database execution/connection remains
inside PostgreSQL. Query wall time is not CPU time and cannot simply be subtracted.
Remote observation takes median 38 ms. Initial SSH probes bound relative clocks
roughly between −2.5 ms and +226 ms; the post-run probe is absent after connectivity
loss. Local API, process and database events share the same host clock.

The result establishes one bounded five-page English invoice workload and its
observed tradeoffs. It does not establish repeatability, representative corpus
parity, bulk throughput, long-run leak freedom, a universal 2 GiB active target or
3 GiB peak cap, or successful offline recovery after the interrupted repetition.
The next comparable repetition requires the model endpoint to be reachable.

## Three-version comparison after Redis wakeups (2026-11-06)

**All six measured runs completed: two original, two polling-integrated, and two
Redis-wakeup runs.** Removing periodic sweeps cuts nominal-idle CPU by about 76%
and upload-to-title-search latency by about 59% against polling. Steady resident
RAM remains close to the first integrated test, with higher memory during the
new 60-second child-retention period. Current latency remains about twice that
of the normalized original deployment; staged-result acceptance now dominates
remaining latency, alongside frequent coordinator child replacement. These are
measurements, not additional product fixes.

[Structured data](inference-release-comparison-20261106.json),
[summary plot](inference-release-comparison-summary.png), and
[time-series plot](inference-release-comparison-timeseries.png) retain the details.
The local unpublished raw archive is
`artifacts/release-comparison-20261106.tar.gz`. The November 5 report, JSON, plots
and raw archive above remain unchanged historical evidence.

### Versions and controlled workload

- **Original:** the retained six-service deployment, core `8eb396d9833a56e6147092620b8832e39397f684`.
- **First integrated test / polling:** the same retained image tested on November 5,
  `sha256:6824eec4681c4087dbff484efab2b1da399c7b0e3adfd614ed78afbabfa6fc1a`.
- **Current:** Redis-wakeup commit `55928c30e0b842f4b881944a09d64fef8cd4ef3f`,
  image `sha256:78803e4f1be2e64499067f2c3d25312fd6991d561a0cd76348e35d0209c20533`.
  It was built from an immutable Git archive over the retained inference image;
  dependencies, provider package and frontend were unchanged by that commit.

The run order was original → polling → current → current → polling → original.
Every run used fresh volumes, the same eight-CPU/15.05 GiB local host, pinned
PostgreSQL 18 image `bd5dfa494a5a…`, and pinned Redis 8 image `f245f4c83b84…`.
The historical PostgreSQL image identity was no longer available locally, so all
three versions were rerun on the same currently available image rather than
mixing yesterday's baseline with today's current run.

Each measured run uploaded identical one-page, three-page, then one-page PDFs,
with three 300-second observation windows: startup, after the initial two uploads,
and after the later upload. That is **18 measured uploads / 30 pages** overall;
the nine preflight uploads are excluded. Source hashes, managed fields, OCR,
archives, content search and metadata-title search passed. Original polling was
again normalized from its deployed 300 seconds to ten seconds; metadata
temperature was zero throughout. Model serving stayed warm and available.

This rerun records both the older content-word marker and a separate query for
the current metadata title. Observation windows now begin after title visibility
and download/metadata verification. Terminal upload-task/operation failures stop
promptly, and partial results are saved per upload. These observation changes
apply equally to all three fresh versions; historical timings are not silently
reinterpreted using the stronger marker.

Current retains its shipped **60-second processor idle timeout**. Its recovery
schedule also changes independently from every five minutes to daily at 03:00.
The experiment preserves those version defaults and counts logged recovery
deliveries separately. No model, worker-lifetime or product tuning was added.

![Both repetitions: steady memory, title-search latency and CPU](inference-release-comparison-summary.png)

### Main measurements

RAM, CPU totals and sampled peaks below are medians of two per-run statistics.
Title-search latency is the median of six balanced upload observations per
version. CPU totals include the database observer and scheduled services.

| Metric | Original | First integrated / polling | Current / Redis wake |
| --- | ---: | ---: | ---: |
| Final steady PSS, last 240 s, MiB | 1,351.1 | 398.9 | 408.3 |
| Final nominal-idle PSS mean, all 300 s, MiB | 1,368.8 | 428.6 | 474.0 |
| First 60 s of final idle, PSS median MiB | 1,433.1 | 485.8 | 766.0 |
| Sampled raw-memory peak, MiB | 1,985.5 | 1,040.4 | 1,056.7 |
| Upload → metadata title searchable, s | 22.54 | 118.36 | 48.07 |
| Three ingestion phases, CPU-s | 29.40 | 160.16 | 81.30 |
| Three nominal-idle windows, 900 s total, CPU-s | 16.04 | 174.60 | 41.11 |
| Core CPU during final 240 s of final idle, CPU-s | 0.399 | 36.822 | 0.379 |
| HTTP API readiness from core launch, s | 12.74 | 13.00 | 14.00 |

The current steady PSS is about **70% below the original**, but **9–13 MiB above
polling** across the three quiet windows. Current mean RAM over the full final
window is higher than polling because initialized processing memory remains
resident for the first minute. The first-60 / last-240 split makes this tradeoff
visible; a single median or endpoint snapshot would hide it.

Current uses **49% less ingestion CPU** and **76% less nominal-idle CPU** than
polling. Its active CPU rate is higher, approximately 0.551 CPU-s per observed
second versus polling's 0.445: work is compressed into less elapsed time even
though total CPU falls. Original is approximately 0.432 CPU-s/s for these phases.
Block reads/writes during ingestion are 189.05/7.18 MiB original,
156.62/15.30 MiB polling, and 150.20/14.82 MiB current; cache/order effects remain.

Raw peak ranges overlap: polling **979–1,102 MiB**, current **1,013–1,100 MiB**.
This does not establish a new peak-RAM saving over polling. Both remain below the
original full deployment's **1,735–2,236 MiB** sampled peaks. Core-only lifetime
peaks are larger than the original's because processing resides inside core;
individual service lifetime peaks must not be summed as a simultaneous peak.

![First repetition of all three versions on the same time axis](inference-release-comparison-timeseries.png)

### Why CPU falls, and why early RAM is higher

Each polling run creates **106 observed provider-processing child identities**.
Each current run creates **three**: startup reconciliation, a child shared by the
first two uploads, and a fresh child for the after-idle upload. The same HTTP
child remains throughout each run. This directly verifies burst reuse and later
release without an HTTP/container restart.

In current1, provider PID 594301 handles both initial uploads and the beginning
of post-feature idle; PID 597472 handles the later upload. Current2 similarly
uses 607035 and 609233. Those initialized providers reach about 279–289 MiB PSS
and remain briefly for reuse, then leave after the quiet deadline. A blocked
resident Redis parent remains; it does not repeatedly initialize Django.

Observed provider CPU during idle drops from roughly **29–32 CPU-s per 300-second
window** to **0.02–0.18 CPU-s**. Active provider CPU contributions fall from
48.83/52.01 seconds in polling runs to about 7.55 seconds in current. These
process contributions are approximate lower bounds: one-second sampling misses
short process tails. Whole-container CPU counters include those tails.

Polling logs four scheduled recovery deliveries in each run, versus zero in
original/current. This supports treating the new recovery schedule as another
contributor. It does not account for the disappearance of repeated provider
children; their measured identities and CPU show that mechanism separately.
Logged deliveries do not count unlogged inline startup/reconnection recovery.

The remaining **whole-stack** idle CPU is not all product overhead. In current's
last 240-second final-idle window, core uses a median **0.379 CPU-s**, while
PostgreSQL uses about **10.109 CPU-s**, including the diagnostic observer's
10 queries/second. The observer is outside app-memory totals, but its SQL executes
inside the measured database. Query wall time cannot simply be subtracted from CPU.

### Latency improvement and the remaining coordinator cost

| Upload case, median of two repeats | Original title visible s | Polling title visible s | Current title visible s |
| --- | ---: | ---: | ---: |
| First one-page PDF | 23.088 | 115.795 | 50.961 |
| Immediate three-page PDF | 22.071 | 124.995 | 46.125 |
| One-page PDF after idle | 22.505 | 118.361 | 48.844 |

The nine serial finite operations now wait only **0.03–0.80 seconds in total when
the provider child is warm**, or **1.66–2.59 seconds cold**. Warm per-operation
pickup is below about 0.18 seconds in these observations. Provider reuse works;
the old roughly 90-second processor-polling delay is gone.

Execution through staging totals **3.58–6.35 seconds** for those operations.
However, **staged-result → coordinator acceptance still totals 27.73–36.74 seconds**.
Metadata-job admission also waits **2.42–5.76 seconds** for its initial coordinator
execution, outside those nine finite-operation queue intervals. Core publication,
other handoffs and one-second API polling account for additional elapsed time.

The remaining coordinator churn is directly observable: **14–15 sampled Celery
child identities appear per current upload**, with many child birth intervals
around **5.03–5.04 seconds**. Across each full run current has 53/56 observed task
children, compared with polling's 64/62 and original's 14/16. Source configures
`CELERY_WORKER_MAX_TASKS_PER_CHILD=1`; `ChildDjangoLoader` initializes Django in
each child. Installed Celery's asynchronous pool has a five-second maintenance
timer. Together, the timing and process evidence point to coordinator replacement
and initialization costs. The exact split between pool-maintenance waiting and
Django initialization was not separately profiled.

Removing the long provider queue gap exposes that existing cost: under polling,
coordinator replacement could overlap waiting for the next provider sweep. With
near-immediate provider execution, the next result is ready before a replacement
coordinator child can accept it. Increased acceptance-side time therefore does
not mean Redis pickup became slower. This overlap explanation is an inference
supported by timing/process evidence. Coordinator-side lock waits were not
instrumented, so their contribution cannot be ruled out. Processor-side retries
before a claim are bounded by the short pending-to-claim intervals; they cannot
explain the much longer acceptance-side total.

OCR endpoint elapsed time remains approximately **0.73 / 1.17 / 0.74 seconds**
for the three PDFs in every version. Current metadata-provider execution takes
**1.18–2.68 seconds**, including transport and proposal handling. These are not
GPU-kernel-only measurements and do not explain the remaining tens of seconds.

Title visibility and durable search acceptance remain different markers. For
example, current2's final title is query-visible at **51.990 seconds**, while the
related latest search job is accepted at **55.927 seconds**. All current search
jobs eventually succeed. Each polling run retains three stale-projection
`attention` jobs and six misleading parent-permission warnings; neither appears
in the current runs. This success workload does not retest the earlier terminal
failure/top-level task-status mismatch.

### Remote resources and observation quality

All **6,756 remote samples** span the measured runs without an error. OCR GPU
allocation is constant at **8,920 MiB**; whole-device allocation is 8,967 MiB.
BGE is absent after this boot, rather than being started just for the benchmark.
Remote working sets are about **2,629 MiB API**, **5,368 MiB VLM**, and **16 MiB
gateway**, effectively unchanged across all three fresh versions. These differ
from the previous boot's observations; none of that inter-day difference is
attributed to the local polling change. GPU utilization reaches 85% in sampled
whole-device observations; sub-second peaks and per-process utilization are not
established. Cloud model hosting resources and charges remain unmeasured.

All measured service cgroup reads succeeded, with monotonic sample intervals;
aggregate comparisons exclude partial startup rows before all services exist.
Recorded idle boundaries yield exactly **300 / 60 / 240 samples** for
whole / early / later windows. One baseline2 phase-labelled sample at +300.054
seconds is retained raw but excluded from the completed 300-second window.
Short-lived process-read races remain explicitly recorded rather than fabricated
as zero-memory processes.

Median memory/process observation cost is about **22.4–22.5 ms/second original**,
**12.2 ms polling**, and **11.9–12.5 ms current**. Mean SQL observation latency is
**0.63–0.64 ms original**, **3.35–3.37 ms polling**, and **3.26–3.54 ms current**;
the maximum is 13.90 ms. A check of current leases found 68 invocations with one
attempt/claim each; all 29 transitions actually sampled as leased agree with the
claim time derived from deadline minus timeout. Shorter transitions can occur
between 100 ms observations, so interval bounds remain in the data.

There are no OOM kills or CPU-quota throttling events. Host pressure is not zero:
maximum 10-second CPU-pressure averages reach 9.51%, and memory pressure 0.77%.
Balanced order and two repeats support the large qualitative differences, not
claims of a noise-free benchmark or significance for small timing differences.
Before/after SSH probes bound relative clocks to roughly −3 to +216 ms using
the best round trips; local API, database and process timestamps share one host.

This is a repeated, controlled English synthetic-invoice workload. It does not
establish representative corpus parity, bulk throughput, multi-user behavior,
long-run leak freedom, failure recovery, or universal resource caps. No full test
suite was rerun and no product code was changed for this comparison.

## Remaining release gates

- Representative multi-document/language/correspondent corpus parity against the
  real endpoints; current instrumented coverage is three PDFs/five pages within
  one synthetic English invoice scenario, not a representative corpus.
- Upgrade from an explicitly retained supported previous deployment, including
  legacy/pending jobs and a broader crash/publication fault matrix. Unit tests
  cover many recovery cases but do not replace that deployment exercise.
- Resource/latency evidence now includes two successful runs of each of three
  versions above. The controlled comparison is complete; representative-load
  behavior, capacity and universal resource limits remain unproven.
- Publication of an immutable release artifact and release notes after these
  gates pass. The retained local images are validation candidates only.

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
