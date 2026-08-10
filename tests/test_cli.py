"""End-to-end behaviour of `python -m ingest`, with both clients stubbed.

The fetchers are replaced at their module attribute rather than through a
test-only parameter on `main`, so the production call path is exercised
unchanged.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from ingest import biorxiv, cli, pubmed
from ingest.db import DATABASE_URL_ENV, make_engine
from ingest.errors import TransportError
from ingest.models import SOURCE_BIORXIV, SOURCE_PUBMED, RawPaper

DOI = "10.1038/nature08221"


def publication(
    pmid="19587680", doi=DOI, abstract="Median survival increased."
) -> RawPaper:
    return RawPaper.build(
        source=SOURCE_PUBMED,
        source_id=pmid,
        pmid=pmid,
        doi=doi,
        title="Rapamycin fed late in life extends lifespan",
        abstract=abstract,
    )


def preprint(source_id="10.1101/2026.01.01.000001", doi=None, **overrides) -> RawPaper:
    return RawPaper.build(
        source=SOURCE_BIORXIV,
        source_id=source_id,
        doi=doi or source_id,
        title="Autophagy extends lifespan (preprint)",
        abstract="Spermidine increased median survival.",
        **overrides,
    )


@pytest.fixture
def sources(monkeypatch):
    """Replace both fetchers; the returned dict records the calls they saw."""
    calls: dict[str, dict] = {}
    results: dict[str, list[RawPaper]] = {"pubmed": [], "biorxiv": []}

    def fake_pubmed(query, limit, **kwargs):
        calls["pubmed"] = {"query": query, "limit": limit, **kwargs}
        return list(results["pubmed"])

    def fake_biorxiv(query, limit, **kwargs):
        calls["biorxiv"] = {"query": query, "limit": limit, **kwargs}
        return list(results["biorxiv"])

    monkeypatch.setattr(pubmed, "fetch_abstracts", fake_pubmed)
    monkeypatch.setattr(biorxiv, "fetch_abstracts", fake_biorxiv)
    return {"calls": calls, "results": results}


@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    """Point DATABASE_URL at a throwaway file so two runs share one database."""
    monkeypatch.setenv(DATABASE_URL_ENV, f"sqlite:///{tmp_path / 'ingest.db'}")


def rows(url) -> list[RawPaper]:
    with Session(make_engine(url)) as session:
        return list(session.exec(select(RawPaper)))


def test_documented_invocation_stores_papers(sources, sqlite_env, capsys, tmp_path):
    sources["results"]["pubmed"] = [
        publication(pmid=str(i), doi=f"10.1/{i}") for i in range(100)
    ]
    sources["results"]["biorxiv"] = [preprint()]

    exit_code = cli.main(["--query", "autophagy lifespan", "--limit", "100"])

    assert exit_code == 0
    assert len(rows(f"sqlite:///{tmp_path / 'ingest.db'}")) == 101
    out = capsys.readouterr().out
    assert "pubmed:   100 record(s)" in out
    assert "101 new, 0 already present" in out


def test_rerunning_the_command_inserts_nothing(sources, sqlite_env, capsys, tmp_path):
    sources["results"]["pubmed"] = [publication()]
    argv = ["--query", "rapamycin", "--limit", "10"]

    assert cli.main(argv) == 0
    capsys.readouterr()
    assert cli.main(argv) == 0

    assert "0 new, 1 already present" in capsys.readouterr().out
    assert len(rows(f"sqlite:///{tmp_path / 'ingest.db'}")) == 1


def test_preprint_and_publication_are_deduped_before_storage(
    sources, sqlite_env, capsys, tmp_path
):
    sources["results"]["pubmed"] = [publication()]
    sources["results"]["biorxiv"] = [preprint(doi=DOI)]

    assert cli.main(["--query", "rapamycin", "--limit", "10"]) == 0

    out = capsys.readouterr().out
    assert "2 fetched -> 1 unique (1 duplicate(s) collapsed by DOI)" in out
    kept = rows(f"sqlite:///{tmp_path / 'ingest.db'}")
    assert [p.source for p in kept] == [SOURCE_PUBMED]


def test_a_preprint_ingested_before_publication_leaves_two_rows(
    sources, sqlite_env, capsys, tmp_path
):
    """Pins the cross-run gap recorded in NOTES.md "Known limitations".

    PLAN.md's dedup case has two shapes. The same-run one works and is covered
    above. This is the other, and it is driven one source at a time so the
    mechanism under test is the only one that can fire:

    Run one — bioRxiv reports no publication, so the preprint is keyed under its
    own `10.1101/...` DOI. Run two — bioRxiv now reports the journal DOI, and
    the record it rebuilds reaches `store_papers` alone. There it collides with
    the day-one row on `(source, source_id)` and `ON CONFLICT DO NOTHING` drops
    it; the "0 new, 1 already present" line below is what proves the database,
    not `ingest.dedup`, is what dropped it. Run three — the PubMed twin inserts
    under the journal DOI, and the work is now two rows, the bioRxiv one still
    carrying a DOI that is no longer canonical.

    The property that makes re-ingest safe is exactly what stops the key being
    corrected. A reconciliation path inside `store_papers` would have to see the
    day-two record, so it would break the run-two assertion here rather than
    quietly outdating the NOTES entry.
    """
    database = f"sqlite:///{tmp_path / 'ingest.db'}"
    argv = ["--query", "rapamycin", "--limit", "10"]

    # Run one: bioRxiv knows of no publication, so the preprint DOI is its key.
    sources["results"]["biorxiv"] = [preprint()]
    assert cli.main(argv) == 0
    assert "1 new, 0 already present" in capsys.readouterr().out

    # Run two: bioRxiv reports the journal DOI. No PubMed record this run, so
    # the rebuilt record is stored, not collapsed in Python beforehand.
    sources["results"]["biorxiv"] = [preprint(doi=DOI)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "1 fetched -> 1 unique (0 duplicate(s) collapsed by DOI)" in out
    assert "0 new, 1 already present" in out

    # Run three: the journal version is indexed and PubMed returns it.
    sources["results"]["biorxiv"] = []
    sources["results"]["pubmed"] = [publication()]
    assert cli.main(argv) == 0
    assert "1 new, 0 already present" in capsys.readouterr().out

    kept = sorted(rows(database), key=lambda row: row.source)
    assert [(row.source, row.dedup_key, row.doi) for row in kept] == [
        (SOURCE_BIORXIV, "doi:10.1101/2026.01.01.000001", "10.1101/2026.01.01.000001"),
        (SOURCE_PUBMED, f"doi:{DOI}", DOI),
    ]


def test_a_later_combined_run_drops_the_published_preprint_in_python(
    sources, sqlite_env, capsys, tmp_path
):
    """The other run shape of the same limitation, and a different mechanism.

    When the published preprint and its PubMed twin arrive in the *same* later
    run, `ingest.dedup` collapses them on the shared journal DOI before storage
    and PubMed wins on source priority — so the bioRxiv record never reaches
    `store_papers` at all, and the `(source, source_id)` conflict pinned by the
    test above never fires. The outcome is the same two rows, by another route;
    both are recorded in NOTES.md because a fix would have to address each.
    """
    database = f"sqlite:///{tmp_path / 'ingest.db'}"
    argv = ["--query", "rapamycin", "--limit", "10"]

    sources["results"]["biorxiv"] = [preprint()]
    assert cli.main(argv) == 0
    capsys.readouterr()

    sources["results"]["biorxiv"] = [preprint(doi=DOI)]
    sources["results"]["pubmed"] = [publication()]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "2 fetched -> 1 unique (1 duplicate(s) collapsed by DOI)" in out
    # One record stored, and it inserted: nothing was offered to the database
    # that it had to refuse.
    assert "1 new, 0 already present" in out

    kept = sorted(rows(database), key=lambda row: row.source)
    assert [(row.source, row.dedup_key, row.doi) for row in kept] == [
        (SOURCE_BIORXIV, "doi:10.1101/2026.01.01.000001", "10.1101/2026.01.01.000001"),
        (SOURCE_PUBMED, f"doi:{DOI}", DOI),
    ]


def test_limit_is_passed_to_each_source(sources, sqlite_env):
    cli.main(["--query", "metformin", "--limit", "25"])
    assert sources["calls"]["pubmed"] == {"query": "metformin", "limit": 25}
    assert sources["calls"]["biorxiv"]["limit"] == 25
    assert sources["calls"]["biorxiv"]["window_days"] == biorxiv.DEFAULT_WINDOW_DAYS


def test_biorxiv_window_is_configurable(sources, sqlite_env):
    cli.main(["--query", "metformin", "--biorxiv-window-days", "180"])
    assert sources["calls"]["biorxiv"]["window_days"] == 180


def test_default_limit_matches_the_documented_contract(sources, sqlite_env):
    cli.main(["--query", "metformin"])
    assert sources["calls"]["pubmed"]["limit"] == cli.DEFAULT_LIMIT == 100


def test_missing_abstracts_are_reported_not_hidden(sources, sqlite_env, capsys):
    sources["results"]["pubmed"] = [publication(abstract=None)]
    cli.main(["--query", "rapamycin"])
    out = capsys.readouterr().out
    assert "1 of 1 record(s) have no abstract" in out


def test_no_matches_says_so(sources, sqlite_env, capsys):
    assert cli.main(["--query", "unobtainium lifespan"]) == 0
    assert "nothing matched" in capsys.readouterr().out


def test_missing_database_url_fails_before_any_fetch(monkeypatch, sources, capsys):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    assert cli.main(["--query", "rapamycin"]) == 1
    assert sources["calls"] == {}
    assert DATABASE_URL_ENV in capsys.readouterr().err


def test_ingest_errors_exit_nonzero_with_an_actionable_message(
    monkeypatch, sources, sqlite_env, capsys
):
    def boom(query, limit, **kwargs):
        raise TransportError("giving up on https://eutils.example after 5 attempt(s)")

    monkeypatch.setattr(pubmed, "fetch_abstracts", boom)
    assert cli.main(["--query", "rapamycin"]) == 1
    err = capsys.readouterr().err
    assert "TransportError" in err
    assert "giving up on" in err


def test_bad_query_for_biorxiv_exits_with_the_argument_status(
    monkeypatch, sources, sqlite_env, capsys
):
    def reject(query, limit, **kwargs):
        raise ValueError("bioRxiv cannot run that query: no query language")

    monkeypatch.setattr(biorxiv, "fetch_abstracts", reject)
    assert cli.main(["--query", "a AND b"]) == 2
    assert "invalid argument" in capsys.readouterr().err


def test_nonpositive_limit_is_rejected_by_the_parser(sources, sqlite_env):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--query", "rapamycin", "--limit", "0"])
    assert excinfo.value.code == 2


def test_query_is_required(sources, sqlite_env):
    with pytest.raises(SystemExit):
        cli.main([])


def test_database_password_is_not_printed(sources, capsys):
    """The db line names the database that was written to, never its password.

    Driven through the line that actually prints the URL, with no socket
    anywhere: both sources return nothing, so `store_papers` returns before it
    opens a session, and `create_engine` does not connect on construction. A
    variant of this test that lets the run die on connect would assert the
    property against an exception path that never reaches the print at all.
    """
    engine = make_engine("postgresql+psycopg://user:hunter2@localhost:5432/lifespan")

    assert cli.run_ingest(engine, query="rapamycin", limit=10) == 0

    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err
    assert "localhost:5432/lifespan" in captured.out
