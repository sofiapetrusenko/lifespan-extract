"""Engine construction and the one write path into `raw_paper`.

Production points at PostgreSQL through a required `DATABASE_URL`; there is no
default, because a default would silently write a hundred papers into a
throwaway SQLite file and look like success. Tests point the same code at
in-memory SQLite, which is why the insert is written against the two dialects'
shared `ON CONFLICT DO NOTHING` support rather than against Postgres alone.

Idempotence is the database's job, not Python's. `store_papers` issues
`INSERT ... ON CONFLICT DO NOTHING ... RETURNING dedup_key` and counts the rows
that came back, so "already present" is measured from what the database
actually did rather than inferred from a prior `SELECT` that another writer
could invalidate.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, NamedTuple

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from ingest.errors import ConfigurationError
from ingest.models import RawPaper

DATABASE_URL_ENV = "DATABASE_URL"

# Statements are chunked so a large run cannot exceed a driver's bind-parameter
# ceiling (psycopg's is 65535; ~11 columns per row leaves ample headroom here).
INSERT_CHUNK = 500

_INSERT_BUILDERS = {
    "postgresql": postgresql_insert,
    "sqlite": sqlite_insert,
}


class StoreResult(NamedTuple):
    """Counts from one `store_papers` call.

    They sum to the number of distinct dedup keys in the input, which is below
    the input size when one batch carries the same key twice — repeats are
    collapsed before the insert and are counted in neither field.
    """

    inserted: int
    already_present: int


def database_url() -> str:
    """Return `DATABASE_URL`, raising if it is unset or blank.

    No fallback by design: the project rule is that missing configuration
    raises rather than resolving to a placeholder.
    """
    value = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not value:
        raise ConfigurationError(
            f"{DATABASE_URL_ENV} is not set. Point it at the ingest database, e.g.\n"
            f"  export {DATABASE_URL_ENV}="
            "'postgresql+psycopg://user:password@localhost:5432/lifespan'"
        )
    return value


def make_engine(url: str | None = None) -> Engine:
    """Build an engine for `url`, defaulting to the `DATABASE_URL` environment.

    Guarantees the returned engine speaks a dialect `store_papers` supports.
    """
    resolved = url if url is not None else database_url()
    _reject_bare_postgres_scheme(resolved)
    # Checked before create_engine, which imports the DBAPI driver: an
    # unsupported URL should say so, not surface as ModuleNotFoundError.
    _check_dialect(_backend_name(resolved))

    if _is_memory_sqlite(resolved):
        # In-memory SQLite is per-connection: with a normal pool every checkout
        # would see a different, empty database. StaticPool keeps one
        # connection so the schema created at startup is still there at insert
        # time. Only reachable from tests; a production URL is PostgreSQL.
        return create_engine(
            resolved,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(resolved)


def init_db(engine: Engine) -> None:
    """Create `raw_paper` and its constraints if they do not exist yet.

    Phase 1 has no migration tool; `create_all` never alters an existing table,
    so a schema change after this point needs a real migration rather than a
    rerun of this function.
    """
    SQLModel.metadata.create_all(engine)


def store_papers(engine: Engine, papers: Sequence[RawPaper]) -> StoreResult:
    """Insert `papers`, skipping any whose identity is already in the table.

    Guarantees that running the same ingest twice inserts nothing the second
    time, and that no existing row is overwritten — a conflicting record is
    dropped, never merged, so re-ingesting can never silently rewrite data an
    extraction run has already been keyed against.

    The cost of that second guarantee: a stored row's identity can never be
    corrected either, so a preprint first seen unpublished keeps its preprint
    DOI even once bioRxiv reports a journal DOI for it. See "Known limitations"
    in NOTES.md.
    """
    unique = _unique_by_key(papers)
    if not unique:
        return StoreResult(0, 0)

    build_insert = _check_dialect(engine.dialect.name)
    table: Table = RawPaper.__table__
    inserted = 0
    with Session(engine) as session:
        for chunk in _chunks([paper.model_dump() for paper in unique], INSERT_CHUNK):
            statement = (
                build_insert(table)
                .values(chunk)
                .on_conflict_do_nothing()
                .returning(table.c.dedup_key)
            )
            inserted += len(session.execute(statement).scalars().all())
        session.commit()
    return StoreResult(inserted, len(unique) - inserted)


def _unique_by_key(papers: Sequence[RawPaper]) -> list[RawPaper]:
    """Drop repeats of a dedup key within one batch, keeping the first.

    `ingest.dedup` already applies the real preference policy; this is the
    safety net that makes `store_papers` correct when called directly, and it
    keeps the guarantee independent of how each dialect handles duplicate keys
    inside a single multi-row `VALUES` clause.
    """
    seen: dict[str, RawPaper] = {}
    for paper in papers:
        seen.setdefault(paper.dedup_key, paper)
    return list(seen.values())


def _check_dialect(name: str):
    """Return the dialect's `insert()` builder, raising if it has none.

    The upsert is what makes re-ingest a no-op, so a dialect without
    `ON CONFLICT DO NOTHING` is refused rather than silently falling back to a
    read-then-write that another writer could race.
    """
    try:
        return _INSERT_BUILDERS[name]
    except KeyError:
        raise ConfigurationError(
            f"unsupported database dialect {name!r}: ingest needs "
            "ON CONFLICT DO NOTHING, which only the "
            f"{sorted(_INSERT_BUILDERS)} dialects provide here. "
            f"Set {DATABASE_URL_ENV} to a PostgreSQL URL."
        ) from None


def _backend_name(url: str) -> str:
    """Return the dialect name of `url`, raising on a URL SQLAlchemy cannot read."""
    try:
        return make_url(url).get_backend_name()
    except ArgumentError as exc:
        raise ConfigurationError(
            f"{DATABASE_URL_ENV} is not a usable database URL: {exc}\n"
            "  Expected form: postgresql+psycopg://user:password@host:5432/database"
        ) from exc


def _reject_bare_postgres_scheme(url: str) -> None:
    """Raise on `postgresql://`, which SQLAlchemy resolves to psycopg2.

    `requirements.txt` pins psycopg 3, so the bare scheme fails deep inside
    SQLAlchemy with "No module named 'psycopg2'" — an error that reads like a
    broken install rather than a URL that needs one word added.
    """
    scheme = url.split("://", 1)[0]
    if scheme in {"postgres", "postgresql"}:
        raise ConfigurationError(
            f"{DATABASE_URL_ENV} uses the bare {scheme!r} scheme, which "
            "SQLAlchemy resolves to the psycopg2 driver; this project installs "
            "psycopg 3. Write the driver explicitly:\n"
            "  postgresql+psycopg://user:password@host:5432/database"
        )


def _is_memory_sqlite(url: str) -> bool:
    """True for the SQLite URLs that address a per-connection memory database."""
    return url in {"sqlite://", "sqlite:///:memory:"}


def _chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Yield `rows` in lists of at most `size`."""
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
