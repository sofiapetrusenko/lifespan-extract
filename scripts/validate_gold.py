#!/usr/bin/env python3
"""Validate every data/gold/*.json against schema/experiment.schema.json.

Local pre-commit check for hand-labelling. Prints ALL errors for every file,
not just the first, and exits 1 if any file fails.

Usage:
    python scripts/validate_gold.py

Runs from any working directory: paths resolve relative to this file, not cwd.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:
    # Never claim "not installed". That is usually false and it hides the real
    # reason: the wrong interpreter was picked up from PATH or from this file's
    # shebang, or the package is present but its native extension was built for
    # a different architecture. Both raise ImportError; only the message tells
    # them apart, so the message is reproduced verbatim.
    sys.exit(
        f"cannot import jsonschema using {sys.executable}\n"
        f"  reason: {exc}\n"
        "  Run via the project venv:  .venv/bin/python scripts/validate_gold.py\n"
        "  Or reinstall it there:     .venv/bin/pip install -r requirements-dev.txt"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "experiment.schema.json"
GOLD_DIR = REPO_ROOT / "data" / "gold"

# Root-level annotation in the schema file. Not a JSON Schema keyword: validators
# ignore unknown keywords, so this carries the version without affecting validation.
SCHEMA_VERSION_KEY = "x-schema-version"


def format_path(error) -> str:
    """Render an error location as experiments[0].organism.value."""
    out = ""
    for part in error.absolute_path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def sort_key(error):
    """Most specific error first.

    A failed `value` inside a claim wrapper produces two errors: a deep, useful
    one ("'D. melanogaster' is not one of [...]") and a shallow, misleading one
    ("Unevaluated properties are not allowed"). The deep one is the real
    diagnosis, so it leads. Both are still printed.
    """
    return (-len(error.absolute_path), list(map(str, error.absolute_path)))


def load_schema() -> tuple[Draft202012Validator, str]:
    """Return the validator and the schema's own declared version."""
    if not SCHEMA_PATH.exists():
        sys.exit(f"schema not found: {SCHEMA_PATH}")
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
    except json.JSONDecodeError as exc:
        sys.exit(f"schema is not valid JSON: {SCHEMA_PATH}\n  {exc}")

    # A malformed schema silently accepts everything, so check it explicitly.
    Draft202012Validator.check_schema(schema)

    # Read the version rather than hardcoding it: a stale literal here would
    # misreport which schema a gold file was checked against.
    version = schema.get(SCHEMA_VERSION_KEY)
    if not version:
        sys.exit(
            f"{SCHEMA_PATH.name} has no {SCHEMA_VERSION_KEY!r} key. "
            "Add it at the schema root; it is the single source of truth "
            "for the schema's version."
        )
    return Draft202012Validator(schema), version


def check_file(path: Path, validator: Draft202012Validator) -> list[str]:
    """Return a list of problems with one gold file. Empty means valid."""
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]

    errors = sorted(validator.iter_errors(document), key=sort_key)
    return [f"{format_path(e)}: {e.message}" for e in errors]


def main() -> int:
    validator, schema_version = load_schema()

    if not GOLD_DIR.is_dir():
        sys.exit(f"gold directory not found: {GOLD_DIR}")

    gold_files = sorted(GOLD_DIR.glob("*.json"))
    if not gold_files:
        # Legitimate during Phase 0, and stated out loud rather than passing
        # silently — an empty glob must not look like a clean run.
        print(f"No gold files found in {GOLD_DIR.relative_to(REPO_ROOT)}/")
        print("Nothing to validate.")
        return 0

    failed = 0
    for path in gold_files:
        name = path.relative_to(REPO_ROOT)
        problems = check_file(path, validator)
        if problems:
            failed += 1
            print(f"FAIL  {name}  ({len(problems)} error(s))")
            for problem in problems:
                print(f"        {problem}")
        else:
            print(f"ok    {name}")

    print()
    total = len(gold_files)
    if failed:
        print(f"{failed} of {total} file(s) failed validation.")
        return 1
    print(f"All {total} file(s) valid against schema v{schema_version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
