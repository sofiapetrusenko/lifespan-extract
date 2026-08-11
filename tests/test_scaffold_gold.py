"""Tests for scripts/scaffold_gold.py. Both fetches are stubbed out."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
import scaffold_gold
from scaffold_gold import (
    DRAFT_ID_SUFFIX,
    DRAFT_NOTE_PREFIX,
    build_skeleton,
    main,
    read_schema_version,
    validation_errors,
)


@dataclass
class FakeRecord:
    """Stands in for a `pubmed_lookup.PubMedRecord`."""

    pmid: str = "19587680"
    title: str = "Rapamycin fed late in life extends lifespan"
    journal: str | None = "Nature"
    year: int | None = 2009
    doi: str | None = "10.1038/nature08221"
    abstract: str | None = "Rapamycin extends median and maximal lifespan of mice."

    @property
    def source(self) -> str:
        return "pubmed"


@dataclass
class FakePreprint:
    """Stands in for a `biorxiv_lookup.BioRxivRecord`.

    Same attribute surface as FakeRecord plus the preprint-only fields, which is
    the property `build_skeleton` relies on to read either without a branch.
    """

    doi: str = "10.1101/2025.08.31.673254"
    title: str = "Lifelong restriction of dietary valine has sex-specific benefits"
    year: int | None = 2025
    abstract: str | None = "Val-R extends the lifespan of male, but not female, mice by 23%."
    posted: str | None = "2025-09-04"
    latest_posted: str | None = "2026-04-02"
    version: int | None = 2
    server: str | None = "bioRxiv"
    published_doi: str | None = None

    @property
    def source(self) -> str:
        return "biorxiv"

    @property
    def pmid(self) -> None:
        return None

    @property
    def journal(self) -> str | None:
        return self.server

    @property
    def is_published(self) -> bool:
        return self.published_doi is not None


@pytest.fixture
def schema_version() -> str:
    return read_schema_version()


# --------------------------------------------------------------------------
# skeleton shape
# --------------------------------------------------------------------------


def test_skeleton_is_schema_valid_once_private_keys_are_stripped(schema_version):
    """The draft's contract: valid against the schema with `_` keys removed."""
    document = build_skeleton(FakeRecord(), "harrison2009", schema_version)
    assert validation_errors(document) == []


def test_skeleton_prefills_only_metadata(schema_version):
    document = build_skeleton(FakeRecord(), "harrison2009", schema_version)
    assert document["paper"] == {
        "doi": "10.1038/nature08221",
        "title": "Rapamycin fed late in life extends lifespan",
        "year": 2009,
        "source": "pubmed",
        "pmid": "19587680",
    }
    assert document["schema_version"] == schema_version


def test_skeleton_embeds_the_abstract_and_journal_under_private_keys(schema_version):
    document = build_skeleton(FakeRecord(), "harrison2009", schema_version)
    assert document["_abstract"] == "Rapamycin extends median and maximal lifespan of mice."
    assert document["_journal"] == "Nature"
    # Underscored so the schema's `additionalProperties: false` root is
    # satisfied once check_gold.py strips them.
    assert all(key.startswith("_") or key in {"schema_version", "paper", "experiments"} for key in document)


def test_skeleton_extracts_nothing(schema_version):
    """Every value is a placeholder — the gold set must not contain machine output."""
    experiment = build_skeleton(FakeRecord(), "harrison2009", schema_version)["experiments"][0]

    assert experiment["intervention"]["agent"]["value"] == "not_reported"
    assert experiment["strain"]["value"] == "not_reported"
    assert experiment["sex"]["value"] == "not_reported"
    assert experiment["sample_size"]["value"] is None
    assert experiment["mechanism"]["value"] is None
    assert experiment["lifespan_effect"]["median_change_pct"]["value"] is None
    assert experiment["lifespan_effect"]["p_value"]["value"] is None
    # No quote is invented for a value nobody has read.
    assert experiment["organism"]["source_quote"] is None
    assert experiment["organism"]["confidence"] == "low"


