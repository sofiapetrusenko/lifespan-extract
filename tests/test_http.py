"""Retry and backoff behaviour of the shared HTTP wrapper."""

from __future__ import annotations

import httpx
import pytest

from ingest.errors import TransportError
from ingest.http import DEFAULT_RETRY_POLICY, RetryPolicy, request_with_retry
from tests.conftest import FAST_POLICY, make_client, queue_responses

URL = "https://example.test/resource"


def test_shipped_default_policy_is_the_documented_schedule(sleeps):
    """Pin the constants callers actually get when they pass no `policy`.

    Every other test here injects `FAST_POLICY`, so without this the shipped
    defaults are asserted nowhere: shortening them to a single attempt, or
    flattening the backoff to zero, would leave the rest of the suite green.
    """
    assert DEFAULT_RETRY_POLICY.max_attempts == 5
    assert DEFAULT_RETRY_POLICY.max_delay == 32.0
    assert DEFAULT_RETRY_POLICY.max_retry_after == 60.0

    # Exercised through the default argument rather than by passing the policy
    # in, so the binding in `request_with_retry` is covered too.
    client = make_client(queue_responses(*[httpx.Response(429)] * 5))
    with pytest.raises(TransportError, match="5 attempt"):
        request_with_retry(client, "GET", URL, sleep=sleeps)
    assert sleeps.delays == [1.0, 2.0, 4.0, 8.0]


def test_returns_first_successful_response(sleeps):
    client = make_client(queue_responses(httpx.Response(200, text="ok")))
    response = request_with_retry(
        client, "GET", URL, policy=FAST_POLICY, sleep=sleeps
    )
    assert response.text == "ok"
    assert sleeps.delays == []


def test_retries_429_then_succeeds(sleeps):
    client = make_client(
        queue_responses(
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, text="ok"),
        )
    )
    response = request_with_retry(
        client, "GET", URL, policy=FAST_POLICY, sleep=sleeps
    )
    assert response.status_code == 200
    assert sleeps.delays == [1.0, 2.0]


def test_backoff_is_exponential_and_capped(sleeps):
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, multiplier=2.0, max_delay=3.0)
    client = make_client(queue_responses(*[httpx.Response(429)] * 5))
    with pytest.raises(TransportError):
        request_with_retry(client, "GET", URL, policy=policy, sleep=sleeps)
    assert sleeps.delays == [1.0, 2.0, 3.0, 3.0]


def test_gives_up_after_max_attempts(sleeps):
    client = make_client(queue_responses(*[httpx.Response(429)] * 4))
    with pytest.raises(TransportError) as excinfo:
        request_with_retry(client, "GET", URL, policy=FAST_POLICY, sleep=sleeps)
    message = str(excinfo.value)
    assert "4 attempt(s)" in message
    assert "HTTP 429" in message
    assert URL in message
    assert len(sleeps.delays) == 3


def test_retry_after_header_overrides_backoff(sleeps):
    client = make_client(
        queue_responses(
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200),
        )
    )
    request_with_retry(client, "GET", URL, policy=FAST_POLICY, sleep=sleeps)
    assert sleeps.delays == [7.0]


def test_unparseable_retry_after_falls_back_to_backoff(sleeps):
    client = make_client(
        queue_responses(
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200),
        )
    )
    request_with_retry(client, "GET", URL, policy=FAST_POLICY, sleep=sleeps)
    assert sleeps.delays == [1.0]


def test_excessive_retry_after_raises_rather_than_stalling(sleeps):
    client = make_client(
        queue_responses(httpx.Response(429, headers={"Retry-After": "3600"}))
    )
    with pytest.raises(TransportError, match="3600s"):
        request_with_retry(client, "GET", URL, policy=FAST_POLICY, sleep=sleeps)
    assert sleeps.delays == []


def test_server_errors_are_retried(sleeps):
    client = make_client(
        queue_responses(httpx.Response(503), httpx.Response(200, text="ok"))
    )
    response = request_with_retry(
        client, "GET", URL, policy=FAST_POLICY, sleep=sleeps
    )
    assert response.text == "ok"
    assert sleeps.delays == [1.0]


def test_client_errors_are_not_retried(sleeps):
    client = make_client(queue_responses(httpx.Response(400, text="bad query")))
    with pytest.raises(TransportError, match="not retryable"):
        request_with_retry(client, "GET", URL, policy=FAST_POLICY, sleep=sleeps)
    assert sleeps.delays == []


def test_connection_errors_are_retried(sleeps):
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, text="ok")

    response = request_with_retry(
        make_client(handler), "GET", URL, policy=FAST_POLICY, sleep=sleeps
    )
    assert response.text == "ok"
    assert sleeps.delays == [1.0, 2.0]


def test_persistent_connection_error_reports_the_cause(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransportError, match="ConnectError"):
        request_with_retry(
            make_client(handler), "GET", URL, policy=FAST_POLICY, sleep=sleeps
        )


def test_zero_attempts_is_rejected(sleeps):
    client = make_client(queue_responses(httpx.Response(200)))
    with pytest.raises(ValueError, match="max_attempts"):
        request_with_retry(
            client, "GET", URL, policy=RetryPolicy(max_attempts=0), sleep=sleeps
        )
