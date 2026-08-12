"""Exception types shared by every module in the extract package.

Kept in their own module so the model layer, the classifier, the extractor and
the CLI can raise the same types without importing one another.

Each type guarantees a message an operator can act on without reading the
traceback: a configuration error names the environment variable, and a
model-response error carries a window of the payload that failed. The project
rule is loud failure over silent fallback — an extractor that returned a
half-filled record on a malformed response would be indistinguishable from a
paper that genuinely reports very little.
"""

from __future__ import annotations

from ingest.errors import DEFAULT_RADIUS, excerpt

__all__ = [
    "ConfigurationError",
    "ExtractError",
    "ModelCallError",
    "ModelResponseError",
    "RecordValidationError",
]


class ExtractError(Exception):
    """Base class for every error raised by the extract package."""


class ConfigurationError(ExtractError):
    """Required configuration is missing or unusable. Never defaulted around."""


class ModelCallError(ExtractError):
    """The API call failed; no response body was ever produced.

    Distinct from `ModelResponseError`, which means something came back and was
    unusable. Here nothing came back — an overloaded or rate-limited API, a
    refused connection, a timeout — so there is no payload to excerpt, and the
    actionable facts are the model and the HTTP status.

    It exists so that transport failures are `ExtractError`s like every other
    failure in this package: the per-paper handler in `extract/cli.py` counts
    one and moves on to the next paper, instead of an SDK exception escaping
    and stranding the rest of the batch with a bare traceback.
    """

    def __init__(self, message: str, *, model: str, status: int | None = None) -> None:
        super().__init__(message)
        self.model = model
        self.status = status

    @classmethod
    def from_api_error(cls, exc: Exception, *, model: str) -> ModelCallError:
        """Wrap an SDK exception, naming the model and the status if there is one.

        `status` is None for the errors that never reached a response —
        connection failures and timeouts — and the HTTP status otherwise, so a
        caller can tell "the API said 529" from "the API was unreachable"
        without matching on the message.
        """
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            status = None
        where = f"HTTP {status}" if status is not None else type(exc).__name__
        return cls(
            f"the {model} API call failed ({where}): {exc}. No record was "
            "produced for this paper.",
            model=model,
            status=status,
        )


class ModelResponseError(ExtractError):
    """The model returned something that is not the requested payload.

    Covers an unparseable body, a refusal, a truncated response and a payload
    that parses as JSON but not as the agreed shape. All four are the same
    thing from the caller's point of view: this call produced no usable record,
    and nothing may be salvaged from it.
    """

    @classmethod
    def from_payload(
        cls,
        *,
        reason: str,
        payload: str | bytes,
        position: int | None = None,
        radius: int = DEFAULT_RADIUS,
    ) -> ModelResponseError:
        """Build an error whose message embeds a window of the offending payload.

        `position` centres the window on a character offset — a JSON decode
        error's `.pos`, for example. Without one the window starts at the top.
        Reuses `ingest.errors.excerpt` so the two packages cannot drift into
        two different ideas of how much of a payload an error may print.
        """
        window = excerpt(payload, position=position, radius=radius)
        return cls(f"{reason}\n  payload excerpt:\n{window}")


class RecordValidationError(ExtractError):
    """An assembled record failed the schema or a provenance invariant.

    Distinct from `ModelResponseError`: the payload was well-formed enough to
    build a record from, and the record is still wrong. Retrying would not help
    because the failure is in the content, not the transport.
    """