def test_skeleton_carries_both_draft_markers(schema_version):
    """These are what stop an unlabelled draft reaching the gold set silently."""
    experiment = build_skeleton(FakeRecord(), "harrison2009", schema_version)["experiments"][0]
    assert experiment["experiment_id"] == f"harrison2009{DRAFT_ID_SUFFIX}"
    assert experiment["notes"].startswith(DRAFT_NOTE_PREFIX)


def test_skeleton_omits_a_missing_doi_rather_than_faking_one(schema_version):
    document = build_skeleton(FakeRecord(doi=None), "preprint2024", schema_version)
    assert "doi" not in document["paper"]
    # And the omission surfaces as a validation error naming the field, rather
    # than as a plausible wrong value nobody rechecks.
    assert any("doi" in error for error in validation_errors(document))


def test_skeleton_omits_a_missing_year_rather_than_guessing(schema_version):
    document = build_skeleton(FakeRecord(year=None), "preprint2024", schema_version)
    assert "year" not in document["paper"]
    assert any("year" in error for error in validation_errors(document))


def test_skeleton_handles_a_paper_with_no_abstract(schema_version):
    document = build_skeleton(FakeRecord(abstract=None), "noabstract2020", schema_version)
    assert document["_abstract"] is None
    assert validation_errors(document) == []


