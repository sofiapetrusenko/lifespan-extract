"""Dedup key derivation and the preprint/publication preference policy."""

from __future__ import annotations

import pytest

from ingest.dedup import dedup
from ingest.models import (
    SOURCE_BIORXIV,
    SOURCE_PUBMED,
    RawPaper,
    compute_dedup_key,
    normalise_doi,
)

DOI = "10.1038/nature08221"


def pubmed_paper(pmid="19587680", doi=DOI, **overrides) -> RawPaper:
    return RawPaper.build(
        source=SOURCE_PUBMED,
        source_id=pmid,
        pmid=pmid,
        doi=doi,
        title="Rapamycin fed late in life extends lifespan",
        abstract="Median survival increased.",
        **overrides,
    )


def biorxiv_paper(preprint="10.1101/2026.01.01.000001", doi=None, **overrides) -> RawPaper:
    return RawPaper.build(
        source=SOURCE_BIORXIV,
        source_id=preprint,
        doi=doi or preprint,
        title="Rapamycin fed late in life extends lifespan (preprint)",
        abstract="Median survival increased.",
        **overrides,
    )


def test_doi_is_the_key_when_present():
    assert compute_dedup_key(SOURCE_PUBMED, "1", DOI) == f"doi:{DOI}"


def test_source_id_is_the_key_when_the_doi_is_absent():
    assert compute_dedup_key(SOURCE_PUBMED, "19587680", None) == "pubmed:19587680"


def test_dois_are_normalised_so_case_cannot_split_a_paper():
    assert normalise_doi("  10.1038/NATURE08221  ") == DOI
    assert normalise_doi("NA") is None
    assert normalise_doi("") is None
    assert normalise_doi(None) is None


def test_publication_wins_over_its_preprint():
    """Same DOI from both sources collapses to the peer-reviewed record."""
    preprint = biorxiv_paper(doi=DOI)
    publication = pubmed_paper()
    assert preprint.dedup_key == publication.dedup_key

    result = dedup([preprint, publication])
    assert result.dropped == 1
    assert [p.source for p in result.papers] == [SOURCE_PUBMED]


def test_preference_is_independent_of_input_order():
    result = dedup([pubmed_paper(), biorxiv_paper(doi=DOI)])
    assert [p.source for p in result.papers] == [SOURCE_PUBMED]


def test_unpublished_preprint_keeps_its_own_identity():
    result = dedup([pubmed_paper(), biorxiv_paper()])
    assert result.dropped == 0
    assert len(result.papers) == 2


def test_records_without_dois_stay_distinct():
    """Two DOI-less papers must not collapse onto a shared sentinel key."""
    first = pubmed_paper(pmid="111", doi=None)
    second = pubmed_paper(pmid="222", doi=None)
    assert first.dedup_key != second.dedup_key
    assert dedup([first, second]).dropped == 0


def test_repeated_identical_record_is_collapsed():
    result = dedup([pubmed_paper(), pubmed_paper()])
    assert result.dropped == 1
    assert len(result.papers) == 1


def test_first_seen_order_is_preserved():
    papers = [
        pubmed_paper(pmid="1", doi="10.1/a"),
        pubmed_paper(pmid="2", doi="10.1/b"),
        pubmed_paper(pmid="1", doi="10.1/a"),
        pubmed_paper(pmid="3", doi="10.1/c"),
    ]
    assert [p.doi for p in dedup(papers).papers] == ["10.1/a", "10.1/b", "10.1/c"]


def test_empty_input_is_not_an_error():
    assert dedup([]) == ([], 0)


def test_build_rejects_an_unknown_source():
    with pytest.raises(ValueError, match="source must be one of"):
        RawPaper.build(source="arxiv", source_id="1", title="t")


def test_build_rejects_an_empty_title():
    with pytest.raises(ValueError, match="empty title"):
        RawPaper.build(source=SOURCE_PUBMED, source_id="1", title="  ")


def test_dedup_key_requires_a_source_id():
    with pytest.raises(ValueError, match="source_id is required"):
        compute_dedup_key(SOURCE_PUBMED, "", None)
