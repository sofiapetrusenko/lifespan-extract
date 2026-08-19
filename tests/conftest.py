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
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine

from extract.schema import (
    CLAIM_KEYS,
    EXPERIMENT_INDEX,
    IDENTITY_PROPERTIES,
    OUTCOME_PROPERTIES,
)
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


def claims(node: Any) -> list[dict[str, Any]]:
    """Return every claim wrapper in a record, at any depth.

    Recognised by shape, the way the production walker recognises them: a dict
    holding all of `CLAIM_KEYS` is a claim and is not descended into. Shared
    here because both the extractor's records and the CLI's written files are
    checked claim by claim.
    """
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if CLAIM_KEYS <= set(node):
            return [node]
        for child in node.values():
            found += claims(child)
    elif isinstance(node, list):
        for child in node:
            found += claims(child)
    return found


# The `direction` vocabulary, in the order `$defs/claim_direction` declares it.
# `experiment_payload` spreads experiments across it; see `payload_identity`.
DIRECTIONS = ("increase", "decrease", "no_effect")


def _identity_values(node: Any) -> Any:
    """Reduce a claim wrapper, or an object of them, to the values alone.

    The quote, confidence and provenance on a claim are the same on every
    experiment a fixture builds, so carrying them into the identity adds
    hundreds of characters and distinguishes nothing. `intervention` is an
    object of claims rather than a claim, so this recurses.
    """
    if isinstance(node, dict):
        if "value" in node:
            return node["value"]
        return {key: _identity_values(child) for key, child in node.items()}
    return node


def payload_identity(payload: dict[str, Any]) -> str:
    """Return the string `experiment_payload` derives an experiment's outcome from.

    The values of the entire identity half, serialised with sorted keys.
    Reading the half rather than a chosen list of arguments is what makes it
    complete: every axis a caller can vary reaches it, including a property a
    later schema version adds to `IDENTITY_PROPERTIES` that no argument list
    written here could know about. A property no fixture builds yet reads as
    `null` here, because of this function's own `.get` below; `_half` applies
    the same rule to the two request payloads, and both are needed — reverting
    either one on its own aborts collection with `KeyError` before a single
    test runs, which is what this function used to do. So two experiments whose
    identity values differ anywhere have different identities here.

    Values only, not whole claim wrappers. Every axis a fixture varies is a
    value, so nothing is lost, and it is the difference between an identity of
    a couple of hundred characters and one of nine hundred — which matters
    because `mechanism` embeds this string, and a pytest diff that truncates
    mid-identity cannot tell the reader which experiment an outcome came from.
    That diff is the output of the join tests this derivation exists to make
    meaningful.

    It was an argument list once — `f"{agent} in {species or organism}"` — and
    nine of the eleven experiments a test could plausibly build collapsed onto
    the same string: `sex=male` against `sex=female`, a low dose against a high
    one, `organism="M. musculus"` against `organism="other"` with that species.
    Those are the axes `data/gold/` separates experiments within a paper on, so
    a join test written on the project's own axes would have been vacuous.
    """
    return json.dumps(
        {name: _identity_values(payload.get(name)) for name in IDENTITY_PROPERTIES},
        sort_keys=True,
    )


def experiment_payload(
    *,
    organism: str = "M. musculus",
    species: Any = None,
    agent: str = "rapamycin",
    direction: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return one experiment exactly as the model is asked to produce it.

    Every key the derived request schema requires, and none of the three the
    caller assigns (`experiment_id`, `notes`, `extracted_from`).

    **The outcome half varies with the identity half, and that is load-bearing.**
    `mechanism`, `lifespan_effect.direction` and `median_change_pct` are all
    derived from `payload_identity(payload)` — the whole identity half, read
    after `**overrides` have been applied, so no axis a caller can vary is
    outside it. Extraction joins outcomes to experiments by `experiment_index`
    (`extract.extract._merge_outcomes`), and while every experiment any fixture
    built carried a byte-identical outcome half, no test in the suite could see
    which experiment an outcome landed on: replacing the index join with a
    positional one, reversing the outcomes, or rotating them one place all left
    the whole suite green, because the merged record still validated, still
    satisfied `check_provenance`, and still passed `check_quotes_verbatim` —
    the quotes being real sentences of the same paper either way.

    `mechanism` embeds the identity string verbatim, so the derivation is
    injective in the identity half's *values*: two experiments whose identity
    values differ anywhere — by `sex`, by `intervention.dose`, by `organism`
    against `organism="other"` plus `species` — differ in their outcome half
    too. That is the whole guarantee, and it is enforced rather than trusted:
    `outcome_payload` refuses a multi-experiment fixture whose outcome halves
    coincide. An outcome half written out by the caller is the caller's, and
    two callers who write the same one are refused there like anyone else.

    `direction` and `median_change_pct` come off a CRC of the same string,
    which spreads a closed enum and a number across experiments without being
    injective on its own; `direction=` fixes the direction explicitly for a
    test that needs a particular one.
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
    }
    # Identity-half overrides before the derivation, so it reads the experiment
    # the caller asked for rather than the defaults it started from.
    payload.update({k: v for k, v in overrides.items() if k in IDENTITY_PROPERTIES})
    identity = payload_identity(payload)
    spread = zlib.crc32(identity.encode())
    payload["mechanism"] = claim(f"mechanism reported for {identity}")
    payload["lifespan_effect"] = {
        "direction": claim(direction or DIRECTIONS[spread % len(DIRECTIONS)]),
        "median_change_pct": claim(float(spread % 50)),
        "mean_change_pct": claim(None),
        "max_change_pct": claim(None),
        "p_value": claim("< 0.001"),
    }
    payload.update(overrides)
    return payload


