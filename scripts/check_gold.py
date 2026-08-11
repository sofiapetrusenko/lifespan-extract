#!/usr/bin/env python3
"""Deterministic checks on gold-set records. No model calls, no judgement.

Usage:
    python scripts/check_gold.py --all
    python scripts/check_gold.py data/drafts/harrison2009.json
    python scripts/check_gold.py --all --offline

Four checks, in order of how much they cost to run:

a. **Schema.** Delegated to `scripts/validate_gold.py` so there is one
   validator, not two. Top-level `_`-prefixed keys are stripped first *for a
   draft*: that is what lets a scaffolded draft carry `_abstract` for the
   labeller to read and still be checked against a schema whose root is
   `additionalProperties: false`.

   **For a file already in `data/gold/` the same keys are a failure, and the
   document is validated unstripped.** Stripping there would be a lie about
   what the pre-commit hook sees: `scripts/validate_gold.py` does not strip, so
   a scaffolded file that passed here would then fail the hook on
   `additionalProperties` — a check that says "ok" and a commit that is refused
   thirty seconds later. The keys are draft-only scaffolding and must be gone
   before promotion; `--promote` is what removes them.
b. **Verbatim quotes.** Every `source_quote` marked `extracted_from: abstract`
   must appear character-for-character in the paper's PubMed abstract. This is
   the check that makes the gold set trustworthy — a quote that is not in the
   paper is a fabricated citation, whoever wrote it.

   Quotes marked `full_text` are checked against the paper's PMC open-access
   full text, resolved PMID -> PMCID via elink. That comparison collapses runs
   of whitespace on both sides first, and does nothing else: PMC's JATS wraps
   lines where the published PDF does not, so a newline is not a difference in
   the quote. Case and punctuation are compared as they are — those *are*
   differences, and the whole value of this check is that it notices them.

   A paper that is not in the PMC open-access subset makes its full-text quotes
   **unverifiable**, reported as a warning. Not a failure: a quote we cannot
   check is not a quote we know to be wrong. It does block `--promote`, because
   promotion is a claim that the record *was* checked.
c. **Cross-file consistency.** The same agent must be written the same way in
   every file, because `GET /interventions/{agent}` aggregates on that string
   and "Rapamycin" and "rapamycin" would aggregate as two drugs.
d. **Pair sanity.** The gold set contains deliberate consistency pairs
   (Harrison 2009 / Miller 2011). If the same agent in the same organism is
   labelled with a different shape of record in each file, one of them is
   probably an oversight rather than a real difference in what the papers
   report. Reported as a warning, never a failure: the papers really may differ.

Failures exit non-zero. Warnings do not — they need a human to look, not a
build to stop.

`--promote` moves a draft that passes every check into `data/gold/`, stripping
the scaffolding keys on the way. It refuses on any failure, and on anything
that leaves a quote *unverified* rather than verified — see `promotion_blockers`.

`--refresh-quotes` repairs the narrow case of a quote that is a *mistranscription*
of the paper rather than a wrong claim about it — a labeller typing `17-a-estradiol`
where the publisher set `17‐α‐estradiol`, or `+/-` for `±`. It replaces the
`source_quote` string with the exact slice of PMC text it should have been, and
touches nothing else: never a `value`, a `confidence` or a `notes`.

It is a dry run unless `--write` is given, and it refuses anything that is not
obvious — see `plan_rewrite` for the three conditions. Like `--promote`, it
writes into `data/gold/` and so is the human's to run; the PreToolUse hook
blocks an agent from invoking it.

Note what this is *not*: unicode folding. Nothing in the checker treats `‐` and
`-` as equal, before or after a refresh. The quote is made verbatim by changing
the quote, which leaves a diff a human can read, rather than by making the
comparison blind to the difference, which would leave nothing at all.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO_ROOT / "data" / "gold"
DRAFTS_DIR = REPO_ROOT / "data" / "drafts"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scaffold_gold import DRAFT_ID_SUFFIX, DRAFT_NOTE_PREFIX
from validate_gold import format_path, load_schema, sort_key

# Papers the gold set deliberately pairs, per PLAN.md's gold-set design. Keys
# are filename stems. A pair with only one member present is reported as
# incomplete, not as a failure — the set is still being built.
CONSISTENCY_PAIRS: tuple[tuple[str, str], ...] = (("harrison2009", "miller2011"),)

# The provenance keys that identify a claim wrapper anywhere in the tree.
CLAIM_KEYS = frozenset({"value", "source_quote", "confidence", "extracted_from"})

# Values that mean "the paper does not say", across both conventions the schema
# uses: null for open-vocabulary fields, the literal string for strain-like ones.
ABSENT = (None, "not_reported")

# Spellings of absence that are *not* the convention. Caught here because the
# schema cannot: `strain` is an open string, so "N/A" validates fine and then
# silently becomes a distinct strain in every aggregate.
ABSENT_LOOKALIKES = frozenset(
    {"n/a", "na", "none", "unknown", "not reported", "notreported", "-", "?", ""}
)

# Deliberately a closed vocabulary rather than "any trailing letters": doses are
# free text, and a general token match flags "in diet" vs "in food" as a unit
# difference, which is noise.
UNIT_RE = re.compile(
    r"(?<![A-Za-z])(%|ppm|w/w|w/v|v/v|mg/kg|mg/ml|mg/l|g/kg|µg/ml|ug/ml|µM|uM|mM|nM|IU)(?![A-Za-z])"
)

FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

# The two texts a quote can be checked against, matching `extracted_from`.
ABSTRACT = "abstract"
FULL_TEXT = "full_text"

# Passed where a full text would go, to say "PMC has no open-access body for
# this paper". A distinct sentinel rather than None because None already means
# "the lookup did not happen", and conflating the two would turn an unreachable
# network into a confident statement about the paper.
NOT_IN_PMC = object()


@dataclass
class Issue:
    level: str
    message: str


@dataclass
class QuoteResult:
    location: str
    status: str
    source: str = ABSTRACT
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass
class QuoteRewrite:
    """One quote `--refresh-quotes` could or could not repair.

    `candidate is None` is a refusal, and `reason` says which rule refused it.
    A refusal is the expected outcome for a quote that was never a slice of the
    paper — those need a labeller, not a better string search.
    """

    location: str
    current: str
    candidate: str | None
    ratio: float
    reason: str = ""

    @property
    def unambiguous(self) -> bool:
        return self.candidate is not None


@dataclass
class FileReport:
    path: Path
    document: dict[str, Any] | None = None
    schema_errors: list[str] = field(default_factory=list)
    quote_results: list[QuoteResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    # The PMC text this file's quotes were checked against, kept so
    # `--refresh-quotes` can slice from the same string the check read.
    full_text: str | object | None = None

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def failed(self) -> bool:
        return bool(
            self.schema_errors
            or any(q.failed for q in self.quote_results)
            or any(i.level == FAIL for i in self.issues)
        )

    def counts(self, source: str | None = None) -> dict[str, int]:
        """Tally quote statuses, optionally for one `extracted_from` source."""
        tally: dict[str, int] = defaultdict(int)
        for result in self.quote_results:
            if source is None or result.source == source:
                tally[result.status] += 1
        return tally


# --------------------------------------------------------------------------
# document traversal
# --------------------------------------------------------------------------


def in_directory(path: Path, directory: Path) -> bool:
    """True when `path` is inside `directory`.

    Resolved on both sides so a relative argument, a symlink or a `..` segment
    cannot smuggle a gold file past the check by spelling its path differently.
    A path that cannot be resolved is not in the directory — a missing file is
    already reported as its own failure.
    """
    try:
        path.resolve().relative_to(directory.resolve())
    except (ValueError, OSError):
        return False
    return True


def private_keys(document: dict[str, Any]) -> list[str]:
    """Top-level `_`-prefixed keys, sorted. The scaffold's helper keys."""
    return sorted(key for key in document if key.startswith("_"))


