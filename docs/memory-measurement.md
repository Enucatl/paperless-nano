# Measured residency reduction (#27)

Running document-language inference on demand reduced post-feature Paperless PSS
by **59.21 MiB (14.7%)** in three comparable runs per variant. The six-service PSS
total fell **59.12 MiB (5.0%)**. Metadata requests took approximately **0.31 seconds
longer**. Startup residency was essentially unchanged. This is a material core
reduction from a small execution change, with a modest whole-stack saving.

The comparison ran on **2026-11-03, 13:40–16:39 UTC**. All six workloads passed;
the four repeat runs also passed injected retry, recovery and stale-result checks
after the measured idle periods. The [sanitized results](memory-measurement-results.json)
retain individual phases, process counts, latencies, peak bounds, outcomes,
image/source identities and measurement limitations. The
[reproduction harness](../scripts/benchmarks/idle_memory/README.md) contains the
workload and isolated runtime checks. Private documents, settings and service logs
are not included in the repository.

## Results

Values below are medians of three run endpoints, except anonymous/raw memory,
which uses the median of each run's 300-second observation window first.
PSS, cgroup anonymous memory and raw cgroup totals are distinct measurements.

| Final post-feature observation            | Baseline MiB | On-demand inference MiB |         Reduction |
| ----------------------------------------- | -----------: | ----------------------: | ----------------: |
| Web application worker PSS                |       267.77 |                  208.41 | 59.36 MiB / 22.2% |
| Paperless container process PSS           |       403.83 |                  344.62 | 59.21 MiB / 14.7% |
| All six services' process PSS             |     1,189.01 |                1,129.89 |  59.12 MiB / 5.0% |
| All six services' cgroup anonymous memory |       950.72 |                  891.55 |  59.17 MiB / 6.2% |
| All six services' raw `memory.current`    |     1,279.58 |                1,216.07 |  63.51 MiB / 5.0% |

Anonymous memory corroborates the PSS reduction. The raw-total difference also
includes changes in file-cache ownership and kernel accounting, and is not all
attributed to the implementation. Baseline run 3 illustrates this: its raw total
was much lower than earlier baselines despite similar application-worker PSS.

| Run         | Final Paperless PSS MiB | Final six-service PSS MiB | Final raw median MiB | Final raw range MiB |
| ----------- | ----------------------: | ------------------------: | -------------------: | ------------------: |
| Baseline 1  |                  408.48 |                  1,195.70 |             1,280.86 |   1,279.33–1,366.89 |
| Baseline 2  |                  403.49 |                  1,189.01 |             1,279.58 |   1,278.10–1,366.68 |
| Baseline 3  |                  403.83 |                  1,179.62 |             1,147.17 |   1,145.64–1,232.90 |
| On-demand 1 |                  342.53 |                  1,127.52 |             1,150.16 |   1,148.50–1,235.86 |
| On-demand 2 |                  347.67 |                  1,131.66 |             1,225.63 |   1,223.84–1,331.08 |
| On-demand 3 |                  344.62 |                  1,129.89 |             1,216.07 |   1,213.55–1,302.42 |

The application-worker PSS progression, using medians of three endpoints, was:

| Phase                                    | Baseline MiB | On-demand inference MiB |
| ---------------------------------------- | -----------: | ----------------------: |
| Startup                                  |       159.88 |                  160.09 |
| After metadata/preview/search            |       250.23 |                  188.00 |
| After all features and final observation |       267.77 |                  208.41 |

Paperless startup PSS was **296.80 versus 295.87 MiB**; six-service startup PSS
was **1,066.49 versus 1,065.29 MiB**. No startup saving is claimed. Other operations
still retain memory, so the final plateau remains above startup in both variants.

| Run         | Sampled aggregate peak MiB, lower bound | Sum of individual peaks MiB, upper bound | API readiness seconds | First / subsequent median metadata seconds |
| ----------- | --------------------------------------: | ---------------------------------------: | --------------------: | -----------------------------------------: |
| Baseline 1  |                                1,406.02 |                                 1,449.38 |                 12.78 |                              0.229 / 0.028 |
| Baseline 2  |                                1,404.08 |                                 1,445.94 |                 11.77 |                              0.243 / 0.028 |
| Baseline 3  |                                1,277.36 |                                 1,484.45 |                 11.77 |                              0.268 / 0.028 |
| On-demand 1 |                                1,306.91 |                                 1,436.49 |                 13.80 |                              0.493 / 0.334 |
| On-demand 2 |                                1,335.14 |                                 1,389.26 |                 12.27 |                              0.465 / 0.330 |
| On-demand 3 |                                1,342.43 |                                 1,384.12 |                 11.77 |                              0.451 / 0.335 |

