"""PubMed client: esearch/efetch parsing, and every way a payload can lie."""

from __future__ import annotations

import json

import httpx
import pytest

from ingest.errors import ResponseFormatError, TransportError
from ingest.pubmed import (
    ANONYMOUS_MIN_INTERVAL,
    API_KEY_ENV,
    EMAIL_ENV,
    KEYED_MIN_INTERVAL,
    TOOL_NAME,
    fetch_abstracts,
)
from tests.conftest import FAST_POLICY, make_client


@pytest.fixture(autouse=True)
def anonymous_tier(monkeypatch):
    """Start every test from the anonymous tier.

    Without this the suite's throttle and parameter assertions would depend on
    whether the developer running it happens to have NCBI credentials exported.
    """
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(EMAIL_ENV, raising=False)

ARTICLE_XML = """
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">19587680</PMID>
      <Article>
        <Journal><JournalIssue><PubDate><Year>2009</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Rapamycin fed late in life extends lifespan in <i>mice</i></ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Inhibition of the TOR pathway.</AbstractText>
          <AbstractText Label="RESULTS">Median survival increased.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Harrison</LastName><ForeName>David</ForeName></Author>
          <Author><LastName>Strong</LastName></Author>
        </AuthorList>
        <ELocationID EIdType="doi">10.1038/NATURE08221</ELocationID>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


def article_xml(pmid: str, title: str = "Rapamycin and lifespan") -> str:
    return f"""
    <PubmedArticle><MedlineCitation>
      <PMID>{pmid}</PMID><Article><ArticleTitle>{title}</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle>
    """


def book_xml(pmid: str, title: str = "Sirolimus (StatPearls)") -> str:
    """A Bookshelf record, as efetch returns it for a StatPearls PMID."""
    return f"""
    <PubmedBookArticle><BookDocument>
      <PMID>{pmid}</PMID><ArticleTitle>{title}</ArticleTitle>
    </BookDocument></PubmedBookArticle>
    """


def delete_citation_xml(*pmids: str) -> str:
    """The `<DeleteCitation>` efetch returns for deleted or merged PMIDs."""
    entries = "".join(f"<PMID>{pmid}</PMID>" for pmid in pmids)
    return f"<DeleteCitation>{entries}</DeleteCitation>"


def article_set(*records: str) -> httpx.Response:
    return httpx.Response(
        200, text=f"<PubmedArticleSet>{''.join(records)}</PubmedArticleSet>"
    )


def esearch_response(*pmids: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=json.dumps({"esearchresult": {"count": str(len(pmids)), "idlist": list(pmids)}}),
    )


def routed(esearch: httpx.Response, efetch: httpx.Response | None = None):
    """Route by endpoint rather than by call order, mirroring the real API."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            return esearch
        if efetch is None:
            raise AssertionError("efetch was called but no response was queued")
        return efetch

    return handler


def fetch(handler, query="rapamycin lifespan", limit=10, sleeps=None):
    return fetch_abstracts(
        query,
        limit,
        client=make_client(handler),
        sleep=sleeps or (lambda _seconds: None),
        policy=FAST_POLICY,
    )


def test_parses_a_full_record():
    papers = fetch(routed(esearch_response("19587680"), httpx.Response(200, text=ARTICLE_XML)))
    assert len(papers) == 1
    paper = papers[0]
    assert paper.pmid == "19587680"
    assert paper.source == "pubmed"
    assert paper.source_id == "19587680"
    # DOIs are case-insensitive; the schema wants the lowercase canonical form.
    assert paper.doi == "10.1038/nature08221"
    assert paper.dedup_key == "doi:10.1038/nature08221"
    assert paper.title == "Rapamycin fed late in life extends lifespan in mice"
    assert paper.year == 2009
    assert paper.first_author == "Harrison"
    assert paper.abstract == (
        "BACKGROUND: Inhibition of the TOR pathway.\n\nRESULTS: Median survival increased."
    )
    assert paper.url == "https://pubmed.ncbi.nlm.nih.gov/19587680/"


