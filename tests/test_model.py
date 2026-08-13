"""The model call path: key resolution, JSON repair, one retry, then raise."""

from __future__ import annotations

import anthropic
import httpx
import pytest

from extract.errors import (
    ConfigurationError,
    ExtractError,
    ModelCallError,
    ModelResponseError,
)
from extract.model import (
    API_KEY_ENV,
    RETRIES,
    api_key,
    call_structured,
    parse_payload,
    repair_json,
    response_text,
)
from ingest.errors import DEFAULT_RADIUS
from tests.conftest import StubBlock, StubClient, StubResponse, model_response

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def api_status_error(status: int, message: str = "Overloaded") -> anthropic.APIStatusError:
    """The SDK exception an overloaded or rate-limited API raises."""
    return anthropic.APIStatusError(
        message, response=httpx.Response(status, request=REQUEST), body=None
    )


def sdk_errors_outside_the_api_error_subtree() -> list[type[BaseException]]:
    """Every exported SDK exception that is an `AnthropicError` but not an `APIError`.

    Read off the installed `anthropic` package rather than written down here. A
    hand-written list would be the same failure one level up — it can be missing
    exactly the type the code under test is missing, which is how the hole this
    covers survived eight review passes. Deduplicated by identity because one
    class can be exported under more than one name.
    """
    seen: dict[int, type[BaseException]] = {}
    for obj in vars(anthropic).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, anthropic.AnthropicError)
            and not issubclass(obj, anthropic.APIError)
        ):
            seen[id(obj)] = obj
    return sorted(seen.values(), key=lambda cls: cls.__name__)


def sdk_error_cases() -> list[tuple[BaseException, int | None]]:
    """Each derived SDK exception paired with the status `from_api_error` owes it.

    One bare instance per type, plus — for a type whose constructor takes a
    `status_code` — a second carrying HTTP 401. The second case is the point:
    `WorkloadIdentityError` declares `status_code` and the SDK's token-exchange
    raise site fills it from the response, so "these errors have no status" is
    false for it, and a suite that only ever built `error_type("boom")` was
    asserting a property of the fixture rather than of the type. The expected
    status is written down beside the instance rather than read back off it, so
    the assertion cannot agree with `from_api_error` by mirroring it.
    """
    cases: list[tuple[BaseException, int | None]] = []
    for error_type in sdk_errors_outside_the_api_error_subtree():
        cases.append((error_type("boom"), None))
        carrying = _instance_with_status(error_type, 401)
        if carrying is not None:
            cases.append((carrying, 401))
    return cases


def _instance_with_status(error_type: type[BaseException], status: int) -> BaseException | None:
    """Return `error_type("boom", status_code=status)`, or None if there is no such instance.

    Probed by constructing one rather than by reading the signature, because
    what matters is whether an instance that reports a status can exist —
    that attribute is what `from_api_error` reads. A type that refuses the
    keyword (`AnthropicError` defines no `__init__` of its own) or takes it
    without storing it contributes only its bare, statusless case.
    """
    try:
        instance = error_type("boom", status_code=status)
    except TypeError:
        return None
    return instance if getattr(instance, "status_code", None) == status else None


SDK_ERROR_CASES = sdk_error_cases()

# `pytest` hands an `ids` callable one argvalue at a time, which cannot name a
# case built from both, so the ids are built from the pairs here.
SDK_ERROR_IDS = [f"{type(exc).__name__}-status-{status}" for exc, status in SDK_ERROR_CASES]


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


@pytest.mark.parametrize(
    "body",
    [
        '{"a": 1}',
        # A comma, which the trailing-comma rule matches on.
        '{"a": 1, "b": 2}',
        '{"experiments": [{"organism": "M. musculus", "sample_size": 1901}]}',
        # A comma inside a string literal, and braces inside one.
        '{"quote": "survival rose, then fell"}',
        '{"quote": "the set {a, b} was dosed"}',
        # No braces at all, so the slice never runs and only the substitutions
        # could touch it. This is the shape the `repair` -> `raise` path ends on.
        "sorry, no JSON here",
        "",
    ],
)
def test_repair_leaves_untouched_what_it_cannot_or_need_not_fix(body):
    """"A body it cannot fix comes back unchanged" — its own words.

    The single clean object this used to check is the one input none of the
    three rules can reach: no fence, no prose, no comma. So it could not fail
    for a rule that rewrites commas, or appends, or trims something it should
    not. These carry the features each rule matches on, plus the brace-less
    body the raise path actually ends on — a repair that mangles that one
    changes the excerpt the operator is shown for every unparseable response.
    """
    assert repair_json(body) == body


