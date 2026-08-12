"""Extraction: record assembly, provenance, identity, and every failure path.

Records are checked against `schema/experiment.schema.json` with a validator
built here, independently of the one the extractor uses — a test that trusted
the code's own validator would pass whatever that validator happened to accept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from extract.errors import ExtractError, ModelResponseError, RecordValidationError
from extract.extract import (
    ABSTRACT,
    EXTRACTED_FROM,
    EXTRACTION_MODEL,
    FULL_TEXT,
    _experiment_id,
    extract_record,
    extraction_schema,
)
from extract.schema import CLAIM_KEYS, SCHEMA_PATH
from ingest.models import RawPaper
from tests.conftest import QUOTE, StubClient, claim, experiment_payload, model_response

TEXT = (
    "Rapamycin fed beginning at 600 days of age extended median survival by 14% in "
    "genetically heterogeneous mice."
)

VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "gold"

# Every gold `experiment_id` the generator does not reproduce, as
# `stem -> ((gold_id, generated_id), ...)`. Fifteen of the gold set's
# twenty-six ids, from two systematic differences:
#
# 1. **Disambiguation.** One paper reporting the same (organism, agent) pair
#    twice is `-male`/`-female` or `-low-dose`/`-high-dose` by hand, and `-2`,
#    `-3` here. The generator has no way to know which axis split the arms.
# 2. **Agent naming.** The labeller normalises what the paper printed
#    (`nordihydroguaiaretic acid (NDGA)` -> `ndga`); `_slug` takes the value as
#    it stands.
#
# This is a pin, not an endorsement: the generator follows the convention the
# schema documents, the gold set follows another, and settling which one wins
# is the human's call — see NOTES.md (2026-08-12). Pinned so that a change on
# either side breaks a test instead of passing quietly, because Phase 3 aligns
# gold against extracted output and would otherwise inherit the gap silently.
KNOWN_ID_DIVERGENCES: dict[str, tuple[tuple[str, str], ...]] = {
    "calubag2025": (
        ("calubag2025-mmusculus-valine-restriction-male",
         "calubag2025-mmusculus-valine-restriction"),
        ("calubag2025-mmusculus-valine-restriction-female",
         "calubag2025-mmusculus-valine-restriction-2"),
    ),
    "harrison2009": (
        ("harrison2009-mmusculus-rapamycin-male", "harrison2009-mmusculus-rapamycin"),
        ("harrison2009-mmusculus-rapamycin-female", "harrison2009-mmusculus-rapamycin-2"),
    ),
    "kenyon1993": (
        ("kenyon1993-celegans-daf-2", "kenyon1993-celegans-daf-2-mutation"),
    ),
    "lakowski1998": (
        ("lakowski1998-celegans-eat-2",
         "lakowski1998-celegans-eat-2-mutation-reference-allele-ad465"),
    ),
    "martinmontalvo2013": (
        ("martinmontalvo2013-mmusculus-metformin-low-dose",
         "martinmontalvo2013-mmusculus-metformin"),
        ("martinmontalvo2013-mmusculus-metformin-high-dose",
         "martinmontalvo2013-mmusculus-metformin-2"),
    ),
    "miller2011": (
        ("miller2011-mmusculus-rapamycin-male", "miller2011-mmusculus-rapamycin"),
        ("miller2011-mmusculus-rapamycin-female", "miller2011-mmusculus-rapamycin-2"),
    ),
    "strong2016": (
        ("strong2016-mmusculus-ndga", "strong2016-mmusculus-nordihydroguaiaretic-acid-ndga"),
        ("strong2016-mmusculus-acarbose-male", "strong2016-mmusculus-acarbose"),
        ("strong2016-mmusculus-acarbose-female", "strong2016-mmusculus-acarbose-2"),
        ("strong2016-mmusculus-metformin-rapamycin",
         "strong2016-mmusculus-metformin-plus-rapamycin"),
        ("strong2016-mmusculus-udca", "strong2016-mmusculus-ursodeoxycholic-acid-udca"),
    ),
}


def paper(**overrides: Any) -> RawPaper:
    fields: dict[str, Any] = {
        "source": "pubmed",
        "source_id": "19587680",
        "pmid": "19587680",
        "doi": "10.1038/nature08221",
        "title": "Rapamycin fed late in life extends lifespan",
        "abstract": TEXT,
        "year": 2009,
        "first_author": "Harrison",
    }
    fields.update(overrides)
    return RawPaper.build(**fields)


def payload(*experiments: dict[str, Any]) -> dict[str, Any]:
    return {"experiments": list(experiments) or [experiment_payload()]}


def claims(node: Any) -> list[dict[str, Any]]:
    """Return every claim wrapper in a record."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if CLAIM_KEYS <= set(node):
            return [node]
        for child in node.values():
            found += claims(child)
    elif isinstance(node, list):
        for child in node:
            found += claims(child)
    return found


