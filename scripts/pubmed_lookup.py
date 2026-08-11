#!/usr/bin/env python3
"""Fetch one PubMed record by PMID, and cache it on disk.

Shared by the two gold-set support tools: `scaffold_gold.py` needs the metadata
and the abstract to seed a draft, `check_gold.py` needs the abstract to verify
that every `source_quote` is verbatim.

`fetch_full_text` is the same idea one level up: resolve the PMID to a PMCID
with elink, and if the paper is in the PMC open-access subset, fetch and flatten
its JATS body so `check_gold.py` can verify the quotes marked
`extracted_from: full_text` too. A paper that is not in PMC OA is a *cached
negative*, not an error — most journals are not, and that answer is as much a
result as the text is.

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
Full text lands in the same directory under `<pmid>.fulltext.json`, with its own
version constant — the two parsers change independently, and re-downloading
every abstract because the JATS flattener moved would be wasteful.
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

# Independent of CACHE_VERSION: `_flatten_jats` changing must not invalidate
# every cached abstract, and vice versa.
FULLTEXT_CACHE_VERSION = 1

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ELINK_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"

# elink returns several link sets for a PubMed ID and only this one is the
# article itself. `pubmed_pmc_refs` — the neighbouring set — is the list of PMC
# articles that *cite* this one, and following it would verify quotes against a
# different paper entirely.
PMC_LINKNAME = "pubmed_pmc"

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

    @property
    def source(self) -> str:
        """Matches `paper.source` in the schema.

        A property rather than a field so that `asdict` and the cache round-trip
        are unchanged: an entry written before this existed still loads.
        `BioRxivRecord` carries the same attribute, which is what lets
        `scaffold_gold.build_skeleton` read either without asking which it has.
        """
        return "pubmed"


@dataclass(frozen=True)
class PMCFullText:
    """What PMC has for one PMID. Three states, all of them answers.

    * `pmcid` set and `text` set — the paper is in the open-access subset and
      the flattened body is here.
    * `pmcid` set, `text` None — PMC has the record but efetch returned no
      `<body>`. That is what a non-open-access deposit looks like: front matter
      only. The full text exists, we are simply not allowed to read it.
    * `pmcid` None — PubMed knows of no PMC record at all.

    The last two are deliberately not distinguished by callers: both mean "the
    quote cannot be checked", which is a warning, never a failure. A quote we
    cannot check is not a quote we know to be wrong.
    """

    pmid: str
    pmcid: str | None = None
    text: str | None = None

    @property
    def available(self) -> bool:
        return self.text is not None


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


# --------------------------------------------------------------------------
# PMC full text
# --------------------------------------------------------------------------


def fetch_full_text(
    pmid: str,
    *,
    cache_dir: Path = CACHE_DIR,
    client: Any | None = None,
    refresh: bool = False,
    use_cache: bool = True,
) -> PMCFullText:
    """Resolve `pmid` to PMC and return its open-access full text, if any.

    Two calls, both cached together: elink for the PMCID, then efetch for the
    article. A "not in PMC" answer is cached like any other — otherwise every
    `check_gold.py --all` run would re-ask NCBI the same question about the same
    closed-access papers. That does mean an embargo lifting is not noticed until
    someone passes `--refresh`, which is the right trade for a check that runs
    on every commit.

    **Only a positively established negative is cached.** The two calls below
    raise rather than return on anything that is a fault in the request — an
    elink `<ERROR>`, a response with no LinkSet, an efetch fault — and the raise
    propagates past `write_fulltext_cache`, so nothing is written. A `None` that
    reaches the cache is always elink answering "no PMC article is linked to
    this PMID", never "NCBI did not answer".

    Raises `ValueError` for a malformed PMID, `PubMedLookupError` when the
    service faulted, and `ingest.errors.TransportError` when a request fails and
    retrying does not help. Never raises for a paper that simply is not in PMC.
    """
    pmid = str(pmid).strip()
    if not PMID_RE.match(pmid):
        raise ValueError(f"PMID must be digits only, got {pmid!r}")

    if use_cache and not refresh:
        cached = read_fulltext_cache(pmid, cache_dir=cache_dir)
        if cached is not None:
            return cached

    with ExitStack() as stack:
        if client is None:
            import httpx

            client = stack.enter_context(httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS)))
        pmcid = elink_pmcid(pmid, client=client)
        text = efetch_full_text(pmcid, client=client) if pmcid else None

    record = PMCFullText(pmid=pmid, pmcid=pmcid, text=text)
    if use_cache:
        write_fulltext_cache(record, cache_dir=cache_dir)
    return record


def fulltext_cache_path(pmid: str, *, cache_dir: Path = CACHE_DIR) -> Path:
    """Sits beside the abstract entry. PMIDs are digits, so `19587680.json` and
    `19587680.fulltext.json` cannot collide."""
    return cache_dir / f"{pmid}.fulltext.json"


def read_fulltext_cache(pmid: str, *, cache_dir: Path = CACHE_DIR) -> PMCFullText | None:
    """Return the cached full-text answer for `pmid`, or None for no usable one.

    None means "nothing cached", *not* "not in PMC" — that answer is a cached
    `PMCFullText` with `text=None`. Callers must not collapse the two: one is a
    fact about the paper, the other is a fact about this machine's disk.
    """
    path = fulltext_cache_path(pmid, cache_dir=cache_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: ignoring unreadable cache entry {path}: {exc}", file=sys.stderr)
        return None

    if not isinstance(payload, dict) or payload.get("cache_version") != FULLTEXT_CACHE_VERSION:
        return None
    record = payload.get("record")
    if not isinstance(record, dict):
        print(f"warning: ignoring malformed cache entry {path}", file=sys.stderr)
        return None
    try:
        return PMCFullText(**record)
    except TypeError as exc:
        print(f"warning: ignoring incompatible cache entry {path}: {exc}", file=sys.stderr)
        return None


def write_fulltext_cache(record: PMCFullText, *, cache_dir: Path = CACHE_DIR) -> Path:
    """Persist `record` and return the file it was written to."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = fulltext_cache_path(record.pmid, cache_dir=cache_dir)
    payload = {
        "cache_version": FULLTEXT_CACHE_VERSION,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "record": asdict(record),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def elink_pmcid(pmid: str, *, client: Any | None = None) -> str | None:
    """Return the `PMC…` id linked to `pmid`, or None if there is none."""
    request_with_retry = _load_request_with_retry()
    import httpx

    params = _common_params(db="pmc") | {"dbfrom": "pubmed", "id": pmid, "retmode": "xml"}
    _throttle()
    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS)))
        response = request_with_retry(client, "GET", ELINK_URL, params=params)
    return parse_pmcid(response.content, pmid)


