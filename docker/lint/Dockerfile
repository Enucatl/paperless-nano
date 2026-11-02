FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git libatomic1 \
    && rm -rf /var/lib/apt/lists/*

ARG LINT_UID=1000
ARG LINT_GID=1000
RUN groupadd --gid "${LINT_GID}" lint \
    && useradd --no-log-init --create-home --uid "${LINT_UID}" --gid lint lint \
    && mkdir /setup /work \
    && chown lint:lint /setup /work

USER lint
ENV HOME=/home/lint
ENV UV_TOOL_BIN_DIR=/home/lint/.local/bin
ENV RUFF_CACHE_DIR=/home/lint/.cache/ruff
ENV PATH="/home/lint/.local/bin:${PATH}"
RUN uv tool install prek==0.5.9

WORKDIR /setup
COPY .pre-commit-config.yaml .
RUN git init --quiet && prek install-hooks

WORKDIR /work
RUN git config --global --add safe.directory /work
ENTRYPOINT ["prek"]
CMD ["run", "--all-files", "--show-diff-on-failure", "--color=always"]
