#!/usr/bin/env bash
# Run CI lint hooks directly against the current checkout.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
image_key=$(cat docker/lint/Dockerfile .pre-commit-config.yaml | sha256sum | cut -d ' ' -f 1)
image="paperless-lint:$image_key-$(id -u)-$(id -g)"
if ! docker image inspect "$image" >/dev/null 2>&1; then
	docker build --build-arg "LINT_UID=$(id -u)" --build-arg "LINT_GID=$(id -g)" -f docker/lint/Dockerfile -t "$image" .
fi
exec docker run --rm --userns=host --mount "type=bind,src=$PWD,dst=/work" "$image"
