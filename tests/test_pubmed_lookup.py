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


# --------------------------------------------------------------------------
# PMC full text
# --------------------------------------------------------------------------

ELINK_XML = """<?xml version="1.0"?>
<eLinkResult>
  <LinkSet>
    <DbFrom>pubmed</DbFrom>
    <IdList><Id>19587680</Id></IdList>
    <LinkSetDb>
      <DbTo>pmc</DbTo>
      <LinkName>pubmed_pmc_refs</LinkName>
      <Link><Id>9999999</Id></Link>
    </LinkSetDb>
    <LinkSetDb>
      <DbTo>pmc</DbTo>
      <LinkName>pubmed_pmc</LinkName>
      <Link><Id>2786175</Id></Link>
    </LinkSetDb>
  </LinkSet>
</eLinkResult>
"""

ELINK_NO_PMC_XML = """<?xml version="1.0"?>
<eLinkResult>
  <LinkSet>
    <DbFrom>pubmed</DbFrom>
    <IdList><Id>19587680</Id></IdList>
  </LinkSet>
</eLinkResult>
"""

PMC_XML = """<?xml version="1.0"?>
<pmc-articleset>
  <article>
    <front>
      <article-meta>
        <title-group><article-title>Rapamycin fed late in life</article-title></title-group>
        <abstract><p>Rapamycin extends median lifespan.</p></abstract>
      </article-meta>
    </front>
    <body>
      <sec>
        <title>Methods</title>
        <p>Mice were produced by mating
        CB6F1 females to <italic>C3D2F1</italic> males.</p>
      </sec>
      <sec>
        <title>Results</title>
        <p>Median survival increased by 14%.</p>
        <table-wrap>
          <caption><p>Table 1. Survival by sex.</p></caption>
          <table><tbody><tr><td>males</td><td>14%</td></tr></tbody></table>
        </table-wrap>
      </sec>
    </body>
    <back>
      <ref-list><ref><p>Some citation nobody quotes.</p></ref></ref-list>
    </back>
    <floats-group>
      <fig>
        <label>Figure 1</label>
        <caption><p>The arrows at 54 weeks indicate the start of treatment.</p></caption>
      </fig>
    </floats-group>
  </article>
</pmc-articleset>
"""

# What efetch returns for a PMC record outside the open-access subset: the front
# matter, and no <body> at all.
PMC_NO_BODY_XML = PMC_XML.split("<body>")[0] + "</article></pmc-articleset>"


class RoutingStubClient:
    """Stub client that answers elink and efetch differently.

    `fetch_full_text` makes two calls with one client, so a single canned
    response cannot exercise it.
    """

    def __init__(self, elink=ELINK_XML, efetch=PMC_XML):
        self.elink = elink
        self.efetch = efetch
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return StubResponse(self.elink if "elink" in url else self.efetch)


def test_parse_pmcid_prefixes_the_bare_numeric_id():
    assert pubmed_lookup.parse_pmcid(ELINK_XML, "19587680") == "PMC2786175"


def test_parse_pmcid_ignores_the_citing_articles_link_set():
    """`pubmed_pmc_refs` is the papers that cite this one — a different article."""
    pmcid = pubmed_lookup.parse_pmcid(ELINK_XML, "19587680")
    assert pmcid != "PMC9999999"


def test_parse_pmcid_returns_none_when_there_is_no_pmc_record():
    assert pubmed_lookup.parse_pmcid(ELINK_NO_PMC_XML, "19587680") is None


def test_parse_pmcid_refuses_a_response_about_a_different_pmid():
    xml = ELINK_XML.replace("<Id>19587680</Id>", "<Id>11111111</Id>", 1)
    with pytest.raises(PubMedLookupError, match="Not treating the response"):
        pubmed_lookup.parse_pmcid(xml, "19587680")


def test_parse_pmcid_refuses_to_guess_between_several_articles():
    xml = ELINK_XML.replace(
        "<Link><Id>2786175</Id></Link>",
        "<Link><Id>2786175</Id></Link><Link><Id>2786176</Id></Link>",
    )
    with pytest.raises(PubMedLookupError, match="refusing to guess"):
        pubmed_lookup.parse_pmcid(xml, "19587680")


def test_parse_full_text_flattens_the_body():
    text = pubmed_lookup.parse_full_text(PMC_XML, "PMC2786175")
    assert "Methods" in text
    assert "Median survival increased by 14%." in text


