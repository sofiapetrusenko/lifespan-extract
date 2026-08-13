"""The request schema derived from schema/experiment.schema.json.

The derivation exists so the two schemas cannot drift, so these tests compare
the derived one against the file rather than against a hand-written copy of
what it should contain — a literal expected-schema fixture would be the exact
duplication the derivation is meant to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import check_gold
import pytest

from extract.errors import ExtractError, RecordValidationError
from extract.schema import (
    CODE_ASSIGNED_CLAIM_KEYS,
    CODE_ASSIGNED_EXPERIMENT_KEYS,
    SCHEMA_PATH,
    UNSUPPORTED_KEYWORDS,
    _closest_window,
    _ellipsis,
    _flatten,
    _is_code_assigned,
    _resolve_composition,
    build_extraction_schema,
    check_provenance,
    check_quotes_verbatim,
    collapse_whitespace,
    load_schema,
    schema_version,
    validate_record,
)

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "gold"


@pytest.fixture
def document() -> dict[str, Any]:
    return load_schema()


@pytest.fixture
def derived(document: dict[str, Any]) -> dict[str, Any]:
    return build_extraction_schema(document)


def walk(node: Any):
    """Yield every dict inside `node`, itself included."""
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def experiment_schema(derived: dict[str, Any]) -> dict[str, Any]:
    return derived["properties"]["experiments"]["items"]


def test_derived_schema_uses_no_unsupported_keyword(derived):
    for node in walk(derived):
        offending = sorted(set(node) & UNSUPPORTED_KEYWORDS)
        assert not offending, f"{offending} survived derivation in {sorted(node)}"


def test_every_object_is_closed_and_fully_required(derived):
    for node in walk(derived):
        if "properties" not in node:
            continue
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])


def test_refs_and_allof_are_gone(derived):
    for node in walk(derived):
        assert "$ref" not in node
        assert "allOf" not in node


def test_code_assigned_keys_are_not_asked_of_the_model(derived):
    """experiment_id, notes and extracted_from are the caller's to fill in."""
    for node in walk(derived):
        properties = node.get("properties", {})
        assert "extracted_from" not in properties
        assert "experiment_id" not in properties
        assert "notes" not in properties


def test_claim_wrappers_carry_merged_provenance(derived):
    organism = experiment_schema(derived)["properties"]["organism"]
    assert set(organism["properties"]) == {"value", "source_quote", "confidence"}
    assert organism["required"] == list(organism["properties"])


# --- descriptions, which are the prompt the model actually reads ---
#
# Two branches in `extract/schema.py` decide where a composed schema's
# description may land, and both were deletable with the suite green. They are
# opposite halves of one rule, so they are pinned separately: `_flatten`'s skip
# keeps a definition's description off the wrapper that composes it, and
# `_inherited_description` puts it back on the bare `$ref` that only names it.
# Both expectations are read out of the schema file rather than written here,
# so a description reworded in the file cannot make either test stale.


def test_a_claim_wrapper_is_not_relabelled_with_the_provenance_boilerplate(document, derived):
    """`claim_organism` composes `provenance`; it is not a description of one.

    Without `_flatten`'s `source is not node` skip, every claim wrapper in the
    request schema — `organism` and `sex` among them — is labelled "Provenance
    keys shared by every claim wrapper", which is what the model is then asked
    to fill in.
    """
    boilerplate = document["$defs"]["provenance"]["description"]
    assert boilerplate, "the premise: the composed definition has a description to leak"

    mislabelled = [node for node in walk(derived) if node.get("description") == boilerplate]
    assert not mislabelled, f"{len(mislabelled)} node(s) carry the composed schema's description"

    for field in ("organism", "sex"):
        claim = experiment_schema(derived)["properties"][field]
        assert "description" not in claim


def test_a_bare_ref_keeps_the_description_of_the_definition_it_names(document, derived):
    """`{"$ref": "#/$defs/confidence"}` means that definition, so it reads as one.

    The inverse case: this node adds nothing of its own, so dropping the
    inherited description — deleting `_inherited_description`'s body — strips
    the guidance from every claim's `confidence` key without changing a
    validated field, and the model is left to guess what the levels mean.
    """
    expected = document["$defs"]["confidence"]["description"]
    assert expected, "the premise: the referenced definition has a description to inherit"

    for field in ("organism", "sex", "strain"):
        claim = experiment_schema(derived)["properties"][field]
        assert claim["properties"]["confidence"].get("description") == expected


