#!/usr/bin/env python3
"""Resolve one bioRxiv preprint by DOI, and cache it on disk.

The bioRxiv counterpart of `pubmed_lookup.py`, and deliberately the same shape:
`scaffold_gold.py` needs metadata and an abstract to seed a draft,
`check_gold.py` needs the abstract to verify that every `source_quote` on a
preprint is verbatim. Both tools work in DOIs for a preprint the way they work
in PMIDs for a published paper.

**This is not a second bioRxiv client.** The HTTP call, the retry policy, the
API's status vocabulary and its JSON shape all live in `ingest/biorxiv.py`; this
module calls `fetch_detail` and does two things that module has no business
doing — projecting an API entry onto the fields the gold-set tools need, and
caching it beside the PubMed abstracts so a `check_gold.py --all` run over a
growing gold set makes at most one network call per paper, ever.

The import is lazy for the reason `pubmed_lookup` gives: `ingest.biorxiv` pulls
in `ingest.models`, and therefore SQLModel, and the offline half of
`check_gold.py` is worth keeping runnable without the ingest runtime
dependencies present.

**Versions.** bioRxiv returns one entry per revision, oldest first, and a
preprint under active revision has several. Content — title and abstract —
comes from the *latest* version, because that is what the DOI serves today and
what a labeller reading the paper will quote from. `year` comes from the
*first* posting, because that is the year the work appeared and what a citation
means by it; a v2 posted eighteen months later does not make it a 2026 paper.
Both dates are carried so the report can show them and a human can disagree.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / ".abstract_cache"

# Bump when the projection below changes. Independent of the PubMed cache
# version: the two parsers move separately.
CACHE_VERSION = 1

# The schema's own DOI pattern, so a value this module accepts is a value the
# schema will accept in `paper.doi`.
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# bioRxiv's "this preprint has since been published" field uses this literal
# when there is no journal version. Treated as absence, not as a DOI.
_NO_PUBLISHED_DOI = {"", "na", "n/a", "none"}

_YEAR_RE = re.compile(r"^(\d{4})")


class BioRxivLookupError(Exception):
    """A DOI could not be resolved into a usable preprint record.

    Distinct from a transport failure: the request succeeded, the response
    simply was not a preprint this tool can use (unknown DOI, no title).
    """


@dataclass(frozen=True)
class BioRxivRecord:
    """The projection of a bioRxiv preprint these tools need.

    Attribute names deliberately match `PubMedRecord` where the concepts match,
    so `scaffold_gold.build_skeleton` can read either without asking which it
    has. `pmid` is always None — a preprint that has been indexed by PubMed is a
    published paper and belongs on the PubMed path — and `journal` reports the
    server, which is what the `_journal` scaffolding key means for a preprint.
    """

    doi: str
    title: str
    posted: str | None = None          # ISO date of the first version
    latest_posted: str | None = None   # ISO date of the version quoted here
    version: int | None = None
    year: int | None = None
    abstract: str | None = None
    category: str | None = None
    server: str | None = None
    published_doi: str | None = None

    @property
    def source(self) -> str:
        return "biorxiv"

    @property
    def pmid(self) -> None:
        """Always None. See the class docstring."""
        return None

    @property
    def journal(self) -> str | None:
        return self.server

    @property
    def is_published(self) -> bool:
        """True when bioRxiv reports a journal DOI for this preprint.

        Worth checking before a preprint goes into the gold set: a published
        preprint is the same work reachable through PubMed, with a PMID and
        often PMC full text, and the published version is the better record.
        """
        return self.published_doi is not None


def fetch_record(
    doi: str,
    *,
    cache_dir: Path = CACHE_DIR,
    client: Any | None = None,
    refresh: bool = False,
    use_cache: bool = True,
) -> BioRxivRecord:
    """Return the record for `doi`, from cache when possible.

    Raises `ValueError` for a malformed DOI and `BioRxivLookupError` when
    bioRxiv has no such preprint or the entry carries no title.
    """
    doi = normalise_doi(doi)

    if use_cache and not refresh:
        cached = read_cache(doi, cache_dir=cache_dir)
        if cached is not None:
            return cached

    from ingest.biorxiv import fetch_detail

    entries = fetch_detail(doi, client=client)
    record = parse_detail(entries, doi)
    if use_cache:
        write_cache(record, cache_dir=cache_dir)
    return record


def normalise_doi(doi: str) -> str:
    """Lowercase, strip a URL or `doi:` prefix, and validate the shape."""
    text = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        text = text.removeprefix(prefix)
    text = text.strip()
    if not DOI_RE.match(text):
        raise ValueError(f"not a DOI: {doi!r} (expected 10.NNNN/suffix)")
    return text


def parse_detail(entries: list[dict], doi: str) -> BioRxivRecord:
    """Project bioRxiv's version entries onto one record.

    Raises rather than guessing when the DOI is unknown or the newest entry has
    no title: a scaffolded draft with an invented title is worse than no draft.
    """
    if not entries:
        raise BioRxivLookupError(
            f"bioRxiv has no preprint with DOI {doi}. Check the DOI, and note that "
            "this endpoint covers bioRxiv only — a medRxiv preprint will not be found here."
        )

    latest = entries[-1]
    returned = str(latest.get("doi", "")).strip().lower()
    if returned and returned != doi:
        raise BioRxivLookupError(
            f"asked bioRxiv for DOI {doi} and it answered for {returned!r}. "
            "Not treating the response as the requested preprint."
        )

    title = str(latest.get("title") or "").strip()
    if not title:
        raise BioRxivLookupError(f"bioRxiv entry for {doi} has an empty title")

    first_posted = str(entries[0].get("date") or "").strip() or None
    latest_posted = str(latest.get("date") or "").strip() or None
    abstract = str(latest.get("abstract") or "").strip() or None

    return BioRxivRecord(
        doi=doi,
        title=title,
        posted=first_posted,
        latest_posted=latest_posted,
        version=_int(latest.get("version")),
        year=_year(first_posted),
        abstract=abstract,
        category=str(latest.get("category") or "").strip() or None,
        server=str(latest.get("server") or "").strip() or None,
        published_doi=_published_doi(latest),
    )


def _published_doi(entry: dict) -> str | None:
    """The journal DOI when this preprint has been published, else None."""
    value = str(entry.get("published") or "").strip()
    if value.lower() in _NO_PUBLISHED_DOI:
        return None
    return value.lower() if DOI_RE.match(value.lower()) else None


def _year(posted: str | None) -> int | None:
    """Year of an ISO posting date. None rather than a guess when absent."""
    if not posted:
        return None
    match = _YEAR_RE.match(posted)
    return int(match.group(1)) if match else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


def cache_path(doi: str, *, cache_dir: Path = CACHE_DIR) -> Path:
    """One file per DOI, beside the PubMed entries.

    A DOI contains `/` and cannot be a filename, so slashes become underscores.
    The `biorxiv-` prefix keeps the namespace visibly separate from the
    PMID-keyed files, which are bare digits and could never collide anyway.
    """
    return cache_dir / f"biorxiv-{doi.replace('/', '_')}.json"


def read_cache(doi: str, *, cache_dir: Path = CACHE_DIR) -> BioRxivRecord | None:
    """Return the cached record for `doi`, or None if there is no usable one.

    A corrupt or stale-version entry is reported on stderr and treated as a
    miss, matching `pubmed_lookup.read_cache`: the cache is derived data, so
    refetching is always correct, but it is never discarded silently.
    """
    path = cache_path(doi, cache_dir=cache_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: ignoring unreadable cache entry {path}: {exc}", file=sys.stderr)
        return None

    if not isinstance(payload, dict) or payload.get("cache_version") != CACHE_VERSION:
        return None
    record = payload.get("record")
    if not isinstance(record, dict):
        print(f"warning: ignoring malformed cache entry {path}", file=sys.stderr)
        return None
    try:
        return BioRxivRecord(**record)
    except TypeError as exc:
        print(f"warning: ignoring incompatible cache entry {path}: {exc}", file=sys.stderr)
        return None


def write_cache(record: BioRxivRecord, *, cache_dir: Path = CACHE_DIR) -> Path:
    """Persist `record` and return the file it was written to."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(record.doi, cache_dir=cache_dir)
    payload = {
        "cache_version": CACHE_VERSION,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "record": asdict(record),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path