def test_parse_full_text_keeps_inline_markup_contiguous():
    """`<italic>C3D2F1</italic>` must not become `C3D2F1` with spaces around it."""
    text = pubmed_lookup.parse_full_text(PMC_XML, "PMC2786175")
    assert "to C3D2F1 males" in " ".join(text.split())


def test_parse_full_text_includes_the_abstract_and_table_captions():
    text = pubmed_lookup.parse_full_text(PMC_XML, "PMC2786175")
    assert "Rapamycin extends median lifespan." in text
    assert "Table 1. Survival by sex." in text


def test_parse_full_text_includes_floats_group_figures():
    """Some deposits park every figure and table outside <body>. Reading only
    <body> would fail each quote from them, which looks like a bad gold file."""
    text = pubmed_lookup.parse_full_text(PMC_XML, "PMC2786175")
    assert "The arrows at 54 weeks indicate the start of treatment." in text
    assert "Figure 1" in text


def test_parse_full_text_leaves_out_the_reference_list():
    """Nothing is quoted from <back>, and it is the biggest source of stray matches."""
    text = pubmed_lookup.parse_full_text(PMC_XML, "PMC2786175")
    assert "Some citation nobody quotes." not in text


def test_parse_full_text_returns_none_without_a_body():
    """A front-matter-only deposit is what closed access looks like from efetch."""
    assert pubmed_lookup.parse_full_text(PMC_NO_BODY_XML, "PMC2786175") is None


def test_parse_full_text_returns_none_for_an_error_payload():
    xml = '<pmc-articleset><error>cannot get document</error></pmc-articleset>'
    assert pubmed_lookup.parse_full_text(xml, "PMC2786175") is None


def test_fetch_full_text_resolves_then_fetches(tmp_path):
    client = RoutingStubClient()
    record = pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)

    assert record.pmcid == "PMC2786175"
    assert "Median survival increased by 14%." in record.text
    assert record.available

    elink_call, efetch_call = client.calls
    assert elink_call[2]["params"] == {
        **elink_call[2]["params"],
        "dbfrom": "pubmed",
        "db": "pmc",
        "id": "19587680",
    }
    assert efetch_call[2]["params"]["db"] == "pmc"
    assert efetch_call[2]["params"]["id"] == "PMC2786175"


def test_fetch_full_text_for_a_paper_with_no_pmc_record(tmp_path):
    client = RoutingStubClient(elink=ELINK_NO_PMC_XML)
    record = pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)

    assert record.pmcid is None
    assert record.text is None
    assert not record.available
    # No point asking efetch about an article PMC does not have.
    assert len(client.calls) == 1


def test_fetch_full_text_for_a_paper_pmc_has_but_will_not_serve(tmp_path):
    client = RoutingStubClient(efetch=PMC_NO_BODY_XML)
    record = pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)

    assert record.pmcid == "PMC2786175"
    assert record.text is None


def test_fetch_full_text_caches_the_negative_answer(tmp_path):
    """Otherwise every run re-asks NCBI the same question about the same papers."""
    client = RoutingStubClient(elink=ELINK_NO_PMC_XML)
    first = pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)
    second = pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)

    assert first == second
    assert len(client.calls) == 1


def test_fetch_full_text_refresh_bypasses_the_cache(tmp_path):
    client = RoutingStubClient()
    pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)
    pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client, refresh=True)
    assert len(client.calls) == 4


def test_fetch_full_text_rejects_a_non_numeric_pmid():
    with pytest.raises(ValueError, match="digits only"):
        pubmed_lookup.fetch_full_text("PMC12345", client=RoutingStubClient())


def test_full_text_cache_round_trips(tmp_path):
    record = pubmed_lookup.PMCFullText(pmid="1", pmcid="PMC1", text="body")
    pubmed_lookup.write_fulltext_cache(record, cache_dir=tmp_path)
    assert pubmed_lookup.read_fulltext_cache("1", cache_dir=tmp_path) == record


def test_full_text_cache_sits_beside_the_abstract_entry(tmp_path):
    """Both are keyed by PMID; the names must not collide."""
    write_cache(PubMedRecord(pmid="1", title="T", abstract="A"), cache_dir=tmp_path)
    pubmed_lookup.write_fulltext_cache(
        pubmed_lookup.PMCFullText(pmid="1", pmcid="PMC1", text="body"), cache_dir=tmp_path
    )

    assert read_cache("1", cache_dir=tmp_path).abstract == "A"
    assert pubmed_lookup.read_fulltext_cache("1", cache_dir=tmp_path).text == "body"