def test_read_schema_version_matches_the_schema_file():
    """Read, never hardcoded: a stale literal would misreport the version."""
    schema = json.loads(scaffold_gold.SCHEMA_PATH.read_text())
    assert read_schema_version() == schema["x-schema-version"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@pytest.fixture
def drafts(tmp_path, monkeypatch):
    """Redirect writes to a temporary drafts directory."""
    destination = tmp_path / "drafts"
    monkeypatch.setattr(scaffold_gold, "DRAFTS_DIR", destination)
    monkeypatch.setattr(scaffold_gold, "REPO_ROOT", tmp_path)
    return destination


class StubFetch:
    """Stands in for pubmed_lookup.fetch_record. Records how it was called."""

    def __init__(self, record: FakeRecord | None = None):
        self.record = record
        self.calls: list[dict] = []

    def __call__(self, pmid, *, refresh=False):
        self.calls.append({"pmid": pmid, "refresh": refresh})
        return self.record or FakeRecord(pmid=pmid)


@pytest.fixture
def stub_fetch() -> StubFetch:
    return StubFetch()


def test_main_writes_a_draft(drafts, stub_fetch, capsys):
    assert main(["19587680", "harrison2009"], fetch=stub_fetch) == 0

    written = json.loads((drafts / "harrison2009.json").read_text())
    assert written["paper"]["pmid"] == "19587680"
    assert written["experiments"][0]["experiment_id"].endswith(DRAFT_ID_SUFFIX)
    assert "placeholder" in capsys.readouterr().out


def test_main_never_writes_into_the_gold_directory(drafts, stub_fetch):
    """data/gold/ is human-labelled ground truth; this tool has no path into it."""
    main(["19587680", "harrison2009"], fetch=stub_fetch)
    assert (drafts / "harrison2009.json").exists()
    assert "gold" not in str(scaffold_gold.DRAFTS_DIR)


def test_main_refuses_to_clobber_an_existing_draft(drafts, stub_fetch):
    drafts.mkdir(parents=True)
    (drafts / "harrison2009.json").write_text('{"hand": "edited"}')

    with pytest.raises(SystemExit, match="already exists"):
        main(["19587680", "harrison2009"], fetch=stub_fetch)

    # The hand edit survived, and no request was made to find that out.
    assert json.loads((drafts / "harrison2009.json").read_text()) == {"hand": "edited"}
    assert stub_fetch.calls == []


def test_main_overwrites_with_force(drafts, stub_fetch):
    drafts.mkdir(parents=True)
    (drafts / "harrison2009.json").write_text('{"hand": "edited"}')

    assert main(["19587680", "harrison2009", "--force"], fetch=stub_fetch) == 0
    assert json.loads((drafts / "harrison2009.json").read_text())["paper"]["pmid"] == "19587680"


def test_main_passes_refresh_through_to_the_fetcher(drafts, stub_fetch):
    main(["19587680", "harrison2009", "--refresh"], fetch=stub_fetch)
    assert stub_fetch.calls[0]["refresh"] is True


@pytest.mark.parametrize("slug", ["Harrison2009", "harrison_2009", "../escape", "harrison 2009", ""])
def test_main_rejects_a_slug_that_would_break_experiment_id_or_the_path(slug, drafts, stub_fetch):
    with pytest.raises(SystemExit, match="slug"):
        main(["19587680", slug], fetch=stub_fetch)
    assert stub_fetch.calls == []


def test_main_reports_when_the_draft_is_not_yet_valid(drafts, capsys):
    fetch = StubFetch(FakeRecord(doi=None))
    main(["19587680", "nodoi2020"], fetch=fetch)
    assert "NOT yet schema-valid" in capsys.readouterr().out


# --------------------------------------------------------------------------
# bioRxiv preprints: a DOI instead of a PMID
# --------------------------------------------------------------------------


def test_preprint_skeleton_is_schema_valid(schema_version):
    """The schema has always allowed this: paper.required is doi/title/year/source."""
    document = build_skeleton(FakePreprint(), "green2025", schema_version)
    assert validation_errors(document) == []


def test_preprint_skeleton_carries_biorxiv_source_and_null_pmid(schema_version):
    document = build_skeleton(FakePreprint(), "green2025", schema_version)
    assert document["paper"] == {
        "doi": "10.1101/2025.08.31.673254",
        "title": "Lifelong restriction of dietary valine has sex-specific benefits",
        "year": 2025,
        "source": "biorxiv",
        "pmid": None,
    }


def test_preprint_embeds_the_abstract_exactly_as_the_pubmed_path_does(schema_version):
    document = build_skeleton(FakePreprint(), "green2025", schema_version)
    assert document["_abstract"] == FakePreprint().abstract
    assert document["_journal"] == "bioRxiv"


def test_pubmed_path_is_unchanged_by_the_preprint_support(schema_version):
    """Regression guard: PMIDs must keep working exactly as before."""
    document = build_skeleton(FakeRecord(), "harrison2009", schema_version)
    assert document["paper"]["source"] == "pubmed"
    assert document["paper"]["pmid"] == "19587680"
    assert validation_errors(document) == []


def test_lookup_dispatch_picks_the_client_from_the_identifier_shape():
    pubmed = scaffold_gold._lookup_for("19587680")
    biorxiv = scaffold_gold._lookup_for("10.1101/2025.08.31.673254")
    assert pubmed.__module__ == "pubmed_lookup"
    assert biorxiv.__module__ == "biorxiv_lookup"


def test_lookup_dispatch_refuses_an_identifier_that_is_neither():
    """Defaulting to PubMed would report 'PMID not found' for a typo'd DOI."""
    with pytest.raises(SystemExit, match="neither a PMID"):
        scaffold_gold._lookup_for("not-an-identifier")


def test_main_writes_a_preprint_draft(drafts, capsys):
    assert main(["10.1101/2025.08.31.673254", "green2025"],
                fetch=lambda identifier, refresh=False: FakePreprint()) == 0

    written = json.loads((drafts / "green2025.json").read_text())
    assert written["paper"]["source"] == "biorxiv"
    assert written["paper"]["pmid"] is None
    out = capsys.readouterr().out
    assert "source   biorxiv" in out
    assert "(none — preprint)" in out
    assert "posted   2025-09-04, latest v2 2026-04-02" in out


def test_main_warns_loudly_when_the_preprint_has_been_published(drafts, capsys):
    """The Gcgr trap: a published preprint is the same work with a PMID and
    often PMC full text, and the difference is invisible once the draft exists."""
    record = FakePreprint(published_doi="10.1007/s11357-025-01899-w")
    main(["10.1101/2025.05.13.653849", "stern2025"], fetch=lambda i, refresh=False: record)

    out = capsys.readouterr().out
    assert "PUBLISHED" in out
    assert "10.1007/s11357-025-01899-w" in out
