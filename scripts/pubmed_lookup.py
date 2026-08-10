#!/usr/bin/env python3
"""Fetch one PubMed record by PMID, and cache it on disk.

Shared by the two gold-set support tools: `scaffold_gold.py` needs the metadata
and the abstract to seed a draft, `check_gold.py` needs the abstract to verify
that every `source_quote` is verbatim.

Why this is not `ingest/pubmed.py`:

* `ingest.fetch_abstracts` is query-driven (esearch -> efetch) and projects onto
  `RawPaper`, a SQLModel table row. These tools resolve one *known* PMID, need
  one field `RawPaper` does not carry (journal), and must run with no database.
* The part actually worth sharing is the retry/backoff policy, and that is
  imported from `ingest.http` rather than reimplemented. The two E-utilities
  constants below mirror `ingest/pubmed.py`; copying two lines is cheaper than
  coupling a labelling script to the SQLModel import chain.

`httpx` and `defusedxml` are imported inside the functions that need them, not
at module scope. The repo has no `requirements.txt` yet (Phase 1 is still in
flight), so CI installs only `requirements-dev.txt` — and the offline half of
`check_gold.py` (schema validation, cross-file consistency) is worth keeping
runnable, and testable, without the ingest runtime dependencies present.

The cache is a directory of JSON files under `data/.abstract_cache/`, so that a
`check_gold.py --all` run over a growing gold set makes at most one network
call per paper, ever. Entries carry a `cache_version`: bumping the constant
invalidates every entry rather than silently mixing outputs of two parsers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / ".abstract_cache"

# Bump when the parser below starts producing a different projection of the
# same XML. Old entries are then ignored instead of being trusted.
CACHE_VERSION = 1

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

API_KEY_ENV = "NCBI_API_KEY"
EMAIL_ENV = "NCBI_EMAIL"

# NCBI blocks unidentified heavy users at the IP level rather than throttling.
TOOL_NAME = "lifespan-extract"

# NCBI's published ceilings, with a margin: 3/s anonymous, 10/s with a key.
ANONYMOUS_MIN_INTERVAL = 0.34
KEYED_MIN_INTERVAL = 0.11

TIMEOUT_SECONDS = 30.0

PMID_RE = re.compile(r"^\d+$")
_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")

# Monotonic timestamp of the last efetch call, so that fetching abstracts for a
# whole gold set stays inside NCBI's rate limit without the caller tracking it.
_last_request_at: float | None = None


class PubMedLookupError(Exception):
    """A PMID could not be resolved into a usable record.

    Distinct from a transport failure: the request succeeded, the response was
    simply not a record this tool can use (unknown PMID, no title).
    """


@dataclass(frozen=True)
class PubMedRecord:
    """The projection of a PubMed record these tools need.

    `journal`, `year`, `doi` and `abstract` are all optional because PubMed
    genuinely omits each of them for some records. They are None rather than a
    placeholder string so that a caller writing gold metadata can tell "PubMed
    does not know" apart from "PubMed says the empty string".
    """

    pmid: str
    title: str
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    abstract: str | None = None


def fetch_record(
    pmid: str,
    *,
    cache_dir: Path = CACHE_DIR,
    client: Any | None = None,
    refresh: bool = False,
    use_cache: bool = True,
) -> PubMedRecord:
    """Return the record for `pmid`, from cache when possible.

    Raises `ValueError` for a malformed PMID, `PubMedLookupError` when the
    response carries no usable record, and `ingest.errors.TransportError` when
    the request fails and retrying does not help.
    """
    pmid = str(pmid).strip()
    if not PMID_RE.match(pmid):
        raise ValueError(f"PMID must be digits only, got {pmid!r}")

    if use_cache and not refresh:
        cached = read_cache(pmid, cache_dir=cache_dir)
        if cached is not None:
            return cached

    record = efetch_one(pmid, client=client)
    if use_cache:
        write_cache(record, cache_dir=cache_dir)
    return record


def cache_path(pmid: str, *, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{pmid}.json"


def read_cache(pmid: str, *, cache_dir: Path = CACHE_DIR) -> PubMedRecord | None:
    """Return the cached record for `pmid`, or None if there is no usable one.

    A corrupt or stale-version entry is reported on stderr and treated as a
    miss. The cache is derived data — refetching is always correct — but it is
    never discarded silently, because a cache that keeps going corrupt is a bug
    worth seeing.
    """
    path = cache_path(pmid, cache_dir=cache_dir)
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
        return PubMedRecord(**record)
    except TypeError as exc:
        print(f"warning: ignoring incompatible cache entry {path}: {exc}", file=sys.stderr)
        return None


def write_cache(record: PubMedRecord, *, cache_dir: Path = CACHE_DIR) -> Path:
    """Persist `record` and return the file it was written to."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(record.pmid, cache_dir=cache_dir)
    payload = {
        "cache_version": CACHE_VERSION,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "record": asdict(record),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def efetch_one(pmid: str, *, client: Any | None = None) -> PubMedRecord:
    """Fetch one record from E-utilities, bypassing the cache."""
    request_with_retry = _load_request_with_retry()
    import httpx

    params = _common_params() | {
        "id": pmid,
        "retmode": "xml",
        "rettype": "abstract",
    }
    _throttle()
    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS)))
        response = request_with_retry(client, "GET", EFETCH_URL, params=params)
    return parse_record(response.content, pmid)


