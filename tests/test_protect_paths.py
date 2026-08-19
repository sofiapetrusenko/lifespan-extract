"""Behaviour tests for `.claude/hooks/protect_paths.py`, the PreToolUse guard.

The hook is driven here the same way the harness drives it — as a subprocess
with a JSON payload on stdin, asserting only the exit code and stderr (0 allow,
2 block). Nothing in this module imports the hook or writes to a protected path.

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

The guard also spent weeks inert: it crashed at import under Python 3.9, exited
1, and PreToolUse read that as "hook errored, proceed". Hence
`test_fails_closed_on_a_crash` and
`test_the_project_configured_invocation_blocks_as_a_script` — exit 1 is not a
verdict, and the invocation the harness actually uses is part of the contract,
not an implementation detail.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOOK = REPO_ROOT / ".claude" / "hooks" / "protect_paths.py"
HOOK = Path(os.environ.get("PROTECT_PATHS_HOOK") or DEFAULT_HOOK)
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

if os.environ.get("PROTECT_PATHS_HOOK") and not HOOK.is_file():
    # Fail here rather than let the suite run. Every case drives the hook as a
    # subprocess, and a path that does not exist makes the interpreter exit 2 —
    # the guard's own block code — so BLOCK cases pass, ALLOW cases fail, and
    # the run reports mass failure. Measured: a candidate-builder that never
    # ran left this variable pointing at nothing and produced 82 failures,
    # which reads as a catastrophic regression in the hook and is an empty
    # path. The affordance is for vetting a candidate before installing it, so
    # a missing candidate is a mistake in the invocation, not a verdict on
    # anything.
    raise pytest.UsageError(
        f"PROTECT_PATHS_HOOK={str(HOOK)!r} does not exist. "
        "Point it at a candidate hook file, or unset it to test the installed "
        f"hook at {DEFAULT_HOOK.relative_to(REPO_ROOT)}."
    )

# The tools the hook's `main()` inspects. Each has to be routed to the hook by
# some PreToolUse matcher, or the guard is installed but never consulted for it.
# `NotebookEdit` is handled separately: the hook inspects it, but the installed
# matcher does not route it. See `test_notebook_edit_is_routed_to_the_guard`.
GUARDED_TOOLS = ("Bash", "Write", "Edit", "MultiEdit")

# A matcher built only of alphanumerics, `_`, `|`, `,`, `-` and spaces is an
# exact-name list, split on `|` or `,` and trimmed — NOT a regex. Only other
# strings reach the regex engine, and an invalid pattern matches nothing. This is
# ported from the harness rather than guessed: the shipped Claude Code binary
# contains the predicate
#
#   if(!t||t==="*")return!0;
#   if((r?/^[a-zA-Z0-9_|, -]+$/:/^[a-zA-Z0-9_|]+$/).test(t))
#     return t.split(r?/[|,]/:"|").map(s=>s.trim()).filter(Boolean)...includes(e);
#   try{let i=new RegExp(t);...}catch{...return!1}
#
# alongside the warning "Hook matcher `…` matches no tool (it is compared as an
# exact string). See CHANGELOG v2.1.195."
#
# The distinction decides a real case: read as a regex, "Edit|Write|MultiEdit"
# would cover "NotebookEdit" through the substring "Edit". Read as an exact
# list — which is what ships — it does not, and the guard never fires for
# notebooks. See `test_notebook_edit_is_routed_to_the_guard`.
#
# The `r` flag resolves to True here, so `,` and ` ` are separators too. It is
# not a mystery flag: the sole call site is `h1v(a,A.matcher,l,c)` with
# `l=f1v.has(n.hook_event_name)`, and `f1v` is a Set containing "PreToolUse".
# So `"Bash, Write"` is a legal four-name list, and reading it any other way
# would fail a valid config with "the guard never runs".
SIMPLE_MATCHER_RE = re.compile(r"[a-zA-Z0-9_|, -]+")


def matcher_matches(matcher: str | None, tool: str) -> bool:
    """Does this PreToolUse `matcher` route `tool` to its hooks?"""
    if not matcher or matcher == "*":
        return True
    if SIMPLE_MATCHER_RE.fullmatch(matcher):
        names = [part.strip() for part in re.split(r"[|,]", matcher)]
        return tool in [name for name in names if name]
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def guarded_entries() -> list[dict]:
    """PreToolUse entries in the project settings whose hooks invoke this guard.

    Read fresh on each call rather than at import, so `SETTINGS` can be pointed
    at a candidate config to check what this test would say about it.
    """
    entries = json.loads(SETTINGS.read_text()).get("hooks", {}).get("PreToolUse", [])
    return [
        entry for entry in entries
        if any(DEFAULT_HOOK.name in h.get("command", "") for h in entry.get("hooks", []))
    ]

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
    # ---- validate_gold.py's writing mode, added with the manifest ---------
    # `--write-manifest` regenerates data/gold/MANIFEST.sha256, so it writes
    # into the gold set and is the human's like the two above. It is on a
    # *different script*, which is the whole reason `check_promote`'s early
    # return had to widen from the literal "check_gold" to HUMAN_ONLY_SCRIPTS:
    # adding the flag to HUMAN_ONLY_FLAGS alone left this command allowed,
    # because the function returned before the flag loop ever ran.
    (BLOCK, "Bash", "python scripts/validate_gold.py --write-manifest",
     "agent-invoked --write-manifest"),
    # Flag mid-command, so the `(?:\s|=|$)` trailing branch is exercised on
    # this script too rather than only on end-of-string, as with --promote.
    (BLOCK, "Bash", "python scripts/validate_gold.py --write-manifest --force",
     "--write-manifest mid-command rather than last"),
    (ALLOW, "Bash", "rg -n -- --refresh-quotes README.md",
     "a human-only flag named by a command that is not check_gold"),
    (BLOCK, "Bash", "echo x > .claude/hooks/protect_paths.py", "the hook editing itself"),
    (BLOCK, "Write", f"{GOLD}/miller2011.json", "editor tool on a gold file"),
    (BLOCK, "Write", ".claude/agents/reviewer.md", "editor tool on .claude"),

    # ---- path-shape cases -------------------------------------------------
    # PROTECTED_RE anchors on `(?:^|/)` and closes on `(?:/|$)`. These pin both
    # ends: a `./`-prefixed path is the same path, and `data/golden/` is not.
    (BLOCK, "Write", f"{GOLD}/harrison2009.json", "editor tool on a second gold file"),
    (BLOCK, "Write", f"./{GOLD}/x.json", "gold reached through a ./ prefix"),
    (BLOCK, "Write", ".claude/hooks/protect_paths.py", "editor tool on the hook itself"),
    (BLOCK, "Bash", f"cat a.json >> {GOLD}/x.json", "append with a real source file"),
    (ALLOW, "Write", "data/golden/x.json", "prefix match must not overreach"),
    (ALLOW, "Write", "extract/extract.py", "writing project source"),
    (ALLOW, "Write", "evals/history.md", "writing eval notes"),
    (ALLOW, "Bash", "pytest -q", "bare pytest names no protected path"),

    # ---- the other editor tools the hook inspects -------------------------
    # `Write` alone left `Edit`, `MultiEdit` and the `notebook_path` branch
    # untested: the tool tuple in `main()` could lose an entry and every case
    # above would still pass.
    (BLOCK, "Edit", f"{GOLD}/x.json", "Edit on a gold file"),
    (BLOCK, "MultiEdit", f"{GOLD}/x.json", "MultiEdit on a gold file"),
    (BLOCK, "NotebookEdit", f"{GOLD}/x.ipynb", "NotebookEdit reads notebook_path"),
    (BLOCK, "Edit", ".claude/settings.json", "Edit on .claude"),
    (ALLOW, "NotebookEdit", "notebooks/scratch.ipynb", "notebook outside gold"),
    (ALLOW, "Edit", "extract/extract.py", "Edit on project source"),
]


def run_hook(tool: str, value: str) -> subprocess.CompletedProcess[str]:
    """Drive the hook exactly as the harness does: JSON in, exit code out."""
    if tool == "Bash":
        key = "command"
    elif tool == "NotebookEdit":
        key = "notebook_path"  # the hook reads this key, not file_path
    else:
        key = "file_path"
    payload = {"tool_name": tool, "tool_input": {key: value}}
    return run_payload(json.dumps(payload))


def run_payload(payload: str) -> subprocess.CompletedProcess[str]:
    """Feed stdin to the hook verbatim, so a payload that is not a well-formed
    tool call — or not JSON at all — can be driven too."""
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
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

    # The line above is satisfied by GOLD_NOTE, which every block emits, so on
    # its own it cannot tell a real reason from boilerplate — nor notice a
    # `.claude/` block whose reason talks about data/gold/. The reason is the
    # first line; require that it is actually there and actually specific.
    reason = stderr.splitlines()[0]
    assert reason.startswith("BLOCKED: "), f"no reason line: {stderr!r}"
    assert len(reason) > len("BLOCKED: ") + 20, f"reason is too thin to act on: {reason!r}"

    # For the editor tools the hook names the offending path outright, so the
    # reason has to mention the boundary that was actually crossed. Bash reasons
    # legitimately vary — an opaque command or unbalanced quoting is refused
    # without the hook ever resolving which path was meant.
    if tool != "Bash":
        root = ".claude" if ".claude" in value else GOLD
        assert root in reason, (
            f"a {tool} block on {value!r} must name {root!r} in its reason, "
            f"not only in the boilerplate: {reason!r}"
        )


@pytest.mark.parametrize(
    ("tool", "value"),
    [pytest.param(t, v, id=label) for e, t, v, label in CASES if e == ALLOW],
)
def test_an_allowed_command_says_nothing(tool: str, value: str) -> None:
    """Stderr on an allowed call would be noise in every unrelated tool result."""
    assert run_hook(tool, value).stderr == ""


def test_a_payload_with_no_tool_input_is_allowed() -> None:
    """Malformed payloads must not wedge the harness on every call."""
    assert verdict(run_payload(json.dumps({"tool_name": "Bash"}))) == ALLOW


def test_an_unrecognised_tool_is_allowed() -> None:
    """A gold path on purpose: the tool name is the gate, not the path. With a
    harmless URL this would still pass if the path check ran for every tool."""
    assert verdict(run_hook("WebFetch", f"{GOLD}/x.json")) == ALLOW


def test_fails_closed_on_a_crash() -> None:
    """A crashing guard must block, not wave the call through.

    Stdin that is not JSON makes `json.load` raise before any policy runs. The
    inert-hook incident was exactly this shape: an exception escaped, the
    process exited non-zero-but-not-2, and PreToolUse proceeded unguarded.
    """
    result = run_payload("not json at all")
    assert verdict(result) == BLOCK
    assert "BLOCKED" in result.stderr


def test_the_installed_hook_is_executable() -> None:
    """Committed 100755. ruff's EXE001 fires on a shebang without the bit, so a
    hook installed by hand with the mode dropped fails lint rather than here —
    which is a confusing place to learn it. Pinned at the source instead."""
    if HOOK != DEFAULT_HOOK:
        pytest.skip("testing a candidate hook, not the installed one")
    assert HOOK.stat().st_mode & stat.S_IXUSR, f"chmod +x {HOOK}"


# The hook has to import under this, the oldest Python the project claims to
# support (CLAUDE.md: a bare interpreter here resolves to a 3.9 install), and it
# is a floor as well as a target: an interpreter older than this was never
# promised, so finding a 3.5 on the box must not produce a failure blaming the
# guard. PEP 604 (`X | Y` at runtime) needs 3.10, which is what made 3.9 the
# version the hook actually crashed under.
MIN_SUPPORTED_PYTHON = (3, 9)
PEP604_PYTHON = (3, 10)

# Names that read as types on one side of a `|`, used to tell a PEP 604 union
# from an ordinary bitwise-or on ints or sets.
TYPE_NAMES = frozenset({
    "list", "dict", "set", "frozenset", "tuple", "str", "int", "float", "bool",
    "bytes", "complex", "object", "type", "None", "Any", "Optional", "Union",
    "Callable", "Sequence", "Mapping", "Iterable", "Iterator", "Path",
})


def is_type_expression(node: ast.expr) -> bool:
    """Does this operand look like a type rather than a value?"""
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Subscript):  # list[str], dict[str, int]
        return True
    if isinstance(node, ast.Name):
        return node.id in TYPE_NAMES or node.id[:1].isupper()
    if isinstance(node, ast.Attribute):  # typing.Optional, pathlib.Path
        return node.attr in TYPE_NAMES or node.attr[:1].isupper()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return is_type_expression(node.left) or is_type_expression(node.right)
    return False


def oldest_available_python() -> tuple[str, tuple[int, int]] | None:
    """An interpreter in [MIN_SUPPORTED_PYTHON, 3.10), or None if there is none.

    Bounded at both ends on purpose. A probe whose output is not two integers —
    a Python 2, a conda banner, a shim — is not a candidate rather than an
    exception: this helper's job is to find a usable old interpreter, not to
    audit every binary named `python`.
    """
    found: list[tuple[str, tuple[int, int]]] = []
    for name in ("/usr/bin/python3", "python3.9", "python3", "python"):
        exe = shutil.which(name) if "/" not in name else (name if Path(name).exists() else None)
        if not exe:
            continue
        probe = subprocess.run(
            [exe, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if probe.returncode != 0:
            continue
        try:
            major, minor = (int(part) for part in probe.stdout.split())
        except ValueError:
            continue
        if MIN_SUPPORTED_PYTHON <= (major, minor) < PEP604_PYTHON:
            found.append((exe, (major, minor)))
    return min(found, key=lambda pair: pair[1]) if found else None


def test_the_hook_parses_under_an_older_python() -> None:
    """The regression this file exists for, pinned at last.

    The guard sat inert for weeks because `list[str] | None` in an annotation
    raised `TypeError` at *import* under Python 3.9. `from __future__ import
    annotations` fixed it. Nothing else here would notice it coming back: every
    other test runs the hook under the venv (3.11), and so does CI.

    Deliberately NOT skipped under `PROTECT_PATHS_HOOK`. Vetting a candidate
    before it is copied into `.claude/` is the one pre-install check available,
    and a candidate carrying this exact regression is what it has to catch —
    skipping here would make that run green on the one property it cannot
    otherwise see. `test_annotations_are_safe_for_the_oldest_supported_python`
    covers the same ground without needing an old interpreter to exist.
    """
    old = oldest_available_python()
    if old is None:
        pytest.skip(
            f"no interpreter in [{MIN_SUPPORTED_PYTHON}, {PEP604_PYTHON}) on this "
            "machine; the static check covers this case"
        )

    exe, version = old
    result = subprocess.run(
        [exe, str(HOOK)],
        input=json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": f"{GOLD}/x.json"}}
        ),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert "BLOCKED" in result.stderr, (
        f"under Python {version[0]}.{version[1]} ({exe}) the hook did not block "
        f"a write into {GOLD}/. An annotation that needs a newer Python raises at "
        f"import, before the __main__ block can fail closed.\n"
        f"  exit {result.returncode}, stderr: {result.stderr.strip()!r}"
    )
    assert verdict(result) == BLOCK


def test_annotations_are_safe_for_the_oldest_supported_python() -> None:
    """The same regression, pinned without needing an old interpreter present.

    `test_the_hook_parses_under_an_older_python` skips wherever no 3.9 exists —
    including CI, which installs 3.11 and whose `/usr/bin/python3` is newer. So
    the subprocess test alone leaves the regression unpinned exactly where it
    would go unnoticed longest. This reads the source instead: a PEP 604 `X | Y`
    annotation is fine under 3.9 only because `from __future__ import
    annotations` keeps annotations unevaluated. Remove that import and the hook
    raises TypeError at import, exits 1, and PreToolUse proceeds unguarded.

    Two runtime positions, not one. The `__future__` import defers *annotations*
    only: a module-level type alias like `Argv = list[str] | None` is an ordinary
    expression, evaluated on import, and still raises under 3.9 with the import
    in place. Checking annotations alone would pass that file.
    """
    # `feature_version` is what makes this a *grammar* check and not only a
    # semantics one. Without it the parse runs under the interpreter pytest
    # is on (3.11), so a `match` statement, `except*` or a PEP 695 alias
    # parses clean here and only fails on a real 3.9 — which is the test
    # below, the one that skips in CI. The PEP 604 scan that follows is
    # still needed: `X | Y` in an annotation is legal 3.9 *syntax*, so the
    # grammar accepts it and only evaluation order decides whether it runs.
    tree = ast.parse(HOOK.read_text(), feature_version=MIN_SUPPORTED_PYTHON)
    deferred = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in ast.walk(tree)
    )

    suspect: list[ast.expr] = []
    if not deferred:
        # Without the import, every annotation is evaluated on import too.
        suspect += [
            node.annotation
            for node in ast.walk(tree)
            if isinstance(node, (ast.AnnAssign, ast.arg)) and node.annotation is not None
        ]
        suspect += [
            node.returns
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns
        ]
    # Module-level alias values are evaluated regardless of the import. Only
    # type-shaped operands count, so `FLAGS = READ | WRITE` on plain ints is not
    # mistaken for a type union.
    suspect += [
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]

    offenders = sorted({
        ast.unparse(node)
        for node in suspect
        for sub in ast.walk(node)
        if isinstance(sub, ast.BinOp)
        and isinstance(sub.op, ast.BitOr)
        and any(is_type_expression(side) for side in (sub.left, sub.right))
    })
    major, minor = MIN_SUPPORTED_PYTHON
    assert not offenders, (
        f"{HOOK} evaluates PEP 604 union(s) {offenders} at import time. That "
        f"raises TypeError under Python {major}.{minor}, the hook exits 1, and "
        f"PreToolUse reads a non-2 exit as 'hook errored, proceed' — the guard "
        f"goes silently inert. Annotations are safe behind `from __future__ "
        f"import annotations`; a module-level alias is not, and must use "
        f"`typing.Optional`/`typing.Union` instead."
    )


def test_every_guarded_tool_is_routed_to_the_guard() -> None:
    """A correctly-formed hook command on a matcher that never fires is the same
    inert guard by another route, and no other test here can see it: every one of
    them invokes the hook directly and so bypasses routing entirely.

    Depends only on `.claude/settings.json`, not on the hook file, so unlike the
    invocation test it is not skipped when a candidate hook is under test.
    """
    entries = guarded_entries()
    for tool in GUARDED_TOOLS:
        assert any(matcher_matches(e.get("matcher"), tool) for e in entries), (
            f"no PreToolUse matcher routes {tool!r} to {DEFAULT_HOOK.name}, so "
            f"the guard never runs for {tool}. Matchers found: "
            f"{[e.get('matcher') for e in entries]}"
        )


def test_notebook_edit_is_routed_to_the_guard() -> None:
    """A NotebookEdit into data/gold/ is unguarded today. Pinned, not hidden."""
    if HOOK != DEFAULT_HOOK:
        pytest.skip("testing a candidate hook, not the installed one")
    assert any(
        matcher_matches(entry.get("matcher"), "NotebookEdit")
        for entry in guarded_entries()
    )


def test_the_project_configured_invocation_blocks_as_a_script() -> None:
    """The guard body sits under `if __name__ == "__main__"`, so it only runs
    when the hook is executed as a script. Imported instead, the module defines
    its functions and blocks nothing.

    That is safe only because the configured invocation is a script path, where
    `__name__` is always `"__main__"`. This pins the premise for the project's
    `.claude/settings.json` — not for `settings.local.json` or the user-level
    settings the harness also merges, which are untracked and machine-dependent.
    Every entry there naming this hook must be a `type: "command"` hook that
    hands the hook *file* to an interpreter, never `-c`/`-m`/an import.

    Then it runs that command string through a shell, which is how the harness
    runs a `type: "command"` hook, and requires that it blocks. Everything else
    in this file drives the hook as an argv under `sys.executable`; only here is
    the deployed command string itself the thing under test — interpreter,
    arguments and all. That matters because the guard's verdict is an exit code,
    and a shell can discard an exit code: `… protect_paths.py || true` exits 0
    and disables the guard completely while still looking like a correct hook.
    It is also how the original incident presented — the hook crashed at
    *import* under a different Python than the venv's and exited 1, which
    PreToolUse reads as "hook errored, proceed". The `except BaseException`
    wrapper cannot catch that; it lives inside the `__main__` block, after
    module import has already succeeded.
    """
    if HOOK != DEFAULT_HOOK:
        pytest.skip("testing a candidate hook, not the installed one")

    entries = guarded_entries()
    # Other PreToolUse hooks may exist and are none of this test's business;
    # only the ones invoking *this* guard bear on the __name__ premise.
    mine = [
        h for entry in entries for h in entry.get("hooks", [])
        if DEFAULT_HOOK.name in h.get("command", "")
    ]
    assert mine, (
        f"no PreToolUse hook in {SETTINGS} invokes {DEFAULT_HOOK.name} — "
        "the guard is not installed"
    )

    for spec in mine:
        assert spec.get("type") == "command", (
            f"{spec!r} is not a command hook; only a command hook is guaranteed "
            'to run the guard as a script with __name__ == "__main__"'
        )
        command = spec["command"]
        # The harness runs this through a shell, so a shell operator is part of
        # the contract, not noise to tokenise away. Reject them outright rather
        # than reason about them: `cmd || true`, `cmd ; exit 0` and
        # `cmd >/dev/null 2>&1 || exit 0` all swallow the exit code the guard
        # communicates its verdict with, and all still name the hook file.
        # Rejecting them also keeps the argv scan below honest: with no
        # operators, shlex tokens are close enough to the real argv for the
        # scan to mean what it says (glob and `~` expansion are not covered,
        # but an expansion still has to land on the hook file, and the
        # behavioural run below is the backstop either way).
        #
        # Bare `$` is deliberately NOT forbidden: it cannot discard an exit
        # code, and `$CLAUDE_PROJECT_DIR/...` is the documented way to make a
        # hook cwd-independent. `$(...)` is still caught, by `(` and `)`.
        forbidden = set(";|&<>`()\n") & set(command)
        assert not forbidden, (
            f"{command!r} contains shell metacharacter(s) {sorted(forbidden)}. "
            "A command hook runs through a shell, and an operator can discard "
            "the hook's exit code — which is the entire verdict. Invoke the "
            "hook as a plain command."
        )

        argv = shlex.split(command)
        # Positional, not `-c`/`-m`/an import: the hook file itself must appear
        # as an argument. Scanning beats indexing — `python3 -u hook.py` and a
        # bare `./hook.py` shebang call are both valid script invocations.
        assert any(Path(tok).name == DEFAULT_HOOK.name for tok in argv), (
            f"{command!r} never names {DEFAULT_HOOK.name} as an argument, so "
            'the __name__ == "__main__" guard would not fire'
        )
        assert "-c" not in argv and "-m" not in argv, (
            f"{command!r} runs the hook through {'-c' if '-c' in argv else '-m'}"
            ", not as a script path"
        )

        # Run the string, not the argv, and through a shell — `shell=True` is
        # the harness's semantics, and the point of this test is fidelity to
        # them. No precheck that the interpreter exists: any such check resolves
        # the name on a different basis than the run itself, so it can only
        # disagree with the thing it guards. A missing interpreter surfaces as a
        # non-zero, non-2 exit, which `verdict` rejects as loudly as an assert.
        result = subprocess.run(
            command,
            shell=True,  # the command under test is a shell command
            cwd=REPO_ROOT,
            # The harness injects CLAUDE_PROJECT_DIR into hook subprocesses; it
            # is absent from a terminal and from CI. Without it, the documented
            # `$CLAUDE_PROJECT_DIR/.claude/hooks/...` form expands to an absolute
            # path at `/`, and CPython's "no such file" exit is also 2 — the
            # guard's block code — so the run would look like a pass.
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO_ROOT)},
            input=json.dumps(
                {"tool_name": "Write", "tool_input": {"file_path": f"{GOLD}/x.json"}}
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,  # a non-zero exit is the result under test, not an error
        )
        # `shutil.which` is diagnostic only, never asserted on: it names the
        # binary the run actually used, which is the first thing worth knowing
        # when settings.json and the venv disagree about `python3`. Built
        # before `verdict`, which raises on its own for an exit code that is
        # neither 0 nor 2 — a missing interpreter exits 127 and would otherwise
        # report without any of this.
        diagnosis = (
            f"\n  command: {command!r}"
            f"\n  argv[0] resolved to {shutil.which(argv[0]) or argv[0]!r}"
            f"\n  sys.executable is {sys.executable!r}"
            f"\n  exit {result.returncode}, stderr: {result.stderr.strip()!r}"
        )
        # stderr first, deliberately. Exit 2 is not proof the guard ran: CPython
        # also exits 2 when it cannot open the script at all, so a typo'd path
        # in settings.json would satisfy the exit code and nothing else. The
        # BLOCKED marker rules that out — but it does NOT distinguish "the guard
        # refused this" from "the guard refuses everything": the crash handler in
        # the hook's __main__ block prints a BLOCKED line and exits 2, so a hook
        # that raises on every input satisfies both assertions below. That is the
        # original incident inverted — fail-shut instead of fail-open — and this
        # is the one test with fidelity to the deployed command, so it is the one
        # that has to see it. The allow-run after this loop is what does: the
        # pair of runs is the discrimination, not the marker on its own.
        assert "BLOCKED" in result.stderr, (
            f"the guard did not refuse a write into {GOLD}/, or its reason "
            f"never reached stderr.{diagnosis}"
        )
        try:
            got = verdict(result)
        except AssertionError as exc:
            raise AssertionError(f"{exc}{diagnosis}") from exc
        assert got == BLOCK, f"stderr says BLOCKED but the exit code allows.{diagnosis}"

        # The other half of the pair: the same command string, the same shell,
        # the same CLAUDE_PROJECT_DIR injection, on a path the guard has no
        # business refusing. A guard that blocks this blocks every tool call in
        # the session, which no BLOCK-only assertion can tell apart from a guard
        # that works. `data/drafts/` is the natural probe — a real directory this
        # project writes to, sitting beside the protected one, so a matcher that
        # over-reaches by a path segment shows up here rather than in production.
        allowed = subprocess.run(
            command,
            shell=True,
            cwd=REPO_ROOT,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO_ROOT)},
            input=json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": "data/drafts/x.json"},
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        allow_diagnosis = (
            f"\n  command: {command!r}"
            f"\n  exit {allowed.returncode}, stderr: {allowed.stderr.strip()!r}"
        )
        assert verdict(allowed) == ALLOW, (
            "the guard refused a write to an unprotected path. A guard that "
            "refuses everything satisfies every BLOCK assertion in this file "
            f"while blocking the whole session.{allow_diagnosis}"
        )
        assert not allowed.stderr.strip(), (
            "the guard allowed the write but still wrote to stderr; a clean "
            f"allow says nothing.{allow_diagnosis}"
        )
