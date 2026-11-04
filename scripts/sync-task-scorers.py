#!/usr/bin/env python3
"""Synchronize canonical verifier and native Oracle files for Harbor tasks."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from esa.reference import SOLVE_SCRIPT

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="check copies without writing")
    args = parser.parse_args()
    changed = []
    for reference in sorted((ROOT / "tasks").glob("*/solution/reference.py")):
        task = reference.parents[1]
        copies = {"tests/score.py": ROOT / "esa/score.py", "tests/state_score.py": ROOT / "esa/state_score.py",
                  "solution/reference_runtime.py": ROOT / "esa/reference.py", "solution/runner.py": ROOT / "esa/oracle.py"}
        for name, source in copies.items():
            target = task / name
            content = source.read_bytes()
            if not target.exists() or target.read_bytes() != content:
                changed.append(str(target.relative_to(ROOT)))
                if not args.check:
                    target.write_bytes(content)
        target = task / "solution/solve.sh"
        content = SOLVE_SCRIPT.encode("utf8")
        if not target.exists() or target.read_bytes() != content:
            changed.append(str(target.relative_to(ROOT)))
            if not args.check:
                target.write_bytes(content)
                target.chmod(0o755)
    if args.check and changed:
        print("outdated verifier or Oracle copies:\n" + "\n".join(changed))
        return 1
    print("verifier and Oracle copies are current" if args.check else f"updated {len(changed)} verifier or Oracle files")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
