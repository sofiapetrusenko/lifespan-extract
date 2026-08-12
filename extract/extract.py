"""Structured extraction of experiment records from one paper's text.

Second stage of the cascade: the expensive model, run only on papers the gate
passed. Output is a complete `schema/experiment.schema.json` record — one
`experiments[]` entry per (organism, intervention) pair, so a multi-organism
paper yields several.

Three parts of the record are assembled here rather than asked for, because
they are not claims about the prose and a model would be guessing at them:

* `paper` — DOI, title, year, source and PMID come from the ingest row. The
  schema says so explicitly, and it means a wrong DOI is an ingest bug rather
  than a hallucination.
* `extracted_from` — a fact about the text the caller supplied. Stamped onto
  every claim from the caller's own argument, so a record can never claim
  full-text provenance for something read in an abstract.
* `experiment_id` — derived identity, generated from the schema's stated
  convention (`<first-author-year>-<organism-slug>-<agent-slug>[-<n>]`).

Everything else — values, quotes and confidences — comes from the model and is
validated before it is returned: against the real schema, then against both
halves of the source-quote invariant — a quote is present exactly when a value
is, and every quote is verbatim in the text this call was handed. A record that
fails any of the three raises; nothing partial is emitted.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from extract.errors import ExtractError, ModelResponseError
from extract.model import ModelClient, call_structured
from extract.schema import (
    CLAIM_KEYS,
    build_extraction_schema,
    check_provenance,
    check_quotes_verbatim,
    load_schema,
    schema_version,
    validate_record,
)
from ingest.models import RawPaper

EXTRACTION_MODEL = "claude-opus-5"

# A multi-organism paper can run to several thousand tokens of JSON. Comfortably
# above that, and below the point where a non-streaming request risks an HTTP
# timeout.
MAX_TOKENS = 16000

ABSTRACT = "abstract"
FULL_TEXT = "full_text"
EXTRACTED_FROM = (ABSTRACT, FULL_TEXT)

SYSTEM_PROMPT = """\
You extract lifespan-intervention experiments from the text of a paper into a
fixed JSON schema.

Record one experiment per (organism, intervention) pair. A paper that reports
the same compound in worms, flies and mice yields three entries; a paper that
reports two compounds in one organism also yields two. Cohorts that differ only
by sex or dose belong to the same entry unless the paper reports their lifespan
results separately.

Every field carries the sentence it came from:
- source_quote is copied verbatim from the text above — never paraphrased,
  never assembled from two places, never written by you.
- confidence is high when the text states the value outright, medium when it
  takes an inference the text supports, low when you are reading between lines.

Absent data is recorded as absent, never inferred:
- Use null for a number, a percentage, a p-value, a dose, a strain-free species
  or a mechanism the text does not give.
- Use "not_reported" for sex and strain when the text does not say. Sex is
  usually in the methods, so an abstract will often not report it.
- When a value is null or "not_reported", source_quote is null too. There is
  nothing to quote.

Care with the fields that are easy to conflate:
- median, mean and maximum lifespan changes are three different numbers. Fill
  in only the ones the text reports, and never move a value from one to another.
