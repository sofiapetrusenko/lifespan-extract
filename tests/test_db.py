"""Persistence: required configuration, and database-enforced idempotence."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from ingest.db import (
    DATABASE_URL_ENV,
    _check_dialect,
    database_url,
    make_engine,
    store_papers,
)
from ingest.errors import ConfigurationError
from ingest.models import SOURCE_BIORXIV, SOURCE_PUBMED, RawPaper

DOI = "10.1038/nature08221"


def paper(pmid="19587680", doi=DOI, title="Rapamycin", **overrides) -> RawPaper:
    return RawPaper.build(
        source=SOURCE_PUBMED, source_id=pmid, pmid=pmid, doi=doi, title=title, **overrides
    )


def stored(engine) -> list[RawPaper]:
    with Session(engine) as session:
        return list(session.exec(select(RawPaper)))


def test_missing_database_url_raises(monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    with pytest.raises(ConfigurationError, match=DATABASE_URL_ENV):
        database_url()


def test_blank_database_url_raises(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "   ")
    with pytest.raises(ConfigurationError, match=DATABASE_URL_ENV):
        database_url()


def test_make_engine_reads_the_environment(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite://")
    assert make_engine().dialect.name == "sqlite"


def test_bare_postgres_scheme_is_rejected_with_the_fix(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@localhost/lifespan")
    with pytest.raises(ConfigurationError, match=r"postgresql\+psycopg://"):
        make_engine()


def test_unsupported_dialect_raises_rather_than_losing_idempotence(monkeypatch):
    """No ON CONFLICT means no idempotence guarantee, so the URL is refused."""
    monkeypatch.setenv(DATABASE_URL_ENV, "mysql+pymysql://user@localhost/lifespan")
    with pytest.raises(ConfigurationError, match="unsupported database dialect"):
        make_engine()


def test_store_papers_refuses_a_foreign_engine():
    """The same guard protects `store_papers` when it is handed an engine directly."""
    with pytest.raises(ConfigurationError, match="unsupported database dialect"):
        _check_dialect("mysql")


def test_unparseable_database_url_raises(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "not a url at all")
    with pytest.raises(ConfigurationError, match="not a usable database URL"):
        make_engine()


def test_papers_are_stored(engine):
    result = store_papers(engine, [paper(), paper(pmid="2", doi="10.1/b")])
    assert result == (2, 0)
    assert len(stored(engine)) == 2


def test_rerunning_the_same_ingest_inserts_nothing(engine):
    batch = [paper(), paper(pmid="2", doi="10.1/b")]
    assert store_papers(engine, batch) == (2, 0)
    assert store_papers(engine, batch) == (0, 2)
    assert len(stored(engine)) == 2


def test_existing_rows_are_never_overwritten(engine):
    store_papers(engine, [paper(title="Original title")])
    store_papers(engine, [paper(title="Rewritten by a later run")])
    assert [p.title for p in stored(engine)] == ["Original title"]


def test_doi_appearing_later_does_not_create_a_second_row(engine):
    """A PubMed record ingested before its DOI was registered, then again after.

    The DOI changes the dedup key, so only the (source, source_id) constraint
    can catch this. Without it the same paper would be stored twice.
    """
    store_papers(engine, [paper(doi=None)])
    result = store_papers(engine, [paper(doi=DOI)])
    assert result == (0, 1)
    rows = stored(engine)
    assert len(rows) == 1
    assert rows[0].dedup_key == "pubmed:19587680"


def test_preprint_and_publication_collapse_to_one_row(engine):
    publication = paper()
    preprint = RawPaper.build(
        source=SOURCE_BIORXIV,
        source_id="10.1101/2026.01.01.000001",
        doi=DOI,
        title="Rapamycin (preprint)",
    )
    assert store_papers(engine, [publication]) == (1, 0)
    assert store_papers(engine, [preprint]) == (0, 1)
    assert len(stored(engine)) == 1


def test_doi_less_records_are_stored_separately(engine):
    assert store_papers(engine, [paper(pmid="1", doi=None), paper(pmid="2", doi=None)]) == (2, 0)
    assert {p.dedup_key for p in stored(engine)} == {"pubmed:1", "pubmed:2"}


def test_duplicates_within_one_batch_are_collapsed(engine):
    assert store_papers(engine, [paper(), paper()]) == (1, 0)
    assert len(stored(engine)) == 1


def test_empty_batch_is_a_no_op(engine):
    assert store_papers(engine, []) == (0, 0)


def test_batches_larger_than_one_chunk_are_stored(engine, monkeypatch):
    monkeypatch.setattr("ingest.db.INSERT_CHUNK", 7)
    batch = [paper(pmid=str(i), doi=f"10.1/{i}") for i in range(20)]
    assert store_papers(engine, batch) == (20, 0)
    assert len(stored(engine)) == 20
