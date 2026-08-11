"""Tests for scripts/check_gold.py.

No network anywhere: every test supplies its own `pmid -> abstract` and
`pmid -> full text` callables.
"""

from __future__ import annotations

import copy
import json

import check_gold
import pytest
from check_gold import (
    FAIL,
    INFO,
    NOT_IN_PMC,
    WARN,
    check_cross_file,
    check_draft_markers,
    check_file,
    check_pairs,
    check_private_keys,
    check_quotes,
    closest_match,
    in_directory,
    iter_claims,
    main,
    promotion_blockers,
    strip_private,
)
from scaffold_gold import DRAFT_ID_SUFFIX, DRAFT_NOTE_PREFIX
from validate_gold import load_schema

ABSTRACT = (
    "Inhibition of the TOR signalling pathway extends lifespan. Here we report that "
    "rapamycin, an inhibitor of the mTOR pathway, extends median and maximal lifespan "
    "of both male and female mice when fed beginning at 600 days of age."
)

# Line-wrapped the way PMC's JATS is, and not the way the published PDF is:
# every full-text test below leans on the wrapping being in the wrong places.
FULL_TEXT = (
    "Genetically heterogeneous mice were produced by mating\nCB6F1 females to C3D2F1 males.\n\n"
    "Rapamycin was\nmicroencapsulated and fed at 14 ppm in the diet.\n\n"
    "Median survival increased by 14% in\nmales and 9% in females."
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


def full_texts(mapping=None):
    """Return a `pmid -> PMC full text` lookup backed by a dict.

    Follows `make_full_text_lookup`'s protocol, which is the point of stubbing
    at this seam: a missing key returns None, meaning "not in PMC open access".
    A lookup that cannot answer at all raises instead, and the tests that need
    that supply their own callable.
    """
    table = {"19587680": FULL_TEXT} if mapping is None else mapping
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
    results = check_quotes(document(), ABSTRACT, FULL_TEXT)
    assert all(r.status == "ok" for r in results)


def test_fulltext_quotes_are_never_checked_against_the_abstract():
    """The abstract is not the text they came from; failing them there is noise."""
    body = document()
    body["experiments"][0]["strain"] = claim(
        "UM-HET3", "mice were produced by mating CB6F1 females", "full_text"
    )
    result = next(r for r in check_quotes(body, ABSTRACT, FULL_TEXT) if r.location.endswith("strain"))
    assert result.status == "ok"
    assert result.source == "full_text"


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

    results = check_quotes(body, None, None)
    assert {r.status for r in results} == {"skipped"}
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
# (b) full-text quotes, against PMC
# --------------------------------------------------------------------------


def with_full_text_quote(quote):
    """A document whose one full-text quote is `quote`."""
    body = document()
    body["experiments"][0]["strain"] = claim("UM-HET3", quote, "full_text")
    return body


def full_text_result(body, full_text=FULL_TEXT):
    return next(
        r for r in check_quotes(body, ABSTRACT, full_text) if r.location.endswith("strain")
    )


def test_full_text_quote_present_in_pmc_passes():
    result = full_text_result(with_full_text_quote("microencapsulated and fed at 14 ppm"))
    assert result.status == "ok"
    assert result.source == "full_text"


def test_full_text_quote_matches_across_pmc_line_wrapping():
    """The reason this is normalised at all: PMC wraps where the PDF does not."""
    body = with_full_text_quote("mice were produced by mating CB6F1 females to C3D2F1 males")
    assert full_text_result(body).status == "ok"


def test_full_text_quote_absent_from_pmc_fails():
    body = with_full_text_quote("a sentence that appears in no version of this paper")
    result = full_text_result(body)
    assert result.failed
    assert "not verbatim in the full text" in result.detail


def test_full_text_mismatch_detail_names_the_full_text_not_the_abstract():
    """A labeller reading the report must not go looking in the wrong document."""
    body = with_full_text_quote("Median survival increased by 40% in males")
    detail = full_text_result(body).detail
    assert "full text:" in detail
    assert "abstract" not in detail
    assert "first difference at char" in detail


def test_case_difference_still_fails_in_full_text():
    """Only whitespace is normalised. Case is a real difference."""
    body = with_full_text_quote("MEDIAN SURVIVAL INCREASED BY 14%")
    assert full_text_result(body).failed


def test_punctuation_difference_still_fails_in_full_text():
    body = with_full_text_quote("Rapamycin was microencapsulated and fed at 14ppm")
    assert full_text_result(body).failed


def test_paper_not_in_pmc_makes_its_quotes_unverifiable_not_failed():
    """A quote we cannot check is not a quote we know to be wrong."""
    body = with_full_text_quote("some sentence only the publisher's PDF has")
    result = full_text_result(body, NOT_IN_PMC)

    assert result.status == "unverifiable"
    assert not result.failed
    assert result.detail == "not in PMC open access"


def test_unavailable_full_text_lookup_is_a_skip_not_an_unverifiable():
    """`None` means we did not look; `NOT_IN_PMC` means nobody can. Distinct."""
    body = with_full_text_quote("microencapsulated and fed at 14 ppm")
    assert full_text_result(body, None).status == "skipped"


def test_abstract_quotes_are_unaffected_by_a_missing_full_text():
    body = with_full_text_quote("microencapsulated and fed at 14 ppm")
    results = {r.location: r for r in check_quotes(body, ABSTRACT, NOT_IN_PMC)}
    assert results["experiments[0].organism"].status == "ok"


def test_check_file_reports_a_paper_missing_from_pmc(tmp_path, validator):
    body = with_full_text_quote("some sentence only the publisher's PDF has")
    report = check_file(
        write(tmp_path, "x.json", body), validator, abstracts(), full_texts({})
    )

    assert not report.failed, "not being in PMC is a warning, never a failure"
    assert any(i.level == WARN and "no PMC open-access full text" in i.message
               for i in report.issues)
    assert report.counts("full_text")["unverifiable"] == 1


def test_check_file_verifies_full_text_quotes_when_pmc_has_them(tmp_path, validator):
    body = with_full_text_quote("Median survival increased by 14% in males")
    report = check_file(
        write(tmp_path, "x.json", body), validator, abstracts(), full_texts()
    )

    assert not report.failed
    assert report.counts("full_text") == {"ok": 1}


def test_check_file_fails_a_full_text_quote_pmc_contradicts(tmp_path, validator):
    body = with_full_text_quote("Median survival increased by 40% in males")
    report = check_file(
        write(tmp_path, "x.json", body), validator, abstracts(), full_texts()
    )
    assert report.failed


def test_unfetchable_full_text_warns_and_does_not_fail(tmp_path, validator):
    def explode(_pmid):
        raise RuntimeError("elink unreachable")

    body = with_full_text_quote("microencapsulated and fed at 14 ppm")
    report = check_file(write(tmp_path, "x.json", body), validator, abstracts(), explode)

    assert not report.failed
    assert any(i.level == WARN and "elink unreachable" in i.message for i in report.issues)
    assert report.counts("full_text")["skipped"] == 1


def test_no_full_text_quotes_means_pmc_is_never_asked(tmp_path, validator):
    """No claim depends on the answer, so a warning about it would be noise."""

    def explode(_pmid):
        raise AssertionError("PMC must not be consulted for a file with no full-text quotes")

    report = check_file(write(tmp_path, "x.json", document()), validator, abstracts(), explode)
    assert not report.failed
    assert not any("PMC" in i.message for i in report.issues)


def test_full_text_column_shows_verified_over_total(tmp_path, capsys, monkeypatch):
    """The gap this closes: `8` read the same whether or not anything was checked."""
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(check_gold, "make_full_text_lookup", lambda **_: full_texts({}))
    body = with_full_text_quote("some sentence only the publisher's PDF has")

    assert main([str(write(tmp_path, "x.json", body))]) == 0
    out = capsys.readouterr().out
    assert "0/1" in out
    assert "unverifiable — not in PMC open access" in out


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


# --------------------------------------------------------------------------
# scaffolding keys: stripped in a draft, fatal in data/gold/
# --------------------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Stand-in `data/gold/` and `data/drafts/`. No test touches the real ones."""
    gold = tmp_path / "gold"
    drafts = tmp_path / "drafts"
    gold.mkdir()
    drafts.mkdir()
    monkeypatch.setattr(check_gold, "GOLD_DIR", gold)
    monkeypatch.setattr(check_gold, "DRAFTS_DIR", drafts)
    return gold, drafts


def scaffolded(**overrides):
    return document(_abstract=ABSTRACT, _journal="Nature", **overrides)


def scaffolded_with_full_text_quote(quote):
    """A promotable draft whose one full-text quote is `quote`."""
    return scaffolded(experiments=with_full_text_quote(quote)["experiments"])


def test_scaffolding_keys_in_a_gold_file_fail(dirs, validator):
    """The gap this closes: check_gold said ok, the pre-commit hook said no."""
    gold, _ = dirs
    report = check_file(write(gold, "harrison2009.json", scaffolded()), validator, abstracts())

    assert report.failed
    offending = [i for i in report.issues if i.level == FAIL and "scaffolding key" in i.message]
    assert len(offending) == 1
    assert "'_abstract'" in offending[0].message
    assert "'_journal'" in offending[0].message


def test_a_gold_file_with_scaffolding_keys_is_validated_unstripped(dirs, validator):
    """check_gold must report the same schema errors the pre-commit hook will.

    validate_gold.py does not strip, so the document it rejects is the raw one.
    If check_gold stripped here it would print a clean schema column for a file
    the hook is about to refuse.
    """
    gold, _ = dirs
    body = scaffolded()
    report = check_file(write(gold, "harrison2009.json", body), validator, abstracts())

    hook_errors = check_gold.check_schema(body, validator, strip=False)
    assert report.schema_errors == hook_errors
    assert any("_abstract" in error for error in report.schema_errors)


def test_scaffolding_keys_in_a_draft_are_stripped_and_announced(dirs, validator):
    """A draft is allowed to carry them — quietly passing is not the same as
    silently passing, so the strip is stated."""
    _, drafts = dirs
    report = check_file(write(drafts, "miller2011.json", scaffolded()), validator, abstracts())

    assert not report.failed
    assert report.schema_errors == []
    stripped = [i for i in report.issues if i.level == INFO and "scaffolding" in i.message]
    assert len(stripped) == 1


def test_a_clean_gold_file_still_passes(dirs, validator):
    gold, _ = dirs
    report = check_file(write(gold, "harrison2009.json", document()), validator, abstracts())
    assert not report.failed
    assert not any("scaffolding" in i.message for i in report.issues)


def test_check_private_keys_distinguishes_gold_from_draft():
    body = {"_abstract": "x", "schema_version": "0.2.1"}
    assert check_private_keys(body, is_gold=True)[0].level == FAIL
    assert check_private_keys(body, is_gold=False)[0].level == INFO
    assert check_private_keys({"schema_version": "0.2.1"}, is_gold=True) == []


def test_a_file_outside_both_directories_keeps_the_lenient_behaviour(tmp_path, validator):
    """Checking an arbitrary path is not a promotion; it must not be stricter
    than checking a draft."""
    report = check_file(write(tmp_path, "scratch.json", scaffolded()), validator, abstracts())
    assert not report.failed


def test_in_directory_resolves_indirect_paths(dirs):
    gold, drafts = dirs
    sneaky = drafts / ".." / "gold" / "harrison2009.json"
    assert in_directory(sneaky, gold)
    assert not in_directory(drafts / "miller2011.json", gold)


# --------------------------------------------------------------------------
# --promote
# --------------------------------------------------------------------------


def promote_argv(path, *extra):
    return [str(path), "--promote", *extra]


def test_promote_moves_a_passing_draft_into_gold(dirs, capsys, monkeypatch):
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    draft = write(drafts, "miller2011.json", scaffolded())

    assert main(promote_argv(draft)) == 0

    target = gold / "miller2011.json"
    assert target.exists()
    assert not draft.exists()
    promoted = json.loads(target.read_text())
    assert "_abstract" not in promoted
    assert "_journal" not in promoted
    assert promoted["experiments"] == document()["experiments"]
    assert "scaffolding stripped, draft removed" in capsys.readouterr().out


def test_a_promoted_file_satisfies_the_pre_commit_hook(dirs, monkeypatch, validator):
    """The end of the loop: what --promote writes must validate unstripped."""
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    main(promote_argv(write(drafts, "miller2011.json", scaffolded())))

    promoted = json.loads((gold / "miller2011.json").read_text())
    assert check_gold.check_schema(promoted, validator, strip=False) == []


def test_promote_refuses_a_draft_that_fails_a_check(dirs, capsys, monkeypatch):
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    body = scaffolded()
    body["experiments"][0]["organism"] = claim("M. musculus", "never appeared in this paper")
    draft = write(drafts, "miller2011.json", body)

    assert main(promote_argv(draft)) == 1
    assert draft.exists(), "a refused promotion must leave the draft alone"
    assert not (gold / "miller2011.json").exists()
    assert "not promoted" in capsys.readouterr().out


def test_promote_never_overwrites_an_existing_gold_file(dirs, capsys, monkeypatch):
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    existing = write(gold, "miller2011.json", document())
    original = existing.read_text()
    draft = write(drafts, "miller2011.json", scaffolded())

    assert main(promote_argv(draft)) == 1
    assert existing.read_text() == original
    assert draft.exists()
    assert "already exists" in capsys.readouterr().out


def test_promote_refuses_when_quotes_could_not_be_verified(dirs, capsys, monkeypatch):
    """Exit zero means nothing is known to be wrong; promoting claims more."""
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts({}))
    draft = write(drafts, "miller2011.json", scaffolded())

    assert main(promote_argv(draft)) == 1
    assert draft.exists()
    assert not (gold / "miller2011.json").exists()
    assert "could not be checked against PubMed" in capsys.readouterr().out


