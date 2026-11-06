# Reuse pinned dependencies, providers and frontend for the Celery comparison.
ARG BASE_IMAGE=sha256:6824eec4681c4087dbff484efab2b1da399c7b0e3adfd614ed78afbabfa6fc1a
FROM ${BASE_IMAGE}
COPY --chown=1000:1000 src/ /usr/src/paperless/src/
COPY docker/rootfs/ /
RUN rm -rf /etc/s6-overlay/s6-rc.d/svc-processor \
    /etc/s6-overlay/s6-rc.d/user/contents.d/svc-processor \
    /usr/src/paperless/src/paperless_services/processor.py \
    /usr/src/paperless/src/paperless_services/processor_queue.py \
    /usr/src/paperless/src/documents/management/commands/document_processor.py \
    && sed -i '1s|^#!/usr/bin/env python3|#!/command/with-contenv python3|' /usr/src/paperless/src/manage.py
ARG BENCHMARK_REVISION
LABEL org.opencontainers.image.revision=${BENCHMARK_REVISION} \
      local.paperless.benchmark="release-memory"
