"""The one path through which model output enters this codebase.

Everything here exists because a model response is untrusted input. The API's
structured-output mode makes a malformed body unlikely, not impossible, and
"unlikely" is not a contract — so the response is parsed defensively, repaired
by a conservative heuristic, retried exactly once, and then raised on with a
window of what came back. Nothing is salvaged from a payload that fails: a
half-parsed record is a fabricated record.

The client is a parameter everywhere, never a module global. Tests inject a
stub that returns canned responses, so the suite exercises this module's real
parsing, repair and retry logic without a network or an API key.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from typing import Any, Protocol

import anthropic

from extract.errors import ConfigurationError, ModelCallError, ModelResponseError

API_KEY_ENV = "ANTHROPIC_API_KEY"

# One retry, per PLAN.md. Not a tunable: two retries would triple the cost of a
# systematically bad prompt while producing the same failure.
RETRIES = 1

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class MessagesResource(Protocol):
    """The one API surface this package uses."""

    def create(self, **kwargs: Any) -> Any: ...


class ModelClient(Protocol):
    """Structural type of `anthropic.Anthropic` as far as this package cares."""

    @property
    def messages(self) -> MessagesResource: ...


def api_key() -> str:
    """Return `ANTHROPIC_API_KEY`, raising if it is unset or blank.

    No fallback and no placeholder: an extraction run without a key must fail
    in a second with a message naming the variable, not minutes later inside
    the SDK.
    """
    value = os.environ.get(API_KEY_ENV, "").strip()
    if not value:
        raise ConfigurationError(
            f"{API_KEY_ENV} is not set. Extraction calls the Anthropic API, e.g.\n"
            f"  export {API_KEY_ENV}='sk-ant-...'"
        )
    return value


def make_client() -> ModelClient:
    """Return an API client, raising before construction if the key is missing."""
    return anthropic.Anthropic(api_key=api_key())


def call_structured(
    client: ModelClient,
    *,
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    """Return the JSON object the model produced for `schema`.

    Guarantees the result is a parsed JSON object, or that this call raised an
    `ExtractError`: repair, then one retry, then `ModelResponseError` carrying
    excerpts of both responses, and `ModelCallError` if the API call itself
    failed. Never returns a partial or default-filled payload, and never lets
    an SDK exception escape — a caller processing a batch must be able to
    handle one paper's failure by catching one type.
    """
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }

    failures: list[str] = []
    for attempt in range(RETRIES + 1):
        response = _create(client, request, model=model)
        _check_stop_reason(response, model=model, max_tokens=max_tokens)
        body = response_text(response, model=model)
        try:
            return parse_payload(body)
        except ModelResponseError as exc:
            failures.append(f"attempt {attempt + 1}: {exc}")
            if attempt < RETRIES:
                print(
                    f"warning:  {model} returned an unparseable payload; "
                    f"retrying once ({str(exc).splitlines()[0]})",
                    file=sys.stderr,
                )

    joined = "\n".join(failures)
    raise ModelResponseError(
        f"{model} did not return usable JSON after {RETRIES + 1} attempt(s). "
        f"No record was produced.\n{joined}"
    )


class _RefusedNumber(ValueError):
    """Internal signal that a payload carries a number `parse_payload` refuses.

    Raised out of the `json.loads` hooks below and converted to a
    `ModelResponseError` by `parse_payload`, which is the only caller. It never
    leaves this module, and that is the point of it: `json.loads` runs each of
    the three number hooks itself, so a hook that let a bare `ValueError` out
    would put a failure originating in model output outside this package's
    error hierarchy — past `parse_payload`'s handlers, past the retry in
    `call_structured`, and past the per-paper `except ExtractError` in
    `extract/cli.py`.

    `token` is the literal as it appeared, used to centre the excerpt window on
    it. `reason` is the sentence the resulting error leads with.
    """

    def __init__(self, token: str, reason: str) -> None:
        super().__init__(reason)
        self.token = token
        self.reason = reason


_NON_FINITE_REASON = (
    "model returned {token}, which decodes to NaN or an infinity. JSON has no "
    "way to write one back, and a reader that meets one either throws or "
    "rewrites it to null"
)


def _reject_constant(token: str) -> Any:
    """`parse_constant` hook: refuse `NaN`, `Infinity` and `-Infinity`."""
    raise _RefusedNumber(token, _NON_FINITE_REASON.format(token=token))


def _finite_float(token: str) -> float:
    """`parse_float` hook: refuse a literal that overflows to an infinity.

    `1e400` is syntactically valid JSON and `float()` turns it into `inf`, so
    the `parse_constant` hook never sees it. Same bad value, different door.
    """
    value = float(token)
    if not math.isfinite(value):
        raise _RefusedNumber(token, _NON_FINITE_REASON.format(token=token))
    return value


def _bounded_int(token: str) -> int:
    """`parse_int` hook: refuse an integer literal CPython will not convert.

    Python 3.11 caps integer-string conversion at `sys.get_int_max_str_digits()`
    digits — 4300 by default — and raises a plain `ValueError` past it. The
    default `parse_int` is `int` itself, so leaving the hook unset means that
    `ValueError` is raised from inside `json.loads`: it is neither a
    `json.JSONDecodeError` nor an `ExtractError`, and `parse_payload` catches
    only those two. A 5000-digit value in a model payload therefore escaped the
    package entirely and ended a batch run on a raw traceback. Converting it
    here closes that door: this failure leaves the module as an `ExtractError`,
    so the existing per-paper handler counts it and moves on. It is one of the
    routes `parse_payload` enumerates, not the whole of them — that docstring
    says what the enumeration covers and what it does not.

    The limit is not restated: `int()` is asked, and whatever it refuses is
    refused, so a future interpreter's different cap needs no change here.
    """
    try:
        return int(token)
    except ValueError as exc:
        raise _RefusedNumber(
            token,
            f"model returned an integer literal {len(token)} characters long, "
            f"which Python refuses to convert to an int ({exc})",
        ) from exc


def parse_payload(body: str) -> dict[str, Any]:
    """Parse `body` as a JSON object, repairing it once before giving up.

    Raises `ModelResponseError` with a window centred on the decode position
    when neither the raw body nor the repaired one parses, and when the payload
    parses as JSON that is not an object.

    Also raises on a number that is not a JSON number. Python's decoder accepts
    the bare `NaN`, `Infinity` and `-Infinity` tokens as an extension, and lets
    an over-large literal overflow to an infinity — and `jsonschema` then calls
    the result a perfectly good `number`, so nothing downstream objects. The
    value that reaches a record file is a token no other JSON reader accepts:
    `JSON.parse` throws on it, serde rejects it, and `jq` silently rewrites it
    to `null`, which turns a bogus number into a fabricated absence. It is
    untrusted input like any other unrepairable payload and is refused the same
    way, with a window of what arrived.

    All three of the decoder's number hooks are overridden, `parse_int`
    included. An integer literal past CPython's conversion cap raises a bare
    `ValueError` out of `json.loads`, which neither handler below would catch —
    see `_bounded_int`.

    What this function guarantees is a list, not a proof. Four ways out of
    `json.loads` are enumerated and each is converted to a
    `ModelResponseError`: a `json.JSONDecodeError`; a `_RefusedNumber` from the
    three number hooks; the bare `ValueError` an over-long integer literal
    raises, caught in `_bounded_int` and re-raised as a `_RefusedNumber`; and
    the `RecursionError` a deeply nested payload raises out of the decoder,
    caught below. Every route on that list leaves as a `ModelResponseError`,
    and so does a payload that parses as JSON but not as an object.

    The list is the doors that have been found. Two of the four were found
    separately, after review, each having survived the pass that closed the one
    before it — so an unqualified claim that nothing else can escape is not one
    this docstring can stand behind. A fifth route would be a defect here, and
    the shape of the fix is the four above: name it, window the payload, raise.
    """
    candidates = [body]
    repaired = repair_json(body)
    if repaired != body:
        candidates.append(repaired)

    # `last_candidate` is the string `last.pos` is an offset into, kept beside
    # it because the two are only meaningful together. `repair_json` never
    # grows a body — it strips fences, slices to the outermost braces and drops
    # trailing commas — so a position measured in the repaired candidate names
    # an earlier character than the same fault does in the raw body. Excerpting
    # `body` at a repaired offset is how a prose-wrapped response, which is the
    # exact shape `repair_json` exists for, produced a window of preamble with
    # the fault nowhere in it.
    last: json.JSONDecodeError | None = None
    last_candidate = body
    for candidate in candidates:
        try:
            payload = json.loads(
                candidate,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
                parse_int=_bounded_int,
            )
        except json.JSONDecodeError as exc:
            last = exc
            last_candidate = candidate
            continue
        except RecursionError as exc:
            # A `RuntimeError`, so neither handler beside this one takes it and
            # nothing downstream does either: it went past the retry in
            # `call_structured` and past the per-paper `except ExtractError` in
            # `extract/cli.py`, ending a batch run on a raw traceback and
            # stranding every paper after it. Roughly two kilobytes of `[` is
            # enough, which is well inside any `max_tokens` used here.
            #
            # Not retried against the repaired candidate: `repair_json` only
            # ever removes text around the object, so the nesting depth that
            # exhausted the stack is identical in both. The window starts at
            # the top because the decoder reports no position for this — the
            # fault is the payload's shape, not a place in it, and the opening
            # of it is what shows that shape.
            raise ModelResponseError.from_payload(
                reason=(
                    "model returned JSON nested too deeply for the decoder to "
                    f"read ({exc}). Nothing was decoded, so nothing is salvaged"
                ),
                payload=candidate,
            ) from exc
        except _RefusedNumber as exc:
            # Not retried against the repaired candidate: `repair_json` fixes
            # fences, prose and trailing commas, and does not touch numbers, so
            # the repaired body would carry the same token.
            found = candidate.find(exc.token)
            raise ModelResponseError.from_payload(
                reason=exc.reason,
                payload=candidate,
                position=found if found != -1 else None,
            ) from exc
        if not isinstance(payload, dict):
            raise ModelResponseError.from_payload(
                reason=(
                    f"model returned a JSON {type(payload).__name__}, "
                    "expected an object"
                ),
                payload=candidate,
            )
        return payload

    raise ModelResponseError.from_payload(
        reason=(
            "model response is not JSON, before or after repair: "
            f"{last.msg if last else 'empty response body'}"
        ),
        payload=last_candidate,
        position=last.pos if last else None,
    )


def repair_json(body: str) -> str:
    """Return `body` with the three failure shapes a model actually produces fixed.

    Deliberately conservative — fenced code blocks, prose wrapped around the
    object, and trailing commas. It never inserts a key, a value or a closing
    brace: repairing a truncated object would mean inventing the part that did
    not arrive. A body it cannot fix comes back unchanged, and the caller
    raises.

    The trailing-comma rule matches `,` before `}` or `]` textually, so a comma
    inside a string literal immediately followed by a closing brace would be
    rewritten. That is accepted: the rule only ever runs on a body that already
    failed to parse.
    """
    trimmed = _FENCE_RE.sub("", body.strip()).strip()
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start != -1 and end > start:
        trimmed = trimmed[start : end + 1]
    return _TRAILING_COMMA_RE.sub(r"\1", trimmed)


def response_text(response: Any, *, model: str) -> str:
    """Return the concatenated text blocks of a message response.

    Raises when there are none: a response with only tool or thinking blocks is
    not a structured-output response, and treating it as an empty string would
    turn a protocol error into an extraction that quietly found nothing.
    """
    blocks = getattr(response, "content", None)
    if not isinstance(blocks, list):
        raise ModelResponseError(
            f"{model} response has no content list (got {type(blocks).__name__}); "
            "this is not a Messages API response."
        )
    texts = [
        block.text
        for block in blocks
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ]
    if not texts:
        kinds = ", ".join(sorted({str(getattr(b, "type", "?")) for b in blocks})) or "none"
        raise ModelResponseError(
            f"{model} response contains no text block (block types: {kinds}); "
            "expected a JSON payload."
        )
    return "\n".join(texts)


def _create(client: ModelClient, request: dict[str, Any], *, model: str) -> Any:
    """Send one request, converting any SDK failure into a `ModelCallError`.

    The boundary is here because this is the only place the SDK is called. An
    overloaded API (529), a rate limit the SDK's own retries did not outlast
    (429), a refused connection and a timeout are all `anthropic.APIError`
    subclasses, and none of them is an `ExtractError` — uncaught, one of them
    ends a twenty-paper run on the first paper with a raw traceback.

    The caught type is `anthropic.AnthropicError`, the root of the SDK's
    *exported* exception hierarchy. (Two SDK exceptions sit outside it
    altogether — `anthropic._response.MissingStreamClassError`, raised only on
    an SSE response, and `anthropic.lib.tools.ToolError`, raised from a tool
    function running under the tool runner. Neither is reachable from the
    non-streaming `messages.create` below.) `APIError` is not that root: it is a child of
    `AnthropicError`, and the exported types outside its subtree got past a
    handler that caught `APIError`, which is exactly the hole this docstring's
    first line promises there is none of. Under anthropic 0.121.0 those types
    are `AnthropicError` itself, `RetryableError` and `WorkloadIdentityError`;
    the count is not load-bearing here, and `tests/test_model.py` derives the
    set from the installed package rather than restating it. `RetryableError`
    is the one with teeth: the SDK's transport middleware propagates exceptions
    as-is unless they opt into the retry policy by type, and one that outlasts
    the retry budget arrives here as itself.

    What `from_api_error` reports for them is per-type, and it already covers
    both shapes. `AnthropicError` and `RetryableError` declare no `status_code`,
    so it reports `status=None` and names the class — the same treatment a
    connection failure gets. `WorkloadIdentityError` declares one and the SDK
    fills it from the token-exchange response, so an instance carrying a status
    is reported as that HTTP status, like any other status-bearing failure.
    """
    try:
        return client.messages.create(**request)
    except anthropic.AnthropicError as exc:
        raise ModelCallError.from_api_error(exc, model=model) from exc


def _check_stop_reason(response: Any, *, model: str, max_tokens: int) -> None:
    """Raise on the stop reasons that mean the payload cannot be trusted."""
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise ModelResponseError(
            f"{model} declined the request (stop_reason=refusal, "
            f"category={category!r}). No record was produced."
        )
    if stop_reason == "max_tokens":
        raise ModelResponseError(
            f"{model} hit the {max_tokens}-token output cap (stop_reason=max_tokens); "
            "the payload is truncated. Raise max_tokens or extract from a shorter text."
        )
