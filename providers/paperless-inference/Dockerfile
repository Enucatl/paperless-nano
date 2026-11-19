FROM docker.io/library/python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS python-base
WORKDIR /opt/paperless-inference

FROM python-base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
RUN apt-get update && apt-get install --yes --no-install-recommends git
COPY providers/paperless-inference/pyproject.toml providers/paperless-inference/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY providers/paperless-inference/src ./src
RUN uv sync --frozen --no-dev --no-editable && uv pip check

FROM python-base AS runtime
RUN apt-get update \
    && apt-get install --yes --no-install-recommends apt libapt-pkg7.0 zlib1g default-jre-headless ghostscript fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip \
    && install -d -o 1000 -g 1000 /workstation
COPY providers/paperless-inference/install-verapdf.sh /tmp/install-verapdf.sh
RUN /tmp/install-verapdf.sh && rm /tmp/install-verapdf.sh
COPY --from=builder /opt/paperless-inference/.venv ./.venv
ENV PATH="/opt/paperless-inference/.venv/bin:$PATH" \
    PAPERLESS_WORKSTATION_STATE_DIR=/workstation \
    PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["python", "-m", "paperless_inference.workstation"]