@pytest.mark.parametrize(
    "body",
    ['```json\n{"a": 1}\n```', 'Here is the record:\n{"a": 1,}\nHope that helps.'],
)
def test_repair_is_idempotent(body):
    """A repaired body needs no repair: the second pass is the one that must be a no-op."""
    once = repair_json(body)
    assert repair_json(once) == once


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


# --- numbers JSON cannot express ---
#
# Python's decoder accepts the bare `NaN`, `Infinity` and `-Infinity` tokens as
# an extension, and lets an over-large literal overflow to an infinity. Nothing
# downstream objects: `jsonschema` calls the result a valid `number`, and
# `json.dumps` writes the token straight back out. What lands in a record file
# is then something `JSON.parse` throws on, serde rejects, and `jq` silently
# rewrites to `null` — a bogus number becoming a fabricated absence, which is
# the `not_reported` rule running backwards.


@pytest.mark.parametrize(
    ("body", "token"),
    [
        ('{"median_change_pct": NaN}', "NaN"),
        ('{"median_change_pct": Infinity}', "Infinity"),
        ('{"median_change_pct": -Infinity}', "-Infinity"),
        # Syntactically a JSON number, and `float()` overflows it to an
        # infinity, so the constant hook never sees it. Same bad value.
        ('{"median_change_pct": 1e400}', "1e400"),
        ('{"median_change_pct": -1e400}', "-1e400"),
    ],
)
def test_a_number_json_cannot_express_is_refused_with_the_payload(body, token):
    with pytest.raises(ModelResponseError) as raised:
        parse_payload(body)

    message = str(raised.value)
    assert token in message
    assert "payload excerpt" in message
    assert body in message


# --- an integer literal Python itself will not convert ---
#
# CPython caps integer-string conversion at 4300 digits and raises a plain
# `ValueError` past it. The decoder's default `parse_int` is `int`, so that
# `ValueError` comes out of `json.loads` as neither a `JSONDecodeError` nor an
# `ExtractError` — which is to say, out of the model layer entirely, past the
# retry and past the per-paper handler in `extract/cli.py`. The contract these
# pin is `call_structured`'s: an untrusted payload leaves it as an
# `ExtractError`, after one retry, carrying a bounded window of what arrived.

OVERSIZED_DIGITS = 5000
CONVERTIBLE_DIGITS = 4000


def test_an_integer_literal_python_refuses_is_refused_as_an_extract_error():
    body = '{"median_change_pct": ' + "9" * OVERSIZED_DIGITS + "}"
    client = StubClient(model_response(body), model_response(body))

    with pytest.raises(ModelResponseError) as raised:
        call(client)

    error = raised.value
    assert isinstance(error, ExtractError)
    message = str(error)
    # Retried like any other unparseable payload, then raised with both attempts.
    assert len(client.requests) == RETRIES + 1
    assert "attempt 1" in message and "attempt 2" in message
    # And with a *window* of the payload, not the payload: the excerpt is
    # bounded, so a five-thousand-digit number cannot land in a log whole.
    assert "payload excerpt" in message
    assert "median_change_pct" in message
    assert "9" * OVERSIZED_DIGITS not in message


def test_an_integer_literal_python_accepts_still_parses_as_that_integer():
    """The other half of the premise: the hook refuses exactly what `int` does."""
    digits = "9" * CONVERTIBLE_DIGITS
    assert parse_payload('{"n": ' + digits + "}") == {"n": int(digits)}


# --- the excerpt window is centred on the failure, not on the top ---
#
# `ingest.errors.excerpt` takes a 200-character radius: with `position=None` it
# returns text[0:400], and with a position it returns text[pos-200:pos+200]. On
# a payload under 400 characters those two are the same string, which is how
# every excerpt assertion above passes with either. These payloads are long
# enough that the windows do not overlap — the marker beside the failure
# appears only in the centred one, the marker at the head only in the other —
# so setting either call site's `position=` to None turns these red.

HEAD_MARKER = "HEADMARKERATTHEVERYTOP"
FAULT_MARKER = "FAULTMARKERBESIDETHEFAILURE"


def long_payload(tail: str) -> str:
    """A payload well over 400 characters whose only fault is in `tail`, at the end."""
    return '{"head": "' + HEAD_MARKER + "x" * 600 + '", ' + tail + "}"


def assert_window_is_centred_on_the_failure(message: str) -> None:
    assert FAULT_MARKER in message, "the excerpt never reaches the offending region"
    assert HEAD_MARKER not in message, "the excerpt starts at the top, not at the failure"


