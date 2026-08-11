#!/usr/bin/env python3
"""Seed a gold-set draft from a PMID or a bioRxiv DOI: metadata and abstract.

Usage:
    python scripts/scaffold_gold.py <pmid|doi> <slug>
    python scripts/scaffold_gold.py 19587680 harrison2009
    python scripts/scaffold_gold.py 10.1101/2025.08.31.673254 green2025

The identifier is dispatched on its shape — all digits is a PMID, `10.NNNN/...`
is a bioRxiv DOI — because those two forms cannot be confused for each other and
a flag would be one more thing to get wrong. A preprint record comes out with
`source: "biorxiv"` and `pmid: null`, which the schema has always permitted:
`paper.required` is doi/title/year/source, and `pmid` is documented "null for
preprints not yet indexed".

Writes `data/drafts/<slug>.json`. It never writes to `data/gold/` — that
directory is human-labelled ground truth, and moving a finished draft into it
is a deliberate human act, not a side effect of running a script.

**This tool does not extract anything.** It fills in the four things PubMed
already knows for certain — pmid, doi, title, journal, year — and embeds the
abstract so the labeller can read it in the same buffer they are typing into.
Every extracted value is a placeholder. Guessing even the easy ones would
defeat the point of a hand-labelled gold set: the set exists to measure a
model, so it cannot contain machine output.

Two consequences of the schema worth knowing before reading the output:

* **A skeleton cannot be both valid and non-committal.** `organism`,
  `intervention.type` and `lifespan_effect.direction` are closed enums with no
  "unlabelled" member, and `experiments` has `minItems: 1`. So the skeleton has
  to assert *something*. It asserts the least-committal member available and
  marks the record as a draft in two machine-checkable ways — an
  `experiment_id` ending in `-todo`, and a `notes` string starting with
  `DRAFT`. `check_gold.py` fails on both, so a half-filled draft cannot reach
  the gold set unnoticed.
* **`_abstract` and `_journal` are underscore-prefixed on purpose.** The schema
  root is `additionalProperties: false` and has no home for either, so they
  would fail validation as ordinary keys. `check_gold.py` strips every
  top-level `_`-prefixed key before validating, which is what makes the draft
  "valid against the schema" in the sense that matters.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "experiment.schema.json"
DRAFTS_DIR = REPO_ROOT / "data" / "drafts"

SCHEMA_VERSION_KEY = "x-schema-version"

# Slug doubles as the filename and as the first segment of `experiment_id`,
# whose pattern is ^[a-z0-9]+(-[a-z0-9]+)+$. Restricting it here means a bad
# slug fails at the CLI with a clear message rather than as a schema error
# three steps later. It also rules out path separators.
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Identifier dispatch. The two forms are disjoint — a PMID is digits only and a
# DOI must start `10.` and contain a slash — so the shape decides which lookup
# runs and a mistyped identifier fails at the CLI rather than as a confusing
# error from the wrong client.
PMID_RE = re.compile(r"^\d+$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# The two draft markers. `check_gold.py` imports these and fails on either, so
# they are the reason a placeholder record cannot be mistaken for a labelled
# one. Changing them means changing both tools.
DRAFT_ID_SUFFIX = "-todo"
DRAFT_NOTE_PREFIX = "DRAFT"

DRAFT_NOTE = (
    "DRAFT — scaffolded from the source record by scripts/scaffold_gold.py. "
    "Every value in this experiment is a placeholder, including organism, "
    "intervention.type and lifespan_effect.direction, which the schema forces "
    "to a closed enum with no unlabelled member. Replace them all, give the "
    "experiment_id its real <first-author-year>-<organism>-<agent> form, and "
    "delete this note before moving the file into data/gold/."
)

# Least-committal member of each closed enum. `other` and `no_effect` are not
# claims about the paper; they are the values that read as obviously unfilled
# next to the DRAFT markers.
PLACEHOLDER_ORGANISM = "other"
PLACEHOLDER_INTERVENTION_TYPE = "other"
PLACEHOLDER_DIRECTION = "no_effect"

# Matches the "strain-like fields use the literal string" convention the schema
# documents on `strain`.
NOT_REPORTED = "not_reported"


def claim(value: Any) -> dict[str, Any]:
    """Wrap a placeholder value in the provenance envelope every claim carries.

    `source_quote` is null because there is nothing to quote yet, and the schema
    permits null exactly when the value is absent. `confidence` is "low" — the
    honest reading of a value nobody has looked at.
    """
    return {
        "value": value,
        "source_quote": None,
        "confidence": "low",
        "extracted_from": "abstract",
    }


def build_skeleton(record, slug: str, schema_version: str) -> dict[str, Any]:
    """Return the draft document for one PubMed record.

    Pure: no I/O, no network. `record` is a `pubmed_lookup.PubMedRecord`, but
    only its attributes are touched, so a stand-in works in tests.

    `doi` and `year` are omitted rather than faked when the source does not
    report them. Both are schema-required, so the draft is then invalid — which
    is the point: the missing field shows up as a validation error naming it,
    instead of as a plausible-looking wrong value nobody rechecks.

    Works for a `pubmed_lookup.PubMedRecord` or a `biorxiv_lookup.BioRxivRecord`
    without asking which it has: both carry `.source`, `.pmid`, `.doi`, `.title`,
    `.year`, `.journal` and `.abstract`, and for a preprint `.pmid` is None and
    `.journal` is the server name.
    """
    paper: dict[str, Any] = {}
    if record.doi:
        paper["doi"] = record.doi
    paper["title"] = record.title
    if record.year is not None:
        paper["year"] = record.year
    paper["source"] = record.source
    paper["pmid"] = record.pmid

    document: dict[str, Any] = {
        "schema_version": schema_version,
        "paper": paper,
        "experiments": [_skeleton_experiment(slug)],
    }

    # Underscore-prefixed, so `check_gold.py` strips them before validating and
    # the schema's `additionalProperties: false` root stays satisfied.
    document["_journal"] = record.journal
    document["_abstract"] = record.abstract
    return document


def _skeleton_experiment(slug: str) -> dict[str, Any]:
    """One unlabelled experiment entry. `experiments` has minItems: 1."""
    return {
        "experiment_id": f"{slug}{DRAFT_ID_SUFFIX}",
        "organism": claim(PLACEHOLDER_ORGANISM),
        "strain": claim(NOT_REPORTED),
        "sex": claim(NOT_REPORTED),
        "sample_size": claim(None),
        "intervention": {
            "type": claim(PLACEHOLDER_INTERVENTION_TYPE),
            "agent": claim(NOT_REPORTED),
            "dose": claim(None),
            "age_at_start": claim(None),
        },
        "mechanism": claim(None),
        "lifespan_effect": {
            "direction": claim(PLACEHOLDER_DIRECTION),
            "median_change_pct": claim(None),
            "mean_change_pct": claim(None),
            "max_change_pct": claim(None),
            "p_value": claim(None),
        },
        "notes": DRAFT_NOTE,
    }


def read_schema_version(schema_path: Path = SCHEMA_PATH) -> str:
    """Return the schema's own declared version.

    Read rather than hardcoded, for the reason `validate_gold.py` gives: a stale
    literal would misreport which schema a record was written against.
    """
    if not schema_path.is_file():
        raise SystemExit(f"schema not found: {schema_path}")
    schema = json.loads(schema_path.read_text())
    version = schema.get(SCHEMA_VERSION_KEY)
    if not version:
        raise SystemExit(
            f"{schema_path.name} has no {SCHEMA_VERSION_KEY!r} key; "
            "cannot stamp a draft with a schema version."
        )
    return version


def validation_errors(document: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> list[str]:
    """Return schema errors for `document`, with its private keys stripped.

    Uses the same stripping rule as `check_gold.py`, so "scaffold says it is
    valid" and "the checker says it is valid" cannot disagree.
    """
    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema)
    stripped = {k: v for k, v in document.items() if not k.startswith("_")}
    errors = sorted(validator.iter_errors(stripped), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{_format_path(e)}: {e.message}" for e in errors]


def _format_path(error) -> str:
    out = ""
    for part in error.absolute_path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<root>"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scaffold_gold.py",
        description="Seed data/drafts/<slug>.json from a PMID. Metadata and abstract only.",
    )
    parser.add_argument(
        "identifier",
        help="PubMed ID (digits only) or bioRxiv DOI (10.NNNN/...)",
    )
    parser.add_argument("slug", help="draft filename stem, e.g. harrison2009")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing draft (refused by default, so hand edits are never clobbered)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the abstract cache and refetch from the source",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, fetch: Any | None = None) -> int:
    """Run the CLI. `fetch` is injectable so tests never touch the network."""
    args = parse_args(argv)

    if not SLUG_RE.match(args.slug):
        raise SystemExit(
            f"slug {args.slug!r} must be lowercase letters, digits and single hyphens "
            "(it becomes both the filename and the first segment of experiment_id)"
        )

    destination = DRAFTS_DIR / f"{args.slug}.json"
    # Checked before the network call: failing after a fetch would waste the
    # request and, worse, read as though the write had been attempted.
    if destination.exists() and not args.force:
        raise SystemExit(
            f"{destination.relative_to(REPO_ROOT)} already exists. "
            "Pass --force to overwrite it, or pick another slug."
        )

    if fetch is None:
        # Imported here, not at module scope: `build_skeleton` and the schema
        # round-trip are worth testing without httpx installed.
        fetch = _lookup_for(args.identifier)

    record = fetch(args.identifier, refresh=args.refresh)

    document = build_skeleton(record, args.slug, read_schema_version())
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")

    _report(record, document, destination)
    return 0


def _lookup_for(identifier: str):
    """Return the fetch function for this identifier's shape.

    Raises `SystemExit` rather than defaulting to one of the two: a typo that
    silently became a PubMed query would report "PMID not found" for a DOI, and
    the operator would go looking in the wrong database.
    """
    if PMID_RE.match(identifier):
        from pubmed_lookup import fetch_record

        return fetch_record
    if DOI_RE.match(identifier):
        from biorxiv_lookup import fetch_record

        return fetch_record
    raise SystemExit(
        f"{identifier!r} is neither a PMID (digits only) nor a DOI (10.NNNN/suffix)"
    )


def _report(record, document: dict[str, Any], destination: Path) -> None:
    unknown = f"(not reported by {record.source})"
    print(f"wrote {destination.relative_to(REPO_ROOT)}")
    print(f"  source   {record.source}")
    print(f"  pmid     {record.pmid if record.pmid else '(none — preprint)'}")
    print(f"  doi      {record.doi or unknown}")
    print(f"  title    {_truncate(record.title)}")
    print(f"  journal  {record.journal or unknown}")
    print(f"  year     {record.year if record.year is not None else unknown}")

    posted = getattr(record, "posted", None)
    if posted:
        latest = getattr(record, "latest_posted", None)
        version = getattr(record, "version", None)
        revised = f", latest v{version} {latest}" if latest and latest != posted else ""
        print(f"  posted   {posted}{revised} — year is taken from the first posting")

    # A preprint that has since been published is the same work reachable
    # through PubMed, usually with a PMID and often with PMC full text. Said
    # loudly because the published version is the better record and the
    # difference is invisible once the draft is written.
    if getattr(record, "is_published", False):
        print(f"  PUBLISHED  bioRxiv reports journal DOI {record.published_doi} for this "
              "preprint.\n             The published version will have a PMID and may be in "
              "PMC; prefer it\n             unless this draft is deliberately about the preprint.")

    if record.abstract:
        print(f"  abstract {len(record.abstract)} chars, embedded as _abstract")
    else:
        # Worth saying loudly: with no abstract there is nothing for the
        # verbatim quote check to match against, so every quote in the finished
        # record will have to come from full text.
        print(f"  abstract (none in {record.source} — every source_quote will be full_text)")

    problems = validation_errors(document)
    print()
    if problems:
        print("draft is NOT yet schema-valid; the human must supply:")
        for problem in problems:
            print(f"  {problem}")
    else:
        print("draft is schema-valid (with _-prefixed keys stripped).")
    print("Every extracted value is a placeholder — label them before moving into data/gold/.")


def _truncate(text: str, limit: int = 72) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
