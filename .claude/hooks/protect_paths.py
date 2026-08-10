#!/usr/bin/env python3
"""PreToolUse guard: deterministically blocks writes to protected paths.

Exit 0 = allow. Exit 2 = block; stderr is fed back to the agent.
"""
import json
import re
import sys

PROTECTED = ("data/gold/", ".claude/")

WRITE_PATTERN = re.compile(r"(>>?|\brm\b|\bmv\b|\bcp\b|\btee\b|sed\s+-i|\btruncate\b)")


def main() -> None:
    payload = json.load(sys.stdin)
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(tool_input.get("file_path", ""))
        if any(p in path for p in PROTECTED):
            sys.stderr.write(
                f"BLOCKED: {path} is protected (human-only). data/gold/ is hand-labeled "
                "ground truth; .claude/ is orchestration config. Do not work around this "
                "block — report it to the human instead.\n"
            )
            sys.exit(2)

    if tool == "Bash":
        cmd = str(tool_input.get("command", ""))
        if any(p in cmd for p in PROTECTED) and WRITE_PATTERN.search(cmd):
            sys.stderr.write(
                "BLOCKED: shell command appears to write into a protected path "
                "(data/gold/ or .claude/). Read-only access only.\n"
            )
            sys.exit(2)

    sys.exit(0)


main()
