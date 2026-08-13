"""The cheap gate: what it decides, and what it refuses to guess at."""

from __future__ import annotations

import pytest

from extract.classify import (
    CLASSIFIER_MODEL,
    CONFIDENCE_LEVELS,
    Classification,
    classify,
)
from extract.errors import ExtractError, ModelResponseError
from extract.extract import EXTRACTION_MODEL
from tests.conftest import StubClient, model_response

TITLE = "Rapamycin fed late in life extends lifespan in genetically heterogeneous mice"
ABSTRACT = "Rapamycin extended median and maximal lifespan of both male and female mice."

# Anthropic's published input price per million tokens, read on 2026-08-12.
#
# A static, hand-maintained claim, and written down as one. There is no pricing
# endpoint this package may call — a unit test that made a network request
# would be worse than a stale number — so the honest form is the figures plus
# the date they were read, correctable by hand when they move. What the test
# below actually asserts is the *ordering*, which is what PLAN.md's cost
# cascade claims and is far more stable than the figures themselves.
MODEL_INPUT_PRICE_PER_MTOK = {
    "claude-haiku-4-5": 1.00,
    "claude-sonnet-5": 3.00,
    "claude-opus-5": 5.00,
}


def decision(relevant=True, reason="Rapamycin was fed and lifespan measured.", confidence="high"):
    return {
        "reports_lifespan_intervention": relevant,
        "reason": reason,
        "confidence": confidence,
    }


def test_a_positive_paper_passes_the_gate():
    client = StubClient(model_response(decision()))
    result = classify(TITLE, ABSTRACT, client=client)

    assert result == Classification(
        relevant=True,
        reason="Rapamycin was fed and lifespan measured.",
        confidence="high",
        model=CLASSIFIER_MODEL,
    )


def test_a_negative_paper_is_screened_out_with_its_reason():
    client = StubClient(
        model_response(decision(relevant=False, reason="Review article.", confidence="medium"))
    )
    result = classify(TITLE, ABSTRACT, client=client)

    assert result.relevant is False
    assert result.reason == "Review article."
    assert result.confidence == "medium"


def test_the_request_uses_the_cheap_model_and_carries_the_text():
    client = StubClient(model_response(decision()))
    classify(TITLE, ABSTRACT, client=client)

    (request,) = client.requests
    assert request["model"] == CLASSIFIER_MODEL
    user = request["messages"][0]["content"]
    assert TITLE in user and ABSTRACT in user
    schema = request["output_config"]["format"]["schema"]
    assert schema["properties"]["confidence"]["enum"] == list(CONFIDENCE_LEVELS)


def test_the_gate_runs_on_a_strictly_cheaper_model_than_the_extractor():
    """The cascade is the deliverable, and the wiring test does not pin it.

    `test_the_request_uses_the_cheap_model_and_carries_the_text` asserts the
    request carries `CLASSIFIER_MODEL`, which says nothing about that
    constant's value: setting `CLASSIFIER_MODEL = "claude-opus-5"`, or simply
    setting it equal to `EXTRACTION_MODEL`, leaves that test green and deletes
    PLAN.md Phase 2's "cheap-model gate ... (cost cascade)" outright. This
    asserts the two values instead — two different models, the classifier the
    cheaper of them.
    """
    assert CLASSIFIER_MODEL != EXTRACTION_MODEL, (
        "the gate and the extractor run on one model, so there is no cost "
        "cascade — every screened-out paper is now billed at extraction rates."
    )
    for constant, model in (
        ("CLASSIFIER_MODEL", CLASSIFIER_MODEL),
        ("EXTRACTION_MODEL", EXTRACTION_MODEL),
    ):
        assert model in MODEL_INPUT_PRICE_PER_MTOK, (
            f"{constant} is {model!r}, which has no entry in "
            "MODEL_INPUT_PRICE_PER_MTOK. Add its published price and the date "
            "you read it, so the cascade stays a checked claim rather than an "
            "assumed one."
        )
    assert (
        MODEL_INPUT_PRICE_PER_MTOK[CLASSIFIER_MODEL]
        < MODEL_INPUT_PRICE_PER_MTOK[EXTRACTION_MODEL]
    ), f"{CLASSIFIER_MODEL!r} is not cheaper than {EXTRACTION_MODEL!r}"


def test_a_paper_with_no_abstract_is_never_judged_from_its_title():
    client = StubClient()  # any call at all fails the test
    with pytest.raises(ExtractError, match="no text was supplied"):
        classify(TITLE, "   ", client=client)
    assert client.requests == []


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"reason": "r", "confidence": "high"}, "reports_lifespan_intervention"),
        (decision(relevant="yes"), "reports_lifespan_intervention"),
        (decision(reason="  "), "no non-empty 'reason'"),
        (decision(confidence="very"), "confidence"),
        ({}, "reports_lifespan_intervention"),
    ],
)
def test_a_malformed_decision_raises_rather_than_defaulting(payload, match):
    """A missing decision must not become a quiet False — that drops papers."""
    client = StubClient(model_response(payload))
    with pytest.raises(ModelResponseError, match=match):
        classify(TITLE, ABSTRACT, client=client)


def test_a_malformed_decision_is_not_retried():
    """Repair and retry cover unparseable JSON, not a well-formed wrong answer."""
    client = StubClient(model_response(decision(confidence="very")))
    with pytest.raises(ModelResponseError):
        classify(TITLE, ABSTRACT, client=client)
    assert len(client.requests) == 1


def test_an_unparseable_response_is_repaired_and_retried():
    client = StubClient(model_response("no JSON here"), model_response(decision()))
    assert classify(TITLE, ABSTRACT, client=client).relevant is True
    assert len(client.requests) == 2
