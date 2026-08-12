"""Shared fixtures. No test in this suite touches the network or a real server.

`httpx.MockTransport` stubs HTTP at the transport layer, so the clients run
their real request-building, retry and parsing code and only the socket is
replaced. The database layer runs against in-memory SQLite for the same reason:
the constraints under test are the ones the production tables declare.

The Anthropic client is stubbed the same way and for the same reason: `StubClient`
satisfies the `extract.model.ModelClient` protocol and replays canned responses,
so the parsing, repair, retry and record-assembly code under test is the real
thing and only the API call is replaced. No test needs an API key.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from ingest.db import init_db, make_engine
from ingest.http import RetryPolicy

# Every delay is recorded rather than slept, so the suite asserts the backoff
# schedule without spending it.
FAST_POLICY = RetryPolicy(
    max_attempts=4, base_delay=1.0, multiplier=2.0, max_delay=32.0
)


class SleepRecorder:
    """A `sleep` stand-in that records the delays it was asked to wait."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def engine() -> Iterator[Engine]:
    """An initialised in-memory database using the production table schema."""
    created = make_engine("sqlite://")
    init_db(created)
    yield created
    created.dispose()


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    """Return a client whose every request is answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


@dataclass(frozen=True)
class StubBlock:
    """One content block of a stubbed Messages API response."""

    type: str
    text: str = ""


@dataclass(frozen=True)
class StubResponse:
    """A stubbed Messages API response, shaped like the SDK's `Message`."""

    content: Any
    stop_reason: str = "end_turn"
    stop_details: Any = None


@dataclass
class StubMessages:
    """Replays queued responses and records every request it was given."""

    responses: list[Any]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError(
                f"unexpected extra model call to {kwargs.get('model')!r}; "
                "the test queued fewer responses than the code asked for"
            )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StubClient:
    """A `ModelClient` that answers with `responses` in order."""

    def __init__(self, *responses: Any) -> None:
        self.messages = StubMessages(list(responses))

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.messages.requests


def model_response(
    body: str | dict[str, Any],
    *,
    stop_reason: str = "end_turn",
) -> StubResponse:
    """Return a response whose single text block carries `body`.

    A dict is serialised, so a test that cares about the payload's *content*
    writes a dict, and one that cares about the payload's *bytes* — repair and
    retry tests — writes the exact string.
    """
    text = body if isinstance(body, str) else json.dumps(body)
    return StubResponse(content=[StubBlock("text", text)], stop_reason=stop_reason)


# The sentence every stubbed claim quotes. It is a substring of every paper text
# the suite extracts from, because `extract_record` checks each quote against
# the text it was handed: a default quote that appeared in no paper would make
# every stub a fabrication and every test a failure.
QUOTE = "extended median survival by 14% in genetically heterogeneous mice"


def claim(value: Any, quote: str = QUOTE) -> dict[str, Any]:
    """Return a claim wrapper, honest by construction.

    An absent value carries no quote — the invariant `extract.schema` enforces —
    so a test has to opt into dishonesty explicitly rather than by forgetting.
    The quote is verbatim in the stub texts for the same reason. `extracted_from`
    is absent because the model never supplies it.
    """
    absent = value is None or value == "not_reported"
    return {"value": value, "source_quote": None if absent else quote, "confidence": "high"}


def experiment_payload(
    *,
    organism: str = "M. musculus",
    species: Any = None,
    agent: str = "rapamycin",
    direction: str = "increase",
    **overrides: Any,
) -> dict[str, Any]:
    """Return one experiment exactly as the model is asked to produce it.

    Every key the derived request schema requires, and none of the three the
    caller assigns (`experiment_id`, `notes`, `extracted_from`).
    """
    payload: dict[str, Any] = {
        "organism": claim(organism),
        "species": claim(species),
        "strain": claim("not_reported"),
        "sex": claim("not_reported"),
        "sample_size": claim(None),
        "intervention": {
            "type": claim("pharmacological"),
            "agent": claim(agent),
            "dose": claim(None),
            "age_at_start": claim(None),
        },
        "mechanism": claim(None),
        "lifespan_effect": {
            "direction": claim(direction),
            "median_change_pct": claim(14.0),
            "mean_change_pct": claim(None),
            "max_change_pct": claim(None),
            "p_value": claim("< 0.001"),
        },
    }
    payload.update(overrides)
    return payload


def queue_responses(*responses: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that replies with `responses` in order.

    Raises if the code under test makes more requests than were queued, so an
    unexpected extra retry fails loudly instead of reusing the last response.
    """
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not remaining:
            raise AssertionError(f"unexpected extra request to {request.url}")
        return remaining.pop(0)

    return handler
