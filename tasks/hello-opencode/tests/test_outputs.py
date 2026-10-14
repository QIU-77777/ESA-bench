"""Verify /app/result.txt and /app/solve.py produced by the agent."""

from pathlib import Path

DATA = Path("/app/data/numbers.txt")
RESULT = Path("/app/result.txt")
SCRIPT = Path("/app/solve.py")


def expected_sum() -> int:
    seen: set[int] = set()
    total = 0
    for line in DATA.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        value = int(line)
        if value < 0 or value in seen:
            continue
        seen.add(value)
        total += value
    return total


def test_script_exists():
    assert SCRIPT.is_file(), "/app/solve.py was not created"


def test_data_unchanged():
    assert DATA.read_text() == "12\n-7\n3\n12\n0\n45\n-2\n3\n9\n100\n-1\n7\n7\n23\n"


def test_result_exists():
    assert RESULT.is_file(), "/app/result.txt was not created"


def test_result_value():
    assert RESULT.read_text().strip() == str(expected_sum())


def test_script_reproducible():
    """The script itself must be correct, not just the recorded output."""
    import subprocess
    import sys

    RESULT.unlink()
    subprocess.run([sys.executable, str(SCRIPT)], check=True, cwd="/app")
    assert RESULT.read_text().strip() == str(expected_sum())
