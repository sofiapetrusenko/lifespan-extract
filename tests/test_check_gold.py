"""Tests for scripts/check_gold.py.

No network anywhere: every test supplies its own `pmid -> abstract` callable.
"""

from __future__ import annotations

import copy
import json

import check_gold
import pytest
from check_gold import (
    FAIL,
    WARN,
    check_cross_file,
    check_draft_markers,
    check_file,
    check_pairs,
    check_quotes,
    closest_match,
    iter_claims,
    main,
    strip_private,
)
from scaffold_gold import DRAFT_ID_SUFFIX, DRAFT_NOTE_PREFIX
from validate_gold import load_schema

ABSTRACT = (
    "Inhibition of the TOR signalling pathway extends lifespan. Here we report that "
    "rapamycin, an inhibitor of the mTOR pathway, extends median and maximal lifespan "
    "of both male and female mice when fed beginning at 600 days of age."
)


def claim(value, quote=None, extracted_from="abstract"):
    return {
        "value": value,
        "source_quote": quote,
        "confidence": "high",
        "extracted_from": extracted_from,
    }


def experiment(agent="rapamycin", organism="M. musculus", **overrides):
    body = {
        "experiment_id": "harrison2009-mmusculus-rapamycin",
        "organism": claim(organism, "extends median and maximal lifespan"),
        "strain": claim("UM-HET3", None, "full_text"),
        "sex": claim("male", "both male and female mice"),
        "sample_size": claim(None, None, "full_text"),
        "intervention": {
            "type": claim("pharmacological", "an inhibitor of the mTOR pathway"),
            "agent": claim(agent, "rapamycin, an inhibitor of the mTOR pathway"),
            "dose": claim(None, None, "full_text"),
            "age_at_start": claim("600 days", "fed beginning at 600 days of age"),
        },
        "mechanism": claim("mTOR inhibition", "an inhibitor of the mTOR pathway"),
        "lifespan_effect": {
            "direction": claim("increase", "extends median and maximal lifespan"),
            "median_change_pct": claim(None, None, "full_text"),
            "mean_change_pct": claim(None, None, "full_text"),
            "max_change_pct": claim(None, None, "full_text"),
            "p_value": claim(None, None, "full_text"),
        },
        "notes": None,
    }
    body.update(overrides)
    return body


def document(**overrides):
    body = {
        "schema_version": "0.2.1",
        "paper": {
            "doi": "10.1038/nature08221",
            "title": "Rapamycin fed late in life extends lifespan",
            "year": 2009,
            "source": "pubmed",
            "pmid": "19587680",
        },
        "experiments": [experiment()],
    }
    body.update(overrides)
    return body


def abstracts(mapping=None):
    """Return a `pmid -> abstract` lookup backed by a dict."""
    table = {"19587680": ABSTRACT} if mapping is None else mapping
    return lambda pmid: table.get(pmid)


@pytest.fixture
def validator():
    return load_schema()[0]


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return path


# --------------------------------------------------------------------------
# traversal
# --------------------------------------------------------------------------


def test_strip_private_removes_only_top_level_underscore_keys():
    body = {"schema_version": "0.2.1", "_abstract": "text", "_journal": "Nature"}
    assert strip_private(body) == {"schema_version": "0.2.1"}


def test_strip_private_leaves_nested_underscore_keys_to_fail_validation():
    """A `_` key inside an experiment is a typo, not a convention."""
    body = {"experiments": [{"_oops": 1}]}
    assert strip_private(body)["experiments"] == [{"_oops": 1}]


def test_iter_claims_finds_every_wrapper_by_shape():
    found = dict(iter_claims(document()))
    assert "experiments[0].organism" in found
    assert "experiments[0].intervention.agent" in found
    assert "experiments[0].lifespan_effect.p_value" in found
    # Flat fields are not claims.
    assert "paper.title" not in found
    assert "experiments[0].experiment_id" not in found


# --------------------------------------------------------------------------
# (a) schema + draft markers
# --------------------------------------------------------------------------


def test_valid_document_passes(tmp_path, validator):
    report = check_file(write(tmp_path, "harrison2009.json", document()), validator, abstracts())
    assert report.schema_errors == []
    assert not report.failed


