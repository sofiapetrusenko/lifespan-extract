"""Tests for scripts/pubmed_lookup.py. No network: the HTTP client is a stub."""

from __future__ import annotations

import json

import pubmed_lookup
import pytest
from pubmed_lookup import (
    CACHE_VERSION,
    PubMedLookupError,
    PubMedRecord,
    efetch_one,
    fetch_record,
    parse_record,
    read_cache,
    write_cache,
)

pytest.importorskip("defusedxml", reason="ingest runtime dependency; see requirements.txt")


ARTICLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">19587680</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2009</Year><Month>Jul</Month></PubDate>
          </JournalIssue>
          <Title>Nature</Title>
          <ISOAbbreviation>Nature</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Rapamycin fed late in life extends lifespan in <i>mice</i></ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Inhibition of the TOR pathway.</AbstractText>
          <AbstractText Label="RESULTS">Rapamycin extends median lifespan.</AbstractText>
        </Abstract>
        <ELocationID EIdType="doi" ValidYN="Y">10.1038/Nature08221</ELocationID>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">19587680</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class StubResponse:
    def __init__(self, content: str):
        self.content = content.encode()
        self.text = content
        self.status_code = 200
        self.is_error = False
        self.headers: dict[str, str] = {}


class StubClient:
    """Stands in for httpx.Client. Records the calls it was asked to make."""

    def __init__(self, content: str = ARTICLE_XML):
        self.content = content
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return StubResponse(self.content)


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    """Keep the rate limiter from making the suite wait on real time."""
    monkeypatch.setattr(pubmed_lookup, "_last_request_at", None)
    monkeypatch.setattr(pubmed_lookup.time, "sleep", lambda _seconds: None)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parse_record_extracts_every_metadata_field():
    record = parse_record(ARTICLE_XML, "19587680")
    assert record.pmid == "19587680"
    assert record.journal == "Nature"
    assert record.year == 2009
    # Inline markup in the title is flattened, not dropped.
    assert record.title == "Rapamycin fed late in life extends lifespan in mice"


def test_parse_record_lowercases_the_doi():
    """The schema calls for a lowercase canonical DOI and DOIs are case-insensitive."""
    assert parse_record(ARTICLE_XML, "19587680").doi == "10.1038/nature08221"


def test_parse_record_keeps_structured_abstract_labels():
    abstract = parse_record(ARTICLE_XML, "19587680").abstract
    assert abstract == (
        "BACKGROUND: Inhibition of the TOR pathway.\n\n"
        "RESULTS: Rapamycin extends median lifespan."
    )


def test_parse_record_returns_none_abstract_when_absent():
    xml = ARTICLE_XML.replace("<Abstract>", "<NoAbstract>").replace("</Abstract>", "</NoAbstract>")
    assert parse_record(xml, "19587680").abstract is None


def test_parse_record_falls_back_to_iso_abbreviation_for_journal():
    xml = ARTICLE_XML.replace("<Title>Nature</Title>", "")
    assert parse_record(xml, "19587680").journal == "Nature"


def test_parse_record_year_is_none_rather_than_guessed():
    xml = ARTICLE_XML.replace("<Year>2009</Year>", "")
    assert parse_record(xml, "19587680").year is None


def test_parse_record_reads_the_year_from_an_unindented_pubdate():
    """Flattening `<Year>2009</Year><Month>Jul</Month>` gives `2009Jul`.

    A `\\b`-anchored regex over that finds nothing, so parsing the year would
    depend on whether NCBI happened to pretty-print the response.
    """
    xml = ARTICLE_XML.replace(
        "<PubDate><Year>2009</Year><Month>Jul</Month></PubDate>",
        "<PubDate><Year>2009</Year><Month>Jul</Month><Day>16</Day></PubDate>",
    ).replace("\n            ", "")
    assert parse_record(xml, "19587680").year == 2009


def test_parse_record_reads_the_year_from_medline_free_text():
    """`<MedlineDate>1998 Nov-Dec</MedlineDate>` replaces `<Year>` on old records."""
    xml = ARTICLE_XML.replace(
        "<PubDate><Year>2009</Year><Month>Jul</Month></PubDate>",
        "<PubDate><MedlineDate>1998 Nov-Dec</MedlineDate></PubDate>",
    )
    assert parse_record(xml, "19587680").year == 1998


