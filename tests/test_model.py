"""The model call path: key resolution, JSON repair, one retry, then raise."""

from __future__ import annotations

import anthropic
import httpx
import pytest

from extract.errors import ConfigurationError, ModelCallError, ModelResponseError
from extract.model import (
    API_KEY_ENV,
    RETRIES,
    api_key,
    call_structured,
    parse_payload,
    repair_json,
    response_text,
)
from tests.conftest import StubBlock, StubClient, StubResponse, model_response

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def api_status_error(status: int, message: str = "Overloaded") -> anthropic.APIStatusError:
    """The SDK exception an overloaded or rate-limited API raises."""
    return anthropic.APIStatusError(
        message, response=httpx.Response(status, request=REQUEST), body=None
    )


def call(client, **overrides):
    kwargs = {
        "model": "claude-haiku-4-5",
        "system": "system",
        "user": "user",
        "schema": SCHEMA,
        "max_tokens": 64,
    }
    kwargs.update(overrides)
    return call_structured(client, **kwargs)


def test_api_key_raises_when_unset(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(ConfigurationError) as raised:
        api_key()
    assert API_KEY_ENV in str(raised.value)


def test_api_key_raises_when_blank(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "   ")
    with pytest.raises(ConfigurationError):
        api_key()


def test_api_key_returns_the_value(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "sk-ant-test")
    assert api_key() == "sk-ant-test"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here is the record:\n{"a": 1}\nHope that helps.', {"a": 1}),
        ('{"a": [1, 2,],}', {"a": [1, 2]}),
        ('```\n{"a": 1,}\n```', {"a": 1}),
    ],
)
def test_repair_recovers_the_shapes_models_actually_produce(body, expected):
    assert parse_payload(body) == expected


def test_repair_leaves_clean_json_untouched():
    assert repair_json('{"a": 1}') == '{"a": 1}'


def test_repair_never_completes_a_truncated_object():
    """Closing a cut-off payload would mean inventing the part that never arrived."""
    truncated = '{"experiments": [{"organism": '
    assert "}" not in repair_json(truncated)
    with pytest.raises(ModelResponseError):
        parse_payload(truncated)


def test_parse_payload_reports_the_offending_window():
    with pytest.raises(ModelResponseError) as raised:
        parse_payload("not json at all, sorry")
    message = str(raised.value)
    assert "payload excerpt" in message
    assert "not json at all" in message


def test_parse_payload_rejects_a_json_array():
    with pytest.raises(ModelResponseError, match="expected an object"):
        parse_payload("[1, 2, 3]")


def test_happy_path_makes_one_call_and_sends_the_schema():
    client = StubClient(model_response({"ok": True}))
    assert call(client) == {"ok": True}

    (request,) = client.requests
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert request["messages"] == [{"role": "user", "content": "user"}]
    # Sampling parameters are rejected by the models this package targets.
    assert not {"temperature", "top_p", "top_k"} & set(request)


def test_repairable_payload_does_not_spend_a_retry():
    client = StubClient(model_response('```json\n{"ok": true}\n```'))
    assert call(client) == {"ok": True}
    assert len(client.requests) == 1


def test_unparseable_payload_is_retried_once(capsys):
    client = StubClient(model_response("sorry, no JSON"), model_response({"ok": True}))
    assert call(client) == {"ok": True}
    assert len(client.requests) == RETRIES + 1
    assert "retrying once" in capsys.readouterr().err


def test_two_bad_payloads_raise_with_both_excerpts():
    client = StubClient(model_response("first mess"), model_response("second mess"))
    with pytest.raises(ModelResponseError) as raised:
        call(client)
    message = str(raised.value)
    assert "attempt 1" in message and "attempt 2" in message
    assert "first mess" in message and "second mess" in message
    assert len(client.requests) == RETRIES + 1


def test_refusal_raises_without_retrying():
    client = StubClient(model_response("{}", stop_reason="refusal"))
    with pytest.raises(ModelResponseError, match="declined"):
        call(client)
    assert len(client.requests) == 1


def test_truncated_response_raises_and_names_the_cap():
    client = StubClient(model_response('{"a": 1}', stop_reason="max_tokens"))
    with pytest.raises(ModelResponseError, match="max_tokens"):
        call(client)


def test_an_api_status_error_becomes_an_extract_error_carrying_the_status():
    """A 529 must reach the caller as this package's error type, not the SDK's.

    The per-paper handler catches `ExtractError`; an `APIStatusError` escaping
    it would end a batch run on whichever paper the API happened to be
    overloaded for.
    """
    client = StubClient(api_status_error(529))
    with pytest.raises(ModelCallError) as raised:
        call(client)

    error = raised.value
    assert error.status == 529
    assert error.model == "claude-haiku-4-5"
    assert "529" in str(error) and "claude-haiku-4-5" in str(error)


def test_a_connection_error_is_wrapped_with_no_status():
    """Nothing reached a response, so there is no status to report — and none is."""
    client = StubClient(anthropic.APIConnectionError(message="refused", request=REQUEST))
    with pytest.raises(ModelCallError) as raised:
        call(client)

    assert raised.value.status is None
    assert "APIConnectionError" in str(raised.value)


def test_a_timeout_is_wrapped_too():
    client = StubClient(anthropic.APITimeoutError(request=REQUEST))
    with pytest.raises(ModelCallError):
        call(client)


def test_an_api_error_is_not_retried():
    """The SDK has already retried; a second attempt here would only double the wait."""
    client = StubClient(api_status_error(429, "Rate limited"), model_response({"ok": True}))
    with pytest.raises(ModelCallError):
        call(client)
    assert len(client.requests) == 1


def test_response_without_a_text_block_raises():
    response = StubResponse(content=[StubBlock("thinking")])
    with pytest.raises(ModelResponseError, match="no text block"):
        response_text(response, model="m")


def test_response_without_a_content_list_raises():
    with pytest.raises(ModelResponseError, match="no content list"):
        response_text(StubResponse(content=None), model="m")


def test_text_blocks_are_concatenated():
    response = StubResponse(content=[StubBlock("text", "a"), StubBlock("text", "b")])
    assert response_text(response, model="m") == "a\nb"


def test_extra_calls_are_a_test_failure_not_a_silent_reuse():
    client = StubClient(model_response("mess"), model_response("mess"))
    with pytest.raises(ModelResponseError):
        call(client)
    with pytest.raises(AssertionError, match="unexpected extra model call"):
        client.messages.create(model="claude-haiku-4-5")