def test_schema_error_is_reported_and_fails(tmp_path, validator):
    broken = document()
    broken["experiments"][0]["organism"]["value"] = "D. melanogaster"
    report = check_file(write(tmp_path, "x.json", broken), validator, abstracts())

    assert report.failed
    assert any("D. melanogaster" in error for error in report.schema_errors)


def test_private_keys_do_not_break_validation(tmp_path, validator):
    """A scaffolded draft carries `_abstract`; it must still validate."""
    draft = document(_abstract=ABSTRACT, _journal="Nature")
    report = check_file(write(tmp_path, "draft.json", draft), validator, abstracts())
    assert report.schema_errors == []


def test_missing_file_fails_without_raising(tmp_path, validator):
    report = check_file(tmp_path / "absent.json", validator, abstracts())
    assert report.failed
    assert any("not found" in issue.message for issue in report.issues)


def test_malformed_json_fails_with_a_position(tmp_path, validator):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": ')
    report = check_file(path, validator, abstracts())
    assert report.failed
    assert any("invalid JSON at line" in error for error in report.schema_errors)


def test_draft_markers_fail_even_though_the_schema_accepts_them():
    """The whole point: a scaffolded skeleton must not pass as a labelled record."""
    draft = document()
    draft["experiments"][0]["experiment_id"] = f"harrison2009{DRAFT_ID_SUFFIX}"
    draft["experiments"][0]["notes"] = f"{DRAFT_NOTE_PREFIX} — replace everything"

    issues = check_draft_markers(draft)
    assert len(issues) == 2
    assert all(issue.level == FAIL for issue in issues)


def test_labelled_record_has_no_draft_markers():
    assert check_draft_markers(document()) == []


# --------------------------------------------------------------------------
# (b) verbatim quotes
# --------------------------------------------------------------------------


def test_exact_quote_passes():
    results = check_quotes(document(), ABSTRACT)
    assert all(r.status in {"ok", "full_text"} for r in results)
    assert any(r.status == "ok" for r in results)


def test_fulltext_quotes_are_counted_but_not_checked():
    """The abstract is not the text they came from; failing them would be noise."""
    body = document()
    body["experiments"][0]["strain"] = claim(
        "UM-HET3", "mice were produced by mating CB6F1 females", "full_text"
    )
    results = {r.location: r.status for r in check_quotes(body, ABSTRACT)}
    assert results["experiments[0].strain"] == "full_text"


def test_quote_absent_from_the_abstract_fails():
    body = document()
    body["experiments"][0]["organism"] = claim("M. musculus", "a sentence the paper never contained")
    results = {r.location: r for r in check_quotes(body, ABSTRACT)}
    assert results["experiments[0].organism"].failed


def test_failure_detail_shows_the_closest_match_and_first_difference():
    body = document()
    # One character changed: a unicode en dash where the abstract has a hyphen.
    body["experiments"][0]["organism"] = claim("M. musculus", "extends median and maximal lifespan!")
    result = next(r for r in check_quotes(body, ABSTRACT) if r.failed)

    assert "closest match" in result.detail
    assert "first difference at char" in result.detail
    assert "extends median and maximal lifespan" in result.detail


def test_whitespace_only_difference_passes_but_is_reported():
    """Structured abstracts are reassembled with newlines an author would not type."""
    body = document()
    body["experiments"][0]["organism"] = claim(
        "M. musculus", "extends median  and maximal\nlifespan"
    )
    result = next(r for r in check_quotes(body, ABSTRACT) if r.location.endswith("organism"))
    assert result.status == "whitespace"
    assert not result.failed


def test_null_quotes_are_not_checked():
    body = document()
    body["experiments"][0]["organism"] = claim("M. musculus", None)
    locations = [r.location for r in check_quotes(body, ABSTRACT)]
    assert "experiments[0].organism" not in locations


def test_missing_abstract_marks_quotes_skipped_not_passed():
    """An unreachable PubMed must not read as a clean run."""
    body = document()
    body["experiments"][0]["strain"] = claim("UM-HET3", "mating CB6F1 females", "full_text")

    results = check_quotes(body, None)
    assert {r.status for r in results} == {"skipped", "full_text"}
    assert not any(r.failed for r in results)
    # Nothing is reported as passing.
    assert not any(r.status == "ok" for r in results)


