#!/usr/bin/env bash
# Build a harness image; every trial mounts it read-only.
# Version, tag and Dockerfile come from harness/<name>/manifest.yaml.
#
# Usage: scripts/build-harness.sh [harness-name]   (default: opencode)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HARNESS="${1:-opencode}"
source scripts/manifest-env.sh "$HARNESS"

case "$HARNESS" in
  opencode)    VERSION_ARG="OPENCODE_CLI_VERSION=${HARNESS_VERSION}" ;;
  pi)          VERSION_ARG="PI_VERSION=${HARNESS_VERSION}" ;;
  claude-code) VERSION_ARG="CLAUDE_CODE_VERSION=${HARNESS_VERSION}" ;;
  *) echo "ERROR: no build-arg mapping for harness '$HARNESS' in scripts/build-harness.sh" >&2; exit 1 ;;
esac

docker build \
  --build-arg "$VERSION_ARG" \
  -t "${HARNESS_IMAGE}" \
  "harness/${HARNESS}"

echo "Built ${HARNESS_IMAGE} (LABEL ${HARNESS}.version=${HARNESS_VERSION})"
