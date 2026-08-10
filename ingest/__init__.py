"""Ingest: PubMed and bioRxiv clients feeding one `raw_paper` table.

Both source clients expose the same contract —
``fetch_abstracts(query, limit) -> list[RawPaper]`` — so the CLI treats them
interchangeably and a third source would only need to satisfy the same
signature.

Deliberately does not import the source clients or the DB layer at package
import time: `ingest.errors` and `ingest.models` are cheap and dependency-light,
while importing `ingest.pubmed` would drag in httpx for a caller that only
wanted an exception type.
"""

from ingest.errors import (
    ConfigurationError,
    IngestError,
    ResponseFormatError,
    ScanLimitError,
    TransportError,
)
from ingest.models import SOURCE_BIORXIV, SOURCE_PUBMED, RawPaper

__all__ = [
    "SOURCE_BIORXIV",
    "SOURCE_PUBMED",
    "ConfigurationError",
    "IngestError",
    "RawPaper",
    "ResponseFormatError",
    "ScanLimitError",
    "TransportError",
]