def test_unfetchable_abstract_warns_and_does_not_fail(tmp_path, validator):
    def explode(_pmid):
        raise RuntimeError("network down")

    report = check_file(write(tmp_path, "x.json", document()), validator, explode)
    assert not report.failed
    assert any(i.level == WARN and "network down" in i.message for i in report.issues)


def test_null_pmid_warns_rather_than_failing(tmp_path, validator):
    """A preprint legitimately has no PMID, so there is no abstract to check."""
    body = document()
    body["paper"]["pmid"] = None
    report = check_file(write(tmp_path, "preprint.json", body), validator, abstracts())

    assert not report.failed
    assert any("pmid is null" in i.message for i in report.issues)
    assert report.quote_results == []


def test_no_quotes_flag_skips_the_check_entirely(tmp_path, validator):
    report = check_file(write(tmp_path, "x.json", document()), validator, None)
    assert report.quote_results == []
    assert not report.failed


def test_closest_match_locates_the_right_region():
    ratio, window, index = closest_match("extends median and maximal lifespan!", ABSTRACT)
    assert ratio > 0.9
    assert "median and maximal lifespan" in window
    assert index is not None


def test_closest_match_on_an_empty_abstract():
    assert closest_match("anything", "") == (0.0, "", None)


# --------------------------------------------------------------------------
# (c) cross-file consistency
# --------------------------------------------------------------------------


def reports_for(**files):
    """Build FileReports directly, skipping disk I/O."""
    built = []
    for stem, body in files.items():
        report = check_gold.FileReport(path=check_gold.Path(f"{stem}.json"), document=body)
        built.append(report)
    return built


def test_agent_case_difference_fails():
    """`GET /interventions/{agent}` aggregates on this string."""
    other = document(experiments=[experiment(agent="Rapamycin")])
    issues = check_cross_file(reports_for(harrison2009=document(), miller2011=other))

    agent_issues = [i for i in issues if "spelled" in i.message]
    assert len(agent_issues) == 1
    assert agent_issues[0].level == FAIL
    assert "'Rapamycin'" in agent_issues[0].message
    assert "'rapamycin'" in agent_issues[0].message


def test_identical_agent_spellings_pass():
    issues = check_cross_file(reports_for(a=document(), b=document()))
    assert not any(i.level == FAIL for i in issues)


def test_whitespace_difference_in_agent_names_fails():
    other = document(experiments=[experiment(agent="rapamycin ")])
    issues = check_cross_file(reports_for(a=document(), b=other))
    assert any(i.level == FAIL and "spelled" in i.message for i in issues)


def test_absent_lookalikes_fail():
    """Open string fields make `N/A` schema-valid; it must not become a strain."""
    body = document()
    body["experiments"][0]["strain"] = claim("N/A", None, "full_text")
    issues = check_cross_file(reports_for(a=body, b=document()))

    lookalike = [i for i in issues if "absence is written" in i.message]
    assert len(lookalike) == 1
    assert lookalike[0].level == FAIL


def test_not_reported_is_the_accepted_spelling_of_absence():
    body = document()
    body["experiments"][0]["strain"] = claim("not_reported", None, "full_text")
    issues = check_cross_file(reports_for(a=body, b=document()))
    assert not any("absence is written" in i.message for i in issues)


def test_unrelated_dose_units_warn_rather_than_fail():
    left = document(experiments=[experiment()])
    left["experiments"][0]["intervention"]["dose"] = claim("14 ppm", None, "full_text")
    right = document(experiments=[experiment()])
    right["experiments"][0]["intervention"]["dose"] = claim("0.1% (w/w)", None, "full_text")

    issues = check_cross_file(reports_for(a=left, b=right))
    unit_issues = [i for i in issues if "unrelated units" in i.message]
    assert len(unit_issues) == 1
    assert unit_issues[0].level == WARN


