"""In-batch dedup policy: which record wins when two describe one paper.

The database enforces uniqueness *across* runs (see `ingest.models`); this
module decides *which* of several candidates in a single run is the one worth
keeping. The two jobs are separate on purpose — a unique constraint can reject
a duplicate but cannot express "prefer the published version over the
preprint".

Preference order is PubMed before bioRxiv. When a preprint has been published,
the journal record is the version of record: it carries the final abstract, a
PMID, and the peer-reviewed wording that the gold set's `source_quote` values
are transcribed from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from ingest.models import SOURCE_BIORXIV, SOURCE_PUBMED, RawPaper

SOURCE_PRIORITY = {SOURCE_PUBMED: 0, SOURCE_BIORXIV: 1}


class DedupResult(NamedTuple):
    """Kept records in first-seen order, plus how many were dropped."""

    papers: list[RawPaper]
    dropped: int


def dedup(papers: Sequence[RawPaper]) -> DedupResult:
    """Collapse records sharing a dedup key, keeping the published version.

    Guarantees the returned list has no two records with the same `dedup_key`,
    and preserves the order in which each key was first seen so the CLI's
    output is stable across runs.
    """
    chosen: dict[str, RawPaper] = {}
    order: list[str] = []
    for paper in papers:
        key = paper.dedup_key
        incumbent = chosen.get(key)
        if incumbent is None:
            chosen[key] = paper
            order.append(key)
            continue
        if _rank(paper) < _rank(incumbent):
            chosen[key] = paper
    return DedupResult([chosen[key] for key in order], len(papers) - len(order))


def _rank(paper: RawPaper) -> int:
    """Lower sorts first. Unknown sources rank last rather than crashing dedup."""
    return SOURCE_PRIORITY.get(paper.source, len(SOURCE_PRIORITY))
