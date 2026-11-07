FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git default-jre-headless ghostscript fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY providers/paperless-inference/install-verapdf.sh /tmp/install-verapdf.sh
RUN /tmp/install-verapdf.sh && rm /tmp/install-verapdf.sh
WORKDIR /opt/paperless-inference
COPY providers/paperless-inference/pyproject.toml providers/paperless-inference/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY providers/paperless-inference/src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && uv pip check \
    && install -d -o 1000 -g 1000 /workstation
ENV PATH="/opt/paperless-inference/.venv/bin:$PATH" \
    PAPERLESS_WORKSTATION_STATE_DIR=/workstation \
    PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["python", "-m", "paperless_inference.workstation"]