def strip_private(document: dict[str, Any]) -> dict[str, Any]:
    """Drop top-level `_`-prefixed keys.

    `scaffold_gold.py` puts the abstract and journal there so a labeller can
    read them beside the fields they are filling in. They have no home in the
    schema, whose root is `additionalProperties: false`, so they come off before
    validation. Only the top level: a `_`-prefixed key nested inside an
    experiment is not a convention, it is a typo, and should fail.
    """
    return {key: value for key, value in document.items() if not key.startswith("_")}


def iter_claims(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every `(dotted_path, claim)` in the document.

    Structural rather than a hardcoded list of field paths: the schema grows by
    adding claim wrappers, and a checker that enumerated them by name would
    quietly stop covering the new ones.
    """
    if isinstance(node, dict):
        if CLAIM_KEYS <= node.keys():
            yield path, node
            return
        for key, value in node.items():
            yield from iter_claims(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_claims(value, f"{path}[{index}]")


def iter_experiments(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    experiments = document.get("experiments")
    if isinstance(experiments, list):
        for experiment in experiments:
            if isinstance(experiment, dict):
                yield experiment


def claim_value(container: Any, *keys: str) -> Any:
    """Return the `value` of a nested claim, or None if the path is absent."""
    node = container
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node.get("value") if isinstance(node, dict) else None


def normalise_agent(name: str) -> str:
    """The key two spellings of one agent must share.

    Casefold plus whitespace collapse only. Nothing smarter: stripping hyphens
    or plurals here would silently merge `17-alpha-estradiol` with a genuinely
    different compound, and this check exists to surface differences, not to
    paper over them.
    """
    return re.sub(r"\s+", " ", name).strip().casefold()


# --------------------------------------------------------------------------
# (a) schema
# --------------------------------------------------------------------------


def check_schema(document: dict[str, Any], validator, *, strip: bool = True) -> list[str]:
    """Validate `document`, optionally dropping the scaffold's helper keys first.

    `strip=False` is what a gold file gets, so the errors reported here are
    character-for-character the errors `scripts/validate_gold.py` would report
    in the pre-commit hook. The two tools must not disagree about the same file.
    """
    subject = strip_private(document) if strip else document
    errors = sorted(validator.iter_errors(subject), key=sort_key)
    return [f"{format_path(error)}: {error.message}" for error in errors]


def check_private_keys(document: dict[str, Any], *, is_gold: bool) -> list[Issue]:
    """Scaffolding keys are a draft convenience and a gold-file failure.

    `scaffold_gold.py` parks `_abstract` and `_journal` at the top level so a
    labeller can read the paper beside the fields they are filling in. They have
    no home in the schema. In `data/gold/` they are simply left-over scaffolding:
    the pre-commit hook validates unstripped and rejects them, so accepting them
    here would hand back a clean bill of health for a file that cannot be
    committed.
    """
    found = private_keys(document)
    if not found:
        return []
    rendered = ", ".join(repr(key) for key in found)
    if not is_gold:
        return [Issue(INFO, f"draft scaffolding {rendered} stripped before validation")]
    return [
        Issue(
            FAIL,
            f"scaffolding key(s) {rendered} in a gold file. These are draft-only: "
            "scripts/validate_gold.py does not strip them and the pre-commit hook "
            "will reject the file on additionalProperties. Remove them, or promote "
            "the draft with --promote, which strips them on the way in.",
        )
    ]


def check_draft_markers(document: dict[str, Any]) -> list[Issue]:
    """Fail on the markers `scaffold_gold.py` leaves in an unlabelled draft.

    The schema cannot catch these: `<slug>-todo` matches the `experiment_id`
    pattern and the DRAFT note is a legal free-text string. Without this check a
    scaffolded skeleton would validate cleanly and could be moved into the gold
    set with its placeholder enums intact, where it would silently become a
    wrong answer key.
    """
    issues: list[Issue] = []
    for index, experiment in enumerate(iter_experiments(document)):
        experiment_id = experiment.get("experiment_id")
        if isinstance(experiment_id, str) and experiment_id.endswith(DRAFT_ID_SUFFIX):
            issues.append(
                Issue(
                    FAIL,
                    f"experiments[{index}].experiment_id is still the scaffold placeholder "
                    f"{experiment_id!r}",
                )
            )
        notes = experiment.get("notes")
        if isinstance(notes, str) and notes.startswith(DRAFT_NOTE_PREFIX):
            issues.append(
                Issue(FAIL, f"experiments[{index}].notes still carries the scaffold DRAFT note")
            )
    return issues


# --------------------------------------------------------------------------
# (b) verbatim quotes
# --------------------------------------------------------------------------


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def iter_quotes(document: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield `(dotted_path, quote, source)` for every non-empty `source_quote`.

    `source` is normalised to `abstract` or `full_text` here so that the schema's
    enum is read in exactly one place.
    """
    for path, wrapper in iter_claims(document):
        quote = wrapper.get("source_quote")
        if not isinstance(quote, str) or not quote:
            continue
        source = ABSTRACT if wrapper.get("extracted_from") == ABSTRACT else FULL_TEXT
        yield path, quote, source


def check_quotes(
    document: dict[str, Any],
    abstract: str | None,
    full_text: str | object | None = None,
) -> list[QuoteResult]:
    """Check every quote against the text it says it came from.

    `abstract=None` means the abstract could not be obtained; every quote that
    depends on it is marked skipped rather than passed, so an unreachable PubMed
    never reads as a clean run.

    `full_text` is the same idea with one more state. A string is the PMC
    open-access text. `NOT_IN_PMC` says PMC has no open-access body for this
    paper, which makes its full-text quotes *unverifiable* — a warning, not a
    failure. None says the lookup did not happen at all, which is a skip, the
    same as an unreachable abstract.
    """
    results: list[QuoteResult] = []
    for path, quote, source in iter_quotes(document):
        if source == ABSTRACT:
            if abstract is None:
                results.append(QuoteResult(path, "skipped", ABSTRACT))
            else:
                results.append(_match_quote(path, quote, abstract))
            continue

        if full_text is NOT_IN_PMC:
            results.append(
                QuoteResult(path, "unverifiable", FULL_TEXT, "not in PMC open access")
            )
        elif isinstance(full_text, str):
            results.append(_match_full_text(path, quote, full_text))
        else:
            results.append(QuoteResult(path, "skipped", FULL_TEXT))
    return results


def _match_quote(path: str, quote: str, abstract: str) -> QuoteResult:
    if quote in abstract:
        return QuoteResult(path, "ok", ABSTRACT)

    # A structured PubMed abstract is reassembled from labelled sections, so it
    # carries newlines an author transcribing a sentence would not. Treating a
    # whitespace-only difference as a pass is not a loosening of "verbatim":
    # no word, character or punctuation mark differs. It is still reported, so
    # the labeller can tidy it if they want to.
    if collapse_whitespace(quote) in collapse_whitespace(abstract):
        return QuoteResult(path, "whitespace", ABSTRACT)

    ratio, window, index = closest_match(quote, abstract)
    return QuoteResult(path, "fail", ABSTRACT, _describe_mismatch(quote, window, ratio, index))


def _match_full_text(path: str, quote: str, full_text: str) -> QuoteResult:
    """Compare a full-text quote against PMC, whitespace-collapsed on both sides.

    Collapsing is not reported separately the way it is for an abstract. There
    it is worth a note because a newline inside a quote is a transcription slip
    the labeller may want to tidy; here it is the normal case, since PMC's XML
    line-wraps wherever the deposit happened to and the published PDF wraps
    somewhere else entirely. Nothing beyond whitespace is normalised: case and
    punctuation differences are real differences.
    """
    needle = collapse_whitespace(quote)
    haystack = collapse_whitespace(full_text)
    if needle in haystack:
        return QuoteResult(path, "ok", FULL_TEXT)

    ratio, window, index = closest_match(needle, haystack)
    return QuoteResult(
        path,
        "fail",
        FULL_TEXT,
        _describe_mismatch(needle, window, ratio, index, source="full text"),
    )


def closest_match(quote: str, abstract: str) -> tuple[float, str, int | None]:
    """Return `(similarity, closest_window, first_difference_index)`.

    Anchors on the longest block the two strings share and reads a
    quote-length window of the abstract around it, rather than scoring
    sentences: a quote that spans a sentence boundary, or that differs only in
    one character, still lands on the right region this way.

    `autojunk=False` matters — the default heuristic treats characters
    appearing in more than 1% of a >200-character string as junk, which for
    English prose means spaces and vowels, and it wrecks the match.
    """
    if not abstract:
        return 0.0, "", None
    matcher = SequenceMatcher(None, abstract, quote, autojunk=False)
    block = matcher.find_longest_match(0, len(abstract), 0, len(quote))
    start = max(0, min(block.a - block.b, len(abstract) - len(quote)))
    window = abstract[start : start + len(quote)]
    ratio = SequenceMatcher(None, window, quote, autojunk=False).ratio()

    index = next((i for i, (a, b) in enumerate(zip(window, quote)) if a != b), None)
    if index is None and len(window) != len(quote):
        index = min(len(window), len(quote))
    return ratio, window, index


def _describe_mismatch(
    quote: str, window: str, ratio: float, index: int | None, source: str = "abstract"
) -> str:
    # Both rows and the caret row share one gutter, so the caret lands under
    # the same column in all three however the source is named.
    width = max(len("gold"), len(source)) + 2
    gutter = " " * (4 + width)
    gold_label = "gold:".ljust(width)
    source_label = f"{source}:".ljust(width)

    lines = [f"not verbatim in the {source} (closest match {ratio:.0%})"]
    if index is None:
        lines.append(f"    {gold_label}{_ellipsis(quote)}")
        lines.append(f"    {source_label}{_ellipsis(window)}")
        return "\n".join(lines)

    radius = 45
    start = max(0, index - radius)
    gold_slice = quote[start : index + radius]
    source_slice = window[start : index + radius]
    caret = " " * (index - start) + "^"
    lines.append(f"    {gold_label}{_prefix(start)}{gold_slice}")
    lines.append(f"    {source_label}{_prefix(start)}{source_slice}")
    lines.append(f"{gutter}{' ' * len(_prefix(start))}{caret} first difference at char {index}")
    lines.append(f"{gutter}{_char_note(quote, window, index, source)}")
    return "\n".join(lines)


def _char_note(quote: str, window: str, index: int, source: str = "abstract") -> str:
    got = quote[index] if index < len(quote) else "<end of quote>"
    expected = window[index] if index < len(window) else f"<end of {source} window>"
    return f"gold has {got!r}, {source} has {expected!r}"


def _prefix(start: int) -> str:
    return "..." if start > 0 else ""


def _ellipsis(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------
# (c) cross-file consistency
# --------------------------------------------------------------------------


def check_cross_file(reports: list[FileReport]) -> list[Issue]:
    """Consistency checks that only make sense across more than one file."""
    documents = [(r.stem, r.document) for r in reports if r.document is not None]

    # `_check_absent_lookalikes` is per-file and stays useful on its own; the
    # rest need something to compare against.
    if len(documents) < 2:
        return _check_absent_lookalikes(documents) + [
            Issue(INFO, "only one readable file — cross-file comparisons skipped")
        ]

    issues: list[Issue] = []
    issues += _check_agent_spellings(documents)
    issues += _check_dose_units(documents)
    issues += _check_absent_lookalikes(documents)
    issues += _check_schema_versions(documents)
    return issues


def _check_agent_spellings(documents: list[tuple[str, dict[str, Any]]]) -> list[Issue]:
    """One agent, one spelling. A FAIL, because aggregation depends on it."""
    spellings: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for stem, document in documents:
        for experiment in iter_experiments(document):
            agent = claim_value(experiment, "intervention", "agent")
            if isinstance(agent, str) and agent:
                spellings[normalise_agent(agent)][agent].add(stem)

    issues: list[Issue] = []
    for _, variants in sorted(spellings.items()):
        if len(variants) > 1:
            rendered = "; ".join(
                f"{name!r} in {', '.join(sorted(files))}" for name, files in sorted(variants.items())
            )
            issues.append(
                Issue(FAIL, f"one agent is spelled {len(variants)} ways across files: {rendered}")
            )
    return issues


def _check_dose_units(documents: list[tuple[str, dict[str, Any]]]) -> list[Issue]:
    """Warn when one agent's doses are quoted in unrelated units.

    A warning, not a failure. Two papers really can dose the same drug in ppm
    and in mg/kg, and normalising that is Phase 2's job, not the labeller's.
    Only *disjoint* non-empty unit sets are reported, so `0.1% (w/w)` beside
    `1% (w/w)` stays quiet.
    """
    units: dict[str, dict[str, frozenset[str]]] = defaultdict(dict)
    for stem, document in documents:
        for experiment in iter_experiments(document):
            agent = claim_value(experiment, "intervention", "agent")
            dose = claim_value(experiment, "intervention", "dose")
            if not isinstance(agent, str) or not isinstance(dose, str):
                continue
            found = frozenset(m.group(1).casefold() for m in UNIT_RE.finditer(dose))
            if found:
                units[normalise_agent(agent)][f"{stem}: {dose!r}"] = found

    issues: list[Issue] = []
    for agent, per_file in sorted(units.items()):
        entries = sorted(per_file.items())
        disjoint = [
            (a, b)
            for i, (a, units_a) in enumerate(entries)
            for (b, units_b) in entries[i + 1 :]
            if not (units_a & units_b)
        ]
        if disjoint:
            first, second = disjoint[0]
            issues.append(
                Issue(WARN, f"{agent!r} doses use unrelated units — {first} vs {second}")
            )
    return issues


def _check_absent_lookalikes(documents: list[tuple[str, dict[str, Any]]]) -> list[Issue]:
    """Absence must be spelled `not_reported` or null, never `N/A`.

    Not reachable by schema validation: the fields this catches are open
    strings, so `"N/A"` validates and then becomes a distinct strain, agent or
    mechanism in every aggregate built on top of the gold set.
    """
    issues: list[Issue] = []
    for stem, document in documents:
        for path, wrapper in iter_claims(document):
            value = wrapper.get("value")
            if isinstance(value, str) and value.strip().casefold() in ABSENT_LOOKALIKES:
                issues.append(
                    Issue(
                        FAIL,
                        f"{stem}: {path}.value is {value!r}; absence is written as "
                        '"not_reported" or null',
                    )
                )
    return issues


def _check_schema_versions(documents: list[tuple[str, dict[str, Any]]]) -> list[Issue]:
    versions: dict[str, list[str]] = defaultdict(list)
    for stem, document in documents:
        version = document.get("schema_version")
        if isinstance(version, str):
            versions[version].append(stem)
    if len(versions) > 1:
        rendered = "; ".join(f"{v}: {', '.join(sorted(f))}" for v, f in sorted(versions.items()))
        return [Issue(WARN, f"gold set spans {len(versions)} schema versions — {rendered}")]
    return []


# --------------------------------------------------------------------------
# (d) pair sanity
# --------------------------------------------------------------------------


def populated_fields(experiment: dict[str, Any]) -> set[str]:
    """Field paths this experiment actually reports a value for."""
    return {
        path
        for path, wrapper in iter_claims(experiment)
        if wrapper.get("value") not in ABSENT
    }


def check_pairs(
    reports: list[FileReport], pairs: tuple[tuple[str, str], ...] = CONSISTENCY_PAIRS
) -> list[Issue]:
    """Compare the record *shape* of each consistency pair.

    Not the values — two papers on the same drug are expected to disagree on
    effect sizes, and the Colman/Mattison pair disagrees on the conclusion by
    design. What is worth flagging is one file reporting a field the other
    leaves empty for the same agent in the same organism: usually that is an
    oversight in labelling rather than a difference between the papers.
    """
    by_stem = {r.stem: r.document for r in reports if r.document is not None}
    issues: list[Issue] = []

    for left, right in pairs:
        if left not in by_stem or right not in by_stem:
            present = [s for s in (left, right) if s in by_stem]
            if present:
                missing = right if left in by_stem else left
                issues.append(
                    Issue(INFO, f"consistency pair {left}/{right} is incomplete: {missing} not checked")
                )
            continue

        left_groups = _group_by_agent_and_organism(by_stem[left])
        right_groups = _group_by_agent_and_organism(by_stem[right])
        shared = sorted(set(left_groups) & set(right_groups))
        if not shared:
            issues.append(
                Issue(WARN, f"pair {left}/{right} shares no (agent, organism) group to compare")
            )
            continue

        for key in shared:
            only_left = left_groups[key] - right_groups[key]
            only_right = right_groups[key] - left_groups[key]
            if only_left or only_right:
                agent, organism = key
                detail = []
                if only_left:
                    detail.append(f"only {left}: {', '.join(sorted(only_left))}")
                if only_right:
                    detail.append(f"only {right}: {', '.join(sorted(only_right))}")
                issues.append(
                    Issue(
                        WARN,
                        f"pair {left}/{right}, {agent!r} in {organism}: "
                        f"different fields populated — {'; '.join(detail)}",
                    )
                )
    return issues


def _group_by_agent_and_organism(document: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    """Union the populated field paths of every experiment per (agent, organism).

    Unioned rather than compared per experiment because a paper may split one
    arm by sex (Harrison 2009 does), and those rows are the same shape by
    construction.
    """
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for experiment in iter_experiments(document):
        agent = claim_value(experiment, "intervention", "agent")
        organism = claim_value(experiment, "organism")
        if not isinstance(agent, str) or not isinstance(organism, str):
            continue
        key = (normalise_agent(agent), organism)
        # Paths are relative to the experiment, and the leading `experiments[n]`
        # index is dropped by construction, so two files line up.
        groups[key] |= populated_fields(experiment)
    return groups


# --------------------------------------------------------------------------
# --refresh-quotes
# --------------------------------------------------------------------------

# How similar a slice of the paper must be to a quote before this tool will
# offer to substitute it. Chosen from the observed distribution on the current
# gold set, which is bimodal and not close: quotes that differ only in how the
# publisher's typography was transliterated score 91-99%, and quotes that were
# never a slice of the paper at all — table rows rewritten as prose, editorial
# parentheticals — score 42-82%. 90% sits in the empty middle.
#
# Raising it is safe; lowering it is not. Below ~85% this stops repairing
# transcriptions and starts rewriting one claim into a different one.
REFRESH_SIMILARITY = 0.90


# A "word character" for the purpose of deciding whether a slice boundary cuts
# through a word. Regex `\w` rather than `str.isalnum` so it is the same notion
# the guard in `splits_a_word` uses, and so `_` counts.
_WORD_RE = re.compile(r"\w")


def _expand_start(source: str, index: int) -> int:
    while index > 0 and not source[index - 1].isspace():
        index -= 1
    return index


def _contract_start(source: str, index: int) -> int:
    while 0 < index < len(source) and not source[index - 1].isspace():
        index += 1
    return index


def _expand_end(source: str, index: int) -> int:
    while index < len(source) and not source[index].isspace():
        index += 1
    return index


def _contract_end(source: str, index: int) -> int:
    while 0 < index < len(source) and not source[index].isspace():
        index -= 1
    return index


def splits_a_word(source: str, start: int, end: int) -> bool:
    """True when `source[start:end]` begins or ends inside a word.

    The condition is adjacency, not whitespace: a boundary is bad only when a
    word character on the inside of the slice touches a word character on the
    outside. A slice ending in `.` followed by a letter is fine — that is a
    sentence boundary, not a severed word.
    """
    candidate = source[start:end]
    if not candidate:
        return False
    if start > 0 and _WORD_RE.match(source[start - 1]) and _WORD_RE.match(candidate[0]):
        return True
    return bool(
        end < len(source) and _WORD_RE.match(source[end]) and _WORD_RE.match(candidate[-1])
    )


def best_slice(quote: str, source: str) -> tuple[str, float, int]:
    """Return `(closest contiguous slice of source, similarity, start index)`.

    Unlike `closest_match`, the slice is not fixed at the quote's length: it is
    trimmed to whatever aligns with the quote's first and last characters. That
    matters for exactly the case this mode exists for — `+/-` becoming `±`
    shortens the text by two characters, and a fixed-length window would return
    a slice with two characters of the next sentence stuck on the end.

    **Boundaries are snapped to whitespace edges.** The character-count walk
    below has no notion of a word: given a quote whose leading characters were
    themselves mistranscribed, it happily walks back into the middle of one and
    returns a slice beginning `nsion of mean lifespan` — verbatim, unique, and
    unusable as a quote. Since `--refresh-quotes --write` substitutes this
    string into `data/gold/`, a slice that reads as gibberish would become
    ground truth. Both the contracted and the expanded boundary are legal, so
    all four combinations are scored and the best-matching one wins; ties go to
    the shorter slice, which is the one that over-includes least.
    """
    if not source or not quote:
        return "", 0.0, -1

    matcher = SequenceMatcher(None, source, quote, autojunk=False)
    anchor = matcher.find_longest_match(0, len(source), 0, len(quote))
    if anchor.size == 0:
        return "", 0.0, -1

    # A generous window around the anchor, so the alignment below has room to
    # find the true start and end even when lengths differ.
    slack = len(quote) // 2 + 20
    lo = max(0, anchor.a - anchor.b - slack)
    hi = min(len(source), anchor.a - anchor.b + len(quote) + slack)
    window = source[lo:hi]

    equal = [op for op in SequenceMatcher(None, window, quote, autojunk=False).get_opcodes()
             if op[0] == "equal"]
    if not equal:
        return "", 0.0, -1

    # Walk out from the first and last shared runs by however much of the quote
    # sits outside them.
    _, first_i1, _, first_j1, _ = equal[0]
    _, _, last_i2, _, last_j2 = equal[-1]
    start = max(0, first_i1 - first_j1)
    end = min(len(window), last_i2 + (len(quote) - last_j2))

    starts = {_contract_start(window, start), _expand_start(window, start)}
    ends = {_contract_end(window, end), _expand_end(window, end)}
    scored = [
        (SequenceMatcher(None, window[s:e], quote, autojunk=False).ratio(), -(e - s), s, e)
        for s in starts
        for e in ends
        if s < e
    ]
    if not scored:
        return "", 0.0, -1

    _, _, start, end = max(scored)
    candidate = window[start:end]
    ratio = SequenceMatcher(None, candidate, quote, autojunk=False).ratio()
    return candidate, ratio, lo + start


def plan_rewrite(
    location: str, quote: str, source: str, threshold: float = REFRESH_SIMILARITY
) -> QuoteRewrite:
    """Decide whether `quote` has one obvious correction in `source`.

    Three independent conditions, all of which must hold. Any one of them
    failing is a refusal, not a weaker suggestion:

    1. The closest slice is at least `threshold` similar. Below that the quote
       is not a mistranscription of anything.
    2. That slice appears exactly once in the source. A boilerplate sentence
       repeated in two sections gives no way to know which one was quoted.
    3. No *second* region matches as well. A paper that says nearly the same
       thing twice is a labelling question, not a substitution.

    Everything is compared whitespace-collapsed, matching how the check itself
    reads full text, so the replacement is a single-line string that PMC's line
    wrapping cannot disagree with.
    """
    needle = collapse_whitespace(quote)
    haystack = collapse_whitespace(source)

    candidate, ratio, at = best_slice(needle, haystack)
    if not candidate or ratio < threshold:
        return QuoteRewrite(
            location, quote, None, ratio,
            f"closest slice is only {ratio:.0%} similar; this quote is not a "
            "mistranscription of any single passage",
        )

    if splits_a_word(haystack, at, at + len(candidate)):
        # Unreachable while `best_slice` snaps to whitespace edges; kept because
        # this is the last gate before a string is written into ground truth,
        # and "the search function is correct" is not a thing to rely on twice.
        return QuoteRewrite(
            location, quote, None, ratio,
            "the closest slice begins or ends inside a word",
        )

    occurrences = haystack.count(candidate)
    if occurrences != 1:
        return QuoteRewrite(
            location, quote, None, ratio,
            f"the candidate text appears {occurrences} times in the source",
        )

    masked = haystack[:at] + "\x00" * len(candidate) + haystack[at + len(candidate):]
    _, runner_up, _ = best_slice(needle, masked)
    if runner_up >= threshold:
        return QuoteRewrite(
            location, quote, None, ratio,
            f"a second passage matches almost as well ({runner_up:.0%}); "
            "which one was quoted is a human call",
        )

    return QuoteRewrite(location, quote, candidate, ratio)


def plan_refresh(report: FileReport) -> list[QuoteRewrite]:
    """Plan a correction for every *failing* full-text quote in `report`.

    Only failing quotes, and only full-text ones. A quote that already verifies
    is character-perfect and must not be "improved". Abstract quotes are left
    alone on purpose: they are compared byte-exact including whitespace, and
    writing back a whitespace-collapsed slice would quietly demote a passing
    quote to one that only matches after normalisation.
    """
    if report.document is None or not isinstance(report.full_text, str):
        return []
    failing = {
        result.location
        for result in report.quote_results
        if result.failed and result.source == FULL_TEXT
    }
    return [
        plan_rewrite(path, quote, report.full_text)
        for path, quote, source in iter_quotes(report.document)
        if source == FULL_TEXT and path in failing
    ]


def apply_rewrites(document: dict[str, Any], rewrites: list[QuoteRewrite], source: str) -> int:
    """Replace `source_quote` strings in place. Touches nothing else.

    Deliberately narrow: it assigns to exactly one key of each matched claim
    wrapper. A `value`, a `confidence` and a `notes` are the labeller's
    judgement, and no amount of string similarity is evidence about them.

    Re-checks every candidate against `source` before writing, and raises rather
    than skipping if one is not a whole-word slice. This duplicates the gate in
    `plan_rewrite` on purpose. It is belt and braces on the one code path in
    this repo that edits ground truth, and the cost of the redundancy is a
    string search where the cost of missing it is a corrupted gold file that
    verifies clean forever after.
    """
    haystack = collapse_whitespace(source)
    for rewrite in rewrites:
        if not rewrite.unambiguous:
            continue
        at = haystack.find(rewrite.candidate)
        if at < 0:
            raise ValueError(
                f"{rewrite.location}: refusing to write a candidate that is not in the "
                f"source text: {rewrite.candidate!r}"
            )
        if splits_a_word(haystack, at, at + len(rewrite.candidate)):
            raise ValueError(
                f"{rewrite.location}: refusing to write a candidate that begins or ends "
                f"inside a word: {rewrite.candidate!r}"
            )

    by_location = {r.location: r.candidate for r in rewrites if r.unambiguous}
    changed = 0
    for path, wrapper in iter_claims(document):
        candidate = by_location.get(path)
        if candidate is not None and wrapper.get("source_quote") != candidate:
            wrapper["source_quote"] = candidate
            changed += 1
    return changed


def write_document(path: Path, document: dict[str, Any]) -> None:
    """Rewrite `path` in the gold set's own formatting.

    Same serialisation `promote_file` uses, and the gold files round-trip
    through it byte-identically, so the diff a human reviews is the changed
    quote strings and nothing else.
    """
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    path.write_text(payload)


def run_refresh(reports: list[FileReport], *, write: bool) -> int:
    """Print the refresh plan, and apply it when `write`. Returns files changed."""
    print("\nquote refresh" + ("" if write else " (dry run — nothing is written)"))
    changed_files = 0

    for report in reports:
        rewrites = plan_refresh(report)
        if not rewrites:
            continue
        print(f"\n{_display(report.path)}")
        for rewrite in rewrites:
            if rewrite.unambiguous:
                print(f"  {'rewrite' if write else 'would rewrite'}  {rewrite.location} "
                      f"({rewrite.ratio:.0%} similar)")
                for line in _rewrite_diff(rewrite):
                    print(f"      {line}")
            else:
                print(f"  {WARN}  {rewrite.location}: left alone — {rewrite.reason}")
                print(f"      current: {_ellipsis(collapse_whitespace(rewrite.current))}")

        if not write:
            continue
        assert report.document is not None and isinstance(report.full_text, str)
        applied = apply_rewrites(report.document, rewrites, report.full_text)
        if applied:
            write_document(report.path, report.document)
            changed_files += 1
            print(f"  wrote {applied} quote(s) to {_display(report.path)}")

    if not changed_files and write:
        print("  nothing was unambiguous enough to rewrite")
    return changed_files


def _rewrite_diff(rewrite: QuoteRewrite) -> list[str]:
    """Show the current string against the PMC slice, marking the first difference."""
    current = collapse_whitespace(rewrite.current)
    candidate = rewrite.candidate or ""
    index = next((i for i, (a, b) in enumerate(zip(current, candidate)) if a != b), None)
    if index is None and len(current) != len(candidate):
        index = min(len(current), len(candidate))

    lines = [f"- current: {current}", f"+ PMC:     {candidate}"]
    if index is not None:
        lines.append(f"           {' ' * index}^ first difference at char {index}: "
                     f"{_char_note(current, candidate, index, 'PMC')}")
    return lines


# --------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------


def promotion_blockers(
    report: FileReport, *, cross_failed: bool, quotes_enabled: bool
) -> list[str]:
    """Every reason this file must not be promoted. Empty means go.

    Stricter than the exit code on purpose. Exiting zero means "nothing is
    known to be wrong"; promoting means "this is ground truth now", and the
    difference is the unverified cases. A run that could not reach PubMed, was
    told not to look, or hit a paper PMC will not serve reports no failures and
    proves nothing — the verbatim quote check is the whole reason to trust a
    gold file, so promoting without it would launder an unchecked record into
    the answer key.

    Warnings do not block. They are for a human to read, and the human is
    standing right here typing --promote.
    """
    reasons: list[str] = []

    if report.failed:
        reasons.append("it did not pass its own checks")
    if cross_failed:
        reasons.append("a cross-file check failed")
    if not quotes_enabled:
        reasons.append("--no-quotes was given, so no quote was verified")

    counts = report.counts()
    if counts["skipped"]:
        reasons.append(
            f"{counts['skipped']} quote(s) could not be checked against PubMed"
        )
    if counts["unverifiable"]:
        # Same rule as the skips above, one step further out: the paper is not
        # in PMC open access, so those quotes are not checkable by anyone here.
        # That is a warning for a run and a blocker for a promotion, because
        # promoting says the record was checked.
        reasons.append(
            f"{counts['unverifiable']} full_text quote(s) are unverifiable — "
            "the paper is not in PMC open access"
        )
    verified = counts["ok"] + counts["whitespace"]
    has_quotes = report.document is not None and any(True for _ in iter_quotes(report.document))
    if has_quotes and not verified:
        reasons.append("no quote was verified against its source text")

    if in_directory(report.path, GOLD_DIR):
        reasons.append("it is already in data/gold/")

    target = GOLD_DIR / report.path.name
    if target.exists():
        # Never overwrite ground truth. A stale draft beside a finished gold
        # file is a mistake to report, not a merge to attempt.
        reasons.append(f"{_display(target)} already exists")

    return reasons


def promote_file(report: FileReport) -> Path:
    """Write the stripped document to data/gold/ and delete the draft.

    Written before the draft is unlinked, and read back before the unlink, so a
    failed or truncated write leaves the draft where it was. Losing the only
    copy of a hand-labelled record to a full disk is not an acceptable
    failure mode.
    """
    assert report.document is not None
    target = GOLD_DIR / report.path.name
    payload = json.dumps(strip_private(report.document), indent=2, ensure_ascii=False) + "\n"

    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(payload)
    if target.read_text() != payload:  # pragma: no cover - filesystem failure
        raise OSError(f"{target} did not read back as written; draft left in place")

    report.path.unlink()
    return target


def run_promotion(
    reports: list[FileReport], *, cross_failed: bool, quotes_enabled: bool
) -> int:
    """Promote every eligible file. Returns the number refused."""
    print("\npromotion")
    refused = 0
    for report in reports:
        name = _display(report.path)
        reasons = promotion_blockers(
            report, cross_failed=cross_failed, quotes_enabled=quotes_enabled
        )
        if reasons:
            refused += 1
            print(f"  {FAIL}  {name} not promoted: {'; '.join(reasons)}")
            continue
        try:
            target = promote_file(report)
        except OSError as exc:
            refused += 1
            print(f"  {FAIL}  {name} not promoted: {exc}")
            continue
        print(f"  ok    {name} -> {_display(target)} (scaffolding stripped, draft removed)")
    return refused


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def make_abstract_lookup(*, offline: bool, refresh: bool) -> Callable[[str], str | None]:
    """Return `pmid -> abstract`, cached on disk, or a cache-only stand-in.

    Built here rather than imported at module scope so that the schema and
    consistency checks — and their tests — run without the ingest runtime
    dependencies installed.
    """
    import pubmed_lookup

    def lookup(pmid: str) -> str | None:
        if offline:
            record = pubmed_lookup.read_cache(pmid)
            return record.abstract if record else None
        return pubmed_lookup.fetch_record(pmid, refresh=refresh).abstract

    return lookup


def make_biorxiv_abstract_lookup(*, offline: bool, refresh: bool) -> Callable[[str], str | None]:
    """Return `doi -> bioRxiv abstract`, cached on disk, or a cache-only stand-in.

    The bioRxiv twin of `make_abstract_lookup`, and the same protocol: `None`
    means bioRxiv has no abstract for that preprint, and anything that could not
    be determined raises, so an unreachable API is never mistaken for an answer.
    """
    import biorxiv_lookup

    def lookup(doi: str) -> str | None:
        if offline:
            record = biorxiv_lookup.read_cache(doi)
            if record is None:
                raise LookupError(
                    f"no cached bioRxiv record for {doi}; run without --offline once"
                )
            return record.abstract
        return biorxiv_lookup.fetch_record(doi, refresh=refresh).abstract

    return lookup


def make_full_text_lookup(*, offline: bool, refresh: bool) -> Callable[[str], str | None]:
    """Return `pmid -> PMC full text`, where None means "not in PMC open access".

    The two ways of having no text are kept apart by the return protocol, not by
    a flag: returning None is a *statement about the paper*, and anything that is
    merely unknown — an unreachable NCBI, an `--offline` run with nothing cached
    — raises instead. `check_file` turns the first into an unverifiable warning
    and the second into a skip, and they must not be swapped: one says "nobody
    can check this", the other says "we did not check this".
    """
    import pubmed_lookup

    def lookup(pmid: str) -> str | None:
        if offline:
            cached = pubmed_lookup.read_fulltext_cache(pmid)
            if cached is None:
                raise LookupError(
                    f"no cached PMC full text for PMID {pmid}; run without --offline once"
                )
            return cached.text
        return pubmed_lookup.fetch_full_text(pmid, refresh=refresh).text

    return lookup


def check_file(
    path: Path,
    validator,
    abstract_for: Callable[[str], str | None] | None,
    full_text_for: Callable[[str], str | None] | None = None,
    biorxiv_abstract_for: Callable[[str], str | None] | None = None,
) -> FileReport:
    """Run every per-file check. Never raises for bad input: that is a result.

    `abstract_for=None` is `--no-quotes`: the quote check does not run at all.
    `full_text_for=None` is narrower — abstract quotes are still checked, and
    full-text quotes are reported as skipped rather than silently passed.

    `paper.source` chooses which abstract a record's quotes are checked against:
    `biorxiv` uses `biorxiv_abstract_for`, keyed by DOI, and everything else uses
    `abstract_for`, keyed by PMID. Three lookups rather than one because the
    identifiers differ; the comparison they feed is the same in both cases.
    """
    report = FileReport(path=path)

    if not path.is_file():
        report.issues.append(Issue(FAIL, "file not found"))
        return report
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        report.schema_errors.append(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
        return report
    if not isinstance(document, dict):
        report.schema_errors.append(f"top level is {type(document).__name__}, expected an object")
        return report

    report.document = document
    is_gold = in_directory(path, GOLD_DIR)
    report.issues += check_private_keys(document, is_gold=is_gold)
    report.schema_errors = check_schema(document, validator, strip=not is_gold)
    report.issues += check_draft_markers(document)

    paper = document.get("paper") if isinstance(document.get("paper"), dict) else {}
    pmid = paper.get("pmid")
    doi = paper.get("doi")
    is_preprint = paper.get("source") == "biorxiv"

    if abstract_for is None:
        report.issues.append(Issue(INFO, "quote check disabled (--no-quotes)"))
        return report

    if is_preprint:
        report.quote_results = check_quotes(
            document,
            _preprint_abstract(doi, biorxiv_abstract_for, report),
            _preprint_full_text(document, report),
        )
        return report

    if not pmid:
        # A `source: pubmed` record with no PMID. Legitimate for a paper PubMed
        # has not indexed, and there is simply no abstract to check against.
        report.issues.append(Issue(WARN, "paper.pmid is null — quotes not checked against PubMed"))
        return report

    try:
        abstract = abstract_for(str(pmid))
    except Exception as exc:  # noqa: BLE001 - one unreachable paper must not abort the run
        report.issues.append(Issue(WARN, f"could not fetch abstract for PMID {pmid}: {exc}"))
        abstract = None
    else:
        if abstract is None:
            report.issues.append(
                Issue(WARN, f"no abstract available for PMID {pmid} — quotes not checked")
            )

    full_text = _full_text_for_document(document, str(pmid), full_text_for, report)
    report.full_text = full_text
    report.quote_results = check_quotes(document, abstract, full_text)
    return report


def _preprint_abstract(
    doi: Any,
    biorxiv_abstract_for: Callable[[str], str | None] | None,
    report: FileReport,
) -> str | None:
    """The bioRxiv abstract a preprint's quotes are checked against.

    A preprint is identified by DOI, not PMID, so this is the one place the two
    sources part company. Everything else about the quote check is identical:
    the same verbatim comparison against the same kind of abstract, cached the
    same way.
    """
    if not isinstance(doi, str) or not doi:
        report.issues.append(
            Issue(WARN, "paper.doi is missing — a preprint's quotes are checked by DOI")
        )
        return None
    if biorxiv_abstract_for is None:
        report.issues.append(
            Issue(WARN, "no bioRxiv lookup configured — quotes not checked against bioRxiv")
        )
        return None
    try:
        abstract = biorxiv_abstract_for(doi)
    except Exception as exc:  # noqa: BLE001 - one unreachable paper must not abort the run
        report.issues.append(Issue(WARN, f"could not fetch bioRxiv abstract for {doi}: {exc}"))
        return None
    if abstract is None:
        report.issues.append(Issue(WARN, f"no abstract available for {doi} — quotes not checked"))
    return abstract


def _preprint_full_text(document: dict[str, Any], report: FileReport) -> object | None:
    """Full-text quotes on a preprint are unverifiable, and that is not a bug.

    bioRxiv full text exists — every preprint carries a JATS XML link — but it is
    not in PMC, and PMC is what this checker verifies against. Rather than
    silently passing a quote nothing has read, or failing one that is probably
    fine, a preprint's full-text quotes are reported `unverifiable`, exactly as
    for a paper outside the PMC open-access subset. Same consequence too:
    a warning on a run, and a refusal on `--promote`.

    Returns None when the document has no full-text quotes, so a preprint
    labelled entirely from its abstract emits no warning at all.
    """
    if not any(source == FULL_TEXT for _, _, source in iter_quotes(document)):
        return None
    report.issues.append(
        Issue(WARN, "bioRxiv full text is not in PMC — full_text quotes cannot be verified")
    )
    return NOT_IN_PMC


def _full_text_for_document(
    document: dict[str, Any],
    pmid: str,
    full_text_for: Callable[[str], str | None] | None,
    report: FileReport,
) -> str | object | None:
    """Resolve the PMC text this document's full-text quotes need.

    Returns a string, `NOT_IN_PMC`, or None — the three states `check_quotes`
    distinguishes. Records a warning on the report for the two that are not a
    plain success.

    Nothing is fetched for a document with no full-text quotes. That is not only
    a saved request: it keeps a paper that happens not to be in PMC from
    emitting a warning nobody can act on, because no claim in the file depends
    on the answer.
    """
    if not any(source == FULL_TEXT for _, _, source in iter_quotes(document)):
        return None
    if full_text_for is None:
        report.issues.append(
            Issue(WARN, "no PMC full-text lookup configured — full_text quotes not checked")
        )
        return None

    try:
        text = full_text_for(pmid)
    except Exception as exc:  # noqa: BLE001 - one unreachable paper must not abort the run
        report.issues.append(Issue(WARN, f"could not fetch PMC full text for PMID {pmid}: {exc}"))
        return None
    if text is None:
        # Covers both shapes of "no": PubMed links to no PMC record at all, and
        # PMC holds a record whose efetch carries no body (a closed deposit, or
        # an old paper archived as scanned pages). Neither is checkable from
        # here, and the distinction does not change what the labeller can do.
        report.issues.append(
            Issue(WARN, f"PMID {pmid} has no PMC open-access full text")
        )
        return NOT_IN_PMC
    return text


def resolve_paths(args: argparse.Namespace) -> list[Path]:
    if args.all:
        if not GOLD_DIR.is_dir():
            raise SystemExit(f"gold directory not found: {GOLD_DIR}")
        paths = sorted(GOLD_DIR.glob("*.json"))
        if not paths:
            raise SystemExit(f"no JSON files in {GOLD_DIR.relative_to(REPO_ROOT)}/ — nothing to check")
        return paths
    return [Path(p) for p in args.files]


def render(reports: list[FileReport], cross: list[Issue], pairs: list[Issue]) -> None:
    width = max([len("FILE")] + [len(str(_display(r.path))) for r in reports])
    print(f"{'FILE'.ljust(width)}  {'SCHEMA':<8}{'QUOTES':<10}{'FULLTEXT':<10}RESULT")
    print("-" * (width + 36))

    for report in reports:
        counts = report.counts(ABSTRACT)
        checked = counts["ok"] + counts["whitespace"] + counts["fail"]
        passed = counts["ok"] + counts["whitespace"]
        schema_cell = "ok" if not report.schema_errors else f"{len(report.schema_errors)} err"
        quote_cell = f"{passed}/{checked}" if checked else ("-" if not counts["skipped"] else "skipped")
        print(
            f"{str(_display(report.path)).ljust(width)}  {schema_cell:<8}{quote_cell:<10}"
            f"{_full_text_cell(report):<10}{'FAIL' if report.failed else 'ok'}"
        )

    for report in reports:
        details = _file_details(report)
        if details:
            print(f"\n{_display(report.path)}")
            for line in details:
                print(f"  {line}")

    _render_issues("cross-file consistency", cross)
    _render_issues("consistency pairs", pairs)


def _full_text_cell(report: FileReport) -> str:
    """`verified/total`, so the column distinguishes "8 checked" from "8 present".

    The bare count this replaced was the whole problem: a file with eight
    full-text quotes and no way to check any of them printed the same `8` as one
    with eight verified quotes.
    """
    counts = report.counts(FULL_TEXT)
    total = sum(counts.values())
    return f"{counts['ok']}/{total}" if total else "-"


def _file_details(report: FileReport) -> list[str]:
    lines: list[str] = []
    for error in report.schema_errors:
        lines.append(f"{FAIL}  schema: {error}")
    for issue in report.issues:
        lines.append(f"{issue.level}  {issue.message}")
    for result in report.quote_results:
        if result.status == "fail":
            head, *rest = result.detail.split("\n")
            lines.append(f"{FAIL}  {result.location}: {head}")
            lines.extend(rest)
        elif result.status == "whitespace":
            lines.append(f"{INFO}  {result.location}: matched after whitespace normalisation")
        elif result.status == "unverifiable":
            lines.append(f"{WARN}  {result.location}: unverifiable — {result.detail}")
    return lines


def _render_issues(title: str, issues: list[Issue]) -> None:
    if not issues:
        return
    print(f"\n{title}")
    for issue in issues:
        print(f"  {issue.level}  {issue.message}")


def _display(path: Path) -> Path | str:
    try:
        return path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_gold.py",
        description="Deterministic checks on gold-set records: schema, verbatim quotes, consistency.",
    )
    parser.add_argument("files", nargs="*", help="JSON files to check")
    parser.add_argument("--all", action="store_true", help=f"check every file in {GOLD_DIR.name}/")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use only cached abstracts and full text; uncached papers are reported, not fetched",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="bypass the abstract and full-text caches"
    )
    parser.add_argument(
        "--no-quotes", action="store_true", help="skip the verbatim quote check entirely"
    )
    parser.add_argument(
        "--refresh-quotes",
        action="store_true",
        help=(
            "propose the exact PMC slice for every failing full_text quote. "
            "Dry run unless --write is also given. Human-invoked, like --promote."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="with --refresh-quotes, actually rewrite the source_quote strings",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help=(
            f"on success, strip scaffolding keys, write the file to "
            f"{GOLD_DIR.name}/ and delete the draft. Refuses on any failure."
        ),
    )
    args = parser.parse_args(argv)
    if not args.files and not args.all:
        parser.error("give one or more files, or --all")
    if args.files and args.all:
        parser.error("--all takes no file arguments")
    if args.offline and args.refresh:
        parser.error("--offline and --refresh contradict each other")
    if args.promote and args.all:
        # --all reads data/gold/; promoting it would be a no-op at best and a
        # self-overwrite at worst. Name the drafts to promote.
        parser.error("--promote takes named draft files, not --all")
    if args.write and not args.refresh_quotes:
        parser.error("--write does nothing on its own; it is the write flag for --refresh-quotes")
    if args.refresh_quotes and args.no_quotes:
        parser.error("--refresh-quotes and --no-quotes contradict each other: the "
                     "quote check is what finds the quotes to refresh")
    if args.refresh_quotes and args.promote:
        # Both write, to different places, and neither should be a side effect
        # of the other. A refreshed draft is promoted by running --promote again.
        parser.error("--refresh-quotes and --promote are separate steps; run them one at a time")
    if args.promote and args.no_quotes:
        parser.error("--promote and --no-quotes contradict each other: promoting a "
                     "record whose quotes were never checked is the one thing this "
                     "tool exists to prevent")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = resolve_paths(args)
    validator, schema_version = load_schema()

    abstract_for = full_text_for = biorxiv_abstract_for = None
    if not args.no_quotes:
        abstract_for = make_abstract_lookup(offline=args.offline, refresh=args.refresh)
        full_text_for = make_full_text_lookup(offline=args.offline, refresh=args.refresh)
        biorxiv_abstract_for = make_biorxiv_abstract_lookup(
            offline=args.offline, refresh=args.refresh
        )

    reports = [
        check_file(path, validator, abstract_for, full_text_for, biorxiv_abstract_for)
        for path in paths
    ]
    cross = check_cross_file(reports)
    pairs = check_pairs(reports)
    render(reports, cross, pairs)

    failures = [r for r in reports if r.failed]
    cross_failures = [i for i in cross + pairs if i.level == FAIL]

    if args.refresh_quotes:
        run_refresh(reports, write=args.write)

    refused = 0
    if args.promote:
        refused = run_promotion(
            reports,
            cross_failed=bool(cross_failures),
            quotes_enabled=not args.no_quotes,
        )

    print()
    print(f"schema v{schema_version}; {len(reports)} file(s) checked.")
    if failures or cross_failures:
        print(f"{len(failures)} file(s) failed, {len(cross_failures)} cross-file failure(s).")
        return 1
    if refused:
        # Every check passed and the promotion was still refused — an
        # unverified quote, or an occupied target. Not a clean run.
        print(f"{refused} file(s) not promoted.")
        return 1
    warnings = sum(1 for i in cross + pairs if i.level == WARN)
    warnings += sum(1 for r in reports for i in r.issues if i.level == WARN)
    print(f"All checks passed{f' with {warnings} warning(s)' if warnings else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
