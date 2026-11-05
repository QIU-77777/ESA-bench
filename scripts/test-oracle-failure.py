#!/usr/bin/env python3
"""Assert native Oracle cannot pass when solve reaches the goal and then fails."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]


def state_at(state, path):
    value = state
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise AssertionError("目标状态路径不存在：" + path)
        value = value[part]
    return value


def main():
    parser = argparse.ArgumentParser(description="检查达标后异常仍被拒绝")
    parser.add_argument("--task", type=Path, default=Path("tasks/esa-navi-execute"))
    parser.add_argument("--goal-path", default="navigation.active")
    args = parser.parse_args()
    task_path = (ROOT / args.task).resolve() if not args.task.is_absolute() else args.task
    name = 'esa-native-oracle-failure-' + uuid.uuid4().hex[:12]
    output = ROOT / 'jobs' / name
    with tempfile.TemporaryDirectory(prefix='esa-oracle-failure-') as tmp:
        root = Path(tmp)
        task = root / 'task'
        shutil.copytree(task_path, task)
        reference = task / 'solution/reference.py'
        reference.write_text(reference.read_text() + '\noriginal_solve = solve\ndef solve(ctx):\n    original_solve(ctx)\n    raise RuntimeError("reference execution failed after reaching the goal")\n')
        config = {'job_name': name, 'jobs_dir': str(ROOT / 'jobs'), 'n_attempts': 1,
                  'orchestrator': {'type': 'local', 'n_concurrent_trials': 1},
                  'environment': {'type': 'docker', 'force_build': True, 'delete': True},
                  'agents': [{'name': 'oracle'}], 'tasks': [{'path': str(task)}]}
        path = root / 'job.yaml'
        path.write_text(yaml.safe_dump(config))
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'STEPFUN_OPENAI_BASE_URL': 'http://127.0.0.1:1/v1', 'STEPFUN_API_KEY': 'oracle-no-model'}
        result = subprocess.run([str(ROOT / '.venv/bin/harbor'), 'run', '-c', str(path), '-y'], cwd=ROOT, env=env, text=True, capture_output=True)
        if result.returncode != 0:
            print('Harbor process failed before the expected verifier result', file=sys.stderr)
            return 1
        rewards = list(output.glob('*/verifier/reward.json'))
        calls = list(output.glob('*/agent/tool-calls.json'))
        details = list(output.glob('*/verifier/score-detail.json'))
        snapshots = list(output.glob('*/agent/snapshot-final.json'))
        if not all(len(items) == 1 for items in (rewards, calls, details, snapshots)):
            raise AssertionError('native Oracle failure did not preserve complete evidence')
        assert json.loads(rewards[0].read_text())['reward'] == 0
        assert json.loads(calls[0].read_text())['program_failure']
        assert json.loads(details[0].read_text())['valid'] is False
        assert state_at(json.loads(snapshots[0].read_text())['state'], args.goal_path) is True
        summary = {'passed': True, 'agent': 'oracle', 'task': str(task_path.relative_to(ROOT)),
                   'goal_path': args.goal_path, 'goal_reached': True, 'program_failure_rejected': True,
                   'reward': 0, 'evidence_directory': str(output.relative_to(ROOT))}
        (output / 'failure-regression.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