Median API readiness was **11.77 versus 12.27 seconds**, measured from Paperless
container start after database preparation. The first metadata request median
was **0.243 versus 0.465 seconds**; subsequent medians were **0.028 versus 0.334**.
Initial upload completion took **3.12–3.15 seconds** in baselines and
**2.14–3.13 seconds** with on-demand inference. Uploads following the five-minute
observation took **1.10–3.27** and **3.12–3.13 seconds**, respectively. Task polling
has one-second resolution, so these are not precision throughput comparisons.

Language-helper exit releases its profiles before the metadata response returns.
Existing Celery retirement remains responsible for processing-worker memory:
logs showed retirement about **32 seconds after reprocessing** in the baselines
and **22–23 seconds** in the candidates. After the later upload, timers and
scheduled work overlapped; the samples include a delayed large memory drop at
130 seconds in baseline 2. This is not evidence of a faster lifecycle policy.

Every 300-second window included scheduled activity. Final PSS captures all had
17 Paperless processes, with no language helper or Celery child remaining.
Intermediate metadata snapshots in baseline 2 and candidate 1 included a Celery
child; their container PSS is not directly comparable as idle memory. Eleven
short-lived PostgreSQL process exits caused intermediate PSS read races; all
final captures were complete. These are observed five-minute windows, not proof
of uninterrupted idle or multi-day behavior.

## Implementation and alternatives

`documents.language.detect_language` runs the existing `langdetect` algorithm
in a single short-lived Python process. Its input is document text over a private
UTF-8 pipe; its output must be a language code. The helper does not initialize
Django, mutate documents, publish files or enqueue work. The standard subprocess
implementation handles waiting, a 30-second timeout, termination and reaping.
There is no resident pool, new dependency, process recycler or custom scheduler.

The metadata endpoint retains permission checks, effective-version selection,
metadata caching and its existing English fallback. Empty content needs no
helper. Inference failures, including timeout, retain that fallback. Concurrent
requests can temporarily overlap helper memory; no new memory cap is imposed.
Django, Celery, search, OCR implementations, database choices and workflows remain.

The measured target was the language-profile graph loaded by ordinary document
metadata requests. An isolated attribution check retained approximately 59 MiB
after language detection, including after garbage collection. PDF metadata
libraries also load on this path and remain resident.

Rejected alternatives were measured or reviewed before choosing the helper:

- `PYTHONMALLOC=mimalloc` was already available, but a complete matched exploratory
  run increased final Paperless PSS by approximately 20 MiB. It was not adopted.
- A per-call `DetectorFactory` initially released much of the graph, but after
  60 calls and two concurrent calls its retained PSS exceeded the global-factory
  baseline. Object lifetime alone did not reliably release allocator residency.
- Granian worker recycling would interrupt websockets; the UI lacks automatic
  reconnect and complete missed-event recovery. It was not justified by the
  measured scope and was not implemented.
- Lean Celery, the API watcher and the API scheduler were already enabled on
  Atlas. Their existing savings are not credited to this change. Classification
  is disabled there, so isolating classifier inference would not help this profile.

The first two exploratory full runs omitted document metadata and therefore were
not accepted as the final workload baseline. The accepted six runs all use the
expanded request sequence; the first pair predates language-result recording and
the post-measurement fault checks. The four repeats used the retained harness.
Request semantics match across all six runs, and all five recorded language-result
maps agree.

## Scope and reproduction manifest

The six measured local services are Paperless (web server, lean Celery parent and
children, API watcher and Supercronic), PostgreSQL, Redis, the AI companion, its
webhook listener and Phoenix. External GPU inference and shared reverse-proxy/auth
infrastructure are excluded. The controller and setup/verification helpers are
also excluded; language helpers are children of the measured Paperless cgroup.

The local host was Linux **7.0.0-42-generic**, x86-64, **8 logical CPUs**, Docker
**29.9.2**, cgroup v2, with **16,155,926,528 bytes** host RAM. No new memory limit
was applied. Both variants use the same deployed runtime and compiled frontend:
Python **3.14.9**, Django **5.2.17**, Celery **5.6.3**, Granian **2.8.2**, Tantivy
**0.26.2**, psycopg **3.3.4**, PostgreSQL **18**, Redis **8**. Dependencies match
`uv.lock` SHA-256 `980b5eeb06c6d564fc96ff8da8a7ac03eba7b3b5b2a91a354be0fec610a22a1b`.

The runtime base is deployed revision
`8eb396d9833a56e6147092620b8832e39397f684`, original registry digest
`sha256:84bdb5d23574800ed28dbea02dc2a8b361a9472643399c9c947767163a03275b`.
The baseline overlays branch commit `f51ce979a`. The candidate changes only the
language helper and its metadata caller; their exact hashes and both built image
IDs are in the results artifact. The four repeats recorded immutable IDs for all
six services and confirm unchanged sidecars. The first pair recorded tags; their
retained build identities were checked afterward. Source and dependencies were
not changed during the accepted runs.

