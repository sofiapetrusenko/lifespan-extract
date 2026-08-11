"""Tests for scripts/validate_classifier_set.py. No network: --resolve is not exercised."""

from __future__ import annotations

import copy
import json

import pytest
import validate_classifier_set as vcs
from validate_classifier_set import check, load, main


def document(**overrides) -> dict:
    body = {
        "set_version": "0.1.0",
        "label": "negative",
        "description": "Hard negatives for the Phase 3 classifier eval.",
        "categories": [
            {"name": "aging-no-lifespan", "description": "No lifespan measured."},
            {"name": "wrong-organism", "description": "Lifespan means something else."},
        ],
        "entries": [
            {"pmid": "38381284", "doi": None, "title": "Epigenetic clock in the aorta",
             "source": "pubmed", "category": "aging-no-lifespan",
             "reason": "No lifespan measured and nothing administered.", "reviewed": False},
            {"pmid": "36721872", "doi": None, "title": "FHY3 increases leaf longevity",
             "source": "pubmed", "category": "wrong-organism",
             "reason": "The entity whose longevity is extended is a leaf.", "reviewed": True},
        ],
    }
    body.update(overrides)
    return body


def entry(**overrides) -> dict:
    base = dict(document()["entries"][0])
    base.update(overrides)
    return base


def with_entry(**overrides) -> dict:
    body = document()
    body["entries"] = [entry(**overrides)]
    return body


# --------------------------------------------------------------------------
# the happy path, and the real file
# --------------------------------------------------------------------------


def test_a_well_formed_document_has_no_problems():
    assert check(document()) == []


def test_the_committed_negative_set_validates():
    """The file in the repo is the thing this script exists to check."""
    problems = [p for p in check(load()) if not p.startswith("NOTE:")]
    assert problems == []


def test_the_committed_set_spreads_across_every_declared_category():
    """A category with no entry means an untested failure mode."""
    doc = load()
    used = {e["category"] for e in doc["entries"]}
    assert used == {c["name"] for c in doc["categories"]}


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_a_non_object_top_level_is_rejected():
    assert check([]) == ["top level is list, expected an object"]


def test_missing_top_level_keys_are_reported_and_stop_the_run():
    body = document()
    del body["entries"]
    problems = check(body)
    assert any("missing top-level key(s): entries" in p for p in problems)


def test_an_unexpected_top_level_key_is_an_error_not_ignored_data():
    problems = check(document(notes="stray"))
    assert any("unexpected top-level key(s): notes" in p for p in problems)


def test_the_label_must_say_negative():
    """This file holds one class; a positive here would silently invert a score."""
    problems = check(document(label="positive"))
    assert any("this file holds negatives only" in p for p in problems)


def test_empty_entries_are_rejected():
    assert any("non-empty" in p for p in check(document(entries=[])))


def test_an_unexpected_entry_key_is_reported():
    problems = check(with_entry(note="stray"))
    assert any("unexpected key(s): note" in p for p in problems)


def test_a_missing_entry_key_is_reported():
    body = document()
    del body["entries"][0]["reason"]
    assert any("missing key(s): reason" in p for p in check(body))


# --------------------------------------------------------------------------
# identity and uniqueness
# --------------------------------------------------------------------------


def test_a_duplicate_pmid_is_rejected():
    """A duplicate silently doubles that paper's weight in precision and recall."""
    body = document()
    body["entries"] = [entry(), entry()]
    assert any("duplicate of entries[0]" in p for p in check(body))


def test_a_duplicate_doi_is_rejected():
    body = document()
    preprint = entry(pmid=None, doi="10.1101/2025.08.31.673254", source="biorxiv")
    body["entries"] = [preprint, dict(preprint)]
    assert any("duplicate of entries[0]" in p for p in check(body))


def test_the_same_paper_under_pmid_and_doi_is_not_detected():
    """Known limit, pinned so it is a decision rather than a surprise: identity is
    whichever key the entry uses, so one paper listed once by PMID and once by
    DOI passes. Nothing in the file records the mapping between the two."""
    body = document()
    body["entries"] = [entry(), entry(pmid=None, doi="10.1000/x", source="pubmed")]
    assert not any("duplicate" in p for p in check(body))


def test_an_entry_with_neither_identifier_is_rejected():
    assert any("both are null" in p for p in check(with_entry(pmid=None, doi=None)))