def test_full_text_cache_version_is_independent_of_the_abstract_cache(tmp_path):
    record = pubmed_lookup.PMCFullText(pmid="1", pmcid="PMC1", text="body")
    path = pubmed_lookup.write_fulltext_cache(record, cache_dir=tmp_path)
    payload = json.loads(path.read_text())
    payload["cache_version"] = pubmed_lookup.FULLTEXT_CACHE_VERSION + 1
    path.write_text(json.dumps(payload))

    assert pubmed_lookup.read_fulltext_cache("1", cache_dir=tmp_path) is None


def test_read_full_text_cache_returns_none_when_absent(tmp_path):
    assert pubmed_lookup.read_fulltext_cache("404", cache_dir=tmp_path) is None


# --------------------------------------------------------------------------
# elink faults: a statement about the request, never about the paper
# --------------------------------------------------------------------------

# The payload NCBI actually served, for every PMID, during the 2026-08-11
# outage — including papers known to be in PMC. The old parser found no
# LinkSet, fell through, and reported "no PMC record".
ELINK_ERROR_XML = """<?xml version="1.0"?>
<eLinkResult>
  <ERROR>NCBI C++ Exception:
    Error: TXCLIENT(CException::eUnknown) --- Read failed: EOF (the other side
    has unexpectedly closed connection), peer: 130.14.18.86:8064
  </ERROR>
</eLinkResult>
"""

ELINK_EMPTY_XML = '<?xml version="1.0"?><eLinkResult></eLinkResult>'


def test_parse_pmcid_raises_on_an_error_payload():
    """The bug: this used to be indistinguishable from 'not in PMC'."""
    with pytest.raises(PubMedLookupError, match="rather than a link set"):
        pubmed_lookup.parse_pmcid(ELINK_ERROR_XML, "27312235")


def test_parse_pmcid_raises_when_there_is_no_link_set_at_all():
    with pytest.raises(PubMedLookupError, match="no LinkSet and no ERROR"):
        pubmed_lookup.parse_pmcid(ELINK_EMPTY_XML, "27312235")


def test_parse_pmcid_still_returns_none_for_a_genuine_no_pmc_answer():
    """The fix must not turn a real negative into an exception."""
    assert pubmed_lookup.parse_pmcid(ELINK_NO_PMC_XML, "19587680") is None


def test_fetch_full_text_does_not_cache_a_negative_it_did_not_establish(tmp_path):
    """A cached 'not in PMC' that was really 'NCBI was down' is indistinguishable
    from the truth on every later run. So it must not be written."""
    client = RoutingStubClient(elink=ELINK_ERROR_XML)
    with pytest.raises(PubMedLookupError):
        pubmed_lookup.fetch_full_text("27312235", cache_dir=tmp_path, client=client)

    assert not pubmed_lookup.fulltext_cache_path("27312235", cache_dir=tmp_path).exists()
    assert pubmed_lookup.read_fulltext_cache("27312235", cache_dir=tmp_path) is None


def test_fetch_full_text_does_cache_a_genuine_negative(tmp_path):
    """The other half of the contract: a real 'no PMC record' is still cached,
    so a closed-access paper is not re-asked on every run."""
    client = RoutingStubClient(elink=ELINK_NO_PMC_XML)
    record = pubmed_lookup.fetch_full_text("19587680", cache_dir=tmp_path, client=client)

    assert record.pmcid is None and record.text is None
    assert pubmed_lookup.fulltext_cache_path("19587680", cache_dir=tmp_path).exists()
    assert pubmed_lookup.read_fulltext_cache("19587680", cache_dir=tmp_path) == record


def test_parse_full_text_raises_on_a_eutils_fault():
    """Upper-case <ERROR> is a fault; lower-case <error> is PMC's answer."""
    with pytest.raises(PubMedLookupError, match="rather than an article"):
        pubmed_lookup.parse_full_text(
            '<pmc-articleset><ERROR>backend failed</ERROR></pmc-articleset>', "PMC1"
        )


def test_parse_full_text_still_returns_none_for_pmcs_own_error_element():
    assert pubmed_lookup.parse_full_text(
        '<pmc-articleset><error>cannot get document</error></pmc-articleset>', "PMC1"
    ) is None
