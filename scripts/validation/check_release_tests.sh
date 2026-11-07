#!/bin/sh
# Qualify the release paths with disposable PostgreSQL, Redis and test storage.
set -eu
if [ "$#" -ne 2 ]; then
	echo "Usage: $0 CORE_IMAGE STANDALONE_WORKER_IMAGE" >&2
	exit 2
fi
checkout=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
network="paperless-release-tests-$$"
postgres="$network-postgres"
redis="$network-redis"
cleanup() {
	docker rm -f "$postgres" "$redis" >/dev/null 2>&1 || true
	docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
docker network create "$network" >/dev/null
docker run --rm --userns=host -d --name "$postgres" --network "$network" \
	--tmpfs /var/lib/postgresql \
	-e POSTGRES_PASSWORD=validation -e POSTGRES_DB=validation postgres:18 >/dev/null
docker run --rm --userns=host -d --name "$redis" --network "$network" \
	docker.io/library/redis:8 >/dev/null
docker run --rm --userns=host --network "$network" --entrypoint /bin/sh \
	-v "$checkout:/source:ro" \
	-e PAPERLESS_DBHOST="$postgres" -e PAPERLESS_DBNAME=validation \
	-e PAPERLESS_DBUSER=postgres -e PAPERLESS_DBPASS=validation \
	-e PAPERLESS_REDIS="redis://$redis:6379" \
	-e PAPERLESS_DATA_DIR=/tmp/validation/data \
	-e PAPERLESS_MEDIA_ROOT=/tmp/validation/media \
	-e PAPERLESS_CONSUMPTION_DIR=/tmp/validation/consume \
	-e PAPERLESS_SECRET_KEY=disposable-validation \
	-e PAPERLESS_DISABLE_DBHANDLER=true -e PYTHONDONTWRITEBYTECODE=1 \
	-e DJANGO_SETTINGS_MODULE=paperless.settings \
-e UV_CACHE_DIR=/tmp/uv-cache "$1" -c '
        set -eu
        mkdir -p /tmp/checkout /tmp/validation/data /tmp/validation/media /tmp/validation/consume
        cp -a /source/src /source/pyproject.toml /source/uv.lock /tmp/checkout/
        cd /tmp/checkout
        uv pip freeze --system > /tmp/runtime-constraints.txt
        uv pip install --system --constraint /tmp/runtime-constraints.txt \
            "pytest-django>=4.12,<4.15" pytest-mock pytest-env factory-boy \
            "Faker>=40.36,<40.40" time-machine pytest-httpx pytest-randomly \
            pytest-rerunfailures pytest-sugar pytest-xdist pytest-cov imagehash daphne
        export PYTHONPATH=/tmp/checkout/src
        python -c "import importlib.util; assert importlib.util.find_spec(\"paperless_inference\") is None"
        python -m pytest -o addopts="" \
            src/documents/tests/test_enrichment_contract.py \
            src/documents/tests/test_external_workers.py \
            src/documents/tests/test_rich_enrichment_jobs.py \
            src/documents/tests/test_enrichment_jobs.py \
            src/documents/tests/test_ocr_provenance.py \
            src/documents/tests/test_document_jobs.py \
            src/documents/tests/test_ingestion_jobs.py \
            src/documents/tests/test_job_callbacks.py \
            src/documents/tests/test_deferred_operations.py \
            src/documents/tests/test_parser_operations.py \
            src/documents/tests/test_thumbnail_operations.py \
            src/documents/tests/test_search_operations.py \
            src/documents/tests/test_queued_recovery.py \
            src/documents/tests/test_celery_operations.py \
            src/documents/tests/test_processor_permissions.py \
            src/documents/tests/test_processor_notifications.py \
            src/documents/tests/test_processor_task_outcomes.py \
            src/documents/tests/test_server_processor.py \
            src/documents/tests/test_document_jobs_command.py \
            src/documents/tests/test_scheduler_api.py \
            src/documents/tests/test_task_signals.py \
            src/documents/tests/test_api_tasks.py \
            src/documents/tests/test_document_updates.py \
            src/paperless_worker/tests src/paperless_services/tests
        python src/manage.py makemigrations --check --dry-run
        uv pip check --system
    '
docker run --rm --userns=host --user 0:0 --entrypoint /bin/sh \
	-v "$checkout/providers/paperless-inference:/source:ro" \
-e PYTHONDONTWRITEBYTECODE=1 -e UV_CACHE_DIR=/tmp/uv-cache "$2" -c '
        set -eu
        cp -a /source /tmp/worker
        uv sync --project /tmp/worker --frozen --group dev
        cd /tmp/worker
        uv run --frozen python -c "import importlib.util; assert importlib.util.find_spec(\"paperless\") is None; assert importlib.util.find_spec(\"documents\") is None"
        uv run --frozen pytest -o addopts=""
        uv pip check --python /tmp/worker/.venv/bin/python
    '
