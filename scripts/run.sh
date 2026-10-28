#!/usr/bin/env bash
# Entry point for harbor_test: preflight checks, then `harbor run`.
#
# The harness (opencode, pi, or claude-code) comes from the job config's
# `harness:` key; its version/label/mount rules come from
# harness/<name>/manifest.yaml, and the job's image mount must match it.
#
# Usage:
#   scripts/run.sh --bench hello --harness pi --model deepseek/deepseek-flash
#   scripts/run.sh --bench hello --harness pi --model deepseek/deepseek-flash --dry-run
#   scripts/run.sh --job deepseek-pi-hello
#   AUTO_BUILD_HARNESS=1 scripts/run.sh --job deepseek-pi-hello
#
# Model connection comes from .env (one <PREFIX>_* group per provider, api
# keys included), selected by the job config's `model: provider/model-id`:
# this script sources .env, validates the selected model ID, and creates a
# resolved temporary job config. Harbor receives the real provider/model-id;
# connection values enter the container as OPENAI_API_KEY and
# ANTHROPIC_API_KEY — each harness reads the name its own protocol expects.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DRY_RUN=0
BENCH_SLUG=""
BENCH_FILE=""
HARNESS_SLUG=""
MODEL_SLUG=""
JOB_SLUG=""
SCORER_CONFIG=""
JOB_CONFIG="${JOB_CONFIG:-}"
JOB_NAME="${JOB_NAME:-$(date '+%Y-%m-%d__%H-%M-%S')}"

usage() {
    cat <<'EOF'
Usage:
  scripts/run.sh --bench <bench> --harness <harness> --model <provider/model-id> [options]
  scripts/run.sh --bench-file <path> --harness <harness> --model <provider/model-id> [options]
  scripts/run.sh --job <job-slug> [options]

Options:
  --bench-file    Rendered bench YAML (tasks point at a generated dataset)
  --dry-run       Validate and print the resolved plan, then exit
  --job-name      Harbor result directory name
  --help          Show this help

Examples:
  scripts/run.sh --bench hello --harness pi --model deepseek/deepseek-flash
  scripts/run.sh --job deepseek-pi-hello --dry-run
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --bench) [ "$#" -ge 2 ] || { echo "ERROR: --bench needs a value" >&2; exit 1; }; BENCH_SLUG="$2"; shift 2 ;;
        --bench-file) [ "$#" -ge 2 ] || { echo "ERROR: --bench-file needs a value" >&2; exit 1; }; BENCH_FILE="$2"; shift 2 ;;
        --harness) [ "$#" -ge 2 ] || { echo "ERROR: --harness needs a value" >&2; exit 1; }; HARNESS_SLUG="$2"; shift 2 ;;
        --model) [ "$#" -ge 2 ] || { echo "ERROR: --model needs a value" >&2; exit 1; }; MODEL_SLUG="$2"; shift 2 ;;
        --job) [ "$#" -ge 2 ] || { echo "ERROR: --job needs a value" >&2; exit 1; }; JOB_SLUG="$2"; shift 2 ;;
        --job-name) [ "$#" -ge 2 ] || { echo "ERROR: --job-name needs a value" >&2; exit 1; }; JOB_NAME="$2"; shift 2 ;;
        --scorer-config) [ "$#" -ge 2 ] || { echo "ERROR: --scorer-config needs a value" >&2; exit 1; }; SCORER_CONFIG="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