def test_promote_refuses_when_a_quote_is_unverifiable(dirs, capsys, monkeypatch):
    """Exit zero is "nothing is known to be wrong"; promoting claims it was checked."""
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(check_gold, "make_full_text_lookup", lambda **_: full_texts({}))
    draft = write(
        drafts,
        "miller2011.json",
        scaffolded_with_full_text_quote("some sentence only the publisher's PDF has"),
    )

    assert main(promote_argv(draft)) == 1
    assert draft.exists()
    assert not (gold / "miller2011.json").exists()
    assert "unverifiable" in capsys.readouterr().out


def test_promote_accepts_a_draft_whose_full_text_quotes_verify(dirs, monkeypatch):
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(check_gold, "make_full_text_lookup", lambda **_: full_texts())
    draft = write(
        drafts,
        "miller2011.json",
        scaffolded_with_full_text_quote("microencapsulated and fed at 14 ppm"),
    )

    assert main(promote_argv(draft)) == 0
    assert (gold / "miller2011.json").exists()
    assert not draft.exists()


def test_promotion_blockers_names_the_unverifiable_quotes(dirs):
    report = check_gold.FileReport(
        path=dirs[1] / "miller2011.json",
        document=with_full_text_quote("x"),
        quote_results=[check_gold.QuoteResult("experiments[0].strain", "unverifiable", "full_text")],
    )
    reasons = promotion_blockers(report, cross_failed=False, quotes_enabled=True)
    assert any("not in PMC open access" in r for r in reasons)


