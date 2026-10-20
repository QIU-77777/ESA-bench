#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker info >/dev/null
docker build --build-arg "GO_IMAGE=${ESA_GO_IMAGE:-golang:1.23.12-alpine3.22}" --build-arg "RUNTIME_IMAGE=${ESA_RUNTIME_BASE_IMAGE:-alpine:3.22.1}" -f "$ROOT/runtime/Dockerfile" -t "${AGENT_API_IMAGE:-esa-react-runtime:1.0.0}" "$ROOT/runtime"
docker build --build-arg "PYTHON_IMAGE=${ESA_PYTHON_IMAGE:-python:3.12.9-slim}" -f "$ROOT/environment/Dockerfile" -t "${MOCK_SYSTEM_IMAGE:-esa-local-environment:1.0.0}" "$ROOT"