def test_a_preprint_may_be_identified_by_doi_alone():
    body = with_entry(pmid=None, doi="10.1101/2025.08.31.673254", source="biorxiv")
    assert main_would_pass(body)


@pytest.mark.parametrize("pmid", ["PMC12345", "abc", "", "123x"])
def test_a_malformed_pmid_is_rejected(pmid):
    assert any("is not digits-only" in p for p in check(with_entry(pmid=pmid)))


@pytest.mark.parametrize("doi", ["not-a-doi", "10.1101", "doi:10.1/x"])
def test_a_malformed_doi_is_rejected(doi):
    assert any("is not a DOI" in p for p in check(with_entry(doi=doi)))


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------


def test_an_undeclared_category_is_rejected():
    """The five categories are the contract; a sixth invented in place would
    silently create a failure mode nobody chose to test."""
    problems = check(with_entry(category="made-up"))
    assert any("not a declared category" in p for p in problems)


def test_an_unknown_source_is_rejected():
    assert any("source 'arxiv'" in p for p in check(with_entry(source="arxiv")))


def test_a_duplicate_category_name_is_rejected():
    body = document()
    body["categories"].append({"name": "wrong-organism", "description": "again"})
    assert any("duplicate category name" in p for p in check(body))


def test_a_category_with_no_description_is_rejected():
    body = document()
    body["categories"][0]["description"] = "  "
    assert any("description is empty" in p for p in check(body))


def test_a_category_with_no_entries_is_a_note_not_a_failure():
    """A gap to fill, reported rather than left unnoticed."""
    body = document()
    body["entries"] = [entry()]
    problems = check(body)
    assert any(p.startswith("NOTE: category with no entries") for p in problems)
    assert main_would_pass(body)


def main_would_pass(body) -> bool:
    return not [p for p in check(body) if not p.startswith("NOTE:")]


# --------------------------------------------------------------------------
# the reason and the review gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_an_entry_with_no_reason_is_rejected(reason):
    """Every negative states why it is one; an unexplained entry is unreviewable."""
    problems = check(with_entry(reason=reason))
    assert any("every negative states why it is one" in p for p in problems)


def test_reviewed_must_be_a_boolean():
    assert any("must be true or false" in p for p in check(with_entry(reviewed="yes")))


def test_unreviewed_entries_are_reported_loudly_but_do_not_fail(tmp_path, monkeypatch, capsys):
    """An entry counts only once a human has agreed; until then the file is a
    proposal, and the count is the honest way to say so."""
    path = tmp_path / "negatives.json"
    path.write_text(json.dumps(document()))
    monkeypatch.setattr(vcs, "SET_PATH", path)
    monkeypatch.setattr(vcs, "REPO_ROOT", tmp_path)

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "1 entry/entries not yet human-reviewed" in out
    assert "do not count yet" in out


def test_a_fully_reviewed_set_says_nothing_about_review(tmp_path, monkeypatch, capsys):
    path = tmp_path / "negatives.json"
    body = document()
    for e in body["entries"]:
        e["reviewed"] = True
    path.write_text(json.dumps(body))
    monkeypatch.setattr(vcs, "SET_PATH", path)
    monkeypatch.setattr(vcs, "REPO_ROOT", tmp_path)

    assert main([]) == 0
    assert "not yet human-reviewed" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_main_exits_non_zero_on_a_broken_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "negatives.json"
    body = document()
    body["entries"][0]["category"] = "made-up"
    path.write_text(json.dumps(body))
    monkeypatch.setattr(vcs, "SET_PATH", path)
    monkeypatch.setattr(vcs, "REPO_ROOT", tmp_path)

    assert main([]) == 1
    assert "not a declared category" in capsys.readouterr().out


def test_main_exits_zero_on_the_committed_file(capsys):
    assert main([]) == 0


def test_malformed_json_fails_with_a_position(tmp_path, monkeypatch):
    path = tmp_path / "negatives.json"
    path.write_text('{"entries": ')
    monkeypatch.setattr(vcs, "SET_PATH", path)
    with pytest.raises(SystemExit, match="not valid JSON"):
        main([])


def test_a_missing_file_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vcs, "SET_PATH", tmp_path / "absent.json")
    with pytest.raises(SystemExit, match="not found"):
        main([])


def test_check_does_not_mutate_the_document():
    body = document()
    before = copy.deepcopy(body)
    check(body)
    assert body == before