def test_promote_refuses_a_file_already_in_gold(dirs, capsys, monkeypatch):
    gold, _ = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    path = write(gold, "harrison2009.json", document())

    assert main(promote_argv(path)) == 1
    assert path.exists()
    assert "already in data/gold/" in capsys.readouterr().out


def test_promote_refuses_every_file_when_a_cross_file_check_fails(dirs, capsys, monkeypatch):
    """One bad agent spelling is a property of the pair, not of one file."""
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    left = write(drafts, "harrison2009.json", scaffolded())
    right = write(
        drafts, "miller2011.json", scaffolded(experiments=[experiment(agent="Rapamycin")])
    )

    assert main([str(left), str(right), "--promote"]) == 1
    assert left.exists() and right.exists()
    assert not list(gold.iterdir())
    assert "a cross-file check failed" in capsys.readouterr().out


def test_promotion_blockers_are_reported_together(dirs):
    """A refusal names every reason, not the first one — same rule as the
    validator's all-errors policy."""
    gold, drafts = dirs
    write(gold, "miller2011.json", document())
    report = check_gold.FileReport(path=drafts / "miller2011.json", document=document())

    reasons = promotion_blockers(report, cross_failed=True, quotes_enabled=False)
    assert len(reasons) >= 3
    assert any("cross-file" in r for r in reasons)
    assert any("--no-quotes" in r for r in reasons)
    assert any("already exists" in r for r in reasons)


