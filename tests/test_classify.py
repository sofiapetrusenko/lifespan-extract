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
from tests.conftest import StubClient, model_response

TITLE = "Rapamycin fed late in life extends lifespan in genetically heterogeneous mice"
ABSTRACT = "Rapamycin extended median and maximal lifespan of both male and female mice."


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
