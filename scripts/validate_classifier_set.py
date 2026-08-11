#!/usr/bin/env python3
"""Validate data/classifier_set/negatives.json: structure, vocabulary, uniqueness.

Local pre-commit check for the Phase 3 classifier eval set, and the sibling of
`validate_gold.py`. Prints ALL problems, not just the first, and exits 1 if any
are found.

Usage:
    python scripts/validate_classifier_set.py
    python scripts/validate_classifier_set.py --resolve   # also check PMIDs at PubMed

**Why this is not JSON Schema.** `validate_gold.py` delegates to a schema
because a gold record is a deep tree with a versioned contract that a model will
one day have to produce. This file is a flat list under a closed vocabulary, and
the properties worth enforcing — every category is one of the five, no PMID
appears twice, no reason is left empty — are as clearly expressed in twenty
lines of Python as in a second schema file that would then need its own version.

**`--resolve` is opt-in and stays out of CI.** Checking that every PMID is a
real PubMed record is worth doing, and worth doing by hand when the file
changes; it is not worth a network call on every push. An identifier that was
valid when it was added does not stop being valid, and CI that fails because
NCBI is having an outage teaches people to ignore CI. The elink outage of
2026-08-11 is the local precedent.

These are **not** gold records. No experiment is extracted, no schema record
exists, and nothing here is ever written to `data/gold/`. An entry carries a
binary label, a category, and a human-readable reason.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SET_PATH = REPO_ROOT / "data" / "classifier_set" / "negatives.json"

PMID_RE = re.compile(r"^\d+$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

SOURCES = frozenset({"pubmed", "biorxiv"})

# Every entry must carry exactly these keys. A closed set rather than a minimum,
# so a typo'd key is an error instead of silently ignored data.
ENTRY_KEYS = frozenset({"pmid", "doi", "title", "source", "category", "reason", "reviewed"})

TOP_LEVEL_KEYS = frozenset({"set_version", "label", "description", "categories", "entries"})


def load(path: Path | None = None) -> dict[str, Any]:
    # Resolved at call time, not bound as a default: a default argument would
    # capture SET_PATH at import and make the module untestable against any
    # other file.
    path = path or SET_PATH
    if not path.is_file():
        raise SystemExit(f"classifier set not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path.name} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def check(document: Any) -> list[str]:
    """Return every problem found. Empty means the file is well-formed."""
    problems: list[str] = []

    if not isinstance(document, dict):
        return [f"top level is {type(document).__name__}, expected an object"]

    unexpected = sorted(set(document) - TOP_LEVEL_KEYS)
    missing = sorted(TOP_LEVEL_KEYS - set(document))
    if unexpected:
        problems.append(f"unexpected top-level key(s): {', '.join(unexpected)}")
    if missing:
        problems.append(f"missing top-level key(s): {', '.join(missing)}")
    if missing:
        # Everything below reads these keys; carrying on would report noise.
        return problems

    if document["label"] != "negative":
        problems.append(f"label is {document['label']!r}; this file holds negatives only")

    categories = document["categories"]
    if not isinstance(categories, list) or not categories:
        return problems + ["categories must be a non-empty array"]

    known: set[str] = set()
    for index, category in enumerate(categories):
        if not isinstance(category, dict) or "name" not in category:
            problems.append(f"categories[{index}] has no name")
            continue
        name = category["name"]
        if name in known:
            problems.append(f"categories[{index}]: duplicate category name {name!r}")
        known.add(name)
        if not str(category.get("description", "")).strip():
            problems.append(f"categories[{index}] ({name}): description is empty")

    entries = document["entries"]
    if not isinstance(entries, list) or not entries:
        return problems + ["entries must be a non-empty array"]

    seen_ids: dict[str, int] = {}
    for index, entry in enumerate(entries):
        problems += _check_entry(entry, index, known, seen_ids)

    unused = sorted(known - {e.get("category") for e in entries if isinstance(e, dict)})
    if unused:
        # Not a failure: a category with no entry yet is a gap to fill, and
        # saying so is more useful than letting it sit unnoticed.
        problems.append(f"NOTE: category with no entries: {', '.join(unused)}")

    return problems


def _check_entry(
    entry: Any, index: int, known: set[str], seen_ids: dict[str, int]
) -> list[str]:
    where = f"entries[{index}]"
    if not isinstance(entry, dict):
        return [f"{where} is {type(entry).__name__}, expected an object"]

    problems: list[str] = []
    unexpected = sorted(set(entry) - ENTRY_KEYS)
    missing = sorted(ENTRY_KEYS - set(entry))
    if unexpected:
        problems.append(f"{where}: unexpected key(s): {', '.join(unexpected)}")
    if missing:
        problems.append(f"{where}: missing key(s): {', '.join(missing)}")
        return problems

    pmid, doi = entry["pmid"], entry["doi"]
    if pmid is None and doi is None:
        problems.append(f"{where}: needs a pmid or a doi; both are null")
    if pmid is not None and not (isinstance(pmid, str) and PMID_RE.match(pmid)):
        problems.append(f"{where}: pmid {pmid!r} is not digits-only")
    if doi is not None and not (isinstance(doi, str) and DOI_RE.match(doi)):
        problems.append(f"{where}: doi {doi!r} is not a DOI")

    # Identity is whichever the entry is keyed by. A paper must not appear twice
    # under either identifier: a duplicate silently doubles that paper's weight
    # in precision and recall.
    identity = f"pmid:{pmid}" if pmid else f"doi:{doi}"
    if identity in seen_ids:
        problems.append(f"{where}: duplicate of entries[{seen_ids[identity]}] ({identity})")
    else:
        seen_ids[identity] = index

    if entry["source"] not in SOURCES:
        problems.append(f"{where}: source {entry['source']!r} not in {sorted(SOURCES)}")
    if not str(entry["title"] or "").strip():
        problems.append(f"{where}: title is empty")
    if entry["category"] not in known:
        problems.append(f"{where}: category {entry['category']!r} is not a declared category")
    if not str(entry["reason"] or "").strip():
        problems.append(f"{where}: reason is empty — every negative states why it is one")
    if not isinstance(entry["reviewed"], bool):
        problems.append(f"{where}: reviewed must be true or false, got {entry['reviewed']!r}")

    return problems


def resolve_ids(entries: list[dict[str, Any]]) -> list[str]:
    """Check every PMID against PubMed. Network; opt-in via --resolve."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pubmed_lookup

    problems = []
    for index, entry in enumerate(entries):
        pmid = entry.get("pmid")
        if not pmid:
            continue
        try:
            record = pubmed_lookup.fetch_record(str(pmid))
        except Exception as exc:  # noqa: BLE001 - one bad id must not abort the sweep
            problems.append(f"entries[{index}]: pmid {pmid} did not resolve: {exc}")
            continue
        if record.title.rstrip(".").lower() != str(entry["title"]).rstrip(".").lower():
            problems.append(
                f"entries[{index}]: pmid {pmid} resolves to a different title\n"
                f"    file:   {entry['title']}\n"
                f"    PubMed: {record.title}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_classifier_set.py",
        description="Structural checks on the classifier negative set.",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="also check every PMID against PubMed (network; not run in CI)",
    )
    args = parser.parse_args(argv)

    document = load()
    problems = check(document)

    if args.resolve and isinstance(document, dict) and isinstance(document.get("entries"), list):
        problems += resolve_ids(document["entries"])

    name = SET_PATH.relative_to(REPO_ROOT)
    failures = [p for p in problems if not p.startswith("NOTE:")]
    for problem in problems:
        print(f"  {problem}")

    if failures:
        print(f"\n{name}: {len(failures)} problem(s).")
        return 1

    entries = document["entries"]
    unreviewed = sum(1 for e in entries if not e.get("reviewed"))
    print(f"\n{name}: {len(entries)} entries valid, {len(document['categories'])} categories.")
    if unreviewed:
        # Loud but not fatal. An entry only counts toward the eval once a human
        # has read the abstract and agreed it is a negative; until then the file
        # is a proposal, and the count is the honest way to say so.
        print(f"{unreviewed} entry/entries not yet human-reviewed — these do not count yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