def test_the_provenance_vocabulary_is_the_schema_s():
    """A drift guard: the stamped values are the ones the schema allows."""
    document = json.loads(SCHEMA_PATH.read_text())
    assert list(EXTRACTED_FROM) == document["$defs"]["extracted_from"]["enum"]


def test_a_record_validates_against_the_real_schema():
    client = StubClient(model_response(payload()))
    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    assert record["schema_version"] == json.loads(SCHEMA_PATH.read_text())["x-schema-version"]
    assert record["paper"] == {
        "doi": "10.1038/nature08221",
        "title": "Rapamycin fed late in life extends lifespan",
        "year": 2009,
        "source": "pubmed",
        "pmid": "19587680",
    }


def test_the_request_uses_the_extraction_model_and_the_derived_schema():
    client = StubClient(model_response(payload()))
    extract_record(paper(), TEXT, client=client)

    (request,) = client.requests
    assert request["model"] == EXTRACTION_MODEL
    assert request["output_config"]["format"]["schema"] == extraction_schema()
    assert TEXT in request["messages"][0]["content"]


def test_paper_metadata_is_never_taken_from_the_model():
    """A model that volunteers bibliographic data cannot influence the record."""
    client = StubClient(
        model_response({"experiments": [experiment_payload()], "paper": {"doi": "10.0000/fake"}})
    )
    record = extract_record(paper(), TEXT, client=client)
    assert record["paper"]["doi"] == "10.1038/nature08221"


def test_extracted_from_is_stamped_from_the_caller_not_the_model():
    client = StubClient(model_response(payload()))
    record = extract_record(paper(), TEXT, client=client, extracted_from=ABSTRACT)

    stamped = {c["extracted_from"] for c in claims(record)}
    assert stamped == {ABSTRACT}
    # Every claim in the schema, not just the top-level ones: 6 on the
    # experiment, 4 under intervention, 5 under lifespan_effect.
    assert len(claims(record)) == 15


def test_full_text_provenance_is_stamped_when_that_is_what_was_read():
    client = StubClient(model_response(payload()))
    record = extract_record(paper(), TEXT, client=client, extracted_from=FULL_TEXT)
    assert {c["extracted_from"] for c in claims(record)} == {FULL_TEXT}


def test_a_model_supplied_provenance_claim_raises():
    """The request schema forbids the key; if it appears, the response is wrong."""
    rogue = experiment_payload()
    rogue["organism"]["extracted_from"] = "full_text"
    client = StubClient(model_response(payload(rogue)))

    with pytest.raises(ModelResponseError, match="extracted_from"):
        extract_record(paper(), TEXT, client=client)


def test_an_unknown_provenance_argument_is_rejected():
    with pytest.raises(ValueError, match="extracted_from"):
        extract_record(paper(), TEXT, client=StubClient(), extracted_from="pdf")


def test_empty_text_is_refused_before_any_call():
    client = StubClient()
    with pytest.raises(ExtractError, match="no text was supplied"):
        extract_record(paper(), "  ", client=client)
    assert client.requests == []


def test_a_multi_organism_paper_yields_one_record_per_organism():
    client = StubClient(
        model_response(
            payload(
                experiment_payload(organism="C. elegans", agent="spermidine"),
                experiment_payload(organism="other", species="S. cerevisiae", agent="spermidine"),
                experiment_payload(
                    organism="other", species="D. melanogaster", agent="spermidine"
                ),
            )
        )
    )
    record = extract_record(paper(first_author="Eisenberg", year=2009), TEXT, client=client)

    VALIDATOR.validate(record)
    assert [e["experiment_id"] for e in record["experiments"]] == [
        "eisenberg2009-celegans-spermidine",
        "eisenberg2009-scerevisiae-spermidine",
        "eisenberg2009-dmelanogaster-spermidine",
    ]


def test_two_interventions_in_one_organism_yield_two_records():
    client = StubClient(
        model_response(
            payload(
                experiment_payload(agent="rapamycin"),
                experiment_payload(agent="metformin"),
            )
        )
    )
    record = extract_record(paper(), TEXT, client=client)
    assert [e["experiment_id"] for e in record["experiments"]] == [
        "harrison2009-mmusculus-rapamycin",
        "harrison2009-mmusculus-metformin",
    ]


