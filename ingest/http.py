"""Retrying HTTP wrapper shared by the PubMed and bioRxiv clients.

Both upstreams rate-limit with 429 and both have transient 5xx windows, so the
retry policy lives here once rather than twice.

Two deliberate properties:

* **Backoff is deterministic — no jitter.** Jitter exists to desynchronise many
  concurrent clients; this is a single-process CLI making sequential requests,
  so there is no herd to spread out, and determinism makes the retry schedule
  directly assertable in tests.
* **`sleep` is a parameter.** Tests need to observe the delay schedule without
  waiting for it. Injecting the clock is the only way to assert backoff
  behaviour without making the suite take a minute.

`Retry-After` is honoured when the server sends it, but only up to
`max_retry_after`: a server asking for an hour is a signal to stop and tell the
operator, not to hang the CLI for an hour.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ingest.errors import TransportError, excerpt

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff schedule for one request.

    Defaults give waits of 1s, 2s, 4s, 8s between five attempts: ~15s of total
    delay, long enough to ride out NCBI's per-second throttle and short enough
    that a genuinely down upstream fails the command promptly.
    """

    max_attempts: int = 5
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 32.0
    max_retry_after: float = 60.0

    def delay_for(self, attempt: int) -> float:
        """Return the wait after a failed `attempt` (1-based)."""
        return min(self.base_delay * self.multiplier ** (attempt - 1), self.max_delay)


DEFAULT_RETRY_POLICY = RetryPolicy()


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Perform one HTTP request, retrying 429/5xx and connection failures.

    Guarantees the returned response has a 2xx/3xx status. Any other outcome
    raises `TransportError` naming the URL, the final status or exception, and
    the number of attempts made — retries are never swallowed into a `None` or
    an empty result.
    """
    if policy.max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {policy.max_attempts}")

    last_detail = "no attempt was made"
    for attempt in range(1, policy.max_attempts + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
            if attempt == policy.max_attempts:
                break
            sleep(policy.delay_for(attempt))
            continue

        if response.status_code not in RETRYABLE_STATUS:
            if response.is_error:
                raise TransportError(
                    f"HTTP {response.status_code} from {url} (not retryable)\n"
                    f"  body excerpt:\n{excerpt(response.text)}"
                )
            return response

        last_detail = f"HTTP {response.status_code}"
        if attempt == policy.max_attempts:
            break
        sleep(_retry_delay(response, policy, attempt, url))

    raise TransportError(
        f"giving up on {url} after {policy.max_attempts} attempt(s): {last_detail}\n"
        "  If this persists the upstream is down or the query is too broad; "
        "retry later or lower --limit."
    )


def _retry_delay(
    response: httpx.Response, policy: RetryPolicy, attempt: int, url: str
) -> float:
    """Return how long to wait before the next attempt.

    Prefers the server's own `Retry-After` when it is a plain number of seconds
    within `policy.max_retry_after`; an unparseable header falls back to the
    exponential schedule, and an excessive one raises rather than stalling.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return policy.delay_for(attempt)
    try:
        requested = float(header.strip())
    except ValueError:
        # HTTP-date form, or junk. Neither is worth a date parser here; the
        # exponential schedule is a safe substitute and the request is retried
        # either way, so nothing is silently dropped.
        return policy.delay_for(attempt)
    if requested > policy.max_retry_after:
        raise TransportError(
            f"{url} asked to wait {requested:g}s, above the "
            f"{policy.max_retry_after:g}s cap. Upstream is throttling this "
            "client hard; rerun later rather than holding the process open."
        )
    return max(requested, 0.0)