def test_promote_rejects_no_quotes():
    with pytest.raises(SystemExit):
        main(["x.json", "--promote", "--no-quotes"])


def test_promote_rejects_all():
    with pytest.raises(SystemExit):
        main(["--all", "--promote"])


# --------------------------------------------------------------------------
# --refresh-quotes
# --------------------------------------------------------------------------

# The real shape of the problem: Wiley sets U+2010 HYPHEN and U+00B1, a labeller
# types ASCII. The claim is right; the quote string is retyped, not copied.
TYPESET_FULL_TEXT = (
    "UM‐HET3 mice were produced at each of the\nthree test sites.\n\n"
    "The dose yielded 0.45 ± 0.09 mM in serum.\n\n"
    "Median survival increased by 14%."
)


def typeset_document(quote, value="UM-HET3"):
    body = document()
    body["experiments"][0]["strain"] = claim(value, quote, "full_text")
    return body


def rewrite_for(quote, source=TYPESET_FULL_TEXT):
    return check_gold.plan_rewrite("experiments[0].strain", quote, source)


def test_best_slice_trims_to_the_alignment_not_the_quote_length():
    """`+/-` becoming `±` shortens the text; a fixed-length window would return
    a slice with two characters of the next sentence stuck on the end."""
    candidate, ratio, _ = check_gold.best_slice(
        "The dose yielded 0.45 +/- 0.09 mM in serum.",
        check_gold.collapse_whitespace(TYPESET_FULL_TEXT),
    )
    assert candidate == "The dose yielded 0.45 ± 0.09 mM in serum."
    assert ratio > 0.9


def test_refresh_proposes_the_exact_pmc_slice_for_a_transliteration():
    rewrite = rewrite_for("UM-HET3 mice were produced at each of the three test sites.")
    assert rewrite.unambiguous
    assert rewrite.candidate == "UM‐HET3 mice were produced at each of the three test sites."
    assert rewrite.ratio > 0.95


def test_refresh_collapses_pmc_line_wrapping_into_one_line():
    """The replacement must be a string PMC's wrapping cannot disagree with."""
    rewrite = rewrite_for("UM-HET3 mice were produced at each of the three test sites.")
    assert "\n" not in rewrite.candidate


def test_refresh_refuses_a_quote_that_is_not_a_mistranscription():
    """A table row rewritten as prose has no slice to substitute."""
    rewrite = rewrite_for("17aE2 144 925 19 0.000 (Table 1, males pooled across sites)")
    assert not rewrite.unambiguous
    assert "not a mistranscription" in rewrite.reason