def parse_pmcid(payload: bytes | str, pmid: str) -> str | None:
    """Pull the PMCID out of an elink response, or None when it links to no PMC record.

    Returning None is a **statement about the paper**: elink answered, and its
    answer was that no PMC article is linked. Everything that is instead a
    statement about the *request* — a server fault, a response with no LinkSet
    at all — raises, because the caller caches None forever and a cached
    "not in PMC" that was really "NCBI was down" is indistinguishable from the
    truth on every later run.

    That is not hypothetical: elink spent an outage returning

        <eLinkResult><ERROR>NCBI C++ Exception: ... Read failed: EOF</ERROR></eLinkResult>

    for every PMID, including papers known to be in PMC. The old code found no
    `LinkSet`, fell through, and reported "no PMC record" for the entire gold
    set. See the 2026-08-11 NOTES.md entry.
    """
    root = _parse_xml(payload, f"elink response for PMID {pmid}")

    faults = [_text(node) for node in root.iter("ERROR")]
    if faults:
        raise PubMedLookupError(
            f"elink reported an error for PMID {pmid} rather than a link set: "
            f"{faults[0][:300]}\n"
            "  This is a fault in the request, not an answer about the paper, so it is "
            "not cached as 'no PMC record'."
        )

    link_sets = root.findall("LinkSet")
    if not link_sets:
        raise PubMedLookupError(
            f"elink response for PMID {pmid} carries no LinkSet and no ERROR; refusing "
            "to read it as 'no PMC record'.\n"
            f"  payload excerpt:\n{_excerpt(payload)}"
        )

    for link_set in link_sets:
        requested = _text(link_set.find("IdList/Id"))
        if requested and requested != pmid:
            raise PubMedLookupError(
                f"asked elink about PMID {pmid} and it answered for {requested!r}. "
                "Not treating the response as the requested paper."
            )
        for link_set_db in link_set.findall("LinkSetDb"):
            if _text(link_set_db.find("LinkName")) != PMC_LINKNAME:
                continue
            ids = [_text(link) for link in link_set_db.findall("Link/Id") if _text(link)]
            if not ids:
                continue
            if len(ids) > 1:
                raise PubMedLookupError(
                    f"elink returned {len(ids)} PMC ids for the single PMID {pmid} "
                    f"({', '.join(ids)}); refusing to guess which article was meant."
                )
            return ids[0] if ids[0].upper().startswith("PMC") else f"PMC{ids[0]}"
    return None


