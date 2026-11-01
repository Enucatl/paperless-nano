FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN uv tool install prek==0.5.9
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /setup
COPY .pre-commit-config.yaml .
RUN git init --quiet && prek install-hooks

WORKDIR /work
RUN git config --global --add safe.directory /work
ENTRYPOINT ["prek"]
CMD ["run", "--all-files", "--show-diff-on-failure", "--color=always"]
