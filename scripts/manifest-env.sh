# Shared loader for harness/<name>/manifest.yaml. Source this file, do not
# execute it. Callers must already be in the repo root (all scripts cd there).
# Usage: source scripts/manifest-env.sh [harness-name]   (default: opencode)
# Exposes: HARNESS_NAME HARNESS_VERSION HARNESS_IMAGE HARNESS_MOUNT_PATH
#          HARNESS_NPM_PACKAGE
set -euo pipefail

_HB_HARNESS="${1:-opencode}"
_HB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_HB_PY="$_HB_ROOT/.venv/bin/python"
[ -x "$_HB_PY" ] || {
  echo "ERROR: $_HB_PY not found — create the venv first (see README)." >&2
  exit 1
}
[ -d "$_HB_ROOT/harness/$_HB_HARNESS" ] || {
  echo "ERROR: unknown harness '$_HB_HARNESS' (no harness/$_HB_HARNESS/)." >&2
  exit 1
}

# Capture-then-eval: a failing substitution must abort here, not after eval
# silently ran with empty strings.
_HB_MANIFEST_VARS="$("$_HB_PY" - "$_HB_HARNESS" <<'PY'
import shlex
import sys

import yaml

harness = sys.argv[1]
try:
    manifest = yaml.safe_load(open(f"harness/{harness}/manifest.yaml"))
except FileNotFoundError:
    sys.exit(f"ERROR: harness/{harness}/manifest.yaml not found (run from repo root)")

image = f"{manifest['image_repository']}:{manifest['version']}"
for key, value in [
    ("HARNESS_NAME", manifest["name"]),
    ("HARNESS_VERSION", manifest["version"]),
    ("HARNESS_IMAGE", image),
    ("HARNESS_MOUNT_PATH", manifest["mount_path"]),
    ("HARNESS_NPM_PACKAGE", manifest["npm_package"]),
]:
    print(f"{key}={shlex.quote(str(value))}")
PY
)" || exit $?
eval "$_HB_MANIFEST_VARS"
