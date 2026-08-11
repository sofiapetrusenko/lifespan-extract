"""bioRxiv client: date-window paging, local keyword matching, loud failures."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from ingest.biorxiv import MAX_PAGES, fetch_abstracts
from ingest.errors import ResponseFormatError, ScanLimitError, TransportError
from tests.conftest import FAST_POLICY, make_client

TODAY = date(2026, 8, 10)


def entry(**overrides) -> dict:
    base = {
        "doi": "10.1101/2026.07.01.123456",
        "title": "Autophagy induction extends lifespan in C. elegans",
        "abstract": "Spermidine treatment increased median survival.",
        "authors": "Eisenberg, T.; Knauer, H.",
        "date": "2026-07-01",
        "version": "1",
        "published": "NA",
    }
    return base | overrides


def page(entries: list[dict], status: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        text=json.dumps(
            {"messages": [{"status": status, "count": len(entries)}], "collection": entries}
        ),
    )


EXHAUSTED = httpx.Response(
    200, text=json.dumps({"messages": [{"status": "no posts found"}]})
)


def pages(*responses: httpx.Response):
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not remaining:
            raise AssertionError(f"unexpected extra request to {request.url}")
        return remaining.pop(0)

    return handler


def fetch(handler, query="autophagy lifespan", limit=10, **kwargs):
    return fetch_abstracts(
        query,
        limit,
        today=TODAY,
        client=make_client(handler),
        sleep=lambda _seconds: None,
        policy=FAST_POLICY,
        **kwargs,
    )


def test_parses_a_matching_preprint():
    papers = fetch(pages(page([entry()]), EXHAUSTED))
    assert len(papers) == 1
    paper = papers[0]
    assert paper.source == "biorxiv"
    assert paper.source_id == "10.1101/2026.07.01.123456"
    assert paper.doi == "10.1101/2026.07.01.123456"
    assert paper.year == 2026
    assert paper.first_author == "Eisenberg"
    assert paper.url == "https://doi.org/10.1101/2026.07.01.123456"


def test_published_preprint_takes_the_journal_doi_as_its_identity():
    """The bridge that makes preprint/publication dedup possible at all."""
    papers = fetch(pages(page([entry(published="10.1038/NATURE08221")]), EXHAUSTED))
    assert papers[0].doi == "10.1038/nature08221"
    assert papers[0].dedup_key == "doi:10.1038/nature08221"
    # The preprint DOI is still the record's source-local identity.
    assert papers[0].source_id == "10.1101/2026.07.01.123456"


@pytest.mark.parametrize(
    "published",
    [
        "https://doi.org/10.1038/nature08221",  # URL form
        "doi:10.1038/nature08221",  # prefixed form
        "31462531",  # a PMID, not a DOI
        "10.1038",  # truncated: prefix with no suffix
    ],
)
def test_a_published_field_that_is_not_a_doi_raises(published):
    """The one value dedup hinges on is checked, not trusted.

    Anything accepted here becomes the row's primary key, so a bad shape would
    make dedup miss its PubMed twin — indistinguishable from a preprint that
    genuinely has no published version.
    """
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(pages(page([entry(published=published)])))
    message = str(excinfo.value)
    assert "is not a bare DOI" in message
    assert published.lower() in message


def test_an_unpublished_preprint_is_not_affected_by_the_check():
    """"NA" means unpublished, not malformed."""
    papers = fetch(pages(page([entry(published="NA")]), EXHAUSTED))
    assert papers[0].doi == "10.1101/2026.07.01.123456"


@pytest.mark.parametrize("field", ["doi", "published"])
@pytest.mark.parametrize(
    "value",
    [
        2026,  # a number
        10.1101,  # a number that looks like a DOI prefix
        ["10.1038/nature08221"],  # a list
        {"doi": "10.1038/nature08221"},  # an object
    ],
)
def test_a_non_string_doi_field_raises_with_an_excerpt(field, value):
    """A wrong type in either DOI field is a format error, not an AttributeError.

    Both fields feed the dedup key, and both used to be handed to
    `normalise_doi` unguarded — where a non-string produced a bare
    `.strip()` AttributeError with no payload and no URL.
    """
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(pages(page([entry(**{field: value})])))
    message = str(excinfo.value)
    assert f"in {field!r}" in message
    assert "expected a string DOI" in message
    assert "payload excerpt" in message


def test_a_null_or_absent_published_field_means_unpublished():
    """Absence is legitimate — only a wrong type is an error."""
    absent = entry()
    del absent["published"]
    for candidate in (entry(published=None), absent):
        papers = fetch(pages(page([candidate]), EXHAUSTED))
        assert papers[0].doi == "10.1101/2026.07.01.123456"


def test_a_null_or_absent_doi_still_reports_the_missing_identity():
    """A null `doi` is absence, so it keeps the "no 'doi'" message, not a type error."""
    absent = entry()
    del absent["doi"]
    for candidate in (entry(doi=None), absent):
        with pytest.raises(ResponseFormatError, match="no 'doi'"):
            fetch(pages(page([candidate])))


def test_non_matching_preprints_are_filtered_out():
    off_topic = entry(
        doi="10.1101/2026.07.02.000001",
        title="A new method for cryo-EM alignment",
        abstract="No biology here.",
    )
    papers = fetch(pages(page([entry(), off_topic]), EXHAUSTED))
    assert [p.source_id for p in papers] == ["10.1101/2026.07.01.123456"]


def test_matching_is_whole_word_not_substring():
    """"rat" must not match "strategy"; the local filter stands in for search."""
    papers = fetch(
        pages(page([entry(title="A strategy for imaging", abstract="No rodents.")]), EXHAUSTED),
        query="rat",
    )
    assert papers == []


def test_query_terms_may_match_the_abstract_alone():
    papers = fetch(
        pages(
            page([entry(title="A short title", abstract="Autophagy extends lifespan.")]),
            EXHAUSTED,
        )
    )
    assert len(papers) == 1


def test_highest_version_of_a_preprint_wins():
    v1 = entry(version="1", abstract="Autophagy lifespan: preliminary.")
    v2 = entry(version="2", abstract="Autophagy lifespan: revised and final.")
    papers = fetch(pages(page([v1, v2]), EXHAUSTED))
    assert len(papers) == 1
    assert papers[0].abstract.endswith("revised and final.")


def test_pages_until_the_limit_is_reached():
    first = [entry(doi=f"10.1101/2026.07.01.{i:06d}") for i in range(3)]
    second = [entry(doi=f"10.1101/2026.07.02.{i:06d}") for i in range(3)]
    papers = fetch(pages(page(first), page(second)), limit=5)
    assert len(papers) == 5


def test_cursor_advances_by_page_size():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rstrip("/").rsplit("/", 1)[-1])
        if len(seen) == 1:
            return page([entry(doi=f"10.1101/x.{i:06d}") for i in range(100)])
        return EXHAUSTED

    fetch(handler, limit=1000)
    assert seen == ["0", "100"]


def test_window_is_derived_from_window_days():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return EXHAUSTED

    fetch(handler, window_days=7)
    assert seen[0] == "/details/biorxiv/2026-08-03/2026-08-10/0"


def test_exhausted_window_stops_paging():
    papers = fetch(pages(EXHAUSTED))
    assert papers == []


def test_empty_collection_stops_paging():
    papers = fetch(pages(page([])))
    assert papers == []


def test_page_cap_raises_rather_than_returning_a_partial_scan():
    def handler(request: httpx.Request) -> httpx.Response:
        return page([entry(doi=f"10.1101/nomatch.{request.url.path[-4:]}",
                           title="Cryo-EM", abstract="Nothing relevant.")])

    with pytest.raises(ScanLimitError, match=str(MAX_PAGES)):
        fetch(handler, window_days=3650)


def test_boolean_query_is_rejected_rather_than_mis_run():
    with pytest.raises(ValueError, match="no query language"):
        fetch(pages(EXHAUSTED), query="autophagy AND lifespan")


def test_field_tags_and_quotes_are_rejected():
    for query in ('"caloric restriction"', "lifespan[tiab]"):
        with pytest.raises(ValueError, match="no query language"):
            fetch(pages(EXHAUSTED), query=query)


def test_query_with_no_searchable_terms_is_rejected():
    with pytest.raises(ValueError, match="no searchable terms"):
        fetch(pages(EXHAUSTED), query="--- ...")


def test_non_json_response_raises_with_an_excerpt():
    body = "<html>502 Bad Gateway from the CDN</html>"
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(pages(httpx.Response(200, text=body)))
    assert "did not return JSON" in str(excinfo.value)
    assert "Bad Gateway" in str(excinfo.value)


def test_missing_collection_raises():
    body = json.dumps({"messages": [{"status": "ok"}]})
    with pytest.raises(ResponseFormatError, match="collection"):
        fetch(pages(httpx.Response(200, text=body)))


def test_unknown_status_raises():
    with pytest.raises(ResponseFormatError, match="status 'error'"):
        fetch(pages(page([], status="error")))


def test_json_array_instead_of_object_raises():
    with pytest.raises(ResponseFormatError, match="expected an object"):
        fetch(pages(httpx.Response(200, text="[]")))


def test_entry_without_a_doi_raises():
    with pytest.raises(ResponseFormatError, match="no 'doi'"):
        fetch(pages(page([entry(doi="")])))


def test_entry_without_a_title_raises():
    with pytest.raises(ResponseFormatError, match="empty title"):
        fetch(pages(page([entry(title="   ")])))


def test_rate_limiting_is_retried():
    responses = [httpx.Response(429), page([entry()]), EXHAUSTED]
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    papers = fetch_abstracts(
        "autophagy lifespan",
        10,
        today=TODAY,
        client=make_client(handler),
        sleep=delays.append,
        policy=FAST_POLICY,
    )
    assert len(papers) == 1
    assert delays == [1.0]


def test_persistent_rate_limiting_raises():
    with pytest.raises(TransportError, match="4 attempt"):
        fetch(lambda request: httpx.Response(429))


def test_arguments_are_validated():
    with pytest.raises(ValueError, match="limit"):
        fetch(pages(EXHAUSTED), limit=0)
    with pytest.raises(ValueError, match="window_days"):
        fetch(pages(EXHAUSTED), window_days=0)


# --------------------------------------------------------------------------
# fetch_detail: resolving a known DOI, the other direction of the same API
# --------------------------------------------------------------------------


def test_fetch_detail_requests_the_doi_endpoint():
    from ingest.biorxiv import fetch_detail

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"messages": [{"status": "ok"}],
                                         "collection": [entry()]})

    entries = fetch_detail("10.1101/2026.07.01.123456",
                           client=make_client(handler), policy=FAST_POLICY)
    assert len(entries) == 1
    assert captured[0].endswith("/details/biorxiv/10.1101/2026.07.01.123456")


def test_fetch_detail_returns_every_version_oldest_first():
    from ingest.biorxiv import fetch_detail

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"status": "ok"}], "collection": [
            entry(version=1, date="2025-09-04"), entry(version=2, date="2026-04-02")]})

    entries = fetch_detail("10.1101/2026.07.01.123456",
                           client=make_client(handler), policy=FAST_POLICY)
    assert [e["version"] for e in entries] == [1, 2]


def test_fetch_detail_returns_empty_for_a_doi_biorxiv_does_not_know():
    """'no posts found' is an answer about the DOI, not a fault, so it is not raised."""
    from ingest.biorxiv import fetch_detail

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"status": "no posts found"}],
                                         "collection": []})

    assert fetch_detail("10.1101/9999.99.99.999999",
                        client=make_client(handler), policy=FAST_POLICY) == []


def test_fetch_detail_raises_on_a_malformed_response():
    """Reuses _fetch_page, so the client's loud-failure contract still holds."""
    from ingest.biorxiv import fetch_detail

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    with pytest.raises(ResponseFormatError):
        fetch_detail("10.1101/2026.07.01.123456",
                     client=make_client(handler), policy=FAST_POLICY)