def test_compatible_dose_units_stay_quiet():
    """0.1% (w/w) beside 1% (w/w) is not an inconsistency."""
    left = document(experiments=[experiment()])
    left["experiments"][0]["intervention"]["dose"] = claim("0.1% (w/w) in diet", None, "full_text")
    right = document(experiments=[experiment()])
    right["experiments"][0]["intervention"]["dose"] = claim("1% (w/w) in diet", None, "full_text")

    issues = check_cross_file(reports_for(a=left, b=right))
    assert not any("unrelated units" in i.message for i in issues)


def test_mixed_schema_versions_warn():
    other = document(schema_version="0.1.0")
    issues = check_cross_file(reports_for(a=document(), b=other))
    version_issues = [i for i in issues if "schema versions" in i.message]
    assert len(version_issues) == 1
    assert version_issues[0].level == WARN


def test_single_file_still_gets_the_per_file_consistency_check():
    body = document()
    body["experiments"][0]["strain"] = claim("unknown", None, "full_text")
    issues = check_cross_file(reports_for(only=body))
    assert any(i.level == FAIL for i in issues)


# --------------------------------------------------------------------------
# (d) pair sanity
# --------------------------------------------------------------------------


def test_pair_with_matching_shape_is_quiet():
    issues = check_pairs(reports_for(harrison2009=document(), miller2011=document()))
    assert not any(i.level == WARN for i in issues)


def test_pair_with_different_populated_fields_warns():
    other = copy.deepcopy(document())
    other["experiments"][0]["lifespan_effect"]["median_change_pct"] = claim(
        10, "a 10% increase", "full_text"
    )
    issues = check_pairs(reports_for(harrison2009=document(), miller2011=other))

    shape_issues = [i for i in issues if "different fields populated" in i.message]
    assert len(shape_issues) == 1
    assert shape_issues[0].level == WARN
    assert "median_change_pct" in shape_issues[0].message


def test_incomplete_pair_is_information_not_failure():
    """miller2011 is not in the gold set yet; that is a to-do, not an error."""
    issues = check_pairs(reports_for(harrison2009=document()))
    assert [i.level for i in issues] == ["INFO"]
    assert "miller2011" in issues[0].message


def test_pair_absent_entirely_says_nothing():
    assert check_pairs(reports_for(kenyon1993=document())) == []


def test_pair_sex_split_arms_do_not_count_as_a_shape_difference():
    """Harrison 2009 splits one arm by sex; those rows are the same shape."""
    split = document(
        experiments=[
            experiment(sex=claim("male", "both male and female mice")),
            experiment(sex=claim("female", "both male and female mice")),
        ]
    )
    issues = check_pairs(reports_for(harrison2009=split, miller2011=document()))
    assert not any("different fields populated" in i.message for i in issues)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_main_exits_zero_on_a_clean_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    path = write(tmp_path, "harrison2009.json", document())

    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "All checks passed" in out
    assert "RESULT" in out


def test_main_exits_non_zero_on_a_bad_quote(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    body = document()
    body["experiments"][0]["organism"] = claim("M. musculus", "never appeared in this paper")

    assert main([str(write(tmp_path, "x.json", body))]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_exits_non_zero_on_a_cross_file_failure(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    left = write(tmp_path, "harrison2009.json", document())
    right = write(tmp_path, "miller2011.json", document(experiments=[experiment(agent="Rapamycin")]))

    assert main([str(left), str(right)]) == 1
    assert "cross-file consistency" in capsys.readouterr().out


def test_main_requires_files_or_all():
    with pytest.raises(SystemExit):
        main([])


def test_main_rejects_all_with_file_arguments():
    with pytest.raises(SystemExit):
        main(["--all", "some.json"])


def test_main_rejects_contradictory_cache_flags():
    with pytest.raises(SystemExit):
        main(["--all", "--offline", "--refresh"])


def test_main_no_quotes_needs_no_network(tmp_path, capsys, monkeypatch):
    """--no-quotes must not even construct the lookup, so it works offline."""

    def explode(**_kwargs):
        raise AssertionError("the network layer must not be touched with --no-quotes")

    monkeypatch.setattr(check_gold, "make_abstract_lookup", explode)
    assert main([str(write(tmp_path, "x.json", document())), "--no-quotes"]) == 0