case "$JOB_NAME" in
    ''|*:*|*/*) fail "--job-name cannot contain ':' or '/' because Docker bind mounts use the job path." ;;
esac

PY=".venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: $PY not found — create the venv first (see README)." >&2; exit 1; }
[ -f .env ] || { echo "ERROR: .env not found (copy .env.example and fill the keys)." >&2; exit 1; }
fail() { echo "ERROR: $*" >&2; exit 1; }
set -a
source .env
set +a

if [ -n "$JOB_SLUG" ]; then
    [ -z "$JOB_CONFIG" ] || fail "do not combine --job with JOB_CONFIG"
    [ -z "$BENCH_SLUG$BENCH_FILE$HARNESS_SLUG$MODEL_SLUG" ] || fail "do not combine --job with --bench/--bench-file/--harness/--model"
    JOB_SOURCE_CONFIG="configs/jobs/${JOB_SLUG}.yaml"
    [ -f "$JOB_SOURCE_CONFIG" ] || { echo "ERROR: job config not found: $JOB_SOURCE_CONFIG" >&2; exit 1; }
    _job_select="$($PY - "$JOB_SOURCE_CONFIG" <<'PY'
import shlex
import sys
import yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
for key in ("bench", "harness", "model"):
    print(f"{key.upper()}_SLUG={shlex.quote(str(c.get(key) or ''))}")
PY
)" || exit $?
    eval "$_job_select"
elif [ -n "$BENCH_SLUG$BENCH_FILE$HARNESS_SLUG$MODEL_SLUG" ]; then
    { [ -n "$BENCH_SLUG" ] || [ -n "$BENCH_FILE" ]; } || fail "--bench or --bench-file is required when using manual selection"
    [ -z "$BENCH_SLUG" ] || [ -z "$BENCH_FILE" ] || fail "do not combine --bench with --bench-file"
    [ -n "$HARNESS_SLUG" ] || fail "--harness is required when using manual selection"
    [ -n "$MODEL_SLUG" ] || fail "--model is required when using manual selection"
else
    BENCH_SLUG="hello"
    HARNESS_SLUG="opencode"
    MODEL_SLUG="go/mimo-v2.6-flash"
fi

JOB_CONFIG="$(mktemp "${TMPDIR:-/tmp}/harbor-test-source.XXXXXX")"
trap 'rm -f "$JOB_CONFIG"' EXIT
RENDER_ARGS=(
    --harness "$HARNESS_SLUG"
    --model "$MODEL_SLUG"
    --output "$JOB_CONFIG"
)
if [ -n "$BENCH_FILE" ]; then
    RENDER_ARGS+=(--bench-file "$BENCH_FILE")
else
    RENDER_ARGS+=(--bench "$BENCH_SLUG")
fi
[ -n "${JOB_SOURCE_CONFIG:-}" ] && RENDER_ARGS+=(--job-config "$JOB_SOURCE_CONFIG")
"$PY" scripts/render-job.py "${RENDER_ARGS[@]}"

VERIFIER_ARGS=()
if [ -n "$SCORER_CONFIG" ]; then
    VERIFIER_ARGS=(
        --verifier harbor_test_verifier:ConsoleVerifier
        --verifier-kwarg "scorer_config=$SCORER_CONFIG"
    )
fi

# ── 1) Combination declaration + .env group mapping ─────────────────────
# run.sh reads both keys; harbor's JobConfig drops them (they are not
# harbor fields, and model_validate accepts unknown keys).
_combo="$("$PY" - "$JOB_CONFIG" <<'PY'
import shlex
import sys

import yaml

config = yaml.safe_load(open(sys.argv[1])) or {}
for key in ("model", "harness"):
    print(f"{key.upper()}_SLUG={shlex.quote(str(config.get(key) or ''))}")
PY
)" || exit $?
eval "$_combo"
[ -n "$MODEL_SLUG" ] || fail "$JOB_CONFIG has no 'model:' value (expected provider/model-id)."
[ -n "$HARNESS_SLUG" ] || fail "$JOB_CONFIG has no 'harness:' value (run.sh reads it to pick harness/<harness>/manifest.yaml)."
case "$MODEL_SLUG" in
    */?*) ;;
    *) fail "model: '$MODEL_SLUG' must use provider/model-id format." ;;
esac
MODEL_PROVIDER="${MODEL_SLUG%%/*}"
MODEL_ID="${MODEL_SLUG#*/}"
case "$MODEL_PROVIDER" in
    ''|[0-9]*|*[!A-Za-z0-9_]*) fail "model provider '$MODEL_PROVIDER' must start with a letter and contain only letters, digits or underscores." ;;
esac
[ -f .env ] || fail ".env not found (connection values and api keys; see README)."
# Exporting here feeds Harbor's model-connection resolution and the
# --agent-env injection in step 8.
set -a
source .env
set +a

# Map the selected provider's group to the fixed names. The key stays a
# plain shell variable: only run.sh consumes it (probe + --agent-env), so it
# never enters harbor's host-side environment. Values required by this
# harness's protocol must be present; the other protocol's pair is optional.
_prefix="$(printf '%s' "$MODEL_PROVIDER" | tr '[:lower:]' '[:upper:]')"
case "$HARNESS_SLUG" in
    claude-code) _required="ANTHROPIC_MODEL_ID ANTHROPIC_BASE_URL API_KEY" ;;
    *)           _required="OPENAI_MODEL_ID OPENAI_BASE_URL API_KEY" ;;
