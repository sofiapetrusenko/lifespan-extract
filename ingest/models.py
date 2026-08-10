"""`RawPaper` — the single row shape both source clients produce.

Only the fields Phase 2 will need to build a `schema/experiment.schema.json`
record (`paper.doi/title/year/source/pmid`), plus the abstract itself, plus the
identity columns dedup depends on. Nothing speculative.

Identity is deliberately three columns, not one:

* `doi` is the *canonical* DOI of the work. For a bioRxiv preprint that has
  since been published, this is the **journal** DOI reported in the API's
  `published` field, not the `10.1101/...` preprint DOI. Without that
  substitution "dedup preprint/publication by DOI" could never fire, because a
  preprint DOI and its publication's DOI are never equal. It fires only when
  both records reach the same run: a preprint stored while still unpublished
  keeps its preprint DOI as `doi` permanently, since correcting it would mean
  rewriting a stored primary key. Recorded under "Known limitations" in
  NOTES.md and pinned by a test.
* `source_id` is the record's identity *within* its source: the PMID for
  PubMed, the `10.1101/...` preprint DOI for bioRxiv. It never changes.
* `dedup_key` is the primary key and the thing the database enforces. It is
  `doi:<doi>` when a DOI is known, and `<source>:<source_id>` when one is not.

The `(source, source_id)` unique constraint closes the gap the DOI key leaves
open: a PubMed record ingested before its publisher registered a DOI lands
under `pubmed:<pmid>`, and would otherwise be inserted a second time under
`doi:<doi>` once the DOI appears. With both constraints in place the second
insert conflicts and is skipped.
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

SOURCE_PUBMED = "pubmed"
SOURCE_BIORXIV = "biorxiv"
SOURCES = (SOURCE_PUBMED, SOURCE_BIORXIV)


def compute_dedup_key(source: str, source_id: str, doi: str | None) -> str:
    """Return the cross-source identity key for one record.

    A DOI, where present, is the key: it is the only identifier a preprint and
    its published version can share. Records without one fall back to their
    source-local id, which keeps two distinct DOI-less papers distinct rather
    than collapsing them onto a shared sentinel.
    """
    if not source_id:
        raise ValueError("source_id is required to compute a dedup key")
    if doi:
        return f"doi:{doi}"
    return f"{source}:{source_id}"


def normalise_doi(doi: str | None) -> str | None:
    """Lowercase and strip a DOI, mapping blank and bioRxiv's "NA" to None.

    The schema calls for a lowercase canonical DOI, and DOIs are
    case-insensitive, so casing differences between PubMed and bioRxiv must not
    produce two rows for one paper.
    """
    if doi is None:
        return None
    cleaned = doi.strip().rstrip(".").lower()
    if not cleaned or cleaned == "na":
        return None
    return cleaned


class RawPaper(SQLModel, table=True):
    """One ingested abstract, before any classification or extraction."""

    __tablename__ = "raw_paper"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_raw_paper_source_id"),
        CheckConstraint(
            "source IN ('pubmed', 'biorxiv')",
            name="ck_raw_paper_source",
        ),
    )

    dedup_key: str = Field(primary_key=True)
    source: str = Field(index=True)
    source_id: str
    doi: str | None = Field(default=None, index=True)
    pmid: str | None = Field(default=None)
    title: str
    abstract: str | None = Field(default=None)
    year: int | None = Field(default=None)
    first_author: str | None = Field(default=None)
    url: str | None = Field(default=None)
    # timezone=True, so PostgreSQL uses `timestamptz`. The default mapping is
    # `timestamp without time zone`, which would silently discard the offset
    # from the UTC values written here and leave the column ambiguous.
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    @classmethod
    def build(
        cls,
        *,
        source: str,
        source_id: str,
        title: str,
        doi: str | None = None,
        pmid: str | None = None,
        abstract: str | None = None,
        year: int | None = None,
        first_author: str | None = None,
        url: str | None = None,
        fetched_at: datetime | None = None,
    ) -> "RawPaper":
        """Construct a row with its DOI normalised and its dedup key derived.

        The only supported way to make a `RawPaper`: constructing one directly
        would require the caller to compute `dedup_key`, and two callers
        computing it separately is how dedup rules drift apart.
        """
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
        if not title.strip():
            raise ValueError(f"{source} record {source_id!r} has an empty title")
        canonical_doi = normalise_doi(doi)
        return cls(
            dedup_key=compute_dedup_key(source, source_id, canonical_doi),
            source=source,
            source_id=source_id,
            doi=canonical_doi,
            pmid=pmid,
            title=title.strip(),
            abstract=abstract,
            year=year,
            first_author=first_author,
            url=url,
            fetched_at=fetched_at or datetime.now(timezone.utc),
        )