def test_the_non_finite_number_excerpt_is_centred_on_the_token():
    with pytest.raises(ModelResponseError) as raised:
        parse_payload(long_payload(f'"{FAULT_MARKER}": NaN'))
    assert_window_is_centred_on_the_failure(str(raised.value))


def test_the_decode_failure_excerpt_is_centred_on_the_decode_position():
    with pytest.raises(ModelResponseError) as raised:
        parse_payload(long_payload(f'"{FAULT_MARKER}": '))
    assert_window_is_centred_on_the_failure(str(raised.value))


# --- the excerpt is windowed on the string the position was measured in ---
#
# `repair_json` only ever shrinks a body, so an offset into the repaired
# candidate names an earlier character than the same fault does in the raw one.
# Excerpting the raw body at a repaired offset therefore misses, and misses
# worst on exactly the shape repair exists for: prose wrapped around the
# object. The payloads above cannot see this — none of them is repairable, so
# the only candidate is the body itself and the two coordinate spaces coincide.

PROSE_OBJECT = '{"' + FAULT_MARKER + '": }'
PROSE_FAULT_POS = PROSE_OBJECT.index("}")
PROSE_PREFACE = (
    HEAD_MARKER + " Certainly! Here is the JSON object you asked for. I have "
    "read the abstract and pulled out every lifespan arm it reports, one "
    "object per intervention, quoting the source sentence verbatim for each "
    "field and leaving anything the paper does not state as not_reported "
    "rather than inferring it. Let me know if you would like the same "
    "treatment applied to the figures, or a different level of detail in the "
    "mechanism field. "
)


def test_the_decode_excerpt_is_windowed_on_the_candidate_that_failed():
    """A prose-wrapped body: the fault is at one offset raw and another repaired."""
    body = PROSE_PREFACE + PROSE_OBJECT

    # The two premises this keys on, checked rather than assumed. Repair has to
    # actually change the string, or there is only one candidate and both
    # coordinate spaces are the same one; and the preamble has to be longer
    # than the window a repaired offset opens on the raw body, or the window
    # reaches the object by accident and the assertion below cannot fail.
    assert repair_json(body) == PROSE_OBJECT != body
    assert len(PROSE_PREFACE) >= PROSE_FAULT_POS + DEFAULT_RADIUS

    with pytest.raises(ModelResponseError) as raised:
        parse_payload(body)
    assert_window_is_centred_on_the_failure(str(raised.value))


# --- JSON nested deeper than the decoder's stack ---
#
# `json.loads` recurses per nesting level and raises `RecursionError` — a
# `RuntimeError` — past roughly a thousand of them. It is neither a
# `JSONDecodeError` nor a `_RefusedNumber`, so it escaped both handlers in
# `parse_payload`, the retry in `call_structured` and the per-paper
# `except ExtractError` in `extract/cli.py`, ending a batch run on a raw
# traceback with every later paper unprocessed. Two kilobytes of `[` reach the
# threshold, which is well inside the token caps this package uses.

SHALLOW_DEPTH = 200


def nested_payload(depth: int) -> str:
    """A syntactically valid object whose only unusual feature is its depth."""
    return '{"experiments": ' + "[" * depth + "]" * depth + "}"


def list_depth(node: object) -> int:
    """How many lists deep `node` goes along its first element."""
    depth = 0
    while isinstance(node, list):
        depth += 1
        if not node:
            break
        node = node[0]
    return depth


def test_nesting_the_decoder_can_read_still_parses():
    """The premise: this refuses a *depth*, not brackets."""
    parsed = parse_payload(nested_payload(SHALLOW_DEPTH))
    assert list_depth(parsed["experiments"]) == SHALLOW_DEPTH


@pytest.mark.parametrize("depth", [1000, 2000])
def test_json_nested_past_the_decoder_is_refused_as_an_extract_error(depth):
    body = nested_payload(depth)

    with pytest.raises(ModelResponseError) as raised:
        parse_payload(body)

    error = raised.value
    assert isinstance(error, ExtractError)
    message = str(error)
    assert "nested too deeply" in message
    # Windowed like every other refusal: a 2000-deep payload cannot land in a
    # log whole.
    assert "payload excerpt" in message
    assert body not in message


def test_a_deeply_nested_payload_is_retried_once_then_raised():
    """The contract the CLI depends on: one retry, then one `ExtractError`."""
    body = nested_payload(2000)
    client = StubClient(model_response(body), model_response(body))

    with pytest.raises(ModelResponseError) as raised:
        call(client)

    assert len(client.requests) == RETRIES + 1
    message = str(raised.value)
    assert "attempt 1" in message and "attempt 2" in message


