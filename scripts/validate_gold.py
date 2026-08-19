#!/usr/bin/env python3
"""Validate every data/gold/*.json against schema/experiment.schema.json.

Local pre-commit check for hand-labelling. Prints ALL errors for every file,
not just the first, and exits 1 if any file fails.

Usage:
    python scripts/validate_gold.py

Runs from any working directory: paths resolve relative to this file, not cwd.
"""

from __future__ import annotations

import argparse
import hashlib
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

# `sha256sum`/`shasum -a 256` format: "<hex>  <name>", two spaces, one per line.
# Deliberately a standard format rather than JSON, so the human can verify the
# set with `shasum -a 256 -c MANIFEST.sha256` and never has to trust this script
# to audit its own integrity claim.
MANIFEST_NAME = "MANIFEST.sha256"
MANIFEST_LINE = "{digest}  {name}\n"

# A floor, not a target. Validating only the files that happen to be present
# means an empty directory validates clean, and so does one with nine of ten —
# deletion is the one corruption a per-file check cannot see. `>=` rather than
# `==` so the set can grow without editing this, but never shrink silently.
EXPECTED_GOLD_FILES = 10  # PLAN.md Phase 0: ten hand-labeled papers

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


def digest(path: Path) -> str:
    """SHA-256 of a file's bytes, as lowercase hex."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    """Parse a sha256sum-format manifest into {filename: digest}.

    Raises ValueError on a malformed line rather than skipping it: a manifest
    that is partly unreadable protects only the part that parsed, and silently
    covering nine of ten files is the failure this whole mechanism exists to
    make impossible.
    """
    entries: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        hexdigest, separator, name = line.partition("  ")
        if not separator or len(hexdigest) != 64 or not name.strip():
            raise ValueError(f"{path.name} line {number} is not '<sha256>  <name>': {line!r}")
        entries[name.strip()] = hexdigest.strip()
    return entries


def manifest_problems(gold_files: list[Path], entries: dict[str, str]) -> list[str]:
    """Return every discrepancy between the files on disk and the manifest.

    All three directions, because each is a different failure: a changed file
    is an edit, a file absent from the manifest is an addition that skipped the
    human-run regeneration, and an entry whose file is gone is a deletion the
    per-file checks cannot see because there is nothing left to check.
    """
    problems: list[str] = []
    on_disk = {path.name: path for path in gold_files}

    for name in sorted(on_disk):
        recorded = entries.get(name)
        if recorded is None:
            problems.append(
                f"{name}: present in {GOLD_DIR.name}/ but absent from {MANIFEST_NAME}"
            )
            continue
        found = digest(on_disk[name])
        if found != recorded:
            problems.append(
                f"{name}: content does not match {MANIFEST_NAME}\n"
                f"        manifest: {recorded}\n"
                f"        on disk : {found}"
            )

    for name in sorted(set(entries) - set(on_disk)):
        problems.append(
            f"{name}: listed in {MANIFEST_NAME} but missing from {GOLD_DIR.name}/"
        )
    return problems


def write_manifest(gold_files: list[Path], path: Path) -> int:
    """Regenerate the manifest. Human-only — see the note in main()."""
    path.write_text(
        "".join(
            MANIFEST_LINE.format(digest=digest(f), name=f.name)
            for f in sorted(gold_files, key=lambda f: f.name)
        )
    )
    print(f"wrote {len(gold_files)} digest(s) to {path.relative_to(REPO_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help=(
            f"regenerate {MANIFEST_NAME} from the files currently in "
            f"{GOLD_DIR.name}/. Writes into the gold set, so it is the human's "
            "to run, like check_gold.py --promote."
        ),
    )
    args = parser.parse_args(argv)

    validator, schema_version = load_schema()

    if not GOLD_DIR.is_dir():
        sys.exit(f"gold directory not found: {GOLD_DIR}")

    gold_files = sorted(GOLD_DIR.glob("*.json"))
    if len(gold_files) < EXPECTED_GOLD_FILES:
        # Was a pass with "Nothing to validate." while Phase 0 was still
        # labelling. Phase 0 is merged, so today an empty or thinned directory
        # is a deletion, not a work-in-progress — and every other check here is
        # per-file, so it is the one corruption none of them can see.
        where = GOLD_DIR.relative_to(REPO_ROOT)
        print(
            f"{where}/ holds {len(gold_files)} file(s); expected at least "
            f"{EXPECTED_GOLD_FILES}.",
            file=sys.stderr,
        )
        print(
            "The gold set is human-controlled ground truth: files are added by "
            "hand and never removed by tooling. If this is a deliberate change, "
            "update EXPECTED_GOLD_FILES in this script in the same commit.",
            file=sys.stderr,
        )
        return 1

    manifest = GOLD_DIR / MANIFEST_NAME
    if args.write_manifest:
        return write_manifest(gold_files, manifest)

    if not manifest.is_file():
        print(f"{manifest.relative_to(REPO_ROOT)} not found.", file=sys.stderr)
        print(
            "Every gold file's digest is recorded there, and without it a "
            "silent edit to a record is indistinguishable from the record. "
            "Generate it once, by hand:\n"
            "    python scripts/validate_gold.py --write-manifest",
            file=sys.stderr,
        )
        return 1

    try:
        entries = read_manifest(manifest)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    drift = manifest_problems(gold_files, entries)
    if drift:
        print(f"FAIL  {manifest.relative_to(REPO_ROOT)}  ({len(drift)} discrepancy(ies))")
        for problem in drift:
            print(f"        {problem}")
        print()
        print(
            "The gold set is human-controlled ground truth. If a change here is "
            "deliberate, regenerate the manifest in the same commit:\n"
            "    python scripts/validate_gold.py --write-manifest",
            file=sys.stderr,
        )
        return 1
    print(f"ok    {manifest.relative_to(REPO_ROOT)}  ({len(entries)} digest(s))")

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
