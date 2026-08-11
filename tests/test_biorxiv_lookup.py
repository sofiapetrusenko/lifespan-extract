"""Tests for scripts/biorxiv_lookup.py. No network: the HTTP client is a stub."""

from __future__ import annotations

import json

import biorxiv_lookup
import pytest
from biorxiv_lookup import (
    CACHE_VERSION,
    BioRxivLookupError,
    BioRxivRecord,
    fetch_record,
    normalise_doi,
    parse_detail,
    read_cache,
    write_cache,
)

pytest.importorskip("httpx", reason="ingest runtime dependency; see requirements.txt")

DOI = "10.1101/2025.08.31.673254"

ABSTRACT = (
    "We find that valine restriction (Val-R) improves metabolic health in C57BL/6J mice, "
    "and extends the lifespan of male, but not female, mice by 23%."
)


def entry(**overrides) -> dict:
    base = {
        "doi": DOI,
        "title": "Lifelong restriction of dietary valine has sex-specific benefits",
        "date": "2025-09-04",
        "version": 1,
        "category": "physiology",
        "server": "bioRxiv",
        "published": "NA",
        "abstract": ABSTRACT,
    }
    base.update(overrides)
    return base


class StubResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.text = json.dumps(payload)
        self.status_code = 200
        self.is_error = False
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


class StubClient:
    """Stands in for httpx.Client. Records the calls it was asked to make."""

    def __init__(self, collection=None, messages=None):
        self.payload = {
            "messages": messages or [{"status": "ok"}],
            "collection": [entry()] if collection is None else collection,
        }
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return StubResponse(self.payload)


# --------------------------------------------------------------------------
# DOI normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [DOI, f"  {DOI}  ", DOI.upper(), f"https://doi.org/{DOI}", f"doi:{DOI}"],
)
def test_normalise_doi_accepts_the_forms_a_human_will_paste(given):
    assert normalise_doi(given) == DOI


@pytest.mark.parametrize("given", ["19587680", "not-a-doi", "10.1101", "", "10/x"])
def test_normalise_doi_rejects_anything_that_is_not_a_doi(given):
    with pytest.raises(ValueError, match="not a DOI"):
        normalise_doi(given)


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------


def test_parse_detail_projects_the_fields_the_gold_tools_need():
    record = parse_detail([entry()], DOI)
    assert record.doi == DOI
    assert record.year == 2025
    assert record.abstract == ABSTRACT
    assert record.server == "bioRxiv"
    assert record.source == "biorxiv"
    assert record.pmid is None
    assert record.journal == "bioRxiv"


def test_parse_detail_takes_content_from_the_latest_version():
    """The DOI serves the newest revision; that is what a labeller will quote."""
    versions = [entry(version=1, abstract="v1 text"),
                entry(version=2, date="2026-04-02", abstract="v2 text")]
    record = parse_detail(versions, DOI)
    assert record.abstract == "v2 text"
    assert record.version == 2
    assert record.latest_posted == "2026-04-02"


def test_parse_detail_takes_the_year_from_the_first_posting():
    """A v2 posted eighteen months later does not make it a 2026 paper."""
    versions = [entry(version=1, date="2025-09-04"),
                entry(version=2, date="2026-04-02")]
    record = parse_detail(versions, DOI)
    assert record.posted == "2025-09-04"
    assert record.year == 2025


def test_parse_detail_raises_for_a_doi_biorxiv_does_not_have():
    """An empty collection is the API's 'no posts found'; a draft must not be
    scaffolded with an invented title."""
    with pytest.raises(BioRxivLookupError, match="no preprint with DOI"):
        parse_detail([], DOI)


def test_parse_detail_refuses_a_response_about_a_different_preprint():
    with pytest.raises(BioRxivLookupError, match="Not treating the response"):
        parse_detail([entry(doi="10.1101/2025.01.01.000000")], DOI)


def test_parse_detail_raises_on_an_empty_title():
    with pytest.raises(BioRxivLookupError, match="empty title"):
        parse_detail([entry(title="  ")], DOI)


def test_parse_detail_reports_no_abstract_as_none_rather_than_empty_string():
    assert parse_detail([entry(abstract="")], DOI).abstract is None


