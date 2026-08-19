"""Structured extraction of experiment records from one paper's text.

Second stage of the cascade: the expensive model, run only on papers the gate
passed. Output is a complete `schema/experiment.schema.json` record — one
`experiments[]` entry per (organism, intervention) pair, so a multi-organism
paper yields several.

**Two requests, not one.** The endpoint compiles at most 16 union-typed
parameters in a request schema and a schema for the whole experiment object
carries 20, so extraction asks twice: once for the experiment as designed
(`IDENTITY_PROPERTIES`) and once for what it found (`OUTCOME_PROPERTIES`), at
10 union-typed parameters each. The first call establishes the list of
experiments and their order; the second is shown the same paper prompt plus a
list of what the first returned, and answers one outcome per listed experiment,
each carrying the `experiment_index` it belongs to. `_merge_outcomes` puts the
two halves back together, refusing anything that is not a one-to-one join.
The cost of this is one extra call to the expensive model per paper that passes
the gate — roughly double per extracted paper. The gate itself is unchanged, so
a screened-out paper costs exactly what it did before.

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

import json
import re
import unicodedata
from typing import Any

from extract.errors import ExperimentIdCollisionError, ExtractError, ModelResponseError
from extract.model import ModelClient, call_structured
from extract.schema import (
    CLAIM_KEYS,
    CODE_ASSIGNED_CLAIM_KEYS,
    CODE_ASSIGNED_EXPERIMENT_KEYS,
    EXPERIMENT_INDEX,
    IDENTITY_PROPERTIES,
    OUTCOME_PROPERTIES,
    build_extraction_schema,
    check_provenance,
    check_quotes_verbatim,
    iter_claims,
    load_schema,
    schema_version,
    validate_record,
)
from ingest.errors import excerpt
from ingest.models import RawPaper

EXTRACTION_MODEL = "claude-opus-5"

# A multi-organism paper can run to several thousand tokens of JSON. Comfortably
# above that, and below the point where a non-streaming request risks an HTTP
# timeout. One cap for both calls: it bounds a single response, and each of the
# two responses is now a part of what one used to return, so the headroom this
# leaves is wider than before rather than narrower. It is a ceiling, not a
# budget — nothing is spent by leaving it high.
MAX_TOKENS = 16000

ABSTRACT = "abstract"
FULL_TEXT = "full_text"
EXTRACTED_FROM = (ABSTRACT, FULL_TEXT)

# The rules that hold for both calls, written once, so the two prompts cannot
# drift apart on the guidance they share.
_OWN_RESULTS_RULE = """\
Only this paper's own results count. A sentence reporting what an earlier study
found is background, not an experiment of this paper's — even when it names an
agent, an organism and a percentage. The framing is what tells them apart.
Background is written as "has been shown", "previously reported", "studies have
demonstrated", "it is known that"; this paper's own results are written as
"here we show", "we report", "in this study", "we found". In full text a
parenthetical citation is a second signal — "extended median lifespan by 14%
(Harrison et al., 2009)" is a result being repeated, not reported — but
abstracts usually carry no citations at all, so the framing is the one to read."""

# "the paper text above" below is exact, and not to be loosened to "the text
# above". The second call's user message is the paper prompt *plus* the
# numbered echo of what the first call found, and only the paper prompt is ever
# a quote haystack (see `extract_record`) — so a quote lifted from the echo is
# rejected as a fabrication and the whole record with it. This sentence is the
# prompt-side half of that rule: it names which of the two blocks the model may
# quote from, before the call is spent.
_QUOTING_RULE = """\
Every field carries the words it came from:
- source_quote is copied verbatim from the paper text above, as one contiguous
  slice of it — never paraphrased, never assembled from two places, never
  written by you. No ellipsis, no bracketed insertions, nothing cut from the
  middle. It need not be a whole sentence: take the span that carries the value
  and what the value refers to, which is often a clause or a table row rather
  than a sentence.
- confidence grades how plainly the text states the value, not how sure you
  are and not how far you had to reason: high when it is stated outright,
  medium when it is stated but has to be read off a table or converted into
  another unit, low when it is stated ambiguously. No level licenses recording
  a value the text does not state."""

_ABSENCE_RULE = """\
Absent data is recorded as absent, never inferred:
- If the text does not state it, it is absent — whatever the surrounding
  evidence suggests, and however confident you would be in the guess.
- When a value is null or "not_reported", source_quote is null too. There is
  nothing to quote."""

IDENTITY_SYSTEM_PROMPT = f"""\
You read the text of a paper and list the lifespan-intervention experiments it
reports, describing each one as it was designed: the organism, the animals it
was run in, and the intervention. You do not report what any of them found —
a second pass does that, against the list you produce here.

