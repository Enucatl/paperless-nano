# Build the matching core commit first; supply its immutable local tag or digest.
ARG PAPERLESS_IMAGE
FROM ${PAPERLESS_IMAGE}
# The inherited s6 entrypoint initializes volumes before dropping privileges.
# hadolint ignore=DL3002
USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
COPY providers/paperless-inference/install-verapdf.sh /tmp/install-verapdf.sh
RUN /tmp/install-verapdf.sh && rm /tmp/install-verapdf.sh
COPY providers/paperless-inference /opt/paperless-inference
# Inexact preserves core dependencies; pip check rejects incompatible installations.
RUN UV_PROJECT_ENVIRONMENT=/usr/local uv sync --project /opt/paperless-inference --frozen --no-dev --inexact --no-editable \
    && uv pip check --system