def test_refresh_refuses_when_the_candidate_is_not_unique():
    """Boilerplate repeated in two sections gives no way to know which was quoted."""
    doubled = TYPESET_FULL_TEXT + "\n\n" + TYPESET_FULL_TEXT
    rewrite = rewrite_for("Median survival increased by 15%.", doubled)
    assert not rewrite.unambiguous
    assert "appears 2 times" in rewrite.reason


def test_refresh_refuses_when_a_second_passage_matches_as_well():
    source = TYPESET_FULL_TEXT + "\n\nMedian survival increased by 13%."
    rewrite = rewrite_for("Median survival increased by 15%.", source)
    assert not rewrite.unambiguous
    assert "second passage" in rewrite.reason


def test_refresh_threshold_separates_the_two_classes():
    """The gold set's distribution is bimodal; the threshold sits in the gap."""
    assert rewrite_for("UM-HET3 mice were produced at each of the three test sites.").ratio >= 0.90
    assert rewrite_for("NDGA (2500): n=133, median 851 days, +10%").ratio < 0.90


def test_plan_refresh_leaves_a_passing_quote_alone():
    """A quote that already verifies is character-perfect; do not "improve" it."""
    body = typeset_document("Median survival increased by 14%.")
    report = check_gold.FileReport(path=check_gold.Path("x.json"), document=body)
    report.full_text = TYPESET_FULL_TEXT
    report.quote_results = check_quotes(body, ABSTRACT, TYPESET_FULL_TEXT)

    assert [r.status for r in report.quote_results if r.location.endswith("strain")] == ["ok"]
    assert check_gold.plan_refresh(report) == []


def test_plan_refresh_leaves_abstract_quotes_alone():
    """They are compared byte-exact; a collapsed slice would demote a pass."""
    body = document()
    body["experiments"][0]["organism"] = claim("M. musculus", "extends median  and maximal lifespan")
    report = check_gold.FileReport(path=check_gold.Path("x.json"), document=body)
    report.full_text = TYPESET_FULL_TEXT
    report.quote_results = check_quotes(body, ABSTRACT, TYPESET_FULL_TEXT)

    assert not any(r.location.endswith("organism") for r in check_gold.plan_refresh(report))


def test_apply_rewrites_touches_only_the_source_quote():
    """A value, a confidence and a note are the labeller's judgement."""
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    body["experiments"][0]["strain"]["confidence"] = "medium"
    body["experiments"][0]["notes"] = "checked against the PDF"
    before = copy.deepcopy(body)

    changed = check_gold.apply_rewrites(
        body,
        [rewrite_for("UM-HET3 mice were produced at each of the three test sites.")],
        TYPESET_FULL_TEXT,
    )

    assert changed == 1
    strain = body["experiments"][0]["strain"]
    assert strain["source_quote"] == "UM‐HET3 mice were produced at each of the three test sites."
    assert strain["value"] == before["experiments"][0]["strain"]["value"]
    assert strain["confidence"] == "medium"
    assert strain["extracted_from"] == "full_text"
    assert body["experiments"][0]["notes"] == "checked against the PDF"
    # Everything outside that one string is byte-identical.
    before["experiments"][0]["strain"]["source_quote"] = strain["source_quote"]
    assert body == before


def refresh_argv(path, *extra):
    return [str(path), "--refresh-quotes", *extra]


def refresh_stubs(monkeypatch):
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(
        check_gold, "make_full_text_lookup", lambda **_: (lambda _p: TYPESET_FULL_TEXT)
    )


def test_refresh_dry_run_changes_nothing_on_disk(tmp_path, capsys, monkeypatch):
    refresh_stubs(monkeypatch)
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    path = write(tmp_path, "strong2016.json", body)
    before = path.read_text()

    main(refresh_argv(path))

    assert path.read_text() == before
    out = capsys.readouterr().out
    assert "dry run — nothing is written" in out
    assert "would rewrite" in out


def test_refresh_write_repairs_the_quote_and_the_file_then_verifies(tmp_path, monkeypatch):
    """The property that matters: after a refresh the quote is verbatim."""
    refresh_stubs(monkeypatch)
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    path = write(tmp_path, "strong2016.json", body)

    assert main(refresh_argv(path, "--write")) == 1, "the pre-refresh run still reports the failure"

    rewritten = json.loads(path.read_text())
    assert rewritten["experiments"][0]["strain"]["source_quote"] == (
        "UM‐HET3 mice were produced at each of the three test sites."
    )
    # Re-checking the rewritten file is clean.
    assert main([str(path)]) == 0


def test_refresh_write_leaves_ambiguous_quotes_untouched(tmp_path, capsys, monkeypatch):
    refresh_stubs(monkeypatch)
    quote = "17aE2 144 925 19 0.000 (Table 1, males pooled across sites)"
    path = write(tmp_path, "strong2016.json", typeset_document(quote))

    main(refresh_argv(path, "--write"))

    assert json.loads(path.read_text())["experiments"][0]["strain"]["source_quote"] == quote
    assert "left alone" in capsys.readouterr().out


