"""`schema/experiment.schema.json` as the extractor sees it.

Three jobs, all keyed to the same file so there is exactly one source of truth:

1. **Derive the request schema.** The structured-output subset the API accepts
   is narrower than draft 2020-12: no `pattern`, no numeric or length bounds,
   no `unevaluatedProperties`, and every object must close with
   `additionalProperties: false`. So the schema sent to the model is *derived
   programmatically* from the real one — `allOf` composition flattened, `$ref`s
   inlined, unsupported keywords dropped. Hand-copying a second schema would
   guarantee the two drift the first time the real one changes.

2. **Decide what the model is not asked for.** Three things in the record are
   not claims about the paper's prose and are therefore not the model's to
   invent: `experiment_id` (derived identity — the schema itself says Phase 2
   generates it from a naming convention), `notes` (human labeller commentary,
   never scored), and every claim's `extracted_from` (a property of the text
   the caller supplied, not of the paper — the model cannot know, and must not
   assert, whether it was handed an abstract or a full text). They are pruned
   from the request schema and filled in by `extract.extract`.

3. **Check the assembled record.** Validation runs against the *real* schema,
   not the derived one, so the constraints the API subset cannot express —
   the DOI and p-value patterns, `minItems`, the numeric bounds — are still
   enforced before a record is written anywhere. On top of the schema come the
   two checks it cannot state: `check_provenance`, that a quote is present
   exactly when a value is, and `check_quotes_verbatim`, that the quote is
   actually in the paper text the caller supplied.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from extract.errors import ExtractError, RecordValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "experiment.schema.json"

# Root-level annotation carrying the schema's semver. Not a JSON Schema
# keyword, so validators ignore it; `scripts/validate_gold.py` reads the same
# key, and reading it beats hardcoding a literal that would silently go stale.
SCHEMA_VERSION_KEY = "x-schema-version"

# Keywords the structured-output subset does not accept, plus the file-level
# annotations that have no meaning in a request. Dropping them is safe in one
# direction only: the derived schema is *less* strict than the real one, and
# `validate_record` closes the gap afterwards.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$id",
        "$schema",
        "default",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "title",
        "unevaluatedProperties",
        "uniqueItems",
        SCHEMA_VERSION_KEY,
    }
)

# Filled in by the caller, not the model. See the module docstring.
CODE_ASSIGNED_EXPERIMENT_KEYS = frozenset({"experiment_id", "notes"})
CODE_ASSIGNED_CLAIM_KEYS = frozenset({"extracted_from"})

# The keys that make a dict a claim wrapper. Used to recognise claims in an
# assembled record, where there is no schema to consult.
CLAIM_KEYS = frozenset({"value", "source_quote", "confidence"})

_DEFS_PREFIX = "#/$defs/"

_validator: Draft202012Validator | None = None


def load_schema(path: Path | None = None) -> dict[str, Any]:
    """Return the parsed schema document, raising if it cannot be read.

    Resolved at call time rather than bound as a default so a test can point
    the loader at another file.
    """
    path = path or SCHEMA_PATH
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ExtractError(
            f"schema not found: {path}\n"
            "  Extraction is defined by schema/experiment.schema.json; there is "
            "no built-in fallback copy."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ExtractError(
            f"{path} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise ExtractError(f"{path} does not contain a JSON object at the top level")
    return document


def schema_version(document: dict[str, Any]) -> str:
    """Return the schema's declared semver, raising if it is absent.

    Every written record is stamped with this and extraction is idempotent per
    (paper, schema_version), so an unversioned schema is not usable.
    """
    version = document.get(SCHEMA_VERSION_KEY)
    if not isinstance(version, str) or not version.strip():
        raise ExtractError(
            f"{SCHEMA_PATH.name} has no usable {SCHEMA_VERSION_KEY!r} key; "
            "it is the single source of truth for the schema version."
        )
    return version


def build_extraction_schema(document: dict[str, Any]) -> dict[str, Any]:
    """Return the API-subset schema for one paper's `experiments` array.

    Guarantees the result contains no keyword from `UNSUPPORTED_KEYWORDS`, that
    every object closes with `additionalProperties: false` and lists all of its
    properties as required, and that the code-assigned keys are absent — so a
    model response can never claim provenance the caller did not supply.
    """
    defs = document.get("$defs")
    experiments = document.get("properties", {}).get("experiments")
    if not isinstance(defs, dict) or not isinstance(experiments, dict):
        raise ExtractError(
            f"{SCHEMA_PATH.name} is missing $defs or properties.experiments; the "
            "extraction schema is derived from both and cannot be built without them."
        )
    if not isinstance(experiments.get("items"), dict):
        raise ExtractError(
            f"{SCHEMA_PATH.name}: properties.experiments has no items schema, so "
            "there is nothing to ask the model to produce."
        )

    items = _flatten(experiments["items"], defs, ())
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["experiments"],
        "properties": {
            "experiments": {
                "type": "array",
                "description": experiments.get("description", ""),
                "items": items,
            }
        },
    }


def validate_record(record: Any, document: dict[str, Any]) -> None:
    """Raise unless `record` validates against the full schema.

    Reports every error, not just the first: a record with three malformed
    fields should say so once rather than over three runs.
    """
    global _validator
    if _validator is None or _validator.schema is not document:
        Draft202012Validator.check_schema(document)
        _validator = Draft202012Validator(document)

    errors = sorted(_validator.iter_errors(record), key=lambda e: list(e.absolute_path))
    if errors:
        lines = "\n".join(f"    {_error_path(e)}: {e.message}" for e in errors)
        raise RecordValidationError(
            f"extracted record does not validate against "
            f"{SCHEMA_PATH.name} ({len(errors)} error(s)):\n{lines}"
        )


def check_provenance(record: Any) -> None:
    """Raise unless every claim's quote agrees with its value.

    The schema states the rule in prose — `source_quote` is null "only when
    there is nothing to quote, i.e. the value is null or not_reported" — but
    cannot express it. It is the honesty invariant the whole project rests on,
    in both directions: an absent value must not carry a quote (that quote
    would be supporting nothing), and a present value must carry one (a value
    with no sentence behind it is a guess).
    """
    for path, claim in _iter_claims(record):
        value = claim.get("value")
        quote = claim.get("source_quote")
        absent = value is None or value == "not_reported"
        if absent and quote is not None:
            raise RecordValidationError(
                f"{path}: value is {value!r} but source_quote is {quote!r}; "
                "an absent value has nothing to quote."
            )
        if not absent and not quote:
            raise RecordValidationError(
                f"{path}: value is {value!r} with no source_quote; every reported "
                "value must carry the sentence it was read from."
            )


def check_quotes_verbatim(record: Any, text: str) -> None:
    """Raise unless every `source_quote` in `record` occurs in `text`.

    The other half of the honesty invariant, and the half the model can break
    on its own: `check_provenance` only asks whether a quote is *there*, and a
    fabricated sentence satisfies it perfectly. PLAN.md's first key invariant
    says the quote is a verbatim sentence *from the paper*, so it is compared
    against the text the extractor was handed.

    Whitespace is collapsed on both sides before the second comparison, matching
    `scripts/check_gold.py`, which applies this same check to the hand-labelled
    records: a structured abstract wraps lines where the paper does not, and a
    newline is not a difference in the quote. Nothing else is normalised —
    case, punctuation and unicode differences are real differences, and
    noticing them is the entire value of the check.
    """
    haystack = collapse_whitespace(text)
    for path, claim in _iter_claims(record):
        quote = claim.get("source_quote")
        if not isinstance(quote, str) or not quote:
            continue
        if quote in text or collapse_whitespace(quote) in haystack:
            continue
        raise RecordValidationError(
            f"{path}: source_quote is not verbatim in the text supplied; a quote "
            "that is not in the paper is a fabricated citation, and no part of "
            "this record can be trusted.\n"
            f"    quote:   {_ellipsis(quote)}\n"
            f"    closest: {_ellipsis(_closest_window(quote, text))}"
        )


def collapse_whitespace(text: str) -> str:
    """Return `text` with every run of whitespace reduced to one space.

    Deliberately identical to `scripts/check_gold.py`'s function of the same
    name, and deliberately not imported from it: that module is a human-run
    script that pulls in the PubMed and bioRxiv lookups on import, and the
    extractor must not depend on them. The two are pinned to each other by a
    test rather than by an import.
    """
    return re.sub(r"\s+", " ", text).strip()


def _closest_window(quote: str, text: str) -> str:
    """Return the slice of `text` that best lines up with `quote`.

    Anchored on the longest block the two share, so a quote that differs by one
    character still reports the region it was nearly right about — which is what
    tells a fabrication apart from a transcription slip at a glance.
    """
    if not text or not quote:
        return ""
    block = SequenceMatcher(None, text, quote, autojunk=False).find_longest_match(
        0, len(text), 0, len(quote)
    )
    start = max(0, min(block.a - block.b, len(text) - len(quote)))
    return text[start : start + len(quote)]


def _ellipsis(text: str, limit: int = 160) -> str:
    """One line, and short enough to read in a terminal."""
    collapsed = collapse_whitespace(text)
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


def _flatten(node: Any, defs: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Return `node` with $refs inlined, allOf merged and unsupported keys dropped."""
    if isinstance(node, list):
        return [_flatten(item, defs, path) for item in node]
    if not isinstance(node, dict):
        return node

    merged: dict[str, Any] = {}
    composed = _resolve_composition(node, defs, path)
    for source in (*composed, node):
        for key, value in source.items():
            if key in {"$ref", "allOf"} or key in UNSUPPORTED_KEYWORDS:
                continue
            # A composed schema's description describes that schema, not this
            # one: without this, every claim wrapper would be labelled
            # "Provenance keys shared by every claim wrapper" in the prompt.
            # `_inherited_description` puts it back where it does belong.
            if key == "description" and source is not node:
                continue
            if key == "properties":
                merged.setdefault("properties", {}).update(value)
            elif key == "required":
                merged["required"] = [*merged.get("required", []), *value]
            else:
                merged[key] = value

    if "properties" in merged:
        merged["properties"] = {
            name: _flatten(child, defs, (*path, name))
            for name, child in merged["properties"].items()
            if not _is_code_assigned(name, path)
        }
        # Every remaining property is required. The schema's optional fields are
        # all nullable or carry a `not_reported` member, so "absent" is
        # expressible as a value — which is what the project wants anyway: an
        # omitted key and a reported null are indistinguishable downstream.
        merged["required"] = list(merged["properties"])
        merged["additionalProperties"] = False
        merged.setdefault("type", "object")
    elif "required" in merged:
        merged.pop("required")

    if "items" in merged:
        merged["items"] = _flatten(merged["items"], defs, path)

    inherited = _inherited_description(node, composed)
    if inherited and "description" not in merged:
        merged["description"] = inherited

    return _split_nullable_type(merged)


