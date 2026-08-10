#!/usr/bin/env python3
"""Deterministic checks on gold-set records. No model calls, no judgement.

Usage:
    python scripts/check_gold.py --all
    python scripts/check_gold.py data/drafts/harrison2009.json
    python scripts/check_gold.py --all --offline

Four checks, in order of how much they cost to run:

a. **Schema.** Delegated to `scripts/validate_gold.py` so there is one
   validator, not two. Top-level `_`-prefixed keys are stripped first: that is
   what lets a scaffolded draft carry `_abstract` for the labeller to read and
   still be checked against a schema whose root is `additionalProperties:
   false`.
b. **Verbatim quotes.** Every `source_quote` marked `extracted_from: abstract`
   must appear character-for-character in the paper's PubMed abstract. This is
   the check that makes the gold set trustworthy — a quote that is not in the
   paper is a fabricated citation, whoever wrote it. Quotes marked `full_text`
   are *not* checked and are counted separately: the abstract is not the text
   they came from, and failing them would only teach the reader to ignore this
   tool. Verifying those needs PMC full text, which is out of scope here.
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


@dataclass
class Issue:
    level: str
    message: str


@dataclass
class QuoteResult:
    location: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass
class FileReport:
    path: Path
    document: dict[str, Any] | None = None
    schema_errors: list[str] = field(default_factory=list)
    quote_results: list[QuoteResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

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

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = defaultdict(int)
        for result in self.quote_results:
            tally[result.status] += 1
        return tally


# --------------------------------------------------------------------------
# document traversal
# --------------------------------------------------------------------------


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


def check_schema(document: dict[str, Any], validator) -> list[str]:
    errors = sorted(validator.iter_errors(strip_private(document)), key=sort_key)
    return [f"{format_path(error)}: {error.message}" for error in errors]


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


def check_quotes(document: dict[str, Any], abstract: str | None) -> list[QuoteResult]:
    """Check every abstract-sourced quote against `abstract`.

    `abstract=None` means the abstract could not be obtained; every quote is
    marked skipped rather than passed, so an unreachable PubMed never reads as
    a clean run.
    """
    results: list[QuoteResult] = []
    for path, wrapper in iter_claims(document):
        quote = wrapper.get("source_quote")
        if not isinstance(quote, str) or not quote:
            continue
        if wrapper.get("extracted_from") != "abstract":
            results.append(QuoteResult(path, "full_text"))
            continue
        if abstract is None:
            results.append(QuoteResult(path, "skipped"))
            continue
        results.append(_match_quote(path, quote, abstract))
    return results


def _match_quote(path: str, quote: str, abstract: str) -> QuoteResult:
    if quote in abstract:
        return QuoteResult(path, "ok")

    # A structured PubMed abstract is reassembled from labelled sections, so it
    # carries newlines an author transcribing a sentence would not. Treating a
    # whitespace-only difference as a pass is not a loosening of "verbatim":
    # no word, character or punctuation mark differs. It is still reported, so
    # the labeller can tidy it if they want to.
    if collapse_whitespace(quote) in collapse_whitespace(abstract):
        return QuoteResult(path, "whitespace")

    ratio, window, index = closest_match(quote, abstract)
    return QuoteResult(path, "fail", _describe_mismatch(quote, window, ratio, index))


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


def _describe_mismatch(quote: str, window: str, ratio: float, index: int | None) -> str:
    lines = [f"not verbatim in the abstract (closest match {ratio:.0%})"]
    if index is None:
        lines.append(f"    gold:     {_ellipsis(quote)}")
        lines.append(f"    abstract: {_ellipsis(window)}")
        return "\n".join(lines)

    radius = 45
    start = max(0, index - radius)
    gold_slice = quote[start : index + radius]
    abstract_slice = window[start : index + radius]
    caret = " " * (index - start) + "^"
    lines.append(f"    gold:     {_prefix(start)}{gold_slice}")
    lines.append(f"    abstract: {_prefix(start)}{abstract_slice}")
    lines.append(f"              {' ' * len(_prefix(start))}{caret} first difference at char {index}")
    lines.append(f"              {_char_note(quote, window, index)}")
    return "\n".join(lines)


def _char_note(quote: str, window: str, index: int) -> str:
    got = quote[index] if index < len(quote) else "<end of quote>"
    expected = window[index] if index < len(window) else "<end of abstract window>"
    return f"gold has {got!r}, abstract has {expected!r}"


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


def check_file(
    path: Path,
    validator,
    abstract_for: Callable[[str], str | None] | None,
) -> FileReport:
    """Run every per-file check. Never raises for bad input: that is a result."""
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
    report.schema_errors = check_schema(document, validator)
    report.issues += check_draft_markers(document)

    paper = document.get("paper")
    pmid = paper.get("pmid") if isinstance(paper, dict) else None

    if abstract_for is None:
        report.issues.append(Issue(INFO, "quote check disabled (--no-quotes)"))
        return report
    if not pmid:
        # Legitimate for a preprint: the schema allows a null PMID. There is
        # simply no PubMed abstract to check against, and saying so is more
        # useful than counting the quotes as passed.
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

    report.quote_results = check_quotes(document, abstract)
    return report


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
        counts = report.counts()
        checked = counts["ok"] + counts["whitespace"] + counts["fail"]
        passed = counts["ok"] + counts["whitespace"]
        schema_cell = "ok" if not report.schema_errors else f"{len(report.schema_errors)} err"
        quote_cell = f"{passed}/{checked}" if checked else ("-" if not counts["skipped"] else "skipped")
        print(
            f"{str(_display(report.path)).ljust(width)}  {schema_cell:<8}{quote_cell:<10}"
            f"{counts['full_text'] or '-':<10}{'FAIL' if report.failed else 'ok'}"
        )

    for report in reports:
        details = _file_details(report)
        if details:
            print(f"\n{_display(report.path)}")
            for line in details:
                print(f"  {line}")

    _render_issues("cross-file consistency", cross)
    _render_issues("consistency pairs", pairs)


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
        help="use only cached abstracts; uncached papers are reported, not fetched",
    )
    parser.add_argument("--refresh", action="store_true", help="bypass the abstract cache")
    parser.add_argument(
        "--no-quotes", action="store_true", help="skip the verbatim quote check entirely"
    )
    args = parser.parse_args(argv)
    if not args.files and not args.all:
        parser.error("give one or more files, or --all")
    if args.files and args.all:
        parser.error("--all takes no file arguments")
    if args.offline and args.refresh:
        parser.error("--offline and --refresh contradict each other")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = resolve_paths(args)
    validator, schema_version = load_schema()

    abstract_for = (
        None
        if args.no_quotes
        else make_abstract_lookup(offline=args.offline, refresh=args.refresh)
    )

    reports = [check_file(path, validator, abstract_for) for path in paths]
    cross = check_cross_file(reports)
    pairs = check_pairs(reports)
    render(reports, cross, pairs)

    failures = [r for r in reports if r.failed]
    cross_failures = [i for i in cross + pairs if i.level == FAIL]

    print()
    print(f"schema v{schema_version}; {len(reports)} file(s) checked.")
    if failures or cross_failures:
        print(f"{len(failures)} file(s) failed, {len(cross_failures)} cross-file failure(s).")
        return 1
    warnings = sum(1 for i in cross + pairs if i.level == WARN)
    warnings += sum(1 for r in reports for i in r.issues if i.level == WARN)
    print(f"All checks passed{f' with {warnings} warning(s)' if warnings else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
