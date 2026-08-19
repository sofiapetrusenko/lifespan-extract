"""`python -m extract` — classify and extract stored papers in one command.

Ordering is not arbitrary. The API client and the database are both opened
before the first paper is read, so a run with a missing `ANTHROPIC_API_KEY` or
a wrong `DATABASE_URL` fails in a second rather than part-way through a batch.
The output directory is probed for writability in the same breath and for the
same reason — see `_check_writable`.

Per-paper failures are logged and counted, and the run continues: PLAN.md wants
twenty papers processed without manual intervention, and one unparseable
response — or one 529 from an overloaded API, or one unwritable path — should
not strand the other nineteen. That holds because `extract/model.py` wraps
every SDK failure as a `ModelCallError`, so transport errors are
`ExtractError`s the loop below catches like any other, and because the loop
catches `OSError` too: the model call for a paper is already paid for by the
time its record is written. A record that cannot be serialised as JSON at all
is the same kind of failure and is named as one at the write, rather than
reaching `main` as the bare `ValueError` it arrives as — see `_write_record`.
Nothing is swallowed — every failure is printed with its full message and the
exit status is non-zero if any occurred.

Idempotence is per (paper, schema_version), as the key invariants require: the
output path carries the schema version, and a paper whose record is already on
disk is skipped rather than re-extracted or overwritten. Bumping the schema
therefore forces a fresh extraction instead of silently reusing an old record.
That marker is a file's existence and nothing else, so records are written
atomically (see `_write_record`) and never into `data/gold/` (see
`resolve_out_root`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from extract.classify import classify
from extract.errors import ExtractError, OutputPathError, RecordValidationError
from extract.extract import ABSTRACT, extract_record, schema_document
from extract.model import ModelClient, make_client
from extract.schema import schema_version
from ingest.db import init_db, make_engine
from ingest.errors import IngestError
from ingest.models import RawPaper

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = REPO_ROOT / "data" / "extracted"
GOLD_ROOT = REPO_ROOT / "data" / "gold"
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
        summary = run_extraction(
            engine, client=client, limit=args.limit, out_root=args.out,
            argv=list(argv) if argv is not None else sys.argv[1:],
        )
    except OutputPathError as exc:
        # Ahead of the clause below, which would otherwise take it: this is the
        # one failure here that is a usage error rather than a run failure, and
        # exit 2 is what argparse already uses to say so. It is matched by its
        # own type and not by `ValueError` — a `ValueError` catch here also
        # catches the `UnicodeEncodeError` a model payload can raise at the
        # write, which is a per-paper data failure and belongs nowhere near an
        # "invalid argument" message or an exit status that means one.
        print(f"invalid argument: {exc}", file=sys.stderr)
        return 2
    except (ExtractError, IngestError, OSError) as exc:
        # OSError belongs here for the setup failures: an --out that names an
        # existing file, a read-only output directory, a database path that
        # cannot be opened. Every one of them is the operator's to fix, and
        # deserves the message rather than a traceback.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 1 if summary.failed else 0


def queue_size(engine: Engine, out_dir: Path) -> int:
    """Count the papers eligible for extraction right now.

    The same three conditions `_papers` applies, without the limit: a row is in
    the queue when it has an abstract and has no record under `out_dir` yet.
    Reported in the header so a run's counts can be read against the work that
    was available when it started — a run that considered 30 of 30 and one that
    considered 30 of 400 print the same summary line otherwise.
    """
    with Session(engine) as session:
        return sum(
            1
            for paper in session.exec(select(RawPaper))
            if paper.abstract
            and paper.abstract.strip()
            and not record_path(out_dir, paper).exists()
        )


def run_extraction(
    engine: Engine,
    *,
    client: ModelClient,
    limit: int,
    out_root: Path,
    argv: Sequence[str] | None = None,
) -> RunSummary:
    """Process up to `limit` papers, writing one record per extracted paper.

    Refuses an `out_root` inside `data/gold/` before anything is written; see
    `resolve_out_root`.

    `argv` is recorded in the header and used for nothing else. It is a
    parameter rather than a read of `sys.argv` because this function is called
    directly by tests and could be called by a future caller that is not this
    CLI, and a header claiming an argv the run did not have is worse than one
    that says the run was driven programmatically.
    """
    version = schema_version(schema_document())
    out_dir = resolve_out_root(out_root) / version
    out_dir.mkdir(parents=True, exist_ok=True)
    _check_writable(out_dir)

    # Provenance first, before any paper line. Without it a log records what a
    # run did but not what produced it: neither the 2026-08-17 nor the
    # 2026-08-19 log carries the argv, the limit, the queue size or a
    # timestamp, so "30 considered" cannot be tied to `--limit 30` by anyone
    # reading the file, and a pass assembling PR evidence from those logs had
    # to stop. Each line is `key:` at a fixed width so the block greps as
    # fields rather than prose.
    print(f"started:  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"argv:     {shlex.join(argv) if argv is not None else '(called directly)'}")
    print(f"limit:    {limit}")
    print(f"out_dir:  {out_dir}")
    print(f"schema:   v{version}  ->  {out_dir}")
    print(f"queued:   {queue_size(engine, out_dir)} paper(s) eligible at start")

    summary = RunSummary()
    for paper in _papers(engine, limit=limit, out_dir=out_dir, summary=summary):
        summary.considered += 1
        try:
            _process(paper, client=client, out_dir=out_dir, summary=summary)
        except (ExtractError, OSError) as exc:
            # Loud and counted, but not fatal: the remaining papers still run,
            # and the non-zero exit status carries the failure to the caller.
            # OSError is here because the write is the last step of a paper
            # whose model calls have already been made and paid for; letting
            # one unwritable record end the run would strand every paper after
            # it and lose the summary with it.
            summary.failed += 1
            print(f"FAIL      {paper.dedup_key}\n          {exc}", file=sys.stderr)

    _report(summary)
    return summary


def _check_writable(out_dir: Path) -> None:
    """Abort the run if records cannot be written to `out_dir`, before paying for one.

    `mkdir(exist_ok=True)` says nothing about writability: an existing
    read-only directory satisfies it, and so does a full disk. The failure then
    lands at the write, which is the last step of a paper whose classify and
    extract calls have already been made and paid for — and because that
    `OSError` is per-paper by design, every paper in the batch buys its model
    calls and throws them away: one at the gate and, for a paper that passes
    it, two more at extraction. Sixty wasted calls on a twenty-paper run, where
    one probe costs nothing.

    Probing with the same `tempfile.mkstemp` the real write uses, in the real
    output directory, so what is tested is what is later relied on rather than
    an approximation of it such as `os.access`.
    """
    try:
        descriptor, name = tempfile.mkstemp(dir=out_dir, prefix=".writable-", suffix=".tmp")
    except OSError as exc:
        raise ExtractError(
            f"cannot write records to {out_dir}: {exc.strerror or exc}. Nothing was "
            "extracted and no model call was made, because a record that cannot be "
            "stored is a paid-for extraction thrown away. Make the directory "
            "writable, or pass a writable --out."
        ) from exc
    os.close(descriptor)
    Path(name).unlink(missing_ok=True)


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
    _write_record(path, record)
    summary.extracted += 1
    print(
        f"ok        {paper.dedup_key}  "
        f"{len(record['experiments'])} experiment(s) -> {path.name}"
    )


def resolve_out_root(out_root: Path) -> Path:
    """Return `out_root` as a real path, refusing anywhere inside `data/gold/`.

    Raises `OutputPathError` on a refusal, which is the type `main` grades as a
    usage error worth exit 2 rather than as a failed run.

    An extracted record is shape-identical to a gold one and validates against
    the same schema, so a run written into `data/gold/` would put unreviewed
    model output into the hand-labelled ground truth with nothing to tell the
    two apart — and Phase 3 would score the model against its own output. The
    working agreement is that nothing in this repository writes there.

    Both sides are resolved first, because `data/extracted/../gold`, a symlink
    to the gold directory and `data/gold` are one directory spelled three ways.
    The comparison is per path component and case-insensitive: the development
    filesystem is APFS, where `data/GOLD` opens the same directory as
    `data/gold`, and comparing components rather than strings keeps a sibling
    named `data/goldilocks` out of it. Case is folded everywhere rather than
    only where the filesystem folds it — on a case-sensitive filesystem
    `data/GOLD` is a different directory, and refusing it there too is the
    harmless direction to be wrong in.
    """
    resolved = out_root.resolve()
    gold = GOLD_ROOT.resolve()
    prefix = _components(gold)
    if _components(resolved)[: len(prefix)] == prefix:
        raise OutputPathError(
            f"--out {out_root} resolves to {resolved}, which is inside the gold set "
            f"at {gold}. That directory is human-labelled ground truth and an "
            "extracted record is indistinguishable from a gold one, so nothing "
            "here may write into it. Choose an output directory outside it."
        )
    return resolved


def _components(path: Path) -> list[str]:
    """Path parts, case-folded, for comparison on a case-insensitive filesystem."""
    return [part.casefold() for part in path.parts]


def _write_record(path: Path, record: dict[str, Any]) -> None:
    """Write `record` to `path` atomically, leaving nothing partial behind.

    A record file's existence is the only idempotence marker there is — there
    is no database row saying a paper was extracted — so a half-written file is
    worse than no file: every later run reads it as "already extracted", the
    paper is never re-extracted, and the truncated JSON flows into Phase 3.
    `write_text` produces exactly that, because it truncates the destination
    when it opens it and a Ctrl-C or a short write can land in between.

    So the bytes go to a temporary file that is renamed over the destination:
    the rename is atomic, and it is in the destination's own directory so the
    rename cannot cross a filesystem boundary and silently become a copy. The
    temporary file is removed on every path out, including `KeyboardInterrupt`,
    which is the interruption this guards against.

    Two ways a record built from model output cannot become JSON at all are
    caught here and re-raised as `RecordValidationError`, so the per-paper
    handler counts one and the batch continues:

    - `allow_nan=False` refuses `NaN` and `Infinity`. `json.dumps` otherwise
      emits those bare tokens, which are not JSON: `JSON.parse` throws, serde
      rejects them, and `jq` rewrites them to `null` — turning a bogus number
      into a fabricated absence, which is the `not_reported` rule inverted.
      `extract/model.py` already refuses them on the way in; this is the second
      lock on the same door, and the one that would also catch a non-finite
      float this file itself introduced.
    - A lone surrogate in model-derived text encodes to nothing under UTF-8.
      The `UnicodeEncodeError` it raises is a `ValueError`, so until it was
      named here it escaped the per-paper handler and was mistaken by `main`
      for an invalid command-line argument.

    Neither may leave a file behind. The record file's existence is the only
    "already extracted" marker there is, so a paper whose record was refused
    has to still look pending to the next run: both raise before `os.replace`,
    and the temporary file is removed in the `finally` as always.
    """
    try:
        body = json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError as exc:
        raise RecordValidationError(
            f"the record for {path.name} cannot be serialised as JSON: {exc}. "
            "Nothing was written, so the paper is still pending and a re-run "
            "will attempt it again."
        ) from exc

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(temporary, path)
    except UnicodeEncodeError as exc:
        raise RecordValidationError(
            f"the record for {path.name} holds text with no UTF-8 encoding: "
            f"{exc}. Nothing was written, so the paper is still pending and a "
            "re-run will attempt it again."
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


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
