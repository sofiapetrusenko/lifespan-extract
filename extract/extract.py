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

from extract.errors import ExperimentIdCollisionError, ExtractError, ModelResponseError
from extract.model import ModelClient, call_structured
from extract.schema import (
    CLAIM_KEYS,
    CODE_ASSIGNED_CLAIM_KEYS,
    CODE_ASSIGNED_EXPERIMENT_KEYS,
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

Only this paper's own results are records. A sentence reporting what an earlier
study found is background, not an experiment of this paper's — even when it
names an agent, an organism and a percentage. The framing is what tells them
apart. Background is written as "has been shown", "previously reported",
"studies have demonstrated", "it is known that"; this paper's own results are
written as "here we show", "we report", "in this study", "we found". In full
text a parenthetical citation is a second signal — "extended median lifespan
by 14% (Harrison et al., 2009)" is a result being repeated, not reported — but
abstracts usually carry no citations at all, so the framing is the one to read.

Every field carries the words it came from:
- source_quote is copied verbatim from the text above, as one contiguous slice
  of it — never paraphrased, never assembled from two places, never written by
  you. No ellipsis, no bracketed insertions, nothing cut from the middle. It
  need not be a whole sentence: take the span that carries the value and what
  the value refers to, which is often a clause or a table row rather than a
  sentence.
- confidence grades how plainly the text states the value, not how sure you
  are and not how far you had to reason: high when it is stated outright,
  medium when it is stated but has to be read off a table or converted into
  another unit, low when it is stated ambiguously. No level licenses recording
  a value the text does not state.

Absent data is recorded as absent, never inferred:
- If the text does not state it, it is absent — whatever the surrounding
  evidence suggests, and however confident you would be in the guess.
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
    consistent with its value, that every quote is a verbatim slice of the
    prompt the model was shown — `paper.title` and `text` — and that
    `extracted_from` on every claim is the value passed in here. Raises
    otherwise — there is no partial record.
    """
    if extracted_from not in EXTRACTED_FROM:
        raise ValueError(f"extracted_from must be one of {EXTRACTED_FROM}, got {extracted_from!r}")
    if not text or not text.strip():
        raise ExtractError(
            f"cannot extract from {paper.dedup_key}: no text was supplied."
        )

    # The one string the model is shown, and therefore the one string a quote
    # may be verbatim in. The title is part of it because an abstract often
    # does not restate the organism or the agent, leaving the title as the only
    # sentence carrying them; checking against `text` alone rejected those
    # quotes as fabrications and threw away the whole record. Built once and
    # passed to both the request and the check so the two cannot drift apart.
    prompt = f"Title: {paper.title}\n\n{text.strip()}"

    payload = call_structured(
        client,
        model=EXTRACTION_MODEL,
        system=SYSTEM_PROMPT,
        user=prompt,
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

    allocated: dict[str, tuple[str, str]] = {}
    for experiment in experiments:
        _refuse_code_assigned(experiment, CODE_ASSIGNED_EXPERIMENT_KEYS, paper)
        _stamp_provenance(experiment, extracted_from, paper)
        experiment["experiment_id"] = _experiment_id(paper, experiment, allocated)
    return experiments


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