def test_enums_match_the_source_schema(document, derived):
    """A drift guard: the derived enum is the file's enum, not a copy of it."""
    experiment = experiment_schema(derived)
    pairs = {
        "organism": "claim_organism",
        "sex": "claim_sex",
    }
    for field, definition in pairs.items():
        expected = document["$defs"][definition]["properties"]["value"]["enum"]
        assert experiment["properties"][field]["properties"]["value"]["enum"] == expected

    direction = experiment["properties"]["lifespan_effect"]["properties"]["direction"]
    assert (
        direction["properties"]["value"]["enum"]
        == document["$defs"]["claim_direction"]["properties"]["value"]["enum"]
    )


def test_nullable_types_become_anyof(derived):
    species = experiment_schema(derived)["properties"]["species"]["properties"]["value"]
    assert species["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "type" not in species


def test_nested_objects_are_flattened_too(derived):
    intervention = experiment_schema(derived)["properties"]["intervention"]
    assert set(intervention["properties"]) == {"type", "agent", "dose", "age_at_start"}
    agent = intervention["properties"]["agent"]
    assert agent["properties"]["value"] == {"type": "string"}


def test_unresolvable_ref_raises(document):
    document["$defs"]["experiment"]["properties"]["organism"] = {"$ref": "#/$defs/nope"}
    with pytest.raises(ExtractError, match=r"\$defs"):
        build_extraction_schema(document)


def test_remote_ref_raises(document):
    document["$defs"]["experiment"]["properties"]["organism"] = {"$ref": "https://x/y.json"}
    with pytest.raises(ExtractError, match="can only inline local"):
        build_extraction_schema(document)


def test_missing_experiments_raises(document):
    del document["properties"]["experiments"]
    with pytest.raises(ExtractError, match="properties.experiments"):
        build_extraction_schema(document)


@pytest.mark.parametrize("items", [None, "nope", [], 42])
def test_experiments_without_an_items_schema_raises(document, items):
    """The sibling of the guard above, and the one nothing was watching.

    Without it, a missing `items` is a bare `KeyError` — not an `ExtractError`,
    so no caller's handler recognises it — and a non-dict `items` is worse: it
    flows through `_flatten` untouched and the model is sent a request schema
    that asks for nothing in particular.
    """
    experiments = document["properties"]["experiments"]
    if items is None:
        del experiments["items"]
    else:
        experiments["items"] = items

    with pytest.raises(ExtractError, match="no items schema"):
        build_extraction_schema(document)


def test_composed_schemas_are_applied_in_the_order_they_are_listed(document):
    """Later entries win the merge, so reversing them silently changes the result.

    Unobservable against the real file, where every wrapper composes exactly
    one definition — which is why a synthetic document is used here rather than
    `document`. The order still has to hold: an `allOf` that narrowed a key the
    first entry declared would be inverted by a reversal, and nothing about the
    merge would announce it.
    """
    defs = {
        "first": {"type": "object", "properties": {"v": {"type": "string"}}},
        "second": {"type": "object", "properties": {"v": {"type": "integer"}}},
    }
    node = {"allOf": [{"$ref": "#/$defs/first"}, {"$ref": "#/$defs/second"}]}

    listed = _resolve_composition(node, defs, ())
    assert [source["properties"]["v"]["type"] for source in listed] == ["string", "integer"]
    # ...and the merge consequence, so the ordering is pinned by its effect too.
    assert _flatten(node, defs, ())["properties"]["v"] == {"type": "integer"}


def test_code_assigned_experiment_keys_are_pruned_only_at_the_experiment_level():
    """`notes` on an experiment is the labeller's; `notes` nested elsewhere is not.

    The path check is what keeps the prune scoped. Without it any future
    property named `notes` or `experiment_id` at any depth would be silently
    dropped from the request schema — the model would never be asked for it and
    nothing would say so. Both key sets are read from the module, so a key added
    there is covered here without editing this test.
    """
    for name in CODE_ASSIGNED_EXPERIMENT_KEYS:
        assert _is_code_assigned(name, ()) is True
        assert _is_code_assigned(name, ("intervention",)) is False
        assert _is_code_assigned(name, ("lifespan_effect", "direction")) is False

    # A claim key is the caller's wherever it appears — no path condition.
    for name in CODE_ASSIGNED_CLAIM_KEYS:
        assert _is_code_assigned(name, ()) is True
        assert _is_code_assigned(name, ("intervention", "agent")) is True


def test_load_schema_reports_a_missing_file(tmp_path):
    with pytest.raises(ExtractError, match="schema not found"):
        load_schema(tmp_path / "absent.json")


@pytest.mark.parametrize("body", ["[]", '["experiment"]', '"a string"', "42", "null"])
def test_load_schema_reports_a_top_level_that_is_not_an_object(tmp_path, body):
    """The annotation says `dict[str, Any]`, and valid JSON is not enough to earn it.

    A schema file that parses but is not an object would otherwise be returned,
    and the first thing to notice would be `.get` failing somewhere downstream.
    """
    path = tmp_path / "wrong-shape.json"
    path.write_text(body)
    with pytest.raises(ExtractError, match="JSON object at the top level"):
        load_schema(path)


def test_load_schema_reports_bad_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(ExtractError, match="not valid JSON"):
        load_schema(path)


def test_schema_version_comes_from_the_file(document):
    assert schema_version(document) == json.loads(SCHEMA_PATH.read_text())["x-schema-version"]


def test_schema_version_raises_when_absent(document):
    del document["x-schema-version"]
    with pytest.raises(ExtractError, match="x-schema-version"):
        schema_version(document)


@pytest.mark.parametrize("path", sorted(GOLD_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_gold_records_satisfy_the_provenance_invariant(path, document):
    """The invariant this package enforces is the one the humans already follow.

    If `check_provenance` disagreed with a hand-labelled record, the extractor
    would be enforcing a rule the gold set does not, and every eval built on it
    would measure the wrong thing.
    """
    record = json.loads(path.read_text())
    validate_record(record, document)
    check_provenance(record)


def test_validate_record_reports_every_error(document):
    record = {"schema_version": "nope", "paper": {}, "experiments": []}
    with pytest.raises(RecordValidationError) as raised:
        validate_record(record, document)
    message = str(raised.value)
    assert "schema_version" in message
    assert "experiments" in message


def test_a_validation_error_names_its_location_with_array_indices():
    """`experiments[0].organism.value`, not `experiments.0.organism.value`.

    The path is the only thing in the message that says *which* experiment
    failed, and a bracketed index is what makes it copy-pasteable into `jq` and
    readable in a record with several experiments. Built from a real gold record
    and the real validator so the shape is the one an operator would actually
    see.
    """
    record = json.loads((GOLD_DIR / "harrison2009.json").read_text())
    record["experiments"][0]["organism"]["value"] = "Mus musculus, not in the enum"

    with pytest.raises(RecordValidationError) as raised:
        validate_record(record, load_schema())

    assert "experiments[0].organism.value" in str(raised.value)


def test_check_provenance_rejects_a_quote_for_an_absent_value():
    record = {"experiments": [{"sex": {
        "value": "not_reported",
        "source_quote": "Male mice were used.",
        "confidence": "high",
        "extracted_from": "abstract",
    }}]}
    with pytest.raises(RecordValidationError, match="nothing to quote"):
        check_provenance(record)


def test_check_provenance_rejects_a_value_with_no_quote():
    record = {"experiments": [{"organism": {
        "value": "M. musculus",
        "source_quote": None,
        "confidence": "high",
        "extracted_from": "abstract",
    }}]}
    with pytest.raises(RecordValidationError, match="no source_quote"):
        check_provenance(record)


@pytest.mark.parametrize("blank", ["   ", "\n", "\t"])
def test_check_provenance_rejects_a_whitespace_only_quote(blank):
    """The one quote that would otherwise satisfy both halves of the invariant.

    A truthy string, so the "value with no quote" check used to pass it, and
    `check_quotes_verbatim` collapses it to the empty string — which is a
    substring of every text there is. It would verify against a paper it was
    never read from, which is the exact failure the verbatim check exists to
    make impossible.
    """
    record = {"experiments": [{"organism": {
        "value": "M. musculus",
        "source_quote": blank,
        "confidence": "high",
        "extracted_from": "abstract",
    }}]}
    with pytest.raises(RecordValidationError, match="no source_quote"):
        check_provenance(record)


PAPER_TEXT = "Rapamycin extended median survival by 14%.\nAll mice were UM-HET3."


def quoted(quote: str | None) -> dict[str, Any]:
    """A one-claim record carrying `quote`."""
    return {"experiments": [{"organism": {
        "value": "M. musculus",
        "source_quote": quote,
        "confidence": "high",
        "extracted_from": "abstract",
    }}]}


def test_a_quote_that_is_in_the_text_passes():
    check_quotes_verbatim(quoted("extended median survival by 14%"), PAPER_TEXT)


def test_a_quote_spanning_a_line_break_passes():
    """The text wraps; the quote does not. No character differs."""
    check_quotes_verbatim(quoted("by 14%. All mice were UM-HET3."), PAPER_TEXT)


def test_a_fabricated_quote_names_the_claim_and_the_closest_window():
    with pytest.raises(RecordValidationError) as raised:
        check_quotes_verbatim(quoted("Naked mole rats lived in colonies."), PAPER_TEXT)

    message = str(raised.value)
    assert "experiments[0].organism" in message
    assert "Naked mole rats" in message
    assert "closest:" in message


@pytest.mark.parametrize(
    ("altered", "difference"),
    [
        ("EXTENDED MEDIAN SURVIVAL BY 14%", "case"),
        ("Extended median survival by 14%", "case, one letter"),
        ("extended median survival by 14%!", "punctuation"),
        ("extended median survival by 14﹪", "unicode look-alike percent sign"),
        ("extended median survivаl by 14%", "unicode look-alike Cyrillic a"),
    ],
)
def test_a_quote_differing_only_in_case_punctuation_or_unicode_is_not_verbatim(
    altered, difference
):
    """"Nothing else is normalised" — whitespace is the only forgiven difference.

    Each of the three dimensions the docstring names gets a case, because
    widening the comparison is a one-line change that reads like a kindness:
    `.lower()` on both sides passes every other verbatim test in this file, and
    then a quote the paper never wrote verifies against it. A fabricated
    citation is the one failure this project cannot absorb, and a fabrication
    that differs from the real sentence by its capitalisation is still one.
    """
    with pytest.raises(RecordValidationError, match="not verbatim"):
        check_quotes_verbatim(quoted(altered), PAPER_TEXT)


def test_the_closest_window_is_anchored_on_what_the_two_share():
    """The window points at the region the quote was nearly right about.

    That is what tells a transcription slip apart from a fabrication at a
    glance. Unanchored — a window from the top of the text — the error prints a
    region with no relation to the quote, and every near-miss looks like an
    invention.
    """
    sentence = "Rapamycin extended median survival by 14%."
    text = "A" * 300 + sentence + "B" * 300
    nearly = sentence.replace("14%", "41%")

    window = _closest_window(nearly, text)

    assert "Rapamycin extended median survival" in window
    assert "AAA" not in window and "BBB" not in window


# "Short enough to read in a terminal", stated as a number here rather than read
# off `_ellipsis`'s own default: a bound derived from the thing under test moves
# when the thing under test moves, which is exactly the failure this pins.
TERMINAL_LINE = 200


@pytest.mark.parametrize(
    "quote",
    ["x" * 5000, "Naked mole rats lived in colonies.\n" * 300, "word " * 2000],
)
def test_a_long_fabricated_quote_is_reported_as_one_short_line(quote):
    """The error names the failure; it does not reproduce the payload.

    Model output is untrusted and unbounded, so an error message built from it
    has to be bounded too. Both halves matter: one line, because a multi-line
    excerpt breaks up the surrounding message, and short, because a run over
    twenty papers should stay readable.
    """
    rendered = _ellipsis(quote)
    assert "\n" not in rendered
    assert len(rendered) <= TERMINAL_LINE

    with pytest.raises(RecordValidationError) as raised:
        check_quotes_verbatim(quoted(quote), PAPER_TEXT)
    for line in str(raised.value).splitlines():
        assert len(line) <= TERMINAL_LINE + 20, "an error line is unbounded by the payload"


def test_a_null_quote_is_not_checked_against_the_text():
    """`check_provenance` owns the absent case; this one has nothing to compare."""
    check_quotes_verbatim(quoted(None), PAPER_TEXT)


@pytest.mark.parametrize(
    "text",
    ["a  b", " a\tb\n", "a\n\n  b", "", "   ", "already collapsed"],
)
def test_whitespace_collapsing_matches_the_gold_checker(text):
    """The two must not drift into two ideas of what "verbatim" means.

    `scripts/check_gold.py` applies this comparison to the human-labelled
    records and this module applies it to the model's; a quote that passes one
    and fails the other would make the Phase 3 eval unreproducible. Duplicated
    rather than imported, because the extractor must not depend on a script
    that pulls in the PubMed and bioRxiv lookups — so the pin is this test.
    """
    assert collapse_whitespace(text) == check_gold.collapse_whitespace(text)


@pytest.mark.parametrize("path", sorted(GOLD_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_gold_quotes_pass_the_extractors_own_verbatim_check(path):
    """A record whose quotes are the text passes, trivially and by construction.

    The gold quotes are verified against the real paper by
    `scripts/check_gold.py`; here they are checked against a text built from the
    quotes themselves, which tests only that the extractor's checker accepts a
    hand-labelled record's shape — nested claims, nulls and all — rather than
    re-verifying the gold set.
    """
    record = json.loads(path.read_text())
    quotes = [
        claim["source_quote"]
        for _, claim in check_gold.iter_claims(record)
        if isinstance(claim.get("source_quote"), str)
    ]
    assert quotes, f"{path.stem} has no quotes to check"
    check_quotes_verbatim(record, "\n".join(quotes))
