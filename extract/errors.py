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
    "ExperimentIdCollisionError",
    "ExtractError",
    "ModelCallError",
    "ModelResponseError",
    "OutputPathError",
    "RecordValidationError",
]


class ExtractError(Exception):
    """Base class for every error raised by the extract package."""


class ConfigurationError(ExtractError):
    """Required configuration is missing or unusable. Never defaulted around."""


class OutputPathError(ExtractError):
    """The directory a run was told to write records to is one nothing may use.

    Its own type because `extract/cli.py` grades it differently from every
    other failure: an operator who named the wrong `--out` gets exit 2, the
    argparse convention for a usage error, while a run that failed on the data
    gets exit 1. It was a bare `ValueError` until that grading proved unsafe —
    `main` caught `ValueError` to recognise it, and `UnicodeEncodeError` is a
    `ValueError`, so a model payload carrying a lone surrogate was reported as
    an invalid argument, exited 2, and stranded every paper after it. A
    dedicated type is what lets that catch be narrow enough to mean what its
    message says.
    """


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

        `status` is the HTTP status when the exception carries an integer
        `status_code`, and None otherwise, so a caller can tell "the API said
        529" from "no response was involved" without matching on the message.
        None covers more than the transport failures it was first written for:
        connection errors and timeouts never reached a response, but a bare
        `AnthropicError` or a `RetryableError` declares no `status_code` at
        all, and a `WorkloadIdentityError` declares one that may be unset. What
        the message names in the None case is the exception's class, which is
        the only thing that distinguishes them.
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


class ExperimentIdCollisionError(ExtractError):
    """Two different experiments in one paper generated one `experiment_id`.

    Its own type because it is the one failure in identity generation that is
    not about a malformed input: every value involved is well-formed, and the
    id is well-formed too — it just no longer names one experiment. Phase 3
    aligns gold records against extracted ones by this id, so an id covering
    two interventions scores them as one and reports nothing amiss.

    Raised rather than resolved with the `-2` tail. That tail exists to
    separate two arms of the *same* intervention, and reusing it here would
    make a numeric suffix the only thing standing between two compounds — a
    distinction no reader of the id could recover. The message names both sets
    of values and the id they landed on, because the fix is always upstream of
    here: a better name in the record, or a reading for the character that made
    the two alike.
    """


class RecordValidationError(ExtractError):
    """An assembled record failed the schema, a provenance invariant, or JSON.

    Distinct from `ModelResponseError`: the payload was well-formed enough to
    build a record from, and the record is still wrong. Retrying would not help
    because the failure is in the content, not the transport.

    The third case is the record that cannot be serialised as the JSON it
    claims to be — a lone surrogate that no UTF-8 encoder will take, a `NaN`
    that `json.dumps` only emits by writing a token no JSON parser accepts.
    Both come from model-derived text, both are caught at the write in
    `extract/cli.py`, and both are per-paper failures for the same reason every
    other content failure is: nothing about them is the operator's to fix, and
    the rest of the batch is still extractable.
    """
