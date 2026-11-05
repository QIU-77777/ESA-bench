#!/usr/bin/env python3
"""Validate references and negative tests through a deployed MCP service."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from esa.bench_selection import select_tasks
from esa.reference import HttpTransport, ReferenceFailure, validate_task

@contextmanager
def local_http_environment(seed_file=None):
    """Explicit integration-test service; always uses real HTTP/MCP serialization."""
    from http.server import ThreadingHTTPServer
    from environment.data import Dataset
    from environment.service import Environment
    from environment.server import make_handler
    from environment.store import MemoryStore
    with tempfile.TemporaryDirectory(prefix="esa-reference-http-") as tmp:
        dataset = Dataset(seed_path=seed_file)
        environment = Environment(MemoryStore(test_only=True), dataset, workspace_root=tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(environment))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield HttpTransport(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            if hasattr(dataset, "temp"):
                dataset.temp.cleanup()


def main():
    parser = argparse.ArgumentParser(description="执行参考程序、合法替代解和错误答案测试")
    tasks = parser.add_mutually_exclusive_group(required=True)
    tasks.add_argument("--task", type=Path)
    tasks.add_argument("--bench", help="按 configs/bench/<slug>.yaml 选择任务集合")
    tasks.add_argument("--all", action="store_true", help="全量发现任务，带登记与单集合两道守卫")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--mock-url", help="实际部署的环境服务地址")
    target.add_argument("--local-http", action="store_true", help="启动临时真实HTTP/MCP服务；仅集成测试")
    parser.add_argument("--seed-file", type=Path, help="临时HTTP服务的seed，仅配合--local-http")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "jobs/reference")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.seed_file and not args.local_http:
        parser.error("--seed-file仅支持--local-http；部署数据由服务决定")
    try:
        paths = [args.task.resolve()] if args.task else select_tasks(args.bench, args.all)
    except ValueError as error:
        parser.error(str(error))
    if not paths:
        parser.error("没有可验证的参考任务")

    def execute(transport):
        summaries = []
        for path in paths:
            try:
                report = validate_task(path, transport, args.output_dir / path.name)
                summary = {"task": path.name, "status": report["status"], "passed": report["passed"], "negatives": len(report["negative_tests"]), "alternatives": len(report["alternatives"]), "failures": report["failures"], "duration_ms": report["duration_ms"]}
            except Exception as exc:
                summary = {"task": path.name, "status": "error", "passed": False, "error_type": type(exc).__name__}
                if isinstance(exc, ReferenceFailure):
                    summary["failure"] = str(exc)
                output = args.output_dir / path.name
                output.mkdir(parents=True, exist_ok=True)
                (output / "golden-run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf8")
            summaries.append(summary)
            if not args.json:
                print(json.dumps(summary, ensure_ascii=False))
        if args.json:
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0 if all(item["passed"] for item in summaries) else 1

    if args.local_http:
        with local_http_environment(args.seed_file) as transport:
            return execute(transport)
    return execute(HttpTransport(args.mock_url))

if __name__ == "__main__":
    raise SystemExit(main())
