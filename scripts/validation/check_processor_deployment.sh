#!/bin/sh
# Run only against throwaway databases, Redis, storage and processor containers.
set -eu
if [ "$#" -ne 1 ]; then
	echo "Usage: $0 CORE_IMAGE_WITH_MATCHING_DEPENDENCIES" >&2
	exit 2
fi
checkout=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
network="paperless-processor-check-$$"
postgres="$network-postgres"
redis="$network-redis"
cleanup() {
	docker rm -f "$postgres" "$redis" >/dev/null 2>&1 || true
	docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
docker network create "$network" >/dev/null
docker run --rm --userns=host -d --name "$postgres" --network "$network" \
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
	-e PAPERLESS_DISABLE_DBHANDLER=true \
	-e PAPERLESS_PROCESSOR_USER=validation \
	-e PAPERLESS_PROCESSOR_CAPABILITIES=search \
	-e PAPERLESS_TASK_WORKERS=2 \
	-e PAPERLESS_PROCESSOR_VALIDATION=1 \
	-e PAPERLESS_SEARCH_PROVIDER=processor_deployment_checks.EndpointSearchProvider \
	-e PYTHONPATH=/tmp/checkout/src:/tmp/checkout/scripts/validation \
-e DJANGO_SETTINGS_MODULE=paperless.settings "$1" -c '
        set -eu
        mkdir -p /tmp/checkout /tmp/validation/data /tmp/validation/media /tmp/validation/consume
        cp -a /source/src /source/scripts /tmp/checkout/
        cd /tmp/checkout/src
        python manage.py migrate --noinput --skip-checks
        python ../scripts/validation/processor_deployment_checks.py
    '