def test_refresh_writes_the_gold_sets_own_formatting(tmp_path, monkeypatch):
    """So the human's diff is the changed quote strings and nothing else."""
    refresh_stubs(monkeypatch)
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    path = tmp_path / "strong2016.json"
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")

    main(refresh_argv(path, "--write"))

    reserialised = json.dumps(json.loads(path.read_text()), indent=2, ensure_ascii=False) + "\n"
    assert path.read_text() == reserialised


def test_write_alone_is_rejected():
    with pytest.raises(SystemExit):
        main(["x.json", "--write"])


def test_refresh_rejects_no_quotes():
    with pytest.raises(SystemExit):
        main(["x.json", "--refresh-quotes", "--no-quotes"])


def test_refresh_and_promote_are_separate_steps():
    with pytest.raises(SystemExit):
        main(["x.json", "--refresh-quotes", "--promote"])


# --------------------------------------------------------------------------
# word-boundary snapping (regression: the "nsion of mean lifespan" defect)
# --------------------------------------------------------------------------

# The real PMC passage that produced the defect. `best_slice` walked back from
# the alignment by raw character count and landed inside "extension", returning
# a candidate that began "nsion of mean lifespan" — verbatim, unique, and
# gibberish. It was never written only because the quote scored 74%, below the
# refresh threshold; a slightly closer transliteration would have committed it.
MARTINMONTALVO_PMC = (
    "The survival curves of control and metformin-treated male mice separated shortly "
    "after the onset of the treatment. Diet supplementation with 0.1% metformin led to "
    "a 5.83% extension of mean lifespan (Fig. 1a), χ2 = 5.46 and p= 0.02 in "
    "Gehan-Breslow survival test. In agreement with these data, a different strain of "
    "male mice (B6C3F1) supplemented with the same dose of metformin (0.1% w/w) "
    "resulted in a 4.15% extension of mean lifespan."
)
MARTINMONTALVO_QUOTE = "chi2 = 5.46 and p = 0.02 in Gehan-Breslow survival test"


def test_best_slice_never_starts_mid_word_regression():
    """The defect itself: the candidate began 'nsion', mid-way through 'extension'."""
    candidate, _, at = check_gold.best_slice(MARTINMONTALVO_QUOTE, MARTINMONTALVO_PMC)

    assert not candidate.startswith("nsion")
    assert not check_gold.splits_a_word(MARTINMONTALVO_PMC, at, at + len(candidate))
    # And it still finds the right passage.
    assert "χ2 = 5.46 and p= 0.02 in Gehan-Breslow survival test" in candidate


def test_best_slice_boundaries_land_on_whitespace_edges():
    candidate, _, at = check_gold.best_slice(MARTINMONTALVO_QUOTE, MARTINMONTALVO_PMC)
    assert at == 0 or MARTINMONTALVO_PMC[at - 1].isspace()
    end = at + len(candidate)
    assert end == len(MARTINMONTALVO_PMC) or MARTINMONTALVO_PMC[end].isspace()


def test_best_slice_snapping_does_not_break_the_ordinary_case():
    """Snapping must not disturb a candidate that was already whole-word."""
    candidate, ratio, _ = check_gold.best_slice(
        "The dose yielded 0.45 +/- 0.09 mM in serum.",
        check_gold.collapse_whitespace(TYPESET_FULL_TEXT),
    )
    assert candidate == "The dose yielded 0.45 ± 0.09 mM in serum."
    assert ratio > 0.9


def test_best_slice_at_the_very_start_and_end_of_the_source():
    """Boundary arithmetic must not walk off either end."""
    source = "alpha beta gamma"
    candidate, _, at = check_gold.best_slice("alpha beta", source)
    assert at == 0 and not check_gold.splits_a_word(source, at, at + len(candidate))
    candidate, _, at = check_gold.best_slice("beta gamma", source)
    assert at + len(candidate) == len(source)


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (3, 8, True),    # "nsion" — word char on both sides of the left edge
        (0, 9, False),   # "extension" — whole word
        (0, 5, True),    # "exten" — cut on the right
        (10, 12, False), # "of" — whole word, spaces either side
    ],
)
def test_splits_a_word_detects_both_edges(start, end, expected):
    source = "extension of mean"
    assert check_gold.splits_a_word(source, start, end) is expected


def test_splits_a_word_allows_a_sentence_boundary():
    """A slice ending in '.' before a letter is a sentence edge, not a cut word."""
    source = "survival test. In agreement"
    assert not check_gold.splits_a_word(source, 0, len("survival test."))


def test_splits_a_word_on_an_empty_slice():
    assert not check_gold.splits_a_word("anything", 3, 3)