- direction is "no_effect" whenever the paper reports no significant change,
  however it is phrased ("did not significantly alter", "no effect on median
  lifespan"). Do not soften that into a small increase.
- organism is one of the listed values; anything else is "other", and then
  species carries the actual species as free text.
- mechanism is the mechanism the authors state, not one you infer from the
  intervention.
"""

_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")

_document: dict[str, Any] | None = None
_extraction_schema: dict[str, Any] | None = None


def schema_document() -> dict[str, Any]:
    """Return the parsed schema, loading it once per process.

    Cached because the same object is handed to the validator, which compiles
    it; reloading per paper would recompile per paper.
    """
    global _document
    if _document is None:
        _document = load_schema()
    return _document


def extraction_schema() -> dict[str, Any]:
    """Return the API-subset request schema derived from the real one."""
    global _extraction_schema
    if _extraction_schema is None:
        _extraction_schema = build_extraction_schema(schema_document())
    return _extraction_schema


def extract_record(
    paper: RawPaper,
    text: str,
    *,
    client: ModelClient,
    extracted_from: str = ABSTRACT,
) -> dict[str, Any]:
    """Return a schema-valid record for `paper`, extracted from `text`.

    Guarantees the returned record validates against
    `schema/experiment.schema.json`, that every claim carries a quote
    consistent with its value, that every quote is a verbatim slice of `text`,
    and that `extracted_from` on every claim is the value passed in here.
    Raises otherwise — there is no partial record.
    """
    if extracted_from not in EXTRACTED_FROM:
        raise ValueError(f"extracted_from must be one of {EXTRACTED_FROM}, got {extracted_from!r}")
    if not text or not text.strip():
        raise ExtractError(
            f"cannot extract from {paper.dedup_key}: no text was supplied."
        )

    payload = call_structured(
        client,
        model=EXTRACTION_MODEL,
        system=SYSTEM_PROMPT,
        user=f"Title: {paper.title}\n\n{text.strip()}",
        schema=extraction_schema(),
        max_tokens=MAX_TOKENS,
    )

    document = schema_document()
    record = {
        "schema_version": schema_version(document),
        "paper": _paper_metadata(paper),
        "experiments": _experiments(payload, paper, extracted_from),
    }
    validate_record(record, document)
    check_provenance(record)
    check_quotes_verbatim(record, text)
    return record


def _paper_metadata(paper: RawPaper) -> dict[str, Any]:
    """Return the record's `paper` block, raising on identity ingest never got.

    No defaults: a record whose DOI or year was invented is worse than no
    record, because the DOI is the dedup key every later phase joins on.
    """
    for field in ("doi", "year", "title", "source"):
        if not getattr(paper, field, None):
            raise ExtractError(
                f"{paper.dedup_key}: paper.{field} is missing, so no schema record "
                "can be built. It comes from the source API at ingest time and is "
                "never guessed here — re-ingest the paper or skip it."
            )
    return {
        "doi": paper.doi,
        "title": paper.title,
        "year": paper.year,
        "source": paper.source,
        "pmid": paper.pmid,
    }


def _experiments(
    payload: dict[str, Any], paper: RawPaper, extracted_from: str
) -> list[dict[str, Any]]:
    """Return the payload's experiments, stamped with provenance and identity."""
    experiments = payload.get("experiments")
    if not isinstance(experiments, list) or not all(isinstance(e, dict) for e in experiments):
        raise ModelResponseError.from_payload(
            reason="model payload has no 'experiments' array of objects",
            payload=repr(payload),
        )
    if not experiments:
        raise ExtractError(
            f"{paper.dedup_key}: the classifier passed this paper but the extractor "
            "returned no experiments. Reported rather than resolved — inventing a "
            "record here would defeat the point of the gate."
        )

    taken: set[str] = set()
    for experiment in experiments:
        _stamp_provenance(experiment, extracted_from, paper)
        experiment["experiment_id"] = _experiment_id(paper, experiment, taken)
        taken.add(experiment["experiment_id"])
    return experiments


def _stamp_provenance(node: Any, extracted_from: str, paper: RawPaper) -> None:
    """Write `extracted_from` into every claim wrapper under `node`, in place.

    Raises if the model supplied the key itself: the request schema forbids it,
    so its presence means the response did not follow the schema, and silently
    overwriting a model's provenance claim would hide that.
    """
    if isinstance(node, dict):
        if CLAIM_KEYS <= set(node):
            if "extracted_from" in node:
                raise ModelResponseError.from_payload(
                    reason=(
                        f"{paper.dedup_key}: model supplied 'extracted_from', which "
                        "is assigned by the caller and is not in the request schema"
                    ),
                    payload=repr(node),
                )
            node["extracted_from"] = extracted_from
            return
        for child in node.values():
            _stamp_provenance(child, extracted_from, paper)
    elif isinstance(node, list):
        for child in node:
            _stamp_provenance(child, extracted_from, paper)


def _experiment_id(paper: RawPaper, experiment: dict[str, Any], taken: set[str]) -> str:
    """Return this experiment's id, following the schema's naming convention.

    `<first-author-year>-<organism-slug>-<agent-slug>`, with a `-2`, `-3` … tail
    when one paper reports the same (organism, agent) pair more than once.
    """
    if not paper.first_author:
        raise ExtractError(
            f"{paper.dedup_key}: no first author, so the experiment_id convention "
            "<first-author-year>-<organism>-<agent> cannot be followed. Collective "
            "authorships hit this; skip the paper rather than naming it something else."
        )

    parts = [
        f"{_slug(paper.first_author)}{paper.year}",
        _organism_slug(experiment),
        _slug(_claim_value(experiment, ("intervention", "agent"))),
    ]
    if not all(parts):
        raise ExtractError(
            f"{paper.dedup_key}: cannot build an experiment_id from parts {parts}; "
            "the organism or agent came back empty or unusable as an identifier."
        )

    stem = "-".join(parts)
    candidate, suffix = stem, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{stem}-{suffix}"
    if not _ID_PATTERN.match(candidate):
        raise ExtractError(
            f"{paper.dedup_key}: generated experiment_id {candidate!r} does not match "
            "the schema's identifier pattern."
        )
    return candidate


def _organism_slug(experiment: dict[str, Any]) -> str:
    """Return the organism component of an id, preferring species when it exists.

    `organism` collapses everything outside the MVP enum to `other`, which would
    give a multi-organism paper several identical ids; `species` is what tells
    the yeast record from the fly one.
    """
    organism = _claim_value(experiment, ("organism",))
    if organism != "other":
        return _binomial_slug(organism)
    species = _claim_value(experiment, ("species",), required=False)
    return _binomial_slug(species) if species else "other"


def _claim_value(experiment: dict[str, Any], path: tuple[str, ...], *, required: bool = True) -> str:
    """Return the string value of a claim at `path`, raising when it is unusable."""
    node: Any = experiment
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    value = node.get("value") if isinstance(node, dict) else None
    if isinstance(value, str) and value.strip():
        return value
    if required:
        raise ModelResponseError.from_payload(
            reason=f"experiment has no usable {'.'.join(path)}.value (got {value!r})",
            payload=repr(experiment),
        )
    return ""


def _slug(value: str) -> str:
    """Lowercase ASCII, hyphen-separated: 'Harrison' -> 'harrison'."""
    return re.sub(r"[^a-z0-9]+", "-", _ascii(value).lower()).strip("-")


def _binomial_slug(value: str) -> str:
    """Slug for a species name: 'M. musculus' -> 'mmusculus'.

    An abbreviated genus joins the epithet without a separator, matching the
    ids in `data/gold/` (`eisenberg2009-scerevisiae-spermidine`). Names that are
    not binomials fall through to ordinary hyphenation.
    """
    tokens = [token.lower() for token in re.split(r"[^A-Za-z0-9]+", _ascii(value)) if token]
    merged: list[str] = []
    for token in tokens:
        if merged and len(merged[-1]) == 1:
            merged[-1] += token
        else:
            merged.append(token)
    return "-".join(merged)


def _ascii(value: str) -> str:
    """Return `value` with accents folded and non-ASCII characters dropped."""
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
