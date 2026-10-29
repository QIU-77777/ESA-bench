"""Keep the local harbor view service reachable for the console proxy.

Harbor view imports litellm lazily inside its pricing endpoint. Under
concurrent requests that import races and raises KeyError, which harbor
does not catch, leaving the server stuck. Starting the viewer through a
wrapper that imports litellm first removes the lazy import entirely.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request

from .. import config

_PORTS = range(8080, 8090)
_PROBE_TIMEOUT = 1.0
_STARTUP_WAIT_SEC = 15.0
_RECOVER_COOLDOWN_SEC = 60.0

_process: subprocess.Popen | None = None
_last_recover = 0.0

_PREWARM = "import litellm; from harbor.cli.main import app; app()"


def ensure_running() -> str:
    """Start harbor view when it is unreachable; return the reachable base."""
    if _probe(config.HARBOUR_VIEW_BASE):
        return config.HARBOUR_VIEW_BASE
    _stop_owned()
    _spawn()
    deadline = time.monotonic() + _STARTUP_WAIT_SEC
    while time.monotonic() < deadline:
        base = _discover()
        if base:
            config.HARBOUR_VIEW_BASE = base
            return base
        time.sleep(0.5)
    return config.HARBOUR_VIEW_BASE


def recover() -> str | None:
    """Restart a viewer this module owns when it stopped responding."""
    global _last_recover
    if _process is None or _process.poll() is not None:
        return None
    if time.monotonic() - _last_recover < _RECOVER_COOLDOWN_SEC:
        return None
    _last_recover = time.monotonic()
    _stop_owned()
    _spawn()
    deadline = time.monotonic() + _STARTUP_WAIT_SEC
    while time.monotonic() < deadline:
        base = _discover()
        if base:
            config.HARBOUR_VIEW_BASE = base
            return base
        time.sleep(0.5)
    return None


def _spawn() -> None:
    global _process
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    log = (config.REPORTS_DIR / "harbor-view.log").open("w")
    _process = subprocess.Popen(
        [str(config.PYTHON), "-c", _PREWARM, "view", "jobs"],
        cwd=config.REPO_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _stop_owned() -> None:
    global _process
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None


def _discover() -> str | None:
    for port in _PORTS:
        base = f"http://127.0.0.1:{port}"
        if _probe(base):
            return base
    return None


def _probe(base: str) -> bool:
    """A harbor view instance answers /api/jobs and serves its asset manifest."""
    try:
        with urllib.request.urlopen(f"{base}/", timeout=_PROBE_TIMEOUT) as response:
            html = response.read(4096).decode("utf-8", "replace")
        if "/assets/manifest-" not in html:
            return False
        with urllib.request.urlopen(f"{base}/api/jobs", timeout=_PROBE_TIMEOUT) as api:
            return api.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False
