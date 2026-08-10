"""Shared fixtures. No test in this suite touches the network or a real server.

`httpx.MockTransport` stubs HTTP at the transport layer, so the clients run
their real request-building, retry and parsing code and only the socket is
replaced. The database layer runs against in-memory SQLite for the same reason:
the constraints under test are the ones the production tables declare.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

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
