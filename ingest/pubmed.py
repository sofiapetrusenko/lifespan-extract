"""PubMed ingest via NCBI E-utilities (esearch -> efetch).

Two calls, because E-utilities separates them: `esearch` turns a query into
PMIDs, `efetch` turns PMIDs into records. Only `efetch` returns abstracts, and
only in XML — the JSON `esummary` endpoint omits the abstract entirely, so XML
parsing is not avoidable here.

`NCBI_API_KEY` is optional, and that is a considered exception to the "a
missing API key raises" rule rather than an oversight: NCBI's anonymous tier is
a documented, supported mode of the API (3 requests/second instead of 10), not
a degraded stand-in for a credential. The client paces itself to whichever tier
applies — before *every* request, esearch included — so it does not manufacture
the 429s it would then have to retry.

`efetch` answers a `db=pubmed` request with three things, not one:
`<PubmedArticle>` for journal articles, `<PubmedBookArticle>` for
Bookshelf-indexed material (StatPearls, GeneReviews), and `<DeleteCitation>`
listing PMIDs that have been deleted or merged since the search index was
built. `esearch` returns the PMIDs of all three. Book chapters report no
primary experiment and deleted citations have no record at all, so both are
skipped — but counted and announced, and the PMIDs the response accounts for
must be exactly the PMIDs asked for.

XML comes from `defusedxml`. The payload is remote input, and stdlib
ElementTree will happily expand a billion-laughs entity bomb.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from typing import NamedTuple
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as parse_xml

from ingest.errors import ResponseFormatError
from ingest.http import DEFAULT_RETRY_POLICY, RetryPolicy, request_with_retry
from ingest.models import SOURCE_PUBMED, RawPaper

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_URL = f"{EUTILS_BASE}/esearch.fcgi"
EFETCH_URL = f"{EUTILS_BASE}/efetch.fcgi"

API_KEY_ENV = "NCBI_API_KEY"
EMAIL_ENV = "NCBI_EMAIL"

# NCBI asks every client to identify itself; unidentified heavy users get
# blocked at the IP level rather than throttled.
TOOL_NAME = "lifespan-extract"

# efetch accepts large id lists but the response grows linearly; 200 keeps any
# single response small enough to hold in memory and to excerpt in an error.
EFETCH_BATCH = 200

# The three ways `efetch db=pubmed` can account for a requested PMID. Its DTD
# is ((PubmedArticle | PubmedBookArticle)*, DeleteCitation?), so a PMID that has
# been deleted or merged upstream comes back under the third and is not a
# malformed payload.
ARTICLE_ELEMENT = "PubmedArticle"
BOOK_ELEMENT = "PubmedBookArticle"
DELETE_ELEMENT = "DeleteCitation"

# NCBI's published ceilings, with a margin: 3/s anonymous, 10/s with a key.
ANONYMOUS_MIN_INTERVAL = 0.34
KEYED_MIN_INTERVAL = 0.11

TIMEOUT = httpx.Timeout(30.0)

# Digit boundaries, not word boundaries. `\b` cannot match between a digit and a
# letter, so `\b(20\d{2})\b` finds nothing in `2025Nov-Dec` — which is exactly
# the shape PubDate children produce when flattened. `(?<!\d)`/`(?!\d)` still
# reject `12025` and `20255`, so a year is no more inferred than before.
_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")
_PMID_RE = re.compile(r"^\d+$")


class _Batch(NamedTuple):
    """One efetch batch's outcome, partitioned by how each PMID was answered."""

    papers: list[RawPaper]
    book_pmids: list[str]
    deleted_pmids: list[str]