Record one experiment per (organism, intervention) pair. A paper that reports
the same compound in worms, flies and mice yields three entries; a paper that
reports two compounds in one organism also yields two. Cohorts that differ only
by sex or dose belong to the same entry unless the paper reports their lifespan
results separately.

{_OWN_RESULTS_RULE}

{_QUOTING_RULE}

{_ABSENCE_RULE}
- Use null for a species, a sample size, a dose or a starting age the text does
  not give.
- Use "not_reported" for sex and strain when the text does not say. Sex is
  usually in the methods, so an abstract will often not report it.

Care with the fields that are easy to conflate:
- organism is one of the listed values; anything else is "other", and then
  species carries the actual species as free text.
"""

OUTCOME_SYSTEM_PROMPT = f"""\
You read the text of a paper and report what each of its lifespan-intervention
experiments found. The experiments have already been identified: they are
listed at the end of the user message, numbered from 0, and that list is fixed.
Return exactly one entry per listed experiment, carrying its number in
experiment_index. Do not add an experiment, drop one, merge two or reorder
them — if the paper looks to you as though it reports a different set, report
against the list anyway.

The numbered list is a summary of the previous pass, not text from the paper.
Never quote from it: every source_quote must come from the paper text that
precedes it.

{_OWN_RESULTS_RULE}

{_QUOTING_RULE}

{_ABSENCE_RULE}
- Use null for a percentage or a p-value the text does not give, and for a
  mechanism the authors do not state.

Care with the fields that are easy to conflate:
- median, mean and maximum lifespan changes are three different numbers. Fill
  in only the ones the text reports, and never move a value from one to another.
