"""The guard must block writes into data/gold/ and .claude/, and only those.

Written after the guard sat inert for weeks: it crashed at import under
Python 3.9, exited 1, and PreToolUse read that as "hook errored, proceed".
These tests assert the exit codes the hook contract is defined in terms of —
0 allow, 2 block — not the wording of any message.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "protect_paths.py"

ALLOW = 0
BLOCK = 2


def run(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def write(path: str) -> str:
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": path}})


def bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize(
    "payload",
    [
        write("data/gold/harrison2009.json"),
        write("./data/gold/x.json"),
        write(".claude/hooks/protect_paths.py"),
        bash("echo x > data/gold/x.json"),
        bash("cat a.json >> data/gold/x.json"),
    ],
)
def test_blocks_writes_to_protected_paths(payload: str) -> None:
    assert run(payload).returncode == BLOCK


@pytest.mark.parametrize(
    "payload",
    [
        write("extract/extract.py"),
        write("data/golden/x.json"),  # prefix match must not overreach
        write("evals/history.md"),
        bash("pytest -q"),
        bash("cat data/gold/harrison2009.json"),  # reads are allowed
    ],
)
def test_allows_everything_else(payload: str) -> None:
    assert run(payload).returncode == ALLOW


def test_fails_closed_on_unparseable_payload() -> None:
    """A crashing guard must block, not wave the call through."""
    assert run("not json at all").returncode == BLOCK