def test_empty_result_set_returns_no_papers_and_skips_efetch():
    # efetch=None makes a stray call an assertion failure: an empty idlist must
    # not produce a request for zero ids.
    assert fetch(routed(esearch_response())) == []


def test_record_without_an_abstract_is_kept_with_none():
    xml = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <PMID>1</PMID>
      <Article><ArticleTitle>Comment on rapamycin</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>
    """
    papers = fetch(routed(esearch_response("1"), httpx.Response(200, text=xml)))
    assert papers[0].abstract is None
    # No DOI in the payload, so identity falls back to the source-local id.
    assert papers[0].doi is None
    assert papers[0].dedup_key == "pubmed:1"


def test_doi_is_read_from_article_id_list_when_elocation_is_absent():
    xml = """
    <PubmedArticleSet><PubmedArticle>
      <MedlineCitation>
        <PMID>2</PMID>
        <Article><ArticleTitle>Metformin</ArticleTitle></Article>
      </MedlineCitation>
      <PubmedData><ArticleIdList>
        <ArticleId IdType="pubmed">2</ArticleId>
        <ArticleId IdType="doi">10.1038/ncomms3192</ArticleId>
      </ArticleIdList></PubmedData>
    </PubmedArticle></PubmedArticleSet>
    """
    papers = fetch(routed(esearch_response("2"), httpx.Response(200, text=xml)))
    assert papers[0].doi == "10.1038/ncomms3192"


def test_medline_date_free_text_still_yields_a_year():
    xml = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <PMID>3</PMID>
      <Article>
        <Journal><JournalIssue><PubDate>
          <MedlineDate>1998 Jan-Feb</MedlineDate>
        </PubDate></JournalIssue></Journal>
        <ArticleTitle>eat-2 mutants</ArticleTitle>
      </Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>
    """
    papers = fetch(routed(esearch_response("3"), httpx.Response(200, text=xml)))
    assert papers[0].year == 1998


