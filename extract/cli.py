"""`python -m extract` — classify and extract stored papers in one command.

Ordering is not arbitrary. The API client and the database are both opened
before the first paper is read, so a run with a missing `ANTHROPIC_API_KEY` or
a wrong `DATABASE_URL` fails in a second rather than part-way through a batch.

Per-paper failures are logged and counted, and the run continues: PLAN.md wants
twenty papers processed without manual intervention, and one unparseable
response — or one 529 from an overloaded API — should not strand the other
nineteen. That holds because `extract/model.py` wraps every SDK failure as a
`ModelCallError`, so transport errors are `ExtractError`s the loop below
catches like any other. Nothing is swallowed — every failure is printed with
its full message and the exit status is non-zero if any occurred.

Idempotence is per (paper, schema_version), as the key invariants require: the
output path carries the schema version, and a paper whose record is already on
disk is skipped rather than re-extracted or overwritten. Bumping the schema
therefore forces a fresh extraction instead of silently reusing an old record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from extract.classify import classify
from extract.errors import ExtractError
from extract.extract import ABSTRACT, extract_record, schema_document
from extract.model import ModelClient, make_client
from extract.schema import schema_version
from ingest.db import init_db, make_engine
from ingest.errors import IngestError
from ingest.models import RawPaper

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = REPO_ROOT / "data" / "extracted"
DEFAULT_LIMIT = 20


@dataclass
class RunSummary:
    """Measured counts from one run. Every number comes from a decision taken."""

    considered: int = 0
    no_abstract: int = 0
    already_extracted: int = 0
    screened_out: int = 0
    extracted: int = 0
    failed: int = 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for `python -m extract`."""
    parser = argparse.ArgumentParser(
        prog="python -m extract",
        description=(
            "Classify ingested papers with the cheap gate, extract experiment "
            "records from the ones that pass, and write them as schema-valid "
            "JSON. Re-running writes nothing new for papers already extracted "
            "against the current schema version."
        ),
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_LIMIT,
        help=(
            f"How many not-yet-extracted papers to process (default: {DEFAULT_LIMIT}). "
            "Papers already extracted against this schema version do not count "
            "toward it."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_ROOT,
        metavar="DIR",
        help=(
            f"Root directory for extracted records (default: {DEFAULT_OUT_ROOT}). "
            "Records are written to <DIR>/<schema-version>/."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one extraction pass. Returns a process exit status."""
    args = build_parser().parse_args(argv)
    try:
        client = make_client()
        engine = make_engine()
        init_db(engine)
        summary = run_extraction(engine, client=client, limit=args.limit, out_root=args.out)
    except (ExtractError, IngestError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=sys.stderr)
        return 2
    return 1 if summary.failed else 0


def run_extraction(
    engine: Engine,
    *,
    client: ModelClient,
    limit: int,
    out_root: Path,
) -> RunSummary:
    """Process up to `limit` papers, writing one record per extracted paper."""
    version = schema_version(schema_document())
    out_dir = out_root / version
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"schema:   v{version}  ->  {out_dir}")

    summary = RunSummary()
    for paper in _papers(engine, limit=limit, out_dir=out_dir, summary=summary):
        summary.considered += 1
        try:
            _process(paper, client=client, out_dir=out_dir, summary=summary)
        except ExtractError as exc:
            # Loud and counted, but not fatal: the remaining papers still run,
            # and the non-zero exit status carries the failure to the caller.
            summary.failed += 1
            print(f"FAIL      {paper.dedup_key}\n          {exc}", file=sys.stderr)

    _report(summary)
    return summary


def _papers(
    engine: Engine, *, limit: int, out_dir: Path, summary: RunSummary
) -> Iterator[RawPaper]:
    """Yield up to `limit` papers that still need extracting.

    Skipped papers — no abstract, or already extracted — are counted here and do
    not consume the limit, so `--limit 20` means twenty attempts rather than
    twenty rows read.
    """
    yielded = 0
    with Session(engine) as session:
        for paper in session.exec(select(RawPaper).order_by(RawPaper.dedup_key)):
            if yielded >= limit:
                return
            if not paper.abstract or not paper.abstract.strip():
                summary.no_abstract += 1
                continue
            if record_path(out_dir, paper).exists():
                summary.already_extracted += 1
                continue
            yielded += 1
            yield paper


def _process(paper: RawPaper, *, client: ModelClient, out_dir: Path, summary: RunSummary) -> None:
    """Classify one paper and, if it passes, extract and write its record.

    `_papers` has already established that the abstract is non-empty; both
    `classify` and `extract_record` re-check and raise, so a caller that skips
    that filter still fails loudly rather than sending an empty prompt.
    """
    abstract = paper.abstract or ""
    decision = classify(paper.title, abstract, client=client)
    if not decision.relevant:
        summary.screened_out += 1
        print(f"skip      {paper.dedup_key}  ({decision.confidence}) {decision.reason}")
        return

    record = extract_record(paper, abstract, client=client, extracted_from=ABSTRACT)
    path = record_path(out_dir, paper)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    summary.extracted += 1
    print(
        f"ok        {paper.dedup_key}  "
        f"{len(record['experiments'])} experiment(s) -> {path.name}"
    )


def record_path(out_dir: Path, paper: RawPaper) -> Path:
    """Return the file a paper's record lives in, for this schema version.

    The name carries a readable slug of the dedup key plus a digest of it. The
    digest is what makes the mapping injective: two dedup keys can slugify to
    the same string, and a collision would look exactly like "already
    extracted" — the one silent failure this file cannot afford.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", paper.dedup_key.lower()).strip("-")
    digest = hashlib.sha256(paper.dedup_key.encode("utf-8")).hexdigest()[:8]
    return out_dir / f"{slug}-{digest}.json"


def _report(summary: RunSummary) -> None:
    """Print the run's measured counts."""
    print(
        f"papers:   {summary.considered} considered, "
        f"{summary.extracted} extracted, "
        f"{summary.screened_out} screened out, "
        f"{summary.failed} failed"
    )
    if summary.already_extracted:
        print(
            f"skipped:  {summary.already_extracted} already extracted against "
            "this schema version"
        )
    if summary.no_abstract:
        print(
            f"warning:  {summary.no_abstract} stored paper(s) have no abstract; "
            "nothing can be extracted from them."
        )
    if not summary.considered:
        print("nothing to do: no stored paper needed extracting.")


def _positive_int(value: str) -> int:
    """argparse type for a count that must be at least 1."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {parsed}")
    return parsed