def efetch_full_text(pmcid: str, *, client: Any | None = None) -> str | None:
    """Fetch one PMC article and flatten it, or None when it has no OA body."""
    request_with_retry = _load_request_with_retry()
    import httpx

    params = _common_params(db="pmc") | {"id": pmcid, "retmode": "xml"}
    _throttle()
    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS)))
        response = request_with_retry(client, "GET", EFETCH_URL, params=params)
    return parse_full_text(response.content, pmcid)


def parse_full_text(payload: bytes | str, pmcid: str) -> str | None:
    """Flatten a PMC JATS article into text, or None if it carries no `<body>`.

    A missing `<body>` is the shape of a non-open-access deposit: efetch answers
    with front matter and stops. Returning None rather than the abstract alone is
    the point — an abstract dressed up as full text would let a full-text quote
    "pass" against a text it was never taken from.

    The PMC abstract is included when there *is* a body, because a claim marked
    `full_text` may still have been quoted from the paper's own abstract, and the
    PMC wording is the one that will match.

    `<floats-group>` is included too, and must be. Some deposits keep figures and
    tables inline in `<body>` and others park them all in that sibling element,
    and a lifespan record quotes figure captions and survival tables constantly —
    reading only `<body>` silently fails every such quote for half the journals,
    which looks exactly like a mislabelled gold file.

    References, acknowledgements and the rest of `<back>` are left out: nothing
    is quoted from them, and they are the largest source of stray substring
    matches in a paper.
    """
    root = _parse_xml(payload, f"PMC efetch response for {pmcid}")

    # Same split as `parse_pmcid`, and the case matters. Upper-case `<ERROR>` is
    # an E-utilities fault — a statement about the request — and must not be
    # cached as an answer. Lower-case `<error>` inside `<pmc-articleset>` is
    # PMC's way of saying it will not serve this article, which *is* the answer.
    faults = [_text(node) for node in root.iter("ERROR")]
    if faults:
        raise PubMedLookupError(
            f"PMC efetch reported an error for {pmcid} rather than an article: "
            f"{faults[0][:300]}"
        )

    article = root.find("article") if root.tag != "article" else root
    if article is None:
        # `<pmc-articleset><error>...</error></pmc-articleset>` — PMC does not
        # serve this one. Indistinguishable, for our purposes, from no body.
        return None
    body = article.find("body")
    if body is None:
        return None

    blocks: list[str] = []
    for abstract in article.findall("front/article-meta/abstract"):
        blocks.extend(_flatten_jats(abstract))
    blocks.extend(_flatten_jats(body))
    for floats in article.findall("floats-group"):
        blocks.extend(_flatten_jats(floats))
    return "\n\n".join(blocks) or None


# JATS elements whose text is one contiguous run of prose. Flattening stops at
# these rather than descending, so inline markup — `<italic>daf-2</italic>`,
# `H<sub>2</sub>O`, a `<xref>` citation marker — is concatenated with no
# separator, exactly as it reads on the page. Insert a space there and every
# quote containing a gene name in italics stops matching.
_JATS_BLOCK_TAGS = frozenset({"p", "title", "label", "td", "th"})


def _flatten_jats(element) -> list[str]:
    """Return one string per text-bearing block beneath `element`.

    Blocks are joined by the caller with blank lines. That is deliberately more
    whitespace than the published text has: `check_gold.py` collapses runs of
    whitespace on both sides before comparing, so extra separation is free,
    whereas two paragraphs run together would invent a sentence boundary that
    exists in no version of the paper.
    """
    if element.tag in _JATS_BLOCK_TAGS or len(element) == 0:
        text = "".join(element.itertext())
        return [text] if text.strip() else []
    blocks: list[str] = []
    for child in element:
        blocks.extend(_flatten_jats(child))
    return blocks


def _parse_xml(payload: bytes | str, what: str):
    """Parse remote XML with `defusedxml`, or raise `PubMedLookupError`."""
    from xml.etree.ElementTree import ParseError

    from defusedxml.common import DefusedXmlException
    from defusedxml.ElementTree import fromstring as parse_xml

    try:
        return parse_xml(payload)
    except (ParseError, DefusedXmlException) as exc:
        raise PubMedLookupError(
            f"{what} was not parseable XML: {exc}\n  payload excerpt:\n{_excerpt(payload)}"
        ) from exc


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


def _common_params(db: str = "pubmed") -> dict[str, str]:
    """Identification parameters NCBI expects on every E-utilities call.

    `db` is a parameter because the full-text path talks to `pmc`; everything
    else about identification and rate limiting is the same endpoint family.

    The API key is optional here for the same reason it is in `ingest/pubmed.py`:
    NCBI's anonymous tier is a documented, supported mode of the API, not a
    degraded stand-in for a credential.
    """
    params = {"db": db, "tool": TOOL_NAME}
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