def test_parse_record_rejects_a_response_for_a_different_pmid():
    """A silent substitution here would attach the wrong abstract to a gold file."""
    with pytest.raises(PubMedLookupError, match="returned '19587680'"):
        parse_record(ARTICLE_XML, "99999999")


def test_parse_record_raises_on_unknown_pmid():
    with pytest.raises(PubMedLookupError, match="no PubmedArticle"):
        parse_record("<PubmedArticleSet></PubmedArticleSet>", "19587680")


def test_parse_record_raises_on_unparseable_xml_with_an_excerpt():
    with pytest.raises(PubMedLookupError) as exc:
        parse_record("<PubmedArticleSet><broken", "19587680")
    assert "payload excerpt" in str(exc.value)
    assert "<broken" in str(exc.value)


def test_parse_record_raises_on_empty_title():
    xml = ARTICLE_XML.replace(
        "<ArticleTitle>Rapamycin fed late in life extends lifespan in <i>mice</i></ArticleTitle>",
        "<ArticleTitle></ArticleTitle>",
    )
    with pytest.raises(PubMedLookupError, match="empty ArticleTitle"):
        parse_record(xml, "19587680")


def test_parse_record_refuses_multiple_articles_for_one_pmid():
    doubled = ARTICLE_XML.replace("</PubmedArticleSet>", "")
    body = doubled.split("<PubmedArticleSet>", 1)[1]
    xml = f"<PubmedArticleSet>{body}{body}</PubmedArticleSet>"
    with pytest.raises(PubMedLookupError, match="refusing to guess"):
        parse_record(xml, "19587680")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def test_efetch_one_requests_the_abstract_rettype():
    client = StubClient()
    efetch_one("19587680", client=client)

    method, url, kwargs = client.calls[0]
    assert method == "GET"
    assert url.endswith("efetch.fcgi")
    assert kwargs["params"]["rettype"] == "abstract"
    assert kwargs["params"]["id"] == "19587680"
    assert kwargs["params"]["db"] == "pubmed"
    # NCBI blocks unidentified clients at the IP level.
    assert kwargs["params"]["tool"] == "lifespan-extract"


def test_efetch_one_sends_the_api_key_when_one_is_configured(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "secret")
    client = StubClient()
    efetch_one("19587680", client=client)
    assert client.calls[0][2]["params"]["api_key"] == "secret"


def test_efetch_one_omits_the_api_key_when_unset(monkeypatch):
    """The anonymous tier is a supported mode, not a degraded credential."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    client = StubClient()
    efetch_one("19587680", client=client)
    assert "api_key" not in client.calls[0][2]["params"]


def test_fetch_record_rejects_a_non_numeric_pmid():
    with pytest.raises(ValueError, match="digits only"):
        fetch_record("PMC12345", client=StubClient())


def test_fetch_record_writes_then_reuses_the_cache(tmp_path):
    client = StubClient()
    first = fetch_record("19587680", cache_dir=tmp_path, client=client)
    second = fetch_record("19587680", cache_dir=tmp_path, client=client)

    assert first == second
    # The whole point of the cache: one network call per paper, ever.
    assert len(client.calls) == 1


def test_fetch_record_refresh_bypasses_the_cache(tmp_path):
    client = StubClient()
    fetch_record("19587680", cache_dir=tmp_path, client=client)
    fetch_record("19587680", cache_dir=tmp_path, client=client, refresh=True)
    assert len(client.calls) == 2


def test_cache_round_trips_every_field(tmp_path):
    record = PubMedRecord(
        pmid="1",
        title="T",
        journal="J",
        year=1999,
        doi="10.1/x",
        abstract="A",
    )
    write_cache(record, cache_dir=tmp_path)
    assert read_cache("1", cache_dir=tmp_path) == record


def test_read_cache_returns_none_for_a_stale_version(tmp_path):
    record = PubMedRecord(pmid="1", title="T")
    path = write_cache(record, cache_dir=tmp_path)
    payload = json.loads(path.read_text())
    payload["cache_version"] = CACHE_VERSION + 1
    path.write_text(json.dumps(payload))

    assert read_cache("1", cache_dir=tmp_path) is None


def test_read_cache_warns_and_misses_on_corrupt_json(tmp_path, capsys):
    (tmp_path / "1.json").write_text("{not json")
    assert read_cache("1", cache_dir=tmp_path) is None
    assert "warning" in capsys.readouterr().err


def test_read_cache_returns_none_when_absent(tmp_path):
    assert read_cache("404", cache_dir=tmp_path) is None