def test_a_repeated_pair_takes_the_schema_s_numeric_suffix_not_the_gold_set_s_wording():
    """Pins the convention `schema/experiment.schema.json` documents: `-2`, `-3`.

    Not the convention `data/gold/` actually uses. Where one paper reports the
    same (organism, agent) pair twice, the human labeller disambiguates
    semantically — `-male`, `-female`, `-low-dose` — and no gold record carries
    a numeric suffix. The generator cannot reproduce that from the payload, and
    which convention wins is the human's call, not this test's:
    `test_generated_ids_match_gold_except_where_pinned` measures the gap, and
    NOTES.md (2026-08-12) records it as an open question for Phase 3.
    """
    client = StubClient(
        model_response(payload(experiment_payload(), experiment_payload(), experiment_payload()))
    )
    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    assert [e["experiment_id"] for e in record["experiments"]] == [
        "harrison2009-mmusculus-rapamycin",
        "harrison2009-mmusculus-rapamycin-2",
        "harrison2009-mmusculus-rapamycin-3",
    ]


def test_non_ascii_agents_still_produce_a_valid_identifier():
    client = StubClient(model_response(payload(experiment_payload(agent="17-α-estradiol"))))
    record = extract_record(paper(first_author="Strong", year=2016), TEXT, client=client)

    VALIDATOR.validate(record)
    assert record["experiments"][0]["experiment_id"] == "strong2016-mmusculus-17-estradiol"


def generated_ids(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Return `(gold_id, generated_id)` for every experiment in a gold record.

    The author-year stem is read off the gold id rather than regenerated: gold
    files carry no `first_author`, and taking it from the id keeps the
    comparison on the two segments that actually differ — the organism/agent
    slug and the disambiguation tail.
    """
    year = document["paper"]["year"]
    taken: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for experiment in document["experiments"]:
        gold_id = experiment["experiment_id"]
        stem = gold_id.split("-")[0]
        stub = RawPaper.build(
            source="pubmed",
            source_id=stem,
            title=document["paper"]["title"],
            year=year,
            first_author=stem[: -len(str(year))],
        )
        generated = _experiment_id(stub, experiment, taken)
        taken.add(generated)
        pairs.append((gold_id, generated))
    return pairs


@pytest.mark.parametrize("path", sorted(GOLD_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_generated_ids_match_gold_except_where_pinned(path):
    """Measure the generator against the ids Phase 3 will have to align on.

    The schema calls `experiment_id` a stable identity "for eval alignment" and
    says Phase 2 generates it from the same convention the gold set was labelled
    by. It does not, for 15 of 26 records — see `KNOWN_ID_DIVERGENCES` for the
    two reasons and NOTES.md for the open question. This test asserts the
    divergence set exactly, in both directions: a new mismatch fails, and so
    does one that quietly goes away.
    """
    document = json.loads(path.read_text())
    divergent = tuple(
        (gold_id, generated)
        for gold_id, generated in generated_ids(document)
        if gold_id != generated
    )
    assert divergent == KNOWN_ID_DIVERGENCES.get(path.stem, ())


def test_not_reported_survives_extraction_unembellished():
    """The honesty case: absent data stays absent and carries no invented quote."""
    sparse = experiment_payload()
    sparse["lifespan_effect"]["median_change_pct"] = claim(None)
    sparse["lifespan_effect"]["p_value"] = claim(None)
    client = StubClient(model_response(payload(sparse)))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)

    experiment = record["experiments"][0]
    assert experiment["sex"]["value"] == "not_reported"
    assert experiment["sex"]["source_quote"] is None
    assert experiment["strain"]["value"] == "not_reported"
    assert experiment["mechanism"]["value"] is None
    assert experiment["lifespan_effect"]["median_change_pct"]["value"] is None
    assert experiment["lifespan_effect"]["p_value"]["value"] is None
    assert all(c["source_quote"] is None for c in claims(experiment) if c["value"] is None)


def test_a_quote_attached_to_an_absent_value_raises():
    dishonest = experiment_payload()
    dishonest["sex"] = {
        "value": "not_reported",
        "source_quote": "Male and female mice were used.",
        "confidence": "high",
    }
    client = StubClient(model_response(payload(dishonest)))

    with pytest.raises(RecordValidationError, match="nothing to quote"):
        extract_record(paper(), TEXT, client=client)


def test_a_value_without_a_quote_raises():
    unsupported = experiment_payload()
    unsupported["mechanism"] = {
        "value": "mTOR inhibition",
        "source_quote": None,
        "confidence": "low",
    }
    client = StubClient(model_response(payload(unsupported)))

    with pytest.raises(RecordValidationError, match="no source_quote"):
        extract_record(paper(), TEXT, client=client)


def test_the_stub_quote_is_verbatim_in_the_stub_text():
    """The premise of every other test here: the stubs are honest by default.

    If this drifts, the fabrication tests below stop testing fabrication and
    start testing a broken fixture.
    """
    assert QUOTE in TEXT


def test_a_fabricated_quote_is_rejected():
    """Schema-valid and provenance-consistent, and still a fabricated citation."""
    invented = experiment_payload()
    invented["strain"] = claim(
        "UM-HET3", "Naked mole rats were housed in colonies at 30 degrees."
    )
    client = StubClient(model_response(payload(invented)))

    with pytest.raises(RecordValidationError) as raised:
        extract_record(paper(), TEXT, client=client)

    message = str(raised.value)
    assert "experiments[0].strain" in message
    assert "not verbatim" in message
    assert "Naked mole rats" in message


def test_a_fabricated_quote_under_a_number_is_rejected_too():
    """The traversal reaches nested claims, where the invented figures live."""
    invented = experiment_payload()
    invented["lifespan_effect"]["median_change_pct"] = claim(
        99.0, "Lifespan doubled in every cohort we examined."
    )
    client = StubClient(model_response(payload(invented)))

    with pytest.raises(RecordValidationError, match="median_change_pct"):
        extract_record(paper(), TEXT, client=client)


def test_a_quote_wrapped_across_lines_is_accepted():
    """A structured abstract wraps where the paper does not; that is not a difference.

    Whitespace is the only thing collapsed, matching scripts/check_gold.py —
    every other character is compared as it stands.
    """
    wrapped = experiment_payload()
    wrapped["strain"] = claim("UM-HET3", QUOTE.replace(" ", "\n   ", 1))
    client = StubClient(model_response(payload(wrapped)))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)


def test_a_quote_differing_by_one_character_is_not_whitespace_forgiven():
    """The collapse must not become a general fuzzy match: 14% is not 41%."""
    altered = experiment_payload()
    altered["strain"] = claim("UM-HET3", QUOTE.replace("14%", "41%"))
    client = StubClient(model_response(payload(altered)))

    with pytest.raises(RecordValidationError, match="not verbatim"):
        extract_record(paper(), TEXT, client=client)


def test_a_value_the_api_subset_cannot_constrain_is_still_validated():
    """p_value's pattern survives only in the real schema; check it is enforced."""
    malformed = experiment_payload()
    malformed["lifespan_effect"]["p_value"] = claim("not significant")
    client = StubClient(model_response(payload(malformed)))

    with pytest.raises(RecordValidationError, match="p_value"):
        extract_record(paper(), TEXT, client=client)


def test_an_out_of_vocabulary_value_is_rejected():
    invented = experiment_payload(direction="slight_increase")
    client = StubClient(model_response(payload(invented)))

    with pytest.raises(RecordValidationError, match="direction"):
        extract_record(paper(), TEXT, client=client)


def test_a_fenced_payload_is_repaired_without_a_retry():
    body = "```json\n" + json.dumps(payload()) + "\n```"
    client = StubClient(model_response(body))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)
    assert len(client.requests) == 1


