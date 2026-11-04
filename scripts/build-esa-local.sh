#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker info >/dev/null
docker build --build-arg "GO_IMAGE=${ESA_GO_IMAGE:-golang:1.23.12-alpine3.22}" --build-arg "RUNTIME_IMAGE=${ESA_RUNTIME_BASE_IMAGE:-alpine:3.22.1}" -f "$ROOT/runtime/Dockerfile" -t "${AGENT_API_IMAGE:-esa-react-runtime:1.0.0}" "$ROOT/runtime"
docker build --build-arg "PYTHON_IMAGE=${ESA_PYTHON_IMAGE:-python:3.12.9-slim}" -f "$ROOT/environment/Dockerfile" -t "${MOCK_SYSTEM_IMAGE:-esa-local-environment:2.0.0}" "$ROOT"
# 座舱链路：recorder 常构建；默认 agent 与 MCP 镜像由本仓库构建，
# 用户以 COCKPIT_AGENT_IMAGE / COCKPIT_MCP_IMAGE 提供自有镜像时跳过对应构建。
docker build --build-arg "PYTHON_IMAGE=${ESA_PYTHON_IMAGE:-python:3.12.9-slim}" -f "$ROOT/cockpit/Dockerfile" -t "${COCKPIT_RECORDER_IMAGE:-esa-cockpit-recorder:1.0.0}" "$ROOT"
if [ -z "${COCKPIT_AGENT_IMAGE:-}" ]; then
  docker build --build-arg "PYTHON_IMAGE=${ESA_PYTHON_IMAGE:-python:3.12.9-slim}" -f "$ROOT/cockpit/Dockerfile.agent" -t "esa-cockpit-agent:1.0.0" "$ROOT"
else
  echo "COCKPIT_AGENT_IMAGE=$COCKPIT_AGENT_IMAGE 已设置，跳过默认座舱 agent 镜像构建"
fi
if [ -z "${COCKPIT_MCP_IMAGE:-}" ]; then
  docker build --build-arg "PYTHON_IMAGE=${ESA_PYTHON_IMAGE:-python:3.12.9-slim}" -f "$ROOT/cockpit/Dockerfile.mcp" -t "esa-cockpit-mcp:1.0.0" "$ROOT"
else
  echo "COCKPIT_MCP_IMAGE=$COCKPIT_MCP_IMAGE 已设置，跳过默认 MCP 镜像构建"
fi