# --------------------------------------------------------------------------
# published-preprint detection
# --------------------------------------------------------------------------


def test_an_unpublished_preprint_reports_no_journal_doi():
    record = parse_detail([entry(published="NA")], DOI)
    assert record.published_doi is None
    assert not record.is_published


def test_a_published_preprint_reports_its_journal_doi():
    """The Gcgr case: the same work is reachable through PubMed, usually with a
    PMID and often PMC full text, and the published version is the better record."""
    record = parse_detail([entry(published="10.1007/s11357-025-01899-w")], DOI)
    assert record.published_doi == "10.1007/s11357-025-01899-w"
    assert record.is_published


def test_a_junk_published_field_is_treated_as_absence_not_as_a_doi():
    assert parse_detail([entry(published="in press")], DOI).published_doi is None


# --------------------------------------------------------------------------
# fetching and caching
# --------------------------------------------------------------------------


def test_fetch_record_requests_the_detail_endpoint_for_the_doi(tmp_path):
    client = StubClient()
    fetch_record(DOI, cache_dir=tmp_path, client=client)

    method, url, _ = client.calls[0]
    assert method == "GET"
    assert url.endswith(f"/details/biorxiv/{DOI}")


def test_fetch_record_rejects_a_pmid():
    with pytest.raises(ValueError, match="not a DOI"):
        fetch_record("19587680", client=StubClient())


def test_fetch_record_writes_then_reuses_the_cache(tmp_path):
    client = StubClient()
    first = fetch_record(DOI, cache_dir=tmp_path, client=client)
    second = fetch_record(DOI, cache_dir=tmp_path, client=client)

    assert first == second
    # The whole point of the cache: one network call per preprint, ever.
    assert len(client.calls) == 1


def test_fetch_record_refresh_bypasses_the_cache(tmp_path):
    client = StubClient()
    fetch_record(DOI, cache_dir=tmp_path, client=client)
    fetch_record(DOI, cache_dir=tmp_path, client=client, refresh=True)
    assert len(client.calls) == 2


def test_fetch_record_raises_rather_than_caching_an_unknown_doi(tmp_path):
    """'no posts found' is an answer about the DOI, but there is no record to
    cache — and a later, correct lookup must not find an empty one."""
    client = StubClient(collection=[], messages=[{"status": "no posts found"}])
    with pytest.raises(BioRxivLookupError):
        fetch_record(DOI, cache_dir=tmp_path, client=client)
    assert read_cache(DOI, cache_dir=tmp_path) is None


def test_cache_round_trips_every_field(tmp_path):
    record = BioRxivRecord(
        doi=DOI, title="T", posted="2025-09-04", latest_posted="2026-04-02",
        version=2, year=2025, abstract="A", category="physiology",
        server="bioRxiv", published_doi=None,
    )
    write_cache(record, cache_dir=tmp_path)
    assert read_cache(DOI, cache_dir=tmp_path) == record


def test_cache_file_sits_beside_the_pubmed_entries_without_colliding(tmp_path):
    """Both caches are keyed by identifier in one directory."""
    write_cache(BioRxivRecord(doi=DOI, title="T"), cache_dir=tmp_path)
    names = [p.name for p in tmp_path.iterdir()]
    assert names == ["biorxiv-10.1101_2025.08.31.673254.json"]
    assert "/" not in names[0]


def test_read_cache_returns_none_for_a_stale_version(tmp_path):
    path = write_cache(BioRxivRecord(doi=DOI, title="T"), cache_dir=tmp_path)
    payload = json.loads(path.read_text())
    payload["cache_version"] = CACHE_VERSION + 1
    path.write_text(json.dumps(payload))

    assert read_cache(DOI, cache_dir=tmp_path) is None


def test_read_cache_warns_and_misses_on_corrupt_json(tmp_path, capsys):
    biorxiv_lookup.cache_path(DOI, cache_dir=tmp_path).write_text("{not json")
    assert read_cache(DOI, cache_dir=tmp_path) is None
    assert "warning" in capsys.readouterr().err


def test_read_cache_returns_none_when_absent(tmp_path):
    assert read_cache("10.1101/9999.99.99.999999", cache_dir=tmp_path) is None
