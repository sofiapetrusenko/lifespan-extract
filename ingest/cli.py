"""`python -m ingest` — fetch, dedup and store raw abstracts in one command.

Ordering is not arbitrary. The database is opened and the schema created
*before* the first HTTP request, so a run with a missing or wrong
`DATABASE_URL` fails in a second instead of after several minutes of polite
rate-limited fetching.

Every count printed is measured, not estimated: "already present" comes from
the rows the database declined to insert, not from a guess. The abstract-less
warning is counted over the deduped records this run *offered* to the database,
not over the ones it inserted, so re-running the same query re-warns about rows
that were already there. A quiet run and a run that fetched nothing look
different on the terminal.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy.engine import Engine

from ingest import biorxiv, pubmed
from ingest.db import init_db, make_engine, store_papers
from ingest.dedup import dedup
from ingest.errors import IngestError
from ingest.models import RawPaper

DEFAULT_LIMIT = 100


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for `python -m ingest`."""
    parser = argparse.ArgumentParser(
        prog="python -m ingest",
        description=(
            "Fetch abstracts from PubMed and bioRxiv, dedup preprint/publication "
            "pairs by DOI, and store them. Re-running the same command stores "
            "nothing new."
        ),
    )
    parser.add_argument(
        "--query",
        required=True,
        help=(
            "Search terms. PubMed receives this verbatim, so its syntax works "
            "there; bioRxiv has no query API and matches plain keywords "
            "locally, so a boolean query is rejected rather than mis-run."
        ),
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_LIMIT,
        help=(
            "Maximum records to request FROM EACH SOURCE (default: "
            f"{DEFAULT_LIMIT}). A two-source run therefore fetches up to twice "
            "this many before dedup."
        ),
    )
    parser.add_argument(
        "--biorxiv-window-days",
        type=_positive_int,
        default=biorxiv.DEFAULT_WINDOW_DAYS,
        metavar="DAYS",
        help=(
            "How far back to scan bioRxiv (default: "
            f"{biorxiv.DEFAULT_WINDOW_DAYS}). Its API returns preprints by date, "
            "not by relevance, so this is the search scope; each extra 100 "
            "preprints in the window costs one request."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one ingest. Returns a process exit status."""
    args = build_parser().parse_args(argv)
    try:
        engine = make_engine()
        init_db(engine)
        return run_ingest(
            engine,
            query=args.query,
            limit=args.limit,
            biorxiv_window_days=args.biorxiv_window_days,
        )
    except IngestError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=sys.stderr)
        return 2


def run_ingest(
    engine: Engine,
    *,
    query: str,
    limit: int,
    biorxiv_window_days: int = biorxiv.DEFAULT_WINDOW_DAYS,
) -> int:
    """Fetch from both sources into `engine`, reporting what happened."""
    print(f"query:    {query!r}  (limit {limit} per source)")

    from_pubmed = pubmed.fetch_abstracts(query, limit)
    print(f"pubmed:   {len(from_pubmed)} record(s)")

    from_biorxiv = biorxiv.fetch_abstracts(
        query, limit, window_days=biorxiv_window_days
    )
    print(f"biorxiv:  {len(from_biorxiv)} record(s) "
          f"(last {biorxiv_window_days} day(s))")

    fetched = [*from_pubmed, *from_biorxiv]
    unique, dropped = dedup(fetched)
    print(
        f"dedup:    {len(fetched)} fetched -> {len(unique)} unique "
        f"({dropped} duplicate(s) collapsed by DOI)"
    )

    result = store_papers(engine, unique)
    print(
        f"db:       {result.inserted} new, {result.already_present} already "
        f"present  [{engine.url.render_as_string(hide_password=True)}]"
    )

    _warn_about_missing_abstracts(unique)
    if not unique:
        print("nothing matched: no records were fetched from either source.")
    return 0


def _warn_about_missing_abstracts(papers: Sequence[RawPaper]) -> None:
    """Report records stored without an abstract.

    They are stored rather than dropped — PubMed genuinely has abstract-less
    entries and discarding them would make the count disagree with PubMed's —
    but Phase 2 cannot extract from them, so the number is stated out loud.
    """
    missing = sum(1 for paper in papers if not paper.abstract)
    if missing:
        print(
            f"warning:  {missing} of {len(papers)} record(s) have no abstract; "
            "stored, but nothing can be extracted from them."
        )


def _positive_int(value: str) -> int:
    """argparse type for a count that must be at least 1."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {parsed}")
    return parsed