esac
for _name in OPENAI_MODEL_ID ANTHROPIC_MODEL_ID OPENAI_BASE_URL ANTHROPIC_BASE_URL API_KEY; do
    _src="${_prefix}_${_name}"
    eval "_value=\${${_src}:-}"
    if [ -z "$_value" ]; then
        case " $_required " in
            *" $_name "*) fail "model '$MODEL_SLUG' requires $_src in .env (missing or empty)." ;;
        esac
        continue
    fi
    if [ "$_name" = "API_KEY" ]; then
        API_KEY="$_value"
    else
        eval "export ${_name}=\"\$_value\""
    fi
done

if [ "$HARNESS_SLUG" = "claude-code" ]; then
    _configured_model_id="$ANTHROPIC_MODEL_ID"
    _configured_model_var="${_prefix}_ANTHROPIC_MODEL_ID"
else
    _configured_model_id="$OPENAI_MODEL_ID"
    _configured_model_var="${_prefix}_OPENAI_MODEL_ID"
fi
[ "$MODEL_ID" = "$_configured_model_id" ] || fail "$JOB_CONFIG declares model: $MODEL_SLUG but .env has $_configured_model_var=$_configured_model_id."

# ── 2) Job config: mount + agent fields ─────────────────────────────────
_job_fields="$("$PY" - "$JOB_CONFIG" <<'PY'
import os
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

datasets = ", ".join(
    str(d.get("path"))
    for d in ((config.get("datasets") or []) + (config.get("tasks") or []))
    if isinstance(d, dict)
)

for name, value in [
    ("JOB_IMAGE", mount_source),
    ("JOB_TARGET", mount_target),
    ("AGENT_IMPORT", str(agent.get("import_path", ""))),
    ("JOB_DATASETS", datasets),
]:
    print(f"{name}={shlex.quote(value)}")
PY
)" || exit $?
eval "$_job_fields"
MODEL_NAME="$MODEL_SLUG"

# Harbor snapshots the config before agent install runs. Render the selected
# provider/model into a temporary job config so config.json records the real
# run identity while the authored YAML stays reusable.
RUNTIME_JOB_CONFIG="$("$PY" - <<'PY'
import os
import tempfile

handle, path = tempfile.mkstemp(prefix="harbor-test-job.", suffix=".yaml")
os.close(handle)
print(path)
PY
)"
trap 'rm -f "$RUNTIME_JOB_CONFIG"' EXIT
"$PY" - "$JOB_CONFIG" "$RUNTIME_JOB_CONFIG" "$MODEL_PROVIDER" "$MODEL_ID" "$HARNESS_SLUG" <<'PY'
import sys

import yaml

source_path, output_path, provider, model_id, harness = sys.argv[1:]
config = yaml.safe_load(open(source_path)) or {}
config.pop("model", None)
config.pop("harness", None)
agent = config["agents"][0]
agent["model_name"] = f"{provider}/{model_id}"

if harness == "opencode":
    providers = agent["kwargs"]["opencode_config"]["provider"]
    gateway = providers.pop("gateway")
    gateway["name"] = provider
    providers[provider] = gateway

with open(output_path, "w") as output:
    yaml.safe_dump(config, output, sort_keys=False)
PY

# ── 3) `harness:` must match import_path, manifest and image mount ─────
case "$HARNESS_SLUG:$AGENT_IMPORT" in
    opencode:*MountedOpenCode|pi:*MountedPi|claude-code:*MountedClaudeCode) ;;
    *) fail "$JOB_CONFIG mismatch: harness: $HARNESS_SLUG vs agents[0].import_path $AGENT_IMPORT.
  Expected pairs: opencode/MountedOpenCode, pi/MountedPi, claude-code/MountedClaudeCode." ;;
esac
[ -n "$JOB_IMAGE" ] || fail "$JOB_CONFIG has no image mount under environment.mounts."
_harness="$HARNESS_SLUG"

source scripts/manifest-env.sh "$_harness"
PY="$_HB_PY"

[ "$JOB_IMAGE" = "$HARNESS_IMAGE" ] || fail "$JOB_CONFIG mounts '$JOB_IMAGE' but harness/$_harness/manifest.yaml says '$HARNESS_IMAGE'.
  Keep them in sync (version bump: edit the manifest, scripts/build-harness.sh $_harness, update $JOB_CONFIG)."
