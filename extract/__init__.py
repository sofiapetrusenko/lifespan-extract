"""Extract: the cheap classifier gate, then structured extraction per schema.

Two stages of PLAN.md's cost cascade. `extract.classify` decides whether a
paper reports lifespan-intervention data at all; `extract.extract` turns the
ones that pass into `schema/experiment.schema.json` records, one per
(organism, intervention) pair.

Like `ingest`, this package deliberately exposes only its exception types at
import time: pulling in `extract.extract` would drag the Anthropic SDK and the
schema file in for a caller that only wanted to catch an error.
"""

from extract.errors import (
    ConfigurationError,
    ExtractError,
    ModelResponseError,
    RecordValidationError,
)

__all__ = [
    "ConfigurationError",
    "ExtractError",
    "ModelResponseError",
    "RecordValidationError",
]