def _half(experiment: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Return the `names` half of `experiment`, absent properties reading None.

    `.get` rather than a subscript, so that adding a property to
    `IDENTITY_PROPERTIES` or `OUTCOME_PROPERTIES` before the fixtures build it
    fails in the test that needs it, naming the property, instead of raising
    `KeyError` while pytest is still collecting and aborting the suite before
    anything runs.
    """
    return {name: experiment.get(name) for name in names}


def identity_payload(*experiments: dict[str, Any]) -> dict[str, Any]:
    """Return the first call's payload for whole experiments: the identity half."""
    return {
        "experiments": [
            _half(experiment, IDENTITY_PROPERTIES)
            for experiment in (experiments or (experiment_payload(),))
        ]
    }


def outcome_half(experiment: dict[str, Any]) -> str:
    """Return the serialised outcome half of `experiment`, keys sorted.

    What the second call answers with, and therefore the only thing a test can
    read to say *which* experiment an outcome was joined to.
    """
    return json.dumps(
        _half(experiment, OUTCOME_PROPERTIES), sort_keys=True
    )


def _refuse_indistinguishable_outcomes(experiments: Sequence[dict[str, Any]]) -> None:
    """Raise unless the outcome halves of `experiments` are pairwise distinct.

    The instrument, not the fix. A permutation of equal objects is the identity
    function, so a join test over experiments with equal outcome halves cannot
    fail however it is written — which is exactly how the two-call join went
    untested while the suite looked thorough. Checked wherever a fixture builds
    its second call's payload here, which is every join test in the suite —
    rather than in one fixture in one file, which is where the premise used to
    be asserted. It is not every multi-experiment fixture: one that only ever
    builds the first call, through `identity_payload`, never passes through
    this and does not need to, having no outcomes to tell apart.
    """
    if len(experiments) < 2:
        return
    halves = [outcome_half(experiment) for experiment in experiments]
    shared = [index for index, half in enumerate(halves) if halves.count(half) > 1]
    if shared:
        raise AssertionError(
            f"experiments {shared} share an outcome half, so no assertion could "
            "tell which experiment an outcome was joined to and any join test "
            "over this fixture would be vacuous. Give them identity halves that "
            f"differ — any of {', '.join(IDENTITY_PROPERTIES)}, and `sex` or "
            "`intervention.dose` are the axes the gold set uses within one "
            "paper — or write the outcome halves out explicitly."
        )


def outcome_payload(*experiments: dict[str, Any]) -> dict[str, Any]:
    """Return the second call's payload: the outcome half, one per experiment, in order.

    Refuses a fixture whose experiments cannot be told apart by their outcome
    halves; see `_refuse_indistinguishable_outcomes`.
    """
    chosen = experiments or (experiment_payload(),)
    _refuse_indistinguishable_outcomes(chosen)
    return {
        "experiments": [
            {
                EXPERIMENT_INDEX: index,
                **_half(experiment, OUTCOME_PROPERTIES),
            }
            for index, experiment in enumerate(chosen)
        ]
    }


def extraction_responses(*experiments: dict[str, Any]) -> tuple[StubResponse, ...]:
    """Return the two responses one `extract_record` call consumes.

    Extraction asks twice — once for the experiments as designed, once for what
    they found — so a test that wants to stub "the model returned this record"
    queues both halves of it. Splitting a whole experiment here rather than in
    each test keeps the fixtures written the way a record reads: a test names
    the experiments it means and never the two calls.

    It does *not* make the partition invisible. Moving a property between
    `IDENTITY_PROPERTIES` and `OUTCOME_PROPERTIES` turns tests red, because the
    two halves are not interchangeable to everything downstream: the identity
    half is what `_experiment_id`, the echoed list in the second call's prompt
    and the parametrized per-half tests are written against. What this helper
    removes is the need to *restate* the split in every fixture, not the split
    itself.

    No count is given for how many turn red, deliberately. This sentence has
    carried a wrong one in three consecutive rounds, and twice the round that
    corrected it invalidated its own correction in the same commit by changing
    what the fixtures build. A number here has nothing keeping it true; the
    claim that survives editing is the one without it.
    """
    return (
        model_response(identity_payload(*experiments)),
        model_response(outcome_payload(*experiments)),
    )


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
