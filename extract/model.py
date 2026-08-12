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


def parse_payload(body: str) -> dict[str, Any]:
    """Parse `body` as a JSON object, repairing it once before giving up.

    Raises `ModelResponseError` with a window centred on the decode position
    when neither the raw body nor the repaired one parses, and when the payload
    parses as JSON that is not an object.
    """
    candidates = [body]
    repaired = repair_json(body)
    if repaired != body:
        candidates.append(repaired)

    last: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last = exc
            continue
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
        payload=body,
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
    """
    try:
        return client.messages.create(**request)
    except anthropic.APIError as exc:
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
