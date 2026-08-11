"""bioRxiv ingest via the public details API.

The awkward fact this module exists around: **bioRxiv's API has no keyword
search.** `api.biorxiv.org/details/biorxiv/<from>/<to>/<cursor>` returns every
preprint posted in a date interval, 100 at a time, and nothing else. So
`fetch_abstracts` keeps the contract PubMed's client has — `(query, limit)` in,
`list[RawPaper]` out — by paging a date window and matching the query against
each title and abstract locally.

Three consequences, all deliberate and all visible to the caller:

* The search is bounded by a **date window**, not by relevance. `window_days`
  defaults to 30 (roughly 3000 preprints, 30 requests). Widen it for a rare
  term; a wider window costs one request per extra 100 preprints.
* The query language is **plain keywords only**. PubMed's boolean and
  field-tag syntax cannot be honoured here, and quietly treating `AND` as a
  required word would return zero results for a query that looks valid, so
  such a query raises instead.
* Hitting the page cap before the window is exhausted raises `ScanLimitError`
  rather than returning a partial scan, which would be indistinguishable from
  a thorough scan that found little.

Deduplication note: a preprint that has since been published reports the
journal DOI in its `published` field, and *that* is what becomes
`RawPaper.doi`. It is the only value that can ever match the PubMed record for
the same work — the `10.1101/...` preprint DOI never will. Because the whole
dedup rule hinges on that one value, its shape is checked rather than trusted;
see `_published_doi`. Within one run this collapses the pair. *Across* runs —
preprint ingested first, publication appearing later — it does not; that gap is
recorded under "Known limitations" in NOTES.md and pinned by a test.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone

import httpx

from ingest.errors import ResponseFormatError, ScanLimitError
from ingest.http import DEFAULT_RETRY_POLICY, RetryPolicy, request_with_retry
from ingest.models import SOURCE_BIORXIV, RawPaper, normalise_doi

API_BASE = "https://api.biorxiv.org/details/biorxiv"

# Fixed by the API: it returns 100 records per cursor position.
PAGE_SIZE = 100

DEFAULT_WINDOW_DAYS = 30

# 200 pages is 20 000 preprints — well beyond any window this CLI sets by
# default, so tripping it means the window was widened past what one run can
# scan, not that bioRxiv ran out of records.
MAX_PAGES = 200

TIMEOUT = httpx.Timeout(30.0)

# Standalone uppercase operators and field tags are PubMed syntax. They have no
# meaning here and would silently over-constrain the local match.
_UNSUPPORTED_SYNTAX = re.compile(r"(\b(AND|OR|NOT)\b)|[\[\]\"]")
_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)
_YEAR_RE = re.compile(r"^(\d{4})")

# A bare DOI: registrant prefix, slash, suffix. Deliberately not a URL form and
# not a PMID — see `_published_doi` for why this one field is validated.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

_STATUS_EXHAUSTED = "no posts found"


def fetch_detail(
    doi: str,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> list[dict]:
    """Return every version entry bioRxiv holds for one preprint DOI.

    The other direction of the same API: `fetch_abstracts` scans a date window
    because bioRxiv has no keyword search, while this resolves a DOI that is
    already known. `scripts/scaffold_gold.py` needs the second — the human has
    chosen the paper and wants its metadata — and putting it here rather than in
    the scripts layer keeps one module responsible for what the bioRxiv API
    returns and what its status strings mean.

    Entries come back oldest version first, as the API orders them. An empty
    list means bioRxiv has no such preprint: the API answers a DOI it does not
    know with `status: no posts found` and an empty collection, which is an
    answer rather than a fault, so it is not raised.
    """
    url = f"{API_BASE}/{doi}"
    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(httpx.Client(timeout=TIMEOUT))
        entries, exhausted = _fetch_page(client, url, sleep=sleep, policy=policy)
    return [] if exhausted else entries


def fetch_abstracts(
    query: str,
    limit: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: date | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> list[RawPaper]:
    """Return up to `limit` bioRxiv preprints from the last `window_days` days
    whose title or abstract contains every term in `query`.

    Guarantees at most one record per preprint DOI, and that the one kept is
    the highest posted version — bioRxiv returns each revision as its own entry
    and the latest abstract is the current one.

    Raises `ValueError` for PubMed-style boolean queries, `ResponseFormatError`
    for a payload that is not the documented shape, and `ScanLimitError` if the
    window is too wide to scan in one run.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1, got {window_days}")
    terms = _query_terms(query)

    # UTC rather than local time: bioRxiv's date filter is server-side, so a
    # local "today" would silently shift the window by a day for most of the
    # world and make the same command scan different preprints.
    end = today or datetime.now(timezone.utc).date()
    start = end - timedelta(days=window_days)
    matched: dict[str, tuple[int, RawPaper]] = {}

    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(httpx.Client(timeout=TIMEOUT))

        cursor = 0
        for _page in range(MAX_PAGES):
            url = f"{API_BASE}/{start.isoformat()}/{end.isoformat()}/{cursor}"
            entries, exhausted = _fetch_page(client, url, sleep=sleep, policy=policy)
            for entry in entries:
                _collect(entry, terms, matched, url)
            if exhausted or not entries:
                return _ordered(matched, limit)
            if len(matched) >= limit:
                return _ordered(matched, limit)
            cursor += PAGE_SIZE

    raise ScanLimitError(
        f"scanned {MAX_PAGES} pages ({MAX_PAGES * PAGE_SIZE} preprints) of "
        f"{start.isoformat()}..{end.isoformat()} without reaching the end of "
        "the window. Narrow --biorxiv-window-days; bioRxiv has no keyword "
        "search, so every preprint in the window is fetched and filtered here."
    )