def test_plan_rewrite_refuses_a_mid_word_candidate(monkeypatch):
    """The gate in plan_rewrite, exercised by forcing best_slice to misbehave."""
    monkeypatch.setattr(
        check_gold, "best_slice", lambda _q, _s: ("nsion of mean lifespan", 0.99, 3)
    )
    rewrite = check_gold.plan_rewrite("x", "sion of mean lifespan", "extension of mean lifespan")
    assert not rewrite.unambiguous
    assert "inside a word" in rewrite.reason


def test_apply_rewrites_raises_rather_than_writing_a_mid_word_candidate():
    """The last gate before ground truth. Loud failure, not a silent skip."""
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    bad = check_gold.QuoteRewrite("experiments[0].strain", "whatever", "nsion of mean", 0.99)

    with pytest.raises(ValueError, match="inside a word"):
        check_gold.apply_rewrites(body, [bad], MARTINMONTALVO_PMC)
    # The document is untouched by the refusal.
    assert body["experiments"][0]["strain"]["source_quote"].startswith("UM-HET3")


def test_apply_rewrites_raises_when_the_candidate_is_not_in_the_source():
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    bogus = check_gold.QuoteRewrite("experiments[0].strain", "whatever", "not in the paper", 0.99)

    with pytest.raises(ValueError, match="not in the source text"):
        check_gold.apply_rewrites(body, [bogus], MARTINMONTALVO_PMC)


def test_apply_rewrites_still_writes_a_whole_word_candidate():
    body = typeset_document("UM-HET3 mice were produced at each of the three test sites.")
    good = rewrite_for("UM-HET3 mice were produced at each of the three test sites.")

    assert check_gold.apply_rewrites(body, [good], TYPESET_FULL_TEXT) == 1
    assert body["experiments"][0]["strain"]["source_quote"].startswith("UM‐HET3")


def test_a_faulting_full_text_lookup_is_a_skip_not_an_unverifiable(tmp_path, validator):
    """The distinction the elink bug destroyed, checked at the layer above.

    `unverifiable` says nobody can check this quote — a fact about the paper.
    `skipped` says we did not check it — a fact about the run. A service outage
    is the second, and reporting it as the first would launder an unanswered
    request into a settled statement about the source.
    """
    def faulting(_pmid):
        raise RuntimeError("elink reported an error rather than a link set")

    body = with_full_text_quote("microencapsulated and fed at 14 ppm")
    report = check_file(write(tmp_path, "x.json", body), validator, abstracts(), faulting)

    counts = report.counts("full_text")
    assert counts["skipped"] == 1
    assert counts["unverifiable"] == 0
    assert not report.failed


def test_a_skipped_full_text_quote_blocks_promotion_like_any_other(dirs, capsys, monkeypatch):
    """An outage must not make a draft look promotable."""
    _gold, drafts = dirs

    def faulting(_pmid):
        raise RuntimeError("elink reported an error rather than a link set")

    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(check_gold, "make_full_text_lookup", lambda **_: faulting)
    draft = write(drafts, "miller2011.json",
                  scaffolded_with_full_text_quote("microencapsulated and fed at 14 ppm"))

    assert main(promote_argv(draft)) == 1
    assert draft.exists()
    assert "could not be checked" in capsys.readouterr().out


# --------------------------------------------------------------------------
# bioRxiv preprints: abstract by DOI, full text unverifiable
# --------------------------------------------------------------------------

PREPRINT_ABSTRACT = (
    "We find that valine restriction (Val-R) improves metabolic health in C57BL/6J mice, "
    "and extends the lifespan of male, but not female, mice by 23%."
)
PREPRINT_DOI = "10.1101/2025.08.31.673254"


def preprint(**overrides):
    body = document()
    body["paper"] = {"doi": PREPRINT_DOI, "title": "Lifelong restriction of dietary valine",
                     "year": 2025, "source": "biorxiv", "pmid": None}
    body["experiments"][0] = experiment(
        organism="M. musculus",
        sex=claim("male", "extends the lifespan of male, but not female, mice by 23%"),
    )
    body["experiments"][0]["organism"] = claim(
        "M. musculus", "improves metabolic health in C57BL/6J mice")
    body["experiments"][0]["intervention"]["type"] = claim(
        "dietary", "valine restriction (Val-R) improves metabolic health")
    body["experiments"][0]["intervention"]["agent"] = claim(
        "valine restriction", "valine restriction (Val-R) improves metabolic health")
    body["experiments"][0]["intervention"]["age_at_start"] = claim(None, None, "full_text")
    body["experiments"][0]["mechanism"] = claim(None, None, "full_text")
    body["experiments"][0]["lifespan_effect"]["direction"] = claim(
        "increase", "extends the lifespan of male, but not female, mice by 23%")
    body.update(overrides)
    return body


def biorxiv_abstracts(mapping=None):
    table = {PREPRINT_DOI: PREPRINT_ABSTRACT} if mapping is None else mapping
    return lambda doi: table.get(doi)