def fetch_abstracts(
    query: str,
    limit: int,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> list[RawPaper]:
    """Return up to `limit` PubMed records matching `query`.

    Guarantees every returned record has a PMID and a non-empty title, and that
    every requested PMID is accounted for by the response — as an article, a
    Bookshelf record, or a deleted citation. A batch that answers for some other
    set of PMIDs raises rather than shrinking the result. A record with no
    abstract is returned with `abstract=None` rather than dropped, and Bookshelf
    and deleted PMIDs are skipped with a count printed to stderr — plenty of
    legitimate PubMed entries have no abstract, and silently discarding any of
    them would make the ingest count disagree with PubMed's own result count
    without saying so.

    Raises `ResponseFormatError` if either endpoint returns something that is
    not the documented payload, and `TransportError` if retries are exhausted.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if not query.strip():
        raise ValueError("query must not be empty")

    api_key = os.environ.get(API_KEY_ENV, "").strip() or None
    pace = _Pacer(KEYED_MIN_INTERVAL if api_key else ANONYMOUS_MIN_INTERVAL, sleep)

    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(httpx.Client(timeout=TIMEOUT))

        pace.wait()
        pmids = _esearch(client, query, limit, api_key, sleep=sleep, policy=policy)
        papers: list[RawPaper] = []
        book_pmids: list[str] = []
        deleted_pmids: list[str] = []
        for batch in _batched(pmids, EFETCH_BATCH):
            pace.wait()
            found = _efetch(client, batch, api_key, sleep=sleep, policy=policy)
            papers.extend(found.papers)
            book_pmids.extend(found.book_pmids)
            deleted_pmids.extend(found.deleted_pmids)

    if book_pmids:
        print(
            f"warning:  skipped {len(book_pmids)} PubMed Bookshelf record(s) "
            "(<PubmedBookArticle>, e.g. StatPearls or GeneReviews chapters); "
            "they report no primary experiment.",
            file=sys.stderr,
        )
    if deleted_pmids:
        print(
            f"warning:  skipped {len(deleted_pmids)} deleted PubMed citation(s) "
            f"(<DeleteCitation>: {', '.join(deleted_pmids)}); the PMID(s) were "
            "deleted or merged upstream after the search index was built, so "
            "PubMed has no record to return for them.",
            file=sys.stderr,
        )
    return papers


class _Pacer:
    """Enforces a minimum gap between consecutive E-utilities requests.

    Guarantees `wait()` sleeps `min_interval` before every request after the
    first, so a run stays under the tier's published ceiling regardless of how
    many batches it makes. Pacing between *batches only* would leave the
    documented one-batch run (`--limit 100`, one esearch plus one efetch)
    entirely unthrottled, which is the tier NCBI actually measures.

    The gap is measured from the previous request's start rather than from a
    clock reading, so it is a lower bound on the real interval: the request
    itself takes time, and overshooting the interval is never a rate violation.
    """

    def __init__(self, min_interval: float, sleep: Callable[[float], None]) -> None:
        self._min_interval = min_interval
        self._sleep = sleep
        self._requested = False

    def wait(self) -> None:
        """Block until the next request may be issued."""
        if self._requested:
            self._sleep(self._min_interval)
        self._requested = True


def _common_params(api_key: str | None) -> dict[str, str]:
    """Identification parameters NCBI expects on every E-utilities call."""
    params = {"db": "pubmed", "tool": TOOL_NAME}
    email = os.environ.get(EMAIL_ENV, "").strip()
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    return params


def _esearch(
    client: httpx.Client,
    query: str,
    limit: int,
    api_key: str | None,
    *,
    sleep: Callable[[float], None],
    policy: RetryPolicy,
) -> list[str]:
    """Return the PMIDs matching `query`, newest first, capped at `limit`."""
    params = _common_params(api_key) | {
        "term": query,
        "retmax": str(limit),
        "retmode": "json",
        "sort": "date",
    }
    response = request_with_retry(
        client, "GET", ESEARCH_URL, params=params, sleep=sleep, policy=policy
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError.from_payload(
            url=ESEARCH_URL,
            reason=f"esearch did not return JSON: {exc}",
            payload=response.text,
            position=getattr(exc, "pos", None),
        ) from exc

    if not isinstance(payload, dict) or "esearchresult" not in payload:
        raise ResponseFormatError.from_payload(
            url=ESEARCH_URL,
            reason="esearch response has no 'esearchresult' key",
            payload=response.text,
        )
    result = payload["esearchresult"]
    if isinstance(result, dict) and "ERROR" in result:
        raise ResponseFormatError.from_payload(
            url=ESEARCH_URL,
            reason=f"esearch rejected the query: {result['ERROR']}",
            payload=response.text,
        )
    idlist = result.get("idlist") if isinstance(result, dict) else None
    if not isinstance(idlist, list):
        raise ResponseFormatError.from_payload(
            url=ESEARCH_URL,
            reason="esearch response has no 'idlist' array",
            payload=response.text,
        )
    for pmid in idlist:
        if not isinstance(pmid, str) or not _PMID_RE.match(pmid):
            raise ResponseFormatError.from_payload(
                url=ESEARCH_URL,
                reason=f"esearch returned a non-numeric PMID: {pmid!r}",
                payload=response.text,
            )
    return idlist[:limit]


def _efetch(
    client: httpx.Client,
    pmids: Sequence[str],
    api_key: str | None,
    *,
    sleep: Callable[[float], None],
    policy: RetryPolicy,
) -> _Batch:
    """Return one batch's journal articles, plus the PMIDs answered another way.

    Guarantees the PMIDs the response accounts for are exactly `pmids`: every
    requested PMID comes back as a `<PubmedArticle>`, a `<PubmedBookArticle>`,
    or a `<DeleteCitation>` entry, and nothing comes back that was not asked
    for. A response that fails either half raises with the offending PMIDs
    named, rather than being handed on as a quietly short or quietly wrong list.
    """
    data = _common_params(api_key) | {
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    # POST rather than GET: a 200-PMID batch exceeds what NCBI guarantees for a
    # query string, and E-utilities documents POST for exactly this case.
    response = request_with_retry(
        client, "POST", EFETCH_URL, data=data, sleep=sleep, policy=policy
    )
    try:
        root = parse_xml(response.content)
    except (ParseError, DefusedXmlException) as exc:
        raise ResponseFormatError.from_payload(
            url=EFETCH_URL,
            reason=f"efetch did not return parseable XML: {exc}",
            payload=response.text,
        ) from exc

    # Bookshelf and deleted records are legitimate parts of a db=pubmed
    # response, not malformed payloads: esearch hands back their PMIDs, so a
    # batch of nothing but book chapters or deleted citations is a correct
    # upstream answer and must not abort a run.
    papers = [
        _to_raw_paper(article, response.text)
        for article in root.findall(ARTICLE_ELEMENT)
    ]
    book_pmids = [
        _text(book.find("BookDocument/PMID")) for book in root.findall(BOOK_ELEMENT)
    ]
    deleted_pmids = [_text(node) for node in root.findall(f"{DELETE_ELEMENT}/PMID")]
    _check_pmids_match(pmids, papers, book_pmids, deleted_pmids, response.text)
    return _Batch(papers, book_pmids, deleted_pmids)


def _check_pmids_match(
    requested: Sequence[str],
    papers: Sequence[RawPaper],
    book_pmids: Sequence[str],
    deleted_pmids: Sequence[str],
    payload: str,
) -> None:
    """Raise unless the response accounts for exactly the requested PMIDs.

    Compares PMID *sets* rather than counts. Counts cannot say which PMID went
    missing, and cannot see a response carrying the right number of records for
    the wrong PMIDs at all.
    """
    wanted = set(requested)
    answered = {paper.pmid for paper in papers if paper.pmid}
    answered.update(pmid for pmid in book_pmids if pmid)
    answered.update(pmid for pmid in deleted_pmids if pmid)

    missing = sorted(wanted - answered)
    unrequested = sorted(answered - wanted)
    if not missing and not unrequested:
        return

    problems = []
    if missing:
        problems.append(f"requested but not returned: {', '.join(missing)}")
    if unrequested:
        problems.append(f"returned but not requested: {', '.join(unrequested)}")
    raise ResponseFormatError.from_payload(
        url=EFETCH_URL,
        reason=(
            f"efetch did not account for the PMIDs in this batch of "
            f"{len(requested)} — {'; '.join(problems)} "
            f"({len(papers)} {ARTICLE_ELEMENT}, {len(book_pmids)} "
            f"{BOOK_ELEMENT}, {len(deleted_pmids)} {DELETE_ELEMENT} returned). "
            "Accepting it would silently shrink the ingest or attribute a "
            "record to a PMID that was never searched for"
        ),
        payload=payload,
    )


def _to_raw_paper(pubmed_article: Element, payload: str) -> RawPaper:
    """Map one `<PubmedArticle>` onto a `RawPaper`."""
    citation = pubmed_article.find("MedlineCitation")
    article = citation.find("Article") if citation is not None else None
    if citation is None or article is None:
        raise ResponseFormatError.from_payload(
            url=EFETCH_URL,
            reason="PubmedArticle has no MedlineCitation/Article",
            payload=payload,
        )

    pmid = _text(citation.find("PMID"))
    if not _PMID_RE.match(pmid or ""):
        raise ResponseFormatError.from_payload(
            url=EFETCH_URL,
            reason=f"PubmedArticle has a missing or non-numeric PMID: {pmid!r}",
            payload=payload,
        )

    title = _text(article.find("ArticleTitle"))
    if not title:
        raise ResponseFormatError.from_payload(
            url=EFETCH_URL,
            reason=f"PubmedArticle {pmid} has an empty ArticleTitle",
            payload=payload,
        )

    return RawPaper.build(
        source=SOURCE_PUBMED,
        source_id=pmid,
        pmid=pmid,
        doi=_doi(pubmed_article, article),
        title=title,
        abstract=_abstract(article),
        year=_year(article),
        first_author=_first_author(article),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


def _text(element: Element | None) -> str:
    """Flatten an element's text, including inline markup such as <i> in titles."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _abstract(article: Element) -> str | None:
    """Join a structured abstract into one string, keeping its section labels.

    Returns None when the record carries no abstract at all, which the schema
    treats as absent rather than empty.
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


def _year(article: Element) -> int | None:
    """Extract the publication year, tolerating PubMed's MedlineDate free text.

    Returns None when no four-digit year is present; the year is never inferred
    from the ingest date.

    Reads `PubDate/Year` directly rather than flattening `PubDate` and searching
    the result. The DTD gives PubDate either `(Year, Month?, Day?)` or a single
    free-text `MedlineDate`, so the year is a named child whenever it exists at
    all — searching for it was always the indirect route. Flattening actively
    broke it: `itertext()` concatenates children with no separator, so
    `<Year>2025</Year><Month>Sep</Month><Day>23</Day>` became `2025Sep23`, and
    28 of the 30 rows ingested before this fix carry `year IS NULL` for that
    reason alone. Only Year-only PubDates ever parsed.

    The other candidate fix was to join `itertext()` with spaces inside `_text`.
    Rejected: `_text` also flattens ArticleTitle and AbstractText, where inline
    markup is *intra-word*. Measured against live PubMed XML, that change turns
    `H2O2` into `H 2 O 2` and `Fe3+/Fe2+` into `Fe 3+ /Fe 2+` — corrupting the
    titles that feed slugs, prompts and `experiment_id` in order to repair a
    field that has its own element.

    `MedlineDate` stays a regex search because it genuinely is free text
    ('2025 Nov-Dec', '1998 Winter'), and it is one text node, so no
    concatenation can run its year into a neighbour.
    """
    pubdate = article.find("Journal/JournalIssue/PubDate")
    if pubdate is None:
        return None

    year = _YEAR_RE.fullmatch(_text(pubdate.find("Year")))
    if year:
        return int(year.group(1))

    medline = _YEAR_RE.search(_text(pubdate.find("MedlineDate")))
    return int(medline.group(1)) if medline else None


def _doi(pubmed_article: Element, article: Element) -> str | None:
    """Return the record's DOI from either place PubMed puts it."""
    for element in article.findall("ELocationID"):
        if element.get("EIdType") == "doi" and _text(element):
            return _text(element)
    for element in pubmed_article.findall("PubmedData/ArticleIdList/ArticleId"):
        if element.get("IdType") == "doi" and _text(element):
            return _text(element)
    return None


def _first_author(article: Element) -> str | None:
    """Return the first author's surname, or None when unavailable.

    Needed by the Phase 2 `experiment_id` convention
    (`<first-author-year>-<organism>-<agent>`); ingest is the only stage where
    the author list is in hand. Collective authors (`<CollectiveName>`) have no
    surname and yield None rather than a guess.
    """
    author = article.find("AuthorList/Author")
    if author is None:
        return None
    return _text(author.find("LastName")) or None


def _batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Yield `items` in slices of at most `size`."""
    for start in range(0, len(items), size):
        yield items[start : start + size]