def _resolve_composition(
    node: dict[str, Any], defs: dict[str, Any], path: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Return the already-flattened schemas `node` composes, in application order."""
    sources: list[dict[str, Any]] = []
    for reference in [*node.get("allOf", []), *([{"$ref": node["$ref"]}] if "$ref" in node else [])]:
        target = reference.get("$ref") if isinstance(reference, dict) else None
        if target is None:
            sources.append(_flatten(reference, defs, path))
            continue
        if not target.startswith(_DEFS_PREFIX):
            raise ExtractError(
                f"unsupported $ref {target!r} at {'.'.join(path) or '<root>'}: the "
                "extraction schema can only inline local #/$defs/ references."
            )
        name = target[len(_DEFS_PREFIX) :]
        if name not in defs:
            raise ExtractError(
                f"$ref {target!r} at {'.'.join(path) or '<root>'} does not exist in $defs"
            )
        sources.append(_flatten(defs[name], defs, path))
    return sources


def _inherited_description(node: dict[str, Any], composed: list[dict[str, Any]]) -> str | None:
    """Return the description a bare `$ref` node borrows from its definition.

    `{"$ref": "#/$defs/confidence"}` means whatever that definition means, so it
    keeps that description. A wrapper that composes a definition and adds
    properties of its own does not: it is a different thing from what it
    composes, and inheriting there is how every claim ends up mislabelled.
    """
    if "properties" in node or "description" in node or len(composed) != 1:
        return None
    return composed[0].get("description")


def _is_code_assigned(name: str, path: tuple[str, ...]) -> bool:
    """True for the keys the caller fills in rather than the model."""
    if name in CODE_ASSIGNED_CLAIM_KEYS:
        return True
    # `experiment_id` and `notes` live directly on an experiment, which is the
    # `items` schema — reached with an empty path from build_extraction_schema.
    return name in CODE_ASSIGNED_EXPERIMENT_KEYS and not path


def _split_nullable_type(node: dict[str, Any]) -> dict[str, Any]:
    """Rewrite `type: [x, "null"]` as an anyOf of single-type schemas.

    A type array is ordinary JSON Schema, but `anyOf` is named explicitly in
    the structured-output subset and a type union is not. Rewriting costs
    nothing and keeps the request inside the documented surface.
    """
    declared = node.get("type")
    if not isinstance(declared, list):
        return node
    rewritten = {key: value for key, value in node.items() if key != "type"}
    rewritten["anyOf"] = [{"type": member} for member in declared]
    return rewritten


def _iter_claims(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Return every (path, claim wrapper) pair inside `node`."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if CLAIM_KEYS <= set(node):
            found.append((path or "<root>", node))
            return found
        for key, child in node.items():
            found += _iter_claims(child, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found += _iter_claims(child, f"{path}[{index}]")
    return found


def _error_path(error: Any) -> str:
    """Render a validation error's location as experiments[0].organism.value."""
    out = ""
    for part in error.absolute_path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<root>"
