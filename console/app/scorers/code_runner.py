"""Code scorer: run a user-provided Python scoring script in a subprocess."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .. import config
from .base import ScoreResult, normalize_result

RUNNER_SOURCE = '''\
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("user_scorer", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
payload = json.load(sys.stdin)
result = module.score(
    payload["input"], payload["expected"], payload["trace"], payload["metadata"]
)
print(json.dumps(result, ensure_ascii=False))
'''

FORBIDDEN_IMPORTS = ("socket", "subprocess", "shutil", "ctypes", "importlib")


def _validate_script(source: str) -> None:
    if "def score(" not in source:
        raise ValueError("评分脚本必须定义 score(input, expected, trace, metadata) 函数")
    for name in FORBIDDEN_IMPORTS:
        if f"import {name}" in source:
            raise ValueError(f"评分脚本禁止导入 {name}")


def run(config: dict, input_text: str, expected: str, trace, metadata: dict) -> ScoreResult:
    source = str(config.get("script_source") or "")
    _validate_script(source)
    timeout_sec = float(config.get("timeout_sec", 60))

    with tempfile.TemporaryDirectory(prefix="console-scorer-") as tmp:
        tmp_dir = Path(tmp)
        script_path = tmp_dir / "user_scorer.py"
        script_path.write_text(source)
        runner_path = tmp_dir / "runner.py"
        runner_path.write_text(RUNNER_SOURCE)

        process = subprocess.run(
            [str(config_python()), str(runner_path), str(script_path)],
            input=json.dumps(
                {
                    "input": input_text,
                    "expected": expected,
                    "trace": trace,
                    "metadata": metadata,
                },
                ensure_ascii=False,
            ),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=_sandbox_env(),
        )
    if process.returncode != 0:
        raise RuntimeError(f"评分脚本执行失败：{process.stderr.strip()[-2000:]}")
    try:
        raw = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"评分脚本输出不是 JSON：{process.stdout[:500]!r}") from exc
    return normalize_result(raw)


def config_python():
    return config.PYTHON


def _sandbox_env() -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "no_proxy": "*",
        "NO_PROXY": "*",
    }