[ "$JOB_TARGET" = "$HARNESS_MOUNT_PATH" ] || fail "$JOB_CONFIG mounts the harness at '$JOB_TARGET' but manifest mount_path is '$HARNESS_MOUNT_PATH'."

# ── 4) Harness image exists and its version label matches ─────────────
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

# ── 5) Docker API version supports image mounts (>= 1.48) ──────────────
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

# ── 6) Model gateway reachable with the provided key ───────────────────
[ -n "${API_KEY:-}" ] || fail "API_KEY is not set (model: $MODEL_SLUG → ${_prefix}_API_KEY in .env)."
# Probe the endpoint this harness speaks (values from the selected .env
# group; the gateway binds models to one protocol per endpoint). OpenAI-style
# base URLs carry /v1 already; ANTHROPIC_BASE_URL omits it (the Claude SDK
# appends /v1 itself), so try the /v1 endpoint first whenever the base URL
# has no /v1 suffix.
if [ "$_harness" = "claude-code" ]; then
    GATEWAY_URL="${ANTHROPIC_BASE_URL:-}"
else
    GATEWAY_URL="${OPENAI_BASE_URL:-}"
fi
[ -n "$GATEWAY_URL" ] || fail "harness $_harness has no base URL (see the ${_prefix}_ group in .env)."
case "$GATEWAY_URL" in
    */v1) _model_urls="${GATEWAY_URL}/models" ;;
    *)    _model_urls="${GATEWAY_URL}/v1/models ${GATEWAY_URL}/models" ;;
esac
_http_code=000
for _url in $_model_urls; do
    _http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        -H "Authorization: Bearer ${API_KEY}" "$_url")" || _http_code=000
    [ "$_http_code" = "200" ] && break
done
[ "$_http_code" = "200" ] || fail "no models endpoint returned HTTP 200 under ${GATEWAY_URL} (tried: $_model_urls; last: HTTP $_http_code) — key invalid or gateway down."

# ── 7) Dry run: report the resolved plan, run nothing ──────────────────
if [ "$DRY_RUN" = "1" ]; then
    echo "Preflight OK. Resolved plan:"
    echo "  job name   : $JOB_NAME"
    echo "  job config : $JOB_CONFIG"
    echo "  bench      : ${BENCH_SLUG:-$BENCH_FILE}"
    echo "  model      : $MODEL_SLUG  (.env ${_prefix}_* group)"
    echo "  tasks      : $JOB_DATASETS"
    echo "  agent      : $AGENT_IMPORT  (model: $MODEL_NAME)"
    echo "  harness    : $HARNESS_IMAGE  (v$HARNESS_VERSION, read-only mount at $HARNESS_MOUNT_PATH)"
    echo "  gateway    : $GATEWAY_URL  (${_prefix}_API_KEY ending ${API_KEY: -4})"
    echo "  docker api : $_server_api"
    PYTHONPATH="$PWD" .venv/bin/harbor run -c "$RUNTIME_JOB_CONFIG" --job-name "$JOB_NAME" \
        ${VERIFIER_ARGS[@]+"${VERIFIER_ARGS[@]}"} --print-config >/dev/null 2>&1 \
        && echo "  harbor     : config accepted (schema + agent import path)" \
        || fail "harbor rejected the job config; run manually:
  PYTHONPATH=\$PWD .venv/bin/harbor run -c $JOB_CONFIG --print-config"
    exit 0
fi

# ── 8) Run ─────────────────────────────────────────────────────────────
# --agent-env delivers key + endpoints into the container (for {env:VAR}
# and $OPENAI_BASE_URL reads); harbor resolves the endpoint names host-side
# from the exported environment. The key enters under both protocol names;
# each harness reads the one its protocol expects.
PYTHONPATH="$PWD" .venv/bin/harbor run -c "$RUNTIME_JOB_CONFIG" --job-name "$JOB_NAME" \
    ${VERIFIER_ARGS[@]+"${VERIFIER_ARGS[@]}"} \
    --agent-env "${_prefix}_BASE_URL=${GATEWAY_URL}" \
    --agent-env "${_prefix}_API_KEY=${API_KEY}" \
    --agent-env "OPENAI_API_KEY=${API_KEY:-}" \
    --agent-env "ANTHROPIC_API_KEY=${API_KEY:-}" \
    --agent-env "OPENAI_BASE_URL=${OPENAI_BASE_URL:-}" \
    --agent-env "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-}" \
    -y