def test_undated_record_yields_no_year_rather_than_a_guess():
    xml = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <PMID>4</PMID><Article><ArticleTitle>Undated</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>
    """
    papers = fetch(routed(esearch_response("4"), httpx.Response(200, text=xml)))
    assert papers[0].year is None


def test_non_json_esearch_raises_with_an_excerpt():
    body = "<html><body>Service temporarily unavailable</body></html>"
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(routed(httpx.Response(200, text=body)))
    message = str(excinfo.value)
    assert "did not return JSON" in message
    assert "Service temporarily unavailable" in message


def test_esearch_without_idlist_raises():
    body = json.dumps({"esearchresult": {"count": "0"}})
    with pytest.raises(ResponseFormatError, match="idlist"):
        fetch(routed(httpx.Response(200, text=body)))


def test_esearch_without_esearchresult_raises():
    with pytest.raises(ResponseFormatError, match="esearchresult"):
        fetch(routed(httpx.Response(200, text=json.dumps({"header": {}}))))


def test_esearch_error_field_is_surfaced():
    body = json.dumps({"esearchresult": {"ERROR": "Invalid db name"}})
    with pytest.raises(ResponseFormatError, match="Invalid db name"):
        fetch(routed(httpx.Response(200, text=body)))


def test_non_numeric_pmid_from_esearch_raises():
    body = json.dumps({"esearchresult": {"idlist": ["not-a-pmid"]}})
    with pytest.raises(ResponseFormatError, match="non-numeric PMID"):
        fetch(routed(httpx.Response(200, text=body)))


def test_malformed_efetch_xml_raises_with_an_excerpt():
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(
            routed(
                esearch_response("19587680"),
                httpx.Response(200, text="<PubmedArticleSet><PubmedArticle>"),
            )
        )
    message = str(excinfo.value)
    assert "parseable XML" in message
    assert "PubmedArticleSet" in message


def test_efetch_returning_nothing_for_known_ids_raises():
    with pytest.raises(
        ResponseFormatError, match="requested but not returned: 19587680"
    ):
        fetch(
            routed(
                esearch_response("19587680"),
                httpx.Response(200, text="<PubmedArticleSet/>"),
            )
        )


def test_bookshelf_records_are_skipped_and_counted_not_dropped(capsys):
    """esearch returns Bookshelf PMIDs; efetch answers with PubmedBookArticle.

    Skipping them silently would make the ingest count disagree with PubMed's
    with nothing on the terminal to explain the gap.
    """
    papers = fetch(
        routed(
            esearch_response("1", "2"),
            article_set(article_xml("1"), book_xml("2")),
        )
    )
    assert [paper.pmid for paper in papers] == ["1"]
    err = capsys.readouterr().err
    assert "skipped 1 PubMed Bookshelf record(s)" in err


def test_a_batch_of_only_book_records_is_not_an_error(capsys):
    """A valid upstream response must not abort the run."""
    papers = fetch(routed(esearch_response("2"), article_set(book_xml("2"))))
    assert papers == []
    assert "skipped 1 PubMed Bookshelf record(s)" in capsys.readouterr().err


def test_no_warning_when_nothing_was_skipped(capsys):
    fetch(routed(esearch_response("1"), article_set(article_xml("1"))))
    assert capsys.readouterr().err == ""


def test_a_short_efetch_batch_raises_rather_than_losing_records():
    """Two PMIDs requested, one element returned: the other vanished."""
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(routed(esearch_response("1", "2"), article_set(article_xml("1"))))
    message = str(excinfo.value)
    assert "requested but not returned: 2" in message
    # The missing PMID is named, not merely counted: an operator can look it up.
    assert "returned but not requested" not in message


def test_a_deleted_pmid_is_accounted_for_and_announced(capsys):
    """A PMID deleted between esearch and efetch is a correct upstream answer.

    `PubmedArticleSet` is ((PubmedArticle | PubmedBookArticle)*,
    DeleteCitation?), so a deleted or merged PMID comes back as neither record
    element. Aborting the run on it would throw away every good paper in the
    batch over an event PubMed handles routinely.
    """
    papers = fetch(
        routed(
            esearch_response("1", "2"),
            article_set(article_xml("1"), delete_citation_xml("2")),
        )
    )
    assert [paper.pmid for paper in papers] == ["1"]
    err = capsys.readouterr().err
    # Named, so the gap between requested and stored is explainable.
    assert "skipped 1 deleted PubMed citation(s) (<DeleteCitation>: 2)" in err


def test_a_batch_of_only_deleted_citations_is_not_an_error(capsys):
    papers = fetch(routed(esearch_response("2"), article_set(delete_citation_xml("2"))))
    assert papers == []
    assert "skipped 1 deleted PubMed citation(s)" in capsys.readouterr().err


def test_the_right_number_of_records_for_the_wrong_pmids_raises():
    """A count check cannot see this: two requested, two returned, one wrong."""
    with pytest.raises(ResponseFormatError) as excinfo:
        fetch(
            routed(
                esearch_response("1", "2"),
                article_set(article_xml("1"), article_xml("3")),
            )
        )
    message = str(excinfo.value)
    assert "requested but not returned: 2" in message
    assert "returned but not requested: 3" in message


def test_article_without_a_pmid_raises():
    xml = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <Article><ArticleTitle>Anonymous</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>
    """
    with pytest.raises(ResponseFormatError, match="non-numeric PMID"):
        fetch(routed(esearch_response("5"), httpx.Response(200, text=xml)))


def test_article_without_a_title_raises():
    xml = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation>
      <PMID>6</PMID><Article><ArticleTitle></ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>
    """
    with pytest.raises(ResponseFormatError, match="empty ArticleTitle"):
        fetch(routed(esearch_response("6"), httpx.Response(200, text=xml)))


def test_entity_expansion_is_refused():
    """A billion-laughs payload must not be expanded. defusedxml refuses it."""
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
    <PubmedArticleSet>&lol2;</PubmedArticleSet>
    """
    with pytest.raises(ResponseFormatError, match="parseable XML"):
        fetch(routed(esearch_response("7"), httpx.Response(200, text=bomb)))


