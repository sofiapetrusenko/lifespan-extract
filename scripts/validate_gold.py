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
except ImportError:
    sys.exit("jsonschema is not installed. Run: pip install jsonschema")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "experiment.schema.json"
GOLD_DIR = REPO_ROOT / "data" / "gold"


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


def load_schema() -> Draft202012Validator:
    if not SCHEMA_PATH.exists():
        sys.exit(f"schema not found: {SCHEMA_PATH}")
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
    except json.JSONDecodeError as exc:
        sys.exit(f"schema is not valid JSON: {SCHEMA_PATH}\n  {exc}")

    # A malformed schema silently accepts everything, so check it explicitly.
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def check_file(path: Path, validator: Draft202012Validator) -> list[str]:
    """Return a list of problems with one gold file. Empty means valid."""
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]

    errors = sorted(validator.iter_errors(document), key=sort_key)
    return [f"{format_path(e)}: {e.message}" for e in errors]


def main() -> int:
    validator = load_schema()

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
    print(f"All {total} file(s) valid against schema v0.2.0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