- direction is "no_effect" whenever the paper reports no significant change,
  however it is phrased ("did not significantly alter", "no effect on median
  lifespan"). Do not soften that into a small increase.
- mechanism is the mechanism the authors state, not one you infer from the
  intervention.
"""

# The header above the echoed identity block in the second call's user message.
# It says the same thing `OUTCOME_SYSTEM_PROMPT` does about not quoting from the
# block, next to the block itself, because that is where it can be acted on.
_OUTCOME_LIST_HEADER = """\
The experiments identified in this paper, numbered. Report one outcome for each,
carrying its number in experiment_index. These lines are the previous pass's
output, not text from the paper: do not quote them."""

_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")

# One run of the characters an id segment is built from. Doubles as the test
# for whether a segment carries any at all: a segment with none names nothing,
# and `_experiment_id` refuses rather than joining it into an id.
_WORD = re.compile(r"[A-Za-z0-9]+")

# What `_ascii` puts in place of a character it cannot read as ASCII. A hyphen
# because both callers already treat it as a word boundary: `_slug` keeps it and
# `_binomial_slug` splits on it.
_SEPARATOR = "-"

# The mark `_ascii` leaves where a character had no ASCII reading at all, so
# that `_split` can tell that separator from one that was only punctuation or
# whitespace. It never reaches an id — `_split` reads it and drops it — and a
# control character is used because `_ascii` maps every ASCII control character
# to `_SEPARATOR`, so a mark in the input cannot be mistaken for one of ours.
_SUBSTITUTE = "\x1a"

# Unicode categories whose members are punctuation, spacing or invisible
# formatting: the characters the trimming rule in `_split` calls incidental,
# because they surround a value rather than being part of it.
_INCIDENTAL_CATEGORIES = frozenset("PZC")

# Characters that render as another character and mean it, mapped onto the one
# spelling before anything else looks at them. Consulted first so that
# `_TRANSLITERATIONS` sees exactly one canonical form per character: the
# alternative is an entry per look-alike, which is how this function acquired a
# bug per review round. Running before NFKD is what makes it work at all —
# NFKD already folds the micro sign onto a Greek mu, so a table entry keyed on
# the micro sign could never be reached.
_CONFUSABLES: dict[str, str] = {
    # U+2206 INCREMENT is the character a chemistry paper's typesetting often
    # uses for the delta of `∆9-tetrahydrocannabinol`. It is a maths symbol
    # with no reading, so it used to become a bare separator, and the leading
    # one was then trimmed away: `∆9-…` and `9-…` became one slug while
    # `Δ9-…` became another. One compound, one spelling.
    "∆": "Δ",  # INCREMENT -> GREEK CAPITAL LETTER DELTA
    # U+00B5 MICRO SIGN and U+03BC GREEK SMALL LETTER MU are two code points
    # for one glyph, and both are typed in doses (`10 µM` / `10 μM`). They
    # canonicalise onto the Greek letter and read as `mu` — the same rule every
    # other Greek letter in the table follows — rather than as `micro` or `u`.
    # This function cannot tell a unit prefix from a letter in a name (`μ-opioid`,
    # `μ-calpain` are letters), so `micro` would be a guess that is wrong half
    # the time; and `u` is the worse failure, because a one-letter reading is
    # the one that can merge a Greek-lettered agent with an unrelated ASCII
    # spelling, which is the thing this whole function exists to prevent.
    #
    # This entry changes nothing on its own: NFKD performs the same fold one
    # step later. It is written because the fold is the reason the table has no
    # micro-sign entry, and that is worth saying where someone would otherwise
    # add one — an entry keyed on the micro sign can never be reached, and
    # `tests/test_extract.py` fails if one appears.
    "µ": "μ",  # MICRO SIGN -> GREEK SMALL LETTER MU
    # The dash family. Every one of these is an ASCII hyphen doing an ASCII
    # hyphen's job — separating words — so they are normalised to one rather
    # than each having to earn a place in the transliteration table. U+2010 is
    # the hyphen `data/gold/strong2016.json`'s source quotes actually use.
    "‐": "-",  # HYPHEN
    "‑": "-",  # NON-BREAKING HYPHEN
    "‒": "-",  # FIGURE DASH
    "–": "-",  # EN DASH
    "—": "-",  # EM DASH
    "―": "-",  # HORIZONTAL BAR
    "−": "-",  # MINUS SIGN
}

# Lowercase characters with an ASCII reading that NFKD does not give, because
# they are base letters in their own right rather than decomposable ones.
# Without this table each falls through to `_SUBSTITUTE` and the distinction it
# carries is lost — for the Greek letters, the distinction between two
# compounds. Written out per letter rather than derived from a Unicode name
# lookup, so the mapping is reviewable here and cannot shift under a Python
# upgrade.
#
# Membership is a rule, not a list of whatever has come up so far, because
# adding one letter per bug report is how a table like this stays permanently
# incomplete. It holds every letter of the Greek alphabet, and every letter of
# Latin-1 Supplement and Latin Extended-A that NFKD leaves as another letter
# instead of an ASCII base plus a combining mark — the ones that appear in
# author names, where `Søren` would otherwise slug to `s-ren` and `Yıldız` to
# `y-ld-z`. The one member of that set missing here is the micro sign, which
# `_CONFUSABLES` has already turned into a Greek mu by the time this is
# consulted. `ə` (Azerbaijani) and `ẞ` sit outside both blocks and are written
# in by hand. `tests/test_extract.py` derives the rule from Unicode and fails
# if a character in it has no reading, and fails again if an entry here has no
# effect, so the table cannot drift out of the rule in either direction.
_TRANSLITERATED: dict[str, str] = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "ο": "omicron",
    "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "sigma", "τ": "tau",
    "υ": "upsilon", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "æ": "ae", "ð": "d", "đ": "d", "ə": "e", "ħ": "h",
    "ı": "i", "ĸ": "k", "ł": "l", "ŋ": "ng", "ø": "o",
    "œ": "oe", "ß": "ss", "ŧ": "t", "þ": "th",
}

# Both cases, because `_ascii` runs before its callers lowercase. Each of the
# three conditions on the derivation excludes a real member, and none of them
# leaves a gap: `ß` upper-cases to the two-character `SS`, which no
# single-character lookup could ever match; `ĸ` has no upper case at all; and
# `ı` upper-cases to the ASCII `I`, which never reaches this table because
# `_ascii` keeps ASCII as it stands, so deriving it would leave a dead entry.
# The capital sharp s is the one capital with no lowercase counterpart to
# derive from, and is written in.
_TRANSLITERATIONS: dict[str, str] = {
    **_TRANSLITERATED,
    **{
        letter.upper(): reading.capitalize()
        for letter, reading in _TRANSLITERATED.items()
        if len(letter.upper()) == 1
        and letter.upper() != letter
        and not letter.upper().isascii()
    },
    "ẞ": "Ss",
}

_document: dict[str, Any] | None = None
_identity_schema: dict[str, Any] | None = None
_outcome_schema: dict[str, Any] | None = None


def schema_document() -> dict[str, Any]:
    """Return the parsed schema, loading it once per process.

    Cached because the same object is handed to the validator, which compiles
    it; reloading per paper would recompile per paper.
    """
    global _document
    if _document is None:
        _document = load_schema()
    return _document


def identity_schema() -> dict[str, Any]:
    """Return the first call's request schema, derived once per process.

    Cached for the reason `schema_document` is: derivation walks the whole
    schema, and doing it per paper would repeat that work per paper.
    """
    global _identity_schema
    if _identity_schema is None:
        _identity_schema = build_extraction_schema(schema_document(), IDENTITY_PROPERTIES)
    return _identity_schema


def outcome_schema() -> dict[str, Any]:
    """Return the second call's request schema, derived once per process."""
    global _outcome_schema
    if _outcome_schema is None:
        _outcome_schema = build_extraction_schema(
            schema_document(), OUTCOME_PROPERTIES, index=True
        )
    return _outcome_schema


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
    consistent with its value, that every quote is a verbatim slice of the
    paper prompt the model was shown — `paper.title` and `text` — and that
    `extracted_from` on every claim is the value passed in here. Raises
    otherwise — there is no partial record.

    **A paper whose first call succeeds and whose second fails produces no
    record and no file.** That is structural rather than cleaned up after: this
    function has no early-return path, so the only way out with a value is the
    last line, and the merged record is validated against the full real schema
    and both halves of the provenance invariant before it gets there. Nothing
    writes anything — `extract/cli.py::_write_record` is only ever reached from
    a record this function returned — so a failure between the two calls leaves
    the paper exactly as pending as it was, and the next run attempts it again.
    What it costs is the first call, which is already paid for.
    """
    if extracted_from not in EXTRACTED_FROM:
        raise ValueError(f"extracted_from must be one of {EXTRACTED_FROM}, got {extracted_from!r}")
    if not text or not text.strip():
        raise ExtractError(
            f"cannot extract from {paper.dedup_key}: no text was supplied."
        )

    # The one string of the paper the model is shown, and therefore the one
    # string a quote may be verbatim in. The title is part of it because an
    # abstract often does not restate the organism or the agent, leaving the
    # title as the only sentence carrying them; checking against `text` alone
    # rejected those quotes as fabrications and threw away the whole record.
    #
    # Built once and used as the haystack for both calls' quotes. The second
    # call's user message carries more than this — the echoed list of what the
    # first call found — and that extra text is deliberately *not* part of the
    # haystack: it is the model's own output, so a "quote" lifted from it would
    # verify while appearing nowhere in the paper, which is the fabrication the
    # check exists to catch.
    prompt = f"Title: {paper.title}\n\n{text.strip()}"

    # Before the first call, not after the last one. It reads only the ingest
    # row, so a paper missing the identity a record is built from can be
    # refused for nothing rather than after two calls to the expensive model.
    metadata = _paper_metadata(paper)

    identities = _identified_experiments(paper, prompt, client=client)
    outcomes = _reported_outcomes(paper, prompt, identities, client=client)
    experiments = _merge_outcomes(identities, outcomes, paper)
    for experiment in experiments:
        _stamp_provenance(experiment, extracted_from, paper)

    document = schema_document()
    record = {
        "schema_version": schema_version(document),
        "paper": metadata,
        "experiments": experiments,
    }
    validate_record(record, document)
    check_provenance(record)
    check_quotes_verbatim(record, prompt)
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


def _identified_experiments(
    paper: RawPaper, prompt: str, *, client: ModelClient
) -> list[dict[str, Any]]:
    """Return the first call's experiments, checked and identified.

    This is the call that fixes how many experiments the paper has and what
    order they are in; everything downstream reports against it. Each one is
    given its `experiment_id` here rather than after the merge, for two
    reasons: the id is built from this half's fields alone, and a paper whose
    ids collide is refused — so refusing it now saves the second call rather
    than paying for it first.
    """
    payload = call_structured(
        client,
        model=EXTRACTION_MODEL,
        system=IDENTITY_SYSTEM_PROMPT,
        user=prompt,
        schema=identity_schema(),
        max_tokens=MAX_TOKENS,
    )
    experiments = _experiments_array(payload, paper, stage="identifying the experiments")
    if not experiments:
        raise ExtractError(
            f"{paper.dedup_key}: the classifier passed this paper but the extractor "
            "returned no experiments. Reported rather than resolved — inventing a "
            "record here would defeat the point of the gate."
        )

    allocated: dict[str, tuple[str, str]] = {}
    for experiment in experiments:
        _refuse_code_assigned(experiment, CODE_ASSIGNED_EXPERIMENT_KEYS, paper)
        _refuse_unknown(experiment, set(IDENTITY_PROPERTIES), paper, half="identified experiment")
        _require_properties(experiment, IDENTITY_PROPERTIES, paper)
        experiment["experiment_id"] = _experiment_id(paper, experiment, allocated)
    return experiments


def _reported_outcomes(
    paper: RawPaper, prompt: str, identities: list[dict[str, Any]], *, client: ModelClient
) -> list[dict[str, Any]]:
    """Return the second call's outcomes, checked but not yet joined.

    The user message is the paper prompt plus the numbered list of what the
    first call found. Only the paper prompt is ever a quote haystack; see
    `extract_record`.
    """
    payload = call_structured(
        client,
        model=EXTRACTION_MODEL,
        system=OUTCOME_SYSTEM_PROMPT,
        user=_outcome_prompt(prompt, identities),
        schema=outcome_schema(),
        max_tokens=MAX_TOKENS,
    )
    outcomes = _experiments_array(payload, paper, stage="reporting the outcomes")
    for outcome in outcomes:
        _refuse_code_assigned(outcome, CODE_ASSIGNED_EXPERIMENT_KEYS, paper)
        _refuse_unknown(outcome, {EXPERIMENT_INDEX, *OUTCOME_PROPERTIES}, paper, half="outcome")
        _require_properties(outcome, OUTCOME_PROPERTIES, paper)
    return outcomes


def _experiments_array(
    payload: dict[str, Any], paper: RawPaper, *, stage: str
) -> list[dict[str, Any]]:
    """Return `payload["experiments"]`, raising unless it is an array of objects."""
    experiments = payload.get("experiments")
    if not isinstance(experiments, list) or not all(isinstance(e, dict) for e in experiments):
        raise ModelResponseError.from_payload(
            reason=f"model payload has no 'experiments' array of objects while {stage}",
            payload=repr(payload),
        )
    return experiments


def _outcome_prompt(prompt: str, identities: list[dict[str, Any]]) -> str:
    """Return the second call's user message: the paper, then what call one found.

    The echoed lines carry values only — no quotes and no confidences — because
    their whole job is to let the model tell one numbered experiment from
    another. Which values that is is read off the claims themselves rather than
    from a list written here, so a claim added to the schema is echoed without
    an edit; two arms of one intervention often differ only in `sex` or
    `intervention.dose`, and a hand-written list that missed one would leave
    them indistinguishable.
    """
    lines = []
    for index, experiment in enumerate(identities):
        values = "; ".join(
            f"{path}={json.dumps(claim.get('value'), ensure_ascii=False)}"
            for path, claim in iter_claims(experiment)
        )
        lines.append(f"{index}. {values}")
    return f"{prompt}\n\n{_OUTCOME_LIST_HEADER}\n" + "\n".join(lines)


def _merge_outcomes(
    identities: list[dict[str, Any]], outcomes: list[dict[str, Any]], paper: RawPaper
) -> list[dict[str, Any]]:
    """Return the identified experiments with their outcome fields merged in.

    The join is `experiment_index`, and it is checked before it is used: the
    multiset of indices returned must be exactly {0 … N-1} for the N
    experiments the first call listed. That one condition is what rejects a
    dropped outcome, a duplicated index, more outcomes than experiments and an
    index naming no experiment — each of which would otherwise land silently as
    a record missing a result, or as two experiments sharing one.

    The field-wise merge then asserts that no target field is already set and
    that none is left unset. Both are guards on this function and on the
    constants it merges by, *given* that `_identified_experiments` refuses an
    identity response carrying an outcome property. Without that refusal the
    "already set" arm was reachable from model output — `_refuse_unknown` ran
    on the outcome half only, so a first call volunteering `mechanism` or
    `lifespan_effect`, both real `experiment` properties and neither pruned as
    code-assigned, arrived here untouched and was reported as a partition fault
    it had nothing to do with. It is refused one layer up now, and what is left
    for this guard is the fault it was written for: `IDENTITY_PROPERTIES` and
    `OUTCOME_PROPERTIES` are meant to partition the experiment object, and an
    overlap introduced in a later schema version would show up here as a field
    already set rather than as one half of the record quietly overwriting the
    other.
    """
    expected = list(range(len(identities)))
    indices = [_outcome_index(outcome, paper) for outcome in outcomes]
    if sorted(indices) != expected:
        raise ModelResponseError.from_payload(
            reason=(
                f"{paper.dedup_key}: the outcomes do not correspond one-to-one with "
                f"the {len(identities)} experiment(s) identified — "
                f"{_index_fault(indices, len(identities))}. Expected "
                f"experiment_index {expected}, got {indices}"
            ),
            payload=repr(outcomes),
        )

    for index, outcome in zip(indices, outcomes):
        target = identities[index]
        for name in OUTCOME_PROPERTIES:
            if name in target:
                raise ExtractError(
                    f"{paper.dedup_key}: experiment {index} already carries {name!r} "
                    "before its outcome was merged in. A response volunteering an "
                    "outcome property on the identity half is refused upstream by "
                    "`_identified_experiments`, so what is left is the partition "
                    "itself: IDENTITY_PROPERTIES and OUTCOME_PROPERTIES both name "
                    f"{name!r}, the two request schemas overlap, and one call's answer "
                    "would silently overwrite the other's. Fix the two constants.\n"
                    "  outcome being merged in:\n"
                    f"{excerpt(repr(outcome))}"
                )
            target[name] = outcome[name]

    unset = [
        (index, name)
        for index, target in enumerate(identities)
        for name in OUTCOME_PROPERTIES
        if name not in target
    ]
    if unset:
        raise ExtractError(
            f"{paper.dedup_key}: after merging, {unset} are still missing from the "
            "record. Every experiment must carry every outcome field before the "
            "record is assembled."
        )
    return identities


def _outcome_index(outcome: dict[str, Any], paper: RawPaper) -> int:
    """Return an outcome's `experiment_index`, raising unless it is an integer.

    `bool` is excluded explicitly. It is a subclass of `int` in Python, so a
    JSON `true` would otherwise be a perfectly good index 1 and attach a
    paper's results to the wrong experiment.
    """
    index = outcome.get(EXPERIMENT_INDEX)
    if isinstance(index, bool) or not isinstance(index, int):
        raise ModelResponseError.from_payload(
            reason=(
                f"{paper.dedup_key}: outcome has {EXPERIMENT_INDEX}={index!r}, which is "
                "not an integer, so there is no experiment it can be attached to"
            ),
            payload=repr(outcome),
        )
    return index


def _index_fault(indices: list[int], count: int) -> str:
    """Name what is wrong with a set of indices, for the error above."""
    faults = []
    outside = sorted({index for index in indices if not 0 <= index < count})
    repeated = sorted({index for index in indices if indices.count(index) > 1})
    missing = sorted(set(range(count)) - set(indices))
    if outside:
        faults.append(f"{outside} name no experiment")
    if repeated:
        faults.append(f"{repeated} appear more than once")
    if missing:
        faults.append(f"{missing} have no outcome")
    return "; ".join(faults) or "the count does not match"


def _require_properties(
    node: dict[str, Any], properties: tuple[str, ...], paper: RawPaper
) -> None:
    """Raise if `node` omits a property its request schema marked required.

    The complement of `_refuse_code_assigned`: that one refuses a key the
    request did not ask for, this one refuses the absence of one it did. Most
    of these fields are optional in the real schema, so an omission validates
    perfectly well and lands as a record that is silently missing `sex` — which
    is not the same claim as `sex: not_reported`, and Phase 3 would score the
    difference as if the model had been asked.
    """
    absent = [name for name in properties if name not in node]
    if absent:
        raise ModelResponseError.from_payload(
            reason=(
                f"{paper.dedup_key}: model omitted {absent}, which the request schema "
                "requires"
            ),
            payload=repr(node),
        )


def _refuse_unknown(
    node: dict[str, Any], allowed: set[str], paper: RawPaper, *, half: str
) -> None:
    """Raise if `node` carries a key outside `allowed`.

    Run on both halves, because each half has its own way of swallowing a key
    the request never asked for, and neither is caught anywhere else:

    * On an **outcome**, an unknown key is dropped by the field-wise merge,
      which copies the fields it knows about and never mentions the rest.
    * On an **identified experiment**, whether anything notices depends on
      *which* key it is. A key that is no property of the schema's `experiment`
      definition survives into the assembled record and is refused there —
      `experiment` closes with `additionalProperties: false`, and the nine
      claim wrappers in `schema/experiment.schema.json` close with
      `unevaluatedProperties: false`, which is what they need instead because
      they compose `$defs/provenance` through `allOf`. But a key that *is* a
      real experiment property and simply belongs to the other half —
      `mechanism` or `lifespan_effect`, volunteered by a first call that was
      not asked for them — never reaches `validate_record` at all: the merge
      hits it first and reports it as a partition fault, which is not what
      happened. Refusing it here names the actual cause, at the layer where the
      payload is still in hand to excerpt.

    `half` names which response the key arrived in, because the two are two
    separate calls to the model and an operator has to know which one to read.
    """
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise ModelResponseError.from_payload(
            reason=(
                f"{paper.dedup_key}: model supplied {unknown} on an {half}, which the "
                "request schema for that call does not declare"
            ),
            payload=repr(node),
        )


def _refuse_code_assigned(node: dict[str, Any], keys: frozenset[str], paper: RawPaper) -> None:
    """Raise if `node` carries a key the caller assigns rather than the model.

    `extract.schema` prunes every one of these keys from the request schema, so
    a response carrying one did not follow the schema it was given — and each
    of the quiet ways of continuing hides that. Overwriting `extracted_from`
    discards a provenance claim the model made; overwriting `experiment_id`
    discards an identity it made; and keeping `notes` persists an
    unprovenanced, never-scored string into the one field documented as human
    annotation. The key sets are imported rather than restated here, so a field
    added there cannot be left unguarded.
    """
    supplied = sorted(keys & set(node))
    if supplied:
        names = ", ".join(repr(key) for key in supplied)
        raise ModelResponseError.from_payload(
            reason=(
                f"{paper.dedup_key}: model supplied {names}, which is assigned by "
                "the caller and is not in the request schema"
            ),
            payload=repr(node),
        )


def _stamp_provenance(node: Any, extracted_from: str, paper: RawPaper) -> None:
    """Write `extracted_from` into every claim wrapper under `node`, in place.

    Raises if the model supplied the key itself, for the reason
    `_refuse_code_assigned` gives.
    """
    if isinstance(node, dict):
        if CLAIM_KEYS <= set(node):
            _refuse_code_assigned(node, CODE_ASSIGNED_CLAIM_KEYS, paper)
            node["extracted_from"] = extracted_from
            return
        for child in node.values():
            _stamp_provenance(child, extracted_from, paper)
    elif isinstance(node, list):
        for child in node:
            _stamp_provenance(child, extracted_from, paper)


def _experiment_id(
    paper: RawPaper, experiment: dict[str, Any], allocated: dict[str, tuple[str, str]]
) -> str:
    """Return this experiment's id, following the schema's naming convention.

    `<first-author-year>-<organism-slug>-<agent-slug>`, with a `-2`, `-3` … tail
    when one paper reports the same (organism, agent) pair more than once.

    `allocated` maps every id handed out for this paper so far to the
    (organism, agent) values it was built from, and this call records its own
    id in it. It is what makes the numeric tail safe: the tail is a legitimate
    way to tell two arms of one intervention apart — male and female rapamycin
    in `data/gold/harrison2009.json` — and never a legitimate way to tell two
    different interventions apart. When a second pair of values would take an
    id the first pair already holds, this raises instead of appending a tail,
    because the id is the key Phase 3 aligns gold against extracted output by,
    and one that silently covers two compounds scores them as one.
    """
    if not paper.first_author:
        raise ExtractError(
            f"{paper.dedup_key}: no first author, so the experiment_id convention "
            "<first-author-year>-<organism>-<agent> cannot be followed. Collective "
            "authorships hit this; skip the paper rather than naming it something else."
        )

    # Checked on its own rather than through `parts`, where the year keeps the
    # stem truthy however little the author contributed: a surname in a script
    # with no ASCII reading left `2016-mmusculus-rapamycin`, a well-formed id
    # every same-year paper would collide on, and it passed silently.
    author = _slug(paper.first_author)
    if not _WORD.search(author):
        raise ExtractError(
            f"{paper.dedup_key}: first author {paper.first_author!r} has no ASCII "
            "reading, so the <first-author-year> stem would be the year alone and "
            "papers from that year would share it. As with a missing author, skip "
            "the paper rather than naming it something else."
        )

    organism, organism_source = _organism_identity(experiment)
    agent_source = _claim_value(experiment, ("intervention", "agent"))
    parts = [f"{author}{paper.year}", organism, _slug(agent_source)]
    if not all(_WORD.search(part) for part in parts):
        raise ExtractError(
            f"{paper.dedup_key}: cannot build an experiment_id from parts {parts}; "
            "the organism or agent came back empty or unusable as an identifier."
        )

    stem = "-".join(parts)
    source = (organism_source, agent_source)
    candidate, suffix = stem, 1
    while candidate in allocated:
        if allocated[candidate] != source:
            held_organism, held_agent = allocated[candidate]
            raise ExperimentIdCollisionError(
                f"{paper.dedup_key}: ({held_organism!r}, {held_agent!r}) and "
                f"({organism_source!r}, {agent_source!r}) are different experiments "
                f"and both generate the experiment_id {candidate!r}. A numeric tail "
                "would be the only thing separating them, which is no separation at "
                "all downstream. Refusing the paper: give the two the names the "
                "paper gives them, or teach `_TRANSLITERATIONS` the character that "
                "made them alike."
            )
        suffix += 1
        candidate = f"{stem}-{suffix}"
    if not _ID_PATTERN.match(candidate):
        raise ExtractError(
            f"{paper.dedup_key}: generated experiment_id {candidate!r} does not match "
            "the schema's identifier pattern."
        )
    allocated[candidate] = source
    return candidate


def _organism_identity(experiment: dict[str, Any]) -> tuple[str, str]:
    """Return the organism component of an id and the value it was read from.

    `organism` collapses everything outside the MVP enum to `other`, which would
    give a multi-organism paper several identical ids; `species` is what tells
    the yeast record from the fly one.

    The value comes back alongside the slug because `_experiment_id` compares
    the values two ids were built from, not the slugs: two slugs being equal is
    the collision, so they cannot also be the evidence about whether it matters.
    """
    organism = _claim_value(experiment, ("organism",))
    if organism != "other":
        return _binomial_slug(organism), organism
    species = _claim_value(experiment, ("species",), required=False)
    if species:
        return _binomial_slug(species), species
    return "other", organism


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
    leading, words, trailing = _split(_ascii(value))
    return leading + _SEPARATOR.join(word.lower() for word in words) + trailing


def _binomial_slug(value: str) -> str:
    """Slug for a species name: 'M. musculus' -> 'mmusculus'.

    An abbreviated genus joins the epithet without a separator, matching the
    ids in `data/gold/` (`eisenberg2009-scerevisiae-spermidine`). Names that are
    not binomials fall through to ordinary hyphenation.
    """
    leading, words, trailing = _split(_ascii(value))
    merged: list[str] = []
    for token in (word.lower() for word in words):
        if merged and len(merged[-1]) == 1:
            merged[-1] += token
        else:
            merged.append(token)
    return leading + _SEPARATOR.join(merged) + trailing


def _split(rendering: str) -> tuple[str, list[str], str]:
    """Split an `_ascii` rendering into its words and the end separators it keeps.

    Guarantees an end separator survives exactly when a character with no ASCII
    reading stood there, and never otherwise — which is a distinction a
    `.strip()` cannot make, and getting it wrong in either direction is a bug
    with a history here. Stripping every end separator lets the deletion this
    module exists to prevent back in through the boundary: `∆9-…` became `9-…`,
    which a compound literally named `9-…` then collides with. Keeping every
    end separator would instead put one on `  rapamycin  ` and on a quoted
    `“rapamycin”`, and an id built from those matches no other spelling of the
    same agent and no longer matches `_ID_PATTERN`. Spaces, quotes and commas
    around a value make no claim about its content; an untransliterable
    character in the middle of it does.

    Gaps *between* words collapse to one separator whatever they were made of,
    so the U+2010 and ASCII-hyphen spellings of one compound land alike.
    """
    words = list(_WORD.finditer(rendering))
    if not words:
        # Nothing to hang a boundary off. The caller checks for this: a slug
        # with no word in it cannot name anything, and is refused rather than
        # joined into an id.
        return "", [], _SEPARATOR if _SUBSTITUTE in rendering else ""
    leading = _SEPARATOR if _SUBSTITUTE in rendering[: words[0].start()] else ""
    trailing = _SEPARATOR if _SUBSTITUTE in rendering[words[-1].end() :] else ""
    return leading, [word.group() for word in words], trailing


def _ascii(value: str) -> str:
    """Return `value` as ASCII, transliterating what it can and never deleting.

    Deleting a character is the one thing this must not do. Agent names in this
    field are chemistry, where a single Greek letter is the whole distinction:
    dropping it turned `17-α-estradiol` and `17-β-estradiol` into one slug, so
    two different compounds in one paper collapsed onto one `experiment_id`
    separated only by an anonymous `-2` tail. `data/gold/strong2016.json` is
    exactly that paper. Dropping punctuation is the same bug quieter — with the
    U+2010 hyphens that paper's quotes actually use, `17‐α‐estradiol` lost its
    word boundaries too and became `17estradiol`.

    So every character leaves a mark, in this order:

    0. `_CONFUSABLES` first, before anything can hide a look-alike behind its
       decomposition: one spelling per character reaches the steps below.
    1. NFKD, which folds accented Latin letters onto a base letter plus a
       combining mark.
    2. Combining marks are dropped — they are the accent that step 1 just
       separated off, and `é` is wanted as `e`, not as `e-`.
    3. ASCII is kept as it stands, except for the control characters, which are
       word boundaries and nothing else.
    4. Anything in `_TRANSLITERATIONS` becomes its ASCII reading, so a Greek
       letter survives as its name: `17-α-estradiol` -> `17-alpha-estradiol`,
       the id `schema/experiment.schema.json` documents as its example.
    5. Non-ASCII punctuation and spacing become the separator: they are word
       boundaries in the same way their ASCII counterparts are.
    6. Everything else — a letter, digit or symbol from a script with no
       reading here — becomes `_SUBSTITUTE`. It is a word boundary too, and
       `_split` turns it into one; what it additionally records is that a
       character *was there*, so a boundary at either end of the value cannot
       be trimmed off as though it were only punctuation.

    The returned string is ASCII but not yet a slug: it still holds the
    original ASCII punctuation and any `_SUBSTITUTE` marks, both of which
    `_split` resolves.
    """
    canonical = "".join(_CONFUSABLES.get(character, character) for character in value)
    folded: list[str] = []
    for character in unicodedata.normalize("NFKD", canonical):
        if character.isascii():
            folded.append(character if character.isprintable() else _SEPARATOR)
        elif unicodedata.combining(character):
            continue
        elif character in _TRANSLITERATIONS:
            folded.append(_TRANSLITERATIONS[character])
        elif unicodedata.category(character)[0] in _INCIDENTAL_CATEGORIES:
            folded.append(_SEPARATOR)
        else:
            folded.append(_SUBSTITUTE)
    return "".join(folded)