def test_rate_limited_esearch_is_retried_then_parsed():
    responses = [httpx.Response(429), esearch_response("19587680")]
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            return responses.pop(0)
        return httpx.Response(200, text=ARTICLE_XML)

    papers = fetch_abstracts(
        "rapamycin",
        10,
        client=make_client(handler),
        sleep=delays.append,
        policy=FAST_POLICY,
    )
    assert [paper.pmid for paper in papers] == ["19587680"]
    # 1.0s of retry backoff, then the tier's own gap before efetch.
    assert delays == [1.0, ANONYMOUS_MIN_INTERVAL]


def test_persistent_rate_limiting_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(TransportError, match="4 attempt"):
        fetch(handler)


def test_limit_and_query_are_validated():
    handler = routed(esearch_response())
    with pytest.raises(ValueError, match="limit"):
        fetch(handler, limit=0)
    with pytest.raises(ValueError, match="query"):
        fetch(handler, query="   ")


def test_limit_is_requested_and_enforced_locally():
    """retmax is sent, and an upstream that ignores it is still trimmed."""
    search_params: dict[str, str] = {}
    fetched_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            search_params.update(dict(request.url.params))
            return esearch_response("1", "2", "3")
        fetched_ids.extend(
            httpx.QueryParams(request.content.decode())["id"].split(",")
        )
        return article_set(article_xml("1"), article_xml("2"))

    fetch_abstracts("rapamycin", 2, client=make_client(handler), sleep=lambda _s: None)
    assert search_params["retmax"] == "2"
    assert fetched_ids == ["1", "2"]


def test_the_documented_invocation_is_paced_between_esearch_and_efetch(sleeps):
    """PLAN.md line 49 runs `--limit 100`: one esearch, one efetch, one gap.

    Pacing only *between efetch batches* would leave this run — the only shape
    the CLI realistically makes — entirely unthrottled.
    """
    handler = routed(esearch_response("1"), article_set(article_xml("1")))
    fetch_abstracts("rapamycin", 100, client=make_client(handler), sleep=sleeps)
    assert sleeps.delays == [ANONYMOUS_MIN_INTERVAL]


def test_an_api_key_buys_the_faster_tier(monkeypatch, sleeps):
    monkeypatch.setenv(API_KEY_ENV, "deadbeef")
    handler = routed(esearch_response("1"), article_set(article_xml("1")))
    fetch_abstracts("rapamycin", 100, client=make_client(handler), sleep=sleeps)
    assert sleeps.delays == [KEYED_MIN_INTERVAL]


def test_every_efetch_batch_is_paced(monkeypatch, sleeps):
    monkeypatch.setattr("ingest.pubmed.EFETCH_BATCH", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            return esearch_response("1", "2", "3")
        pmid = httpx.QueryParams(request.content.decode())["id"]
        return article_set(article_xml(pmid))

    fetch_abstracts("rapamycin", 3, client=make_client(handler), sleep=sleeps)
    # Four requests, three gaps: the first request is never delayed.
    assert sleeps.delays == [ANONYMOUS_MIN_INTERVAL] * 3


def test_credentials_are_sent_on_every_request_when_set(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "deadbeef")
    monkeypatch.setenv(EMAIL_ENV, "sofia@example.org")
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            seen["esearch"] = dict(request.url.params)
            return esearch_response("1")
        seen["efetch"] = dict(httpx.QueryParams(request.content.decode()))
        return article_set(article_xml("1"))

    fetch(handler)
    assert set(seen) == {"esearch", "efetch"}
    for endpoint, params in seen.items():
        assert params["api_key"] == "deadbeef", endpoint
        assert params["email"] == "sofia@example.org", endpoint
        assert params["tool"] == TOOL_NAME, endpoint


def test_credentials_are_omitted_rather_than_sent_blank_when_unset():
    """The anonymous tier is a mode of the API, not a request with empty fields."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            seen.append(dict(request.url.params))
            return esearch_response("1")
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return article_set(article_xml("1"))

    fetch(handler)
    assert len(seen) == 2
    for params in seen:
        assert "api_key" not in params
        assert "email" not in params
        assert params["tool"] == TOOL_NAME
