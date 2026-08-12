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
    SCHEMA_PATH,
    UNSUPPORTED_KEYWORDS,
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


def test_load_schema_reports_a_missing_file(tmp_path):
    with pytest.raises(ExtractError, match="schema not found"):
        load_schema(tmp_path / "absent.json")


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
