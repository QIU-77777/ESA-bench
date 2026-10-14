#!/usr/bin/env bash
# Entry point for harbor_test: preflight checks, then `harbor run`.
#
# The harness (opencode, pi, or claude-code) is derived from the job config's image mount;
# its version/label/mount rules come from harness/<name>/manifest.yaml.
#
# Usage:
#   scripts/run.sh                    # preflight + run configs/opencode-harness.yaml
#   scripts/run.sh --dry-run          # preflight + print the plan, run nothing
#   AUTO_BUILD_HARNESS=1 scripts/run.sh   # build the harness image if missing
#   JOB_CONFIG=configs/pi-harness.yaml scripts/run.sh
#   JOB_CONFIG=configs/other.yaml scripts/run.sh
#
# GO_API_KEY must be exported in the calling shell (opencode-go key from
# ~/.local/share/opencode/auth.json); it is injected into the container with
# --agent-env and never stored in the job config. OPENAI_API_KEY and
# ANTHROPIC_API_KEY receive the same value — each harness reads the name its
# own model connection expects (the opencode config ignores both and uses
# {env:GO_API_KEY} inside opencode_config).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
elif [ -n "${1:-}" ]; then
    echo "Usage: scripts/run.sh [--dry-run]" >&2
    exit 1
fi

PY=".venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: $PY not found — create the venv first (see README)." >&2; exit 1; }
JOB_CONFIG="${JOB_CONFIG:-configs/opencode-harness.yaml}"
[ -f "$JOB_CONFIG" ] || { echo "ERROR: job config not found: $JOB_CONFIG" >&2; exit 1; }

fail() { echo "ERROR: $*" >&2; exit 1; }

# ── 1) Job config: mount + agent + gateway fields ──────────────────────
_job_fields="$("$PY" - "$JOB_CONFIG" <<'PY'
import shlex
import sys

import yaml

config = yaml.safe_load(open(sys.argv[1])) or {}
env = config.get("environment") or {}
mount_source = mount_target = ""
for mount in env.get("mounts") or []:
    if isinstance(mount, dict) and mount.get("type") == "image":
        mount_source = str(mount.get("source", ""))
        mount_target = str(mount.get("target", ""))
        break

agents = config.get("agents") or [{}]
agent = agents[0] if agents else {}
kwargs = agent.get("kwargs") or {}
providers = ((kwargs.get("opencode_config") or {}).get("provider")) or {}
gateway_url = ""
for spec in providers.values():
    if isinstance(spec, dict):
        gateway_url = str((spec.get("options") or {}).get("baseURL", ""))
        break
if not gateway_url:
    # Agents without opencode_config (e.g. pi) declare the endpoint as env.
    agent_env = agent.get("env") or {}
    gateway_url = next(
        (
            str(v)
            for k, v in agent_env.items()
            if k in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL")
        ),
        "",
    )

datasets = ", ".join(
    str(d.get("path"))
    for d in ((config.get("datasets") or []) + (config.get("tasks") or []))
    if isinstance(d, dict)
)

for name, value in [
    ("JOB_IMAGE", mount_source),
    ("JOB_TARGET", mount_target),
    ("GATEWAY_URL", gateway_url),
    ("MODEL_NAME", str(agent.get("model_name", ""))),
    ("AGENT_IMPORT", str(agent.get("import_path", ""))),
    ("JOB_DATASETS", datasets),
]:
    print(f"{name}={shlex.quote(value)}")
PY
)" || exit $?
eval "$_job_fields"

# ── 1b) Which harness does this job config mount? ──────────────────────
[ -n "$JOB_IMAGE" ] || fail "$JOB_CONFIG has no image mount under environment.mounts."
_harness="$("$PY" - "$JOB_IMAGE" <<'PY'
import glob
import pathlib
import sys

import yaml

source = sys.argv[1]
last_segment = source.rsplit("/", 1)[-1]
repo = source.rsplit(":", 1)[0] if ":" in last_segment else source
for path in sorted(glob.glob("harness/*/manifest.yaml")):
    manifest = yaml.safe_load(open(path)) or {}
    if str(manifest.get("image_repository", "")) == repo:
        print(pathlib.Path(path).parent.name)
        break
else:
    print(
        f"ERROR: no harness/*/manifest.yaml matches the mount image {source!r}",
        file=sys.stderr,
    )
    sys.exit(1)
PY
)" || exit $?

source scripts/manifest-env.sh "$_harness"
PY="$_HB_PY"

[ "$JOB_IMAGE" = "$HARNESS_IMAGE" ] || fail "$JOB_CONFIG mounts '$JOB_IMAGE' but harness/$_harness/manifest.yaml says '$HARNESS_IMAGE'.
  Keep them in sync (version bump: edit the manifest, scripts/build-harness.sh $_harness, update $JOB_CONFIG)."
[ "$JOB_TARGET" = "$HARNESS_MOUNT_PATH" ] || fail "$JOB_CONFIG mounts the harness at '$JOB_TARGET' but manifest mount_path is '$HARNESS_MOUNT_PATH'."