def _query_terms(query: str) -> list[str]:
    """Split a plain-keyword query into lowercase terms, rejecting PubMed syntax."""
    if _UNSUPPORTED_SYNTAX.search(query):
        raise ValueError(
            f"bioRxiv cannot run the query {query!r}: its API has no query "
            "language, so this client matches plain keywords locally. Boolean "
            "operators (AND/OR/NOT), field tags ([tiab]) and quoted phrases "
            "are not supported here. Use plain keywords, e.g. "
            '--query "autophagy lifespan".'
        )
    terms = [term.lower() for term in _TERM_RE.findall(query)]
    if not terms:
        raise ValueError(f"query {query!r} contains no searchable terms")
    return terms


def _fetch_page(
    client: httpx.Client,
    url: str,
    *,
    sleep: Callable[[float], None],
    policy: RetryPolicy,
) -> tuple[list[dict], bool]:
    """Return one page's entries and whether the window is exhausted."""
    response = request_with_retry(client, "GET", url, sleep=sleep, policy=policy)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError.from_payload(
            url=url,
            reason=f"bioRxiv did not return JSON: {exc}",
            payload=response.text,
            position=getattr(exc, "pos", None),
        ) from exc

    if not isinstance(payload, dict):
        raise ResponseFormatError.from_payload(
            url=url,
            reason=f"bioRxiv returned {type(payload).__name__}, expected an object",
            payload=response.text,
        )

    status = _status(payload)
    if status == _STATUS_EXHAUSTED:
        return [], True
    if status != "ok":
        raise ResponseFormatError.from_payload(
            url=url,
            reason=f"bioRxiv reported status {status!r}",
            payload=response.text,
        )

    collection = payload.get("collection")
    if not isinstance(collection, list):
        raise ResponseFormatError.from_payload(
            url=url,
            reason="bioRxiv response has no 'collection' array",
            payload=response.text,
        )
    for entry in collection:
        if not isinstance(entry, dict):
            raise ResponseFormatError.from_payload(
                url=url,
                reason=f"bioRxiv collection holds a {type(entry).__name__}, "
                "expected objects",
                payload=response.text,
            )
    return collection, False


