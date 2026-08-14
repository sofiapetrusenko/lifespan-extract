#!/usr/bin/env python3
"""PreToolUse guard: blocks writes to protected paths. Reads are allowed.

Exit 0 = allow. Exit 2 = block; stderr is fed back to the agent.

Policy (CLAUDE.md): `data/gold/` is human-controlled ground truth and `.claude/`
is orchestration config. An agent never writes to either. The one permitted
programmatic write into `data/gold/` is `scripts/check_gold.py --promote`, run
by the *human* — so this hook blocks an agent-invoked `--promote` outright
rather than trusting the rule to be read.

The Bash rule is "is a protected path the TARGET of a write", not "does this
line contain a protected path and a write-ish character somewhere". The old
version was the latter, and refused `head -c 300 data/gold/x.json 2>/dev/null`
because the `>` in `2>/dev/null` matched. Reads must not be blocked: an agent
that cannot read the gold set cannot check its work against it.

Conservative where it cannot be sure. A line that cannot be tokenised, or that
reaches for `eval`, a subshell or an inline interpreter while naming a protected
path, is blocked unparsed — being wrong in the allow direction costs ground
truth, being wrong in the block direction costs one round trip.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

# Path prefixes, matched as whole path segments so `data/gold`, `./data/gold/x`
# and `/abs/path/data/gold/x` all count and `data/golden/` does not.
PROTECTED_RE = re.compile(r"(?:^|/)(?:data/gold|\.claude)(?:/|$)")

# Loose "is this line even about a protected path" test, used to decide whether
# an unparseable or opaque command is worth blocking.
MENTIONS_RE = re.compile(r"data/gold|\.claude")

# Redirection targets: `> f`, `>> f`, `2> f`, `&> f`, `>| f`.
REDIRECT_RE = re.compile(r"(?:&|\d+)?>{1,2}\|?\s*([^\s;|&<>()]+)")

# Every path argument is a write or a destruction.
WRITES_ANY_ARG = frozenset({
    "rm", "rmdir", "unlink", "shred", "mv", "tee", "truncate", "touch", "mkdir",
    "chmod", "chown", "chgrp", "ln", "dd", "patch", "zip", "tar", "split",
})

# Only the final path argument is written; earlier ones are sources.
WRITES_LAST_ARG = frozenset({"cp", "rsync", "install"})

# Write only with an in-place flag; otherwise they stream to stdout.
IN_PLACE = {
    "sed": ("-i", "--in-place"),
    "perl": ("-i",),
    "ruby": ("-i",),
    "gawk": ("-i", "--in-place"),
}

# git subcommands that only read. Anything else touching a protected path is
# blocked: `git checkout`, `restore`, `rm`, `mv`, `clean`, `apply`, `stash` all
# write, and the list of writers is longer and less stable than the readers.
GIT_READONLY = frozenset({
    "status", "log", "diff", "show", "ls-files", "ls-tree", "blame", "cat-file",
    "rev-parse", "describe", "branch", "remote", "config", "shortlog", "grep",
    "check-ignore", "for-each-ref", "reflog", "tag", "whatchanged",
})

# Opaque enough that arguments say nothing about what will be written.
INTERPRETERS = frozenset({
    "python", "python3", "python3.11", "py", "perl", "ruby", "node", "osascript",
    "sh", "bash", "zsh", "fish", "dash", "ksh", "eval", "exec", "source",
})

# Shell metacharacters that hide the real command from a token scan.
OPAQUE_RE = re.compile(r"\$\(|`|<\(|\beval\b|\bexec\b")

SEGMENT_RE = re.compile(r"\|\||&&|[;|\n]")

# Wrappers that prefix a real command; skip them to find the actual argv[0].
PREFIXES = frozenset({
    "sudo", "env", "nohup", "time", "command", "builtin", "xargs", "nice",
    "stdbuf", "timeout", "arch", "caffeinate",
})

GOLD_NOTE = (
    "data/gold/ is human-controlled ground truth; .claude/ is orchestration "
    "config. Reads are fine — writes are not. The only permitted write into "
    "data/gold/ is `scripts/check_gold.py --promote`, run by the human. "
    "Do not work around this block, including by rephrasing the command: "
    "stop and report it."
)


def is_protected(token: str) -> bool:
    return bool(PROTECTED_RE.search(token.strip().strip("'\"")))


def block(reason: str) -> None:
    sys.stderr.write(f"BLOCKED: {reason}\n{GOLD_NOTE}\n")
    sys.exit(2)


def split_argv(segment: str) -> list[str] | None:
    """Tokenise one command segment, or None if the quoting is unbalanced."""
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return None


def strip_prefixes(argv: list[str]) -> list[str]:
    """Drop `sudo`, `env FOO=bar`, and friends to reach the real command."""
    while argv:
        head = argv[0]
        if "=" in head and not head.startswith("-") and "/" not in head.split("=")[0]:
            argv = argv[1:]  # VAR=value assignment
            continue
        if head.rsplit("/", 1)[-1] in PREFIXES:
            argv = argv[1:]
            continue
        return argv
    return argv


def path_args(argv: list[str]) -> list[str]:
    return [token for token in argv[1:] if not token.startswith("-")]


# check_gold.py flags that write into data/gold/. Both are human-invoked:
# --promote moves a draft in, --refresh-quotes --write edits records in place.
HUMAN_ONLY_FLAGS = ("--promote", "--refresh-quotes")


def check_promote(command: str) -> None:
    """check_gold.py's writing modes are the human's to run."""
    if "check_gold" not in command:
        return
    for flag in HUMAN_ONLY_FLAGS:
        if re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|=|$)", command):
            block(
                f"this command runs check_gold.py {flag}, which writes into "
                "data/gold/. That is a human-invoked step. Hand the exact command "
                "back to the human instead of running it."
            )


def check_redirections(command: str) -> None:
    for target in REDIRECT_RE.findall(command):
        if is_protected(target):
            block(f"shell redirection writes to the protected path {target!r}.")


def check_segment(segment: str) -> None:
    argv = split_argv(segment)
    if argv is None:
        if MENTIONS_RE.search(segment):
            block("could not parse this command (unbalanced quotes) and it names "
                  "a protected path, so it is refused unread.")
        return

    argv = strip_prefixes(argv)
    if not argv:
        return

    name = argv[0].rsplit("/", 1)[-1]
    protected = [token for token in path_args(argv) if is_protected(token)]

    if name in INTERPRETERS and MENTIONS_RE.search(segment):
        block(f"{name!r} is given a protected path and its effect cannot be read "
              "from the command line. Use the Read tool, or a script whose "
              "behaviour is known.")

    if name == "git":
        subcommand = next((t for t in argv[1:] if not t.startswith("-")), "")
        if subcommand not in GIT_READONLY and MENTIONS_RE.search(segment):
            block(f"`git {subcommand}` is not a known read-only git subcommand "
                  "and it names a protected path.")
        return

    if not protected:
        return

    if name in WRITES_ANY_ARG:
        block(f"`{name}` writes to or destroys {protected[0]!r}.")

    if name in WRITES_LAST_ARG:
        destination = path_args(argv)[-1:]
        if destination and is_protected(destination[0]):
            block(f"`{name}` writes to {destination[0]!r}.")
        return

    flags = IN_PLACE.get(name)
    if flags and any(arg.startswith(flags) for arg in argv[1:]):
        block(f"`{name}` edits {protected[0]!r} in place.")


def check_bash(command: str) -> None:
    check_promote(command)
    check_redirections(command)

    if OPAQUE_RE.search(command) and MENTIONS_RE.search(command):
        block("this command uses a subshell, backticks, eval or exec while "
              "naming a protected path; its effect cannot be determined.")

    for segment in SEGMENT_RE.split(command):
        if segment.strip():
            check_segment(segment)


def main() -> None:
    payload = json.load(sys.stdin)
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if is_protected(path):
            block(f"{path} is protected (human-only).")

    if tool == "Bash":
        check_bash(str(tool_input.get("command", "")))

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — fail closed, never fail open
        print(
            f"BLOCKED: guard crashed ({type(exc).__name__}: {exc}). "
            "Refusing the tool call rather than proceeding unguarded. "
            "Fix .claude/hooks/protect_paths.py before continuing.",
            file=sys.stderr,
        )
        sys.exit(2)
