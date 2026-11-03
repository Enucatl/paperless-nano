# Build from the checkout being measured; use the same immutable base for both runs.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY --chown=1000:1000 src/ /usr/src/paperless/src/
COPY docker/rootfs/ /
RUN sed -i '1s|^#!/usr/bin/env python3|#!/command/with-contenv python3|' /usr/src/paperless/src/manage.py

ARG BENCHMARK_REVISION
LABEL org.opencontainers.image.revision=${BENCHMARK_REVISION} \
      local.paperless.benchmark="issue-27"
