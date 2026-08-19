"""The cheap gate: does this paper report lifespan-intervention data?

First stage of PLAN.md's cost cascade. Every ingested paper passes through
here on a small model; only the ones that clear it reach the expensive
extractor. The gate answers one question and returns a reason, because a
classifier that only says "no" cannot be debugged — and Phase 3 scores this
against `data/classifier_set/negatives.json` plus the gold set, where knowing
*which* boundary a paper failed is the whole diagnostic value.

The three criteria are the ones the negative set was built to probe: something
must be administered, an organism's lifespan must be the measured outcome, and
the paper must report its own experiment rather than describe others'.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from extract.errors import ExtractError, ModelResponseError
from extract.model import ModelClient, call_structured

# Cheap tier by design: PLAN.md specifies a "cheap-model gate" as a cost
# cascade, so the small model here is the stated architecture, not a downgrade.
CLASSIFIER_MODEL = "claude-haiku-4-5"

# A decision, a short reason and a confidence label. Nothing here is long.
MAX_TOKENS = 512

CONFIDENCE_LEVELS = ("high", "medium", "low")

CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reports_lifespan_intervention", "reason", "confidence"],
    "properties": {
        "reports_lifespan_intervention": {
            "type": "boolean",
            "description": "True when all three inclusion criteria hold.",
        },
        "reason": {
            "type": "string",
            "description": (
                "One sentence naming the criterion that decided it, in the "
                "paper's own terms."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": list(CONFIDENCE_LEVELS),
            "description": "How clearly the text settles the question.",
        },
    },
}

SYSTEM_PROMPT = """\
You screen biomedical papers for a database of lifespan-intervention experiments.

Answer true only when all three hold:
1. An intervention is administered to the organisms — a compound, a genetic
   manipulation, or a dietary regimen. An observed association, a natural
   variant, or a cohort study is not an intervention.
2. The lifespan or survival of whole organisms is a measured outcome. Ageing
   biomarkers, biological-age clocks, healthspan endpoints, cellular senescence
   and the replicative lifespan of cell lines are all outside scope. But they
   do not exclude a paper on their own: if whole-organism lifespan or survival
   is among the measured outcomes, other endpoints reported alongside it make
   no difference.
3. The paper reports its own experiment. Reviews, meta-analyses, commentary and
   protocol papers describe experiments rather than performing them.

Judge only the text you are given, on the three criteria as written. When the
text genuinely does not settle a criterion, say which one and what is
unresolved in the reason, and set confidence to low.
"""


@dataclass(frozen=True)
class Classification:
    """One gate decision, with the reason and confidence behind it."""

    relevant: bool
    reason: str
    confidence: str
    model: str


def classify(title: str, text: str, *, client: ModelClient) -> Classification:
    """Return the gate's decision for one paper.

    `text` is the abstract (or full text) to judge; it must be non-empty,
    because a decision made from a title alone would be a guess wearing a
    confidence label.
    """
    if not text or not text.strip():
        raise ExtractError(
            f"cannot classify {title!r}: no text was supplied. Papers stored "
            "without an abstract must be skipped, not judged from the title."
        )

    payload = call_structured(
        client,
        model=CLASSIFIER_MODEL,
        system=SYSTEM_PROMPT,
        user=f"Title: {title}\n\n{text.strip()}",
        schema=CLASSIFICATION_SCHEMA,
        max_tokens=MAX_TOKENS,
    )
    return _classification(payload)


def _classification(payload: dict[str, Any]) -> Classification:
    """Convert a model payload into a `Classification`, raising on any surprise.

    Structured output makes the shape very likely, not guaranteed, and the
    difference matters here: coercing a missing decision to False would silently
    drop papers.
    """
    relevant = payload.get("reports_lifespan_intervention")
    reason = payload.get("reason")
    confidence = payload.get("confidence")

    if not isinstance(relevant, bool):
        raise ModelResponseError.from_payload(
            reason=(
                "classifier payload has no boolean "
                f"'reports_lifespan_intervention' (got {relevant!r})"
            ),
            payload=repr(payload),
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ModelResponseError.from_payload(
            reason="classifier payload has no non-empty 'reason'",
            payload=repr(payload),
        )
    if confidence not in CONFIDENCE_LEVELS:
        raise ModelResponseError.from_payload(
            reason=(
                f"classifier payload confidence {confidence!r} is not one of "
                f"{list(CONFIDENCE_LEVELS)}"
            ),
            payload=repr(payload),
        )
    return Classification(
        relevant=relevant,
        reason=reason.strip(),
        confidence=confidence,
        model=CLASSIFIER_MODEL,
    )