def _status(payload: dict) -> str:
    """Return the API's own status string, lowercased."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "ok" if "collection" in payload else "missing"
    first = messages[0]
    if not isinstance(first, dict):
        return "missing"
    return str(first.get("status", "missing")).lower()


def _collect(
    entry: dict,
    terms: list[str],
    matched: dict[str, tuple[int, RawPaper]],
    url: str,
) -> None:
    """Add `entry` to `matched` if it matches, keeping the highest version."""
    preprint_doi = _doi_field(entry, "doi", url)
    if preprint_doi is None:
        raise ResponseFormatError.from_payload(
            url=url,
            reason="bioRxiv entry has no 'doi'; it is the record's only identity",
            payload=repr(entry),
        )
    title = str(entry.get("title") or "").strip()
    if not title:
        raise ResponseFormatError.from_payload(
            url=url,
            reason=f"bioRxiv entry {preprint_doi} has an empty title",
            payload=repr(entry),
        )
    abstract = str(entry.get("abstract") or "").strip() or None
    if not _matches(terms, title, abstract):
        return

    version = _version(entry)
    incumbent = matched.get(preprint_doi)
    if incumbent is not None and incumbent[0] >= version:
        return

    matched[preprint_doi] = (
        version,
        RawPaper.build(
            source=SOURCE_BIORXIV,
            source_id=preprint_doi,
            # The published DOI, when bioRxiv knows one, is what makes this
            # record collide with its PubMed twin under the shared dedup key.
            doi=_published_doi(entry, preprint_doi, url) or preprint_doi,
            title=title,
            abstract=abstract,
            year=_year(entry),
            first_author=_first_author(entry),
            url=f"https://doi.org/{preprint_doi}",
        ),
    )


def _doi_field(entry: dict, field: str, url: str) -> str | None:
    """Return `entry[field]` normalised, refusing a value that is not a string.

    Guarantees the result is either None — the field is absent, null, blank or
    bioRxiv's "NA" — or a normalised DOI string. Absence is legitimate and
    stays None; a wrong *type* is not, and reaches `normalise_doi` as a bare
    `AttributeError` unless it is caught here. These are the two fields the
    dedup key is built from, so a malformed one is reported with a payload
    window like every other shape violation in this module.
    """
    value = entry.get(field)
    if value is not None and not isinstance(value, str):
        raise ResponseFormatError.from_payload(
            url=url,
            reason=(
                f"bioRxiv entry holds a {type(value).__name__} in {field!r}, "
                "expected a string DOI"
            ),
            payload=repr(entry),
        )
    return normalise_doi(value)


def _published_doi(entry: dict, preprint_doi: str, url: str) -> str | None:
    """Return the validated journal DOI from `published`, or None if unpublished.

    Guarantees the result is either None or a bare DOI. This is the one field
    the entire preprint/publication dedup rule rests on: whatever it holds
    becomes `RawPaper.doi` and the row's primary key, so a URL form, a PMID or
    a truncated string would key the record under something its PubMed twin can
    never equal. Dedup would then fail in a way indistinguishable from a paper
    that genuinely has no twin, which is why a bad shape raises here instead of
    falling back to the preprint DOI.
    """
    published = _doi_field(entry, "published", url)
    if published is None:
        return None
    if not _DOI_RE.match(published):
        raise ResponseFormatError.from_payload(
            url=url,
            reason=(
                f"bioRxiv entry {preprint_doi} reports published={published!r}, "
                f"which is not a bare DOI (expected {_DOI_RE.pattern}). It would "
                "become this record's canonical DOI and primary key and could "
                "never match its published version in PubMed"
            ),
            payload=repr(entry),
        )
    return published


def _matches(terms: list[str], title: str, abstract: str | None) -> bool:
    """True when every term appears as a whole word in the title or abstract.

    Whole-word rather than substring: a substring match would let "rat" hit
    "strategy", and the point of the local filter is to stand in for a real
    search endpoint, not to approximate one loosely.
    """
    haystack = set(_TERM_RE.findall(f"{title} {abstract or ''}".lower()))
    return all(term in haystack for term in terms)


def _version(entry: dict) -> int:
    """Return the entry's version number, defaulting to 1 for absent or junk.

    A missing version is not fatal: it only affects which of several revisions
    of the *same* preprint wins, and every revision carries the same identity.
    """
    try:
        return int(str(entry.get("version", "1")).strip())
    except ValueError:
        return 1


def _year(entry: dict) -> int | None:
    """Return the posting year from the API's YYYY-MM-DD date, else None."""
    match = _YEAR_RE.match(str(entry.get("date") or ""))
    return int(match.group(1)) if match else None


def _first_author(entry: dict) -> str | None:
    """Return the first author's surname from bioRxiv's "Last, F.; ..." list.

    Normalised to a bare surname so it matches the shape PubMed's client
    produces; the Phase 2 `experiment_id` convention needs one or the other,
    not two formats.
    """
    authors = str(entry.get("authors") or "").strip()
    if not authors:
        return None
    surname = authors.split(";", 1)[0].split(",", 1)[0].strip()
    return surname or None


def _ordered(matched: dict[str, tuple[int, RawPaper]], limit: int) -> list[RawPaper]:
    """Return the matched records in discovery order, capped at `limit`."""
    return [paper for _, paper in matched.values()][:limit]
