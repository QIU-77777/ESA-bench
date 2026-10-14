#!/usr/bin/env bash
# Remove leftovers from interrupted harbor_test runs: trial containers and the
# per-trial task images harbor builds (repository hb__<hash>). A normal run
# deletes its own containers; this covers interrupted runs and stale task
# images after Dockerfile changes. The harness image and base layers are kept.
set -euo pipefail

_containers="$(docker ps -a --filter 'name=hb__' --format '{{.ID}}')"
if [ -n "$_containers" ]; then
    echo "Removing containers: $_containers"
    # Word splitting is intended: one docker command with every ID.
    # shellcheck disable=SC2086
    docker rm -f $_containers
fi

_images="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^hb__' || true)"
if [ -n "$_images" ]; then
    echo "Removing images:"
    echo "$_images" | sed 's/^/  /'
    echo "$_images" | xargs docker rmi -f
fi

echo "Cleanup done."