def test_the_same_tokens_inside_a_string_are_ordinary_text():
    """The premise: this refuses a JSON *number*, not the letters N-a-N."""
    assert parse_payload('{"strain": "NaN mutant", "notes": "Infinity Gene Co."}') == {
        "strain": "NaN mutant",
        "notes": "Infinity Gene Co.",
    }


def test_finite_numbers_still_parse_as_the_numbers_they_are():
    """The other half of the premise: the hooks are not a blanket refusal."""
    assert parse_payload('{"a": 14.0, "b": -3, "c": 1e300, "d": 0.0}') == {
        "a": 14.0,
        "b": -3,
        "c": 1e300,
        "d": 0.0,
    }


def test_a_fenced_payload_carrying_nan_is_refused_after_repair_too():
    """Repair fixes fences, prose and commas; it never touches a number.

    So the token survives into the repaired candidate, and the refusal has to
    come from there rather than from the raw body — which fails on the fence
    long before any number is read.
    """
    with pytest.raises(ModelResponseError, match="NaN"):
        parse_payload('```json\n{"median_change_pct": NaN}\n```')


def test_a_payload_carrying_nan_is_retried_once_then_raised():
    """Untrusted input like any other unparseable payload, with the same policy."""
    client = StubClient(
        model_response('{"median_change_pct": NaN}'),
        model_response('{"median_change_pct": NaN}'),
    )
    with pytest.raises(ModelResponseError) as raised:
        call(client)

    assert len(client.requests) == RETRIES + 1
    assert "attempt 1" in str(raised.value) and "attempt 2" in str(raised.value)


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


def test_the_sdk_puts_exceptions_outside_the_api_error_subtree():
    """The premise of the test below, asserted rather than assumed.

    `anthropic.APIError` is a *child* of `anthropic.AnthropicError`, not the
    root, so catching `APIError` leaves a non-empty remainder. Stated as its own
    test because an empty parametrization is a green test that ran nothing —
    the failure mode this whole file's fixtures keep producing.
    """
    assert issubclass(anthropic.APIError, anthropic.AnthropicError)
    assert not issubclass(anthropic.AnthropicError, anthropic.APIError)

    outside = sdk_errors_outside_the_api_error_subtree()
    assert outside, (
        "no exported SDK exception sits outside the APIError subtree; the test "
        "below would parametrize over nothing and pass without running."
    )
    assert anthropic.AnthropicError in outside

    # And the other half of the premise: at least one of them can carry an HTTP
    # status, so the status-bearing branch below is exercised rather than
    # merely written. `WorkloadIdentityError` is that type today. If a future
    # SDK drops it, this fails loudly instead of quietly reducing the test to
    # the `status is None` case it used to be.
    assert any(status is not None for _, status in SDK_ERROR_CASES)


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    SDK_ERROR_CASES,
    ids=SDK_ERROR_IDS,
)
def test_an_sdk_error_outside_the_api_error_subtree_is_wrapped(exc, expected_status):
    """`_create` promises to convert *any* SDK failure, and the root is the bar.

    The three tests above build their input from `APIError` subclasses, so none
    of them can reach a handler that catches `APIError` and misses its parent —
    the fixtures all sit on one side of the hierarchy the bug keys on. These
    inputs sit on the other side. `RetryableError` is the live case: the SDK's
    transport middleware propagates an exception as-is unless it opts into the
    retry policy by type, so one that outlasts the retry budget arrives at
    `_create` as itself, escapes `call_structured`'s "never lets an SDK
    exception escape", escapes the per-paper `except ExtractError` in
    `extract/cli.py`, and strands every later paper in the batch.
    """
    client = StubClient(exc)
    with pytest.raises(ModelCallError) as raised:
        call(client)

    error = raised.value
    assert isinstance(error, ExtractError)
    assert error.model == "claude-haiku-4-5"
    # Status is per-type, not uniform. `AnthropicError` and `RetryableError`
    # declare no `status_code`, so the wrap has nothing to report and names the
    # class instead; `WorkloadIdentityError` declares one, and when the SDK has
    # filled it from the token-exchange response the wrap reports that status
    # the way it reports any other. Both branches are asserted because both are
    # parametrized — see `sdk_error_cases`.
    assert error.status == expected_status
    if expected_status is None:
        assert type(exc).__name__ in str(error)
    else:
        assert f"HTTP {expected_status}" in str(error)
    assert "boom" in str(error)


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
