"""Behaviour tests for `.claude/hooks/protect_paths.py`, the PreToolUse guard.

The hook is not importable: it runs its work at import time and exits, because
that is the contract the harness invokes it under (JSON on stdin, exit 0 to
allow, exit 2 to block). So it is driven here the same way the harness drives
it — as a subprocess with a payload on stdin — and only the exit code and
stderr are asserted. Nothing in this module imports the hook or writes to a
protected path.

**Why the hook path is overridable.** `.claude/` is protected from the agent
that wrote these tests, so a new hook is installed by hand rather than by the
tooling. `PROTECT_PATHS_HOOK` lets a candidate file be run against this suite
*before* it is copied into place:

    PROTECT_PATHS_HOOK=/tmp/candidate.py .venv/bin/pytest tests/test_protect_paths.py

Unset, it tests the installed hook, which is what CI and a normal run do.

The cases below are the contract, not an inventory of the implementation. The
two halves matter equally and for different reasons: the `allow` cases exist
because an agent that cannot *read* `data/gold/` cannot check its work against
it, and the `block` cases exist because `data/gold/` is ground truth. A guard
that fails either way is broken — one silently, one loudly.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOOK = REPO_ROOT / ".claude" / "hooks" / "protect_paths.py"
HOOK = Path(os.environ.get("PROTECT_PATHS_HOOK") or DEFAULT_HOOK)

ALLOW, BLOCK = "allow", "block"

GOLD = "data/gold"

# (expectation, tool, command or file_path, label)
CASES: list[tuple[str, str, str, str]] = [
    # ---- reads and unrelated work: must not be blocked --------------------
    (ALLOW, "Bash", f"head -c 300 {GOLD}/miller2011.json 2>/dev/null",
     "read with a stderr redirect"),
    (ALLOW, "Bash", f"cat {GOLD}/harrison2009.json", "plain read"),
    (ALLOW, "Bash", f"ls -la {GOLD}/", "listing"),
    (ALLOW, "Bash", f"grep -n rapamycin {GOLD}/*.json", "grep"),
    (ALLOW, "Bash", f"diff {GOLD}/a.json {GOLD}/b.json", "diff"),
    (ALLOW, "Bash", f"wc -l {GOLD}/*.json | tail -3", "pipe into a reader"),
    (ALLOW, "Bash", f"git status --short && git diff -- {GOLD}", "read-only git"),
    (ALLOW, "Bash", f"git log --oneline -- {GOLD}/miller2011.json", "git log"),
    (ALLOW, "Bash", ".venv/bin/pytest -q", "unrelated command"),
    (ALLOW, "Bash", ".venv/bin/python scripts/validate_gold.py", "the validator itself"),
    (ALLOW, "Bash", f"cp {GOLD}/a.json /tmp/backup.json", "copying out of gold is a read"),
    (ALLOW, "Bash", "echo hi > /tmp/x.txt", "redirect to an unprotected path"),
    (ALLOW, "Bash", "sed 's/a/b/' data/drafts/x.json", "sed streaming, no -i"),
    (ALLOW, "Write", "data/drafts/miller2011.json", "writing a draft"),

    # ---- writes: must be blocked -----------------------------------------
    (BLOCK, "Bash", f"echo x > {GOLD}/x.json", "redirect into gold"),
    (BLOCK, "Bash", f"echo x >> {GOLD}/x.json", "append into gold"),
    (BLOCK, "Bash", f"rm {GOLD}/x.json", "rm"),
    (BLOCK, "Bash", f"rm -rf {GOLD}", "rm of the directory, no trailing slash"),
    (BLOCK, "Bash", f"mv {GOLD}/a.json /tmp/", "mv out is still destructive"),
    (BLOCK, "Bash", f"cp /tmp/x.json {GOLD}/x.json", "copying into gold"),
    (BLOCK, "Bash", f"sed -i '' 's/a/b/' {GOLD}/x.json", "in-place sed"),
    (BLOCK, "Bash", f"tee {GOLD}/x.json < /tmp/x", "tee"),
    (BLOCK, "Bash", f"truncate -s 0 {GOLD}/x.json", "truncate"),
    (BLOCK, "Bash", f"chmod 777 {GOLD}/x.json", "chmod"),
    (BLOCK, "Bash", f"git checkout -- {GOLD}", "a writing git subcommand"),
    (BLOCK, "Bash", f"git rm {GOLD}/x.json", "git rm"),
    (BLOCK, "Bash", f"python3 -c \"open('{GOLD}/x.json','w').write('')\"",
     "inline interpreter"),
    (BLOCK, "Bash", f'eval "rm {GOLD}/x.json"', "eval"),
    (BLOCK, "Bash", f"bash -c 'rm {GOLD}/x.json'", "subshell"),
    (BLOCK, "Bash", f"cat $(ls {GOLD}/*.json)", "command substitution"),
    (BLOCK, "Bash", f"cat '{GOLD}/x.json", "unbalanced quoting"),
    (BLOCK, "Bash", ".venv/bin/python scripts/check_gold.py data/drafts/x.json --promote",
     "agent-invoked --promote"),

    # ---- check_gold.py's writing modes are the human's to run -------------
    # `check_promote` used to match one hardcoded flag and now loops over
    # HUMAN_ONLY_FLAGS. These pin both flags through that rewrite. The dry run
    # is blocked as well as the write: an agent that can read the proposed
    # rewrites is one step from running them, and the whole mode is the
    # human's to drive.
    (BLOCK, "Bash", ".venv/bin/python scripts/check_gold.py --all --refresh-quotes",
     "agent-invoked --refresh-quotes, dry run"),
    (BLOCK, "Bash", ".venv/bin/python scripts/check_gold.py --all --refresh-quotes --write",
     "agent-invoked --refresh-quotes --write"),
    # No protected path named, deliberately: with one, the INTERPRETERS rule
    # would block this on `python` alone and the case would pass without
    # testing the flag at all.
    (BLOCK, "Bash", ".venv/bin/python scripts/check_gold.py --refresh-quotes=x --all",
     "--refresh-quotes in the = form"),
    # The pre-existing --promote case above has the flag last, where `(?:\s|$)`
    # matches on end-of-string. This one puts it mid-command so the trailing
    # whitespace branch is exercised too — the regression the loop could hide.
    (BLOCK, "Bash", ".venv/bin/python scripts/check_gold.py --promote data/drafts/x.json",
     "--promote before its argument rather than last"),
    (ALLOW, "Bash", "rg -n -- --refresh-quotes README.md",
     "a human-only flag named by a command that is not check_gold"),
    (BLOCK, "Bash", "echo x > .claude/hooks/protect_paths.py", "the hook editing itself"),
    (BLOCK, "Write", f"{GOLD}/miller2011.json", "editor tool on a gold file"),
    (BLOCK, "Write", ".claude/agents/reviewer.md", "editor tool on .claude"),
]


def run_hook(tool: str, value: str) -> subprocess.CompletedProcess[str]:
    """Drive the hook exactly as the harness does: JSON in, exit code out."""
    key = "command" if tool == "Bash" else "file_path"
    payload = {"tool_name": tool, "tool_input": {key: value}}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # a non-zero exit is the result under test, not an error
    )


def verdict(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode == 2:
        return BLOCK
    if result.returncode == 0:
        return ALLOW
    raise AssertionError(
        f"hook exited {result.returncode}, which is neither allow (0) nor "
        f"block (2). stderr: {result.stderr.strip()!r}"
    )


@pytest.mark.parametrize(
    ("expected", "tool", "value"),
    [pytest.param(e, t, v, id=label) for e, t, v, label in CASES],
)
def test_hook_verdict(expected: str, tool: str, value: str) -> None:
    result = run_hook(tool, value)
    assert verdict(result) == expected, (
        f"{tool} {value!r}\n  wanted {expected}, got {verdict(result)}\n"
        f"  stderr: {result.stderr.strip()!r}"
    )


@pytest.mark.parametrize(
    ("tool", "value"),
    [pytest.param(t, v, id=label) for e, t, v, label in CASES if e == BLOCK],
)
def test_a_block_explains_itself(tool: str, value: str) -> None:
    """The exit code stops the call; stderr is the only thing the agent reads.

    A silent block teaches an agent to retry with a variation, which is the one
    behaviour CLAUDE.md forbids outright.
    """
    stderr = run_hook(tool, value).stderr
    assert "BLOCKED" in stderr
    assert "data/gold/" in stderr, "the message must name the boundary it enforces"


@pytest.mark.parametrize(
    ("tool", "value"),
    [pytest.param(t, v, id=label) for e, t, v, label in CASES if e == ALLOW],
)
def test_an_allowed_command_says_nothing(tool: str, value: str) -> None:
    """Stderr on an allowed call would be noise in every unrelated tool result."""
    assert run_hook(tool, value).stderr == ""


def test_a_payload_with_no_tool_input_is_allowed() -> None:
    """Malformed payloads must not wedge the harness on every call."""
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash"}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # a non-zero exit is the result under test, not an error
    )
    assert result.returncode == 0


def test_an_unrecognised_tool_is_allowed() -> None:
    assert verdict(run_hook("WebFetch", "https://example.com")) == ALLOW


def test_the_installed_hook_is_executable() -> None:
    """Committed 100755. ruff's EXE001 fires on a shebang without the bit, so a
    hook installed by hand with the mode dropped fails lint rather than here —
    which is a confusing place to learn it. Pinned at the source instead."""
    if HOOK != DEFAULT_HOOK:
        pytest.skip("testing a candidate hook, not the installed one")
    assert HOOK.stat().st_mode & stat.S_IXUSR, f"chmod +x {HOOK}"