One web worker, one task worker, two OCR threads, German OCR configuration and
Europe/Zurich scheduling match the captured deployment. Local OCR, built-in
classification and built-in AI are disabled in both variants, as on Atlas.
No feature flag was disabled to obtain this reduction. Existing autoscaling,
watcher and schedules remain enabled.

The private sample contains **14 documents**: 12 PDFs spanning **1–203 pages**,
one PNG and one JPEG. Its 41 original/archive/thumbnail files total
**36,654,299 bytes**. Twelve documents are seeded, and two distinct one-/ten-page
PDFs are reserved for real API ingestion. Saved parser settings are retained;
identities, permissions, tags, mail accounts and external-effect workflows are
replaced with isolated test state and one final-state indexing workflow.

Atlas has 2,348 active documents, 15,468,890 content characters and 7,498 pages.
The seed has 343,302 characters and 420 pages. It exercises large individual
documents but not full-corpus metadata/index/cache size. PostgreSQL, companion
chat and Phoenix history start fresh. The clone blocks external access, so
successful local ingestion does not measure the external AI pipeline or OCR
peak. The initial excess corpus copy was deleted when the user clarified the
bounded-copy requirement; no further document copying occurred. Atlas was not
modified.

The previously reported **300 MB** has no verified matching scope. A read-only
warmed Atlas snapshot summed to **1,497.89 MiB** across six cgroups, with a later
reading of 1,428.84 MiB; these are not stable baseline measurements. Its warmed
web worker was approximately 445 MiB PSS. Neither figure is treated as a matched
before/after result for this small-sample experiment.

## Method and verification

Runs started fresh application containers and fresh database/media volumes from
the same bounded selection. Host filesystem caches were warm; this was not a
cold-host reboot experiment. Run order was baseline 1, candidate 1, baseline 2,
candidate 2, candidate 3, baseline 3.

The sampler records each service's `memory.current`, `memory.peak`, anonymous and
file memory, inactive-file memory and memory events every second. Concurrent
`memory.current` values are summed without double-counting parent/child cgroups.
PSS comes from host `/proc/<pid>/smaps_rollup` at phase boundaries. A one-second
aggregate peak is a lower bound; summing individual lifetime peaks is an upper
bound, not an observed simultaneous peak. All memory-limit/OOM event counters
remained zero. No universal 2 GB active target or 3 GB aggregate cap is established.
Atlas's existing 3 GiB Paperless-container limit is a different scope.

Each accepted run browsed/downloaded all seeded documents; retrieved detail,
metadata and preview; repeated cached metadata; downloaded the largest selected
original concurrently twice; asserted a content-search hit; requested cold/warm
suggestions; checked workflow-final metadata in search; ingested a reserved PDF;
reprocessed an existing PDF; observed five minutes; ingested the second PDF; and
observed another five minutes. Final document count was 14. All assertions passed.
Scheduled task records show **15 successful mail checks** (empty mail accounts)
and **three successful workflow checks** across the six runs.

After sampling stopped, each of the four repeat runs used the same live isolated
PostgreSQL/Redis/lean-worker stack to verify:

1. An admitted parser failure retains its input and leaves permanent files intact;
   the real Celery recovery sweep redispatches it and the second attempt succeeds.
2. A committed result with unfinished completion resumes through `document_jobs`
   without reparsing or changing the committed publication.
3. A newer content edit causes a prepared result to be rejected, preserving that
   edit, permanent file hashes and the retained recovery input.

These checks do not acknowledge or blindly replay uncertain ingestion effects.
Periodic recovery receipt logs alone are not counted as proof of completion;
the injected checks verify durable terminal state. Their memory is outside the
measurement phases. The entire disposable clone is removed after verification.

**386 focused tests passed**, including API behavior, Unicode transport,
concurrent inference, permissions/version selection, timeouts, job retries,
stale-result rejection, recovery and normal/lean-worker compatibility. Exact
selections and environment are recorded in
[document-operations.md](document-operations.md#language-detection-residency-27).
The full suite, MariaDB, external inference, full production histories and
multi-day idle were not tested. The proposed peak cap remains unenforced.

Changed-file hooks and migration checks passed. Per-trial containers, networks
and volumes were removed after every run. After the sanitized evidence was
retained, the private snapshot and verification volumes and temporary runtime
images were removed. The preexisting workflow changes and historical memory
report diff remain uncommitted and unchanged. Nothing was pushed or deployed.
