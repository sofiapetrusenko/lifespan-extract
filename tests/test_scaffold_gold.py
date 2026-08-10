"""Tests for scripts/scaffold_gold.py. The PubMed fetch is stubbed out."""

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
    pmid: str = "19587680"
    title: str = "Rapamycin fed late in life extends lifespan"
    journal: str | None = "Nature"
    year: int | None = 2009
    doi: str | None = "10.1038/nature08221"
    abstract: str | None = "Rapamycin extends median and maximal lifespan of mice."


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
