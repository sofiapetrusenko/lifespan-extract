"""Exception types shared by every module in the ingest package.

Kept in their own module so the HTTP layer, the source clients, the DB layer
and the CLI can raise the same types without importing one another.

Each type guarantees a message the operator can act on without reading the
traceback: a configuration error names the environment variable, a transport
error names the URL and the status, and a response-format error carries a
window of the payload that failed to parse. That last one exists because the
project rule is "loud failure over silent fallback" — a client that quietly
returned an empty list on a malformed response would look identical to a query
that legitimately matched nothing.
"""

from __future__ import annotations

DEFAULT_RADIUS = 200


class IngestError(Exception):
    """Base class for every error raised by the ingest package."""


class ConfigurationError(IngestError):
    """Required configuration is missing or unusable. Never defaulted around."""


class TransportError(IngestError):
    """An HTTP request failed and retrying it will not help, or already didn't."""


class ScanLimitError(IngestError):
    """A paged scan hit its page cap before it finished its search window.

    Distinct from "no more results": stopping here and returning a partial scan
    would be indistinguishable from an exhaustive scan that found little, so the
    caller is told instead of guessing.
    """


class ResponseFormatError(IngestError):
    """A response parsed as transport but not as the documented payload shape."""

    @classmethod
    def from_payload(
        cls,
        *,
        url: str,
        reason: str,
        payload: str | bytes,
        position: int | None = None,
        radius: int = DEFAULT_RADIUS,
    ) -> ResponseFormatError:
        """Build an error whose message embeds a window of the offending payload.

        `position` is a character offset to centre the window on — a JSON decode
        error's `.pos`, for example. Without one the window starts at the top of
        the payload.
        """
        window = excerpt(payload, position=position, radius=radius)
        return cls(f"{reason}\n  url: {url}\n  payload excerpt:\n{window}")


def excerpt(
    payload: str | bytes,
    *,
    position: int | None = None,
    radius: int = DEFAULT_RADIUS,
) -> str:
    """Return an indented, elision-marked window of `payload`.

    Guarantees the result is at most ``2 * radius`` characters of payload plus
    elision markers, so an error message can never dump a multi-megabyte body.
    """
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    if position is None:
        start, end = 0, 2 * radius
    else:
        start, end = max(0, position - radius), position + radius
    body = text[start:end]
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"    {prefix}{body}{suffix}"