# ── 2) Harness image exists and its version label matches ─────────────
if docker image inspect "$HARNESS_IMAGE" >/dev/null 2>&1; then
    _label="$(docker image inspect -f '{{index .Config.Labels "'"$HARNESS_NAME"'.version"}}' "$HARNESS_IMAGE")"
    [ "$_label" = "$HARNESS_VERSION" ] || fail "image $HARNESS_IMAGE carries LABEL $HARNESS_NAME.version=$_label but manifest says $HARNESS_VERSION.
  Rebuild: scripts/build-harness.sh $HARNESS_NAME"
else
    if [ "${AUTO_BUILD_HARNESS:-0}" = "1" ]; then
        echo "Harness image missing; building: $HARNESS_IMAGE"
        scripts/build-harness.sh "$HARNESS_NAME"
        docker image inspect "$HARNESS_IMAGE" >/dev/null 2>&1 \
            || fail "build finished but image still missing: $HARNESS_IMAGE"
    else
        fail "harness image missing: $HARNESS_IMAGE
  Build: scripts/build-harness.sh $HARNESS_NAME
  Or:    AUTO_BUILD_HARNESS=1 scripts/run.sh"
    fi
fi

# ── 3) Docker API version supports image mounts (>= 1.48) ──────────────
_server_api="$(docker version --format '{{.Server.APIVersion}}' 2>/dev/null)" \
    || fail "cannot query the Docker daemon (is Docker running?)"
_api_major="${_server_api%%.*}"
_api_minor="${_server_api#*.}"
_api_minor="${_api_minor%%.*}"
case "$_api_major$_api_minor" in
    ''|*[!0-9]*) fail "Docker reported an unexpected API version: '$_server_api'" ;;
esac
if ! { [ "$_api_major" -gt 1 ] || { [ "$_api_major" -eq 1 ] && [ "$_api_minor" -ge 48 ]; }; }; then
    fail "Docker API $_server_api is older than 1.48; harness image mounts need >= 1.48. Upgrade Docker."
fi

# ── 4) Model gateway reachable with the provided key ───────────────────
[ -n "${GO_API_KEY:-}" ] || fail "GO_API_KEY is not set (opencode-go key from ~/.local/share/opencode/auth.json; see README)."
[ -n "$GATEWAY_URL" ] || fail "$JOB_CONFIG has no gateway base URL (opencode_config provider baseURL, or agents[].env OPENAI_BASE_URL / ANTHROPIC_BASE_URL)."
# OpenAI-style configs declare the base URL with /v1 already included;
# ANTHROPIC_BASE_URL omits it (the Claude SDK appends /v1 itself), so try the
# /v1 endpoint first whenever the base URL has no /v1 suffix.
case "$GATEWAY_URL" in
    */v1) _model_urls="${GATEWAY_URL}/models" ;;
    *)    _model_urls="${GATEWAY_URL}/v1/models ${GATEWAY_URL}/models" ;;
esac
_http_code=000
for _url in $_model_urls; do
    _http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        -H "Authorization: Bearer ${GO_API_KEY}" "$_url")" || _http_code=000
    [ "$_http_code" = "200" ] && break
done
[ "$_http_code" = "200" ] || fail "no models endpoint returned HTTP 200 under ${GATEWAY_URL} (tried: $_model_urls; last: HTTP $_http_code) — key invalid or gateway down."

# ── 5) Dry run: report the resolved plan, run nothing ──────────────────
if [ "$DRY_RUN" = "1" ]; then
    echo "Preflight OK. Resolved plan:"
    echo "  job config : $JOB_CONFIG"
    echo "  tasks      : $JOB_DATASETS"
    echo "  agent      : $AGENT_IMPORT  (model: $MODEL_NAME)"
    echo "  harness    : $HARNESS_IMAGE  (v$HARNESS_VERSION, read-only mount at $HARNESS_MOUNT_PATH)"
    echo "  gateway    : $GATEWAY_URL  (GO_API_KEY ending ${GO_API_KEY: -4})"
    echo "  docker api : $_server_api"
    PYTHONPATH="$PWD" .venv/bin/harbor run -c "$JOB_CONFIG" --print-config >/dev/null 2>&1 \
        && echo "  harbor     : config accepted (schema + agent import path)" \
        || fail "harbor rejected the job config; run manually:
  PYTHONPATH=\$PWD .venv/bin/harbor run -c $JOB_CONFIG --print-config"
    exit 0
fi

# ── 6) Run ─────────────────────────────────────────────────────────────
PYTHONPATH="$PWD" .venv/bin/harbor run -c "$JOB_CONFIG" \
    --agent-env "GO_API_KEY=${GO_API_KEY}" \
    --agent-env "OPENAI_API_KEY=${GO_API_KEY}" \
    --agent-env "ANTHROPIC_API_KEY=${GO_API_KEY}" \
    -y
