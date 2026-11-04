#!/usr/bin/env python3
"""Validate all references and Redis contracts in isolated Docker resources."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from esa.reference import HttpTransport, validate_task


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout.strip()


def main():
    prefix = "esa-reference-test-" + uuid.uuid4().hex[:12]
    network, redis, service = prefix, prefix + "-redis", prefix + "-service"
    created = []
    output = ROOT / "jobs" / prefix
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
        references = sorted((ROOT / "tasks").glob("*/solution/reference.py"))
        if not references:
            raise RuntimeError("no standard Harbor reference tasks were found")
        reports = [validate_task(reference.parents[1], HttpTransport(base), output / reference.parents[1].name)
                   for reference in references]
        redis_port = int(docker("port", redis, "6379/tcp").rsplit(":", 1)[1])
        env = {**os.environ, "ESA_TEST_REDIS_URL": f"redis://127.0.0.1:{redis_port}/0"}
        with (output / "redis-tests.log").open("w") as log:
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", "environment.test_environment"],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        summary = {"passed": all(report["passed"] for report in reports) and result.returncode == 0,
                   "environment_image": os.environ.get("MOCK_SYSTEM_IMAGE", "esa-local-environment:2.0.0"),
                   "tasks": [{"task": report["case_id"], "passed": report["passed"],
                              "negatives": len(report["negative_tests"]), "alternatives": len(report["alternatives"]),
                              "failures": report["failures"]} for report in reports],
                   "redis_tests_exit": result.returncode, "evidence_directory": str(output.relative_to(ROOT))}
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
