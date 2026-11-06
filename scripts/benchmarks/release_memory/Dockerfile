# Dependencies, providers and frontend are unchanged by the Redis-wakeup commit.
ARG BASE_IMAGE=paperless-release49-inference:secrets-validation
FROM ${BASE_IMAGE}
COPY --chown=1000:1000 src/ /usr/src/paperless/src/
COPY docker/rootfs/ /
RUN sed -i '1s|^#!/usr/bin/env python3|#!/command/with-contenv python3|' /usr/src/paperless/src/manage.py
ARG BENCHMARK_REVISION
LABEL org.opencontainers.image.revision=${BENCHMARK_REVISION} \
      local.paperless.benchmark="release-memory"