def parse_record(payload: bytes | str, pmid: str) -> PubMedRecord:
    """Map an efetch XML payload onto a `PubMedRecord`.

    XML is parsed with `defusedxml` for the same reason `ingest/pubmed.py` does:
    the payload is remote input and stdlib ElementTree will cheerfully expand an
    entity bomb.
    """
    from xml.etree.ElementTree import ParseError

    from defusedxml.common import DefusedXmlException
    from defusedxml.ElementTree import fromstring as parse_xml

    try:
        root = parse_xml(payload)
    except (ParseError, DefusedXmlException) as exc:
        raise PubMedLookupError(
            f"efetch did not return parseable XML for PMID {pmid}: {exc}\n"
            f"  payload excerpt:\n{_excerpt(payload)}"
        ) from exc

    articles = root.findall("PubmedArticle")
    if not articles:
        raise PubMedLookupError(
            f"efetch returned no PubmedArticle for PMID {pmid}. "
            "Either the PMID does not exist or PubMed has withdrawn the record.\n"
            f"  payload excerpt:\n{_excerpt(payload)}"
        )
    if len(articles) > 1:
        raise PubMedLookupError(
            f"efetch returned {len(articles)} articles for the single PMID {pmid}; "
            "refusing to guess which one was meant."
        )

    article_root = articles[0]
    citation = article_root.find("MedlineCitation")
    article = citation.find("Article") if citation is not None else None
    if citation is None or article is None:
        raise PubMedLookupError(
            f"PubmedArticle for PMID {pmid} has no MedlineCitation/Article"
        )

    returned_pmid = _text(citation.find("PMID"))
    if returned_pmid != pmid:
        raise PubMedLookupError(
            f"asked efetch for PMID {pmid} and it returned {returned_pmid!r}. "
            "Not treating the response as the requested paper."
        )

    title = _text(article.find("ArticleTitle"))
    if not title:
        raise PubMedLookupError(f"PubMed record {pmid} has an empty ArticleTitle")

    return PubMedRecord(
        pmid=pmid,
        title=title,
        journal=_journal(article),
        year=_year(article),
        doi=_doi(article_root, article),
        abstract=_abstract(article),
    )


