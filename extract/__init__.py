"""Extract: the cheap classifier gate, then structured extraction per schema.

Two stages of PLAN.md's cost cascade. `extract.classify` decides whether a
paper reports lifespan-intervention data at all; `extract.extract` turns the
ones that pass into `schema/experiment.schema.json` records, one per
(organism, intervention) pair.

Like `ingest`, this package deliberately exposes only its exception types at
import time: pulling in `extract.extract` would drag the Anthropic SDK and the
schema file in for a caller that only wanted to catch an error.

Every public error type in `extract.errors` is re-exported here, and
`tests/test_extract_errors.py` derives that list from the module rather than
restating it, so a new type cannot be added and left unreachable — which has
happened twice.
"""

from extract.errors import (
    ConfigurationError,
    ExperimentIdCollisionError,
    ExtractError,
    ModelCallError,
    ModelResponseError,
    OutputPathError,
    RecordValidationError,
)

__all__ = [
    "ConfigurationError",
    "ExperimentIdCollisionError",
    "ExtractError",
    "ModelCallError",
    "ModelResponseError",
    "OutputPathError",
    "RecordValidationError",
]
