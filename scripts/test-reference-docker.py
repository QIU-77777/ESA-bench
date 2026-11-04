#!/usr/bin/env python3
"""Validate references and Redis contracts in isolated Docker resources.

``--mock-url`` validates against an already running service (the cockpit
recorder) without creating Docker resources, and skips the ESA environment
Redis contract tests, which the recorder covers in cockpit/recorder_test.py.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from esa.bench_selection import select_tasks
from esa.reference import HttpTransport, validate_task


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout.strip()


def run_references(paths, transport, output):
    reports = [validate_task(path, transport, output / path.name) for path in paths]
    summary = {"passed": all(report["passed"] for report in reports),
               "tasks": [{"task": report["case_id"], "passed": report["passed"],
                          "negatives": len(report["negative_tests"]), "alternatives": len(report["alternatives"]),
                          "failures": report["failures"]} for report in reports]}
    return reports, summary


def main():
    parser = argparse.ArgumentParser(description="执行参考程序、合法替代解和错误答案测试")
    tasks = parser.add_mutually_exclusive_group()
    tasks.add_argument("--bench", help="按 configs/bench/<slug>.yaml 选择任务集合")
    tasks.add_argument("--all", action="store_true", help="全量发现任务，带登记与单集合两道守卫")
    tasks.add_argument("--task", type=Path)
    parser.add_argument("--mock-url", help="直接对给定地址验证；跳过 Docker 资源与 ESA Redis 契约测试")
    parser.add_argument("--job-name", default=None, help="结果目录名，默认按前缀加随机串生成")
    args = parser.parse_args()
    try:
        if args.task:
            paths = [args.task.resolve()]
        elif args.bench:
            paths = select_tasks(args.bench)
        else:
            paths = select_tasks(None, True)
    except ValueError as error:
        parser.error(str(error))
    if not paths:
        parser.error("没有可验证的参考任务")

    if args.mock_url:
        name = args.job_name or ("esa-reference-mock-" + uuid.uuid4().hex[:12])
        output = ROOT / "jobs" / name
        output.mkdir(parents=True)
        _, summary = run_references(paths, HttpTransport(args.mock_url), output)
        summary["mock_url"] = args.mock_url
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 1

    prefix = "esa-reference-test-" + uuid.uuid4().hex[:12]
    network, redis, service = prefix, prefix + "-redis", prefix + "-service"
    created = []
    output = ROOT / "jobs" / (args.job_name or prefix)
    output.mkdir(parents=True)
    try:
        docker("network", "create", network)
        created.append(network)
        docker("run", "-d", "--name", redis, "--network", network,
               "-p", "127.0.0.1::6379", "redis:7-alpine")
        created.append(redis)
        docker("run", "-d", "--name", service, "--network", network,
               "-p", "127.0.0.1::8003", "-e", f"REDIS_URL=redis://{redis}:6379/0",
               os.environ.get("MOCK_SYSTEM_IMAGE", "esa-local-environment:2.0.0"))
        created.append(service)
        port = int(docker("port", service, "8003/tcp").rsplit(":", 1)[1])
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(base + "/health", timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("isolated environment did not become healthy") from None
                time.sleep(0.2)
        reports, summary = run_references(paths, HttpTransport(base), output)
        redis_port = int(docker("port", redis, "6379/tcp").rsplit(":", 1)[1])
        env = {**os.environ, "ESA_TEST_REDIS_URL": f"redis://127.0.0.1:{redis_port}/0"}
        with (output / "redis-tests.log").open("w") as log:
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", "environment.test_environment"],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        summary["passed"] = summary["passed"] and result.returncode == 0
        summary["environment_image"] = os.environ.get("MOCK_SYSTEM_IMAGE", "esa-local-environment:2.0.0")
        summary["redis_tests_exit"] = result.returncode
        summary["evidence_directory"] = str(output.relative_to(ROOT))
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 1
    finally:
        # These exact unique resources were created by this process only.
        for name in reversed(created):
            try:
                docker("network", "rm", name) if name == network else docker("rm", "-f", name)
            except subprocess.CalledProcessError:
                print(f"failed to clean isolated Docker resource {name}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