def test_an_unparseable_payload_is_retried_once_then_succeeds():
    client = StubClient(model_response("I could not find any experiments."), model_response(payload()))
    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    assert len(client.requests) == 2


def test_two_unparseable_payloads_raise_and_produce_no_record():
    client = StubClient(model_response("first mess"), model_response("second mess"))
    with pytest.raises(ModelResponseError, match="2 attempt"):
        extract_record(paper(), TEXT, client=client)


def test_an_empty_experiments_array_is_reported_not_papered_over():
    client = StubClient(model_response({"experiments": []}))
    with pytest.raises(ExtractError, match="returned no experiments"):
        extract_record(paper(), TEXT, client=client)


def test_a_payload_without_experiments_raises():
    client = StubClient(model_response({"records": []}))
    with pytest.raises(ModelResponseError, match="experiments"):
        extract_record(paper(), TEXT, client=client)


@pytest.mark.parametrize("missing", ["doi", "year"])
def test_missing_paper_identity_raises_rather_than_being_invented(missing):
    client = StubClient(model_response(payload()))
    with pytest.raises(ExtractError, match=f"paper.{missing} is missing"):
        extract_record(paper(**{missing: None}), TEXT, client=client)


def test_a_paper_with_no_first_author_cannot_be_named_and_says_so():
    client = StubClient(model_response(payload()))
    with pytest.raises(ExtractError, match="experiment_id convention"):
        extract_record(paper(first_author=None), TEXT, client=client)


def test_an_experiment_with_no_agent_value_raises():
    nameless = experiment_payload()
    nameless["intervention"]["agent"] = claim(None)
    client = StubClient(model_response(payload(nameless)))

    with pytest.raises(ModelResponseError, match="intervention.agent.value"):
        extract_record(paper(), TEXT, client=client)


def test_an_other_organism_without_a_species_still_gets_an_identifier():
    unnamed = experiment_payload(organism="other", species=None)
    client = StubClient(model_response(payload(unnamed)))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)
    assert record["experiments"][0]["experiment_id"] == "harrison2009-other-rapamycin"