def test_preprint_abstract_quotes_verify_against_biorxiv(tmp_path, validator):
    """A preprint is identified by DOI, and its quotes check the same way."""
    report = check_file(write(tmp_path, "green2025.json", preprint()), validator,
                        abstracts(), None, biorxiv_abstracts())

    assert not report.failed
    counts = report.counts("abstract")
    assert counts["fail"] == 0
    assert counts["ok"] > 0


def test_preprint_never_consults_pubmed(tmp_path, validator):
    """paper.source decides. A preprint has no PMID to look up."""
    def explode(_pmid):
        raise AssertionError("PubMed must not be consulted for a source: biorxiv record")

    report = check_file(write(tmp_path, "green2025.json", preprint()), validator,
                        explode, None, biorxiv_abstracts())
    assert not report.failed


def test_preprint_quote_absent_from_the_biorxiv_abstract_fails(tmp_path, validator):
    body = preprint()
    body["experiments"][0]["organism"] = claim("M. musculus", "a sentence the preprint never had")
    report = check_file(write(tmp_path, "green2025.json", body), validator,
                        abstracts(), None, biorxiv_abstracts())
    assert report.failed


def test_preprint_full_text_quotes_are_unverifiable_not_failed(tmp_path, validator):
    """bioRxiv full text is not in PMC, so nothing here can check it."""
    body = preprint()
    body["experiments"][0]["strain"] = claim("C57BL/6J", "mice were fed a valine-restricted diet",
                                             "full_text")
    report = check_file(write(tmp_path, "green2025.json", body), validator,
                        abstracts(), None, biorxiv_abstracts())

    assert not report.failed, "unverifiable is a warning, never a failure"
    assert report.counts("full_text")["unverifiable"] == 1
    assert any(i.level == WARN and "not in PMC" in i.message for i in report.issues)


def test_preprint_with_no_full_text_quotes_emits_no_pmc_warning(tmp_path, validator):
    report = check_file(write(tmp_path, "green2025.json", preprint()), validator,
                        abstracts(), None, biorxiv_abstracts())
    assert not any("PMC" in i.message for i in report.issues)


def test_promote_refuses_a_preprint_with_full_text_quotes(dirs, capsys, monkeypatch):
    """Same rule as lakowski1998: promotion cannot rest on unchecked quotes."""
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(check_gold, "make_full_text_lookup", lambda **_: full_texts({}))
    monkeypatch.setattr(check_gold, "make_biorxiv_abstract_lookup",
                        lambda **_: biorxiv_abstracts())
    body = preprint(_abstract=PREPRINT_ABSTRACT, _journal="bioRxiv")
    body["experiments"][0]["strain"] = claim("C57BL/6J", "mice were fed a valine-restricted diet",
                                             "full_text")
    draft = write(drafts, "green2025.json", body)

    assert main(promote_argv(draft)) == 1
    assert draft.exists()
    assert not (gold / "green2025.json").exists()
    assert "unverifiable" in capsys.readouterr().out


def test_promote_accepts_an_abstract_only_preprint(dirs, monkeypatch):
    """The point of the preprint slot: labelable and promotable without PMC."""
    gold, drafts = dirs
    monkeypatch.setattr(check_gold, "make_abstract_lookup", lambda **_: abstracts())
    monkeypatch.setattr(check_gold, "make_full_text_lookup", lambda **_: full_texts({}))
    monkeypatch.setattr(check_gold, "make_biorxiv_abstract_lookup",
                        lambda **_: biorxiv_abstracts())
    draft = write(drafts, "green2025.json",
                  preprint(_abstract=PREPRINT_ABSTRACT, _journal="bioRxiv"))

    assert main(promote_argv(draft)) == 0
    assert (gold / "green2025.json").exists()


def test_unreachable_biorxiv_warns_and_does_not_fail(tmp_path, validator):
    def explode(_doi):
        raise RuntimeError("bioRxiv unreachable")

    report = check_file(write(tmp_path, "green2025.json", preprint()), validator,
                        abstracts(), None, explode)
    assert not report.failed
    assert any(i.level == WARN and "bioRxiv unreachable" in i.message for i in report.issues)
    assert report.counts("abstract")["skipped"] > 0


def test_preprint_without_a_biorxiv_lookup_skips_rather_than_passes(tmp_path, validator):
    report = check_file(write(tmp_path, "green2025.json", preprint()), validator, abstracts())
    assert not report.failed
    assert report.counts("abstract")["ok"] == 0
    assert report.counts("abstract")["skipped"] > 0


def test_pubmed_records_are_unaffected_by_the_preprint_path(tmp_path, validator):
    """Regression guard: source: pubmed keeps checking against PubMed."""
    report = check_file(write(tmp_path, "harrison2009.json", document()), validator,
                        abstracts(), None, biorxiv_abstracts())
    assert not report.failed
    assert report.counts("abstract")["ok"] > 0