def _load_request_with_retry():
    """Import `ingest.http.request_with_retry` lazily.

    Lazy for two reasons. The repo root has to go on `sys.path` first — `ingest`
    is a top-level package, not an installed distribution, and a script run as
    `python scripts/check_gold.py` gets `scripts/` on the path, not the root.
    Doing that at module scope would mean an import after code, which `ruff`
    correctly flags (E402). And `ingest.http` pulls in httpx, which the offline
    checks deliberately do not require.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from ingest.http import request_with_retry

    return request_with_retry


def _common_params() -> dict[str, str]:
    """Identification parameters NCBI expects on every E-utilities call.

    The API key is optional here for the same reason it is in `ingest/pubmed.py`:
    NCBI's anonymous tier is a documented, supported mode of the API, not a
    degraded stand-in for a credential.
    """
    params = {"db": "pubmed", "tool": TOOL_NAME}
    email = os.environ.get(EMAIL_ENV, "").strip()
    if email:
        params["email"] = email
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if api_key:
        params["api_key"] = api_key
    return params


def _throttle(sleep=time.sleep, monotonic=time.monotonic) -> None:
    """Wait long enough that consecutive calls stay inside NCBI's rate limit.

    Self-throttling rather than retrying: manufacturing 429s and then backing
    off from them is slower and ruder than simply not sending too fast.
    """
    global _last_request_at
    interval = KEYED_MIN_INTERVAL if os.environ.get(API_KEY_ENV, "").strip() else ANONYMOUS_MIN_INTERVAL
    now = monotonic()
    if _last_request_at is not None:
        waited = now - _last_request_at
        if waited < interval:
            sleep(interval - waited)
    _last_request_at = monotonic()


def _text(element) -> str:
    """Flatten an element's text, including inline markup such as <i> in titles."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _journal(article) -> str | None:
    """Return the full journal title, falling back to the MEDLINE abbreviation."""
    for path in ("Journal/Title", "Journal/ISOAbbreviation"):
        title = _text(article.find(path))
        if title:
            return title
    return None


def _year(article) -> int | None:
    """Extract the publication year, tolerating PubMed's MedlineDate free text.

    None when no four-digit year is present. Never inferred from today's date.

    Reads `<Year>` directly rather than regexing the flattened `<PubDate>`.
    Flattening concatenates the child elements, so an unindented
    `<Year>2009</Year><Month>Jul</Month>` becomes `2009Jul`, where a
    `\\b`-anchored year pattern finds nothing — the match would then depend on
    whether NCBI happened to pretty-print that response. The regex is kept for
    `<MedlineDate>`, which really is free text ("1998 Nov-Dec").
    """
    pubdate = article.find("Journal/JournalIssue/PubDate")
    if pubdate is None:
        return None
    year = _text(pubdate.find("Year"))
    if year.isdigit():
        return int(year)
    match = _YEAR_RE.search(_text(pubdate.find("MedlineDate")) or _text(pubdate))
    return int(match.group(1)) if match else None


def _doi(article_root, article) -> str | None:
    """Return the record's DOI from either place PubMed puts it, lowercased.

    Lowercased because the schema calls for a lowercase canonical DOI and DOIs
    are case-insensitive.
    """
    for element in article.findall("ELocationID"):
        if element.get("EIdType") == "doi" and _text(element):
            return _text(element).lower()
    for element in article_root.findall("PubmedData/ArticleIdList/ArticleId"):
        if element.get("IdType") == "doi" and _text(element):
            return _text(element).lower()
    return None


def _abstract(article) -> str | None:
    """Join a structured abstract into one string, keeping its section labels.

    Matches `ingest/pubmed.py` exactly, and that identity matters here: the
    verbatim `source_quote` check in `check_gold.py` compares against this
    string, so any difference in how sections are joined would make quotes pass
    one tool and fail the other.
    """
    node = article.find("Abstract")
    if node is None:
        return None
    sections: list[str] = []
    for section in node.findall("AbstractText"):
        body = _text(section)
        if not body:
            continue
        label = section.get("Label")
        sections.append(f"{label}: {body}" if label else body)
    return "\n\n".join(sections) or None


def _excerpt(payload: bytes | str, limit: int = 400) -> str:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    body = text[:limit]
    return f"    {body}{'...' if len(text) > limit else ''}"
